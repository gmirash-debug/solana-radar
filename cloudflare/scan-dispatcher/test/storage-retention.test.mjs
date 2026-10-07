import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {DatabaseSync} from "node:sqlite";
import {createHash} from "node:crypto";
import test from "node:test";
import {collectSqlRuntimeGarbage, previewSqlRuntimeGarbage, writeSqlRuntime,
  RUNTIME_RETENTION_LIMITS} from "../src/runtime-sql.js";
import {cleanupSqlHistoryRetention, runStorageRetention, previewStorageRetention,
  cleanupClosedSqlEpisodes, previewClosedSqlEpisodes, terminalClosureMarker,
  STORAGE_RETENTION_POLICY} from "../src/storage-retention.js";
import {createTursoDatabase} from "../src/storage-sql.js";

const NOW=Date.parse("2026-10-07T12:00:00Z"), HOUR=3600_000, DAY=24*HOUR;
const iso=time => new Date(time).toISOString();
const part=id => `dashboard:blob:${id.toString(16).padStart(64,"0")}`;

function fixture(t, {schema=true} = {}) {
  const sql=new DatabaseSync(":memory:"); t.after(() => sql.close());
  sql.exec("PRAGMA foreign_keys=ON");
  if (schema) for (const name of ["migrations/0001_radar_data.sql","migrations/0002_history_outbox.sql",
    "migrations-history/0001_wallet_edge_history.sql","migrations-history/0002_cluster_edge_evidence.sql",
    "migrations-history/0003_resumable_history.sql","migrations-storage/0002_runtime_sql.sql",
    "migrations-storage/0001_daily_learning.sql",
    "migrations-storage/0003_sql_queue.sql","migrations-storage/0004_history_outbox_cleanup_index.sql",
    "migrations-storage/0005_cutover_staging.sql","migrations-storage/0006_storage_retention.sql"]) {
    sql.exec(readFileSync(new URL(`../${name}`,import.meta.url),"utf8"));
  }
  const f={sql,calls:[],before:null,after:null,afterBatch:null};
  const execute=async (text,values,method) => {
    f.calls.push({text,values,method}); await f.before?.(text,values);
    const statement=sql.prepare(text);
    const args=/\?\d+/.test(text) ? [Object.fromEntries(values.map((value,i) => [i+1,value]))] : values;
    const rows=statement.all(...args);
    const response=method === "first" ? rows[0] ?? null : {success:true,results:rows,
      meta:{changes:Number(sql.prepare("SELECT changes() n").get().n)}};
    return await f.after?.(text,response) ?? response;
  };
  const db={prepare(text) {
    const wrap=values => ({bind:(...next) => wrap(next),first:() => execute(text,values,"first"),
      all:() => execute(text,values,"all"),run:() => execute(text,values,"run")});
    return wrap([]);
  },async batch(statements) {
    sql.exec("BEGIN IMMEDIATE");
    const results=[];
    try {for (const statement of statements) results.push(await statement.run());sql.exec("COMMIT");}
    catch (error) {sql.exec("ROLLBACK");throw error;}
    return await f.afterBatch?.(results) ?? results;
  }};
  const forbidden=new Proxy({}, {get() {assert.fail("Retention must not contact R2/RPC/DO");}});
  f.env={RADAR_DB:db,RADAR_HISTORY_DB:db,STORAGE_SQL_BACKEND:"turso",RUNTIME_STORAGE_BACKEND:"turso_sql",
    RADAR_ARCHIVE:forbidden,RUNTIME_SNAPSHOTS:forbidden,R2_BUDGET:forbidden};
  f.doc=(name,value={},touched=NOW-2*HOUR) => {
    const payload=typeof value === "string" ? value : JSON.stringify(value), blob=name.includes(":blob:");
    sql.prepare(`INSERT INTO runtime_sql_documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)`)
      .run(name,payload,createHash("sha256").update(payload).digest("hex"),iso(NOW),NOW,0,
        Buffer.byteLength(payload),blob ? name.slice(-64) : null,blob ? "json-ascii" : null,
        blob ? Buffer.byteLength(payload) : null,blob ? "token" : null,touched);
    return Buffer.byteLength(payload);
  };
  f.blobs=(count,bytes=32768) => {
    for (let index=0;index<count;index++) f.doc(part(index),"x".repeat(bytes));
  };
  f.episode=(id,run=null) => sql.prepare(`INSERT INTO signal_episodes
    (episode_id,token_address,lane,signal_family,caught_at,last_signal_at,mcap_band,liquidity_band,
      age_band,source_run_key,created_at,updated_at) VALUES(?,?,'reactivation','signal',?,?,'m','l','a',?,?,?)`)
    .run(id,`token:${id}`,iso(NOW-100*DAY),iso(NOW-100*DAY),run,iso(NOW-100*DAY),iso(NOW));
  f.event=(id,payload,episode=id) => {
    if (!sql.prepare("SELECT 1 FROM signal_episodes WHERE episode_id=?").get(episode)) f.episode(episode);
    sql.prepare(`INSERT INTO signal_episode_events(event_id,episode_id,observed_at,event_type,payload_json,created_at)
      VALUES(?,?,'2026-09-01T00:00:00Z','snapshot',?,'2026-09-01T00:00:00Z')`).run(id,episode,payload);
  };
  f.outbox=(id,payload="original",{status="delivered",delivered=NOW-8*DAY}={}) => {
    sql.prepare(`INSERT INTO history_outbox(event_id,event_type,payload_json,status,next_attempt_at,
      delivered_at,created_at,updated_at) VALUES(?,'snapshot',?,?,?, ?,?,?)`)
      .run(id,payload,status,iso(NOW),delivered===null ? null : iso(delivered),iso(NOW-8*DAY),iso(NOW-8*DAY));
  };
  f.queue=(id,{status="pending",archivePending=0}={}) => sql.prepare(`INSERT INTO history_sql_queue_events
    (event_id,episode_id,source_at,payload_json,payload_bytes,status,next_attempt_at,archive_pending)
    VALUES(?,?,?,'original',8,?,?,?)`).run(id,id,NOW,status,NOW,archivePending);
  f.run=(id,generated=NOW-31*DAY) => sql.prepare(`INSERT INTO scan_runs
    (run_key,generated_at,lanes_scanned_json,stats_json,lane_stats_json,created_at) VALUES(?,?,'[]','{}','{}',?)`)
    .run(id,iso(generated),iso(generated));
  f.closed=(id,closed=NOW-91*DAY) => {
    f.env.HISTORY_EPISODE_CLOSURE_CONTRACT="terminal-closed-v1";
    f.episode(id);
    sql.prepare("UPDATE signal_episodes SET closed_at=?,data_quality_status='complete' WHERE episode_id=?").run(iso(closed),id);
    sql.prepare(`INSERT INTO signal_episode_events(event_id,episode_id,observed_at,event_type,
      thesis_status,payload_json,raw_object_key,created_at,cohort_retained_pct,retained_supply_pct,data_quality_status)
      VALUES(?,?,?,'retention_check','closed','original','cold',?,0,0,'complete')`)
      .run(`${id}:closure`,id,iso(closed),iso(closed));
    for (const minutes of [60,360,1440,4320,10080]) {
      const due=iso(NOW-100*DAY+minutes*60_000);
      sql.prepare(`INSERT INTO signal_outcomes(episode_id,horizon_minutes,due_at,evaluated_at,status,updated_at)
        VALUES(?,?,?,?,'complete',?)`).run(id,minutes,due,due,due);
    }
    sql.prepare("INSERT INTO wallet_observation_bundles(event_id,episode_id,observed_at,observations_json) VALUES(?,?,?,'[]')")
      .run(`${id}:closure`,id,iso(closed));
  };
  f.names=table => sql.prepare(`SELECT * FROM ${table}`).all();
  return f;
}

