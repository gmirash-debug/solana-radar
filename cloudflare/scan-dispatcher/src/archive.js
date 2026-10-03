import { historyEventId } from "./history.js";

export const HISTORY_ARCHIVE_LIMITS = Object.freeze({
  eventBytes: 128 * 1024, compressedBytes: 129 * 1024, timeoutMs: 10000, depth: 100,
});
const ENCODER = new TextEncoder();
const HASH = /^[a-f0-9]{64}$/;
const FIELDS = ["schema_version", "storage", "encoding", "key", "sha256", "compressed_sha256",
  "decoded_bytes", "compressed_bytes", "event_id", "episode_id", "token_address",
  "observed_at", "event_type", "caught_at"];

export class HistoryArchiveError extends Error {
  constructor(message, status = 503) { super(message); this.status = status; }
}

export function historyArchiveEnabled(env) { return env?.HISTORY_ARCHIVE_MODE === "r2"; }

function account(json, budget) {
  // Character count is a cheap lower bound; the final UTF-8 check enforces the exact byte ceiling.
  budget.characters += json.length;
  if (budget.characters > HISTORY_ARCHIVE_LIMITS.eventBytes) throw new HistoryArchiveError("history_event_oversize", 413);
  return json;
}

function canonical(value, seen = new Set(), depth = 0, budget = {characters: 0}) {
  if (depth > HISTORY_ARCHIVE_LIMITS.depth) throw new HistoryArchiveError("history_archive_json_depth", 400);
  if (typeof value === "string" && value.length > HISTORY_ARCHIVE_LIMITS.eventBytes) {
    throw new HistoryArchiveError("history_event_oversize", 413);
  }
  if (value === null || typeof value === "string" || typeof value === "boolean"
      || (typeof value === "number" && Number.isFinite(value))) return account(JSON.stringify(value), budget);
  if (!value || typeof value !== "object" || seen.has(value)
      || (!Array.isArray(value) && Object.getPrototypeOf(value) !== Object.prototype)) {
    throw new HistoryArchiveError("history_archive_invalid_json", 400);
  }
  seen.add(value);
  account("[]", budget);
  const output = Array.isArray(value)
    ? `[${Array.from(value, (item, index) => {
      if (index) account(",", budget);
      return canonical(item, seen, depth + 1, budget);
    }).join(",")}]`
    : `{${Object.keys(value).sort().map((key, index) => {
      if (index) account(",", budget);
      const prefix = account(`${JSON.stringify(key)}:`, budget);
      return `${prefix}${canonical(value[key], seen, depth + 1, budget)}`;
    }).join(",")}}`;
  seen.delete(value);
  return output;
}

function timestamp(value) {
  return typeof value === "string" && /T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? Date.parse(value) : NaN;
}

function identity(raw) {
  const event_id = historyEventId(raw);
  const episode = raw?.episode;
  const event = raw?.event;
  if (!episode || !event || typeof episode.episode_id !== "string" || !episode.episode_id.trim()
      || episode.episode_id.length > 240 || typeof episode.token_address !== "string" || !episode.token_address.trim()
      || episode.token_address.length > 240 || typeof event.event_type !== "string" || !event.event_type.trim()
      || event.event_type.length > 120 || episode.caught_at?.length > 64 || event.observed_at?.length > 64
      || !Number.isFinite(timestamp(episode.caught_at)) || !Number.isFinite(timestamp(event.observed_at))
      || timestamp(event.observed_at) < timestamp(episode.caught_at)
      || !event_id || event_id.length > 240 || /[\u0000-\u001f]/.test(event_id)
      || (raw.wallets !== undefined && !Array.isArray(raw.wallets))) {
    throw new HistoryArchiveError("history_archive_event_identity_invalid", 400);
  }
  return {event_id, episode_id: episode.episode_id, token_address: episode.token_address,
    observed_at: event.observed_at, event_type: event.event_type, caught_at: episode.caught_at};
}

function archiveKey(sha256) { return `history/v1/sha256/${sha256.slice(0, 2)}/${sha256}.json.gz`; }

