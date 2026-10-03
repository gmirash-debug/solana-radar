import test from "node:test";
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {archiveRuntimeDocument, readRuntimeArchive, deleteRuntimeArchive} from "../src/runtime-archive.js";
import {RuntimeSnapshots} from "../src/runtime.js";

function bucket() {
  const objects = new Map();
  return {objects, writes:0, deletes:[], fail:false, before:null,
    async head(key) {await this.before?.("head",key); const row = objects.get(key); return row ? {...row, body:undefined} : null; },
    async put(key, value, options) {
      if (this.fail) throw new Error("R2 unavailable");
      await this.before?.("put",key);
      assert.equal(options.onlyIf.get("If-None-Match"),"*");
      assert.equal(options.sha256,createHash("sha256").update(value).digest("hex"));
      if (!objects.has(key)) {
        this.writes++;
        objects.set(key,{key,bytes:Uint8Array.from(value), size:value.byteLength, customMetadata:options.customMetadata,
          checksums:{sha256:Uint8Array.from(Buffer.from(options.sha256,"hex")).buffer}});
      }
      return this.head(key);
    },
    async get(key) {await this.before?.("get",key); const row = objects.get(key); return row ? {...row, body:new Blob([row.bytes]).stream()} : null; },
    async delete(key) {await this.before?.("delete",key);if(this.fail)throw new Error("delete unavailable");this.deletes.push(key);objects.delete(key);},
  };
}
function storage() {
  const values = new Map();
  return {values, async transaction(callback) {
    const staged = new Map(structuredClone([...values]));
    const result = await callback({async get(key) {
      return Array.isArray(key) ? new Map(key.filter(k=>staged.has(k)).map(k=>[k,staged.get(k)])) : staged.get(key);
    }, async put(rows) {for (const [key,value] of Object.entries(rows)) staged.set(key,value);},
    async delete(keys) {for (const key of keys) staged.delete(key);},
    async list({prefix,limit=512,startAfter}) {return new Map([...staged].filter(([k])=>k.startsWith(prefix) && (!startAfter || k>startAfter))
      .sort(([a],[b])=>a.localeCompare(b)).slice(0,limit));}});
    values.clear(); for (const [key,value] of staged) values.set(key,value);
    return result;
  }};
}
const AT = "2026-10-03T00:00:00Z";
const envFor = archive => ({RADAR_ARCHIVE:archive, RUNTIME_ARCHIVE_MODE:"r2"});

test("runtime archives compress, round-trip and deduplicate immutable blobs", async () => {
  const archive=bucket(), env=envFor(archive), value={data:"x".repeat(1_000_000)};
  const a=await archiveRuntimeDocument(env,"checkpoint:deep",value);
  const b=await archiveRuntimeDocument(env,"checkpoint:deep",value);
  assert.deepEqual(a,b); assert.equal(archive.writes,1);
  assert.ok(a.compressed_bytes<a.decoded_bytes/100);
  assert.deepEqual(await readRuntimeArchive(env,a),value);
  await assert.rejects(readRuntimeArchive(env,{...a,key:"https://attacker.invalid"}),/reference_invalid/);
  archive.objects.get(a.key).bytes=new Uint8Array(a.compressed_bytes);
  await assert.rejects(readRuntimeArchive(env,a));
});

test("R2 checkpoint stores only a pointer in DO; failed next write retains old checkpoint", async () => {
  const archive=bucket(), state=storage(), object=new RuntimeSnapshots({storage:state},envFor(archive));
  const post=value=>object.fetch(new Request("https://runtime/checkpoint:deep",{method:"POST",
    body:JSON.stringify({value,updated_at:AT})}));
  const value={data:"x".repeat(2_000_000)};
  assert.equal((await post(value)).status,200);
  assert.equal([...state.values.keys()].filter(key=>key.includes(":part:")).length,0);
  assert.equal(state.values.get("checkpoint:deep:meta").chunks,0);
  assert.deepEqual((await (await object.fetch(new Request("https://runtime/checkpoint:deep"))).json()).document.value,value);
  archive.fail=true;
  assert.equal((await post({data:"new"})).status,400);
  assert.deepEqual((await (await object.fetch(new Request("https://runtime/checkpoint:deep"))).json()).document.value,value);
});