test("runtime preview is metadata-only, read-only and distinguishes refs, grace and eligible blobs",async t => {
  const f=fixture(t); f.doc("dashboard");
  f.doc(part(1),"current"); f.doc(part(2),"obsolete");
  f.doc(part(3),"recent",NOW-HOUR+1); f.doc(part(4),"boundary",NOW-HOUR);
  f.doc("outbox:"+"a".repeat(64),"cold metadata"); f.doc("unknown:blob:"+"b".repeat(64),"foreign");
  f.sql.prepare("INSERT INTO runtime_sql_references VALUES(?,?)").run("dashboard",part(1));
  const changes=f.sql.prepare("SELECT total_changes() n").get().n;
  const preview=await previewSqlRuntimeGarbage(f.env,{now:NOW});
  assert.equal(preview.ok,true); assert.equal(preview.preview,true); assert.equal(preview.grace_minutes,60);
  assert.equal(preview.backlog_rows,1); assert.equal(preview.backlog_bytes,8);
  assert.equal(preview.referenced_rows,1); assert.equal(preview.grace_rows,2); assert.equal(preview.deleted_rows,0);
  assert.equal(f.sql.prepare("SELECT total_changes() n").get().n,changes);
  assert.ok(f.calls.every(call => !/payload_json|\bDELETE\b|\bUPDATE\b|\bINSERT\b/.test(call.text)));
});

test("runtime GC expires at strictly over sixty minutes, preserving roots and non-runtime blobs",async t => {
  const f=fixture(t); f.doc("dashboard"); f.doc("outbox:"+"a".repeat(64));
  f.doc(part(1),"expired",NOW-HOUR-1); f.doc(part(2),"boundary",NOW-HOUR);
  f.doc(part(3),"restaged",NOW); f.doc("unknown:blob:"+"b".repeat(64));
  const result=await collectSqlRuntimeGarbage(f.env,NOW);
  assert.equal(result.ok,true); assert.equal(result.deleted_rows,1); assert.equal(result.deleted_bytes,7);
  assert.equal(result.meta.changes,1); assert.equal(result.backlog_rows,0);
  assert.equal(f.names("runtime_sql_documents").length,5);
});

test("real FK RESTRICT and deletion-time ref/restaging guards protect concurrent publication",async t => {
  const f=fixture(t); f.doc("dashboard"); f.blobs(3,10);
  f.sql.prepare("INSERT INTO runtime_sql_references VALUES(?,?)").run("dashboard",part(2));
  assert.throws(() => f.sql.prepare("DELETE FROM runtime_sql_documents WHERE name=?").run(part(2)),/FOREIGN KEY/);
  let raced=false;
  f.before=text => {
    if (!raced && /^DELETE FROM runtime_sql_documents/.test(text)) {
      raced=true; f.sql.prepare("UPDATE runtime_sql_documents SET touched_at=? WHERE name=?").run(NOW,part(0));
      f.sql.prepare("INSERT INTO runtime_sql_references VALUES(?,?)").run("dashboard",part(1));
    }
  };
  const result=await collectSqlRuntimeGarbage(f.env,NOW);
  assert.equal(result.ok,true); assert.equal(result.deleted_rows,0); assert.equal(result.backlog_rows,0);
  assert.equal(f.names("runtime_sql_documents").length,4);
  assert.equal(f.sql.prepare("PRAGMA foreign_keys").get().foreign_keys,1);
});

test("supersession starts a fresh reader grace, even for a previously old referenced part",async t => {
  t.mock.timers.enable({apis:["Date"],now:NOW});
  const f=fixture(t), make=data => {const sha256=createHash("sha256").update(data).digest("hex");
    return {encoding:"gzip+base64-part",data,sha256,encoded_bytes:data.length};};
  const a=make("AAAA"),b=make("BBBB");
  const manifest=p => ({schema_version:2,encoding:"gzip+base64+parts",sha256:"f".repeat(64),decoded_bytes:4,
    encoded_bytes:4,parts:[{id:p.sha256,bytes:4}]});
  for (const p of [a,b]) await writeSqlRuntime(f.env,`checkpoint:deep:blob:${p.sha256}`,p,iso(NOW));
  await writeSqlRuntime(f.env,"checkpoint:deep",manifest(a),iso(NOW));
  f.sql.prepare("UPDATE runtime_sql_documents SET touched_at=? WHERE content_id IS NOT NULL").run(NOW-2*HOUR);
  await writeSqlRuntime(f.env,"checkpoint:deep",manifest(b),iso(NOW),1);
  assert.equal((await collectSqlRuntimeGarbage(f.env,NOW+HOUR)).deleted_rows,0);
  assert.equal((await collectSqlRuntimeGarbage(f.env,NOW+HOUR+1)).deleted_rows,1);
  assert.ok(f.sql.prepare("SELECT 1 FROM runtime_sql_documents WHERE content_id=?").get(b.sha256));
});