export function validateHistoryArchiveReference(ref) {
  if (!ref || typeof ref !== "object" || Array.isArray(ref)
      || ref.schema_version !== 1 || ref.storage !== "r2" || ref.encoding !== "gzip+json"
      || !HASH.test(ref.sha256) || !HASH.test(ref.compressed_sha256) || ref.key !== archiveKey(ref.sha256)
      || !Number.isInteger(ref.decoded_bytes) || ref.decoded_bytes < 1 || ref.decoded_bytes > HISTORY_ARCHIVE_LIMITS.eventBytes
      || !Number.isInteger(ref.compressed_bytes) || ref.compressed_bytes < 1 || ref.compressed_bytes > HISTORY_ARCHIVE_LIMITS.compressedBytes) {
    throw new HistoryArchiveError("history_archive_reference_invalid");
  }
  const checked = identity({event_id: ref.event_id,
    episode: {episode_id: ref.episode_id, token_address: ref.token_address, caught_at: ref.caught_at},
    event: {event_type: ref.event_type, observed_at: ref.observed_at}});
  if (checked.event_id !== ref.event_id) throw new HistoryArchiveError("history_archive_reference_invalid");
  const normalized = Object.fromEntries(FIELDS.map(key => [key, ref[key]]));
  if (ENCODER.encode(JSON.stringify(normalized)).byteLength > 2000) throw new HistoryArchiveError("history_archive_reference_oversize");
  return normalized;
}

function bucket(env) {
  if (!env?.RADAR_ARCHIVE || ["head", "get", "put"].some(method => typeof env.RADAR_ARCHIVE[method] !== "function")) {
    throw new HistoryArchiveError("history_archive_not_configured");
  }
  return env.RADAR_ARCHIVE;
}

async function bounded(action, options) {
  const timeout = Math.min(HISTORY_ARCHIVE_LIMITS.timeoutMs, options?.timeoutMs ?? HISTORY_ARCHIVE_LIMITS.timeoutMs,
    options?.deadline === undefined ? Infinity : options.deadline - Date.now());
  if (!Number.isFinite(timeout) || timeout < 1) throw new HistoryArchiveError("history_archive_timeout");
  let timer;
  try {
    return await Promise.race([Promise.resolve().then(action), new Promise((_, reject) => {
      timer = setTimeout(() => reject(new HistoryArchiveError("history_archive_timeout")), timeout);
    })]);
  } catch (error) {
    if (error instanceof HistoryArchiveError) throw error;
    throw new HistoryArchiveError("history_archive_unavailable");
  } finally { clearTimeout(timer); }
}

async function collect(stream, maximum, options = {}) {
  const reader = stream.getReader();
  const chunks = [];
  let length = 0;
  const limits = {...options, deadline: Math.min(options.deadline ?? Infinity,
    Date.now() + (options.timeoutMs ?? HISTORY_ARCHIVE_LIMITS.timeoutMs))};
  try {
    while (true) {
      const {done, value} = await bounded(() => reader.read(), limits);
      if (done) break;
      length += value.byteLength;
      if (length > maximum) throw new HistoryArchiveError("history_archive_body_oversize");
      chunks.push(value);
    }
  } catch (error) { reader.cancel().catch(() => {}); throw error; }
  finally { reader.releaseLock(); }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return bytes;
}

async function digest(bytes) {
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
}

function metadata(ref) { return Object.fromEntries(FIELDS.map(key => [key, String(ref[key])])); }

