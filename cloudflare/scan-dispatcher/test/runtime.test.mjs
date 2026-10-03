import assert from "node:assert/strict";
import test from "node:test";
import {RuntimeSnapshots} from "../src/runtime.js";
import worker, {schedulerBucket, schedulerKindForCron} from "../src/index.js";

function storage() {
  const map = new Map();
  let fail = false;
  return {map, setFail(value) {fail = value;}, async transaction(callback) {
    const staging = new Map(structuredClone([...map]));
    const tx = {async get(key) {return Array.isArray(key) ? new Map(key.filter(k=>staging.has(k)).map(k=>[k, staging.get(k)])) : staging.get(key);},
      async put(values) {assert.ok(Object.keys(values).length <= 128); if (fail) throw new Error("disk failure"); for (const [key,value] of Object.entries(values)) staging.set(key,value);},
      async delete(keys) {for (const key of keys) staging.delete(key);}};
    const result = await callback(tx);
    map.clear(); for (const [key,value] of staging) map.set(key,value);
    return result;
  }};
}
function namespace() {
  const objects = new Map();
  return {objects, idFromName:name=>name, get(name) {
    if (!objects.has(name)) objects.set(name, new RuntimeSnapshots({storage:storage()}));
    return objects.get(name);
  }};
}
const AT = "2026-10-02T23:00:00Z";
async function put(object, value, updated_at=AT, revision=1) {
  return object.fetch(new Request("https://runtime/doc", {method:"POST", body:JSON.stringify({value,updated_at,revision})}));
}

test("durable documents chunk safely, overwrite atomically and reject stale revisions", async () => {
  const state = storage(), object = new RuntimeSnapshots({storage:state});
  const value = {text:"e".repeat(3_000_000)};
  assert.equal((await put(object,value)).status,200);
  assert.deepEqual((await (await object.fetch(new Request("https://runtime/doc"))).json()).document.value,value);
  const stale = await (await put(object,{wrong:true}, "2026-10-01T00:00:00Z",9)).json();
  assert.equal(stale.accepted,false);
  state.setFail(true);
  assert.equal((await put(object,{newer:true}, "2026-10-03T00:00:00Z")).status,400);
  state.setFail(false);
  assert.deepEqual((await (await object.fetch(new Request("https://runtime/doc"))).json()).document.value,value);
  assert.equal((await put(object,{small:true}, AT, 2)).status,200);
  assert.equal(state.map.size,2);
});

test("checkpoints require authentication and do not leak through public routes", async () => {
  const env = {RUNTIME_SNAPSHOTS:namespace(), RADAR_INGEST_SECRET:"secret"};
  const body = {checkpoint:{private:"cursor"},updated_at:AT,revision:1};
  const path = "https://worker/api/runtime/checkpoint?kind=deep";
  assert.equal((await worker.fetch(new Request(path,{method:"POST",body:JSON.stringify(body)}),env,{})).status,401);
  assert.equal((await worker.fetch(new Request(path,{method:"POST",body:JSON.stringify(body),headers:{"x-radar-ingest-secret":"secret"}}),env,{})).status,200);
  assert.equal((await worker.fetch(new Request(path),env,{})).status,401);
  const response = await worker.fetch(new Request(path,{headers:{"x-radar-ingest-secret":"secret"}}),env,{});
  assert.equal((await response.json()).document.value.private,"cursor");
});

test("dashboard and token details survive D1 outage without database reads", async () => {
  let dbReads=0;
  const env = {RUNTIME_SNAPSHOTS:namespace(), RADAR_INGEST_SECRET:"secret",
    RADAR_DB:{prepare() {dbReads++; throw new Error("daily quota exhausted");}}};
  const snapshot = {report:{generated_at:AT,alerts:[],signal_theses:[{token_address:"token",signal_at:AT}]},
    history:[], market:{token:{latest_mcap_usd:100}}, detail_signal_theses:[{token_address:"token",signal_at:AT,cohort:[{owner:"owner"}]}]};
  const posted = await worker.fetch(new Request("https://worker/api/runtime/dashboard",{method:"POST",
    body:JSON.stringify(snapshot),headers:{"x-radar-ingest-secret":"secret"}}),env,{});
  assert.equal(posted.status,200);
  const payload = await (await worker.fetch(new Request("https://worker/api/dashboard"),env,{})).json();
  assert.equal(payload.storage_source,"durable_snapshot");
  assert.equal(payload.detail_signal_theses,undefined);
  assert.equal(payload.report.signal_theses[0].cohort,undefined);
  const detail = await (await worker.fetch(new Request("https://worker/api/dashboard/token?token_key=token"),env,{})).json();
  assert.equal(detail.thesis.cohort[0].owner,"owner");
  const missingKey = await worker.fetch(new Request("https://worker/api/dashboard/token"),env,{});
  assert.equal(missingKey.status,400);
  assert.equal((await missingKey.json()).error,"token_key_required");
  assert.equal(dbReads,0);
});

test("large checkpoint bodies and responses are streamed through the edge worker", async () => {
  const env = {RUNTIME_SNAPSHOTS:namespace(), RADAR_INGEST_SECRET:"secret"};
  const path = "https://worker/api/runtime/checkpoint?kind=deep";
  const checkpoint = {encoded:"x".repeat(3_000_000)};
  const request = new Request(path, {method:"POST", headers:{"x-radar-ingest-secret":"secret"},
    body:JSON.stringify({checkpoint, updated_at:AT, revision:2})});
  request.json = () => { throw new Error("edge must not parse checkpoint bodies"); };
  assert.equal((await worker.fetch(request, env, {})).status,200);
  const object = env.RUNTIME_SNAPSHOTS.get("checkpoint:deep");
  const originalFetch = object.fetch.bind(object);
  object.fetch = async incoming => {
    const response = await originalFetch(incoming);
    response.json = () => { throw new Error("edge must not parse checkpoint responses"); };
    return response;
  };
  const response = await worker.fetch(new Request(path, {headers:{"x-radar-ingest-secret":"secret"}}),env,{});
  assert.equal(response.status,200);
  assert.deepEqual(JSON.parse(await response.text()).document.value,checkpoint);
});

test("targeted cron uses distinct fifteen-minute buckets without replacing the hourly bucket", () => {
  assert.equal(schedulerKindForCron("22,37,52 * * * *"),"targeted");
  assert.notEqual(schedulerBucket("targeted","2026-10-03T00:22:00Z"),schedulerBucket("targeted","2026-10-03T00:37:00Z"));
  assert.equal(schedulerBucket("deep_scan","2026-10-03T00:37:00Z"),"deep_scan:2026-10-03T00:00:00.000Z");
});