test("adaptive catch-up is capped at four 256-row batches and reports exact stored bytes/backlog",async t => {
  const f=fixture(t); f.blobs(1050,16384);
  const result=await collectSqlRuntimeGarbage(f.env,{now:NOW});
  assert.equal(result.ok,true); assert.equal(result.batches,4); assert.equal(result.deleted_rows,1024);
  assert.equal(result.deleted_bytes,1024*16384); assert.equal(result.backlog_rows,26);
  assert.equal(result.backlog_bytes,26*16384); assert.equal(result.queries,f.calls.length);
  assert.ok(result.queries<=32); assert.equal(result.deferred_reason,"runtime_gc_batch_budget");
});

test("maintenance uses the 8 MiB threshold and 256 MiB target without overriding protection",async t => {
  const f=fixture(t); f.doc(part(0),"small");
  let result=await runStorageRetention(f.env,{now:NOW,history:false});
  assert.equal(result.deleted_rows,0); assert.equal(result.runtime.deferred_reason,"runtime_gc_below_threshold");
  result=await runStorageRetention(f.env,{now:NOW,history:false,force:true});
  assert.equal(result.deleted_rows,1);
  f.doc("dashboard"); f.doc(part(1),"protected");
  f.sql.prepare("INSERT INTO runtime_sql_references VALUES(?,?)").run("dashboard",part(1));
  f.doc(part(2),"obsolete");
  result=await runStorageRetention(f.env,{now:NOW,history:false,targetBytes:1});
  assert.equal(result.deleted_rows,1); assert.equal(result.runtime.target_met,false);
  assert.equal(result.runtime.protected_over_target,true);
  assert.equal(RUNTIME_RETENTION_LIMITS.targetBytes,256*1024*1024);
});

test("query budget reserves a final fresh backlog and never starts a too-large deletion unit",async t => {
  const f=fixture(t); f.blobs(300,32768);
  const result=await collectSqlRuntimeGarbage(f.env,{now:NOW,maxQueries:4});
  assert.equal(result.ok,true); assert.equal(result.deleted_rows,256); assert.equal(result.backlog_rows,44);
  assert.equal(result.queries,4); assert.equal(result.deferred_reason,"runtime_gc_query_budget");
  f.calls.length=0;
  const deferred=await collectSqlRuntimeGarbage(f.env,{now:NOW,maxQueries:3});
  assert.equal(deferred.deleted_rows,0); assert.equal(deferred.backlog_rows,44);
  assert.ok(f.calls.length<=3);
});

test("BLOCKED is a failure with no invented deletion/backlog and no automatic retry",async t => {
  const f=fixture(t); f.doc(part(0),"original");
  f.before=text => {if (/^DELETE/.test(text)) throw Object.assign(new Error("storage_sql_statement_failed:BLOCKED"),{outcomeUnknown:false});};
  const result=await collectSqlRuntimeGarbage(f.env,NOW);
  assert.equal(result.ok,false); assert.equal(result.error,"storage_sql_statement_failed:BLOCKED");
  assert.equal(result.deleted_rows,0); assert.equal(result.deleted_bytes,0); assert.equal(result.backlog_rows,null);
  assert.equal(result.outcome_unknown,false); assert.equal(f.calls.filter(call => /^DELETE/.test(call.text)).length,1);
  assert.equal(f.names("runtime_sql_documents").length,1);
});

test("a lost response after commit marks uncertain outcome instead of claiming zero actual deletions",async t => {
  const f=fixture(t); f.doc(part(0));
  f.after=text => {if (/^DELETE/.test(text)) throw Object.assign(new Error("storage_sql_network_error"),{outcomeUnknown:true});};
  const result=await collectSqlRuntimeGarbage(f.env,NOW);
  assert.equal(result.ok,false); assert.equal(result.outcome_unknown,true);
  assert.equal(result.deleted_rows,0); assert.equal(result.backlog_rows,null);
  assert.equal(f.names("runtime_sql_documents").length,0);
});

test("malformed returned bytes after a committed deletion are marked uncertain",async t => {
  const f=fixture(t); f.doc(part(0));
  f.after=(text,response) => /^DELETE/.test(text) ? {...response,results:[{name:part(0),bytes:-1}]} : undefined;
  const result=await collectSqlRuntimeGarbage(f.env,NOW);
  assert.equal(result.ok,false); assert.equal(result.outcome_unknown,true);
  assert.equal(result.deleted_rows,0); assert.equal(result.backlog_rows,null);
});

test("a failed second batch rolls back that statement and preserves confirmed first-batch accounting",async t => {
  const f=fixture(t); f.blobs(520,32768);
  f.sql.exec(`CREATE TRIGGER fail_gc BEFORE DELETE ON runtime_sql_documents WHEN OLD.name='${part(300)}'
    BEGIN SELECT RAISE(ABORT,'injected'); END`);
  const result=await collectSqlRuntimeGarbage(f.env,NOW);
  assert.equal(result.ok,false); assert.equal(result.deleted_rows,256); assert.equal(result.deleted_bytes,256*32768);
  assert.equal(result.backlog_rows,null); assert.equal(f.names("runtime_sql_documents").length,264);
});

test("runtime limits and grace reject unsafe overrides before any SQL",async t => {
  const f=fixture(t);
  for (const options of [{maxBatches:5},{batchRows:257},{maxQueries:33},{graceMinutes:59},{now:NaN},
    {preview:"yes"},{maxBatches:false},{maxBatches:null},{maxBatches:""},{now:null}]) {
    assert.equal((await collectSqlRuntimeGarbage(f.env,options)).ok,false);
    assert.equal((await runStorageRetention(f.env,options)).ok,false);
  }
  assert.equal(f.calls.length,0);
});

