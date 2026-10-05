import {checkedHistoryDb} from "./history.js";
import {resumeHistoryEvent, HistoryYield, minimumHistoryWork, historyInfrastructure} from "./history-progress.js";
import {archiveHistoryEvent, readHistoryArchive, historyArchiveEnabled} from "./archive.js";

const WRITE_UNITS = 8;
const ENCODER = new TextEncoder();

function checked(result) {
  if (!result || result.success === false) throw new Error(result?.error || "history_queue_sql_failed");
  return result;
}

function sameJson(left, right, depth = 0) {
  if (depth > 100) return false;
  if (left === right) return true;
  if (!left || !right || typeof left !== "object" || typeof right !== "object"
      || Array.isArray(left) !== Array.isArray(right)) return false;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length
    && keys.every(key => Object.hasOwn(right,key) && sameJson(left[key],right[key],depth+1));
}

// All transactions are bounded SQL batches. Never hold a transaction open
// while awaiting R2 or history work; lease tokens fence every cursor/receipt.
export class SqlHistoryQueue {
  constructor(env, policy) {
    this.env = env;
    this.db = env.RADAR_HISTORY_DB;
    this.policy = policy;
    this.limits = policy.limits;
    const {lowered} = policy, limits = this.limits;
    this.maxRows = lowered(env,"HISTORY_QUEUE_MAX_PENDING_ROWS",limits.pendingRows);
    this.coldRows = this.maxRows;
    this.liveRows = lowered(env,"HISTORY_QUEUE_LIVE_RESERVED_ROWS",512,0);
    this.maxRows += this.liveRows;
    this.maxBytes = lowered(env,"HISTORY_QUEUE_MAX_PENDING_BYTES",limits.pendingBytes);
    this.maxReceipts = lowered(env,"HISTORY_QUEUE_MAX_RECEIPT_ROWS",limits.receiptRows);
    this.eventBytes = lowered(env,"HISTORY_QUEUE_MAX_EVENT_BYTES",limits.eventBytes);
    this.queryLimit = lowered(env,"HISTORY_QUEUE_FLUSH_QUERIES",limits.flushQueries);
    this.requestLimit = limits.sqlFlushRequests;
    this.historyBudget = lowered(env,"HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS",
      limits.maximumTursoHistoryWriteUnits,limits.dailyTursoHistoryWriteUnits);
    this.liveUnits = lowered(env,"HISTORY_QUEUE_LIVE_RESERVED_UNITS",this.historyBudget,0);
    this.archiveWrites = lowered(env,"HISTORY_ARCHIVE_DAILY_WRITES",limits.dailyArchiveWrites);
    this.archiveReads = lowered(env,"HISTORY_ARCHIVE_DAILY_READS",limits.dailyArchiveReads);
    this.archiveBytes = lowered(env,"HISTORY_ARCHIVE_DAILY_WRITE_BYTES",limits.dailyArchiveWriteBytes);
    this.infra = historyInfrastructure(env);
  }

  error(message, status = 503) { return new this.policy.QueueError(message,status); }
  day(now) { return `turso:${new Date(now).toISOString().slice(0,10)}`; }
  migrationPending(meta) { return Boolean(this.env.HISTORY_QUEUE) && !meta.legacy_complete; }

  async batch(statements, requests, reserve = 0) {
    if (requests) {
      if (requests.remaining <= reserve) throw new HistoryYield("history_flush_request_budget");
      requests.reserve("queue");
    }
    const results = await this.db.batch(statements);
    if (!Array.isArray(results) || results.length !== statements.length) throw this.error("history_queue_sql_batch_incomplete");
    results.forEach(checked);
    return results;
  }

  async meta(requests) {
    const row = (await this.batch([this.db.prepare("SELECT * FROM history_sql_queue_meta WHERE id=1")],requests))[0].results[0];
    if (!row) throw this.error("history_sql_queue_schema_required");
    return row;
  }

  async admissionModel(requests) {
    if (!this.liveRows && !this.liveUnits) return;
    if (this.admissionReady) return;
    await this.batch([
      this.db.prepare(`CREATE TABLE IF NOT EXISTS history_sql_queue_admission
        (id INTEGER PRIMARY KEY CHECK(id=1),cold_rows INTEGER NOT NULL CHECK(cold_rows>=1))`),
      this.db.prepare(`CREATE TABLE IF NOT EXISTS history_sql_live_priority
        (event_id TEXT PRIMARY KEY,priority_until INTEGER NOT NULL,
         FOREIGN KEY(event_id) REFERENCES history_sql_queue_events(event_id) ON DELETE CASCADE)`),
      this.db.prepare(`CREATE TABLE IF NOT EXISTS history_sql_cutover_priority
        (event_id TEXT PRIMARY KEY,priority_until INTEGER NOT NULL,
         FOREIGN KEY(event_id) REFERENCES history_sql_cutover_events(event_id) ON DELETE CASCADE)`),
      this.db.prepare(`INSERT INTO history_sql_queue_admission(id,cold_rows) VALUES(1,?1)
        ON CONFLICT(id) DO UPDATE SET cold_rows=excluded.cold_rows
        WHERE cold_rows!=excluded.cold_rows`).bind(this.coldRows),
      this.db.prepare("DROP TRIGGER IF EXISTS history_sql_queue_cold_capacity"),
      this.db.prepare(`CREATE TRIGGER IF NOT EXISTS history_sql_queue_cold_capacity_v2
        BEFORE INSERT ON history_sql_queue_events BEGIN
        SELECT CASE WHEN NEW.status='pending' AND COALESCE(json_extract(NEW.progress_json,'$._live_until'),0)=0
          AND EXISTS (SELECT 1 FROM history_sql_queue_meta WHERE id=1 AND pending_rows+archive_pending_rows+1>
            (SELECT cold_rows FROM history_sql_queue_admission WHERE id=1))
        THEN RAISE(ABORT,'history_queue_pending_capacity') END; END`),
    ],requests);
    this.admissionReady=true;
  }

