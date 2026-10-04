import test from "node:test";
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {R2Budget, R2_BATCH_LIMITS, guardR2Env, r2BudgetCall, withR2BudgetBatch} from "../src/r2-budget.js";
import {archiveHistoryEvent, archiveHistoryEvents, readHistoryArchive} from "../src/archive.js";
import {archiveRuntimeDocument, archiveRuntimeDocuments, readRuntimeArchive, deleteRuntimeArchive} from "../src/runtime-archive.js";

const hash = bytes => createHash("sha256").update(bytes).digest("hex");
function fixture(t, baseline = {}) {
  let now = Date.parse("2026-10-03T12:00:00Z"), tail = Promise.resolve(), failWrite = false;
  const original = Date.now;
  Date.now = () => now; t.after(()=>{Date.now=original;});
  const values = new Map(), writes = [], calls = [], requests = [], objects = new Map();
  const storage = {async get(key) {return structuredClone(values.get(key));}, transaction(fn) {
    const promise = tail.then(async()=>{
      const staged = new Map(structuredClone([...values]));
      const result = await fn({async get(key) {return staged.get(key);},
        async put(rows) {if (failWrite) throw new Error("authority writes unavailable");
          writes.push(Object.keys(rows)); for (const [key,value] of Object.entries(rows)) staged.set(key,value);},
        async delete(keys) {for (const key of keys) staged.delete(key);}});
      values.clear(); for (const [key,value] of staged) values.set(key,value);
      return result;
    }); tail=promise.catch(()=>{});return promise;
  }};
  let budget = new R2Budget({storage});
  const metadata = row => ({key:row.key,size:row.bytes.byteLength,customMetadata:row.metadata,
    checksums:{sha256:Uint8Array.from(Buffer.from(row.checksum,"hex")).buffer}});
  const raw = {before:null,
    async head(key) {calls.push({method:"head",key});await raw.before?.("head",key);const row=objects.get(key);return row?metadata(row):null;},
    async get(key) {calls.push({method:"get",key});await raw.before?.("get",key);const row=objects.get(key);
      return row?{...metadata(row),body:new Blob([row.bytes]).stream()}:null;},
    async put(key,value,options={}) {calls.push({method:"put",key});await raw.before?.("put",key);
      if (options.onlyIf && objects.has(key)) return null;
      const bytes=typeof value==="string"?new TextEncoder().encode(value):new Uint8Array(value);
      if (options.sha256) assert.equal(hash(bytes),options.sha256);
      objects.set(key,{key,bytes,metadata:options.customMetadata,checksum:hash(bytes)});return metadata(objects.get(key));},
    async list() {calls.push({method:"list"});return {objects:[],truncated:false};},
    async delete(key) {calls.push({method:"delete",key});objects.delete(key);},
  };
  const env={R2_BUDGET_GUARD:"enabled",RUNTIME_ARCHIVE_MODE:"r2",RADAR_ARCHIVE:raw,
    R2_BUDGET:{idFromName:name=>name,get:()=>({fetch(request) {requests.push(new URL(request.url).pathname);return budget.fetch(request);}})}};
  const call=(path,body)=>r2BudgetCall(env,path,body);
  return {env,raw,values,writes,calls,requests,objects,call,
    advance:date=>{now=typeof date==="number"?date:Date.parse(date);},
    restart:()=>{budget=new R2Budget({storage});}, failWrites:()=>{failWrite=true;},
    seed:()=>call("bootstrap",{month:"2026-10",class_a:0,class_b:0,account_storage_bytes:0,objects:[],...baseline})};
}

const head = key => ({kind:"head",key});
const put = (key,bytes) => ({kind:"put",key,bytes});
const event = id => ({event_id:id,episode:{episode_id:"episode",token_address:"mint",caught_at:"2026-10-01T00:00:00Z"},
  event:{event_type:"signal",observed_at:"2026-10-01T01:00:00Z"},wallets:[],evidence:{id}});
const budgetWrites = f => f.writes.filter(keys=>keys.includes("budget:v1")).length;
const catalogRows = f => f.writes.flat().filter(key=>key.startsWith("object:")).length;

