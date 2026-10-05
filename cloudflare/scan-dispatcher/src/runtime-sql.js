import {compactDashboardReport, compactDashboardAlert, dashboardRecordMatchesToken} from "./dashboard-shaping.js";
import {validateBlob, documentReferences} from "./runtime-documents.js";

const encoder = new TextEncoder();
const MAX_BYTES = 8 * 1024 * 1024;
export const runtimeUsesSql = env => env?.RUNTIME_STORAGE_BACKEND === "turso_sql";

function database(env) {
  if (!env.RADAR_DB || env.STORAGE_SQL_BACKEND !== "turso") throw new Error("runtime_sql_not_configured");
  return env.RADAR_DB;
}

function document(row, metadataOnly = false) {
  if (!row) return null;
  const meta = {updated_at:row.updated_at, revision:row.revision, bytes:row.bytes, chunks:0};
  if (row.content_id) Object.assign(meta, {content_id:row.content_id, encoding:row.encoding,
    encoded_bytes:row.encoded_bytes, ...(row.token_key ? {token_key:row.token_key} : {})});
  return metadataOnly ? meta : {...meta, value:JSON.parse(row.payload_json)};
}

export async function readSqlRuntime(env, name, metadataOnly = false) {
  const fields = metadataOnly ? "updated_at,revision,bytes,content_id,encoding,encoded_bytes,token_key" : "*";
  return document(await database(env).prepare(`SELECT ${fields} FROM runtime_sql_documents WHERE name=?1`).bind(name).first(), metadataOnly);
}

export async function availableSqlDashboardParts(env, ids) {
  return availableSqlRuntimeParts(env, "dashboard", ids);
}

export async function availableSqlRuntimeParts(env, root, ids) {
  if (!["dashboard", "checkpoint:deep", "checkpoint:discovery"].includes(root)) throw new Error("invalid_runtime_part_root");
  if (!Array.isArray(ids) || ids.length > 250 || ids.some(id => typeof id !== "string" || !/^[a-f0-9]{64}$/.test(id))) {
    throw new Error("invalid_dashboard_part_probe");
  }
  const unique = [...new Set(ids)];
  if (!unique.length) return [];
  const encoding = root === "dashboard" ? "json-ascii" : "gzip+base64-part";
  const rows = await database(env).prepare(`SELECT content_id,encoded_bytes FROM runtime_sql_documents
    WHERE encoding=?1 AND name IN (${unique.map((_, i) => `?${i + 2}`).join(",")})`)
    .bind(encoding,...unique.map(id => `${root}:blob:${id}`)).all();
  return (rows.results || []).map(row => ({id:row.content_id,bytes:row.encoded_bytes}));
}

async function sha256(value) {
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(value))),
    byte => byte.toString(16).padStart(2, "0")).join("");
}

