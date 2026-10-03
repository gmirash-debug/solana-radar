import { historyEventId, checkedHistoryDb } from "./history.js";
import { resumeHistoryEvent, HistoryYield, minimumHistoryWork, minimumProgressWork, estimatedHistoryQueries, historyInfrastructure } from "./history-progress.js";

// Parent integration (no public DO route): export {HistoryQueue} from index.js;
// bind HISTORY_QUEUE to HistoryQueue and append a new_sqlite_classes migration.
// Await enqueueDurableHistory before acknowledging an authenticated ingest.
// Invoke flushDurableHistory from the existing bounded background scheduler.
export const HISTORY_QUEUE_LIMITS = Object.freeze({
  enqueueEvents: 25, flushEvents: 25, flushQueries: 40, eventBytes: 128 * 1024, requestBytes: 1024 * 1024,
  // Metadata only: retain 30 days even at 5,000 minimum-cost events/day,
  // including UTC-boundary bursts and pending work. Payload caps stay unchanged.
  pendingRows: 2048, pendingBytes: 16 * 1024 * 1024, receiptRows: 160000,
  receiptRetentionMs: 30 * 86400_000, leaseMs: 10 * 60_000,
  dailyDoWriteUnits: 50000, dailyHistoryWriteUnits: 80000, flushHistoryWriteUnits: 40000,
  scheduledFlushes: 2,
});
const ENCODER = new TextEncoder();
const QUEUE_NAME = "history-events-v1";
const WRITE_UNITS_PER_STATEMENT = 8; // Includes headroom for existing table indexes.

class QueueError extends Error {
  constructor(message, status = 400) { super(message); this.status = status; }
}

function lowered(env, name, maximum) {
  if (env?.[name] === undefined) return maximum;
  const value = Number(env[name]);
  if (!Number.isInteger(value) || value < 1 || value > maximum) throw new QueueError(`invalid_${name}`);
  return value;
}

function sourceTime(value) {
  if (typeof value !== "string" || !/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return NaN;
  return Date.parse(value);
}

function assertJson(value, seen = new Set()) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return;
  if (typeof value === "number" && Number.isFinite(value)) return;
  if (typeof value !== "object" || seen.has(value)) throw new QueueError("history_event_not_json");
  if (!Array.isArray(value) && Object.getPrototypeOf(value) !== Object.prototype) throw new QueueError("history_event_not_plain_json");
  seen.add(value);
  for (const child of Object.values(value)) assertJson(child, seen);
  seen.delete(value);
}

export function validateHistoryEvents(events, now = Date.now(), eventBytes = HISTORY_QUEUE_LIMITS.eventBytes) {
  if (!Array.isArray(events) || events.length > HISTORY_QUEUE_LIMITS.enqueueEvents) throw new QueueError("history_batch_max_25");
  return events.map(event => {
    assertJson(event);
    if (!event || Array.isArray(event) || !event.episode || !event.event) throw new QueueError("history_event_shape_required");
    const episode = event.episode;
    const sourceAt = sourceTime(event.event.observed_at);
    const caughtAt = sourceTime(episode.caught_at);
    if (typeof episode.episode_id !== "string" || !episode.episode_id.trim() || episode.episode_id.length > 240
        || typeof episode.token_address !== "string" || !episode.token_address.trim()
        || typeof event.event.event_type !== "string" || !event.event.event_type.trim()
        || !Number.isFinite(sourceAt) || !Number.isFinite(caughtAt) || sourceAt < caughtAt || sourceAt > now + 300000) {
      throw new QueueError("history_source_identity_or_time_invalid");
    }
    if (event.wallets !== undefined && !Array.isArray(event.wallets)) throw new QueueError("history_wallets_must_be_array");
    const id = historyEventId(event);
    if (!id || id.length > 240 || /[\u0000-\u001f]/.test(id)) throw new QueueError("history_event_id_invalid");
    const json = JSON.stringify(event);
    const bytes = ENCODER.encode(json).byteLength;
    if (bytes > eventBytes) throw new QueueError("history_event_oversize", 413);
    return { id, episodeId: episode.episode_id, sourceAt, json, bytes };
  });
}