  async enqueue(events, now = Date.now(), priorities = new Set()) {
    const rows = this.policy.validateHistoryEvents(events,now,this.eventBytes);
    if (events.some(event => minimumHistoryWork(event,this.infra)>this.historyBudget)) {
      throw this.error("history_event_exceeds_atomic_daily_allowance",400);
    }
    const meta = await this.meta();
    if (this.migrationPending(meta) && this.env.HISTORY_LEGACY_MIGRATION === "verified_turso_v1") {
      await this.admissionModel();
      const statements = rows.map(row => this.db.prepare(`INSERT OR IGNORE INTO history_sql_cutover_events
        (event_id,episode_id,source_at,payload_json,payload_bytes) VALUES(?1,?2,?3,?4,?5)`)
        .bind(row.id,row.episodeId,row.sourceAt,row.json,row.bytes));
      if (this.liveRows || this.liveUnits) {
        for (const row of rows.filter(row=>priorities.has(row.episodeId) || row.sourceAt>=now-24*3600000)) {
          statements.push(this.db.prepare(`INSERT INTO history_sql_cutover_priority(event_id,priority_until) VALUES(?1,?2)
            ON CONFLICT(event_id) DO UPDATE SET priority_until=MAX(priority_until,excluded.priority_until)`)
            .bind(row.id,now+24*3600000));
        }
      }
      statements.push(this.db.prepare("SELECT * FROM history_sql_cutover_meta WHERE id=1"));
      const saved = await this.batch(statements);
      const staging = saved.at(-1).results[0];
      if (!staging) throw this.error("history_cutover_schema_required");
      const queued = saved.slice(0,rows.length).reduce((sum,row) => sum+(row.meta?.changes || 0),0);
      return {...this.enqueueResult(meta,queued,events.length),staged:true,
        cutover_pending:staging.pending_rows,cutover_pending_bytes:staging.pending_bytes};
    }
    return this.persist(rows.map(row => ({...row,status:"pending",attempts:0,nextAttempt:now,
      progress:priorities.has(row.episodeId) || row.sourceAt>=now-24*3600000
        ? JSON.stringify({_live_until:now+24*3600000}) : null,
      archiveVersion:0,deliveredAt:null,lastError:null})),events.length,now);
  }

  async drainCutover(requests, now) {
    if (this.env.HISTORY_LEGACY_MIGRATION !== "verified_turso_v1") return {enabled:false};
    await this.admissionModel(requests);
    const [page] = await this.batch([this.db.prepare(`SELECT c.*${this.liveRows || this.liveUnits ? ",p.priority_until" : ""}
      FROM history_sql_cutover_events c ${this.liveRows || this.liveUnits
        ? "LEFT JOIN history_sql_cutover_priority p ON p.event_id=c.event_id" : ""}
      ORDER BY c.source_at,c.event_id LIMIT 25`)],requests);
    const rows = page.results.map(row => ({id:row.event_id,episodeId:row.episode_id,sourceAt:row.source_at,
      json:row.payload_json,bytes:row.payload_bytes,status:"pending",attempts:0,nextAttempt:now,
      progress:row.source_at>=now-24*3600000 || row.priority_until>now
        ? JSON.stringify({_live_until:Math.max(row.priority_until || 0,now+24*3600000)}) : null,
      archiveVersion:0,deliveredAt:null,lastError:null}));
    if (!rows.length) return {enabled:true,forwarded:0};
    try { await this.persist(rows,rows.length,now,requests); }
    catch (error) {
      if (/history_queue_(pending|receipt)_capacity/.test(error.message)) return {enabled:true,deferred:true,forwarded:0};
      throw error;
    }
    // The primary SQL queue (or its original delivered receipt) is durable
    // before removing a staged copy. The old DO remains untouched.
    const [removed] = await this.batch([this.db.prepare(`DELETE FROM history_sql_cutover_events
      WHERE event_id IN (${rows.map((_,i) => `?${i+1}`).join(",")}) AND EXISTS
      (SELECT 1 FROM history_sql_queue_events q WHERE q.event_id=history_sql_cutover_events.event_id)
      RETURNING event_id`).bind(...rows.map(row => row.id))],requests);
    return {enabled:true,forwarded:removed.results.length};
  }

