import assert from "node:assert/strict";
import {DatabaseSync} from "node:sqlite";
import {readFileSync} from "node:fs";
import test from "node:test";
import {writeSqlRuntime,readSqlRuntime,sqlDashboardResponse,collectSqlRuntimeGarbage,availableSqlDashboardParts} from "../src/runtime-sql.js";
import {runtimeCheckpointResponse,runtimeDocument} from "../src/runtime.js";
import {availableSqlRuntimeParts} from "../src/runtime-sql.js";
import {coldOutboxResponse} from "../src/cold-outbox.js";
import {archiveBackupResponse} from "../src/archive-backup.js";
import {gzipSync} from "node:zlib";
import {createHash} from "node:crypto";

const AT = "2026-10-04T12:00:00Z";
function fixture(t) {
  const sql = new DatabaseSync(":memory:"); t.after(()=>sql.close());
  sql.exec("PRAGMA foreign_keys=ON");
  sql.exec(readFileSync(new URL("../migrations-storage/0002_runtime_sql.sql",import.meta.url),"utf8"));
  let writes = 0;
  const db = {prepare(text) {let args=[];return {bind(...values){args=values;return this;},
    async first(field){const row=sql.prepare(text).get(...args);return field ? row?.[field] : row || null;},
    async all(){return {results:sql.prepare(text).all(...args)};},
    async run(){const result=sql.prepare(text).run(...args);writes+=Number(result.changes);return {meta:{changes:Number(result.changes)}};}};},
    async batch(statements){sql.exec("BEGIN IMMEDIATE");try {const rows=[];for(const stmt of statements)rows.push(await stmt.run());sql.exec("COMMIT");return rows;}
      catch(error){sql.exec("ROLLBACK");throw error;}}};
  const forbidden = {idFromName(){assert.fail("SQL runtime must not call a DO");}};
  return {sql,env:{RADAR_DB:db,STORAGE_SQL_BACKEND:"turso",RUNTIME_STORAGE_BACKEND:"turso_sql",
    RUNTIME_SNAPSHOTS:forbidden,RADAR_ARCHIVE:{get(){assert.fail("SQL runtime must not use R2");}},R2_BUDGET:forbidden},get writes(){return writes;}};
}
async function blob(data,encoding="gzip+base64-part") {
  const sha256=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(data))),n=>n.toString(16).padStart(2,"0")).join("");
  return {encoding,sha256,encoded_bytes:data.length,data};
}
const manifest=part=>({schema_version:2,encoding:"gzip+base64+parts",sha256:"f".repeat(64),decoded_bytes:4,
  encoded_bytes:part.encoded_bytes,parts:[{id:part.sha256,bytes:part.encoded_bytes}]});

test("SQL publication survives unavailable DO/R2 and duplicate retries do not rewrite a root",async t=>{
  const f=fixture(t);
  assert.equal((await runtimeDocument(f.env,"scan_status",{status:"done"},AT)).accepted,true);
  const before=f.writes;
  assert.equal((await runtimeDocument(f.env,"scan_status",{status:"done"},AT)).unchanged,true);
  assert.equal(f.writes,before);
  assert.equal((await runtimeDocument(f.env,"scan_status")).document.value.status,"done");
  assert.equal((await writeSqlRuntime(f.env,"scan_status",{status:"old"},"2026-10-03T12:00:00Z",90)).accepted,false);
});

test("RPC ledger cannot lower usage, drop a provider or discard an older period",async t=>{
  const f=fixture(t);
  const value={version:1,ledger:{"2026-10":{alchemy:{estimated_units:100,attempts:2}}}};
  await writeSqlRuntime(f.env,"rpc_ledger",value,AT);
  for(const next of [{version:1,ledger:{}},
    {version:1,ledger:{"2026-10":{alchemy:{estimated_units:99,attempts:2}}}},
    {version:1,ledger:{"2026-10":{alchemy:{estimated_units:100,attempts:1}}}}]) {
    await assert.rejects(writeSqlRuntime(f.env,"rpc_ledger",next,AT,1),/counter_regression/);
  }
  const next=structuredClone(value);next.ledger['2026-10'].alchemy.estimated_units=150;
  assert.equal((await writeSqlRuntime(f.env,"rpc_ledger",next,AT,1)).accepted,true);
  assert.equal((await readSqlRuntime(f.env,"rpc_ledger")).value.ledger['2026-10'].alchemy.estimated_units,150);
});

