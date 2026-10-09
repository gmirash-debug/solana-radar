import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {DatabaseSync} from "node:sqlite";
import {createHash} from "node:crypto";
import {archiveHistoryEvent} from "../src/archive.js";
import test from "node:test";
import {HistoryQueue, HISTORY_QUEUE_LIMITS, enqueueDurableHistory, durableHistoryIngestResponse,
  flushDurableHistory, flushDurableHistoryArchives, durableHistoryReceipts, durableHistoryStatus,
  exportLegacyDurableHistory, migrateLegacyDurableHistory} from "../src/runtime-history.js";

const NOW = Date.parse("2026-10-04T12:00:00.000Z");
const iso = time => new Date(time).toISOString();
const ledger = events => ({history_ledger:{events}});
const credentials = {TURSO_DATABASE_URL:"libsql://queue-test.turso.io",TURSO_AUTH_TOKEN:"test-only"};

test("a retirement discards queued raw evidence without an archive read or replaying the old episode",async t=> {
  const f=fixture(t,{TOKEN_RETIREMENT_ENABLED:"true",HISTORY_ARCHIVE_MODE:"r2",
    RADAR_ARCHIVE:new Proxy({}, {get(){assert.fail("Retired history cannot contact R2");}})});
  const raw=event("retired-row");
  await enqueueDurableHistory(f.env,ledger([raw]));
  f.sqlite.prepare(`INSERT INTO token_retirements(token_address,retired_at,reason,cleanup_before,updated_at)
    VALUES(?,?,'below_20k_24h',?,?)`).run(raw.episode.token_address,iso(NOW),iso(NOW),iso(NOW));
  const result=await flushDurableHistory(f.env);
  assert.equal(result.delivered,1);assert.equal(result.failed,0);
  assert.equal(f.sqlite.prepare("SELECT COUNT(*) n FROM signal_episodes").get().n,0);
  assert.equal(f.rows()[0].payload_json,null);assert.equal(f.meta().pending_rows,0);
  assert.equal(f.meta().archive_pending_rows,0);
  const replay=await enqueueDurableHistory(f.env,ledger([raw]));
  assert.equal(replay.queued,0);assert.equal(replay.retired_discarded,1);
});

test("recapture admits only a new episode while old archive receipts remain fenced",async t=> {
  const f=fixture(t,{TOKEN_RETIREMENT_ENABLED:"true"});
  const old=event("old-retired"), fresh=event("new-catch");
  fresh.episode.token_address=old.episode.token_address;
  f.sqlite.prepare(`INSERT INTO token_retirements(token_address,retired_at,reactivated_at,reason,cleanup_before,updated_at)
    VALUES(?,?,?,'below_20k_24h',?,?)`).run(old.episode.token_address,iso(NOW-HOUR),iso(NOW),iso(NOW-HOUR),iso(NOW));
  fresh.episode.caught_at=iso(NOW-600000);
  fresh.event.observed_at=iso(NOW);
  const result=await enqueueDurableHistory(f.env,ledger([old,fresh]));
  assert.equal(result.retired_discarded,1);assert.equal(result.queued,1);
  assert.equal(f.rows()[0].episode_id,"new-catch");
});

const HOUR=3600000;
function event(id, {episode = id, at = NOW-3600_000, wallets = [], type = "snapshot"} = {}) {
  return {event_id:id,episode:{episode_id:episode,token_address:`token:${episode}`,caught_at:iso(NOW-4*3600_000),
    caught_mcap_usd:80000,caught_liquidity_usd:20000,token_age_days:40},
  event:{event_type:type,observed_at:iso(at)},wallets,evidence:{untouched:[1,{extra:"source"}]}};
}

function coldEvent(id, {episode = id, at = NOW-48*3600_000} = {}) {
  const row = event(id,{episode,at});
  row.episode.caught_at = iso(at-24*3600_000);
  return row;
}

function seedHistoryBudget(f, units, at = Date.now()) {
  f.sqlite.prepare("UPDATE history_sql_queue_meta SET hist_day=?,hist_writes=? WHERE id=1")
    .run(`turso:${iso(at).slice(0,10)}`,units);
}

// Execute the production Hrana transaction/conditional steps on real SQLite.
// No fake queue state or SQL matching supplies the query results.
function fixture(t, overrides = {}, {migrate = true} = {}) {
  t.mock.timers.enable({apis:["Date"],now:NOW});
  const sqlite = new DatabaseSync(":memory:");
  t.after(() => sqlite.close());
  for (const name of ["0001_wallet_edge_history.sql","0002_cluster_edge_evidence.sql","0003_resumable_history.sql"]) {
    sqlite.exec(readFileSync(new URL(`../migrations-history/${name}`,import.meta.url),"utf8"));
  }
  sqlite.exec(readFileSync(new URL("../migrations-storage/0001_daily_learning.sql",import.meta.url),"utf8"));
  if (migrate) sqlite.exec(readFileSync(new URL("../migrations-storage/0003_sql_queue.sql",import.meta.url),"utf8"));
  if (migrate) sqlite.exec(readFileSync(new URL("../migrations-storage/0005_cutover_staging.sql",import.meta.url),"utf8"));
  if (overrides.TOKEN_RETIREMENT_ENABLED === "true") {
    for (const file of ["migrations/0001_radar_data.sql","migrations-storage/0002_runtime_sql.sql",
      "migrations-storage/0007_token_retirement.sql"]) {
      sqlite.exec(readFileSync(new URL(`../${file}`,import.meta.url),"utf8"));
    }
  }
  const f = {sqlite,calls:[],sql:[],before:null,failSql:null,afterCommit:null};
  const encode = n => n === null ? {type:"null"} : typeof n === "string" ? {type:"text",value:n}
    : Number.isInteger(n) ? {type:"integer",value:String(n)} : {type:"float",value:n};
  const decode = n => n.type === "null" ? null : n.type === "integer" ? Number(n.value) : n.value;
  const condition = (cond, results) => !cond ? true : cond.type === "ok" ? results[cond.step]!==null
    : cond.type === "not" ? !condition(cond.cond,results)
    : cond.type === "and" ? cond.conds.every(child => condition(child,results)) : assert.fail("Hrana condition");
  const oldFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch=oldFetch; });
  globalThis.fetch = async (_,options) => {
    const body = JSON.parse(options.body), steps = body.requests[0].batch.steps;
    f.calls.push(body);
    await f.before?.(steps);
    const results = [], errors = [];
    for (const step of steps) {
      if (!condition(step.condition,results)) { results.push(null); errors.push(null); continue; }
      try {
        f.sql.push(step.stmt.sql);
        if (f.failSql?.(step.stmt.sql)) throw new Error("injected write failure");
        const statement = sqlite.prepare(step.stmt.sql), columns = statement.columns();
        const rows = statement.all(...step.stmt.args.map(decode));
        const dml = /^\s*(INSERT|UPDATE|DELETE)\b/.test(step.stmt.sql);
        const info = sqlite.prepare("SELECT changes() changed,last_insert_rowid() id").get();
        results.push({cols:columns.map(col => ({name:col.name,decltype:col.type})),
          rows:step.stmt.want_rows === false ? [] : rows.map(row => columns.map(col => encode(row[col.name]))),
          affected_row_count:dml ? Number(info.changed) : 0,last_insert_rowid:dml ? String(info.id) : null});
        errors.push(null);
      } catch (error) {
        results.push(null);errors.push({code:(error.errcode & 255) === 19 ? "SQLITE_CONSTRAINT" : "SQLITE_ERROR",message:error.message});
      }
    }
    try { sqlite.exec("ROLLBACK"); } catch { /* Stream closed in autocommit. */ }
    await f.afterCommit?.(steps,errors);
    return Response.json({baton:null,results:[{type:"ok",response:{type:"batch",result:{step_results:results,step_errors:errors}}},
      {type:"ok",response:{type:"close"}}]});
  };
  const poison = {prepare() {assert.fail("D1 fallback must not be accessed");}};
  f.env = {...credentials,STORAGE_SQL_BACKEND:"turso",HISTORY_QUEUE_BACKEND:"turso_sql",HISTORY_DERIVED_MODE:"daily",
    RADAR_DB:poison,RADAR_HISTORY_DB:poison,...overrides};
  f.advance = ms => t.mock.timers.tick(ms);
  f.rows = () => sqlite.prepare("SELECT * FROM history_sql_queue_events ORDER BY source_at,event_id").all();
  f.meta = () => sqlite.prepare("SELECT * FROM history_sql_queue_meta WHERE id=1").get();
  return f;
}