  async persist(input, count, now, requests) {
    const first = new Map();
    for (const row of input) if (!first.has(row.id)) first.set(row.id,row);
    const unique = [...first.values()];
    if (!unique.length) return this.enqueueResult(await this.meta(requests),0,count);
    const [known] = await this.batch([this.db.prepare(`SELECT event_id FROM history_sql_queue_events
      WHERE event_id IN (${unique.map((_,i) => `?${i+1}`).join(",")})`).bind(...unique.map(row => row.id))],requests);
    const ids = new Set(known.results.map(row => row.event_id));
    const added = unique.filter(row => !ids.has(row.id));
    const meta = await this.meta(requests);
    await this.admissionModel(requests);
    const promoted = unique.filter(row=>ids.has(row.id) && JSON.parse(row.progress || "{}")._live_until>now);
    if (promoted.length && (this.liveRows || this.liveUnits)) await this.batch(promoted.map(row=>this.db.prepare(`INSERT INTO history_sql_live_priority(event_id,priority_until)
      SELECT event_id,?2 FROM history_sql_queue_events WHERE event_id=?1 AND status='pending'
      ON CONFLICT(event_id) DO UPDATE SET priority_until=MAX(priority_until,excluded.priority_until)`)
      .bind(row.id,JSON.parse(row.progress)._live_until)),requests);
    const pending = added.filter(row => row.status === "pending");
    const cold = pending.some(row => row.sourceAt < now-24*3600000 && !(JSON.parse(row.progress || "{}")._live_until>=now));
    if (pending.length && (meta.pending_rows+meta.archive_pending_rows+pending.length>this.maxRows
        || (cold && meta.pending_rows+meta.archive_pending_rows+pending.length>this.coldRows)
        || meta.pending_bytes+meta.archive_pending_bytes+pending.reduce((sum,row) => sum+row.bytes,0)>this.maxBytes)) {
      throw this.error("history_queue_pending_capacity",507);
    }
    if (!added.length) return this.enqueueResult(meta,0,count);
    const statements = [];
    statements.push(this.db.prepare(`UPDATE history_sql_queue_meta SET max_pending_rows=?1,
      max_pending_bytes=?2,max_receipts=?3 WHERE id=1`).bind(this.maxRows,this.maxBytes,this.maxReceipts),
    this.db.prepare(`DELETE FROM history_sql_queue_events WHERE event_id IN (
      SELECT event_id FROM history_sql_queue_events WHERE status='delivered' AND archive_pending=0 AND delivered_at<?1
      ORDER BY delivered_at,event_id LIMIT ?2)`).bind(now-this.limits.receiptRetentionMs,added.length));
    // Compact receipt migration must not create one SQL statement per row.
    // Live payloads still use the same 25-event and 1-MiB admission bounds.
    for (let start=0;start<added.length;start+=25) {
      const args=[], tuples=added.slice(start,start+25).map(row => {
        const offset=args.length;
        args.push(row.id,row.episodeId,row.sourceAt,row.json,row.bytes,row.status,row.attempts,
          row.nextAttempt,row.progress,row.archiveVersion,row.deliveredAt,row.lastError);
        return `(${Array.from({length:12},(_,i)=>`?${offset+i+1}`).join(",")})`;
      });
      statements.push(this.db.prepare(`INSERT INTO history_sql_queue_events
        (event_id,episode_id,source_at,payload_json,payload_bytes,status,attempts,next_attempt_at,
          progress_json,archive_version,delivered_at,last_error)
        SELECT column1,column2,column3,column4,column5,column6,column7,column8,column9,column10,column11,column12
        FROM (VALUES ${tuples.join(",")})
        WHERE NOT EXISTS (SELECT 1 FROM history_sql_queue_events WHERE event_id=column1) RETURNING event_id`).bind(...args));
    }
    statements.push(this.db.prepare("SELECT * FROM history_sql_queue_meta WHERE id=1"));
    const results = await this.batch(statements,requests);
    const queued = results.slice(2,-1).reduce((sum,row) => sum+row.results.length,0);
    return this.enqueueResult(results.at(-1).results[0],queued,count);
  }

  enqueueResult(meta, queued, count) {
    return {enabled:true,backend:"turso_sql",queued,duplicates:count-queued,pending:meta.pending_rows,
      pending_bytes:meta.pending_bytes,delivered_receipts:meta.delivered_rows,
      archive_pending:meta.archive_pending_rows,archive_pending_bytes:meta.archive_pending_bytes,
      migration_pending:this.migrationPending(meta),raw_payload_durable:true};
  }

  async receipts(ids) {
    if (!Array.isArray(ids) || ids.length>25 || ids.some(id => typeof id!=="string" || id.length>240)) {
      throw this.error("history_receipt_ids_invalid",400);
    }
    if (!ids.length) return {ok:true,backend:"turso_sql",receipts:[]};
    const result = checked(await this.db.prepare(`SELECT event_id,status,archive_pending FROM history_sql_queue_events
      WHERE event_id IN (${ids.map((_,i) => `?${i+1}`).join(",")})`).bind(...ids).all());
    const statuses = new Map(result.results.map(row => [row.event_id,row]));
    return {ok:true,backend:"turso_sql",receipts:ids.map(event_id => ({event_id,status:statuses.get(event_id)?.status || "unknown",
      archive_pending:Boolean(statuses.get(event_id)?.archive_pending)}))};
  }