test("retention timestamps with offsets or invalid dates cannot expire before their UTC cutoff",async t => {
  const f=fixture(t); f.outbox("offset"); f.event("offset","original"); f.run("offset");
  f.outbox("invalid"); f.event("invalid","original"); f.run("invalid");
  f.sql.prepare("UPDATE history_outbox SET delivered_at=? WHERE event_id='offset'")
    .run("2026-09-30T06:00:00-12:00");
  f.sql.prepare("UPDATE scan_runs SET generated_at=? WHERE run_key='offset'")
    .run("2026-09-07T06:00:00-12:00");
  f.sql.prepare("UPDATE history_outbox SET delivered_at='2000-invalid' WHERE event_id='invalid'").run();
  f.sql.prepare("UPDATE scan_runs SET generated_at='2000-invalid' WHERE run_key='invalid'").run();
  const result=await cleanupSqlHistoryRetention(f.env,{now:NOW});
  assert.equal(result.ok,true); assert.equal(result.deleted_rows,0);
  assert.equal(f.names("history_outbox").length,2); assert.equal(f.names("scan_runs").length,2);
});

test("runtime, history and atomic episode retirement work through the actual Turso adapter without network",async t => {
  const f=fixture(t); f.blobs(300,32768); f.run("old"); f.closed("closed");
  let requests=0, foreignKeys=0;
  const db=createTursoDatabase({TURSO_DATABASE_URL:"https://retention.invalid",TURSO_AUTH_TOKEN:"local-test-only"},
    {fetch:async (_,options) => {
      requests++;
      const steps=JSON.parse(options.body).requests[0].batch.steps;
      const results=[], errors=[];
      const accepted=condition => !condition ? true : condition.type==="ok" ? results[condition.step]!==null
        : condition.type==="not" ? !accepted(condition.cond)
        : condition.type==="and" ? condition.conds.every(accepted) : assert.fail("Unexpected Hrana condition");
      for (const {stmt,condition} of steps) {
        if (!accepted(condition)) {results.push(null); errors.push(null); continue;}
        if (stmt.sql==="PRAGMA foreign_keys = ON") foreignKeys++;
        const statement=f.sql.prepare(stmt.sql); statement.setReadBigInts(true);
        const args=stmt.args.map(arg => arg.type==="integer" ? BigInt(arg.value) : arg.value);
        const binding=/\?\d+/.test(stmt.sql) ? [Object.fromEntries(args.map((arg,i) => [i+1,arg]))] : args;
        const columns=statement.columns(), rows=statement.all(...binding);
        const encode=value => value===null ? {type:"null"} : typeof value==="bigint"
          ? {type:"integer",value:String(value)} : {type:"text",value};
        results.push({cols:columns.map(col => ({name:col.name,decltype:col.type})),
          rows:stmt.want_rows ? rows.map(row => columns.map(col => encode(row[col.name]))) : [],
          affected_row_count:/^DELETE/.test(stmt.sql) ? Number(f.sql.prepare("SELECT changes() n").get().n) : 0,
          last_insert_rowid:null});
        errors.push(null);
      }
      return Response.json({baton:null,results:[{type:"ok",response:{type:"batch",result:{
        step_results:results,step_errors:errors}}},{type:"ok",response:{type:"close"}}]});
    }});
  const env={...f.env,RADAR_DB:db,RADAR_HISTORY_DB:db};
  const result=await runStorageRetention(env,{now:NOW});
  assert.equal(result.ok,true); assert.equal(result.runtime.deleted_rows,300);
  assert.equal(result.history.tables.scan_runs.deleted_rows,1); assert.equal(result.history.episodes.deleted_episodes,1);
  assert.equal(result.deleted_bytes,300*32768+6+10); assert.equal(result.batches,4);
  assert.equal(result.queries,requests+4); assert.equal(foreignKeys,requests); assert.ok(result.queries<=32);
  assert.equal(f.sql.prepare("PRAGMA foreign_keys").get().foreign_keys,1);
});

test("protocol preview uses only SELECT metadata and never loads payload_json",async t => {
  const f=fixture(t); f.doc(part(0)); f.outbox("outbox"); f.run("run");
  const changes=f.sql.prepare("SELECT total_changes() n").get().n;
  const result=await previewStorageRetention(f.env,{now:NOW});
  assert.equal(result.ok,true); assert.equal(result.deleted_rows,0); assert.equal(result.history.candidates.history_outbox,1);
  assert.equal(result.history.candidates.scan_runs,1); assert.equal(result.runtime.preview,true);
  assert.equal(result.queries,f.calls.length); assert.ok(result.queries<=32);
  assert.equal(f.sql.prepare("SELECT total_changes() n").get().n,changes);
  assert.ok(f.calls.every(call => /^SELECT/.test(call.text) && !call.text.includes("payload_json")));
});

test("outbox cleanup needs an identical SQL copy, seven-day age and no pending dependency",async t => {
  const f=fixture(t);
  for (const id of ["safe","changed","compact","pending","young","boundary","queue","archive","cutover"]) {
    f.outbox(id,"original",{status:id==="pending" ? "pending" : "delivered",
      delivered:id==="young" ? NOW-7*DAY+1 : id==="boundary" ? NOW-7*DAY : NOW-8*DAY});
    f.event(id,id==="changed" ? "different" : id==="compact" ? '{"archive_ref":{"key":"cold"}}' : "original");
  }
  f.queue("queue"); f.queue("archive",{status:"delivered",archivePending:1});
  f.sql.prepare("INSERT INTO history_sql_cutover_events VALUES(?,?,?,'original',8)").run("cutover","cutover",NOW);
  const sources=f.names("signal_episode_events"), queue=f.names("history_sql_queue_events");
  const result=await cleanupSqlHistoryRetention(f.env,{now:NOW});
  assert.equal(result.ok,true); assert.equal(result.deleted_rows,1); assert.equal(result.deleted_bytes,8);
  assert.equal(result.tables.history_outbox.deleted_rows,1); assert.equal(result.candidates.history_outbox,5);
  assert.deepEqual(f.names("history_outbox").map(row => row.event_id).sort(),
    ["archive","boundary","changed","compact","cutover","pending","queue","young"].sort());
  assert.deepEqual(f.names("signal_episode_events"),sources); assert.deepEqual(f.names("history_sql_queue_events"),queue);
});