async function readRequest(request) {
  if (Number(request.headers.get("content-length")) > HISTORY_QUEUE_LIMITS.requestBytes) throw new QueueError("history_request_oversize", 413);
  if (!request.body) throw new QueueError("history_request_body_required");
  const reader = request.body.getReader();
  const chunks = [];
  let bytes = 0;
  while (true) {
    const item = await reader.read();
    if (item.done) break;
    bytes += item.value.byteLength;
    if (bytes > HISTORY_QUEUE_LIMITS.requestBytes) {
      await reader.cancel();
      throw new QueueError("history_request_oversize", 413);
    }
    chunks.push(item.value);
  }
  const joined = new Uint8Array(bytes);
  let offset = 0;
  for (const chunk of chunks) { joined.set(chunk, offset); offset += chunk.byteLength; }
  try { return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(joined)); }
  catch { throw new QueueError("history_request_invalid_json"); }
}

export class HistoryQueue {
  constructor(ctx, env = {}) {
    this.ctx = ctx;
    this.env = env;
    this.sql = ctx.storage.sql;
    this.inFlight = null;
    this.maxRows = lowered(env, "HISTORY_QUEUE_MAX_PENDING_ROWS", HISTORY_QUEUE_LIMITS.pendingRows);
    this.maxBytes = lowered(env, "HISTORY_QUEUE_MAX_PENDING_BYTES", HISTORY_QUEUE_LIMITS.pendingBytes);
    this.maxReceipts = lowered(env, "HISTORY_QUEUE_MAX_RECEIPT_ROWS", HISTORY_QUEUE_LIMITS.receiptRows);
    this.eventBytes = lowered(env, "HISTORY_QUEUE_MAX_EVENT_BYTES", HISTORY_QUEUE_LIMITS.eventBytes);
    this.doBudget = lowered(env, "HISTORY_QUEUE_DAILY_WRITE_UNITS", HISTORY_QUEUE_LIMITS.dailyDoWriteUnits);
    this.historyBudget = lowered(env, "HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS", HISTORY_QUEUE_LIMITS.dailyHistoryWriteUnits);
    this.infra = historyInfrastructure(env);
    ctx.storage.transactionSync(() => {
      this.sql.exec(`CREATE TABLE IF NOT EXISTS history_queue_events (
        event_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, source_at INTEGER NOT NULL,
        payload_json TEXT, payload_bytes INTEGER NOT NULL, status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL,
        lease_token TEXT, delivered_at INTEGER, last_error TEXT,
        CHECK(status IN ('pending', 'delivered')))`);
      this.sql.exec(`CREATE INDEX IF NOT EXISTS history_queue_due
        ON history_queue_events(status, next_attempt_at, source_at, event_id)`);
      this.sql.exec(`CREATE INDEX IF NOT EXISTS history_queue_episode
        ON history_queue_events(status, episode_id, source_at, event_id)`);
      this.sql.exec(`CREATE INDEX IF NOT EXISTS history_queue_receipts
        ON history_queue_events(status, delivered_at, event_id)`);
      this.sql.exec(`CREATE TABLE IF NOT EXISTS history_queue_meta (
        id INTEGER PRIMARY KEY CHECK(id=1), pending_rows INTEGER NOT NULL DEFAULT 0,
        pending_bytes INTEGER NOT NULL DEFAULT 0, delivered_rows INTEGER NOT NULL DEFAULT 0,
        do_day TEXT NOT NULL DEFAULT '', do_writes INTEGER NOT NULL DEFAULT 0,
        hist_day TEXT NOT NULL DEFAULT '', hist_writes INTEGER NOT NULL DEFAULT 0)`);
      this.sql.exec("INSERT OR IGNORE INTO history_queue_meta(id) VALUES (1)");
      const eventColumns = new Set(this.rows("PRAGMA table_info(history_queue_events)").map(row => row.name));
      if (!eventColumns.has("progress_json")) this.sql.exec("ALTER TABLE history_queue_events ADD COLUMN progress_json TEXT");
      const metaColumns = new Set(this.rows("PRAGMA table_info(history_queue_meta)").map(row => row.name));
      for (const [name, type] of [["last_flush_at", "TEXT"], ["last_flush_error", "TEXT"],
        ["last_flush_delivered", "INTEGER NOT NULL DEFAULT 0"]]) {
        if (!metaColumns.has(name)) this.sql.exec(`ALTER TABLE history_queue_meta ADD COLUMN ${name} ${type}`);
      }
    });
  }

  rows(sql, ...values) { return this.sql.exec(sql, ...values).toArray(); }

  meta(now) {
    const meta = this.rows("SELECT * FROM history_queue_meta WHERE id=1")[0];
    const day = new Date(now).toISOString().slice(0, 10);
    if (meta.do_day !== day) { meta.do_day = day; meta.do_writes = 0; }
    if (meta.hist_day !== day) { meta.hist_day = day; meta.hist_writes = 0; }
    return meta;
  }