  async status(now = Date.now()) {
    const results = await this.batch([this.db.prepare("SELECT * FROM history_sql_queue_meta WHERE id=1"),
      this.db.prepare(`SELECT source_at FROM history_sql_queue_events WHERE status='pending'
        ORDER BY source_at,event_id LIMIT 1`),
      this.db.prepare(`SELECT next_attempt_at,last_error FROM history_sql_queue_events WHERE status='pending'
        ORDER BY next_attempt_at,source_at,event_id LIMIT 1`),
      this.db.prepare(`SELECT last_error FROM history_sql_queue_events WHERE status='pending' AND last_error IS NOT NULL
        ORDER BY source_at,event_id LIMIT 1`),
      this.db.prepare(`SELECT archive_error,archive_next_attempt_at FROM history_sql_queue_events WHERE archive_pending=1
        ORDER BY archive_next_attempt_at,event_id LIMIT 1`)]);
    const meta = results[0].results[0];
    if (!meta) throw this.error("history_sql_queue_schema_required");
    const oldest = results[1].results[0], due = results[2].results[0], failure = results[3].results[0];
    const archiveDue = results[4].results[0];
    const cutover = this.env.HISTORY_LEGACY_MIGRATION === "verified_turso_v1"
      ? (await this.batch([this.db.prepare("SELECT * FROM history_sql_cutover_meta WHERE id=1")]))[0].results[0] : null;
    const writes = meta.hist_day === this.day(now) ? meta.hist_writes : 0;
    const archiveDay = new Date(now).toISOString().slice(0,10), currentArchive = meta.archive_day === archiveDay;
    return {ok:true,enabled:true,backend:"turso_sql",pending:meta.pending_rows,pending_bytes:meta.pending_bytes,
      archive_pending:meta.archive_pending_rows,archive_pending_bytes:meta.archive_pending_bytes,
      archive_pending_last_error:archiveDue?.archive_error || null,
      archive_next_attempt_at:archiveDue ? new Date(archiveDue.archive_next_attempt_at).toISOString() : null,
      delivered_receipts:meta.delivered_rows,last_flush_at:meta.last_flush_at,last_flush_error:meta.last_flush_error,
      last_flush_delivered:meta.last_flush_delivered,pending_last_error:failure?.last_error || null,
      oldest_pending_at:oldest ? new Date(oldest.source_at).toISOString() : null,
      oldest_pending_age_seconds:oldest ? Math.max(0,Math.floor((now-oldest.source_at)/1000)) : null,
      next_attempt_at:due ? new Date(due.next_attempt_at).toISOString() : null,
      write_budget_day:this.day(now),history_write_units:writes,history_budget_exhausted:writes>=this.historyBudget
        || (meta.last_flush_error === "history_daily_write_budget" && meta.last_flush_at?.slice(0,10) === new Date(now).toISOString().slice(0,10)),
      do_write_units:0,do_budget_exhausted:false,archive_mode:historyArchiveEnabled(this.env) ? "r2" : "inline",
      archive_budget_day:archiveDay,archive_write_requests:currentArchive ? meta.archive_writes : 0,
      archive_read_requests:currentArchive ? meta.archive_reads : 0,
      archive_write_bytes:currentArchive ? meta.archive_write_bytes : 0,
      migration_pending:this.migrationPending(meta),legacy_backlog_preserved:Boolean(this.env.HISTORY_QUEUE),
      cutover_pending:cutover?.pending_rows || 0,cutover_pending_bytes:cutover?.pending_bytes || 0,
      legacy_migration:{after:meta.legacy_cursor,complete:Boolean(meta.legacy_complete),imported:meta.legacy_imported},
      limits:{...this.limits,flushQueries:this.queryLimit,flushRequests:this.requestLimit,pendingRows:this.maxRows,
        liveReservedRows:this.liveRows,liveReservedWorkUnits:this.liveUnits,
        pendingBytes:this.maxBytes,receiptRows:this.maxReceipts,eventBytes:this.eventBytes,
        dailyHistoryWriteUnits:this.historyBudget,dailyDoWriteUnits:0,
        dailyArchiveWrites:this.archiveWrites,dailyArchiveReads:this.archiveReads,dailyArchiveWriteBytes:this.archiveBytes}};
  }

