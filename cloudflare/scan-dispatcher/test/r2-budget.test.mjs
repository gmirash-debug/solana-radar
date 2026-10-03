import test from "node:test";
import assert from "node:assert/strict";
import {R2Budget,guardR2Env,r2BudgetCall,dispatchR2BudgetNotice} from "../src/r2-budget.js";
import worker from "../src/index.js";

function fixture(t, baseline = {}) {
  let now = Date.parse("2026-10-03T12:00:00Z");
  const saved = Date.now; Date.now = () => now; t.after(()=>{Date.now=saved;});
  const values = new Map(); let tail = Promise.resolve(), writes = 0;
  const storage = {transaction(fn) {
    const promise = tail.then(async()=>{
      const staged = new Map(structuredClone([...values]));
      const result = await fn({async get(key){return staged.get(key);},
        async put(rows){writes++;for(const [k,v] of Object.entries(rows))staged.set(k,v);},
        async delete(keys){keys.forEach(k=>staged.delete(k));}});
      values.clear();for(const [k,v] of staged)values.set(k,v);return result;
    }); tail=promise.catch(()=>{});return promise;
  }};
  const object = new R2Budget({storage});
  const calls = [];
  const raw = Object.fromEntries(["put","head","get","list","delete"].map(method=>[method,async(...args)=>{calls.push({method,args});return method==="list"?{objects:[],truncated:false}:null;}]));
  const env = {R2_BUDGET_GUARD:"enabled",R2_BUDGET:{idFromName:name=>name,get:()=>object},RADAR_ARCHIVE:raw};
  const call = (path,body)=>r2BudgetCall(env,path,body);
  return {env,raw,calls,values,call,storage,get writes(){return writes;},advance:date=>{now=Date.parse(date);},
    seed:()=>call("bootstrap",{month:"2026-10",class_a:0,class_b:0,account_storage_bytes:0,objects:[],...baseline})};
}

test("uninitialized guard fails closed without touching R2",async t=>{
  const f=fixture(t), env=guardR2Env(f.env);
  for(const method of ["head","get","put","list"]) await assert.rejects(env.RADAR_ARCHIVE[method]("key","x"),/baseline_required/);
  assert.equal(f.calls.length,0);assert.equal((await f.call("status")).paused,true);
});

test("every HEAD, GET, PUT and LIST is charged before native I/O",async t=>{
  const f=fixture(t);await f.seed();const env=guardR2Env(f.env);
  await env.RADAR_ARCHIVE.head("one");await env.RADAR_ARCHIVE.get("one");
  await env.RADAR_ARCHIVE.put("one",new Uint8Array(10));await env.RADAR_ARCHIVE.list({limit:100});
  const s=await f.call("status");assert.deepEqual(s.usage,{class_a:2,class_b:2,storage_bytes:4106});
  assert.equal(f.calls.length,4);
  const before=f.writes;await f.call("status");await f.call("status");assert.equal(f.writes,before);
  assert.equal(guardR2Env(env),env);
});

test("failed network operations remain conservatively charged",async t=>{
  const f=fixture(t);await f.seed();f.raw.put=async()=>{throw new Error("uncertain network outcome");};
  await assert.rejects(guardR2Env(f.env).RADAR_ARCHIVE.put("one","abc"));
  assert.equal((await f.call("status")).usage.class_a,1);assert.equal((await f.call("status")).usage.storage_bytes,4099);
});

test("90 percent of Class A blocks ALL paid R2 operations until next month",async t=>{
  const f=fixture(t,{class_a:899998});await f.seed();const env=guardR2Env(f.env);
  await env.RADAR_ARCHIVE.put("one","a");
  await assert.rejects(env.RADAR_ARCHIVE.put("two","b"),/monthly_budget_paused/);
  await assert.rejects(env.RADAR_ARCHIVE.head("one"),/monthly_budget_paused/);
  await assert.rejects(env.RADAR_ARCHIVE.get("one"),/monthly_budget_paused/);
  await assert.rejects(env.RADAR_ARCHIVE.list({}),/monthly_budget_paused/);
  assert.equal(f.calls.length,1);
  const s=await f.call("status");assert.equal(s.usage.class_a,899999);assert.equal(s.pause_reason,"class_a");
  assert.equal(s.notifications.filter(n=>n.kind==="paused").length,1);
  await f.call("status");assert.equal((await f.call("status")).notifications.filter(n=>n.kind==="paused").length,1);
});

test("Class B cannot exceed 90 percent under concurrent reservations",async t=>{
  const f=fixture(t,{class_b:8999995});await f.seed();
  const results=await Promise.all(Array.from({length:40},()=>f.call("reserve",{kind:"get",key:"one"})));
  assert.equal(results.filter(r=>r.allowed).length,4);assert.equal((await f.call("status")).usage.class_b,8999999);
});