  saveMeta(meta) {
    this.sql.exec(`UPDATE history_queue_meta SET pending_rows=?, pending_bytes=?, delivered_rows=?,
      do_day=?, do_writes=?, hist_day=?, hist_writes=?,last_flush_at=?,last_flush_error=?,last_flush_delivered=? WHERE id=1`,
    meta.pending_rows, meta.pending_bytes, meta.delivered_rows,
    meta.do_day, meta.do_writes, meta.hist_day, meta.hist_writes,
    meta.last_flush_at, meta.last_flush_error, meta.last_flush_delivered);
  }

  reserveDo(meta, units) {
    if (meta.do_writes + units > this.doBudget) throw new QueueError("history_queue_daily_write_budget", 429);
    meta.do_writes += units;
  }

  written(cursor, fallback) {
    // SQLite DO reports index writes too. Retain conservative reservations on
    // runtimes which do not expose the counter, rather than guessing billable rows.
    return Number.isInteger(cursor?.rowsWritten) && cursor.rowsWritten >= 0 ? cursor.rowsWritten : fallback;
  }

  enqueue(events, now = Date.now()) {
    const validated = validateHistoryEvents(events, now, this.eventBytes);
    if (events.some(event => minimumHistoryWork(event,this.infra) > this.historyBudget)) {
      throw new QueueError("history_event_exceeds_atomic_daily_allowance");
    }
    return this.ctx.storage.transactionSync(() => {
      const meta = this.meta(now);
      const unique = new Map();
      let duplicates = 0;
      for (const event of validated) {
        if (unique.has(event.id) || this.rows("SELECT status FROM history_queue_events WHERE event_id=?", event.id).length) {
          duplicates += 1; // First full payload wins; replay never rewrites it or resets its retry timer.
        } else unique.set(event.id, event);
      }
      const addedBytes = [...unique.values()].reduce((sum, event) => sum + event.bytes, 0);
      if (meta.pending_rows + unique.size > this.maxRows || meta.pending_bytes + addedBytes > this.maxBytes) {
        throw new QueueError("history_queue_pending_capacity", 507);
      }
      const needed = Math.max(0, meta.pending_rows + meta.delivered_rows + unique.size - this.maxReceipts);
      const expired = needed ? this.rows(`SELECT event_id FROM history_queue_events
        WHERE status='delivered' AND delivered_at < ? ORDER BY delivered_at,event_id LIMIT ?`,
      now - HISTORY_QUEUE_LIMITS.receiptRetentionMs, needed) : [];
      if (expired.length < needed) throw new QueueError("history_queue_receipt_capacity", 507);
      if (unique.size || expired.length) {
        const reserved=(unique.size+expired.length)*8+2;
        this.reserveDo(meta,reserved);
        let actual=0;
        for (const row of expired) actual+=this.written(this.sql.exec("DELETE FROM history_queue_events WHERE event_id=?", row.event_id),8);
        meta.delivered_rows -= expired.length;
        for (const event of unique.values()) {
          actual+=this.written(this.sql.exec(`INSERT INTO history_queue_events
            (event_id,episode_id,source_at,payload_json,payload_bytes,status,next_attempt_at)
            VALUES (?,?,?,?,?,'pending',?)`, event.id, event.episodeId, event.sourceAt, event.json, event.bytes, now),8);
        }
        meta.pending_rows += unique.size;
        meta.pending_bytes += addedBytes;
        meta.do_writes-=Math.max(0,reserved-actual-2);
        this.saveMeta(meta);
      }
      return { enabled: true, queued: unique.size, duplicates, pending: meta.pending_rows,
        pending_bytes: meta.pending_bytes, delivered_receipts: meta.delivered_rows };
    });
  }