test("SQL checkpoint rejects missing or corrupted parts, publishes atomically, and restores",async t=>{
  const f=fixture(t),part=await blob("AAAA");
  await writeSqlRuntime(f.env,"checkpoint:deep",{old:true},AT);
  await assert.rejects(writeSqlRuntime(f.env,"checkpoint:deep",manifest(part),AT,1),/runtime_manifest_part_missing/);
  assert.equal((await readSqlRuntime(f.env,"checkpoint:deep")).value.old,true);
  await writeSqlRuntime(f.env,`checkpoint:deep:blob:${part.sha256}`,part,AT);
  assert.equal((await writeSqlRuntime(f.env,"checkpoint:deep",manifest(part),AT,1)).accepted,true);
  await assert.rejects(writeSqlRuntime(f.env,`checkpoint:deep:blob:${part.sha256}`,{...part,data:"BBBB"},AT),/digest_mismatch/);
  const response=await runtimeCheckpointResponse(f.env,new Request(`https://worker/api/runtime/checkpoint?part=${part.sha256}`),"deep");
  assert.equal((await response.json()).document.value.data,"AAAA");
  assert.equal(f.sql.prepare("SELECT COUNT(*) n FROM runtime_sql_references").get().n,1);
});

test("generation-bound token evidence never becomes a public checkpoint or a different token",async t=>{
  const f=fixture(t),part=await blob(JSON.stringify({token_key:"token",thesis:{cohort:[{owner:"owner"}]}}),"json-ascii");
  await writeSqlRuntime(f.env,`dashboard:blob:${part.sha256}`,part,AT);
  const root={report:{generated_at:AT,alerts:[]},token_detail_refs:{token:{id:part.sha256,bytes:part.encoded_bytes}}};
  await writeSqlRuntime(f.env,"dashboard",root,AT);
  const summary=await (await sqlDashboardResponse(f.env,new Request("https://worker/api/dashboard"),{r2_budget:{paused:true}})).json();
  assert.equal(summary.token_detail_refs,undefined);assert.equal(summary.storage_source,"turso_runtime");
  assert.equal(summary.r2_budget.paused,true);
  const detail=await (await sqlDashboardResponse(f.env,new Request("https://worker/api/dashboard/token?token_key=solana:token"))).json();
  assert.equal(detail.thesis.cohort[0].owner,"owner");assert.equal(detail.report_source_updated_at,AT);
  assert.equal(detail.detail_status,"ready");
  await assert.rejects(writeSqlRuntime(f.env,"dashboard",{...root,token_detail_refs:{wrong:root.token_detail_refs.token}},AT,1),/missing_or_mismatched/);
  assert.equal((await readSqlRuntime(f.env,"dashboard")).value.token_detail_refs.token.id,part.sha256);
});

test("list-first token responses remain pending until the full generation manifest is published",async t=>{
  const f=fixture(t),part=await blob(JSON.stringify({token_key:"token",thesis:{token_address:"token",cohort:[{owner:"owner"}]}}),"json-ascii");
  const root={report:{generated_at:AT,alerts:[],signal_theses:[{token_address:"token"}]},runtime_snapshot_stage:"summary"};
  await writeSqlRuntime(f.env,"dashboard",root,AT,2);
  const request=()=>new Request("https://worker/api/dashboard/token?token_key=token");
  const summary=await (await sqlDashboardResponse(f.env,request())).json();
  assert.equal(summary.detail_status,"pending");
  assert.equal(summary.thesis.cohort,undefined);
  await writeSqlRuntime(f.env,`dashboard:blob:${part.sha256}`,part,AT);
  await writeSqlRuntime(f.env,"dashboard",{...root,runtime_snapshot_stage:"complete",
    token_detail_refs:{token:{id:part.sha256,bytes:part.encoded_bytes}}},AT,3);
  const detail=await (await sqlDashboardResponse(f.env,request())).json();
  assert.equal(detail.detail_status,"ready");
  assert.equal(detail.thesis.cohort[0].owner,"owner");
});

test("bounded dashboard part probe only reads verified dashboard metadata without DO or R2",async t=>{
  const f=fixture(t),part=await blob(JSON.stringify({token_key:"token"}),"json-ascii");
  await writeSqlRuntime(f.env,`dashboard:blob:${part.sha256}`,part,AT);
  const other=await blob("AAAA");
  await writeSqlRuntime(f.env,`checkpoint:deep:blob:${other.sha256}`,other,AT);
  const before=f.writes;
  assert.deepEqual(await availableSqlDashboardParts(f.env,[part.sha256,part.sha256,other.sha256]),
    [{id:part.sha256,bytes:part.encoded_bytes}]);
  assert.equal(f.writes,before);
  assert.deepEqual(await availableSqlDashboardParts(f.env,[]),[]);
  for(const ids of [["bad"],Array(251).fill(part.sha256),null]) {
    await assert.rejects(availableSqlDashboardParts(f.env,ids),/invalid_dashboard_part_probe/);
  }
});