test("projected storage includes metadata and rejects overshooting PUT",async t=>{
  const f=fixture(t,{account_storage_bytes:9_000_000_000-5000});await f.seed();
  await assert.rejects(guardR2Env(f.env).RADAR_ARCHIVE.put("one",new Uint8Array(1000)),/monthly_budget_paused/);
  assert.equal(f.calls.length,0);assert.equal((await f.call("status")).pause_reason,"storage_bytes");
});

test("same-key overwrites do not inflate known storage; failed DELETE cannot refund",async t=>{
  const f=fixture(t,{objects:[{key:"one",size:10}]});await f.seed();const env=guardR2Env(f.env);
  await env.RADAR_ARCHIVE.put("one",new Uint8Array(10));assert.equal((await f.call("status")).usage.storage_bytes,4106);
  f.raw.delete=async()=>{throw new Error("delete unavailable");};await assert.rejects(env.RADAR_ARCHIVE.delete("one"));
  assert.equal((await f.call("status")).usage.storage_bytes,4106);
  f.raw.delete=async()=>{};await env.RADAR_ARCHIVE.delete("one");assert.equal((await f.call("status")).usage.storage_bytes,0);
  assert.equal((await f.call("status")).usage.class_a,1);
});

test("free safe GC works while paused but does not resume within the same month",async t=>{
  const f=fixture(t,{class_a:900000,objects:[{key:"one",size:10}]});await f.seed();
  await guardR2Env(f.env).RADAR_ARCHIVE.delete("one");
  assert.equal((await f.call("status")).paused,true);assert.equal((await f.call("status")).usage.storage_bytes,0);
});

test("UTC month rollover resets calendar counts, but retains billing-cycle safety and storage",async t=>{
  const f=fixture(t,{class_b:9000000,objects:[{key:"one",size:10}]});await f.seed();
  f.advance("2026-10-31T23:59:59.999Z");assert.equal((await f.call("status")).paused,true);
  f.advance("2026-11-01T00:00:00Z");const s=await f.call("status");
  assert.equal(s.paused,true);assert.deepEqual(s.calendar_usage,{class_a:0,class_b:0});
  assert.equal(s.usage.storage_bytes,4106);assert.equal(s.pause_reason,"rolling_class_b");
  f.advance("2026-11-05T00:00:00Z");const resumed=await f.call("status");
  assert.equal(resumed.paused,false);assert.equal(resumed.usage.class_b,0);
  assert.equal(resumed.notifications.filter(n=>n.kind==="resumed").length,1);
});

test("a calendar reset cannot spend two free allowances inside the real provider billing cycle",async t=>{
  const f=fixture(t,{class_a:899998});await f.seed();
  await guardR2Env(f.env).RADAR_ARCHIVE.put("one","x");
  f.advance("2026-11-01T00:00:00Z");
  await assert.rejects(guardR2Env(f.env).RADAR_ARCHIVE.put("two","x"),/monthly_budget_paused/);
  assert.equal(f.calls.length,1);assert.equal((await f.call("status")).calendar_usage.class_a,0);
});

test("storage at 90 percent remains paused across month rollover",async t=>{
  const f=fixture(t,{account_storage_bytes:9_000_000_000});await f.seed();f.advance("2026-11-01T00:00:00Z");
  const s=await f.call("status");assert.equal(s.paused,true);assert.equal(s.pause_reason,"storage_bytes");
  assert.equal(s.notifications.some(n=>n.kind==="resumed"),false);
});

test("baseline cannot be reset to evade an already active guard",async t=>{
  const f=fixture(t,{class_a:900000});await f.seed();const r=await f.call("bootstrap",{month:"2026-10",class_a:0,class_b:0,account_storage_bytes:0,objects:[]});
  assert.equal(r.unchanged,true);assert.equal(r.paused,true);assert.equal(r.usage.class_a,900000);
});

test("warning at 80 percent and notification acknowledgement are durable and deduplicated",async t=>{
  const f=fixture(t,{class_b:8000000});await f.seed();const s=await f.call("status");assert.equal(s.notifications.length,1);
  await assert.rejects(f.call("ack",{id:s.notifications[0].id,issue_url:"https://attacker.invalid/1"}));
  await f.call("ack",{id:s.notifications[0].id,issue_url:"https://github.com/gmirash-debug/solana-radar/issues/123"});
  assert.ok((await f.call("status")).notifications[0].notified_at);
  assert.equal((await f.call("claim-dispatch",{})).dispatch,false);
});

test("missing budget DO and unbounded or Infrequent Access writes fail closed",async t=>{
  const f=fixture(t);await f.seed();
  await assert.rejects(guardR2Env({...f.env,R2_BUDGET:undefined}).RADAR_ARCHIVE.get("one"),/not_configured/);
  await assert.rejects(guardR2Env(f.env).RADAR_ARCHIVE.put("one",new ReadableStream()),/unbounded/);
  await assert.rejects(guardR2Env(f.env).RADAR_ARCHIVE.put("one","x",{storageClass:"InfrequentAccess"}),/infrequent/);
  assert.equal(f.calls.length,0);
});

