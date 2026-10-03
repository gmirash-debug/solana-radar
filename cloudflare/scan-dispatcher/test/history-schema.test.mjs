import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {DatabaseSync} from "node:sqlite";
import test from "node:test";
import worker, {dashboardTokenDetail} from "../src/index.js";
import {HISTORY_SCHEMA_RELEASE, HISTORY_SCHEMA_SQL, historySchemaState} from "../src/history-schema.js";

function fixture(t, {base=true} = {}) {
  const db=new DatabaseSync(":memory:");
  t.after(()=>db.close());
  db.exec(`CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)`);
  if (base) for (const filename of ["0001_wallet_edge_history.sql","0002_cluster_edge_evidence.sql"]) {
    db.exec(readFileSync(new URL(`../migrations-history/${filename}`,import.meta.url),"utf8"));
    db.prepare("INSERT INTO d1_migrations(name) VALUES (?)").run(filename);
  }
  const api={db,calls:[],batches:[],failAt:null,failure:"D1_ERROR: account FREE daily D1 writes exhausted [code: 7500]"};
  api.prepare=sql=>{
    const prepared=values=>({sql,values,bind(...v){return prepared(v);},async first(){
      api.calls.push({sql,values,method:"first"});
      return db.prepare(sql).get(...values) || null;
    },async run(){
      api.calls.push({sql,values,method:"run"});
      db.prepare(sql).run(...values);
      return {success:true};
    }});
    return prepared([]);
  };
  api.batch=async statements=>{
    api.batches.push(statements.map(row=>row.sql));
    db.exec("BEGIN");
    try {
      const results=[];
      for (let i=0;i<statements.length;i++) {
        if (i===api.failAt) throw new Error(api.failure);
        results.push(await statements[i].run());
      }
      db.exec("COMMIT");
      return results;
    } catch (error) {db.exec("ROLLBACK");throw error;}
  };
  const env={HISTORY_SCHEMA_AUTO_UPGRADE:HISTORY_SCHEMA_RELEASE,RADAR_HISTORY_DB:api};
  const markerCount=()=>db.prepare("SELECT COUNT(*) n FROM d1_migrations WHERE name=?").get(HISTORY_SCHEMA_RELEASE).n;
  const schema=()=>db.prepare("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").all();
  return {db,api,env,markerCount,schema};
}

function runtimeAndQueue(f) {
  const documents=new Map();
  const pending=[{event_id:"accepted-original",raw:{unknown:null,wallets:[{wallet:"a",balance:null}]}}];
  const original=structuredClone(pending);
  let flushes=0;
  let statusReads=0;
  f.env.RADAR_INGEST_SECRET="test-secret";
  f.env.SCHEDULER_ENABLED="disabled";
  f.env.HISTORY_QUEUE={idFromName:name=>name,get(){return {async fetch(request){
    const path=new URL(request.url).pathname;
    if (path==="/flush") {
      assert.equal(f.markerCount(),1,"no queue flush before committed marker");
      flushes++;
      pending.length=0;
      return Response.json({ok:true,delivered:1,pending:0});
    }
    if (path==="/ingest") {
      pending.push(...(await request.json()).history_ledger.events);
      return Response.json({ok:true,queued:1,pending:pending.length});
    }
    assert.equal(path,"/status");
    statusReads++;
    return Response.json({ok:true,pending:pending.length,pending_bytes:12345,oldest_pending_age_seconds:86400});
  }};}};
  f.env.RUNTIME_SNAPSHOTS={idFromName:name=>name,get(){return {async fetch(request){
    const url=new URL(request.url);
    const name=decodeURIComponent(url.pathname.slice(1));
    if (request.method==="POST") {
      documents.set(name,await request.json());
      return Response.json({ok:true});
    }
    if (url.searchParams.has("projection")) return Response.json({ok:false},{status:404});
    return Response.json({ok:true,document:documents.get(name) || null});
  }};}};
  const scheduled=async cron=>{
    const tasks=[];
    await worker.scheduled({cron},f.env,{waitUntil:promise=>tasks.push(promise)});
    await Promise.all(tasks);
  };
  return {documents,pending,original,get flushes(){return flushes;},get statusReads(){return statusReads;},scheduled};
}

