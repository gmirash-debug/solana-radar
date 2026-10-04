import {R2_BATCH_LIMITS, guardR2Env, withR2BudgetBatch} from "./r2-budget.js";

const MAX_BYTES = 8 * 1024 * 1024;
const HEX = /^[a-f0-9]{64}$/;
const ENCODER = new TextEncoder();
const TIMEOUT_MS = 10000;

function deadline(options = {}) {
  return Math.min(options.deadline ?? Infinity, Date.now() + Math.min(TIMEOUT_MS, options.timeoutMs ?? TIMEOUT_MS));
}

async function timed(action, until) {
  const remaining = until - Date.now();
  if (!Number.isFinite(remaining) || remaining < 1) throw new Error("runtime_archive_timeout");
  let timer;
  try {
    return await Promise.race([Promise.resolve().then(action), new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("runtime_archive_timeout")), remaining);
    })]);
  } finally { clearTimeout(timer); }
}

function hex(bytes) { return Array.from(new Uint8Array(bytes), value => value.toString(16).padStart(2, "0")).join(""); }

async function digest(bytes) {
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
    value => value.toString(16).padStart(2, "0")).join("");
}

async function bounded(stream, maximum, until) {
  if (!stream?.getReader) throw new Error("runtime_archive_body_missing");
  const reader = stream.getReader(), parts = [];
  let size = 0;
  try {
    while (true) {
      const {value, done} = await timed(() => reader.read(), until);
      if (done) break;
      size += value.byteLength;
      if (size > maximum) throw new Error("runtime_archive_size_exceeded");
      parts.push(value);
    }
  } catch (error) { reader.cancel().catch(() => {}); throw error; }
  finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) { bytes.set(part, offset); offset += part.byteLength; }
  return bytes;
}

export function runtimeArchiveEnabled(env) {
  if (env.RUNTIME_ARCHIVE_MODE === undefined || env.RUNTIME_ARCHIVE_MODE === "durable") return false;
  if (env.RUNTIME_ARCHIVE_MODE !== "r2") throw new Error("runtime_archive_mode_invalid");
  if (!env.RADAR_ARCHIVE?.put || !env.RADAR_ARCHIVE?.get || !env.RADAR_ARCHIVE?.head) {
    throw new Error("runtime_archive_not_configured");
  }
  return true;
}

export function shouldArchiveRuntime(env, name, value) {
  return runtimeArchiveEnabled(env) && (name.includes(":blob:")
    || (name.startsWith("checkpoint:") && value?.encoding !== "gzip+base64+parts"));
}

function metadataMatches(object, ref) {
  return object && object.key === ref.key && object.size === ref.compressed_bytes
    && object.customMetadata?.sha256 === ref.sha256
    && object.customMetadata?.decoded_bytes === String(ref.decoded_bytes)
    && object.customMetadata?.compressed_bytes === String(ref.compressed_bytes)
    && object.customMetadata?.compressed_sha256 === ref.compressed_sha256
    && object.customMetadata?.kind === "runtime" && object.customMetadata?.schema_version === "1"
    && object.checksums?.sha256 && hex(object.checksums.sha256) === ref.compressed_sha256;
}

async function prepareRuntimeDocument(name, value, until) {
  if (!/^(?:dashboard|checkpoint:(?:deep|discovery))(?::blob:[a-f0-9]{64})?$/.test(name)) {
    throw new Error("runtime_archive_name_invalid");
  }
  const bytes = ENCODER.encode(JSON.stringify(value));
  if (!bytes.byteLength || bytes.byteLength > MAX_BYTES) throw new Error("runtime_archive_size_exceeded");
  const sha256 = await digest(bytes);
  const decoded_bytes = bytes.byteLength;
  const key = `runtime/${name.replaceAll(":", "/")}/${sha256}.json.gz`;
  const fromObject = object => {
    const ref = {version:1, key, sha256, decoded_bytes,
      compressed_bytes:object?.size, compressed_sha256:object?.customMetadata?.compressed_sha256};
    validateRuntimeArchiveReference(ref);
    if (!metadataMatches(object, ref)) throw new Error("runtime_archive_write_unverified");
    return ref;
  };
  const compressed = await bounded(new Blob([bytes]).stream().pipeThrough(new CompressionStream("gzip")), MAX_BYTES + 65536, until);
  const compressed_sha256 = await digest(compressed);
  return {key, compressed, compressed_sha256, sha256, decoded_bytes, fromObject};
}