test("old scan runs preserve source_run_key dependencies and TTL never cascades into positions or proof",async t => {
  const f=fixture(t); f.run("safe"); f.run("referenced"); f.run("boundary",NOW-30*DAY); f.run("young",NOW-30*DAY+1);
  f.episode("active-position","referenced"); f.event("evidence","source","active-position");
  f.sql.prepare(`INSERT INTO signal_outcomes(episode_id,horizon_minutes,due_at,status,updated_at)
    VALUES('active-position',10080,?,'pending',?)`).run(iso(NOW+DAY),iso(NOW));
  f.sql.prepare(`INSERT INTO wallet_observations(episode_id,wallet_address,observed_at)
    VALUES('active-position','wallet',?)`).run(iso(NOW-90*DAY));
  const before=["signal_episodes","signal_episode_events","signal_outcomes","wallet_observations"].map(table => f.names(table));
  const result=await cleanupSqlHistoryRetention(f.env,{now:NOW});
  assert.equal(result.ok,true); assert.equal(result.tables.scan_runs.deleted_rows,1); assert.equal(result.deleted_bytes,6);
  assert.deepEqual(f.names("scan_runs").map(row => row.run_key).sort(),["boundary","referenced","young"]);
  assert.deepEqual(["signal_episodes","signal_episode_events","signal_outcomes","wallet_observations"].map(table => f.names(table)),before);
  assert.equal(STORAGE_RETENTION_POLICY.activePositionsExpire,false);
  assert.equal(STORAGE_RETENTION_POLICY.episodeCleanupEnabled,true);
  assert.equal(STORAGE_RETENTION_POLICY.episodeClosureContract,"terminal-closed-v1");
  assert.equal(STORAGE_RETENTION_POLICY.observationCleanupEnabled,false);
});

test("dependency and duplicate proof are rechecked when they change after selection",async t => {
  const f=fixture(t); f.outbox("race"); f.event("race","original"); f.run("run-race");
  f.before=text => {
    if (/^DELETE FROM history_outbox/.test(text)) f.sql.prepare("UPDATE signal_episode_events SET payload_json='different' WHERE event_id='race'").run();
    if (/^DELETE FROM scan_runs/.test(text)) f.episode("new-ref","run-race");
  };
  const result=await cleanupSqlHistoryRetention(f.env,{now:NOW});
  assert.equal(result.ok,true); assert.equal(result.deleted_rows,0);
  assert.equal(f.names("history_outbox").length,1); assert.equal(f.names("scan_runs").length,1);
});

test("history catch-up is bounded and can continue without reinitializing schema",async t => {
  const f=fixture(t);
  for (let index=0;index<1030;index++) f.run(`run-${String(index).padStart(4,"0")}`);
  const first=await cleanupSqlHistoryRetention(f.env,{now:NOW});
  assert.equal(first.ok,true); assert.equal(first.batches,4); assert.equal(first.deleted_rows,1024);
  assert.equal(first.deleted_bytes,1024*6); assert.equal(first.candidates.scan_runs,6); assert.ok(first.queries<=32);
  const second=await cleanupSqlHistoryRetention(f.env,{now:NOW});
  assert.equal(second.deleted_rows,6); assert.equal(second.candidates.scan_runs,0);
  assert.ok(f.calls.every(call => !/^CREATE|^ALTER/.test(call.text)));
});

test("combined protocol shares its four-batch/32-query budget across runtime and history",async t => {
  const f=fixture(t); f.blobs(1030,16384); f.run("old");
  const result=await runStorageRetention(f.env,{now:NOW});
  assert.equal(result.ok,true); assert.equal(result.batches,4); assert.equal(result.runtime.deleted_rows,1024);
  assert.equal(result.history.deleted_rows,0); assert.equal(result.history.candidates.scan_runs,1);
  assert.equal(result.queries,f.calls.length); assert.ok(result.queries<=32);
});

test("missing tables and separate database bindings never allow speculative history deletion",async t => {
  const f=fixture(t,{schema:false});
  const empty=await cleanupSqlHistoryRetention(f.env,{now:NOW});
  assert.equal(empty.ok,true); assert.deepEqual(empty.unavailable_tables,["history_outbox","scan_runs"]);
  f.calls.length=0;
  const separate=await cleanupSqlHistoryRetention({...f.env,RADAR_HISTORY_DB:{}},{now:NOW});
  assert.equal(separate.ok,false); assert.equal(separate.error,"storage_retention_canonical_db_required");
  assert.equal(f.calls.length,0);
});

test("missing retention indexes fail closed without migrations or payload writes",async t => {
  const f=fixture(t); f.outbox("old"); f.event("old","original");
  f.sql.exec("DROP INDEX idx_history_outbox_delivered_retention");
  const result=await cleanupSqlHistoryRetention(f.env,{now:NOW});
  assert.equal(result.ok,false); assert.equal(result.error,"storage_retention_index_migration_required");
  assert.equal(f.names("history_outbox").length,1); assert.ok(f.calls.every(call => /^SELECT/.test(call.text)));
});

test("history BLOCKED stops the protocol and leaves candidate counts unknown",async t => {
  const f=fixture(t); f.run("old");
  f.before=text => {if (/^DELETE FROM scan_runs/.test(text)) throw new Error("storage_sql_statement_failed:BLOCKED");};
  const result=await runStorageRetention(f.env,{now:NOW,runtime:false});
  assert.equal(result.ok,false); assert.equal(result.error,"storage_sql_statement_failed:BLOCKED");
  assert.equal(result.deleted_rows,0); assert.equal(result.history.candidates,null);
  assert.equal(f.names("scan_runs").length,1); assert.equal(f.calls.filter(call => /^DELETE/.test(call.text)).length,1);
});