  claim(now) {
    return this.ctx.storage.transactionSync(() => {
      const meta = this.meta(now);
      // A poisoned episode waits, but unrelated newer episodes remain eligible.
      const candidates = this.rows(`SELECT q.* FROM history_queue_events q
        WHERE q.status='pending' AND q.next_attempt_at <= ? AND q.source_at <= ?
          AND NOT EXISTS (SELECT 1 FROM history_queue_events older
            WHERE older.status='pending' AND older.episode_id=q.episode_id
              AND (older.source_at < q.source_at OR (older.source_at=q.source_at AND older.event_id < q.event_id)))
        ORDER BY q.source_at,q.event_id LIMIT ?`, now, now, HISTORY_QUEUE_LIMITS.flushEvents);
      if (!candidates.length) return { rows:[], budget: 0, day: meta.hist_day };
      const eligible=candidates.filter(row=>minimumProgressWork(JSON.parse(row.payload_json),JSON.parse(row.progress_json || "{}"),this.infra)
        <= this.historyBudget-meta.hist_writes);
      if (!eligible.length) throw new QueueError("history_daily_write_budget", 429);
      const rows=[];
      let queries=0;
      for (const row of eligible) {
        const cost=estimatedHistoryQueries(JSON.parse(row.payload_json),JSON.parse(row.progress_json || "{}"));
        if (rows.length && queries+cost>HISTORY_QUEUE_LIMITS.flushQueries) break;
        if ((rows.length+1)*16+4>this.doBudget-meta.do_writes) break;
        queries+=cost; rows.push(row);
      }
      if (!rows.length) throw new QueueError("history_queue_daily_write_budget",429);
      const budget = Math.min(HISTORY_QUEUE_LIMITS.flushHistoryWriteUnits, this.historyBudget - meta.hist_writes);
      this.reserveDo(meta, rows.length * 16 + 4); // Claim AND finish, even after restart.
      meta.hist_writes += budget; // A crash loses unused reservation until UTC reset, never overspends it.
      const lease = crypto.randomUUID();
      let actual=0;
      for (const row of rows) {
        row.lease_token = lease;
        row.attempts += 1;
        actual+=this.written(this.sql.exec(`UPDATE history_queue_events SET lease_token=?, attempts=?, next_attempt_at=? WHERE event_id=?`,
        lease, row.attempts, now + HISTORY_QUEUE_LIMITS.leaseMs, row.event_id),8);
      }
      meta.do_writes-=Math.max(0,rows.length*8-actual); // Keep the entire finish reservation.
      this.saveMeta(meta);
      return { rows, budget, day: meta.hist_day };
    });
  }

  finish(claim, result, used, error, now = Date.now()) {
    return this.ctx.storage.transactionSync(() => {
      const meta = this.meta(now);
      if (meta.do_day!==claim.day) this.reserveDo(meta,claim.rows.length*8+2);
      const successful = new Set(result.ingested.map(event => event.event_id));
      const failures = new Map((result?.failed || []).map(event => [event.event_id, event.error]));
      const deferred = new Set(result.deferred || []);
      const progress = result.progress || new Map();
      let delivered = 0;
      let failed = 0;
      let continued = 0;
      let leaseLost = 0;
      let actual=0;
      for (const row of claim.rows) {
        const current = this.rows("SELECT status,lease_token FROM history_queue_events WHERE event_id=?", row.event_id)[0];
        if (current?.status !== "pending" || current.lease_token !== row.lease_token) { leaseLost += 1; continue; }
        if (successful.has(row.event_id)) {
          actual+=this.written(this.sql.exec(`UPDATE history_queue_events SET status='delivered', payload_json=NULL, payload_bytes=0,
            progress_json=NULL,delivered_at=?, lease_token=NULL, last_error=NULL WHERE event_id=?`, now, row.event_id),8);
          meta.pending_rows -= 1;
          meta.pending_bytes -= row.payload_bytes;
          meta.delivered_rows += 1;
          delivered += 1;
        } else {
          const delay = Math.min(6 * 3600_000, 60_000 * 2 ** Math.min(12, row.attempts));
          const continuing = deferred.has(row.event_id);
          actual+=this.written(this.sql.exec(`UPDATE history_queue_events SET lease_token=NULL,next_attempt_at=?,last_error=?,progress_json=?,
            attempts=? WHERE event_id=?`, now + (continuing ? 1000 : delay),
          continuing ? null : String(failures.get(row.event_id) || error?.message || "history_not_completed").slice(0,500),
          JSON.stringify(progress.get(row.event_id) || JSON.parse(row.progress_json || "{}")),
          continuing ? 0 : row.attempts,row.event_id),8);
          if (continuing) continued += 1; else failed += 1;
        }
      }
      if (meta.hist_day === claim.day) meta.hist_writes -= Math.max(0, claim.budget - used);
      meta.do_writes-=Math.max(0,claim.rows.length*8-actual);
      meta.last_flush_at = new Date(now).toISOString();
      meta.last_flush_error = error ? String(error.message || error).slice(0,500) : failures.values().next().value || null;
      meta.last_flush_delivered = delivered;
      this.saveMeta(meta);
      return { enabled: true, delivered, failed, continued, lease_lost: leaseLost, pending: meta.pending_rows,
        pending_bytes: meta.pending_bytes, history_write_units: used,
        error: meta.last_flush_error };
    });
  }

