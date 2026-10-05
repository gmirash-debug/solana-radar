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
function event(id, {episode = id, at = NOW-3600_000, wallets = [], type = "snapshot"} = {}) {
  return {event_id:id,episode:{episode_id:episode,token_address:`token:${episode}`,caught_at:iso(NOW-4*3600_000),
    caught_mcap_usd:80000,caught_liquidity_usd:20000,token_age_days:40},
  event:{event_type:type,observed_at:iso(at)},wallets,evidence:{untouched:[1,{extra:"source"}]}};
}

// Execute the production Hrana transaction/conditional steps on real SQLite.
// No fake queue state or SQL matching supplies the query results.
function fixture(t, overrides = {}, {migrate = true} = {}) {
  const sqlite = new DatabaseSync(":memory:");
  t.after(() => sqlite.close());
  for (const name of ["0001_wallet_edge_history.sql","0002_cluster_edge_evidence.sql","0003_resumable_history.sql"]) {
    sqlite.exec(readFileSync(new URL(`../migrations-history/${name}`,import.meta.url),"utf8"));
  }
  sqlite.exec(readFileSync(new URL("../migrations-storage/0001_daily_learning.sql",import.meta.url),"utf8"));
  if (migrate) sqlite.exec(readFileSync(new URL("../migrations-storage/0003_sql_queue.sql",import.meta.url),"utf8"));
  if (migrate) sqlite.exec(readFileSync(new URL("../migrations-storage/0005_cutover_staging.sql",import.meta.url),"utf8"));
  const f = {sqlite,calls:[],sql:[],before:null,failSql:null,afterCommit:null};
  const encode = n => n === null ? {type:"null"} : typeof n === "string" ? {type:"text",value:n}
    : Number.isInteger(n) ? {type:"integer",value:String(n)} : {type:"float",value:n};
  const decode = n => n.type === "null" ? null : n.type === "integer" ? Number(n.value) : n.value;
  const condition = (cond, results) => !cond ? true : cond.type === "ok" ? results[cond.step]!==null
    : cond.type === "not" ? !condition(cond.cond,results)
    : cond.type === "and" ? cond.conds.every(child => condition(child,results)) : assert.fail("Hrana condition");
  const oldFetch = globalThis.fetch;
  let clock = NOW;
  t.mock.timers.enable({apis:["Date"],now:clock});
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
  f.advance = ms => {clock+=ms;t.mock.timers.tick(ms);};
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