  async claim(now, requests, maximumWork = this.limits.flushHistoryWriteUnits) {
    await this.admissionModel(requests);
    const selection = await this.batch([this.db.prepare("SELECT * FROM history_sql_queue_meta WHERE id=1"),
      this.db.prepare(`SELECT q.*,EXISTS (SELECT 1 FROM history_sql_queue_events fresh
          WHERE fresh.status='pending' AND fresh.episode_id=q.episode_id
          AND (fresh.source_at>=?3 OR json_extract(fresh.progress_json,'$._live_until')>=?1
            ${this.liveRows || this.liveUnits ? `OR EXISTS (SELECT 1 FROM history_sql_live_priority promoted WHERE promoted.event_id=fresh.event_id
              AND promoted.priority_until>=?1)` : ""})) live_episode
        FROM history_sql_queue_events q WHERE q.status='pending'
        AND q.next_attempt_at<=?1 AND q.source_at<=?1 AND (q.lease_until IS NULL OR q.lease_until<=?1)
        AND NOT EXISTS (SELECT 1 FROM history_sql_queue_events older WHERE older.status='pending'
          AND older.episode_id=q.episode_id AND (older.source_at,older.event_id)<(q.source_at,q.event_id))
        ORDER BY live_episode DESC,q.source_at,q.event_id LIMIT ?2`).bind(now,this.limits.flushEvents,now-24*3600000)],requests,2);
    const meta = selection[0].results[0];
    if (!meta) throw this.error("history_sql_queue_schema_required");
    if (this.migrationPending(meta)) return {meta,migration_pending:true};
    const candidates = selection[1].results;
    if (!candidates.length) return {meta};
    const day = this.day(now), spent = meta.hist_day === day ? meta.hist_writes : 0;
    const allowance = row => this.historyBudget-(row.live_episode ? 0 : this.liveUnits);
    const row = candidates.find(row => this.policy.progressWork(JSON.parse(row.payload_json),
      JSON.parse(row.progress_json || "{}"),this.infra)<=allowance(row)-spent);
    if (!row) throw this.error(spent<this.historyBudget && this.liveUnits
      ? "history_live_budget_reserved" : "history_daily_write_budget",429);
    const minimum = this.policy.progressWork(JSON.parse(row.payload_json),JSON.parse(row.progress_json || "{}"),this.infra);
    const token = crypto.randomUUID();
    const results = await this.batch([
      this.db.prepare(`UPDATE history_sql_queue_meta SET hist_day=?1,hist_writes=0 WHERE id=1 AND hist_day!=?1`).bind(day),
      this.db.prepare(`UPDATE history_sql_queue_events SET lease_token=?2,lease_until=?3,
        next_attempt_at=?3,attempts=attempts+1,budget_day=?4,
        budget_reserved=MIN(?5,?6-(SELECT hist_writes FROM history_sql_queue_meta WHERE id=1))
        WHERE event_id=?1 AND status='pending' AND next_attempt_at<=?7 AND source_at<=?7
          AND (lease_until IS NULL OR lease_until<=?7)
          AND ?6-(SELECT hist_writes FROM history_sql_queue_meta WHERE id=1)>=?8
          AND NOT EXISTS (SELECT 1 FROM history_sql_queue_events older WHERE older.status='pending'
            AND older.episode_id=history_sql_queue_events.episode_id
            AND (older.source_at,older.event_id)<(history_sql_queue_events.source_at,history_sql_queue_events.event_id))`)
        .bind(row.event_id,token,now+this.limits.leaseMs,day,maximumWork,allowance(row),now,minimum),
      this.db.prepare(`UPDATE history_sql_queue_meta SET hist_writes=hist_writes+COALESCE((
        SELECT budget_reserved FROM history_sql_queue_events WHERE event_id=?1 AND lease_token=?2),0) WHERE id=1`)
        .bind(row.event_id,token),
      this.db.prepare("SELECT * FROM history_sql_queue_events WHERE event_id=?1 AND lease_token=?2").bind(row.event_id,token),
    ],requests,2);
    return {meta,row:results[3].results[0]};
  }

  archiveOptions(requests, reserve = 2) {
    return {deadline:Date.now()+this.limits.archiveBatchTimeoutMs,onOperation:async (kind,bytes) => {
      // Every R2 operation also has one atomic SQL daily-quota reservation.
      if (requests.remaining<reserve+2) throw new HistoryYield("history_flush_request_budget");
      const day = new Date().toISOString().slice(0,10);
      const result = await this.batch([
        this.db.prepare(`UPDATE history_sql_queue_meta SET archive_day=?1,archive_writes=0,archive_reads=0,
          archive_write_bytes=0 WHERE id=1 AND archive_day!=?1`).bind(day),
        this.db.prepare(`UPDATE history_sql_queue_meta SET archive_writes=archive_writes+?2,
          archive_reads=archive_reads+?3,archive_write_bytes=archive_write_bytes+?4 WHERE id=1 AND archive_day=?1
          AND archive_writes+?2<=?5 AND archive_reads+?3<=?6 AND archive_write_bytes+?4<=?7 RETURNING id`)
          .bind(day,kind === "write" ? 1 : 0,kind === "read" ? 1 : 0,kind === "write" ? bytes : 0,
            this.archiveWrites,this.archiveReads,this.archiveBytes),
      ],requests,reserve+1);
      if (!result[1].results.length) throw this.error("history_archive_daily_budget",429);
      requests.reserve("archive");
    }};
  }

  async saveProgress(row, state, requests) {
    const result = await this.batch([this.db.prepare(`UPDATE history_sql_queue_events SET progress_json=?3
      WHERE event_id=?1 AND lease_token=?2 AND status='pending' AND lease_until>?4 RETURNING event_id`)
      .bind(row.event_id,row.lease_token,JSON.stringify(state),Date.now())],requests,1);
    if (!result[0].results.length) throw this.error("history_queue_lease_lost",409);
  }