  async runFlush() {
    if (!this.env.RADAR_HISTORY_DB || typeof this.env.RADAR_HISTORY_DB.prepare !== "function") {
      throw new QueueError("history_db_not_configured", 503);
    }
    const now = Date.now();
    const claim = this.claim(now);
    if (!claim.rows.length) return { enabled: true, delivered: 0, failed: 0, pending: this.meta(now).pending_rows };
    let used = 0;
    let queries = 0;
    const result = {ingested:[],failed:[],deferred:[],progress:new Map()};
    let error = null;
    try {
      const db = checkedHistoryDb(this.env.RADAR_HISTORY_DB, statements => {
            if (new Date(Date.now()).toISOString().slice(0, 10) !== claim.day) throw new Error("history_write_day_changed");
            const units = statements * WRITE_UNITS_PER_STATEMENT;
            if (used + units > claim.budget) throw new HistoryYield("history_flush_write_budget");
            used += units;
          }, () => {
            if (queries >= HISTORY_QUEUE_LIMITS.flushQueries) throw new HistoryYield("history_flush_query_budget");
            queries++;
          });
      for (const row of claim.rows) {
        const state = JSON.parse(row.progress_json || "{}");
        result.progress.set(row.event_id,state);
        try {
          result.ingested.push(await resumeHistoryEvent(db,JSON.parse(row.payload_json),state,{
            now:new Date(now).toISOString(),remaining:()=>Math.floor((claim.budget-used)/WRITE_UNITS_PER_STATEMENT),infra:this.infra,
          }));
        } catch (caught) {
          if (caught.deferred) result.deferred.push(row.event_id);
          else result.failed.push({event_id:row.event_id,error:String(caught.message || caught).slice(0,500)});
        }
        // Separate from acknowledgement: if finish fails or the process dies
        // after this commit, the next lease resumes rather than replaying phases.
        if (row.progress_json || (state.phase !== "episode" && state.phase !== "done")) {
          this.ctx.storage.transactionSync(() => {
            const meta=this.meta(Date.now());
            // Unindexed progress plus its metadata write. When the DO cap is
            // exhausted, the already-reserved finish still saves this cursor.
            if (meta.do_writes+4>this.doBudget) return;
            this.reserveDo(meta,4);
            const actual=this.written(this.sql.exec(`UPDATE history_queue_events SET progress_json=? WHERE event_id=? AND lease_token=?`,
              JSON.stringify(state),row.event_id,row.lease_token),2);
            meta.do_writes-=Math.max(0,2-actual);
            this.saveMeta(meta);
          });
        }
      }
    } catch (caught) { error = caught; }
    return {...this.finish(claim, result, used, error),history_queries:queries};
  }

  flush() {
    if (!this.inFlight) this.inFlight = this.runFlush().catch(error => {
      // A failed claim has no finish path. Record it without changing payloads
      // or counters; status also derives exhausted budgets from current meta.
      this.ctx.storage.transactionSync(() => {
        const meta=this.meta(Date.now());
        this.reserveDo(meta,2);
        meta.last_flush_at=new Date().toISOString();
        meta.last_flush_error=String(error.message || error).slice(0,500);
        meta.last_flush_delivered=0;
        this.saveMeta(meta);
      });
      throw error;
    }).finally(() => { this.inFlight = null; });
    return this.inFlight;
  }