function legacy(t, f, {empty = false} = {}) {
  const sqlite = new DatabaseSync(":memory:");
  t.after(() => sqlite.close());
  let writes = 0;
  const storage = {sql:{exec(sql,...values) {
    if (!/^\s*(SELECT|PRAGMA)/.test(sql)) writes++;
    const statement = sqlite.prepare(sql), rows = statement.columns().length ? statement.all(...values) : (statement.run(...values),[]);
    return {toArray:() => rows};
  }},transactionSync(fn) {
    sqlite.exec("BEGIN");try {const value=fn();sqlite.exec("COMMIT");return value;} catch(error) {sqlite.exec("ROLLBACK");throw error;}
  }};
  const old = empty ? null : new HistoryQueue({storage},{});
  let queue = old;
  const paths = [];
  f.env.HISTORY_QUEUE = {idFromName:name => name,get:() => ({fetch:request => {
    paths.push(new URL(request.url).pathname);
    return queue.fetch(request);
  }})};
  return {sqlite,old,paths,get writes() {return writes;},readOnly() {queue=new HistoryQueue({storage},f.env);return queue;}};
}

function bucket() {
  const objects = new Map();
  const f = {objects,calls:[],outage:false,failConfirmation:false};
  const metadata = row => row ? {key:row.key,size:row.bytes.byteLength,customMetadata:row.metadata,
    checksums:{sha256:Uint8Array.from(Buffer.from(row.sha,"hex")).buffer}} : null;
  f.head = async key => {f.calls.push("head");if(f.outage || (f.failConfirmation && objects.has(key))) throw new Error("R2 down");return metadata(objects.get(key));};
  f.put = async (key,bytes,options) => {
    f.calls.push("put");if(f.outage) throw new Error("R2 down");
    if (!objects.has(key)) objects.set(key,{key,bytes:Uint8Array.from(bytes),metadata:options.customMetadata,
      sha:createHash("sha256").update(bytes).digest("hex")});
    return metadata(objects.get(key));
  };
  f.get = async key => {f.calls.push("get");if(f.outage) throw new Error("R2 down");const row=objects.get(key);
    return row ? {...metadata(row),body:new Blob([row.bytes]).stream()} : null;};
  return f;
}

test("SQL helpers durably enqueue first full payload, stable IDs and receipts without DO contact", async t => {
  const f = fixture(t);
  const original = event("stable");delete original.event_id;
  const changed = structuredClone(original);changed.evidence.changed=true;
  const response = await enqueueDurableHistory(f.env,ledger([original,changed]));
  assert.equal(response.queued,1);assert.equal(response.duplicates,1);
  assert.deepEqual(JSON.parse(f.rows()[0].payload_json),original);
  const id = f.rows()[0].event_id, timer = f.rows()[0].next_attempt_at;
  assert.equal((await enqueueDurableHistory(f.env,ledger([changed]))).duplicates,1);
  assert.equal(f.rows()[0].next_attempt_at,timer);
  assert.deepEqual((await durableHistoryReceipts(f.env,[id,"unknown"])).receipts.map(row => row.status),["pending","unknown"]);
  const health = await durableHistoryStatus(f.env);
  assert.equal(health.backend,"turso_sql");assert.equal(health.do_write_units,0);assert.equal(health.migration_pending,false);
  assert.equal(health.limits.flushRequests,35);
  assert.equal(f.sql.filter(sql => /^\s*(CREATE|ALTER)/.test(sql)).length,0);
});

test("streamed SQL ingest validates size, envelopes and errors without a DO binding", async t => {
  const f = fixture(t), original = event("streamed");
  const response = await durableHistoryIngestResponse(f.env,new Request("https://worker/history",{method:"POST",body:JSON.stringify(ledger([original]))}));
  assert.equal(response.status,200);assert.equal((await response.json()).queued,1);
  const invalid = await durableHistoryIngestResponse(f.env,new Request("https://worker/history",{method:"POST",body:JSON.stringify({history_ledger:[]})}));
  assert.equal(invalid.status,400);
  await assert.rejects(durableHistoryReceipts(f.env,Array(26).fill("x")),/history_receipt_ids_invalid/);
  const large = Array.from({length:12},(_,i) => ({...event(`big${i}`),data:"x".repeat(95000)}));
  await assert.rejects(enqueueDurableHistory(f.env,ledger(large)),/history_request_oversize/);
});

test("explicit migration is required; flag errors never create schema or silently fall back", async t => {
  const f = fixture(t,{}, {migrate:false});
  await assert.rejects(enqueueDurableHistory(f.env,ledger([event("missing")])),/storage_sql_statement_failed/);
  assert.equal(f.sqlite.prepare("SELECT COUNT(*) n FROM sqlite_master WHERE name LIKE 'history_sql_queue%'").get().n,0);
  await assert.rejects(enqueueDurableHistory({...f.env,STORAGE_SQL_BACKEND:"d1"},ledger([])),/history_queue_turso_backend_required/);
  await assert.rejects(enqueueDurableHistory({...f.env,HISTORY_QUEUE_BACKEND:"typo"},ledger([])),/history_queue_backend_invalid/);
});

test("SQL enqueue rollback is atomic and a lost response retries idempotently", async t => {
  const f = fixture(t);
  f.sqlite.exec(`CREATE TRIGGER reject_second_test_event BEFORE INSERT ON history_sql_queue_events
    WHEN NEW.event_id='b' BEGIN SELECT RAISE(ABORT,'injected second-row failure'); END`);
  await assert.rejects(enqueueDurableHistory(f.env,ledger([event("a"),event("b")])),/storage_sql_statement_failed/);
  assert.equal(f.rows().length,0);assert.equal(f.meta().pending_rows,0);
  f.sqlite.exec("DROP TRIGGER reject_second_test_event");
  f.afterCommit = steps => {if(steps.some(step => /INSERT INTO history_sql_queue_events/.test(step.stmt.sql))) throw new Error("response lost");};
  await assert.rejects(enqueueDurableHistory(f.env,ledger([event("a")])),/storage_sql_network_error/);
  assert.equal(f.rows().length,1);
  f.afterCommit=null;
  assert.equal((await enqueueDurableHistory(f.env,ledger([event("a")]))).duplicates,1);
});

test("concurrent enqueue respects SQL-trigger capacity and preserves the first payload", async t => {
  const f = fixture(t,{HISTORY_QUEUE_MAX_PENDING_ROWS:1});
  const results = await Promise.allSettled([enqueueDurableHistory(f.env,ledger([event("one")])),enqueueDurableHistory(f.env,ledger([event("two")]))]);
  assert.equal(results.filter(row => row.status === "fulfilled").length,1);
  assert.equal(f.rows().length,1);assert.equal(f.meta().pending_rows,1);
});

test("conditional concurrent leases allow only one history writer", async t => {
  const f = fixture(t);await enqueueDurableHistory(f.env,ledger([event("leased")]));
  let release, started;
  const gate = new Promise(resolve => {release=resolve;}), entered = new Promise(resolve => {started=resolve;});
  f.before = async steps => {if(steps.some(step => /INSERT INTO signal_episodes/.test(step.stmt.sql))) {started();await gate;}};
  const first = flushDurableHistory(f.env);await entered;
  const token = f.rows()[0].lease_token;
  const second = await flushDurableHistory(f.env);
  assert.equal(second.delivered,0);assert.equal(f.rows()[0].lease_token,token);
  release();assert.equal((await first).delivered,1);
  assert.equal(f.sqlite.prepare("SELECT COUNT(*) n FROM signal_episode_events").get().n,1);
});

test("stale workers cannot save progress or release a replacement lease", async t => {
  const f = fixture(t);await enqueueDurableHistory(f.env,ledger([event("stale")]));
  let release, started, gated = false;
  const gate = new Promise(resolve => {release=resolve;}), entered = new Promise(resolve => {started=resolve;});
  f.before = async steps => {if(!gated && steps.some(step => /INSERT INTO signal_episodes/.test(step.stmt.sql))) {gated=true;started();await gate;}};
  const first = flushDurableHistory(f.env);await entered;
  f.advance(HISTORY_QUEUE_LIMITS.leaseMs+1);
  assert.equal((await flushDurableHistory(f.env)).delivered,1);
  release();await assert.rejects(first,/history_queue_lease_lost/);
  assert.equal(f.rows()[0].status,"delivered");assert.equal(f.meta().pending_rows,0);
});

