import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {gzipSync, gunzipSync} from "node:zlib";
import test from "node:test";
import {archiveHistoryEvent, readHistoryArchive, validateHistoryArchiveReference,
  historyArchiveEnabled, HISTORY_ARCHIVE_LIMITS} from "../src/archive.js";

const hash = bytes => createHash("sha256").update(bytes).digest("hex");
function event() {
  return {event_id:"archive-1", episode:{episode_id:"episode-1",token_address:"token-1",caught_at:"2026-10-01T00:00:00Z"},
    event:{event_type:"signal",observed_at:"2026-10-01T01:00:00Z"},wallets:[{wallet_address:"a",bought_tokens:10}],
    evidence:{unicode:"\u00e9",null:null,unknown:{x:1,y:true}}};
}
function bucket() {
  const objects = new Map(), calls = [];
  const result = {objects,calls,before:null};
  const metadata = row => ({key:row.key,size:row.bytes.byteLength,customMetadata:structuredClone(row.metadata),
    checksums:{sha256:Uint8Array.from(Buffer.from(row.checksum,"hex")).buffer}});
  result.head = async key => { calls.push({method:"head",key}); await result.before?.("head",key); const row=objects.get(key); return row?metadata(row):null; };
  result.put = async (key,bytes,options) => {
    calls.push({method:"put",key,options}); await result.before?.("put",key);
    assert.equal(options.onlyIf.get("If-None-Match"),"*");
    assert.equal(options.storageClass,"Standard");
    assert.equal(hash(bytes),options.sha256);
    if (objects.has(key)) return null;
    const row={key,bytes:Uint8Array.from(bytes),metadata:structuredClone(options.customMetadata),checksum:hash(bytes)};
    objects.set(key,row); return metadata(row);
  };
  result.get = async key => { calls.push({method:"get",key}); await result.before?.("get",key); const row=objects.get(key);
    return row?{...metadata(row),body:new Blob([row.bytes]).stream()}:null; };
  return result;
}

test("R2 archive is canonical, private, compressed, checksummed and round-trippable", async () => {
  const store=bucket(),env={RADAR_ARCHIVE:store};
  const raw=event(),before=structuredClone(raw);
  const ref=await archiveHistoryEvent(env,raw);
  assert.deepEqual(raw,before);
  assert.equal(ref.schema_version,1); assert.equal(ref.encoding,"gzip+json");
  assert.match(ref.key,/^history\/v1\/sha256\/[a-f0-9]{2}\/[a-f0-9]{64}\.json\.gz$/);
  const row=store.objects.get(ref.key),json=gunzipSync(row.bytes);
  assert.equal(hash(json),ref.sha256); assert.equal(hash(row.bytes),ref.compressed_sha256);
  assert.equal(ref.decoded_bytes,json.byteLength); assert.equal(ref.compressed_bytes,row.bytes.byteLength);
  assert.deepEqual(await readHistoryArchive(env,ref),raw);
  assert.deepEqual(store.calls.map(call=>call.method),["head","put","head","get"]);
  assert.deepEqual(validateHistoryArchiveReference(ref),ref);
  assert.equal(store.calls[1].options.httpMetadata.contentEncoding,"gzip");
});

test("different property order deduplicates an immutable content address", async () => {
  const store=bucket(),env={RADAR_ARCHIVE:store},raw=event();
  const ref=await archiveHistoryEvent(env,raw);
  const reordered={evidence:raw.evidence,wallets:raw.wallets,event:raw.event,episode:raw.episode,event_id:raw.event_id};
  assert.deepEqual(await archiveHistoryEvent(env,reordered),ref);
  assert.equal(store.calls.filter(call=>call.method==="put").length,1);
  const changed=await archiveHistoryEvent(env,{...raw,evidence:{changed:true}});
  assert.notEqual(changed.key,ref.key);
  assert.equal(store.objects.size,2);
});

test("conditionally lost PUT accepts only the verified competing object", async () => {
  const store=bucket(),env={RADAR_ARCHIVE:store},raw=event();
  const originalPut=store.put;
  store.put=async (...args) => {await originalPut(...args); return null;};
  const ref=await archiveHistoryEvent(env,raw);
  assert.deepEqual(await readHistoryArchive(env,ref),raw);
});

test("upload or confirmation failure is 503, and retry reuses the persisted immutable object", async () => {
  const store=bucket(),env={RADAR_ARCHIVE:store};
  store.before=method=>{if(method==="put")throw new Error("service unavailable");};
  await assert.rejects(archiveHistoryEvent(env,event()),error=>error.status===503);
  assert.equal(store.objects.size,0);
  let heads=0;
  store.before=method=>{if(method==="head"&&++heads===2)throw new Error("confirmation unavailable");};
  await assert.rejects(archiveHistoryEvent(env,event()),error=>error.status===503);
  assert.equal(store.objects.size,1);
  store.before=null;
  const before=store.calls.filter(call=>call.method==="put").length;
  const ref=await archiveHistoryEvent(env,event());
  assert.equal(store.calls.filter(call=>call.method==="put").length,before);
  assert.deepEqual(await readHistoryArchive(env,ref),event());
});

