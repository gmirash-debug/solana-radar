import { historyEventId, ingestHistoryBatch } from "./history.js";

// Parent integration (no public DO route): export {HistoryQueue} from index.js;
// bind HISTORY_QUEUE to HistoryQueue and append a new_sqlite_classes migration.
// Await enqueueDurableHistory before acknowledging an authenticated ingest.
// Invoke flushDurableHistory from the existing bounded background scheduler.
export const HISTORY_QUEUE_LIMITS = Object.freeze({
  enqueueEvents: 25, flushEvents: 2, eventBytes: 128 * 1024, requestBytes: 1024 * 1024,
  pendingRows: 2048, pendingBytes: 16 * 1024 * 1024, receiptRows: 20000,
  receiptRetentionMs: 30 * 86400_000, leaseMs: 10 * 60_000,
  dailyDoWriteUnits: 50000, dailyHistoryWriteUnits: 80000, flushHistoryWriteUnits: 40000,
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
      do_day=?, do_writes=?, hist_day=?, hist_writes=? WHERE id=1`,
    meta.pending_rows, meta.pending_bytes, meta.delivered_rows,
    meta.do_day, meta.do_writes, meta.hist_day, meta.hist_writes);
  }

  reserveDo(meta, units) {
    if (meta.do_writes + units > this.doBudget) throw new QueueError("history_queue_daily_write_budget", 429);
    meta.do_writes += units;
  }

  enqueue(events, now = Date.now()) {
    const validated = validateHistoryEvents(events, now, this.eventBytes);
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
        this.reserveDo(meta, (unique.size + expired.length) * 8 + 2);
        for (const row of expired) this.sql.exec("DELETE FROM history_queue_events WHERE event_id=?", row.event_id);
        meta.delivered_rows -= expired.length;
        for (const event of unique.values()) {
          this.sql.exec(`INSERT INTO history_queue_events
            (event_id,episode_id,source_at,payload_json,payload_bytes,status,next_attempt_at)
            VALUES (?,?,?,?,?,'pending',?)`, event.id, event.episodeId, event.sourceAt, event.json, event.bytes, now);
        }
        meta.pending_rows += unique.size;
        meta.pending_bytes += addedBytes;
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
      const rows = this.rows(`SELECT q.* FROM history_queue_events q
        WHERE q.status='pending' AND q.next_attempt_at <= ? AND q.source_at <= ?
          AND NOT EXISTS (SELECT 1 FROM history_queue_events older
            WHERE older.status='pending' AND older.episode_id=q.episode_id
              AND (older.source_at < q.source_at OR (older.source_at=q.source_at AND older.event_id < q.event_id)))
        ORDER BY q.source_at,q.event_id LIMIT ?`, now, now, HISTORY_QUEUE_LIMITS.flushEvents);
      if (!rows.length) return { rows, budget: 0, day: meta.hist_day };
      if (meta.hist_writes >= this.historyBudget) throw new QueueError("history_daily_write_budget", 429);
      const budget = Math.min(HISTORY_QUEUE_LIMITS.flushHistoryWriteUnits, this.historyBudget - meta.hist_writes);
      this.reserveDo(meta, rows.length * 16 + 4); // Reserves claim AND finish, even after a restart.
      meta.hist_writes += budget; // A crash loses unused reservation until UTC reset, never overspends it.
      const lease = crypto.randomUUID();
      for (const row of rows) {
        row.lease_token = lease;
        row.attempts += 1;
        this.sql.exec(`UPDATE history_queue_events SET lease_token=?, attempts=?, next_attempt_at=? WHERE event_id=?`,
        lease, row.attempts, now + HISTORY_QUEUE_LIMITS.leaseMs, row.event_id);
      }
      this.saveMeta(meta);
      return { rows, budget, day: meta.hist_day };
    });
  }

  finish(claim, result, used, error, now = Date.now()) {
    return this.ctx.storage.transactionSync(() => {
      const meta = this.meta(now);
      const successful = new Set(error ? [] : result.ingested.map(event => event.event_id));
      const failures = new Map((result?.failed || []).map(event => [event.event_id, event.error]));
      let delivered = 0;
      let failed = 0;
      let leaseLost = 0;
      for (const row of claim.rows) {
        const current = this.rows("SELECT status,lease_token FROM history_queue_events WHERE event_id=?", row.event_id)[0];
        if (current?.status !== "pending" || current.lease_token !== row.lease_token) { leaseLost += 1; continue; }
        if (successful.has(row.event_id)) {
          this.sql.exec(`UPDATE history_queue_events SET status='delivered', payload_json=NULL, payload_bytes=0,
            delivered_at=?, lease_token=NULL, last_error=NULL WHERE event_id=?`, now, row.event_id);
          meta.pending_rows -= 1;
          meta.pending_bytes -= row.payload_bytes;
          meta.delivered_rows += 1;
          delivered += 1;
        } else {
          const delay = Math.min(6 * 3600_000, 60_000 * 2 ** Math.min(12, row.attempts));
          this.sql.exec(`UPDATE history_queue_events SET lease_token=NULL,next_attempt_at=?,last_error=? WHERE event_id=?`,
          now + delay, String(error?.message || failures.get(row.event_id) || "history_not_completed").slice(0, 500), row.event_id);
          failed += 1;
        }
      }
      if (meta.hist_day === claim.day) meta.hist_writes -= Math.max(0, claim.budget - used);
      this.saveMeta(meta);
      return { enabled: true, delivered, failed, lease_lost: leaseLost, pending: meta.pending_rows,
        pending_bytes: meta.pending_bytes, history_write_units: used,
        error: error ? String(error.message || error) : null };
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
    let result = null;
    let error = null;
    try {
      result = await ingestHistoryBatch({ RADAR_HISTORY_DB: this.env.RADAR_HISTORY_DB },
        claim.rows.map(row => JSON.parse(row.payload_json)), {
          now: new Date(now).toISOString(),
          onWrite: statements => {
            if (new Date(Date.now()).toISOString().slice(0, 10) !== claim.day) throw new Error("history_write_day_changed");
            const units = statements * WRITE_UNITS_PER_STATEMENT;
            if (used + units > claim.budget) throw new Error("history_flush_write_budget");
            used += units;
          },
        });
    } catch (caught) { error = caught; }
    return this.finish(claim, result, used, error);
  }

  flush() {
    if (!this.inFlight) this.inFlight = this.runFlush().finally(() => { this.inFlight = null; });
    return this.inFlight;
  }

  status(now = Date.now()) {
    const meta = this.meta(now);
    const oldest = this.rows(`SELECT source_at FROM history_queue_events
      WHERE status='pending' ORDER BY source_at,event_id LIMIT 1`)[0];
    const due = this.rows(`SELECT next_attempt_at FROM history_queue_events
      WHERE status='pending' ORDER BY next_attempt_at,source_at,event_id LIMIT 1`)[0];
    return { enabled: true, pending: meta.pending_rows, pending_bytes: meta.pending_bytes,
      delivered_receipts: meta.delivered_rows,
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

export async function enqueueDurableHistory(env, payload) {
  const ledger = payload?.history_ledger;
  if (ledger !== undefined && (!ledger || typeof ledger !== "object" || Array.isArray(ledger))) {
    throw new QueueError("history_ledger_invalid");
  }
  const events = ledger?.events === undefined ? [] : ledger.events;
  validateHistoryEvents(events);
  return queueRequest(env, "enqueue", { events });
}

export async function flushDurableHistory(env) {
  return queueRequest(env, "flush");
}

export async function durableHistoryStatus(env) {
  if (!env?.HISTORY_QUEUE) return { enabled: false, error: "history_queue_not_configured" };
  return queueRequest(env, "status", undefined, "GET");
}