test("cursor checkpoints survive receipt failure and resume without replaying source phases", async t => {
  const f = fixture(t);await enqueueDurableHistory(f.env,ledger([event("crash")]));
  f.failSql = sql => /SET status='delivered'/.test(sql);
  await assert.rejects(flushDurableHistory(f.env),/storage_sql_statement_failed/);
  assert.equal(JSON.parse(f.rows()[0].progress_json).phase,"done");
  assert.ok(f.rows()[0].payload_json);assert.ok(f.rows()[0].lease_token);
  const before = f.sql.filter(sql => /INSERT INTO signal_episode_events/.test(sql)).length;
  f.failSql=null;f.advance(HISTORY_QUEUE_LIMITS.leaseMs+1);
  assert.equal((await flushDurableHistory(f.env)).delivered,1);
  assert.equal(f.sql.filter(sql => /INSERT INTO signal_episode_events/.test(sql)).length,before);
});

test("low query limits preserve durable phase progress and retry promptly", async t => {
  const f = fixture(t,{HISTORY_QUEUE_FLUSH_QUERIES:1});
  await enqueueDurableHistory(f.env,ledger([event("resumed")]));
  const first = await flushDurableHistory(f.env);
  assert.equal(first.continued,1);assert.equal(first.history_queries,1);
  assert.equal(JSON.parse(f.rows()[0].progress_json).phase,"event");assert.equal(f.rows()[0].attempts,0);
  f.advance(1001);
  assert.equal((await flushDurableHistory(f.env)).delivered,1);
});

test("bounded flush handles multiple events and keeps per-episode source order", async t => {
  const f = fixture(t);
  const events = Array.from({length:25},(_,i) => event(`event-${String(i).padStart(2,"0")}`,{episode:"same",at:NOW-3600_000+i}));
  await enqueueDurableHistory(f.env,ledger(events.reverse()));
  const calls = f.calls.length, result = await flushDurableHistory(f.env);
  assert.equal(result.delivered,5);assert.ok(result.history_queries<=40);assert.ok(result.history_requests<=35);
  assert.equal(f.calls.length-calls,result.history_requests);
  const rows = f.rows();assert.equal(rows[0].status,"delivered");
  assert.ok(rows.slice(0,result.delivered).every(row => row.status === "delivered"));
  assert.equal(f.meta().last_flush_delivered,result.delivered);
});

test("R2 outage does not block SQL analytics or remove the last raw copy", async t => {
  const r2 = bucket();r2.outage=true;
  const f = fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:r2});
  const original = event("raw-without-r2");
  await enqueueDurableHistory(f.env,ledger([original]));
  const result = await flushDurableHistory(f.env);
  assert.equal(result.delivered,1);assert.equal(result.archive_pending,1);assert.equal(r2.calls.length,0);
  assert.deepEqual(JSON.parse(f.rows()[0].payload_json),original);
  assert.deepEqual(JSON.parse(f.sqlite.prepare("SELECT payload_json FROM signal_episode_events").get().payload_json),original);
  const failed = await flushDurableHistoryArchives(f.env);
  assert.equal(failed.failed,1);assert.equal(f.rows()[0].archive_pending,1);assert.ok(f.rows()[0].payload_json);
  assert.equal((await durableHistoryReceipts(f.env,[original.event_id])).receipts[0].status,"delivered");
  assert.equal((await durableHistoryStatus(f.env)).archive_pending,1);
  assert.equal((await durableHistoryStatus(f.env)).archive_pending_last_error,"history_archive_unavailable");
  r2.outage=false;f.advance(120001);
  const archived = await flushDurableHistoryArchives(f.env);
  assert.equal(archived.archived,1);assert.equal(f.rows()[0].payload_json,null);assert.equal(f.meta().archive_pending_bytes,0);
  assert.ok(JSON.parse(f.sqlite.prepare("SELECT payload_json FROM signal_episode_events").get().payload_json).archive_ref);
});

test("failed PUT confirmation retains raw SQL and never reports archive success", async t => {
  const r2 = bucket(), f = fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:r2});
  await enqueueDurableHistory(f.env,ledger([event("unconfirmed")]));await flushDurableHistory(f.env);
  r2.failConfirmation=true;
  const result = await flushDurableHistoryArchives(f.env);
  assert.equal(result.archived,0);assert.equal(result.failed,1);assert.ok(f.rows()[0].payload_json);
  r2.failConfirmation=false;f.advance(120001);
  assert.equal((await flushDurableHistoryArchives(f.env)).archived,1);
});

test("unarchived delivered receipts remain capacity-accounted and never expire raw payloads", async t => {
  const f = fixture(t,{HISTORY_ARCHIVE_MODE:"r2",HISTORY_QUEUE_MAX_PENDING_ROWS:1,HISTORY_QUEUE_MAX_RECEIPT_ROWS:1});
  await enqueueDurableHistory(f.env,ledger([event("retained")]));await flushDurableHistory(f.env);
  f.advance(31*86400_000);
  await assert.rejects(enqueueDurableHistory(f.env,ledger([event("new")])),/history_queue_pending_capacity/);
  assert.ok(f.rows()[0].payload_json);assert.equal(f.meta().archive_pending_rows,1);
});

test("legacy constructor/export/status are read-only even at an exhausted DO write budget", async t => {
  const f = fixture(t), old = legacy(t,f);
  old.old.enqueue([event("legacy")]);
  old.sqlite.prepare("UPDATE history_queue_meta SET do_writes=?").run(HISTORY_QUEUE_LIMITS.dailyDoWriteUnits);
  const writes = old.writes, queue = old.readOnly();
  const page = await exportLegacyDurableHistory(f.env,{limit:1});
  assert.equal(page.rows.length,1);assert.equal(page.read_only,true);assert.equal(page.complete,true);
  assert.equal(queue.status().pending,1);
  assert.throws(() => queue.enqueue([event("blocked")]),/history_legacy_queue_read_only/);
  assert.equal(old.writes,writes);
  const health = await durableHistoryStatus(f.env);assert.equal(health.migration_pending,true);
  await enqueueDurableHistory(f.env,ledger([event("new-sql")]));
  assert.equal((await flushDurableHistory(f.env)).reason,"history_legacy_migration_pending");
  assert.equal(old.writes,writes);assert.deepEqual(old.paths,["/export"]);
});

test("explicit bounded migration acknowledges SQL before advancing, preserving all legacy data", async t => {
  const f = fixture(t), old = legacy(t,f);
  old.old.enqueue([event("a"),event("b")]);old.readOnly();
  const options = {limit:1,legacyWritersStopped:true,historyStateMigrated:true};
  assert.equal((await migrateLegacyDurableHistory(f.env,{})).reason,"history_legacy_migration_prerequisites_required");
  const writes = old.writes;
  f.failSql = sql => /INSERT INTO history_sql_queue_events/.test(sql);
  await assert.rejects(migrateLegacyDurableHistory(f.env,options));
  assert.equal(f.meta().legacy_cursor,"");assert.equal(f.rows().length,0);
  f.failSql=null;
  assert.equal((await migrateLegacyDurableHistory(f.env,options)).migration_pending,true);
  assert.equal(f.meta().legacy_cursor,"a");assert.equal(f.rows().length,1);
  assert.equal((await migrateLegacyDurableHistory(f.env,options)).migration_pending,false);
  assert.equal(f.meta().legacy_cursor,"b");assert.equal(f.rows().length,2);
  assert.equal(old.sqlite.prepare("SELECT COUNT(*) n FROM history_queue_events").get().n,2);
  assert.equal(old.writes,writes);assert.equal((await flushDurableHistory(f.env)).delivered,2);
});