async function storeRuntimeDocument(store, {key, compressed, compressed_sha256, sha256, decoded_bytes, fromObject}, until) {
  const existing = await timed(() => store.head(key), until);
  if (existing) return fromObject(existing);
  await timed(() => store.put(key, compressed, {
      onlyIf:new Headers({"If-None-Match":"*"}), sha256:compressed_sha256, storageClass:"Standard",
      httpMetadata:{contentType:"application/gzip"},
      customMetadata:{sha256, compressed_sha256, compressed_bytes:String(compressed.byteLength),
        decoded_bytes:String(decoded_bytes), kind:"runtime", schema_version:"1"},
    }), until);
  return fromObject(await timed(() => store.head(key), until));
}

export async function archiveRuntimeDocuments(env, documents, options = {}) {
  if (!runtimeArchiveEnabled(env)) throw new Error("runtime_archive_disabled");
  if (!Array.isArray(documents) || !documents.length || documents.length * 3 > R2_BATCH_LIMITS.operations) {
    throw new Error("runtime_archive_batch_too_large");
  }
  const until = deadline(options), prepared = [];
  let putBytes = 0, decodedBytes = 0;
  for (const doc of documents) {
    const item = await prepareRuntimeDocument(doc?.name,doc?.value,until);
    putBytes += item.compressed.byteLength; decodedBytes += item.decoded_bytes;
    if (Math.max(putBytes,decodedBytes) > R2_BATCH_LIMITS.putBytes) throw new Error("runtime_archive_batch_bytes_exceeded");
    prepared.push(item);
  }
  const plan = prepared.flatMap(({key,compressed})=>[{kind:"head",key},
    {kind:"put",key,bytes:compressed.byteLength},{kind:"head",key}]);
  return withR2BudgetBatch(env,plan,async scoped => {
    const refs = [];
    for (const item of prepared) refs.push(await storeRuntimeDocument(scoped.RADAR_ARCHIVE,item,until));
    return refs;
  }, {deadline:until});
}

export async function archiveRuntimeDocument(env, name, value, options = {}) {
  return (await archiveRuntimeDocuments(env,[{name,value}],options))[0];
}

export function validateRuntimeArchiveReference(ref) {
  if (ref?.version !== 1 || !HEX.test(ref.sha256 || "") || typeof ref.key !== "string"
    || !/^runtime\/(?:dashboard|checkpoint\/(?:deep|discovery))\/(?:blob\/[a-f0-9]{64}\/)?[a-f0-9]{64}\.json\.gz$/.test(ref.key)
    || !ref.key.endsWith(`/${ref.sha256}.json.gz`)
    || !HEX.test(ref.compressed_sha256 || "")
    || !Number.isInteger(ref.decoded_bytes) || ref.decoded_bytes < 1 || ref.decoded_bytes > MAX_BYTES
    || !Number.isInteger(ref.compressed_bytes) || ref.compressed_bytes < 1 || ref.compressed_bytes > MAX_BYTES + 65536) {
    throw new Error("runtime_archive_reference_invalid");
  }
  return ref;
}

export async function readRuntimeArchive(env, ref, options = {}) {
  // A rollback stops new archive writes, not reads of already published parts.
  if (!env.RADAR_ARCHIVE?.get) throw new Error("runtime_archive_not_configured");
  validateRuntimeArchiveReference(ref);
  const until = deadline(options);
  const object = await timed(() => guardR2Env(env).RADAR_ARCHIVE.get(ref.key), until);
  if (!metadataMatches(object, ref)) {
    object?.body?.cancel().catch(() => {});
    throw new Error("runtime_archive_missing_or_mismatched");
  }
  const compressed = await bounded(object.body, ref.compressed_bytes, until);
  if (compressed.byteLength !== ref.compressed_bytes) throw new Error("runtime_archive_length_mismatch");
  if (await digest(compressed) !== ref.compressed_sha256) throw new Error("runtime_archive_compressed_digest_mismatch");
  const bytes = await bounded(new Blob([compressed]).stream().pipeThrough(new DecompressionStream("gzip")), ref.decoded_bytes, until);
  if (bytes.byteLength !== ref.decoded_bytes || await digest(bytes) !== ref.sha256) throw new Error("runtime_archive_digest_mismatch");
  return JSON.parse(new TextDecoder("utf-8", {fatal:true}).decode(bytes));
}

export async function deleteRuntimeArchive(env, ref, options = {}) {
  validateRuntimeArchiveReference(ref);
  if (typeof env.RADAR_ARCHIVE?.delete !== "function") throw new Error("runtime_archive_not_configured");
  await timed(() => guardR2Env(env).RADAR_ARCHIVE.delete(ref.key), deadline(options));
}
