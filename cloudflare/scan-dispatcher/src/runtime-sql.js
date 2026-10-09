import {compactDashboardReport, compactDashboardAlert, dashboardRecordMatchesToken} from "./dashboard-shaping.js";
import {validateBlob, documentReferences} from "./runtime-documents.js";
import {retirementEnabled, guardRetirementSnapshot, retirementMarkers} from "./token-retirement.js";

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

export async function writeSqlRuntime(env, name, value, updatedAt, revision = 0, options = {}) {
  if (retirementEnabled(env)) {
    if (name === "dashboard") value = await guardRetirementSnapshot(env,value);
    if (/^checkpoint:(deep|discovery)(?::blob:[a-f0-9]{64})?$/.test(name) && options.tokenLifecycleVersion!==1) {
      const fenced=await database(env).prepare("SELECT 1 FROM token_retirements LIMIT 1").first();
      if (fenced) throw new Error("checkpoint_token_retirement_contract_required");
    }
  }
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
       ${representationUpgrade}))${name === "rpc_ledger" ? " RETURNING updated_at,revision,bytes,payload_sha256" : ""}`).bind(...args);
  const statements = [write];
  if (!blob && name !== "rpc_ledger") {
    const retainedParts = refs.map(ref => `${name}:blob:${ref.id}`);
    const retiredOnly = retainedParts.length
      ? ` AND part NOT IN (${retainedParts.map((_,i) => `?${i+4}`).join(",")})` : "";
    // Only retired parts need new grace timestamps. Stable references incur
    // no row writes on metadata-only checkpoint revisions.
    statements.push(db.prepare(`UPDATE runtime_sql_documents SET touched_at=?3 WHERE name IN
      (SELECT part FROM runtime_sql_references WHERE root=?1${retiredOnly}) AND EXISTS
      (SELECT 1 FROM runtime_sql_documents WHERE name=?1 AND payload_sha256=?2)`).bind(name, digest, touchedAt,...retainedParts));
    const removedOnly = retainedParts.length
      ? ` AND part NOT IN (${retainedParts.map((_,i) => `?${i+3}`).join(",")})` : "";
    statements.push(db.prepare(`DELETE FROM runtime_sql_references WHERE root=?1${removedOnly} AND EXISTS
      (SELECT 1 FROM runtime_sql_documents WHERE name=?1 AND payload_sha256=?2)`).bind(name, digest,...retainedParts));
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
  const current = (name === "rpc_ledger" && results[0]?.results?.[0])
    || await db.prepare("SELECT updated_at,revision,bytes,payload_sha256 FROM runtime_sql_documents WHERE name=?1").bind(name).first();
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
    return Response.json(await writeSqlRuntime(env, name, value, updatedAt, payload.revision || 0,
      {tokenLifecycleVersion:payload.token_lifecycle_version}));
  } catch (error) {
    return Response.json({ok:false,error:error.message},{status:error.code?.startsWith("storage_sql_") ? 503 : 400});
  }
}

export async function sqlDashboardResponse(env, request, extra = {}) {
  const url = new URL(request.url);
  const stored = await readSqlRuntime(env, "dashboard");
  if (!stored?.value?.report?.generated_at) return null;
  const snapshot = retirementEnabled(env) ? await guardRetirementSnapshot(env,stored.value) : stored.value;
  const token = url.searchParams.get("token_key");
  if (token) {
    const key = token.replace(/^solana:/, "");
    const marker=(await retirementMarkers(env,[key]))[key];
    if (marker && !marker.reactivated_at) return Response.json({ok:false,error:"token_retired_low_cap",token_key:token},{status:404});
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

export const RUNTIME_RETENTION_LIMITS = Object.freeze({graceMinutes:60, maxBatches:4,
  batchRows:256, maxQueries:32, garbageThresholdBytes:8 * 1024 * 1024, targetBytes:256 * 1024 * 1024});

const GC_BLOB = `(d.content_id IS NOT NULL AND length(d.content_id)=64
  AND d.content_id NOT GLOB '*[^a-f0-9]*' AND d.name IN
  ('dashboard:blob:'||d.content_id,'checkpoint:deep:blob:'||d.content_id,'checkpoint:discovery:blob:'||d.content_id))`;
const GC_UNREFERENCED = "NOT EXISTS (SELECT 1 FROM runtime_sql_references r WHERE r.part=d.name)";
const GC_ELIGIBLE = `${GC_BLOB} AND typeof(d.touched_at)='integer' AND d.touched_at>=0
  AND d.touched_at<?1 AND ${GC_UNREFERENCED}`;

function gcInteger(value, fallback, minimum, maximum) {
  if (value !== undefined && typeof value !== "number"
      && (typeof value !== "string" || !/^\d+$/.test(value))) throw new Error("runtime_gc_options_invalid");
  const parsed = value === undefined ? fallback : Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) throw new Error("runtime_gc_options_invalid");
  return parsed;
}

function gcOptions(env, options) {
  if (!options || typeof options !== "object" || Array.isArray(options)
      || (options.preview !== undefined && typeof options.preview !== "boolean")
      || (options.force !== undefined && typeof options.force !== "boolean")
      || (options.onQuery !== undefined && typeof options.onQuery !== "function")) throw new Error("runtime_gc_options_invalid");
  const limits = RUNTIME_RETENTION_LIMITS;
  const now = gcInteger(options.now, Date.now(), 0, 8_640_000_000_000_000);
  const graceMinutes = gcInteger(options.graceMinutes ?? env.RUNTIME_GC_GRACE_MINUTES, limits.graceMinutes, 60, 2880);
  return {...options, now, graceMinutes, cutoff:now - graceMinutes * 60_000,
    maxBatches:gcInteger(options.maxBatches, limits.maxBatches, 0, limits.maxBatches),
    batchRows:gcInteger(options.batchRows, limits.batchRows, 1, limits.batchRows),
    maxQueries:gcInteger(options.maxQueries, limits.maxQueries, 1, limits.maxQueries),
    garbageThresholdBytes:gcInteger(options.garbageThresholdBytes, 0, 0, Number.MAX_SAFE_INTEGER),
    targetBytes:gcInteger(options.targetBytes, limits.targetBytes, 1, Number.MAX_SAFE_INTEGER)};
}

function gcCount(value) {
  if (!Number.isSafeInteger(value) || value < 0) throw new Error("runtime_gc_result_invalid");
  return value;
}

async function runtimeInventory(db, cutoff, reserve) {
  reserve();
  // Only scalar metadata is read, never the blobs or manifests' payload_json.
  const row = await db.prepare(`SELECT COUNT(*) runtime_rows,COALESCE(SUM(bytes),0) runtime_bytes,
    COALESCE(SUM(CASE WHEN content_id IS NOT NULL AND NOT (${GC_UNREFERENCED}) THEN 1 ELSE 0 END),0) referenced_rows,
    COALESCE(SUM(CASE WHEN content_id IS NOT NULL AND NOT (${GC_UNREFERENCED}) THEN bytes ELSE 0 END),0) referenced_bytes,
    COALESCE(SUM(CASE WHEN ${GC_ELIGIBLE} THEN 1 ELSE 0 END),0) backlog_rows,
    COALESCE(SUM(CASE WHEN ${GC_ELIGIBLE} THEN bytes ELSE 0 END),0) backlog_bytes,
    COALESCE(SUM(CASE WHEN ${GC_BLOB} AND ${GC_UNREFERENCED} AND d.touched_at>=?1 THEN 1 ELSE 0 END),0) grace_rows,
    COALESCE(SUM(CASE WHEN ${GC_BLOB} AND ${GC_UNREFERENCED} AND d.touched_at>=?1 THEN bytes ELSE 0 END),0) grace_bytes
    FROM runtime_sql_documents d`).bind(cutoff).first();
  if (!row) throw new Error("runtime_gc_result_invalid");
  return Object.fromEntries(Object.entries(row).map(([key, value]) => [key, gcCount(value)]));
}

export async function previewSqlRuntimeGarbage(env, options = {}) {
  return collectSqlRuntimeGarbage(env, {...options, preview:true});
}

export async function collectSqlRuntimeGarbage(env, now = Date.now(), options = {}) {
  const result = {ok:true, preview:false, deleted_rows:0, deleted_bytes:0, batches:0, queries:0,
    backlog_rows:null, backlog_bytes:null, runtime_bytes:null, error:null, outcome_unknown:false,
    bytes_basis:"stored_payload_bytes_not_physical", meta:{changes:0}};
  let writeInFlight = false;
  try {
    const config = gcOptions(env, typeof now === "object" ? now : {...options, now});
    Object.assign(result, {preview:Boolean(config.preview), grace_minutes:config.graceMinutes,
      cutoff_ms:config.cutoff, target_bytes:config.targetBytes});
    const db = database(env);
    const reserve = () => {
      if (result.queries >= config.maxQueries) throw new Error("runtime_gc_query_budget");
      config.onQuery?.(); result.queries++;
    };
    let inventory = await runtimeInventory(db, config.cutoff, reserve);
    Object.assign(result, inventory);
    const pressure = inventory.backlog_bytes >= RUNTIME_RETENTION_LIMITS.garbageThresholdBytes
      || inventory.runtime_bytes > config.targetBytes;
    const batches = pressure ? config.maxBatches : Math.min(1, config.maxBatches);
    const rows = pressure ? config.batchRows : Math.min(128, config.batchRows);
    if (!config.preview && inventory.backlog_rows && (config.force
        || inventory.backlog_bytes >= config.garbageThresholdBytes || inventory.runtime_bytes > config.targetBytes)) {
      for (let index = 0; index < batches; index++) {
        // Leave one query for a fresh inventory; never advertise a stale backlog.
        if (result.queries + 3 > config.maxQueries) { result.deferred_reason="runtime_gc_query_budget"; break; }
        reserve();
        const page = await db.prepare(`SELECT d.name,d.bytes,d.touched_at,d.payload_sha256
          FROM runtime_sql_documents d WHERE ${GC_ELIGIBLE}
          ORDER BY d.touched_at,d.name LIMIT ?2`).bind(config.cutoff, rows).all();
        if (page?.success === false || !Array.isArray(page?.results)) throw new Error("runtime_gc_result_invalid");
        if (!page.results.length) break;
        const args = [config.cutoff];
        const tuples = page.results.map(row => {
          const first = args.length + 1;
          args.push(row.name,row.touched_at,row.payload_sha256,row.bytes);
          return `(?${first},?${first+1},?${first+2},?${first+3})`;
        });
        reserve(); writeInFlight=true;
        // Recheck both protection and the selected version in the DELETE itself.
        // Publication and FK RESTRICT fence concurrent ref additions/restaging.
        const removed = await db.prepare(`DELETE FROM runtime_sql_documents AS d WHERE ${GC_ELIGIBLE}
          AND (d.name,d.touched_at,d.payload_sha256,d.bytes) IN (VALUES ${tuples.join(",")})
          RETURNING name,bytes`).bind(...args).all();
        if (removed?.success === false || !Array.isArray(removed?.results)) {
          throw Object.assign(new Error("runtime_gc_result_invalid"), {outcomeUnknown:true});
        }
        let deletedBytes;
        try { deletedBytes = removed.results.reduce((sum, row) => gcCount(sum + gcCount(row.bytes)), 0); }
        catch { throw Object.assign(new Error("runtime_gc_result_invalid"), {outcomeUnknown:true}); }
        writeInFlight=false;
        result.deleted_rows += removed.results.length; result.deleted_bytes += deletedBytes; result.batches++;
        result.backlog_rows=result.backlog_bytes=result.runtime_bytes=null;
        if (!removed.results.length) { result.deferred_reason="runtime_gc_concurrent_protection"; break; }
        if (page.results.length < rows) break;
      }
      inventory = await runtimeInventory(db, config.cutoff, reserve);
      Object.assign(result, inventory);
    } else if (!config.preview && inventory.backlog_rows) result.deferred_reason="runtime_gc_below_threshold";
    result.target_met = result.runtime_bytes <= config.targetBytes;
    result.protected_over_target = !result.target_met && !result.backlog_rows;
    if (!config.preview && result.backlog_rows && !result.deferred_reason) result.deferred_reason="runtime_gc_batch_budget";
  } catch (error) {
    result.ok=false; result.error=String(error?.message || error);
    result.outcome_unknown=writeInFlight && Boolean(error?.outcomeUnknown);
    result.backlog_rows=result.backlog_bytes=result.runtime_bytes=null;
  }
  result.meta.changes=result.deleted_rows;
  return result;
}