test("500 compact legacy receipts migrate in one bounded page, with atomic rollback and no DO writes", async t => {
  const f=fixture(t),old=legacy(t,f);
  for(let start=0;start<500;start+=25) old.old.enqueue(Array.from({length:25},(_,i)=>event(`receipt-${String(start+i).padStart(4,'0')}`)));
  old.sqlite.prepare(`UPDATE history_queue_events SET status='delivered',delivered_at=?,payload_json=NULL,
    payload_bytes=0,lease_token=NULL`).run(NOW);
  old.sqlite.prepare("UPDATE history_queue_meta SET pending_rows=0,pending_bytes=0,delivered_rows=500").run();
  old.readOnly();const writes=old.writes;
  const page=await exportLegacyDurableHistory(f.env);
  assert.equal(page.rows.length,500);assert.equal(page.complete,true);
  let inserts=0;f.failSql=sql=>/INSERT INTO history_sql_queue_events/.test(sql) && ++inserts===2;
  const options={legacyWritersStopped:true,historyStateMigrated:true};
  await assert.rejects(migrateLegacyDurableHistory(f.env,options),/storage_sql_statement_failed/);
  assert.equal(f.meta().legacy_cursor,'');assert.equal(f.rows().length,0);
  f.failSql=null;
  const result=await migrateLegacyDurableHistory(f.env,options);
  assert.equal(result.complete,true);assert.equal(result.queued,500);assert.ok(result.requests<=35);
  assert.equal(f.meta().delivered_rows,500);assert.equal(f.meta().legacy_imported,500);
  assert.ok(f.calls.every(call=>call.requests[0].batch.steps.length<100));
  assert.equal(old.writes,writes);
  assert.equal((await enqueueDurableHistory(f.env,ledger([event('receipt-0000')]))).duplicates,1);
});

test("larger receipt pages do not relax the 25-pending-event admission bound",async t=>{
  const f=fixture(t),old=legacy(t,f);
  old.old.enqueue(Array.from({length:25},(_,i)=>event(`pending-${String(i).padStart(3,'0')}`)));
  old.old.enqueue([event('pending-025')]);old.readOnly();
  const first=await exportLegacyDurableHistory(f.env);
  assert.equal(first.rows.length,25);assert.equal(first.complete,false);assert.equal(first.after,'pending-024');
  const second=await exportLegacyDurableHistory(f.env,{after:first.after});
  assert.equal(second.rows.length,1);assert.equal(second.complete,true);
  await assert.rejects(exportLegacyDurableHistory(f.env,{limit:501}),/export_cursor_invalid/);
  await assert.rejects(enqueueDurableHistory(f.env,ledger(Array.from({length:26},(_,i)=>event(`live-${i}`)))),/history_batch_max_25/);
});

test("cutover stages new raw data apart from inaccessible legacy receipts and resumes after sweep", async t => {
  const f=fixture(t,{HISTORY_LEGACY_MIGRATION:"verified_turso_v1"}),old=legacy(t,f);
  const original=event("same");
  old.old.enqueue([original]);old.old.claim(NOW);
  old.sqlite.prepare("UPDATE history_queue_events SET status='delivered',delivered_at=?,payload_json=NULL,payload_bytes=0,lease_token=NULL WHERE event_id='same'").run(NOW);
  old.readOnly();
  const first=event("fresh"),changed=structuredClone(first);changed.evidence.changed=true;
  const saved=await enqueueDurableHistory(f.env,ledger([original,first]));
  assert.equal(saved.staged,true);assert.equal(saved.cutover_pending,2);assert.equal(f.rows().length,0);
  await enqueueDurableHistory(f.env,ledger([changed]));
  assert.deepEqual(JSON.parse(f.sqlite.prepare("SELECT payload_json FROM history_sql_cutover_events WHERE event_id='fresh'").get().payload_json),first);
  assert.equal((await flushDurableHistory(f.env)).migration_pending,true);
  const oldWrites=old.writes;
  await migrateLegacyDurableHistory(f.env,{limit:25,legacyWritersStopped:true,historyStateMigrated:true});
  const result=await flushDurableHistory(f.env);
  assert.equal(result.delivered,1);assert.equal(result.cutover_drain.forwarded,2);
  assert.equal(f.sqlite.prepare("SELECT pending_rows FROM history_sql_cutover_meta WHERE id=1").get().pending_rows,0);
  assert.equal(old.writes,oldWrites);assert.equal(f.rows().find(row=>row.event_id==='same').status,'delivered');
});

test("cutover SQL failure cannot acknowledge raw staging or remove the last copy", async t => {
  const f=fixture(t,{HISTORY_LEGACY_MIGRATION:"verified_turso_v1"});legacy(t,f,{empty:true});
  f.failSql=sql=>/INSERT OR IGNORE INTO history_sql_cutover_events/.test(sql);
  await assert.rejects(enqueueDurableHistory(f.env,ledger([event("raw")])));
  assert.equal(f.sqlite.prepare("SELECT pending_rows FROM history_sql_cutover_meta WHERE id=1").get().pending_rows,0);
  f.failSql=null;await enqueueDurableHistory(f.env,ledger([event("raw")]));
  f.sqlite.prepare("UPDATE history_sql_queue_meta SET legacy_complete=1").run();
  f.failSql=sql=>/INSERT INTO history_sql_queue_events/.test(sql);
  await assert.rejects(flushDurableHistory(f.env));
  assert.equal(f.sqlite.prepare("SELECT COUNT(*) n FROM history_sql_cutover_events").get().n,1);
});

test("live legacy leases and conflicting first payloads keep migration_pending truthful", async t => {
  const f = fixture(t), old = legacy(t,f);
  old.old.enqueue([event("active")]);old.old.claim(NOW);old.readOnly();
  const options = {legacyWritersStopped:true,historyStateMigrated:true};
  assert.equal((await migrateLegacyDurableHistory(f.env,options)).reason,"history_legacy_active_lease");
  assert.equal(f.rows().length,0);
  f.advance(HISTORY_QUEUE_LIMITS.leaseMs+1);
  const different = event("active");different.evidence.changed=true;
  await enqueueDurableHistory(f.env,ledger([different]));
  await assert.rejects(migrateLegacyDurableHistory(f.env,options),/history_legacy_migration_payload_conflict/);
  assert.equal((await durableHistoryStatus(f.env)).migration_pending,true);assert.equal(f.meta().legacy_cursor,"");
});

test("an uninitialized legacy DO exports empty without initializing its SQLite schema", async t => {
  const f = fixture(t), old = legacy(t,f,{empty:true}), queue = old.readOnly();
  assert.equal(queue.status().pending,0);assert.equal((await exportLegacyDurableHistory(f.env)).complete,true);
  assert.equal(old.writes,0);
  assert.equal((await migrateLegacyDurableHistory(f.env,{legacyWritersStopped:true,historyStateMigrated:true})).migration_pending,false);
  assert.equal(old.writes,0);
});

test("legacy immutable envelopes migrate without any R2 hydration and preserve progress/timers", async t => {
  const r2 = bucket(), f = fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:r2}), old = legacy(t,f);
  const original = event("archived-legacy"), ref = await archiveHistoryEvent(f.env,original);
  old.old.enqueue([original]);
  const envelope = JSON.stringify({archive_queue_version:1,archive_ref:ref,work:{minimum:8,edges:8,initial_queries:2}});
  const progress = JSON.stringify({version:1,phase:"episode"});
  old.sqlite.prepare(`UPDATE history_queue_events SET payload_json=?,payload_bytes=?,archive_version=1,
    progress_json=?,attempts=3,next_attempt_at=? WHERE event_id=?`)
    .run(envelope,Buffer.byteLength(envelope),progress,NOW-1,original.event_id);
  old.readOnly();r2.outage=true;
  const reads = r2.calls.length, writes = old.writes;
  const imported = await migrateLegacyDurableHistory(f.env,{legacyWritersStopped:true,historyStateMigrated:true});
  assert.ok(imported.requests<=35);
  assert.equal(imported.complete,true);assert.equal(imported.migration_pending,false);
  const row = f.rows()[0];
  assert.equal(row.payload_json,envelope);assert.equal(row.payload_bytes,Buffer.byteLength(envelope));
  assert.equal(row.archive_version,1);assert.equal(row.progress_json,progress);assert.equal(row.attempts,3);
  assert.equal(row.next_attempt_at,NOW-1);assert.equal(r2.calls.length,reads);assert.equal(old.writes,writes);
  await enqueueDurableHistory(f.env,ledger([event("independent-new")]));
  const failed = await flushDurableHistory(f.env);
  assert.equal(failed.failed,1);assert.equal(failed.delivered,1);
  assert.equal(f.rows().find(row => row.event_id===original.event_id).payload_json,envelope);
  r2.outage=false;f.advance(16*60_000+1);
  assert.equal((await flushDurableHistory(f.env)).delivered,1);
  assert.equal(old.sqlite.prepare("SELECT payload_json FROM history_queue_events").get().payload_json,envelope);
});