  async finish(row, state, used, outcome, requests) {
    const now = Date.now(), continuing = outcome.deferred, delivered = outcome.delivered;
    const delay = Math.min(6*3600_000,60_000*2**Math.min(12,row.attempts));
    const statements = [this.db.prepare(`UPDATE history_sql_queue_meta SET
      hist_writes=hist_writes-?3 WHERE id=1 AND hist_day=?4 AND EXISTS (
        SELECT 1 FROM history_sql_queue_events WHERE event_id=?1 AND lease_token=?2
          AND status='pending' AND lease_until>?5)`)
      .bind(row.event_id,row.lease_token,Math.max(0,row.budget_reserved-used),row.budget_day,now)];
    statements.push(delivered
      ? this.db.prepare(`UPDATE history_sql_queue_events SET status='delivered',
          payload_json=CASE WHEN ?4=1 THEN payload_json ELSE NULL END,
          payload_bytes=CASE WHEN ?4=1 THEN payload_bytes ELSE 0 END,archive_pending=?4,archive_next_attempt_at=?3,
          progress_json=NULL,delivered_at=?3,lease_token=NULL,lease_until=NULL,last_error=NULL,budget_reserved=0
          WHERE event_id=?1 AND lease_token=?2 AND status='pending' AND lease_until>?3 RETURNING event_id`)
        .bind(row.event_id,row.lease_token,now,outcome.archivePending ? 1 : 0)
      : this.db.prepare(`UPDATE history_sql_queue_events SET progress_json=?3,lease_token=NULL,lease_until=NULL,
          next_attempt_at=?4,last_error=?5,attempts=?6,budget_reserved=0
          WHERE event_id=?1 AND lease_token=?2 AND status='pending' AND lease_until>?7 RETURNING event_id`)
        .bind(row.event_id,row.lease_token,JSON.stringify(state),now+(continuing ? 1000 : delay),
          continuing ? null : outcome.error,continuing ? 0 : row.attempts,now));
    statements.push(this.db.prepare(`UPDATE history_sql_queue_meta SET last_flush_at=?1,
      last_flush_error=?2,last_flush_delivered=CASE WHEN changes()>0 THEN ?3 ELSE ?4 END WHERE id=1`)
      .bind(new Date(now).toISOString(),outcome.error || null,(outcome.deliveredBefore || 0)+(delivered ? 1 : 0),outcome.deliveredBefore || 0));
    statements.push(this.db.prepare("SELECT * FROM history_sql_queue_meta WHERE id=1"));
    const results = await this.batch(statements,requests);
    const held = results[1].results.length>0, meta = results[3].results[0];
    return {enabled:true,backend:"turso_sql",delivered:held && delivered ? 1 : 0,
      failed:held && !delivered && !continuing ? 1 : 0,continued:held && continuing ? 1 : 0,
      lease_lost:held ? 0 : 1,pending:meta.pending_rows,pending_bytes:meta.pending_bytes,
      archive_pending:meta.archive_pending_rows,archive_pending_bytes:meta.archive_pending_bytes,
      history_write_units:used,error:outcome.error || null,migration_pending:this.migrationPending(meta)};
  }

  async flush() {
    const requests = this.policy.requestBudget(this.requestLimit);
    let queries = 0, used = 0;
    const metrics = () => ({history_queries:queries,queue_requests:requests.queue,archive_requests:requests.archive,
      history_requests:requests.used,external_history_requests:requests.queue+requests.history});
    const total = {enabled:true,backend:"turso_sql",delivered:0,failed:0,continued:0,lease_lost:0};
    try {
      if (this.env.HISTORY_LEGACY_MIGRATION === "verified_turso_v1") {
        const meta = await this.meta(requests);
        if (!this.migrationPending(meta)) total.cutover_drain = await this.drainCutover(requests,Date.now());
      }
      for (let action = 0; action<this.limits.flushEvents && requests.remaining>=6
          && queries<this.queryLimit && used<this.limits.flushHistoryWriteUnits; action++) {
        let claim;
        try { claim = await this.claim(Date.now(),requests,this.limits.flushHistoryWriteUnits-used); }
        catch (error) {
          if ((total.delivered || total.continued) && ["history_live_budget_reserved","history_daily_write_budget"].includes(error.message)) {
            return {...total,history_write_units:used,deferred_reason:error.message,...metrics()};
          }
          throw error;
        }
        if (!claim.row) return {...total,pending:claim.meta.pending_rows,pending_bytes:claim.meta.pending_bytes,
          archive_pending:claim.meta.archive_pending_rows,archive_pending_bytes:claim.meta.archive_pending_bytes,
          history_write_units:used,migration_pending:Boolean(claim.migration_pending),...(claim.migration_pending
            ? {deferred:true,reason:"history_legacy_migration_pending"} : {}),...metrics()};
        const row = claim.row, state = JSON.parse(row.progress_json || "{}");
        const outcome = {deliveredBefore:total.delivered}, beforeUsed = used;
        try {
          let raw = JSON.parse(row.payload_json);
          const envelope = this.policy.archivedEnvelope(raw);
          const archiveOptions = this.archiveOptions(requests);
          if (envelope) {
            raw = await readHistoryArchive(this.env,envelope.ref,archiveOptions);
            this.verifyIdentity(raw,row);
            raw.archive_ref = envelope.ref;
            raw.event.raw_object_key = envelope.ref.key;
          } else {
            // An unverified caller-supplied pointer must never suppress raw SQL evidence.
            delete raw.archive_ref;
          }
          const db = checkedHistoryDb(this.db, statements => {
            if (this.day(Date.now())!==row.budget_day) throw new Error("history_write_day_changed");
            if (used-beforeUsed+statements*WRITE_UNITS>row.budget_reserved) throw new HistoryYield("history_flush_write_budget");
            used += statements*WRITE_UNITS;
          }, () => {
            if (queries>=this.queryLimit) throw new HistoryYield("history_flush_query_budget");
            if (requests.remaining<=2) throw new HistoryYield("history_flush_request_budget");
            if (Date.now()>=row.lease_until) throw this.error("history_queue_lease_lost",409);
            requests.reserve("history"); queries++;
          });
          await resumeHistoryEvent(db,raw,state,{now:new Date().toISOString(),
            remaining:() => Math.floor((row.budget_reserved-(used-beforeUsed))/WRITE_UNITS),infra:this.infra,
            derivedMode:this.env.HISTORY_DERIVED_MODE});
          outcome.delivered=true;
          outcome.archivePending=!envelope && historyArchiveEnabled(this.env);
        } catch (error) {
          outcome.deferred=Boolean(error.deferred);
          if (!outcome.deferred) outcome.error=String(error.message || error).slice(0,500);
        }
        // A separate fenced checkpoint survives a receipt failure or restart.
        await this.saveProgress(row,state,requests);
        const result = await this.finish(row,state,used-beforeUsed,outcome,requests);
        for (const key of ["delivered","failed","continued","lease_lost"]) total[key] += result[key];
        for (const key of ["pending","pending_bytes","archive_pending","archive_pending_bytes","error","migration_pending"]) total[key] = result[key];
      }
      return {...total,history_write_units:used,...metrics()};
    } catch (error) {
      // No payload deletion or lease release after an uncertain SQL outcome.
      if (requests.remaining>0) {
        try {
          await this.batch([this.db.prepare(`UPDATE history_sql_queue_meta SET last_flush_at=?1,
            last_flush_error=?2,last_flush_delivered=?3 WHERE id=1`)
            .bind(new Date().toISOString(),String(error.message || error).slice(0,500),total.delivered)],requests);
        } catch { /* The next read/lease recovers a potentially committed write. */ }
      }
      throw error;
    }
  }