test("history lost commit response retains confirmed counters and exposes uncertain outcome",async t => {
  const f=fixture(t); f.run("old");
  f.after=text => {if (/^DELETE FROM scan_runs/.test(text)) throw Object.assign(new Error("storage_sql_timeout"),{outcomeUnknown:true});};
  const result=await cleanupSqlHistoryRetention(f.env,{now:NOW});
  assert.equal(result.ok,false); assert.equal(result.outcome_unknown,true); assert.equal(result.candidates,null);
  assert.equal(result.deleted_rows,0); assert.equal(f.names("scan_runs").length,0);
});

test("retention migration indexes both the age page and referenced-source guard",t => {
  const f=fixture(t);
  const outbox=f.sql.prepare(`EXPLAIN QUERY PLAN SELECT event_id FROM history_outbox INDEXED BY idx_history_outbox_delivered_retention
    WHERE status='delivered' AND delivered_at<? ORDER BY delivered_at,event_id LIMIT 256`).all(iso(NOW));
  const runs=f.sql.prepare(`EXPLAIN QUERY PLAN SELECT d.run_key FROM scan_runs d WHERE d.generated_at<?
    AND NOT EXISTS(SELECT 1 FROM signal_episodes e WHERE e.source_run_key=d.run_key)
    ORDER BY d.generated_at,d.run_key LIMIT 256`).all(iso(NOW));
  assert.ok(outbox.some(row => row.detail.includes("idx_history_outbox_delivered_retention")));
  assert.ok(runs.some(row => row.detail.includes("idx_signal_episodes_source_run_key")));
});

test("episode retirement cannot infer terminal CLOSED from reversible invalidated_at or force",async t => {
  const f=fixture(t); f.closed("legacy"); delete f.env.HISTORY_EPISODE_CLOSURE_CONTRACT;
  const blocked=await cleanupClosedSqlEpisodes(f.env,{now:NOW,force:true});
  assert.equal(blocked.ok,true); assert.equal(blocked.enabled,false); assert.equal(blocked.deleted_rows,0);
  assert.equal(blocked.deferred_reason,"episode_retention_terminal_closure_contract_required");
  assert.equal(f.calls.length,0);
  f.env.HISTORY_EPISODE_CLOSURE_CONTRACT="terminal-closed-v1";
  f.sql.prepare("UPDATE signal_episode_events SET thesis_status='invalidated'").run();
  const legacy=await cleanupClosedSqlEpisodes(f.env,{now:NOW,force:true});
  assert.equal(legacy.candidate_rows,1); assert.equal(legacy.proven_rows,0); assert.equal(legacy.deleted_rows,0);
});

test("episode preview is metadata-only, makes no marker and applies the strict ninety-day boundary",async t => {
  const f=fixture(t); f.closed("old"); f.closed("boundary",NOW-90*DAY); f.closed("young",NOW-90*DAY+1);
  const changes=f.sql.prepare("SELECT total_changes() n").get().n;
  const result=await previewClosedSqlEpisodes(f.env,{now:NOW});
  assert.equal(result.ok,true); assert.equal(result.candidate_rows,1); assert.equal(result.proven_rows,1);
  assert.equal(result.deleted_rows,0); assert.equal(f.sql.prepare("SELECT total_changes() n").get().n,changes);
  assert.ok(f.calls.every(call => /^SELECT/.test(call.text) && !/payload_json|observations_json/.test(call.text)));
  assert.equal(f.names("history_episode_retirement_work").length,0);
});

test("closed episode and non-cascading bundles retire atomically with exact source accounting",async t => {
  const f=fixture(t); f.closed("old"); f.episode("active");
  assert.throws(() => f.sql.prepare("DELETE FROM signal_episodes WHERE episode_id='old'").run(),/FOREIGN KEY/);
  const result=await cleanupClosedSqlEpisodes(f.env,{now:NOW});
  assert.equal(result.ok,true); assert.equal(result.deleted_episodes,1); assert.equal(result.deleted_rows,8);
  assert.equal(result.deleted_bytes,10); assert.equal(result.proven_rows,0); assert.ok(result.queries<=16);
  assert.deepEqual(f.names("signal_episodes").map(row => row.episode_id),["active"]);
  for (const table of ["signal_episode_events","signal_outcomes","wallet_observation_bundles","history_episode_retirement_work"]) {
    assert.equal(f.names(table).length,0);
  }
  assert.ok(!f.calls.some(call => /^SELECT \*/.test(call.text)));
});

test("all real pipeline horizons must be complete with no error; partial is not terminal success",async t => {
  for (const state of ["pending","partial","error","failed","unknown","complete-error","missing","extra-pending"]) {
    const f=fixture(t); f.closed(state);
    if (state==="missing") f.sql.prepare("DELETE FROM signal_outcomes WHERE horizon_minutes=10080").run();
    else if (state==="extra-pending") f.sql.prepare(`INSERT INTO signal_outcomes
      (episode_id,horizon_minutes,due_at,status,updated_at) VALUES(?,20000,?,'pending',?)`).run(state,iso(NOW),iso(NOW));
    else f.sql.prepare("UPDATE signal_outcomes SET status=?,error=? WHERE horizon_minutes=10080")
      .run(state==="complete-error" ? "complete" : state,state==="complete-error" ? "error" : null);
    const result=await cleanupClosedSqlEpisodes(f.env,{now:NOW});
    assert.equal(result.ok,true,state); assert.equal(result.proven_rows,0,state);
    assert.equal(f.names("signal_episodes").length,1,state); assert.equal(f.names("wallet_observation_bundles").length,1,state);
  }
});