export async function writeSqlRuntime(env, name, value, updatedAt, revision = 0) {
  if (!/^[a-zA-Z0-9:_-]{1,180}$/.test(name)) throw new Error("invalid_runtime_document");
  const sourceMs = Date.parse(updatedAt);
  if (!Number.isFinite(sourceMs) || sourceMs > Date.now() + 300000) throw new Error("invalid_runtime_timestamp");
  if (!Number.isSafeInteger(revision) || revision < 0) throw new Error("invalid_runtime_revision");
  const blob = name.match(/^(checkpoint:(?:deep|discovery)|dashboard):blob:([a-f0-9]{64})$/);
  if (blob) await validateBlob(value, blob[2]);
  const payload = JSON.stringify(value);
  const bytes = payload ? encoder.encode(payload).byteLength : 0;
  if (!bytes || bytes > MAX_BYTES) throw new Error("runtime_document_exceeds_8mb");
  const digest = await sha256(payload);
  const representationUpgrade = !blob && /^checkpoint:(deep|discovery)$/.test(name)
    && value?.schema_version===2 && /^[a-f0-9]{64}$/.test(value.sha256 || "")
    ? ` OR (excluded.source_ms=runtime_sql_documents.source_ms AND excluded.revision=runtime_sql_documents.revision
       AND json_extract(runtime_sql_documents.payload_json,'$.sha256')=json_extract(excluded.payload_json,'$.sha256')
       AND json_extract(runtime_sql_documents.payload_json,'$.decoded_bytes')=json_extract(excluded.payload_json,'$.decoded_bytes')
       AND json_extract(runtime_sql_documents.payload_json,'$.runtime')=json_extract(excluded.payload_json,'$.runtime'))`
    : "";
  const db = database(env);
  const touchedAt = Date.now();
  let previousLedger;
  if (!blob) {
    const previous = await db.prepare(`SELECT updated_at,revision,bytes,payload_sha256${name === "rpc_ledger" ? ",payload_json" : ""} FROM runtime_sql_documents WHERE name=?1`).bind(name).first();
    if (name === "rpc_ledger") {
      const old = previous ? JSON.parse(previous.payload_json) : {ledger:{}};
      if (value?.version !== 1 || !value.ledger || typeof value.ledger !== "object" || Array.isArray(value.ledger)) {
        throw new Error("invalid_rpc_ledger");
      }
      for (const [period,providers] of Object.entries(value.ledger)) {
        if (!/^\d{4}-\d{2}$/.test(period) || !providers || typeof providers !== "object" || Array.isArray(providers)) throw new Error("invalid_rpc_ledger");
        for (const entry of Object.values(providers)) {
          if (!entry || typeof entry !== "object" || Array.isArray(entry)) throw new Error("invalid_rpc_ledger");
          for (const key of ["estimated_units","attempts","allocated_units","allocations"]) {
            if (!Number.isSafeInteger(entry[key] ?? 0) || (entry[key] ?? 0)<0) throw new Error("invalid_rpc_ledger");
          }
        }
      }
      for (const [period,providers] of Object.entries(old.ledger || {})) {
        for (const [provider,entry] of Object.entries(providers)) {
          const next = value.ledger[period]?.[provider];
          if (!next) throw new Error("rpc_ledger_counter_regression");
          for (const key of ["estimated_units","attempts","allocated_units","allocations"]) {
            if ((next[key] ?? 0) < (entry[key] ?? 0)) throw new Error("rpc_ledger_counter_regression");
          }
        }
      }
      previousLedger = previous?.payload_sha256 || "";
    }
    if (previous?.payload_sha256 === digest) return {ok:true,accepted:true,unchanged:true,
      updated_at:previous.updated_at,revision:previous.revision,bytes:previous.bytes,storage_source:"turso_runtime"};
  }
  const tokenKey = blob && value.encoding === "json-ascii" ? JSON.parse(value.data).token_key : null;
  const refs = blob ? [] : documentReferences(value, name);
  // Check references again in the write transaction, not only before it: a GC
  // or a competing publication must never commit a partial manifest.
  const args = [name, payload, digest, updatedAt, sourceMs, revision, bytes,
    blob?.[2] || null, blob ? value.encoding : null, blob ? value.encoded_bytes : null, tokenKey, touchedAt];
  let refGuard = "1";
  if (name === "rpc_ledger") {
    args.push(previousLedger);
    refGuard = "NOT EXISTS(SELECT 1 FROM runtime_sql_documents WHERE name=?1 AND payload_sha256!=?13)";
  }
  if (refs.length) {
    const tuples = refs.map(ref => {
      const start = args.length + 1;
      args.push(`${name}:blob:${ref.id}`, ref.id, ref.bytes, ref.encoding, ref.token || null);
      return `(?${start},?${start+1},?${start+2},?${start+3},?${start+4})`;
    });
    refGuard = `NOT EXISTS (SELECT 1 FROM (SELECT column1 part,column2 id,column3 bytes,column4 encoding,column5 token
      FROM (VALUES ${tuples.join(",")})) r LEFT JOIN runtime_sql_documents d ON d.name=r.part
      WHERE d.name IS NULL OR d.content_id!=r.id OR d.encoded_bytes!=r.bytes OR d.encoding!=r.encoding
        OR (r.token IS NOT NULL AND (d.token_key IS NULL OR d.token_key!=r.token)))`;
  }
  const write = db.prepare(`INSERT INTO runtime_sql_documents
    (name,payload_json,payload_sha256,updated_at,source_ms,revision,bytes,content_id,encoding,encoded_bytes,token_key,touched_at)
    SELECT ?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12 WHERE ${refGuard}
    ON CONFLICT(name) DO UPDATE SET payload_json=excluded.payload_json,payload_sha256=excluded.payload_sha256,
      updated_at=excluded.updated_at,source_ms=excluded.source_ms,revision=excluded.revision,bytes=excluded.bytes,
      touched_at=excluded.touched_at
    WHERE (runtime_sql_documents.content_id IS NOT NULL AND runtime_sql_documents.payload_sha256=excluded.payload_sha256)
      OR (runtime_sql_documents.content_id IS NULL AND
      (excluded.source_ms>runtime_sql_documents.source_ms OR
       (excluded.source_ms=runtime_sql_documents.source_ms AND excluded.revision>runtime_sql_documents.revision)
       ${representationUpgrade}))`).bind(...args);
  const statements = [write];
  if (!blob) {
    // A restore that fetched the superseded manifest keeps its parts for 48h.
    statements.push(db.prepare(`UPDATE runtime_sql_documents SET touched_at=?3 WHERE name IN
      (SELECT part FROM runtime_sql_references WHERE root=?1) AND EXISTS
      (SELECT 1 FROM runtime_sql_documents WHERE name=?1 AND payload_sha256=?2)`).bind(name, digest, touchedAt));
    statements.push(db.prepare(`DELETE FROM runtime_sql_references WHERE root=?1 AND EXISTS
      (SELECT 1 FROM runtime_sql_documents WHERE name=?1 AND payload_sha256=?2)`).bind(name, digest));
    // One statement rather than one external request per token/part.
    if (refs.length) {
      const refArgs = [name, digest];
      const tuples = refs.map(ref => {refArgs.push(`${name}:blob:${ref.id}`); return `(?${refArgs.length})`;});
      statements.push(db.prepare(`INSERT OR IGNORE INTO runtime_sql_references(root,part)
        SELECT ?1,column1 FROM (VALUES ${tuples.join(",")}) WHERE EXISTS
        (SELECT 1 FROM runtime_sql_documents WHERE name=?1 AND payload_sha256=?2)`).bind(...refArgs));
    }
  }
  const results = await db.batch(statements);
  const current = await db.prepare("SELECT updated_at,revision,bytes,payload_sha256 FROM runtime_sql_documents WHERE name=?1").bind(name).first();
  if (!current) throw new Error("runtime_manifest_part_missing_or_mismatched");
  const accepted = current.payload_sha256 === digest;
  // Missing parts of a newer manifest are errors, not a successful stale ack.
  if (!accepted && (sourceMs > Date.parse(current.updated_at) ||
      (sourceMs === Date.parse(current.updated_at) && revision > current.revision))) {
    throw new Error("runtime_manifest_part_missing_or_mismatched");
  }
  return {ok:true, accepted, ...(accepted ? {unchanged:!(results[0]?.meta?.changes > 0)} : {ignored:"stale_checkpoint"}),
    updated_at:current.updated_at, revision:current.revision, bytes:current.bytes, storage_source:"turso_runtime"};
}