test("inline legacy checkpoint remains readable and migrates only after R2 verification", async () => {
  const archive=bucket(), state=storage(), object=new RuntimeSnapshots({storage:state});
  const post=value=>object.fetch(new Request("https://runtime/checkpoint:deep",{method:"POST",
    body:JSON.stringify({value,updated_at:AT})}));
  assert.equal((await post({legacy:true})).status,200);
  object.env=envFor(archive); archive.fail=true;
  assert.equal((await post({next:true})).status,400);
  assert.equal((await (await object.fetch(new Request("https://runtime/checkpoint:deep"))).json()).document.value.legacy,true);
  archive.fail=false; assert.equal((await post({next:true})).status,200);
  assert.equal(state.values.has("checkpoint:deep:part:0"),false);
});

test("token evidence validates content identity and serves generation-bound detail from private R2", async () => {
  const archive=bucket(), state=storage(), object=new RuntimeSnapshots({storage:state},envFor(archive));
  const data=JSON.stringify({token_key:"mint",thesis:{cohort_wallets:[{owner:"w"}]},history:[]});
  const sha256=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(data))),x=>x.toString(16).padStart(2,"0")).join("");
  const post=(name,value)=>object.fetch(new Request(`https://runtime/${name}`,{method:"POST",body:JSON.stringify({value,updated_at:AT})}));
  assert.equal((await post(`dashboard:blob:${sha256}`,{encoding:"json-ascii",sha256,encoded_bytes:data.length,data})).status,200);
  assert.equal((await post("dashboard",{report:{generated_at:AT},token_detail_refs:{mint:{id:sha256,bytes:data.length}}})).status,200);
  const result=await (await object.fetch(new Request("https://runtime/dashboard?projection=public&token_key=mint"))).json();
  assert.equal(result.thesis.cohort_wallets[0].owner,"w");
  assert.equal(result.report_source_updated_at,AT);
  assert.equal(state.values.get(`dashboard:blob:${sha256}:meta`).chunks,0);
});

const sha = value => createHash("sha256").update(value).digest("hex");
async function blobFixture(t) {
  let clock = Date.parse("2026-10-03T12:00:00Z");
  const original=Date.now;Date.now=()=>clock;t.after(()=>{Date.now=original;});
  const archive=bucket(),state=storage(),object=new RuntimeSnapshots({storage:state},envFor(archive));
  const post=(name,value)=>object.fetch(new Request(`https://runtime/${name}`,{method:"POST",
    body:JSON.stringify({value,updated_at:AT})}));
  const blob=data=>({schema_version:1,encoding:"gzip+base64-part",sha256:sha(data),encoded_bytes:data.length,data});
  const manifest=part=>({schema_version:2,encoding:"gzip+base64+parts",sha256:"f".repeat(64),
    decoded_bytes:4,encoded_bytes:part.encoded_bytes,parts:[{id:part.sha256,bytes:part.encoded_bytes}]});
  const a=blob("AAAA"),b=blob("BBBB");
  assert.equal((await post(`checkpoint:deep:blob:${a.sha256}`,a)).status,200);
  assert.equal((await post("checkpoint:deep",manifest(a))).status,200);
  const aRef=state.values.get(`checkpoint:deep:blob:${a.sha256}:meta`).archive_ref;
  return {archive,state,object,a,b,aRef,post,manifest,advance:ms=>{clock+=ms;}};
}