  status(now = Date.now()) {
    const meta = this.meta(now);
    const oldest = this.rows(`SELECT source_at FROM history_queue_events
      WHERE status='pending' ORDER BY source_at,event_id LIMIT 1`)[0];
    const due = this.rows(`SELECT next_attempt_at FROM history_queue_events
      WHERE status='pending' ORDER BY next_attempt_at,source_at,event_id LIMIT 1`)[0];
    const failure = this.rows(`SELECT last_error FROM history_queue_events
      WHERE status='pending' AND last_error IS NOT NULL ORDER BY source_at,event_id LIMIT 1`)[0];
    return { enabled: true, pending: meta.pending_rows, pending_bytes: meta.pending_bytes,
      delivered_receipts: meta.delivered_rows,
      last_flush_at: meta.last_flush_at, last_flush_error: meta.last_flush_error,
      last_flush_delivered: meta.last_flush_delivered,
      pending_last_error: failure?.last_error || null,
      oldest_pending_age_seconds: oldest ? Math.max(0,Math.floor((now-oldest.source_at)/1000)) : null,
      history_budget_exhausted: meta.hist_writes >= this.historyBudget
        || (meta.last_flush_error === "history_daily_write_budget" && meta.last_flush_at?.slice(0,10) === new Date(now).toISOString().slice(0,10)),
      do_budget_exhausted: meta.do_writes >= this.doBudget,
      oldest_pending_at: oldest ? new Date(oldest.source_at).toISOString() : null,
      next_attempt_at: due ? new Date(due.next_attempt_at).toISOString() : null,
      write_budget_day: meta.hist_day, do_write_units: meta.do_writes, history_write_units: meta.hist_writes,
      limits: { ...HISTORY_QUEUE_LIMITS, pendingRows: this.maxRows, pendingBytes: this.maxBytes,
        receiptRows: this.maxReceipts, eventBytes: this.eventBytes,
        dailyDoWriteUnits: this.doBudget, dailyHistoryWriteUnits: this.historyBudget } };
  }

  async fetch(request) {
    try {
      const path = new URL(request.url).pathname;
      if (request.method === "GET" && path === "/status") return Response.json({ ok: true, ...this.status() });
      if (request.method !== "POST") throw new QueueError("history_queue_post_required", 405);
      if (path === "/enqueue") return Response.json({ ok: true, ...this.enqueue((await readRequest(request)).events) });
      if (path === "/receipts") {
        const ids=(await readRequest(request)).ids;
        if (!Array.isArray(ids) || ids.length>25 || ids.some(id=>typeof id!=="string" || id.length>240)) {
          throw new QueueError("history_receipt_ids_invalid");
        }
        return Response.json({ok:true,receipts:ids.map(event_id=>({event_id,
          status:this.rows("SELECT status FROM history_queue_events WHERE event_id=?",event_id)[0]?.status || "unknown"}))});
      }
      if (path === "/ingest") return Response.json({ ok: true, ...this.enqueue(historyLedgerEvents(await readRequest(request))) });
      if (path === "/flush") return Response.json({ ok: true, ...await this.flush() });
      throw new QueueError("history_queue_route_not_found", 404);
    } catch (error) {
      return Response.json({ ok: false, error: String(error?.message || error) }, { status: error.status || 503 });
    }
  }
}

async function queueRequest(env, path, value, method = "POST") {
  if (!env?.HISTORY_QUEUE) throw new QueueError("history_queue_not_configured", 503);
  const stub = env.HISTORY_QUEUE.get(env.HISTORY_QUEUE.idFromName(QUEUE_NAME));
  const body = value === undefined ? undefined : JSON.stringify(value);
  if (body && ENCODER.encode(body).byteLength > HISTORY_QUEUE_LIMITS.requestBytes) throw new QueueError("history_request_oversize", 413);
  const response = await stub.fetch(new Request(`https://history-queue/${path}`, {
    method, headers: { "content-type": "application/json" }, body,
  }));
  const result = await response.json();
  if (!response.ok || !result.ok) throw new QueueError(result.error || "history_queue_unavailable", response.status);
  return result;
}

function historyLedgerEvents(payload) {
  const ledger = payload?.history_ledger;
  if (ledger !== undefined && (!ledger || typeof ledger !== "object" || Array.isArray(ledger))) {
    throw new QueueError("history_ledger_invalid");
  }
  return ledger?.events === undefined ? [] : ledger.events;
}

export async function enqueueDurableHistory(env, payload) {
  const events = historyLedgerEvents(payload);
  validateHistoryEvents(events);
  return queueRequest(env, "enqueue", { events });
}

export function durableHistoryIngestResponse(env, request) {
  if (!env?.HISTORY_QUEUE) throw new QueueError("history_queue_not_configured", 503);
  const stub = env.HISTORY_QUEUE.get(env.HISTORY_QUEUE.idFromName(QUEUE_NAME));
  return stub.fetch(new Request("https://history-queue/ingest", {
    method:"POST", body:request.body, duplex:"half", headers:{"content-type":"application/json"},
  }));
}

export async function flushDurableHistory(env) {
  return queueRequest(env, "flush");
}

export async function durableHistoryReceipts(env, ids) {
  return queueRequest(env,"receipts",{ids});
}

export async function durableHistoryStatus(env) {
  if (!env?.HISTORY_QUEUE) return { enabled: false, error: "history_queue_not_configured" };
  return queueRequest(env, "status", undefined, "GET");
}