test("missing R2, missing objects, corrupt metadata and bodies fail closed", async () => {
  await assert.rejects(archiveHistoryEvent({},event()),/history_archive_not_configured/);
  const store=bucket(),env={RADAR_ARCHIVE:store},ref=await archiveHistoryEvent(env,event());
  await assert.rejects(readHistoryArchive({},ref),/history_archive_not_configured/);
  const row=store.objects.get(ref.key);
  row.bytes[10]^=1;
  await assert.rejects(readHistoryArchive(env,ref),/history_archive_checksum_mismatch/);
  row.bytes[10]^=1; row.metadata.event_id="another";
  await assert.rejects(readHistoryArchive(env,ref),/history_archive_metadata_mismatch/);
  await assert.rejects(archiveHistoryEvent(env,event()),/history_archive_metadata_mismatch/);
  row.metadata.event_id=ref.event_id; row.checksum="0".repeat(64);
  await assert.rejects(readHistoryArchive(env,ref),/checksum_missing_or_mismatched/);
  store.objects.clear();
  await assert.rejects(readHistoryArchive(env,ref),/history_archive_missing/);
});

test("reference schema rejects traversal, foreign identities, versions and unbounded sizes", async () => {
  const store=bucket(),env={RADAR_ARCHIVE:store},ref=await archiveHistoryEvent(env,event());
  const invalid=[{...ref,key:"../private"},{...ref,schema_version:2},{...ref,storage:"url"},
    {...ref,sha256:"0"},{...ref,decoded_bytes:0},{...ref,decoded_bytes:HISTORY_ARCHIVE_LIMITS.eventBytes+1},
    {...ref,compressed_bytes:HISTORY_ARCHIVE_LIMITS.compressedBytes+1},{...ref,event_id:"\u0000"},
    {...ref,observed_at:"bad"},{...ref,episode_id:""}];
  const calls=store.calls.length;
  for(const bad of invalid)await assert.rejects(readHistoryArchive(env,bad));
  assert.equal(store.calls.length,calls);
  await assert.rejects(readHistoryArchive(env,{...ref,event_id:"wrong"}),/metadata_mismatch/);
});

test("decompression bombs and decompressed identity mismatches never produce evidence", async () => {
  const store=bucket(),env={RADAR_ARCHIVE:store},ref=await archiveHistoryEvent(env,event());
  const row=store.objects.get(ref.key);
  row.bytes=Uint8Array.from(gzipSync("x".repeat(ref.decoded_bytes+100000)));
  row.checksum=hash(row.bytes); row.metadata.compressed_sha256=row.checksum; row.metadata.compressed_bytes=String(row.bytes.length);
  const bomb={...ref,compressed_sha256:row.checksum,compressed_bytes:row.bytes.length};
  await assert.rejects(readHistoryArchive(env,bomb),/history_archive_body_oversize/);
  row.bytes=Uint8Array.from(gzipSync(JSON.stringify(event()))); row.checksum=hash(row.bytes);
  // Build a self-consistent metadata/ref for another event ID but retain the original archived JSON.
  const json=Buffer.from(JSON.stringify(event()));
  const wrong={...ref,event_id:"foreign",sha256:hash(json),decoded_bytes:json.length,
    compressed_sha256:row.checksum,compressed_bytes:row.bytes.length};
  wrong.key=`history/v1/sha256/${wrong.sha256.slice(0,2)}/${wrong.sha256}.json.gz`;
  row.key=wrong.key;row.metadata=Object.fromEntries(Object.entries(wrong).map(([key,value])=>[key,String(value)]));
  store.objects.set(wrong.key,row);
  await assert.rejects(readHistoryArchive(env,wrong),/history_archive_identity_mismatch/);
});

test("validation preserves unknown fields while refusing cycles, non-JSON, excessive depth and oversize", async () => {
  const store=bucket(),env={RADAR_ARCHIVE:store};
  const cycle=event();cycle.self=cycle;
  const deep=event();let p=deep;for(let i=0;i<101;i++){p.next={};p=p.next;}
  for(const raw of [cycle,deep,{...event(),bad:Infinity},{...event(),bad:undefined},
    {...event(),bad:"x".repeat(HISTORY_ARCHIVE_LIMITS.eventBytes)}]) await assert.rejects(archiveHistoryEvent(env,raw));
  assert.equal(store.calls.length,0);
  assert.equal(historyArchiveEnabled({HISTORY_ARCHIVE_MODE:"r2"}),true);
  assert.equal(historyArchiveEnabled({}),false);
});

test("archive operations and stalled body reads have bounded time and budget hooks", async () => {
  const store=bucket(),env={RADAR_ARCHIVE:store},operations=[];
  const ref=await archiveHistoryEvent(env,event(),{onOperation:(kind,bytes)=>operations.push({kind,bytes})});
  assert.deepEqual(operations.map(row=>row.kind),["read","write","read"]);
  assert.equal(operations[1].bytes,ref.compressed_bytes);
  store.before=()=>new Promise(()=>{});
  await assert.rejects(archiveHistoryEvent(env,event(),{timeoutMs:10}),/history_archive_timeout/);
  await assert.rejects(archiveHistoryEvent(env,event(),{deadline:Date.now()-1}),/history_archive_timeout/);
  store.before=null;
  const originalGet=store.get;
  let cancelled=false;
  store.get=async key=>({...await originalGet(key),body:new ReadableStream({cancel(){cancelled=true;}})});
  await assert.rejects(readHistoryArchive(env,ref,{timeoutMs:10}),/history_archive_timeout/);
  assert.equal(cancelled,true);
  await assert.rejects(archiveHistoryEvent(env,event(),{onOperation(){throw Object.assign(new Error("budget"),{status:429});}}),/budget/);
});
