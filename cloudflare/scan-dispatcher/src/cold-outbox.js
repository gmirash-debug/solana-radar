import {readSqlRuntime, writeSqlRuntime} from "./runtime-sql.js";
import {withR2BudgetBatch, guardR2Env} from "./r2-budget.js";

const HASH = /^[a-f0-9]{64}$/;
const MAX_BYTES = 16 * 1024 * 1024;
const name = id => `outbox:${id}`;
const key = id => `outbox/v1/sha256/${id.slice(0,2)}/${id}.json.gz`;
const fail = message => { throw new Error(message); };

export async function coldOutboxStatus(env) {
  const row = await env.RADAR_DB.prepare(`SELECT COUNT(*) pending,
    COALESCE(SUM(json_extract(payload_json,'$.archive_ref.bytes')),0) bytes,
    MIN(json_extract(payload_json,'$.source_generated_at')) oldest_source_at
    FROM runtime_sql_documents WHERE name>'outbox:' AND name<'outbox;'
      AND COALESCE(json_extract(payload_json,'$.completed'),0)!=1`).first();
  return {backend:"turso_manifest_r2_original",...row};
}

async function bytes(request) {
  if (Number(request.headers.get("content-length")) > MAX_BYTES) fail("outbox_archive_too_large");
  const reader = request.body?.getReader();
  if (!reader) fail("outbox_archive_body_required");
  const chunks = []; let size = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > MAX_BYTES) fail("outbox_archive_too_large");
      chunks.push(part.value);
    }
  } finally { await reader.cancel().catch(()=>{}); }
  const data = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) {data.set(chunk,offset);offset += chunk.byteLength;}
  return data;
}

function validateReference(ref,id) {
  if (!ref || !HASH.test(id) || ref.sha256 !== id || ref.key !== key(id)
      || !Number.isSafeInteger(ref.bytes) || ref.bytes < 1 || ref.bytes > MAX_BYTES) fail("outbox_archive_reference_invalid");
  return ref;
}

export async function coldOutboxResponse(env,request) {
  const url = new URL(request.url), id = url.searchParams.get("id");
  if (request.method === "GET" && !id) {
    const after = url.searchParams.get("after") || "";
    if (after && !HASH.test(after)) fail("outbox_cursor_invalid");
    const rows = await env.RADAR_DB.prepare(`SELECT name,payload_json FROM runtime_sql_documents
      WHERE name>?1 AND name<'outbox;' ORDER BY name LIMIT 25`).bind(name(after)).all();
    const entries = rows.results.map(row=>({id:row.name.slice(7),...JSON.parse(row.payload_json)}));
    return Response.json({ok:true,entries,next_cursor:entries.at(-1)?.id || after});
  }
  if (!HASH.test(id || "")) fail("outbox_id_invalid");
  const existing = await readSqlRuntime(env,name(id));
  if (request.method === "GET") {
    if (!existing) return Response.json({ok:false,error:"outbox_not_found"},{status:404});
    const ref = validateReference(existing.value.archive_ref,id);
    const object = await guardR2Env(env).RADAR_ARCHIVE.get(ref.key);
    if (!object || object.size !== ref.bytes || object.customMetadata?.sha256 !== id) fail("outbox_archive_missing_or_mismatched");
    return new Response(object.body,{headers:{"content-type":"application/gzip","content-length":String(ref.bytes),"x-radar-sha256":id}});
  }
  if (request.method === "PATCH") {
    if (!existing) return Response.json({ok:false,error:"outbox_not_found"},{status:404});
    // An archive receipt is not an analytics receipt. Completion is explicit,
    // after the replay worker has acknowledged every event (or its quarantine).
    if (Number(request.headers.get("content-length"))>32768) fail("outbox_progress_too_large");
    const payload = await request.json();
    const progress = payload.progress || {};
    if (Object.keys(progress).length>20 || Object.values(progress).some(value=>!Number.isSafeInteger(value) || value<0)) fail("outbox_progress_invalid");
    const merged = {...existing.value.progress};
    for (const [field,value] of Object.entries(progress)) merged[field] = Math.max(merged[field] || 0,value);
    const completed = existing.value.completed || payload.completed === true;
    const result = await writeSqlRuntime(env,name(id),{...existing.value,progress:merged,completed,
      ...(completed ? {completed_at:existing.value.completed_at || new Date().toISOString()} : {})},new Date().toISOString(),existing.revision+1);
    return Response.json(result);
  }
  if (request.method !== "POST") return Response.json({ok:false,error:"GET, POST or PATCH required"},{status:405});
  if (existing) return Response.json({ok:true,accepted:true,unchanged:true,id,...existing.value});
  const sourceAt = request.headers.get("x-radar-generated-at");
  if (!Number.isFinite(Date.parse(sourceAt)) || Date.parse(sourceAt)>Date.now()+300000) fail("outbox_source_timestamp_invalid");
  const data = await bytes(request);
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",data)),n=>n.toString(16).padStart(2,"0")).join("");
  if (digest !== id || data[0] !== 31 || data[1] !== 139) fail("outbox_archive_digest_mismatch");
  const ref = {key:key(id),sha256:id,bytes:data.byteLength};
  await withR2BudgetBatch(env,[{kind:"head",key:ref.key},{kind:"put",key:ref.key,bytes:ref.bytes},{kind:"head",key:ref.key}],async scoped=>{
    let object = await scoped.RADAR_ARCHIVE.head(ref.key);
    if (!object) {
      await scoped.RADAR_ARCHIVE.put(ref.key,data,{customMetadata:{sha256:id},httpMetadata:{contentType:"application/gzip"}});
      object = await scoped.RADAR_ARCHIVE.head(ref.key);
    }
    if (!object || object.size !== ref.bytes || object.customMetadata?.sha256 !== id) fail("outbox_archive_write_unverified");
  });
  const saved = {schema_version:1,archive_ref:ref,source_generated_at:sourceAt,completed:false,progress:{}};
  const result = await writeSqlRuntime(env,name(id),saved,new Date().toISOString());
  if (!result.accepted) fail("outbox_manifest_not_acknowledged");
  return Response.json({...result,id,...saved});
}