test("pending, archive, cutover, learning, reopened and unarchived dependencies protect CLOSED episodes",async t => {
  for (const dependency of ["outbox","queue","archive","lease","cutover","dirty","daily-job","cluster-lock",
    "cluster-work","reopened","unarchived","legacy-observation","holding","coverage","edge"]) {
    const f=fixture(t); f.closed(dependency);
    if (dependency==="outbox") f.outbox("not-indexed-by-episode","large raw",{status:"pending"});
    if (["queue","archive","lease"].includes(dependency)) {
      f.queue(dependency,{status:dependency==="queue" ? "pending" : "delivered",archivePending:dependency==="archive" ? 1 : 0});
      if (dependency==="lease") f.sql.prepare("UPDATE history_sql_queue_events SET archive_lease_token='inflight'").run();
    }
    if (dependency==="cutover") f.sql.prepare("INSERT INTO history_sql_cutover_events VALUES(?,?,?,'original',8)")
      .run(dependency,dependency,NOW);
    if (dependency==="dirty") f.sql.prepare("INSERT INTO history_maintenance_dirty VALUES(?,?,?,?,?)")
      .run(dependency,dependency,iso(NOW),iso(NOW),iso(NOW));
    if (dependency==="daily-job") f.sql.prepare(`INSERT INTO history_maintenance_jobs
      (job_day,cutoff_at,created_at,updated_at) VALUES('day',?,?,?)`).run(iso(NOW),iso(NOW),iso(NOW));
    if (dependency==="cluster-lock") f.sql.prepare("UPDATE history_cluster_lock SET job_id='running'").run();
    if (dependency==="cluster-work") f.sql.prepare("INSERT INTO history_cluster_work(job_id,wallet_address) VALUES('job','wallet')").run();
    if (dependency==="reopened") {
      f.event("reopened-event","original",dependency);
      f.sql.prepare("UPDATE signal_episode_events SET thesis_status='weakening',raw_object_key='cold' WHERE event_id='reopened-event'").run();
    }
    if (dependency==="unarchived") f.sql.prepare("UPDATE signal_episode_events SET raw_object_key=NULL").run();
    if (dependency==="legacy-observation") f.sql.prepare(`INSERT INTO wallet_observations
      (episode_id,wallet_address,observed_at) VALUES(?,'wallet',?)`).run(dependency,iso(NOW-92*DAY));
    if (dependency==="holding") f.sql.prepare(`INSERT INTO wallet_observations
      (episode_id,wallet_address,observed_at,current_token_balance,raw_object_key) VALUES(?,'wallet',?,1,'cold')`)
        .run(dependency,iso(NOW-91*DAY));
    if (dependency==="coverage") f.sql.prepare("UPDATE signal_episode_events SET data_quality_status='partial'").run();
    if (dependency==="edge") {
      f.sql.prepare(`INSERT INTO wallet_cluster_edges(edge_id,wallet_a,wallet_b,relation_type,
        first_seen_at,last_seen_at,created_at,updated_at,is_infrastructure) VALUES('edge','a','b','funder',?,?,?,?,1)`)
        .run(iso(NOW),iso(NOW),iso(NOW),iso(NOW));
      f.sql.prepare("INSERT INTO wallet_cluster_edge_evidence VALUES('edge',?,?,'{}',?)").run(dependency,iso(NOW),iso(NOW));
    }
    const result=await cleanupClosedSqlEpisodes(f.env,{now:NOW,force:true});
    assert.equal(result.ok,true,dependency); assert.equal(result.proven_rows,0,dependency);
    assert.equal(f.names("signal_episodes").length,1,dependency); assert.equal(f.names("wallet_observation_bundles").length,1,dependency);
  }
});

test("episode retirement counts cascades in the row cap and reserves five SQL statements per atomic batch",async t => {
  const f=fixture(t); for (const id of ["a","b","c","oversized"]) f.closed(id);
  for (let index=0;index<256;index++) f.sql.prepare(`INSERT INTO wallet_observations
    (episode_id,wallet_address,observed_at,raw_object_key) VALUES('oversized',?,?,'cold')`).run(`wallet-${index}`,iso(NOW-92*DAY));
  const result=await cleanupClosedSqlEpisodes(f.env,{now:NOW});
  assert.equal(result.ok,true); assert.equal(result.deleted_episodes,2); assert.equal(result.batches,2);
  assert.equal(result.deleted_rows,16); assert.equal(result.deleted_bytes,20); assert.ok(result.queries<=16);
  const next=await cleanupClosedSqlEpisodes(f.env,{now:NOW});
  assert.equal(next.deleted_episodes,1); assert.equal(next.proven_rows,1);
  assert.equal(next.deferred_reason,"episode_retention_dependency_row_budget");
  assert.equal(f.names("history_episode_retirement_work").length,0);
});

test("dependency arriving before the atomic retirement is rechecked before bundles are removed",async t => {
  const f=fixture(t); f.closed("race"); let raced=false;
  f.before=text => {if (!raced && /^INSERT INTO history_episode_retirement_work/.test(text)) {raced=true;f.queue("race");}};
  const result=await cleanupClosedSqlEpisodes(f.env,{now:NOW});
  assert.equal(result.ok,true); assert.equal(result.deleted_rows,0); assert.equal(result.proven_rows,0);
  assert.equal(f.names("signal_episodes").length,1); assert.equal(f.names("wallet_observation_bundles").length,1);
});

test("parent failure rolls bundles back; a parent that becomes protected inside the transaction also rolls back",async t => {
  for (const failure of ["abort","new-pending"]) {
    const f=fixture(t); f.closed(failure);
    if (failure==="abort") f.sql.exec(`CREATE TRIGGER fail_retire BEFORE DELETE ON signal_episodes
      BEGIN SELECT RAISE(ABORT,'injected'); END`);
    else f.sql.exec(`CREATE TRIGGER protect_parent AFTER DELETE ON wallet_observation_bundles
      BEGIN UPDATE signal_outcomes SET status='pending' WHERE episode_id=OLD.episode_id; END`);
    const result=await cleanupClosedSqlEpisodes(f.env,{now:NOW});
    assert.equal(result.ok,false,failure); assert.equal(result.deleted_rows,0,failure);
    assert.equal(f.names("signal_episodes").length,1,failure); assert.equal(f.names("wallet_observation_bundles").length,1,failure);
    assert.ok(f.names("signal_outcomes").every(row => row.status==="complete"),failure);
    assert.equal(f.names("history_episode_retirement_work").length,0,failure);
  }
});