test("one batch precharges the full native A/B and per-key maximum storage before any I/O",async t=>{
  const f=fixture(t,{objects:[{key:"known",size:4}],class_a:20,class_b:30});await f.seed();
  const before=budgetWrites(f),catalogBefore=catalogRows(f);
  const plan=[head("new"),put("new",10),head("new"),put("known",5),put("known",20),{kind:"list"}];
  await withR2BudgetBatch(guardR2Env(f.env),plan,async scoped=>{
    assert.deepEqual((await f.call("status")).usage,{class_a:24,class_b:32,storage_bytes:4106+4116});
    await scoped.RADAR_ARCHIVE.head("new");await scoped.RADAR_ARCHIVE.put("new","x");
    await scoped.RADAR_ARCHIVE.head("new");await scoped.RADAR_ARCHIVE.put("known","a");
    await scoped.RADAR_ARCHIVE.put("known","b");await scoped.RADAR_ARCHIVE.list({limit:100});
  });
  assert.equal(f.calls.length,6);assert.equal(budgetWrites(f)-before,1);assert.equal(catalogRows(f)-catalogBefore,2);
  assert.equal(f.requests.filter(path=>path==="/reserve").length,0);
});

test("simultaneous native calls consume a local slot once, including uncertain failures",async t=>{
  const f=fixture(t);await f.seed();
  f.raw.before=()=>{throw new Error("uncertain outcome");};
  await withR2BudgetBatch(f.env,[head("one")],async scoped=>{
    const results=await Promise.allSettled(Array.from({length:30},()=>scoped.RADAR_ARCHIVE.head("one")));
    assert.equal(results.filter(r=>r.reason?.message==="uncertain outcome").length,1);
    assert.equal(results.filter(r=>r.reason?.message==="r2_budget_batch_operation_not_reserved").length,29);
    await assert.rejects(scoped.RADAR_ARCHIVE.head("one"),/not_reserved/);
  });
  assert.equal(f.calls.length,1);assert.equal((await f.call("status")).usage.class_b,1);
});

test("concurrent independent batches cannot cross either native operation cap",async t=>{
  for (const [counter,ops,remaining] of [["class_a",[put("one",1),put("two",1)],7],
    ["class_b",[head("one"),head("two")],7]]) {
    const f=fixture(t,{[counter]:counter==="class_a"?900000-remaining:9000000-remaining});await f.seed();
    const results=await Promise.allSettled(Array.from({length:20},()=>withR2BudgetBatch(f.env,ops,async scoped=>{
      for (const op of ops) await scoped.RADAR_ARCHIVE[op.kind](op.key,...(op.kind==="put"?["x"]:[]));
    })));
    assert.equal(results.filter(r=>r.status==="fulfilled").length,3);
    assert.equal((await f.call("status")).usage[counter],counter==="class_a"?899999:8999999);
    assert.equal(f.calls.length,6);
  }
});

test("concurrent unique-key grants reserve their combined storage atomically below 90 percent",async t=>{
  const f=fixture(t,{account_storage_bytes:9_000_000_000-3*4097});await f.seed();
  const results=await Promise.allSettled(Array.from({length:20},(_,i)=>withR2BudgetBatch(f.env,[put(`key-${i}`,1)],async scoped=>{
    await scoped.RADAR_ARCHIVE.put(`key-${i}`,"x");
  })));
  assert.equal(results.filter(r=>r.status==="fulfilled").length,2);assert.equal(f.calls.length,2);
  assert.equal((await f.call("status")).usage.storage_bytes,9_000_000_000-4097);
  assert.equal((await f.call("status")).pause_reason,"storage_bytes");
});