test("legacy envelope identity mismatch never advances the migration cursor", async t => {
  const r2 = bucket(), f = fixture(t,{RADAR_ARCHIVE:r2}), old = legacy(t,f);
  const original = event("identity"), ref = await archiveHistoryEvent(f.env,original);
  old.old.enqueue([original]);
  ref.episode_id="different";
  const envelope = JSON.stringify({archive_queue_version:1,archive_ref:ref,work:{minimum:8,edges:8,initial_queries:2}});
  old.sqlite.prepare("UPDATE history_queue_events SET payload_json=?,archive_version=1").run(envelope);
  old.readOnly();r2.outage=true;
  await assert.rejects(migrateLegacyDurableHistory(f.env,{legacyWritersStopped:true,historyStateMigrated:true}),/history_archive_queue_identity_mismatch/);
  assert.equal(f.meta().legacy_cursor,"");assert.equal(f.meta().legacy_complete,0);assert.equal(f.rows().length,0);
});

test("delivered legacy receipts migrate durably and suppress duplicate SQL enqueue", async t => {
  const f = fixture(t), old = legacy(t,f), original = event("old-receipt");
  old.old.enqueue([original]);
  old.sqlite.prepare(`UPDATE history_queue_events SET status='delivered',delivered_at=?,payload_json=NULL,
    payload_bytes=0 WHERE event_id=?`).run(NOW-1,original.event_id);
  old.readOnly();
  const result = await migrateLegacyDurableHistory(f.env,{legacyWritersStopped:true,historyStateMigrated:true});
  assert.equal(result.complete,true);assert.equal(f.rows()[0].status,"delivered");
  assert.equal((await enqueueDurableHistory(f.env,ledger([original]))).duplicates,1);
});

test("archive consumers have conditional leases and failed SQL ack never deletes raw", async t => {
  const r2 = bucket(), f = fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:r2});
  await enqueueDurableHistory(f.env,ledger([event("archive-lease")]));await flushDurableHistory(f.env);
  const head = r2.head;
  let release, started;
  const gate = new Promise(resolve => {release=resolve;}), entered = new Promise(resolve => {started=resolve;});
  r2.head=async key => {started();await gate;return head(key);};
  const first = flushDurableHistoryArchives(f.env);await entered;
  assert.equal((await flushDurableHistoryArchives(f.env)).archived,0);
  f.failSql=sql => /SET archive_pending=0/.test(sql);
  release();await assert.rejects(first,/storage_sql_statement_failed/);
  assert.ok(f.rows()[0].payload_json);assert.equal(f.rows()[0].archive_pending,1);
  assert.equal(JSON.parse(f.sqlite.prepare("SELECT payload_json FROM signal_episode_events").get().payload_json).archive_ref,undefined,
    "canonical compaction rolls back atomically with failed archive receipt");
  f.failSql=null;r2.head=head;f.advance(HISTORY_QUEUE_LIMITS.leaseMs+1);
  assert.equal((await flushDurableHistoryArchives(f.env)).archived,1);
  assert.equal(r2.calls.filter(method => method === "put").length,1);
});

test("SQL queue EXPLAIN uses pending/episode/receipt/archive indexes rather than JSON scans", t => {
  const f = fixture(t);
  const plans = [
    ["SELECT event_id FROM history_sql_queue_events WHERE status='pending' AND next_attempt_at<=? ORDER BY next_attempt_at,source_at,event_id LIMIT 25",[NOW],"idx_history_sql_queue_due"],
    ["SELECT event_id FROM history_sql_queue_events WHERE status='pending' AND episode_id=? AND (source_at,event_id)<(?,?)",["episode",NOW,"id"],"idx_history_sql_queue_episode"],
    ["SELECT event_id FROM history_sql_queue_events WHERE status='delivered' AND archive_pending=0 AND delivered_at<? ORDER BY delivered_at,event_id LIMIT 25",[NOW],"idx_history_sql_queue_receipts"],
    ["SELECT event_id FROM history_sql_queue_events WHERE archive_pending=1 AND archive_next_attempt_at<=? ORDER BY archive_next_attempt_at,event_id LIMIT 5",[NOW],"idx_history_sql_queue_archive_due"],
  ];
  for (const [sql,args,index] of plans) {
    const plan = f.sqlite.prepare(`EXPLAIN QUERY PLAN ${sql}`).all(...args).map(row => row.detail).join("\n");
    assert.ok(plan.includes(index),plan);assert.ok(!plan.includes("USE TEMP B-TREE"),plan);
  }
});

test("SQL daily write reservations persist across restarts and reset only at UTC midnight", async t => {
  const f = fixture(t,{HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:8});
  await enqueueDurableHistory(f.env,ledger([event("daily")]));
  const first = await flushDurableHistory(f.env);
  assert.equal(first.continued,1);assert.equal(f.meta().hist_writes,8);
  f.advance(1001);
  await assert.rejects(flushDurableHistory(f.env),/history_daily_write_budget/);
  assert.equal((await durableHistoryStatus(f.env)).history_budget_exhausted,true);
  assert.ok(f.rows()[0].payload_json);assert.equal(JSON.parse(f.rows()[0].progress_json).phase,"event");
  f.advance(86400_000);
  assert.equal((await durableHistoryStatus(f.env)).history_budget_exhausted,false);
  assert.equal((await flushDurableHistory(f.env)).delivered,1);assert.equal(f.meta().hist_writes,8);
});

test("fresh events have reserved capacity and are processed ahead of cold recovery",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_MAX_PENDING_ROWS:1,HISTORY_QUEUE_LIVE_RESERVED_ROWS:1,HISTORY_QUEUE_FLUSH_QUERIES:1});
  const cold=event("cold",{at:NOW-48*3600_000});cold.episode.caught_at=iso(NOW-72*3600_000);
  await enqueueDurableHistory(f.env,ledger([cold]));
  const second=structuredClone(cold);second.event_id="cold-2";second.episode.episode_id="cold-2";
  await assert.rejects(enqueueDurableHistory(f.env,ledger([second])),/pending_capacity/);
  await enqueueDurableHistory(f.env,ledger([event("fresh")]));
  await flushDurableHistory(f.env);
  assert.equal(JSON.parse(f.rows().find(row=>row.event_id==="fresh").progress_json).phase,"event");
  assert.equal(f.rows().find(row=>row.event_id==="cold").progress_json,null);
});

test("fresh episode prerequisites use reserved slots without rewriting catch times",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_MAX_PENDING_ROWS:1,HISTORY_QUEUE_LIVE_RESERVED_ROWS:1});
  const cold=event("cold",{at:NOW-48*3600_000});cold.episode.caught_at=iso(NOW-72*3600_000);
  await enqueueDurableHistory(f.env,ledger([cold]));
  const prerequisite=structuredClone(cold);prerequisite.event_id="prior";prerequisite.episode.episode_id="current";
  await enqueueDurableHistory(f.env,{...ledger([prerequisite]),generated_at:iso(NOW),priority_episodes:["current"]});
  assert.equal(f.rows().length,2);
  assert.deepEqual(JSON.parse(f.rows().find(row=>row.event_id==="prior").payload_json),prerequisite);
});

test("concurrent cold admission cannot consume any of the 512 reserved live slots",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_MAX_PENDING_ROWS:2,HISTORY_QUEUE_LIVE_RESERVED_ROWS:512});
  await enqueueDurableHistory(f.env,ledger([coldEvent("cold-existing")]));
  let release, arrivals=0;
  const gate=new Promise(resolve=>{release=resolve;});
  f.before=async steps=>{
    if (!steps.some(step=>/INSERT INTO history_sql_queue_events/.test(step.stmt.sql))) return;
    if (++arrivals===2) release();
    await gate;
  };
  const results=await Promise.allSettled([
    enqueueDurableHistory(f.env,ledger([coldEvent("cold-race-a")])),
    enqueueDurableHistory(f.env,ledger([coldEvent("cold-race-b")])),
  ]);
  f.before=null;
  assert.equal(arrivals,2,"both requests must pass their stale preflight before either INSERT");
  assert.equal(results.filter(row=>row.status==="fulfilled").length,1);
  assert.match(results.find(row=>row.status==="rejected").reason.message,/storage_sql_statement_failed:SQLITE_CONSTRAINT/);
  assert.equal(f.meta().pending_rows,2);
  assert.equal(f.meta().max_pending_rows,514);
  for (let start=0;start<512;start+=25) {
    const count=Math.min(25,512-start);
    const saved=await enqueueDurableHistory(f.env,ledger(Array.from({length:count},(_,i)=>event(`live-${start+i}`))));
    assert.equal(saved.queued,count);
  }
  assert.equal(f.meta().pending_rows,514);
  assert.equal(f.rows().filter(row=>JSON.parse(row.progress_json || "{}")._live_until).length,512);
  await assert.rejects(enqueueDurableHistory(f.env,ledger([event("live-over-capacity")])),/history_queue_pending_capacity/);
  assert.equal(f.meta().pending_rows,514);
});

