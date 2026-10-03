const HEX = /^[a-f0-9]{64}$/;
export const isContentId = value => typeof value === "string" && HEX.test(value);

export async function validateBlob(value, id) {
  if (!isContentId(id) || value?.sha256 !== id || typeof value.data !== "string"
      || /[^\x00-\x7f]/.test(value.data) || value.data.length !== value.encoded_bytes
      || !value.data.length || !["gzip+base64-part", "json-ascii"].includes(value.encoding)) {
    throw new Error("invalid_runtime_blob");
  }
  const maximum = value.encoding === "gzip+base64-part" ? 1024 * 1024 : 6 * 1024 * 1024;
  if (value.data.length > maximum) throw new Error("runtime_blob_exceeds_capacity");
  if (value.encoding === "gzip+base64-part" && !/^[A-Za-z0-9+/=]+$/.test(value.data)) throw new Error("invalid_checkpoint_part");
  if (value.encoding === "json-ascii") {
    const detail = JSON.parse(value.data);
    if (!detail || typeof detail.token_key !== "string" || !detail.token_key) throw new Error("token_detail_identity_required");
  }
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value.data))),
    byte => byte.toString(16).padStart(2, "0")).join("");
  if (digest !== id) throw new Error("runtime_blob_digest_mismatch");
}

export function documentReferences(value, root) {
  if (root.startsWith("checkpoint:") && value?.schema_version === 2) {
    if (value.encoding !== "gzip+base64+parts" || !Array.isArray(value.parts) || !value.parts.length
        || value.parts.length > 192 || !isContentId(value.sha256)
        || !Number.isInteger(value.decoded_bytes) || value.decoded_bytes < 1 || value.decoded_bytes > 128 * 1024 * 1024) {
      throw new Error("invalid_checkpoint_manifest");
    }
    let total = 0;
    const refs = value.parts.map(part => {
      if (!isContentId(part?.id) || !Number.isInteger(part.bytes) || part.bytes < 1 || part.bytes > 1024 * 1024) {
        throw new Error("invalid_checkpoint_part_reference");
      }
      total += part.bytes;
      return {id:part.id, bytes:part.bytes, encoding:"gzip+base64-part"};
    });
    if (total !== value.encoded_bytes || total > 192 * 1024 * 1024) throw new Error("checkpoint_manifest_length_mismatch");
    return refs;
  }
  if (root === "dashboard" && value?.token_detail_refs) {
    const rows = Object.entries(value.token_detail_refs);
    if (rows.length > 1024) throw new Error("dashboard_detail_capacity_exceeded");
    return rows.map(([token, part]) => {
      if (!token || token.length > 240 || !isContentId(part?.id) || !Number.isInteger(part.bytes)
          || part.bytes < 1 || part.bytes > 6 * 1024 * 1024) throw new Error("invalid_token_detail_reference");
      return {id:part.id, bytes:part.bytes, encoding:"json-ascii", token};
    });
  }
  return [];
}

export async function validateReferences(tx, root, refs) {
  for (let start = 0; start < refs.length; start += 128) {
    const batch = refs.slice(start, start + 128);
    const metadata = await tx.get(batch.map(ref => `${root}:blob:${ref.id}:meta`));
    for (const ref of batch) {
      const meta = metadata.get(`${root}:blob:${ref.id}:meta`);
      if (!meta || meta.content_id !== ref.id || meta.encoded_bytes !== ref.bytes || meta.encoding !== ref.encoding
          || (ref.token && meta.token_key !== ref.token)) throw new Error("runtime_manifest_part_missing_or_mismatched");
    }
  }
}

export async function collectOldBlobs(tx, root, protectedIds, now = Date.now()) {
  if (!tx.list) return;
  const cursorKey = `${root}:gc-cursor`;
  const cursor = await tx.get(cursorKey);
  const entries = await tx.list({prefix:`${root}:blob-index:`, limit:512, ...(cursor ? {startAfter:cursor} : {})});
  let removed = 0;
  let last = null;
  for (const [key, index] of entries) {
    last = key;
    if (protectedIds.has(index.id) || now - index.staged_at < 3600000) continue;
    const name = `${root}:blob:${index.id}`;
    const meta = await tx.get(`${name}:meta`);
    const keys = [key, `${name}:meta`, ...Array.from({length:meta?.chunks || 0}, (_, i) => `${name}:part:${i}`)];
    for (let start = 0; start < keys.length; start += 128) await tx.delete(keys.slice(start, start + 128));
    if (++removed >= 16) break;
  }
  await tx.put({[cursorKey]:entries.size === 512 || removed >= 16 ? last : null});
}
