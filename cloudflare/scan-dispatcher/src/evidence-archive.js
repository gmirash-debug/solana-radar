import {readSqlRuntime} from "./runtime-sql.js";
import {withR2BudgetBatch, guardR2Env} from "./r2-budget.js";

const MAX_BYTES = 16 * 1024 * 1024;
const HASH = /^[a-f0-9]{64}$/;
const name = id => `evidence-archive:${id}`;
const key = id => `evidence/v1/sha256/${id.slice(0, 2)}/${id}.json.gz`;

class EvidenceArchiveError extends Error {
  constructor(message, status = 503) { super(message); this.status = status; }
}

function fail(message, status) { throw new EvidenceArchiveError(message, status); }
function validId(id) { return typeof id === "string" && id.length === 64 && HASH.test(id); }

function sourceTimestamp(value) {
  if (typeof value !== "string" || value.length > 64 || /[\x00-\x20]/.test(value)
      || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
      || !Number.isFinite(Date.parse(value)) || Date.parse(value) > Date.now() + 300000) {
    fail("evidence_archive_source_timestamp_invalid", 400);
  }
  return value;
}

function reference(ref, id) {
  if (!ref || !validId(id) || ref.sha256 !== id || ref.key !== key(id)
      || !Number.isSafeInteger(ref.bytes) || ref.bytes < 1 || ref.bytes > MAX_BYTES) {
    fail("evidence_archive_reference_invalid");
  }
  return {key:ref.key, sha256:id, bytes:ref.bytes};
}

function manifest(document, id) {
  if (document?.value?.version !== 1) fail("evidence_archive_manifest_invalid");
  sourceTimestamp(document.value.source_generated_at);
  return reference(document.value.archive_ref, id);
}

function verified(object, ref) {
  if (!object || (object.key !== undefined && object.key !== ref.key)
      || object.size !== ref.bytes || object.customMetadata?.sha256 !== ref.sha256) {
    fail("evidence_archive_missing_or_mismatched");
  }
  if (object.checksums?.sha256) {
    const checksum = hex(object.checksums.sha256);
    if (checksum !== ref.sha256) fail("evidence_archive_missing_or_mismatched");
  }
}

function hex(bytes) {
  return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, "0")).join("");
}
async function digest(bytes) { return hex(await crypto.subtle.digest("SHA-256", bytes)); }

async function requestBytes(request) {
  const length = request.headers.get("content-length");
  if (length !== null && (!/^\d+$/.test(length) || !Number.isSafeInteger(Number(length)))) {
    fail("evidence_archive_length_invalid", 400);
  }
  if (Number(length) > MAX_BYTES) fail("evidence_archive_too_large", 413);
  const reader = request.body?.getReader();
  if (!reader) fail("evidence_archive_body_required", 400);
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > MAX_BYTES) fail("evidence_archive_too_large", 413);
      chunks.push(part.value);
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  if (length !== null && Number(length) !== size) fail("evidence_archive_length_invalid", 400);
  const data = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength; }
  return data;
}

async function publishManifest(env, id, ref, sourceAt) {
  const value = {version:1, archive_ref:ref, source_generated_at:sourceAt};
  const payload = JSON.stringify(value), encoded = new TextEncoder().encode(payload);
  // Runtime roots normally allow newer revisions. Evidence receipts are insert-only,
  // including concurrent writers and retries after an uncertain SQL outcome.
  await env.RADAR_DB.prepare(`INSERT INTO runtime_sql_documents
    (name,payload_json,payload_sha256,updated_at,source_ms,revision,bytes,touched_at)
    VALUES (?1,?2,?3,?4,?5,0,?6,?7) ON CONFLICT(name) DO NOTHING`)
    .bind(name(id), payload, await digest(encoded), sourceAt, Date.parse(sourceAt), encoded.byteLength, Date.now()).run();
  const saved = manifest(await readSqlRuntime(env, name(id)), id);
  if (saved.bytes !== ref.bytes) fail("evidence_archive_manifest_not_acknowledged");
  return saved;
}

async function archiveResponse(env, request) {
  if (!["GET", "POST"].includes(request.method)) {
    return Response.json({ok:false, error:"GET or POST required"}, {status:405, headers:{allow:"GET, POST"}});
  }
  const ids = new URL(request.url).searchParams.getAll("id"), id = ids[0];
  if (ids.length !== 1 || !validId(id)) fail("evidence_archive_id_invalid", 400);
  const sourceAt = request.method === "POST" ? sourceTimestamp(request.headers.get("x-radar-generated-at")) : null;
  const existing = await readSqlRuntime(env, name(id));
  if (request.method === "GET") {
    if (!existing) return Response.json({ok:false, error:"evidence_archive_not_found"}, {status:404});
    const ref = manifest(existing, id);
    const object = await guardR2Env(env).RADAR_ARCHIVE.get(ref.key);
    try {
      verified(object, ref);
      if (!object.body?.getReader) fail("evidence_archive_missing_or_mismatched");
    }
    catch (error) { await object?.body?.cancel().catch(() => {}); throw error; }
    return new Response(object.body, {headers:{"content-type":"application/gzip",
      "content-length":String(ref.bytes), "x-radar-sha256":id, "cache-control":"private, no-store"}});
  }
  const data = await requestBytes(request);
  if (data[0] !== 31 || data[1] !== 139 || data[2] !== 8 || await digest(data) !== id) {
    fail("evidence_archive_digest_or_gzip_invalid", 400);
  }
  // A committed receipt avoids any R2 reservation or retry, even while R2 is paused.
  if (existing) {
    const saved = manifest(existing, id);
    if (saved.bytes !== data.byteLength) fail("evidence_archive_manifest_not_acknowledged");
    return Response.json({ok:true, accepted:true, id, archive_ref:saved});
  }
  const ref = {key:key(id), sha256:id, bytes:data.byteLength};
  await withR2BudgetBatch(env, [{kind:"head", key:ref.key},
    {kind:"put", key:ref.key, bytes:ref.bytes}, {kind:"head", key:ref.key}], async scoped => {
    let object = await scoped.RADAR_ARCHIVE.head(ref.key);
    if (!object) {
      await scoped.RADAR_ARCHIVE.put(ref.key, data, {onlyIf:new Headers({"If-None-Match":"*"}),
        sha256:id, storageClass:"Standard", customMetadata:{sha256:id},
        httpMetadata:{contentType:"application/gzip"}});
      object = await scoped.RADAR_ARCHIVE.head(ref.key);
    }
    verified(object, ref);
  });
  return Response.json({ok:true, accepted:true, id, archive_ref:await publishManifest(env, id, ref, sourceAt)});
}

export async function evidenceArchiveResponse(env, request) {
  try { return await archiveResponse(env, request); }
  catch (error) {
    const known = error instanceof EvidenceArchiveError;
    return Response.json({ok:false, error:known ? error.message : "evidence_archive_unavailable"},
      {status:known ? error.status : 503});
  }
}