test("bounded garbage collection preserves current references and a superseded restore grace",async t=>{
  const f=fixture(t),a=await blob("AAAA"),b=await blob("BBBB");
  for(const part of [a,b])await writeSqlRuntime(f.env,`checkpoint:deep:blob:${part.sha256}`,part,AT);
  await writeSqlRuntime(f.env,"checkpoint:deep",manifest(a),AT);
  f.sql.prepare("UPDATE runtime_sql_documents SET touched_at=1 WHERE content_id IS NOT NULL").run();
  await writeSqlRuntime(f.env,"checkpoint:deep",manifest(b),AT,1);
  await collectSqlRuntimeGarbage(f.env);
  assert.ok(await readSqlRuntime(f.env,`checkpoint:deep:blob:${a.sha256}`));
  const retired=f.sql.prepare("SELECT touched_at FROM runtime_sql_documents WHERE content_id=?").get(a.sha256).touched_at;
  await collectSqlRuntimeGarbage(f.env,retired+48*3600000+1);
  assert.equal(await readSqlRuntime(f.env,`checkpoint:deep:blob:${a.sha256}`),null);
  assert.ok(await readSqlRuntime(f.env,`checkpoint:deep:blob:${b.sha256}`));
});

test("checkpoint probes read only their own immutable parts",async t=>{
  const f=fixture(t),part=await blob("AAAA");
  await writeSqlRuntime(f.env,`checkpoint:deep:blob:${part.sha256}`,part,AT);
  assert.deepEqual(await availableSqlRuntimeParts(f.env,"checkpoint:deep",[part.sha256]),[{id:part.sha256,bytes:4}]);
  assert.deepEqual(await availableSqlRuntimeParts(f.env,"checkpoint:discovery",[part.sha256]),[]);
  await assert.rejects(availableSqlRuntimeParts(f.env,"outbox",[]),/invalid_runtime_part_root/);
});

function archiveFixture(f) {
  const rows=new Map();
  const metadata=row=>row ? {size:row.data.byteLength,customMetadata:row.meta,etag:"etag"} : null;
  f.env.RADAR_ARCHIVE={
    async put(key,data,options){rows.set(key,{data:Uint8Array.from(data),meta:options.customMetadata});},
    async head(key){return metadata(rows.get(key));},
    async get(key){const row=rows.get(key);return row ? {...metadata(row),body:new Blob([row.data]).stream(),
      arrayBuffer:async()=>Uint8Array.from(row.data).buffer} : null;},
    async list(){return {objects:[...rows].map(([key,row])=>({key,size:row.data.byteLength,etag:"etag"})),truncated:false};}
  };
  return rows;
}

test("cold originals are acknowledged only after R2 confirmation plus SQL and retry never resets progress",async t=>{
  const f=fixture(t),objects=archiveFixture(f),data=gzipSync(JSON.stringify({report:{generated_at:AT}}));
  const id=createHash("sha256").update(data).digest("hex"),url=`https://worker/api/storage/outbox?id=${id}`;
  const post=()=>new Request(url,{method:"POST",body:data,headers:{"x-radar-generated-at":AT}});
  const saved=await (await coldOutboxResponse(f.env,post())).json();
  assert.equal(saved.accepted,true);assert.equal(objects.size,1);
  assert.equal((await readSqlRuntime(f.env,`outbox:${id}`)).value.completed,false);
  await coldOutboxResponse(f.env,new Request(url,{method:"PATCH",body:JSON.stringify({progress:{durable_history_ledger:7}})}));
  const retry=await (await coldOutboxResponse(f.env,post())).json();
  assert.equal(retry.progress.durable_history_ledger,7);assert.equal(retry.unchanged,true);
  const body=await (await coldOutboxResponse(f.env,new Request(url))).arrayBuffer();
  assert.deepEqual(Buffer.from(body),data);
  await coldOutboxResponse(f.env,new Request(url,{method:"PATCH",body:JSON.stringify({completed:true})}));
  assert.equal((await readSqlRuntime(f.env,`outbox:${id}`)).value.completed,true);
});

test("failed cold SQL acknowledgement leaves the source archived but unacknowledged",async t=>{
  const f=fixture(t),objects=archiveFixture(f),data=gzipSync("original");
  const id=createHash("sha256").update(data).digest("hex");
  f.sql.exec("CREATE TRIGGER fail_archive BEFORE INSERT ON runtime_sql_documents BEGIN SELECT RAISE(ABORT,'unavailable'); END");
  await assert.rejects(coldOutboxResponse(f.env,new Request(`https://worker/api/storage/outbox?id=${id}`,
    {method:"POST",body:data,headers:{"x-radar-generated-at":AT}})),/unavailable/);
  assert.equal(objects.size,1);assert.equal(await readSqlRuntime(f.env,`outbox:${id}`),null);
});