test("release guard SQL exactly matches 0003 and opts in only to that Wrangler release", t=>{
  const source=readFileSync(new URL("../migrations-history/0003_resumable_history.sql",import.meta.url),"utf8");
  assert.equal(HISTORY_SCHEMA_SQL,source);
  const config=readFileSync(new URL("../wrangler.toml",import.meta.url),"utf8");
  assert.match(config,/HISTORY_SCHEMA_AUTO_UPGRADE = "0003_resumable_history\.sql"/);
});

test("absent flag adds no queries; unsupported releases fail clearly without executing SQL", async()=>{
  const db={prepare(){throw new Error("unexpected query");}};
  assert.deepEqual(await historySchemaState({RADAR_HISTORY_DB:db},{upgrade:true}),{enabled:false,ready:true});
  for (const flag of ["0004_unknown.sql","",true]) {
    const state=await historySchemaState({RADAR_HISTORY_DB:db,HISTORY_SCHEMA_AUTO_UPGRADE:flag},{upgrade:true});
    assert.equal(state.ready,false);
    assert.equal(state.error,"history_schema_auto_upgrade_invalid_release");
  }
});

test("read-only gate checks one marker and does not attempt migration", async t=>{
  const f=fixture(t);
  const before=f.schema();
  const state=await historySchemaState(f.env);
  assert.equal(state.error,"history_schema_upgrade_pending");
  assert.equal(f.api.calls.length,1);
  assert.equal(f.api.batches.length,0);
  assert.deepEqual(f.schema(),before);
});

test("quota failure rolls back all ALTERs and tracking; reset succeeds atomically and retries are read-only", async t=>{
  const f=fixture(t);
  const before=f.schema();
  f.api.failAt=13;
  const pending=await historySchemaState(f.env,{upgrade:true});
  assert.equal(pending.error,"history_schema_upgrade_pending");
  assert.match(pending.cause,/7500/);
  assert.equal(f.markerCount(),0);
  assert.deepEqual(f.schema(),before);
  assert.equal(f.api.calls.filter(row=>row.method==="first").length,2);
  assert.equal(f.api.batches.length,1);
  f.api.failAt=null;
  const ready=await historySchemaState(f.env,{upgrade:true});
  assert.equal(ready.applied,true);
  assert.equal(f.markerCount(),1);
  assert.equal(f.db.prepare("SELECT job_id FROM history_cluster_lock").get().job_id,null);
  const expected=HISTORY_SCHEMA_SQL.split(";").map(sql=>sql.trim()).filter(Boolean);
  assert.deepEqual(f.api.batches[1].slice(0,-1),expected);
  assert.equal(f.api.batches[1].at(-1),"INSERT INTO d1_migrations(name) VALUES('0003_resumable_history.sql')");
  const calls=f.api.calls.length;
  assert.equal((await historySchemaState(f.env,{upgrade:true})).ready,true);
  assert.equal(f.api.calls.length,calls+1);
  assert.equal(f.api.batches.length,2);
});

test("concurrent migration loser rechecks committed marker without retrying ALTER", async t=>{
  const f=fixture(t);
  // Both initial marker reads see absence. Serialize actual D1 batch execution.
  const batch=f.api.batch;
  let tail=Promise.resolve();
  f.api.batch=statements=>{
    const current=tail.then(()=>batch(statements));
    tail=current.catch(()=>{});
    return current;
  };
  const states=await Promise.all([historySchemaState(f.env,{upgrade:true}),historySchemaState(f.env,{upgrade:true})]);
  assert.ok(states.every(state=>state.ready));
  assert.equal(states.filter(state=>state.applied).length,1);
  assert.equal(states.filter(state=>state.concurrent_commit).length,1);
  assert.equal(f.markerCount(),1);
  assert.equal(f.api.batches.length,2);
});