export async function sqlRuntimeResponse(env, request, name) {
  try {
    const url = new URL(request.url);
    if (request.method === "GET") return Response.json({ok:true, document:await readSqlRuntime(env, name, url.searchParams.has("meta"))});
    if (request.method !== "POST") return Response.json({ok:false,error:"GET or POST required"},{status:405});
    const declared = Number(request.headers.get("content-length"));
    if (declared > MAX_BYTES + 8192) throw new Error("runtime_document_exceeds_8mb");
    const reader = request.body?.getReader();
    if (!reader) throw new Error("runtime_document_body_required");
    const chunks = []; let size = 0;
    while (true) {
      const {value,done} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BYTES + 8192) {await reader.cancel();throw new Error("runtime_document_exceeds_8mb");}
      chunks.push(value);
    }
    const joined = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) {joined.set(chunk, offset);offset += chunk.byteLength;}
    const text = new TextDecoder("utf-8", {fatal:true}).decode(joined);
    const payload = JSON.parse(text);
    let value = payload.value;
    let updatedAt = payload.updated_at;
    if (url.searchParams.has("ingest_checkpoint")) value = payload.checkpoint;
    if (url.searchParams.has("ingest_detail")) value = payload.detail;
    if (url.searchParams.has("ingest_dashboard")) {
      value = {...payload,report:compactDashboardReport(payload.report),history:(payload.history || []).map(compactDashboardAlert)};
      delete value.history_ledger; delete value._sync_progress;
      updatedAt = value.report.generated_at;
    }
    return Response.json(await writeSqlRuntime(env, name, value, updatedAt, payload.revision || 0));
  } catch (error) {
    return Response.json({ok:false,error:error.message},{status:error.code?.startsWith("storage_sql_") ? 503 : 400});
  }
}