test("unused, thrown and process-lost grants stay charged with their complete storage bounds",async t=>{
  const f=fixture(t);await f.seed();let leaked;
  await withR2BudgetBatch(f.env,[head("one"),put("one",10)],scoped=>{leaked=scoped;});
  await assert.rejects(leaked.RADAR_ARCHIVE.head("one"),/batch_closed/);
  await assert.rejects(withR2BudgetBatch(f.env,[put("two",20)],()=>{throw new Error("invocation crash");}),/invocation crash/);
  const lost=await f.call("reserve-batch",{operations:[put("three",30),head("three")]});
  assert.equal(lost.allowed,true);f.restart();f.advance("2026-10-03T12:01:00Z");
  assert.deepEqual((await f.call("status")).usage,{class_a:3,class_b:2,storage_bytes:3*4096+60});
  assert.equal(f.calls.length,0);
});

test("grant scope cannot be nested, reused by another invocation or spent through an ordinary guard",async t=>{
  const f=fixture(t);await f.seed();let leaked;
  await withR2BudgetBatch(f.env,[head("one")],async scoped=>{
    leaked=scoped;
    await assert.rejects(withR2BudgetBatch(scoped,[head("one")],()=>{}),/scope_invalid/);
    await guardR2Env(f.env).RADAR_ARCHIVE.head("one");
    await scoped.RADAR_ARCHIVE.head("one");
  });
  await withR2BudgetBatch(f.env,[head("one")],async scoped=>{
    await assert.rejects(leaked.RADAR_ARCHIVE.head("one"),/batch_closed/);
    await scoped.RADAR_ARCHIVE.head("one");
  });
  assert.equal(f.calls.length,3);assert.equal((await f.call("status")).usage.class_b,3);
});

test("request replay recharges operations but writes an immutable-key catalog bound only once",async t=>{
  const f=fixture(t);await f.seed();const catalogBefore=catalogRows(f),before=budgetWrites(f);
  const body={operations:[head("one"),put("one",10),head("one")],request_id:"same-id"};
  const a=await f.call("reserve-batch",body),b=await f.call("reserve-batch",{...body,grant:a.grant});
  assert.equal(a.grant.catalog_rows_written,1);assert.equal(b.grant.catalog_rows_written,0);
  assert.equal(a.grant.budget_rows_written,1);assert.equal(budgetWrites(f)-before,2);assert.equal(catalogRows(f)-catalogBefore,1);
  assert.deepEqual((await f.call("status")).usage,{class_a:2,class_b:4,storage_bytes:4106});
});

test("another invocation cannot reuse a cached grant response even within the same clock millisecond",async t=>{
  const f=fixture(t);await f.seed();let cached;
  const authority=f.env.R2_BUDGET.get();
  const env={...f.env,R2_BUDGET:{idFromName:n=>n,get:()=>({async fetch(request) {
    if (!cached) cached=await authority.fetch(request);return cached.clone();
  }})}};
  await withR2BudgetBatch(env,[head("one")],scoped=>scoped.RADAR_ARCHIVE.head("one"));
  await assert.rejects(withR2BudgetBatch(env,[head("one")],scoped=>scoped.RADAR_ARCHIVE.head("one")),/grant_invalid/);
  assert.equal((await f.call("status")).usage.class_b,1);assert.equal(f.calls.length,1);
});

test("DELETE and smaller single-operation PUT cannot refund a batch high-water mark or permit a late-write race",async t=>{
  const f=fixture(t,{objects:[{key:"one",size:10}]});await f.seed();
  await withR2BudgetBatch(f.env,[put("one",20)],async scoped=>{
    await guardR2Env(f.env).RADAR_ARCHIVE.delete("one");
    assert.equal((await f.call("status")).usage.storage_bytes,4116);
    await scoped.RADAR_ARCHIVE.put("one","late");
  });
  await guardR2Env(f.env).RADAR_ARCHIVE.put("one","x");
  await guardR2Env(f.env).RADAR_ARCHIVE.delete("one");
  assert.equal((await f.call("status")).usage.storage_bytes,4116);
  assert.deepEqual(f.values.get("object:one"),{bytes:4116,retained:true});
  await withR2BudgetBatch(f.env,[put("one",20)],scoped=>scoped.RADAR_ARCHIVE.put("one","again"));
  assert.equal((await f.call("status")).usage.storage_bytes,4116);
});