test("missing baseline or tracker and non-quota SQL errors surface without partial schema or fabricated marker", async t=>{
  const f=fixture(t,{base:false});
  const before=f.schema();
  const missing=await historySchemaState(f.env,{upgrade:true});
  assert.equal(missing.error,"history_schema_upgrade_failed");
  assert.match(missing.cause,/no such table.*wallet_clusters/);
  assert.deepEqual(f.schema(),before);
  assert.equal(f.markerCount(),0);
  f.db.exec("DROP TABLE d1_migrations");
  const tracker=await historySchemaState(f.env,{upgrade:true});
  assert.equal(tracker.error,"history_schema_upgrade_failed");
  assert.match(tracker.cause,/no such table.*d1_migrations/);
  assert.equal(f.api.batches.length,1);
  const g=fixture(t);
  const beforeSqlError=g.schema();
  g.api.failAt=10; g.api.failure="D1_ERROR: unexpected SQL migration failure";
  const failed=await historySchemaState(g.env,{upgrade:true});
  assert.equal(failed.status,"failed");
  assert.equal(failed.cause,g.api.failure);
  assert.deepEqual(g.schema(),beforeSqlError);
  assert.equal(g.markerCount(),0);
});

test("cron quota pending preserves raw backlog, publishes DO health, then resets into genuine flush", async t=>{
  const f=fixture(t);
  const runtime=runtimeAndQueue(f);
  f.api.failAt=13;
  await runtime.scheduled("*/5 * * * *");
  assert.equal(runtime.flushes,0);
  assert.equal(runtime.statusReads,1);
  assert.deepEqual(runtime.pending,runtime.original);
  let health=runtime.documents.get("history_status").value;
  assert.equal(health.error,"history_schema_upgrade_pending");
  assert.equal(health.history_schema.ready,false);
  assert.match(health.history_schema.cause,/7500/);
  assert.equal(health.pending_bytes,12345);
  assert.equal(health.oldest_pending_age_seconds,86400);
  assert.equal(health.flush_skipped,true);
  assert.equal(health.healthy,false);
  assert.ok(Date.parse(health.checked_at));
  f.api.failAt=null;
  await runtime.scheduled("*/5 * * * *");
  assert.equal(runtime.flushes,1);
  assert.equal(f.markerCount(),1);
  health=runtime.documents.get("history_status").value;
  assert.equal(health.history_schema.ready,true);
  assert.equal(health.pending,0);
  assert.equal(health.delivered,1);
  const batches=f.api.batches.length;
  await runtime.scheduled("*/5 * * * *");
  assert.equal(f.api.batches.length,batches);
});