  verifyIdentity(raw, row) {
    const [verified] = this.policy.validateHistoryEvents([raw],Date.now(),this.eventBytes);
    if (verified.id!==row.event_id || verified.episodeId!==row.episode_id || verified.sourceAt!==row.source_at) {
      throw this.error("history_archive_queue_identity_mismatch");
    }
    return verified;
  }

  async flushArchives() {
    const requests = this.policy.requestBudget(this.requestLimit);
    let archived = 0, failed = 0, lastError = null;
    // Separate consumer: archive outages never run before, or prevent, analytics.
    for (let action = 0; action<this.limits.archiveSpillRows && requests.remaining>=9; action++) {
      const now = Date.now(), token = crypto.randomUUID();
      const [claimed] = await this.batch([this.db.prepare(`UPDATE history_sql_queue_events SET
        archive_lease_token=?1,archive_lease_until=?2,archive_attempts=archive_attempts+1
        WHERE event_id=(SELECT event_id FROM history_sql_queue_events WHERE status='delivered' AND archive_pending=1
          AND archive_next_attempt_at<=?3 AND (archive_lease_until IS NULL OR archive_lease_until<=?3)
          ORDER BY archive_next_attempt_at,event_id LIMIT 1)
          AND archive_pending=1 AND (archive_lease_until IS NULL OR archive_lease_until<=?3) RETURNING *`)
        .bind(token,now+this.limits.leaseMs,now)],requests,1);
      const row = claimed.results[0];
      if (!row) break;
      let ref, error;
      try {
        const raw = JSON.parse(row.payload_json);
        this.verifyIdentity(raw,row);
        ref = await archiveHistoryEvent(this.env,raw,this.archiveOptions(requests,1));
      } catch (caught) { error=String(caught.message || caught).slice(0,500); }
      const time = Date.now();
      const statements = [];
      if (ref) {
        const raw = JSON.parse(row.payload_json), inline = structuredClone(raw);
        delete inline.archive_ref;
        const compact = {schema_version:2,archive_ref:ref,
          episode:{episode_id:raw.episode.episode_id,token_address:raw.episode.token_address},
          event:{...raw.event,raw_object_key:ref.key}};
        // Only replace the exact inline document produced by this consumer,
        // and only while our archive lease is still current. Foreign evidence
        // with the same ID is left intact, not compacted speculatively.
        statements.push(this.db.prepare(`UPDATE signal_episode_events SET payload_json=?3,raw_object_key=?4
          WHERE event_id=?1 AND payload_json=?2 AND EXISTS (
            SELECT 1 FROM history_sql_queue_events WHERE event_id=?1 AND archive_lease_token=?5
              AND archive_pending=1 AND archive_lease_until>?6)`)
          .bind(row.event_id,JSON.stringify(inline),JSON.stringify(compact),ref.key,token,time));
        statements.push(this.db.prepare(`UPDATE history_sql_queue_events SET archive_pending=0,
          payload_json=NULL,payload_bytes=0,archive_ref_json=?3,archive_lease_token=NULL,
          archive_lease_until=NULL,archive_error=NULL WHERE event_id=?1 AND archive_lease_token=?2
          AND archive_pending=1 AND archive_lease_until>?4 RETURNING event_id`)
          .bind(row.event_id,token,JSON.stringify(ref),time));
      } else statements.push(this.db.prepare(`UPDATE history_sql_queue_events SET archive_lease_token=NULL,
        archive_lease_until=NULL,archive_next_attempt_at=?3,archive_error=?4
        WHERE event_id=?1 AND archive_lease_token=?2 AND archive_pending=1 AND archive_lease_until>?5 RETURNING event_id`)
        .bind(row.event_id,token,time+Math.min(6*3600_000,60_000*2**Math.min(12,row.archive_attempts)),error,time));
      const results = await this.batch(statements,requests);
      if (results.at(-1).results.length) { if (ref) archived++; else failed++; }
      lastError = error || lastError;
      if (error) break;
    }
    return {enabled:true,backend:"turso_sql",archived,failed,error:lastError,archive_requests:requests.archive,
      queue_requests:requests.queue,history_requests:requests.used,
      external_history_requests:requests.queue};
  }