test("SQL cold capacity rolls back a mixed live/cold batch after concurrent admission",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_MAX_PENDING_ROWS:2,HISTORY_QUEUE_LIVE_RESERVED_ROWS:2});
  let release, entered;
  const gate=new Promise(resolve=>{release=resolve;}), started=new Promise(resolve=>{entered=resolve;});
  f.before=async steps=>{
    if (steps.some(step=>/INSERT INTO history_sql_queue_events/.test(step.stmt.sql)
        && step.stmt.args.some(arg=>arg.value==="mixed-live"))) {entered();await gate;}
  };
  const mixed=enqueueDurableHistory(f.env,ledger([event("mixed-live"),coldEvent("mixed-cold")]));
  const rejection=assert.rejects(mixed,/storage_sql_statement_failed:SQLITE_CONSTRAINT/);
  await started;
  try {
    await enqueueDurableHistory(f.env,ledger([coldEvent("winner-a"),coldEvent("winner-b")]));
  } finally {release();}
  await rejection;f.before=null;
  assert.deepEqual(f.rows().map(row=>row.event_id),["winner-a","winner-b"]);
  assert.equal(f.meta().pending_rows,2);
  assert.equal(f.meta().pending_bytes,f.rows().reduce((sum,row)=>sum+row.payload_bytes,0));
  await enqueueDurableHistory(f.env,ledger([event("live-after-rollback")]));
  assert.equal(f.meta().pending_rows,3);
});

test("cold archive-pending payloads consume cold capacity but leave live slots available",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_MAX_PENDING_ROWS:1,HISTORY_QUEUE_LIVE_RESERVED_ROWS:1,HISTORY_ARCHIVE_MODE:"r2"});
  const cold=coldEvent("cold-archive");
  await enqueueDurableHistory(f.env,ledger([cold]));
  assert.equal((await flushDurableHistory(f.env)).delivered,1);
  assert.equal(f.meta().pending_rows,0);assert.equal(f.meta().archive_pending_rows,1);
  await assert.rejects(enqueueDurableHistory(f.env,ledger([coldEvent("second-cold")])),/history_queue_pending_capacity/);
  await enqueueDurableHistory(f.env,ledger([event("fresh-with-archive-backlog")]));
  assert.equal(f.meta().pending_rows,1);assert.equal(f.meta().archive_pending_rows,1);
  assert.deepEqual(JSON.parse(f.rows().find(row=>row.event_id===cold.event_id).payload_json),cold);
});

test("cold work stops at 90000 while live can use the rest of the 180000 total",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:180000,HISTORY_QUEUE_LIVE_RESERVED_UNITS:90000,
    HISTORY_QUEUE_FLUSH_QUERIES:1});
  await enqueueDurableHistory(f.env,ledger([coldEvent("cold-budget")]));
  seedHistoryBudget(f,89992);
  assert.equal((await flushDurableHistory(f.env)).continued,1);
  assert.equal(f.meta().hist_writes,90000);
  f.advance(1001);
  await assert.rejects(flushDurableHistory(f.env),/history_live_budget_reserved/);
  assert.equal(f.meta().hist_writes,90000);
  assert.equal(f.rows()[0].lease_token,null);
  await enqueueDurableHistory(f.env,ledger([event("live-budget")]));
  assert.equal((await flushDurableHistory(f.env)).continued,1);
  assert.equal(f.meta().hist_writes,90008);
  assert.equal(JSON.parse(f.rows().find(row=>row.event_id==="cold-budget").progress_json).phase,"event");
  seedHistoryBudget(f,179992);
  f.advance(1001);
  assert.equal((await flushDurableHistory(f.env)).delivered,1);
  assert.equal(f.meta().hist_writes,180000);
  await enqueueDurableHistory(f.env,ledger([event("live-budget-exhausted")]));
  await assert.rejects(flushDurableHistory(f.env),/history_daily_write_budget/);
  assert.equal(f.meta().hist_writes,180000);
  assert.equal((await durableHistoryStatus(f.env)).history_budget_exhausted,true);
});

test("a cold claim with a stale preflight cannot spend reserved live work",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:180000,HISTORY_QUEUE_LIVE_RESERVED_UNITS:90000,
    HISTORY_QUEUE_FLUSH_QUERIES:1});
  await enqueueDurableHistory(f.env,ledger([coldEvent("cold-stale-budget")]));
  seedHistoryBudget(f,89992);
  let release, entered, gated=false;
  const gate=new Promise(resolve=>{release=resolve;}), started=new Promise(resolve=>{entered=resolve;});
  f.before=async steps=>{
    if (!gated && steps.some(step=>/budget_reserved=MIN/.test(step.stmt.sql))) {
      gated=true;entered();await gate;
    }
  };
  const cold=flushDurableHistory(f.env);await started;
  try {
    await enqueueDurableHistory(f.env,ledger([event("live-budget-winner")]));
    assert.equal((await flushDurableHistory(f.env)).continued,1);
    assert.equal(f.meta().hist_writes,90000);
  } finally {release();}
  assert.equal((await cold).delivered,0);f.before=null;
  const row=f.rows().find(row=>row.event_id==="cold-stale-budget");
  assert.equal(row.progress_json,null);assert.equal(row.lease_token,null);assert.equal(row.budget_reserved,0);
  assert.equal(f.meta().hist_writes,90000);
});

test("a fresh episode processes its oldest prerequisites before the new observation",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:180000,HISTORY_QUEUE_LIVE_RESERVED_UNITS:90000,
    HISTORY_QUEUE_LIVE_RESERVED_ROWS:512,HISTORY_QUEUE_FLUSH_QUERIES:1});
  const first=coldEvent("prior-a",{episode:"current",at:NOW-48*3600_000});
  const second=coldEvent("prior-b",{episode:"current",at:NOW-36*3600_000});
  second.episode.caught_at=first.episode.caught_at;
  const fresh=event("current-check",{episode:"current"});
  fresh.episode.caught_at=first.episode.caught_at;fresh.episode.caught_mcap_usd=999999;
  const unrelated=coldEvent("unrelated-cold",{at:NOW-96*3600_000});
  await enqueueDurableHistory(f.env,ledger([second,unrelated,first,fresh]));
  seedHistoryBudget(f,90000);
  const order=[];
  f.afterCommit=(steps,errors)=>{
    steps.forEach((step,index)=>{
      if (!errors[index] && /INSERT OR IGNORE INTO signal_episode_events/.test(step.stmt.sql)) order.push(step.stmt.args[0].value);
    });
  };
  for (let attempt=0;attempt<8;attempt++) {
    await flushDurableHistory(f.env);
    if (f.rows().find(row=>row.event_id===fresh.event_id).status==="delivered") break;
    f.advance(1001);
  }
  assert.deepEqual(order,[first.event_id,second.event_id,fresh.event_id]);
  assert.equal(f.rows().find(row=>row.event_id===unrelated.event_id).progress_json,null);
  assert.equal(f.rows().find(row=>row.event_id===fresh.event_id).status,"delivered");
  const episode=f.sqlite.prepare("SELECT caught_at,caught_mcap_usd FROM signal_episodes WHERE episode_id='current'").get();
  assert.equal(episode.caught_at,first.episode.caught_at);assert.equal(episode.caught_mcap_usd,first.episode.caught_mcap_usd);
  const stored=f.sqlite.prepare("SELECT event_id,observed_at,payload_json FROM signal_episode_events WHERE episode_id='current' ORDER BY observed_at,event_id").all();
  for (const [index,original] of [first,second,fresh].entries()) {
    assert.equal(stored[index].observed_at,original.event.observed_at);
    assert.deepEqual(JSON.parse(stored[index].payload_json),original);
    assert.equal(f.rows().find(row=>row.event_id===original.event_id).source_at,Date.parse(original.event.observed_at));
  }
});