function verifiedObject(object, expected) {
  if (!object || object.key !== expected.key) throw new HistoryArchiveError("history_archive_missing");
  const values = object.customMetadata || {};
  const ref = validateHistoryArchiveReference({...values, schema_version: Number(values.schema_version),
    decoded_bytes: Number(values.decoded_bytes), compressed_bytes: Number(values.compressed_bytes)});
  for (const field of FIELDS) {
    if (expected[field] !== undefined && ref[field] !== expected[field]) throw new HistoryArchiveError("history_archive_metadata_mismatch");
  }
  if (object.size !== ref.compressed_bytes) throw new HistoryArchiveError("history_archive_size_mismatch");
  // R2 verifies this checksum during PUT; customMetadata alone is not proof of a persisted body.
  const checksum = object.checksums?.sha256;
  if (!checksum || Array.from(new Uint8Array(checksum), byte => byte.toString(16).padStart(2, "0")).join("") !== ref.compressed_sha256) {
    throw new HistoryArchiveError("history_archive_checksum_missing_or_mismatched");
  }
  return ref;
}

async function operation(options, kind, bytes = 0) {
  if (options?.deadline !== undefined && options.deadline <= Date.now()) throw new HistoryArchiveError("history_archive_timeout");
  await options?.onOperation?.(kind, bytes);
}

export async function archiveHistoryEvent(env, raw, options = {}) {
  const store = bucket(env);
  const ids = identity(raw);
  const bytes = ENCODER.encode(canonical(raw));
  if (bytes.byteLength > HISTORY_ARCHIVE_LIMITS.eventBytes) throw new HistoryArchiveError("history_event_oversize", 413);
  const sha256 = await digest(bytes);
  const expected = {key: archiveKey(sha256), sha256, decoded_bytes: bytes.byteLength, ...ids};
  await operation(options, "read");
  const existing = await bounded(() => store.head(expected.key), options);
  if (existing) return verifiedObject(existing, expected);
  const compressed = await collect(new Blob([bytes]).stream().pipeThrough(new CompressionStream("gzip")),
    HISTORY_ARCHIVE_LIMITS.compressedBytes, options);
  const ref = {schema_version: 1, storage: "r2", encoding: "gzip+json", ...expected,
    compressed_sha256: await digest(compressed), compressed_bytes: compressed.byteLength};
  validateHistoryArchiveReference(ref);
  await operation(options, "write", compressed.byteLength);
  await bounded(() => store.put(ref.key, compressed, {
    onlyIf: new Headers({"If-None-Match": "*"}), sha256: ref.compressed_sha256, storageClass: "Standard",
    httpMetadata: {contentType: "application/json", contentEncoding: "gzip"}, customMetadata: metadata(ref),
  }), options);
  await operation(options, "read");
  // Another writer may win the conditional PUT with a different gzip stream of the same canonical JSON.
  return verifiedObject(await bounded(() => store.head(ref.key), options), expected);
}

export async function readHistoryArchive(env, reference, options = {}) {
  const ref = validateHistoryArchiveReference(reference);
  const store = bucket(env);
  await operation(options, "read");
  const object = await bounded(() => store.get(ref.key), options);
  try { verifiedObject(object, ref); }
  catch (error) { object?.body?.cancel().catch(() => {}); throw error; }
  if (!object.body || typeof object.body.getReader !== "function") throw new HistoryArchiveError("history_archive_body_missing");
  const compressed = await collect(object.body, ref.compressed_bytes, options);
  if (compressed.byteLength !== ref.compressed_bytes || await digest(compressed) !== ref.compressed_sha256) {
    throw new HistoryArchiveError("history_archive_checksum_mismatch");
  }
  const decoded = await collect(new Blob([compressed]).stream().pipeThrough(new DecompressionStream("gzip")), ref.decoded_bytes, options);
  if (decoded.byteLength !== ref.decoded_bytes || await digest(decoded) !== ref.sha256) {
    throw new HistoryArchiveError("history_archive_checksum_mismatch");
  }
  let raw;
  try { raw = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(decoded)); }
  catch { throw new HistoryArchiveError("history_archive_invalid_json"); }
  const ids = identity(raw);
  for (const [key, value] of Object.entries(ids)) {
    if (value !== ref[key]) throw new HistoryArchiveError("history_archive_identity_mismatch");
  }
  if (canonical(raw) !== new TextDecoder().decode(decoded)) throw new HistoryArchiveError("history_archive_not_canonical");
  return raw;
}