test("archive backup credential permits only reads and cannot bypass the R2 guard",async t=>{
  const f=fixture(t),objects=archiveFixture(f);
  objects.set("outbox/v1/source.json.gz",{data:new Uint8Array([1,2,3]),meta:{}});
  f.env.RADAR_ARCHIVE_BACKUP_SECRET="read-only-test";
  const url="https://worker/api/storage/archive-backup";
  assert.equal((await archiveBackupResponse(f.env,new Request(url))).status,403);
  const headers={"x-radar-archive-backup-secret":"read-only-test"};
  assert.equal((await archiveBackupResponse(f.env,new Request(url,{method:"POST",headers}))).status,405);
  const inventory=await (await archiveBackupResponse(f.env,new Request(url,{headers}))).json();
  assert.equal(inventory.objects[0].bytes,3);
  const data=await archiveBackupResponse(f.env,new Request(url+"?key=outbox/v1/source.json.gz",{headers}));
  assert.equal(data.headers.get("x-radar-source-etag"),"etag");
  assert.equal((await data.arrayBuffer()).byteLength,3);
  f.env.R2_BUDGET_GUARD="enabled";
  await assert.rejects(archiveBackupResponse(f.env,new Request(url,{headers})),/R2|DO|r2_budget/);
});

test("bounded archive batch preserves requested order and exact binary bodies",async t=>{
  const f=fixture(t),objects=archiveFixture(f),keys=["history/v1/b","outbox/v1/a"];
  keys.forEach((key,i)=>objects.set(key,{data:new Uint8Array([i,0,255]),meta:{}}));
  f.env.RADAR_ARCHIVE_BACKUP_SECRET="read-only-test";
  const request=()=>new Request("https://worker/api/storage/archive-backup/batch",{method:"POST",
    headers:{"x-radar-archive-backup-secret":"read-only-test"},body:JSON.stringify({keys})});
  const before=f.writes,response=await archiveBackupResponse(f.env,request());
  assert.equal(response.headers.get("x-radar-batch-count"),"2");
  const bytes=new Uint8Array(await response.arrayBuffer());let offset=0;
  for(const [index,key] of keys.entries()) {
    const size=new DataView(bytes.buffer).getUint32(offset);offset+=4;
    const header=JSON.parse(new TextDecoder().decode(bytes.slice(offset,offset+size)));offset+=size;
    assert.deepEqual(header,{key,bytes:3,etag:"etag"});
    assert.deepEqual(bytes.slice(offset,offset+header.bytes),new Uint8Array([index,0,255]));offset+=header.bytes;
  }
  assert.equal(offset,bytes.length);assert.equal(f.writes,before);
  f.env.R2_BUDGET_GUARD="enabled";
  await assert.rejects(archiveBackupResponse(f.env,request()),/R2|DO|r2_budget/);
});

test("archive batch rejects invalid, missing, oversized and incomplete bodies",async t=>{
  const f=fixture(t),objects=archiveFixture(f),key="runtime/v1/a";
  f.env.RADAR_ARCHIVE_BACKUP_SECRET="read-only-test";
  const request=keys=>new Request("https://worker/api/storage/archive-backup/batch",{method:"POST",
    headers:{"x-radar-archive-backup-secret":"read-only-test"},body:JSON.stringify({keys})});
  for(const keys of [[],[key,key],Array.from({length:17},(_,i)=>`runtime/v1/${i}`),["secrets/a"],["outbox/../a"]]) {
    assert.equal((await archiveBackupResponse(f.env,request(keys))).status,400);
  }
  await assert.rejects(archiveBackupResponse(f.env,request([key])),/capacity_or_missing/);
  objects.set(key,{data:new Uint8Array(8*1024*1024),meta:{}});
  assert.equal((await archiveBackupResponse(f.env,request([key]))).status,200);
  objects.set("runtime/v1/b",{data:new Uint8Array(1),meta:{}});
  await assert.rejects(archiveBackupResponse(f.env,request([key,"runtime/v1/b"])),/capacity_or_missing/);
  f.env.RADAR_ARCHIVE.get=async()=>({size:2,etag:"etag",arrayBuffer:async()=>new Uint8Array(1).buffer});
  await assert.rejects(archiveBackupResponse(f.env,request([key])),/body_incomplete/);
});