test("retry backoff on an older prerequisite prevents a fresh event overtaking its episode",async t=>{
  const f=fixture(t);
  const prior=coldEvent("blocked-prior",{episode:"blocked"});
  const fresh=event("blocked-fresh",{episode:"blocked"});fresh.episode.caught_at=prior.episode.caught_at;
  await enqueueDurableHistory(f.env,ledger([fresh,prior,event("independent-live")]));
  f.sqlite.prepare("UPDATE history_sql_queue_events SET next_attempt_at=? WHERE event_id=?").run(NOW+120000,prior.event_id);
  const first=await flushDurableHistory(f.env);
  assert.equal(first.delivered,1);
  assert.equal(f.rows().find(row=>row.event_id===fresh.event_id).progress_json,JSON.stringify({_live_until:NOW+24*3600_000}));
  assert.equal(f.sqlite.prepare("SELECT COUNT(*) n FROM signal_episode_events WHERE episode_id='blocked'").get().n,0);
  f.advance(120000);
  assert.equal((await flushDurableHistory(f.env)).delivered,2);
  const stored=f.sqlite.prepare("SELECT event_id,observed_at FROM signal_episode_events WHERE episode_id='blocked' ORDER BY rowid").all();
  assert.deepEqual(stored.map(row=>row.event_id),[prior.event_id,fresh.event_id]);
  assert.deepEqual(stored.map(row=>row.observed_at),[prior.event.observed_at,fresh.event.observed_at]);
});

test("only a current generated_at can give an old prerequisite live priority",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_MAX_PENDING_ROWS:1,HISTORY_QUEUE_LIVE_RESERVED_ROWS:1});
  await enqueueDurableHistory(f.env,ledger([coldEvent("cold-full")]));
  const prerequisite=coldEvent("priority-boundary",{episode:"current"});
  for (const generated_at of [iso(NOW-24*3600_000-1),iso(NOW+300001),"not-a-date"]) {
    await assert.rejects(enqueueDurableHistory(f.env,{...ledger([prerequisite]),generated_at,priority_episodes:["current"]}),
      /history_queue_pending_capacity/);
    assert.equal(f.meta().pending_rows,1);
  }
  await enqueueDurableHistory(f.env,{...ledger([prerequisite]),generated_at:iso(NOW-24*3600_000),priority_episodes:["current"]});
  const row=f.rows().find(row=>row.event_id===prerequisite.event_id);
  assert.deepEqual(JSON.parse(row.payload_json),prerequisite);
  assert.equal(row.source_at,Date.parse(prerequisite.event.observed_at));
  assert.equal(JSON.parse(row.progress_json)._live_until,NOW+24*3600_000);
});

test("live priority promotes an already queued prerequisite without replacing its source payload",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:180000,HISTORY_QUEUE_LIVE_RESERVED_UNITS:90000,
    HISTORY_QUEUE_LIVE_RESERVED_ROWS:512,HISTORY_QUEUE_FLUSH_QUERIES:1});
  const original=coldEvent("existing-prerequisite",{episode:"current"});
  await enqueueDurableHistory(f.env,ledger([original]));
  const changed=structuredClone(original);changed.evidence.changed=true;
  const duplicate=await enqueueDurableHistory(f.env,{...ledger([changed]),generated_at:iso(NOW),priority_episodes:["current"]});
  assert.equal(duplicate.queued,0);assert.equal(duplicate.duplicates,1);
  const stored=f.rows()[0];
  assert.deepEqual(JSON.parse(stored.payload_json),original);
  assert.equal(stored.source_at,Date.parse(original.event.observed_at));
  seedHistoryBudget(f,90000);
  assert.equal((await flushDurableHistory(f.env)).continued,1);
  assert.equal(f.meta().hist_writes,90008);
  assert.equal(JSON.parse(f.rows()[0].progress_json).phase,"event");
});

test("live promotion survives a leased writer saving its pre-promotion phase cursor",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:180000,HISTORY_QUEUE_LIVE_RESERVED_UNITS:90000,
    HISTORY_QUEUE_LIVE_RESERVED_ROWS:512,HISTORY_QUEUE_FLUSH_QUERIES:1});
  const original=coldEvent("promotion-phase-race",{episode:"current"});
  await enqueueDurableHistory(f.env,ledger([original]));
  let release, entered, gated=false;
  const gate=new Promise(resolve=>{release=resolve;}), started=new Promise(resolve=>{entered=resolve;});
  f.before=async steps=>{
    if (!gated && steps.some(step=>/SET progress_json=\?3/.test(step.stmt.sql))) {
      gated=true;entered();await gate;
    }
  };
  const writer=flushDurableHistory(f.env);await started;
  const held=f.rows()[0];
  assert.ok(held.lease_token);assert.equal(held.progress_json,null);
  try {
    const response=await enqueueDurableHistory(f.env,{...ledger([original]),generated_at:iso(NOW),priority_episodes:["current"]});
    assert.equal(response.queued,0);assert.equal(response.duplicates,1);
    const promoted=f.rows()[0];
    for (const key of ["source_at","payload_json","progress_json","lease_token","lease_until","attempts","budget_reserved"]) {
      assert.equal(promoted[key],held[key],`promotion must not rewrite the active writer's ${key}`);
    }
    assert.equal(f.sqlite.prepare("SELECT priority_until FROM history_sql_live_priority WHERE event_id=?").get(original.event_id).priority_until,
      NOW+24*3600_000);
    const competing=await flushDurableHistory(f.env);
    assert.equal(competing.delivered,0);assert.equal(competing.continued,0);
    assert.equal(f.rows()[0].lease_token,held.lease_token);
  } finally {release();}
  assert.equal((await writer).continued,1);f.before=null;
  assert.equal(JSON.parse(f.rows()[0].progress_json).phase,"event");
  assert.equal(JSON.parse(f.rows()[0].progress_json)._live_until,undefined,
    "the writer really saved a cursor captured before the promotion");
  assert.equal(f.rows()[0].lease_token,null);
  seedHistoryBudget(f,90000);f.advance(1001);
  assert.equal((await flushDurableHistory(f.env)).delivered,1);
  assert.equal(f.meta().hist_writes,90008);
  const stored=f.sqlite.prepare("SELECT payload_json,observed_at FROM signal_episode_events WHERE event_id=?").get(original.event_id);
  assert.deepEqual(JSON.parse(stored.payload_json),original);assert.equal(stored.observed_at,original.event.observed_at);
  assert.equal(f.sql.filter(sql=>/INSERT INTO signal_episodes/.test(sql)).length,1);
});

test("fresh cutover staging can drain into reserved live capacity while cold slots are full",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_MAX_PENDING_ROWS:1,HISTORY_QUEUE_LIVE_RESERVED_ROWS:512,
    HISTORY_LEGACY_MIGRATION:"verified_turso_v1"});
  const cold=coldEvent("cold-before-cutover");
  await enqueueDurableHistory(f.env,ledger([cold]));
  legacy(t,f,{empty:true}).readOnly();
  const fresh=event("fresh-cutover-reserved");
  assert.equal((await enqueueDurableHistory(f.env,ledger([fresh]))).staged,true);
  assert.equal(f.sqlite.prepare("SELECT pending_rows FROM history_sql_cutover_meta WHERE id=1").get().pending_rows,1);
  assert.equal((await migrateLegacyDurableHistory(f.env,{legacyWritersStopped:true,historyStateMigrated:true})).complete,true);
  const result=await flushDurableHistory(f.env);
  assert.equal(result.cutover_drain.forwarded,1);
  assert.equal(f.sqlite.prepare("SELECT pending_rows FROM history_sql_cutover_meta WHERE id=1").get().pending_rows,0);
  const stored=f.sqlite.prepare("SELECT payload_json,observed_at FROM signal_episode_events WHERE event_id=?").get(fresh.event_id);
  assert.equal(stored.observed_at,fresh.event.observed_at);assert.deepEqual(JSON.parse(stored.payload_json),fresh);
  assert.equal(f.rows().find(row=>row.event_id===fresh.event_id).status,"delivered");
});