test("grant expiry precedes UTC month and day boundaries; expired slots never re-reserve",async t=>{
  const f=fixture(t);await f.seed();f.advance("2026-10-31T23:59:58Z");
  const grant=await f.call("reserve-batch",{operations:[head("unused")]});
  assert.equal(grant.grant.expires_at,Date.parse("2026-10-31T23:59:59Z"));
  await withR2BudgetBatch(f.env,[head("one"),head("two")],async scoped=>{
    await scoped.RADAR_ARCHIVE.head("one");f.advance("2026-11-01T00:00:00Z");
    await assert.rejects(scoped.RADAR_ARCHIVE.head("two"),/batch_expired/);
  });
  assert.equal(f.calls.length,1);const s=await f.call("status");
  assert.deepEqual(s.calendar_usage,{class_a:0,class_b:0});assert.equal(s.rolling_usage.class_b,3);
  f.advance("2026-11-02T23:59:59.500Z");
  await assert.rejects(withR2BudgetBatch(f.env,[head("three")],()=>{}),/batch_expired/);
});

test("duration, caller deadline and backwards-clock checks cannot extend a grant",async t=>{
  const f=fixture(t);await f.seed();const start=Date.now();
  await withR2BudgetBatch(f.env,[head("one")],async scoped=>{
    f.advance(start+R2_BATCH_LIMITS.durationMs);await assert.rejects(scoped.RADAR_ARCHIVE.head("one"),/batch_expired/);
  });
  f.advance(start);
  await withR2BudgetBatch(f.env,[head("one")],async scoped=>{
    f.advance(start+5);await assert.rejects(scoped.RADAR_ARCHIVE.head("one"),/batch_expired/);
  },{deadline:start+5});
  f.advance(start);
  await withR2BudgetBatch(f.env,[head("one")],async scoped=>{
    f.advance(start-1);await assert.rejects(scoped.RADAR_ARCHIVE.head("one"),/batch_expired/);
  });
  assert.equal(f.calls.length,0);
});

test("warning80 also uses the rolling grant charge after calendar rollover",async t=>{
  const f=fixture(t,{class_a:799999});await f.seed();
  await withR2BudgetBatch(f.env,[put("one",1)],()=>{});
  let s=await f.call("status");assert.equal(s.notifications.find(n=>n.kind==="warning").class_a,800000);
  f.advance("2026-11-01T00:00:00Z");s=await f.call("status");
  assert.equal(s.calendar_usage.class_a,0);assert.equal(s.rolling_usage.class_a,800000);
  assert.equal(s.notifications.filter(n=>n.kind==="warning"&&n.month==="2026-11").length,1);
});

test("calendar rollover cannot erase a 33-day grant charge or bypass the global pause",async t=>{
  const f=fixture(t,{class_a:899997});await f.seed();
  await withR2BudgetBatch(f.env,[put("one",1),{kind:"list"}],()=>{});
  f.advance("2026-11-01T00:00:00Z");
  // The requested plan, not just its first call, must fit below the rolling threshold.
  await assert.rejects(withR2BudgetBatch(f.env,[put("two",1),put("three",1)],()=>{}),/monthly_budget_paused/);
  await assert.rejects(withR2BudgetBatch(f.env,[head("one")],()=>{}),/monthly_budget_paused/);
  assert.equal((await f.call("status")).pause_reason,"rolling_class_a");assert.equal(f.calls.length,0);
  f.advance("2026-11-04T23:59:00Z");assert.equal((await f.call("status")).paused,true);
  f.advance("2026-11-05T00:00:00Z");
  await withR2BudgetBatch(f.env,[head("one")],scoped=>scoped.RADAR_ARCHIVE.head("one"));
  assert.equal((await f.call("status")).usage.class_a,0);
});