test("notification dispatch is bounded, targets the main workflow and retries after 15 minutes",async t=>{
  const f=fixture(t,{class_a:900000});await f.seed();const env={...f.env,GITHUB_TOKEN:"not-a-real-key"};let requests=0;
  const send=async(url,opts)=>{requests++;assert.ok(url.endsWith("/r2-budget-monitor.yml/dispatches"));
    assert.equal(opts.headers.authorization,"Bearer not-a-real-key");assert.equal(opts.redirect,"manual");return new Response(null,{status:204});};
  assert.equal((await dispatchR2BudgetNotice(env,send)).dispatched,true);
  assert.equal((await dispatchR2BudgetNotice(env,send)).dispatched,false);assert.equal(requests,1);
  f.advance("2026-10-03T12:15:01Z");assert.equal((await dispatchR2BudgetNotice(env,send)).dispatched,true);
});

test("public monitor reports usage but bootstrap and ACK require authentication",async t=>{
  const f=fixture(t);await f.seed();
  assert.equal((await worker.fetch(new Request("https://radar/api/storage/r2-budget"),f.env)).status,200);
  for(const name of ["bootstrap","ack"]) assert.equal((await worker.fetch(new Request(`https://radar/api/storage/r2-budget/${name}`,{method:"POST",body:"{}"}),{...f.env,RADAR_INGEST_SECRET:"secret"})).status,401);
  assert.equal(f.calls.length,0);
});

test("paused dashboard and token detail use SQL without reading any archive",async t=>{
  const f=fixture(t,{class_a:900000});await f.seed();
  const at="2026-10-03T11:00:00Z";
  const thesis={token_address:"mint",cohort_id:"cohort",age_hours:2,signal_at:at,last_checked_at:at,cohort_wallets:[{owner:"buyer",current_balance:25}]};
  const report={generated_at:at,signal_theses:[thesis],alerts:[],config:{age_min_hours:0.5,age_max_hours:360}};
  let durableReads=0;
  const db={prepare(sql){let values=[];return {bind(...args){values=args;return this;},async all(){return {results:[]};},async first(){
    const detail=values[0]==="signal_thesis:mint";
    if(sql.includes("'latest_report'")||detail) return {payload_json:JSON.stringify(detail?thesis:report),source_updated_at:at};
    return null;
  }};}};
  const env={...f.env,RADAR_DB:db,RUNTIME_SNAPSHOTS:{idFromName:n=>n,get:()=>({fetch(){durableReads++;throw new Error("archive must not be read");}})}};
  const dashboard=await worker.fetch(new Request("https://radar/api/dashboard"),env);
  assert.equal(dashboard.status,200);assert.equal((await dashboard.json()).r2_budget.paused,true);
  const response=await worker.fetch(new Request("https://radar/api/dashboard/token?token_key=mint"),env);
  assert.equal(response.status,200);assert.equal((await response.json()).thesis.cohort_wallets[0].current_balance,25);
  assert.equal(durableReads,0);assert.equal(f.calls.length,0);
});

test("malformed bootstrap baseline cannot consume an unguarded inventory call",async t=>{
  const f=fixture(t);
  const response=await worker.fetch(new Request("https://radar/api/storage/r2-budget/bootstrap",{
    method:"POST",headers:{"x-radar-ingest-secret":"secret"},body:JSON.stringify({month:"2026-10",class_a:-1,class_b:0,account_storage_bytes:0}),
  }),{...f.env,RADAR_INGEST_SECRET:"secret"});
  assert.equal(response.status,400);assert.equal(f.calls.length,0);
});

test("bootstrap inventories every page and cannot skip older archive objects",async t=>{
  const f=fixture(t);let lists=0;
  f.raw.list=async(options)=>{
    lists++;if(lists===1)return {truncated:true,cursor:"next",objects:Array.from({length:1000},(_,i)=>({key:`k${i}`,size:10}))};
    assert.equal(options.cursor,"next");return {truncated:false,objects:[{key:"last",size:20}]};
  };
  const request=()=>new Request("https://radar/api/storage/r2-budget/bootstrap",{method:"POST",headers:{"x-radar-ingest-secret":"secret"},
    body:JSON.stringify({month:new Date().toISOString().slice(0,7),class_a:10,class_b:20,account_storage_bytes:0})});
  const env={...f.env,RADAR_INGEST_SECRET:"secret"};
  const response=await worker.fetch(request(),env);assert.equal(response.status,200);
  const state=await response.json();assert.equal(state.usage.class_a,12);assert.equal(state.usage.storage_bytes,1000*4106+4116);
  const again=await worker.fetch(request(),env);assert.equal((await again.json()).unchanged,true);assert.equal(lists,2);
});