test("episode retirement refuses partial writes without FK enforcement or its explicit migration",async t => {
  const f=fixture(t); f.closed("old"); f.sql.exec("PRAGMA foreign_keys=OFF");
  const rollback=await cleanupClosedSqlEpisodes(f.env,{now:NOW});
  assert.equal(rollback.ok,false); assert.equal(rollback.deleted_rows,0);
  assert.equal(f.names("signal_episodes").length,1); assert.equal(f.names("wallet_observation_bundles").length,1);
  f.sql.exec("PRAGMA foreign_keys=ON; DROP TABLE history_episode_retirement_work"); f.calls.length=0;
  const missing=await cleanupClosedSqlEpisodes(f.env,{now:NOW});
  assert.equal(missing.ok,true); assert.equal(missing.enabled,false);
  assert.equal(missing.deferred_reason,"episode_retention_schema_required"); assert.ok(f.calls.every(call => /^SELECT/.test(call.text)));
});

test("episode BLOCKED and a lost committed batch response do not invent deletion counters",async t => {
  const f=fixture(t); f.closed("old");
  f.before=text => {if (/^INSERT INTO history_episode_retirement_work/.test(text)) throw new Error("storage_sql_statement_failed:BLOCKED");};
  const blocked=await cleanupClosedSqlEpisodes(f.env,{now:NOW});
  assert.equal(blocked.ok,false); assert.equal(blocked.deleted_rows,0); assert.equal(blocked.outcome_unknown,false);
  assert.equal(f.names("wallet_observation_bundles").length,1);
  f.before=null; f.afterBatch=() => {throw Object.assign(new Error("storage_sql_network_error"),{outcomeUnknown:true});};
  const lost=await cleanupClosedSqlEpisodes(f.env,{now:NOW});
  assert.equal(lost.ok,false); assert.equal(lost.deleted_rows,0); assert.equal(lost.outcome_unknown,true);
  assert.equal(lost.proven_rows,null); assert.equal(f.names("signal_episodes").length,0);
});

test("invalid post-commit episode metrics are uncertain and a failed second batch keeps confirmed first-batch counts",async t => {
  const malformed=fixture(t); malformed.closed("old");
  malformed.afterBatch=results => results.map((item,index) => index===1
    ? {...item,results:item.results.map(row => ({...row,bytes:-1}))} : item);
  const unknown=await cleanupClosedSqlEpisodes(malformed.env,{now:NOW});
  assert.equal(unknown.ok,false); assert.equal(unknown.outcome_unknown,true); assert.equal(unknown.deleted_rows,0);
  assert.equal(malformed.names("signal_episodes").length,0);
  const f=fixture(t); f.closed("a"); f.closed("b");
  f.sql.exec(`CREATE TRIGGER fail_second_retire BEFORE DELETE ON signal_episodes WHEN OLD.episode_id='b'
    BEGIN SELECT RAISE(ABORT,'injected'); END`);
  const partial=await cleanupClosedSqlEpisodes(f.env,{now:NOW});
  assert.equal(partial.ok,false); assert.equal(partial.deleted_episodes,1); assert.equal(partial.deleted_rows,8);
  assert.equal(partial.deleted_bytes,10); assert.equal(partial.proven_rows,null);
  assert.deepEqual(f.names("signal_episodes").map(row => row.episode_id),["b"]);
  assert.equal(f.names("wallet_observation_bundles").length,1); assert.equal(f.names("history_episode_retirement_work").length,0);
});

test("history integration shares its sixteen-query budget with conditional episode retirement",async t => {
  const f=fixture(t); f.closed("old");
  const result=await cleanupSqlHistoryRetention(f.env,{now:NOW});
  assert.equal(result.ok,true); assert.equal(result.episodes.deleted_episodes,1);
  assert.equal(result.deleted_rows,8); assert.equal(result.deleted_bytes,10); assert.ok(result.queries<=16);
  assert.equal((await cleanupSqlHistoryRetention(f.env,{now:NOW,maxQueries:17})).ok,false);
});

test("terminal marker requires full exact-zero cohort inventory and resolved disposition, never balance-only invalidation",() => {
  const valid={version:1,scope:"entire_original_cohort",status:"complete",checked_at:iso(NOW-91*DAY),
    history_complete:true,outflows_resolved:true,original_cohort_denominator_complete:true,original_sales_proven:true,
    issues:[],cohort_wallet_coverage_pct:100,cohort_token_coverage_pct:100,balance_coverage_pct:100,token_balance_coverage_pct:100,
    cohort_balance_raw:"0",remaining_upper_bound_raw:"0",active_positions_raw:"0",
    amounts_raw:{bought:"1000",sold:"1000",original:"0",transferred:"0",unknown:"0"}};
  const marker=terminalClosureMarker(valid);
  assert.equal(marker.event.thesis_status,"closed"); assert.equal(marker.episode.closed_at,valid.checked_at);
  assert.equal(marker.event.cohort_retained_pct,0); assert.equal(marker.event.data_quality_status,"complete");
  const invalid=[null,{}, {...valid,scope:"receipt_component_not_entire_cohort"}, {...valid,status:"partial"},
    {...valid,history_complete:false}, {...valid,outflows_resolved:false}, {...valid,original_sales_proven:false},
    {...valid,original_cohort_denominator_complete:false}, {...valid,cohort_wallet_coverage_pct:99.99},
    {...valid,balance_coverage_pct:99.99}, {...valid,token_balance_coverage_pct:null},
    {...valid,cohort_balance_raw:"1"}, {...valid,active_positions_raw:"1"}, {...valid,remaining_upper_bound_raw:"1"},
    {...valid,issues:["unknown_transfer_destination"]}, {...valid,amounts_raw:{...valid.amounts_raw,transferred:"1"}},
    {...valid,amounts_raw:{...valid.amounts_raw,unknown:"1"}}, {...valid,amounts_raw:{...valid.amounts_raw,sold:"999"}},
    {...valid,amounts_raw:{...valid.amounts_raw,bought:"0",sold:"0"}}, {...valid,cohort_balance_raw:0}];
  for (const proof of invalid) assert.equal(terminalClosureMarker(proof),null);
});