test("native compressed checksum is required before replacing a DO checkpoint", async () => {
  const archive=bucket(),state=storage(),object=new RuntimeSnapshots({storage:state},envFor(archive));
  const post=value=>object.fetch(new Request("https://runtime/checkpoint:deep",{method:"POST",body:JSON.stringify({value,updated_at:AT})}));
  assert.equal((await post({old:true})).status,200);
  const before=structuredClone(state.values.get("checkpoint:deep:meta"));
  archive.before=(method,key)=>{
    if(method==="head"&&archive.objects.has(key)&&key!==before.archive_ref.key)archive.objects.get(key).checksums={};
  };
  assert.equal((await post({next:true})).status,400);
  assert.deepEqual(state.values.get("checkpoint:deep:meta"),before);
  assert.equal((await (await object.fetch(new Request("https://runtime/checkpoint:deep"))).json()).document.value.old,true);
  archive.before=null;
  archive.objects.get(before.archive_ref.key).checksums.sha256=new Uint8Array(32).buffer;
  await assert.rejects(readRuntimeArchive(envFor(archive),before.archive_ref),/missing_or_mismatched/);
});

test("retired archived blob is removed only after its supersession grace; current root and part survive", async t => {
  const f=await blobFixture(t);
  f.advance(7200000);
  assert.equal((await f.post(`checkpoint:deep:blob:${f.b.sha256}`,f.b)).status,200);
  assert.equal((await f.post("checkpoint:deep",f.manifest(f.b))).status,200);
  assert.equal(f.archive.objects.has(f.aRef.key),true);
  f.advance(3599999);await f.post("checkpoint:deep",f.manifest(f.b));
  assert.equal(f.archive.objects.has(f.aRef.key),true);
  f.advance(2);await f.post("checkpoint:deep",f.manifest(f.b));
  assert.equal(f.archive.objects.has(f.aRef.key),false);
  assert.equal(f.state.values.has(`checkpoint:deep:blob:${f.a.sha256}:meta`),false);
  assert.equal(f.state.values.has(`checkpoint:deep:archive-gc:${f.a.sha256}`),false);
  assert.deepEqual((await (await f.object.fetch(new Request("https://runtime/checkpoint:deep"))).json()).document.value,f.manifest(f.b));
  const current=f.state.values.get(`checkpoint:deep:blob:${f.b.sha256}:meta`).archive_ref;
  assert.equal(f.archive.objects.has(current.key),true);
});

test("R2 deletion failure retains its durable marker and retries without losing the current manifest", async t => {
  const f=await blobFixture(t);
  await f.post(`checkpoint:deep:blob:${f.b.sha256}`,f.b);await f.post("checkpoint:deep",f.manifest(f.b));
  f.advance(3600001);
  f.archive.before=method=>{if(method==="delete")throw new Error("delete unavailable");};
  assert.equal((await f.post("checkpoint:deep",f.manifest(f.b))).status,200);
  assert.equal(f.state.values.has(`checkpoint:deep:archive-gc:${f.a.sha256}`),true);
  assert.equal(f.archive.objects.has(f.aRef.key),true);
  f.archive.before=null;await f.post("checkpoint:deep",f.manifest(f.b));
  assert.equal(f.state.values.has(`checkpoint:deep:archive-gc:${f.a.sha256}`),false);
  assert.equal(f.archive.objects.has(f.aRef.key),false);
});

test("restaged current and still pending blobs are protected even if an old GC marker exists", async t => {
  const f=await blobFixture(t);
  await f.post(`checkpoint:deep:blob:${f.b.sha256}`,f.b);await f.post("checkpoint:deep",f.manifest(f.b));
  f.advance(3600001);await f.post("checkpoint:deep",f.manifest(f.b));
  await f.post(`checkpoint:deep:blob:${f.a.sha256}`,f.a);
  f.state.values.set(`checkpoint:deep:archive-gc:${f.a.sha256}`,{id:f.a.sha256,archive_ref:f.aRef});
  await f.post("checkpoint:deep",f.manifest(f.a));
  const deletes=f.archive.deletes.length;
  assert.equal(f.archive.objects.has(f.aRef.key),true);
  assert.equal(f.state.values.has(`checkpoint:deep:archive-gc:${f.a.sha256}`),false);
  // B was just superseded; a manifest replay must not reclaim it before its grace.
  await f.post("checkpoint:deep",f.manifest(f.a));
  assert.equal(f.archive.deletes.length,deletes);
});