test("bootstrap cannot reset usage, and a batch retains baseline and account-wide storage",async t=>{
  const f=fixture(t,{class_a:800000,class_b:30,account_storage_bytes:1_000_000,objects:[{key:"known",size:10}]});await f.seed();
  await withR2BudgetBatch(f.env,[put("known",10),put("new",20)],()=>{});
  const s=await f.call("bootstrap",{month:"2026-10",class_a:0,class_b:0,account_storage_bytes:0,objects:[]});
  assert.equal(s.unchanged,true);assert.deepEqual(s.usage,{class_a:800002,class_b:30,storage_bytes:1_004_116});
  assert.equal(s.baseline_at,"2026-10-03T12:00:00.000Z");
});

test("missing, uninitialized, write-exhausted or malformed authorities fail closed before native I/O",async t=>{
  const f=fixture(t);
  await assert.rejects(withR2BudgetBatch(f.env,[head("one")],scoped=>scoped.RADAR_ARCHIVE.head("one")),/baseline_required/);
  await f.seed();
  await assert.rejects(withR2BudgetBatch({...f.env,R2_BUDGET:undefined},[head("one")],()=>{}),/not_configured/);
  const fake={idFromName:n=>n,get:()=>({fetch:async()=>Response.json({ok:true,allowed:true})})};
  await assert.rejects(withR2BudgetBatch({...f.env,R2_BUDGET:fake},[head("one")],()=>{}),/grant_invalid/);
  f.failWrites();await assert.rejects(withR2BudgetBatch(f.env,[put("one",1)],()=>{}),/budget_unavailable/);
  assert.equal((await f.call("status")).usage.class_a,0);assert.equal(f.values.has("object:one"),false);assert.equal(f.calls.length,0);
});

test("lost committed authority response never starts I/O and never refunds its reservation",async t=>{
  const f=fixture(t);await f.seed();
  const authority=f.env.R2_BUDGET.get();
  const env={...f.env,R2_BUDGET:{idFromName:n=>n,get:()=>({async fetch(request) {await authority.fetch(request);throw new Error("response lost");}})}};
  await assert.rejects(withR2BudgetBatch(env,[put("one",1)],()=>{}),/budget_unavailable/);
  assert.deepEqual((await f.call("status")).usage,{class_a:1,class_b:0,storage_bytes:4097});assert.equal(f.calls.length,0);
});

test("stalled authority preflight is bounded even if its reservation commits after timeout",async t=>{
  const f=fixture(t);await f.seed();let release;
  const held=new Promise(resolve=>{release=resolve;}),authority=f.env.R2_BUDGET.get();
  const pending=[];
  const env={...f.env,R2_BUDGET:{idFromName:n=>n,get:()=>({fetch(request) {
    const promise=held.then(()=>authority.fetch(request));pending.push(promise);return promise;
  }})}};
  await assert.rejects(withR2BudgetBatch(env,[put("one",1)],()=>{throw new Error("callback must not run");},
    {deadline:Date.now()+5}),/batch_expired/);
  release();await Promise.all(pending);
  assert.deepEqual((await f.call("status")).usage,{class_a:1,class_b:0,storage_bytes:4097});assert.equal(f.calls.length,0);
});

test("concurrent same-key grants charge every PUT but only the maximum immutable storage bound",async t=>{
  const f=fixture(t);await f.seed();
  await Promise.all(Array.from({length:20},(_,i)=>withR2BudgetBatch(f.env,[put("one",i+1)],scoped=>scoped.RADAR_ARCHIVE.put("one","x"))));
  assert.deepEqual((await f.call("status")).usage,{class_a:20,class_b:0,storage_bytes:4116});assert.equal(f.calls.length,20);
  assert.deepEqual(f.values.get("object:one"),{bytes:4116,retained:true});
});

test("uncertain PUT consumes its slot and retains the whole plan after TTL and month rollover",async t=>{
  const f=fixture(t);await f.seed();f.raw.before=()=>{throw new Error("PUT outcome unknown");};
  await withR2BudgetBatch(f.env,[put("one",10),head("one")],async scoped=>{
    await assert.rejects(scoped.RADAR_ARCHIVE.put("one","x"),/outcome unknown/);
    await assert.rejects(scoped.RADAR_ARCHIVE.put("one","x"),/not_reserved/);
  });
  f.advance("2026-11-01T00:00:00Z");
  assert.deepEqual((await f.call("status")).usage,{class_a:1,class_b:1,storage_bytes:4106});assert.equal(f.calls.length,1);
});