test("cutover preserves trusted live priority for an old prerequisite without changing its timestamps",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_MAX_PENDING_ROWS:1,HISTORY_QUEUE_LIVE_RESERVED_ROWS:512,
    HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:180000,HISTORY_QUEUE_LIVE_RESERVED_UNITS:90000,
    HISTORY_QUEUE_FLUSH_QUERIES:2,
    HISTORY_LEGACY_MIGRATION:"verified_turso_v1"});
  await enqueueDurableHistory(f.env,ledger([coldEvent("cold-before-priority-cutover")]));
  legacy(t,f,{empty:true}).readOnly();
  const prior=coldEvent("old-live-cutover-prerequisite",{episode:"current"});
  const saved=await enqueueDurableHistory(f.env,{...ledger([prior]),generated_at:iso(NOW),priority_episodes:["current"]});
  assert.equal(saved.staged,true);
  assert.equal(saved.queued,1);assert.equal(saved.duplicates,0);
  const staged=f.sqlite.prepare("SELECT source_at,payload_json FROM history_sql_cutover_events WHERE event_id=?").get(prior.event_id);
  assert.equal(staged.source_at,Date.parse(prior.event.observed_at));assert.deepEqual(JSON.parse(staged.payload_json),prior);
  assert.equal((await migrateLegacyDurableHistory(f.env,{legacyWritersStopped:true,historyStateMigrated:true})).complete,true);
  seedHistoryBudget(f,90000);
  const result=await flushDurableHistory(f.env);
  assert.equal(result.cutover_drain.forwarded,1);assert.equal(result.delivered,1);
  assert.equal(f.sqlite.prepare("SELECT pending_rows FROM history_sql_cutover_meta WHERE id=1").get().pending_rows,0);
  const stored=f.sqlite.prepare("SELECT payload_json,observed_at FROM signal_episode_events WHERE event_id=?").get(prior.event_id);
  assert.equal(stored.observed_at,prior.event.observed_at);assert.deepEqual(JSON.parse(stored.payload_json),prior);
  assert.equal(f.sqlite.prepare("SELECT caught_at FROM signal_episodes WHERE episode_id='current'").get().caught_at,prior.episode.caught_at);
});

test("atomic cold capacity follows a changed live reservation rather than the first trigger definition",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_MAX_PENDING_ROWS:2,HISTORY_QUEUE_LIVE_RESERVED_ROWS:1});
  await enqueueDurableHistory(f.env,ledger([coldEvent("before-reservation-change")]));
  f.env.HISTORY_QUEUE_LIVE_RESERVED_ROWS=512;
  let release, arrivals=0;
  const gate=new Promise(resolve=>{release=resolve;});
  f.before=async steps=>{
    if (!steps.some(step=>/INSERT INTO history_sql_queue_events/.test(step.stmt.sql))) return;
    if (++arrivals===2) release();
    await gate;
  };
  const results=await Promise.allSettled([
    enqueueDurableHistory(f.env,ledger([coldEvent("resized-race-a")])),
    enqueueDurableHistory(f.env,ledger([coldEvent("resized-race-b")])),
  ]);
  f.before=null;
  assert.equal(arrivals,2);
  assert.equal(results.filter(row=>row.status==="fulfilled").length,1);
  assert.equal(f.meta().pending_rows,2);assert.equal(f.meta().max_pending_rows,514);
});

test("the 180000 work limit resets at UTC midnight, not the local midnight",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:180000,HISTORY_QUEUE_LIVE_RESERVED_UNITS:90000,
    HISTORY_QUEUE_FLUSH_QUERIES:1});
  await enqueueDurableHistory(f.env,ledger([event("utc-reset")]));
  seedHistoryBudget(f,180000);
  f.advance(10*3600_000-1);
  await assert.rejects(flushDurableHistory(f.env),/history_daily_write_budget/);
  f.advance(1);
  assert.equal(iso(Date.now()),"2026-10-04T22:00:00.000Z");
  assert.equal((await durableHistoryStatus(f.env)).history_write_units,180000);
  f.advance(2*3600_000-1);
  assert.equal((await durableHistoryStatus(f.env)).history_budget_exhausted,true);
  f.advance(1);
  const status=await durableHistoryStatus(f.env);
  assert.equal(status.write_budget_day,"turso:2026-10-05");
  assert.equal(status.history_write_units,0);assert.equal(status.history_budget_exhausted,false);
  assert.equal((await flushDurableHistory(f.env)).continued,1);
  assert.equal(f.meta().hist_day,"turso:2026-10-05");assert.equal(f.meta().hist_writes,8);
});

test("an in-flight history job stops on a UTC day change and resumes its saved cursor",async t=>{
  const f=fixture(t,{HISTORY_QUEUE_DAILY_HIST_WRITE_UNITS:180000,HISTORY_QUEUE_LIVE_RESERVED_UNITS:90000});
  await enqueueDurableHistory(f.env,ledger([event("utc-in-flight")]));
  f.advance(12*3600_000-1);
  let crossed=false;
  f.afterCommit=steps=>{
    if (!crossed && steps.some(step=>/INSERT INTO signal_episodes/.test(step.stmt.sql))) {crossed=true;f.advance(1);}
  };
  const result=await flushDurableHistory(f.env);
  assert.equal(result.failed,1);assert.equal(result.error,"history_write_day_changed");
  assert.equal(f.rows()[0].status,"pending");assert.ok(f.rows()[0].payload_json);
  assert.equal(JSON.parse(f.rows()[0].progress_json).phase,"event");
  assert.equal(f.meta().hist_day,"turso:2026-10-04");assert.equal(f.meta().hist_writes,8);
  assert.equal(f.sqlite.prepare("SELECT COUNT(*) n FROM signal_episode_events").get().n,0);
  f.afterCommit=null;f.advance(120001);
  assert.equal((await flushDurableHistory(f.env)).delivered,1);
  assert.equal(f.meta().hist_day,"turso:2026-10-05");assert.equal(f.meta().hist_writes,8);
  assert.equal(f.sql.filter(sql=>/INSERT INTO signal_episodes/.test(sql)).length,1);
});

test("SQL graph work resumes bounded daily-mode phases without replaying the frozen catch", async t => {
  const f = fixture(t,{HISTORY_QUEUE_FLUSH_QUERIES:24});
  const wallets = Array.from({length:40},(_,i) => ({wallet_address:`wallet-${i}`,common_funder:"private-funder",bought_tokens:100}));
  await enqueueDurableHistory(f.env,ledger([event("graph",{type:"signal",wallets})]));
  let complete = false;
  for (let i=0;i<100;i++) {
    const result = await flushDurableHistory(f.env);
    assert.ok(result.history_queries<=24);assert.ok(result.history_requests<=35);
    assert.ok(result.history_write_units<=HISTORY_QUEUE_LIMITS.flushHistoryWriteUnits);
    if (result.delivered) {complete=true;break;}
    f.advance(1001);
  }
  assert.equal(complete,true);
  assert.equal(f.sqlite.prepare("SELECT COUNT(*) n FROM signal_wallets").get().n,40);
  assert.equal(f.sqlite.prepare("SELECT COUNT(*) n FROM wallet_cluster_edges").get().n,780);
  assert.equal(f.sqlite.prepare("SELECT COUNT(*) n FROM wallet_clusters WHERE active=1").get().n,1);
  assert.equal(f.sql.filter(sql => /INSERT OR IGNORE INTO signal_episode_events/.test(sql)).length,1);
});

test("delayed archive batches reserve all SQL and R2 work within the 35-request cap", async t => {
  const r2 = bucket(), f = fixture(t,{HISTORY_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:r2});
  await enqueueDurableHistory(f.env,ledger(Array.from({length:5},(_,i) => event(`archive-${i}`))));
  assert.equal((await flushDurableHistory(f.env)).delivered,5);
  const calls = f.calls.length, archiveCalls = r2.calls.length;
  const result = await flushDurableHistoryArchives(f.env);
  assert.equal(result.archived,4);assert.ok(result.history_requests<=35);
  assert.equal(f.calls.length-calls,result.external_history_requests);
  assert.equal(r2.calls.length-archiveCalls,result.archive_requests);
  assert.equal(f.meta().archive_pending_rows,1);
});