test("rollback disables new archive writes, not reads of committed refs or legacy client manifests", async t => {
  const f=await blobFixture(t);
  f.object.env.RUNTIME_ARCHIVE_MODE="durable";
  const writes=f.archive.writes;
  assert.deepEqual((await (await f.object.fetch(new Request(`https://runtime/checkpoint:deep:blob:${f.a.sha256}`))).json()).document.value,f.a);
  assert.deepEqual(await readRuntimeArchive(f.object.env,f.aRef),f.a);
  await assert.rejects(archiveRuntimeDocument(f.object.env,"checkpoint:deep",{new:true}),/disabled/);
  assert.equal(f.archive.writes,writes);
});

test("superseded inline checkpoint roots are reclaimed by committed index only, with grace and rollback protection", async t => {
  const f=await blobFixture(t);
  await f.post("checkpoint:discovery",{cursor:"old"});
  const old=f.state.values.get("checkpoint:discovery:meta").archive_ref;
  await f.post("checkpoint:discovery",{cursor:"new"});
  assert.equal(f.state.values.has(`checkpoint:discovery:archive-root-gc:${old.sha256}`),true);
  f.advance(3599999);await f.post("checkpoint:discovery",{cursor:"new"});
  assert.equal(f.archive.objects.has(old.key),true);
  // Re-publishing a retired object protects the current root, even once the marker matures.
  await f.post("checkpoint:discovery",{cursor:"old"});f.advance(2);
  await f.post("checkpoint:discovery",{cursor:"old"});
  assert.equal(f.archive.objects.has(old.key),true);
  assert.equal(f.state.values.has(`checkpoint:discovery:archive-root-gc:${old.sha256}`),false);
  const current=f.state.values.get("checkpoint:discovery:meta").archive_ref;
  const next=[...f.archive.objects.keys()].find(key=>key.startsWith("runtime/checkpoint/discovery/")&&key!==current.key);
  f.advance(3600001);await f.post("checkpoint:discovery",{cursor:"old"});
  assert.equal(f.archive.objects.has(next),false);
  assert.equal(f.archive.objects.has(current.key),true);
});

test("R2 calls and stalled body reads/deletes time out; digest and byte bounds reject corruption", async () => {
  const archive=bucket(),env=envFor(archive),ref=await archiveRuntimeDocument(env,"checkpoint:deep",{a:"x".repeat(1000)});
  archive.before=()=>new Promise(()=>{});
  await assert.rejects(archiveRuntimeDocument(env,"checkpoint:deep",{next:true},{timeoutMs:10}),/timeout/);
  await assert.rejects(readRuntimeArchive(env,ref,{timeoutMs:10}),/timeout/);
  await assert.rejects(deleteRuntimeArchive(env,ref,{timeoutMs:10}),/timeout/);
  archive.before=null;
  const originalGet=archive.get.bind(archive);let cancelled=false;
  archive.get=async key=>({...await originalGet(key),body:new ReadableStream({cancel(){cancelled=true;}})});
  await assert.rejects(readRuntimeArchive(env,ref,{timeoutMs:10}),/timeout/);assert.equal(cancelled,true);
  archive.get=originalGet;
  const row=archive.objects.get(ref.key);row.bytes[10]^=1;
  await assert.rejects(readRuntimeArchive(env,ref),/compressed_digest_mismatch/);
  await assert.rejects(readRuntimeArchive(env,{...ref,decoded_bytes:99999999}),/reference_invalid/);
});