export async function sqlDashboardResponse(env, request, extra = {}) {
  const url = new URL(request.url);
  const stored = await readSqlRuntime(env, "dashboard");
  if (!stored?.value?.report?.generated_at) return null;
  const snapshot = stored.value;
  const token = url.searchParams.get("token_key");
  if (token) {
    const key = token.replace(/^solana:/, "");
    const ref = snapshot.token_detail_refs?.[token] || snapshot.token_detail_refs?.[key];
    if (ref) {
      const part = await readSqlRuntime(env, `dashboard:blob:${ref.id}`);
      if (part?.content_id !== ref.id) throw new Error("token_detail_unavailable");
      const detail = JSON.parse(part.value.data);
      if (![token, key].includes(detail.token_key)) throw new Error("token_detail_identity_mismatch");
      return Response.json({...detail,ok:true,detail_status:"ready",token_key:token,
        report_source_updated_at:stored.updated_at,storage_source:"turso_runtime"});
    }
    const matches = row => dashboardRecordMatchesToken(row, token);
    const thesis = (snapshot.detail_signal_theses || snapshot.report.signal_theses || []).find(matches);
    const current = (snapshot.detail_current_alerts || snapshot.report.alerts || []).filter(matches);
    const history = (snapshot.detail_history || snapshot.history || []).filter(matches);
    const market = snapshot.market?.[token] || snapshot.market?.[key];
    if (!thesis && !current.length && !history.length && !market) return null;
    return Response.json({ok:true,detail_status:snapshot.runtime_snapshot_stage === "summary" ? "pending" : "ready",
      token_key:token,thesis:thesis || null,current_alerts:current,history,market:market || null,
      wallet_edge:null,report_source_updated_at:stored.updated_at,storage_source:"turso_runtime"});
  }
  const {detail_signal_theses,detail_current_alerts,detail_history,token_detail_refs,...summary} = snapshot;
  summary.report = compactDashboardReport(summary.report);
  summary.history = (summary.history || []).slice(0, Math.max(1, Math.min(250, Number(url.searchParams.get("history_limit")) || 40)));
  const names = ["scan_status","discovery_status","deleted_tokens","history_status"];
  const rows = await database(env).prepare(`SELECT name,payload_json FROM runtime_sql_documents WHERE name IN (?1,?2,?3,?4)`).bind(...names).all();
  for (const row of rows.results || []) summary[row.name] = JSON.parse(row.payload_json);
  return Response.json({...summary,...extra,ok:true,report_source_updated_at:stored.updated_at,storage_source:"turso_runtime"});
}

export async function collectSqlRuntimeGarbage(env, now = Date.now()) {
  // The indexed bounded deletion is atomic with publication and the FK is an
  // additional safeguard. No R2 calls and no legacy DO data are deleted.
  return database(env).prepare(`DELETE FROM runtime_sql_documents WHERE name IN
    (SELECT d.name FROM runtime_sql_documents d WHERE d.content_id IS NOT NULL AND d.touched_at<?1
      AND NOT EXISTS (SELECT 1 FROM runtime_sql_references r WHERE r.part=d.name)
      ORDER BY d.touched_at,d.name LIMIT 128)`).bind(now - 48 * 3600000).run();
}