test("public intelligence only reads marker, ingest stays durable, auth/manual boundaries prevent unauthorized upgrade", async t=>{
  const f=fixture(t);
  const runtime=runtimeAndQueue(f);
  for (const suffix of ["overview","wallets","wallet","clusters","cluster","episodes","episode","token"]) {
    const response=await worker.fetch(new Request(`https://worker/api/intelligence/${suffix}`),f.env);
    assert.equal(response.status,503);
    assert.equal((await response.json()).error,"history_schema_upgrade_pending");
  }
  assert.equal(f.api.calls.length,8);
  assert.equal(f.api.batches.length,0);
  assert.equal(runtime.flushes,0);
  const accepted={event_id:"new-pending",raw:{amount:null,retained_evidence:"full"}};
  const ingest=await worker.fetch(new Request("https://worker/api/runtime/history",{method:"POST",
    headers:{"x-radar-ingest-secret":"test-secret"},body:JSON.stringify({history_ledger:{events:[accepted]}})}),f.env);
  assert.equal(ingest.status,200);
  assert.deepEqual(runtime.pending,[...runtime.original,accepted]);
  const flushUrl="https://worker/api/runtime/history/flush";
  assert.equal((await worker.fetch(new Request(flushUrl,{method:"POST"}),f.env)).status,401);
  assert.equal((await worker.fetch(new Request(flushUrl,{headers:{"x-radar-ingest-secret":"test-secret"}}),f.env)).status,405);
  assert.equal(f.api.batches.length,0);
  f.api.failAt=0;
  const manual=await worker.fetch(new Request(flushUrl,{method:"POST",headers:{"x-radar-ingest-secret":"test-secret"}}),f.env);
  assert.equal(manual.status,503);
  assert.equal((await manual.json()).error,"history_schema_upgrade_pending");
  assert.equal(f.api.batches.length,1);
  assert.equal(runtime.flushes,0);
  const handoff=await worker.fetch(new Request("https://worker/api/runtime/history/migrate-outbox",{method:"POST",
    headers:{"x-radar-ingest-secret":"test-secret"}}),f.env);
  assert.equal(handoff.status,503);
  assert.equal(f.api.batches.length,1);
});

test("legacy hourly flush cannot run before schema, and public dashboard overlays fresh schema pending health", async t=>{
  const f=fixture(t);
  const runtime=runtimeAndQueue(f);
  f.env.RADAR_DB={prepare(){throw new Error("legacy outbox/dashboard must not query D1");}};
  await runtime.scheduled("37 * * * *");
  assert.equal(f.api.batches.length,0);
  assert.equal(runtime.flushes,0);
  const fresh=runtime.documents.get("history_status").value;
  runtime.documents.set("dashboard",{updated_at:"2026-10-03T12:00:00Z",value:{report:{generated_at:"2026-10-03T12:00:00Z"},
    history_status:{healthy:true,checked_at:"old"}}});
  const response=await worker.fetch(new Request("https://worker/api/dashboard"),f.env);
  assert.equal(response.status,200);
  const dashboard=await response.json();
  assert.deepEqual(dashboard.history_status,fresh);
  assert.equal(dashboard.history_status.error,"history_schema_upgrade_pending");
  assert.equal(dashboard.report.generated_at,"2026-10-03T12:00:00Z");
  assert.equal(f.api.batches.length,0);
});

test("pending schema does not block scanner dispatch or token details with an optional null history edge", async t=>{
  const f=fixture(t);
  const runtime=runtimeAndQueue(f);
  f.api.failAt=0;
  f.env.SCHEDULER_ENABLED="true";
  f.env.GITHUB_TOKEN="test-token";
  const originalFetch=globalThis.fetch;
  let dispatches=0;
  globalThis.fetch=async(_url,options)=>{
    if (options?.method==="POST") {dispatches++;return new Response(null,{status:204});}
    return Response.json({workflow_runs:[]});
  };
  try {await runtime.scheduled("*/5 * * * *");}
  finally {globalThis.fetch=originalFetch;}
  assert.equal(dispatches,1);
  assert.equal(runtime.flushes,0);
  assert.equal(runtime.documents.get("history_status").value.error,"history_schema_upgrade_pending");
  const generation="2026-10-03T12:00:00Z";
  const summary={token_address:"mint",cohort_id:"cohort",signal_at:generation};
  f.env.RADAR_DB={prepare(sql){return {bind(){return this;},async all(){return {results:[]};},async first(){
    if (sql.includes("'latest_report'")) return {payload_json:JSON.stringify({signal_theses:[summary]}),source_updated_at:generation};
    return null;
  }};}};
  const detail=await dashboardTokenDetail(f.env,"mint");
  assert.equal(detail.ok,true);
  assert.equal(detail.wallet_edge,null);
  assert.equal(detail.thesis.token_address,"mint");
  const reads=f.api.calls.filter(row=>row.method==="first");
  assert.ok(reads.every(row=>row.sql.includes("d1_migrations")));
});