  async migrateLegacy(options, exporter) {
    if (!options || typeof options!=="object" || Array.isArray(options)) throw this.error("history_legacy_migration_options_invalid",400);
    if (!this.env.HISTORY_QUEUE) return {enabled:true,migration_pending:false,reason:"history_legacy_not_configured"};
    const limit = options.limit ?? this.limits.legacyExportRows;
    if (!Number.isInteger(limit) || limit<1 || limit>this.limits.legacyExportRows) throw this.error("history_legacy_export_cursor_invalid",400);
    if (options.legacyWritersStopped!==true || options.historyStateMigrated!==true) {
      return {enabled:true,migration_pending:true,reason:"history_legacy_migration_prerequisites_required"};
    }
    const requests = this.policy.requestBudget(this.requestLimit);
    const meta = await this.meta(requests);
    if (meta.legacy_complete) return {enabled:true,migration_pending:false,complete:true,after:meta.legacy_cursor};
    requests.reserve("queue");
    const page = await exporter({after:meta.legacy_cursor,limit});
    if (page.read_only!==true || !Array.isArray(page.rows) || page.rows.length>limit
        || page.after!==(page.rows.at(-1)?.event_id || meta.legacy_cursor)
        || typeof page.complete!=="boolean"
        || page.rows.filter(row=>row.status==="pending").length>this.limits.enqueueEvents) throw this.error("history_legacy_export_invalid");
    if (page.rows.some(row => row.active_lease || (row.lease_token && row.next_attempt_at>Date.now()))) {
      return {enabled:true,migration_pending:true,after:meta.legacy_cursor,reason:"history_legacy_active_lease"};
    }
    const rows = [];
    // Previously confirmed immutable references can migrate without R2 reads.
    // They are hydrated/verified by the consumer, never treated as new uploads.
    let previous = meta.legacy_cursor;
    for (const row of page.rows) {
      if (typeof row.event_id!=="string" || row.event_id<=previous || row.event_id.length>240
          || !["pending","delivered"].includes(row.status) || !Number.isSafeInteger(row.source_at)
          || !Number.isSafeInteger(row.next_attempt_at) || !Number.isSafeInteger(row.attempts) || row.attempts<0) {
        throw this.error("history_legacy_export_invalid");
      }
      let validated = {id:row.event_id,episodeId:row.episode_id,sourceAt:row.source_at,json:null,bytes:0};
      if (row.status === "pending") {
        const raw = JSON.parse(row.payload_json);
        const envelope = this.policy.archivedEnvelope(raw);
        if (envelope) {
          if (envelope.ref.event_id!==row.event_id || envelope.ref.episode_id!==row.episode_id
              || Date.parse(envelope.ref.observed_at)!==row.source_at) {
            throw this.error("history_archive_queue_identity_mismatch");
          }
          validated = {...validated,json:row.payload_json,bytes:ENCODER.encode(row.payload_json).byteLength};
          if (validated.bytes>this.eventBytes) throw this.error("history_event_oversize",413);
        } else validated = this.verifyIdentity(raw,row);
        validated.archiveVersion=envelope ? 1 : 0;
      } else if (!Number.isSafeInteger(row.delivered_at)) throw this.error("history_legacy_export_invalid");
      rows.push({...validated,status:row.status,attempts:row.attempts,nextAttempt:row.next_attempt_at,
        progress:row.progress_json || null,archiveVersion:validated.archiveVersion || 0,
        deliveredAt:row.delivered_at || null,lastError:row.last_error || null});
      previous = row.event_id;
    }
    const complete = page.complete && rows.length === page.rows.length;
    // Reject collisions before and after INSERT: a concurrent producer must not
    // silently replace the legacy first payload or acknowledge a different one.
    const verify = async () => {
      if (!rows.length) return;
      const [stored] = await this.batch([this.db.prepare(`SELECT event_id,status,payload_json FROM history_sql_queue_events
        WHERE event_id IN (${rows.map((_,i) => `?${i+1}`).join(",")})`).bind(...rows.map(row => row.id))],requests);
      for (const actual of stored.results) {
        const expected = rows.find(row => row.id === actual.event_id);
        if (expected.status === "pending" && (actual.status !== "pending"
            || !sameJson(JSON.parse(expected.json),JSON.parse(actual.payload_json)))) {
          throw this.error("history_legacy_migration_payload_conflict",409);
        }
        if (expected.status === "delivered" && actual.status !== "delivered") {
          throw this.error("history_legacy_migration_receipt_conflict",409);
        }
      }
      return stored.results.length;
    };
    await verify();
    const persisted = await this.persist(rows,rows.length,Date.now(),requests);
    if (await verify() !== rows.length && rows.length) throw this.error("history_legacy_migration_ack_incomplete");
    const [saved] = await this.batch([this.db.prepare(`UPDATE history_sql_queue_meta SET legacy_cursor=?2,
      legacy_complete=?3,legacy_imported=legacy_imported+?4 WHERE id=1 AND legacy_cursor=?1 AND legacy_complete=0
      RETURNING legacy_cursor`).bind(meta.legacy_cursor,previous,complete ? 1 : 0,rows.length)],requests);
    if (!saved.results.length) return {enabled:true,migration_pending:true,reason:"history_legacy_migration_cursor_changed"};
    return {enabled:true,queued:persisted.queued,duplicates:persisted.duplicates,after:previous,
      complete,migration_pending:!complete,legacy_backlog_preserved:true,requests:requests.used};
  }
}