test("storage warning80 and exact pre-90 cutoff include all batch metadata without partial reservations",async t=>{
  const f=fixture(t,{account_storage_bytes:8_000_000_000-4096});await f.seed();
  await withR2BudgetBatch(f.env,[put("one",0)],()=>{});
  assert.equal((await f.call("status")).notifications.find(n=>n.kind==="warning").storage_bytes,8_000_000_000);
  const g=fixture(t,{account_storage_bytes:9_000_000_000-4097});await g.seed();
  await assert.rejects(withR2BudgetBatch(g.env,[head("one"),put("one",1)],()=>{}),/monthly_budget_paused/);
  const s=await g.call("status");assert.equal(s.pause_reason,"storage_bytes");assert.equal(s.usage.class_a,0);
  assert.equal(s.usage.class_b,0);assert.equal(g.values.has("object:one"),false);assert.equal(g.calls.length,0);
});

test("bounded plans and PUT slots reject excess, foreign keys, streams and paid storage classes",async t=>{
  const f=fixture(t);await f.seed();const before=budgetWrites(f);
  for (const operations of [[],Array.from({length:129},()=>head("one")),new Array(2),
    [put("one",R2_BATCH_LIMITS.putBytes+1)],[put("one",-1)],[{kind:"delete",key:"one"}]]) {
    await assert.rejects(withR2BudgetBatch(f.env,operations,()=>{}));
  }
  assert.equal(budgetWrites(f),before);
  await withR2BudgetBatch(f.env,[put("one",3)],async scoped=>{
    await assert.rejects(scoped.RADAR_ARCHIVE.put("other","x"),/not_reserved/);
    await assert.rejects(scoped.RADAR_ARCHIVE.put("one","xxxx"),/not_reserved/);
    await assert.rejects(scoped.RADAR_ARCHIVE.put("one",new ReadableStream()),/unbounded/);
    await assert.rejects(scoped.RADAR_ARCHIVE.put("one","x",{storageClass:"InfrequentAccess"}),/infrequent/);
    await scoped.RADAR_ARCHIVE.put("one","abc");
  });
  assert.equal(f.calls.length,1);
});

test("history batch API preserves rawrefs and hooks with one budget row plus unique catalog rows",async t=>{
  const f=fixture(t);await f.seed();const before=budgetWrites(f),catalogBefore=catalogRows(f),operations=[];
  const raws=[event("one"),event("two")];
  f.raw.before=()=>{
    const state=f.values.get("budget:v1");assert.equal(state.class_a,2);assert.equal(state.class_b,4);
    assert.equal(state.storage_bytes,[...f.objects.values()].reduce((sum,row)=>sum+row.bytes.length+4096,0)
      + [...f.values.entries()].filter(([key])=>key.startsWith("object:")&&!f.objects.has(key.slice(7))).reduce((sum,[,value])=>sum+value.bytes,0));
  };
  const refs=await archiveHistoryEvents(f.env,raws,{onOperation:(kind,bytes)=>operations.push({kind,bytes})});
  f.raw.before=null;
  assert.equal(budgetWrites(f)-before,1);assert.equal(catalogRows(f)-catalogBefore,2);
  assert.deepEqual(operations.map(op=>op.kind),["read","write","read","read","write","read"]);
  assert.deepEqual(f.calls.map(call=>call.method),["head","put","head","head","put","head"]);
  for (let i=0;i<refs.length;i++) assert.deepEqual(await readHistoryArchive(f.env,refs[i]),raws[i]);
  const replayBefore=budgetWrites(f),replayCatalog=catalogRows(f),nativeBefore=f.calls.length;
  assert.deepEqual(await archiveHistoryEvents(f.env,raws),refs);
  assert.equal(budgetWrites(f)-replayBefore,1);assert.equal(catalogRows(f),replayCatalog);
  assert.deepEqual(f.calls.slice(nativeBefore).map(call=>call.method),["head","head"]);
  assert.deepEqual(await archiveHistoryEvent(f.env,raws[0]),refs[0]);
});

