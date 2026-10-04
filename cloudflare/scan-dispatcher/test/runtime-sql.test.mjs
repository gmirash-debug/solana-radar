import assert from "node:assert/strict";
import {DatabaseSync} from "node:sqlite";
import {readFileSync} from "node:fs";
import test from "node:test";
import {writeSqlRuntime,readSqlRuntime,sqlDashboardResponse,collectSqlRuntimeGarbage} from "../src/runtime-sql.js";
import {runtimeCheckpointResponse,runtimeDocument} from "../src/runtime.js";

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
  const summary=await (await sqlDashboardResponse(f.env,new Request("https://worker/api/dashboard"))).json();
  assert.equal(summary.token_detail_refs,undefined);assert.equal(summary.storage_source,"turso_runtime");
  const detail=await (await sqlDashboardResponse(f.env,new Request("https://worker/api/dashboard/token?token_key=solana:token"))).json();
  assert.equal(detail.thesis.cohort[0].owner,"owner");assert.equal(detail.report_source_updated_at,AT);
  await assert.rejects(writeSqlRuntime(f.env,"dashboard",{...root,token_detail_refs:{wrong:root.token_detail_refs.token}},AT,1),/missing_or_mismatched/);
  assert.equal((await readSqlRuntime(f.env,"dashboard")).value.token_detail_refs.token.id,part.sha256);
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