test("runtime batch API round-trips, preserves single API and protects storage through GC",async t=>{
  const f=fixture(t);await f.seed();const before=budgetWrites(f),catalogBefore=catalogRows(f);
  const docs=[{name:"checkpoint:deep",value:{cursor:"a"}},{name:"checkpoint:discovery",value:{cursor:"b"}}];
  const refs=await archiveRuntimeDocuments(guardR2Env(f.env),docs);
  assert.equal(budgetWrites(f)-before,1);assert.equal(catalogRows(f)-catalogBefore,2);
  const held=(await f.call("status")).usage.storage_bytes;
  for (let i=0;i<refs.length;i++) assert.deepEqual(await readRuntimeArchive(f.env,refs[i]),docs[i].value);
  assert.deepEqual(await archiveRuntimeDocument(f.env,docs[0].name,docs[0].value),refs[0]);
  await deleteRuntimeArchive(f.env,refs[0]);assert.equal(f.objects.has(refs[0].key),false);
  assert.equal((await f.call("status")).usage.storage_bytes,held);
});

test("archive batch validation is complete before reservation or native I/O",async t=>{
  const f=fixture(t);await f.seed();const before=budgetWrites(f);
  await assert.rejects(archiveHistoryEvents(f.env,[event("good"),{bad:true}]));
  await assert.rejects(archiveRuntimeDocuments(f.env,[{name:"checkpoint:deep",value:{good:true}},{name:"invalid",value:{}}]));
  await assert.rejects(archiveHistoryEvents(f.env,Array.from({length:43},()=>event("one"))),/batch_too_large/);
  await assert.rejects(archiveRuntimeDocuments(f.env,[]),/batch_too_large/);
  assert.equal(budgetWrites(f),before);assert.equal(f.calls.length,0);
});

test("archive writers and readers cannot bypass missing budget authority on a raw parent environment",async t=>{
  const f=fixture(t);await f.seed();
  const history=await archiveHistoryEvent(f.env,event("one"));
  const runtime=await archiveRuntimeDocument(f.env,"checkpoint:deep",{a:1});
  const env={...f.env,R2_BUDGET:undefined},before=f.calls.length;
  await assert.rejects(archiveHistoryEvent(env,event("two")),/not_configured/);
  await assert.rejects(archiveRuntimeDocument(env,"checkpoint:deep",{a:2}),/not_configured/);
  await assert.rejects(readHistoryArchive(env,history),/not_configured/);
  await assert.rejects(readRuntimeArchive(env,runtime),/not_configured/);
  assert.equal(f.calls.length,before);
});

test("archive preflight denies full plans even when the first HEAD alone would fit",async t=>{
  const f=fixture(t,{class_b:8999998});await f.seed();
  await assert.rejects(archiveHistoryEvent(f.env,event("one")),/monthly_budget_paused/);
  assert.equal(f.calls.length,0);assert.equal((await f.call("status")).usage.class_b,8999998);
});

test("archive crash leaves complete reservation charged and replay recovers immutable bodies without a second PUT",async t=>{
  const f=fixture(t);await f.seed();let heads=0;
  f.raw.before=method=>{if(method==="head"&&++heads===2)throw new Error("confirmation lost");};
  await assert.rejects(archiveHistoryEvent(f.env,event("one")),/archive_unavailable/);
  assert.equal(f.objects.size,1);assert.equal((await f.call("status")).usage.class_b,2);
  const held=(await f.call("status")).usage.storage_bytes;
  f.restart();f.raw.before=null;const ref=await archiveHistoryEvent(f.env,event("one"));
  assert.equal(f.calls.filter(call=>call.method==="put").length,1);
  assert.equal((await f.call("status")).usage.storage_bytes,held);
  assert.equal((await f.call("status")).usage.class_a,2);
  assert.deepEqual(await readHistoryArchive(f.env,ref),event("one"));
});
