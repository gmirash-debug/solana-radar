"""Compact durable recovery contract; caches remain reconstructible, evidence does not."""
import base64
import copy
import gzip
import hashlib
import io
import json
import struct
import zlib

MAX_ENCODED_BYTES = 192 * 1024 * 1024
MAX_DECODED_BYTES = 128 * 1024 * 1024
INLINE_BYTES = 256 * 1024
PART_BYTES = 256 * 1024
MAX_PARTS = MAX_ENCODED_BYTES // PART_BYTES
RECORD_BYTES = 180 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_PATH_BYTES = 4096
MEMBER_LAYOUT = "aligned-gzip-members-v1"
REBUILDABLE_KEYS = {"wallet_cache", "social_cache", "gmgn_cache", "enrichment_cache"}


def _json_bytes(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True,
                      sort_keys=True, allow_nan=False).encode("ascii")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _is_digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _size(value, maximum, minimum=1):
    return type(value) is int and minimum <= value <= maximum


def _manifest_digest(payload):
    return _digest(_json_bytes({key: value for key, value in payload.items() if key != "manifest_sha256"}))


def _fragments(durable):
    yield [], "open", b"{"
    for position, (key, value) in enumerate(sorted(durable.items())):
        prefix = (b"," if position else b"") + _json_bytes(key) + b":"
        if not isinstance(value, dict) or key == "_runtime":
            yield [key], "value", prefix + _json_bytes(value)
            continue
        yield [key], "open", prefix + b"{"
        group, entries = None, []
        for index, (child, entry) in enumerate(sorted(value.items())):
            raw = (b"," if index else b"") + _json_bytes(child) + b":" + _json_bytes(entry)
            # Fixed lexical buckets keep both pools and discovery maps below
            # the reference cap while limiting changes to the affected bucket.
            bucket = child[:1]
            if entries and bucket != group:
                yield [key, group], "group", b"".join(entries)
                entries = []
            group = bucket
            entries.append(raw)
        if entries:
            yield [key, group], "group", b"".join(entries)
        yield [key], "close", b"}"
    yield [], "close", b"}"


def _member(raw, path, role, index):
    compressed = gzip.compress(raw, mtime=0)
    extra = _json_bytes({"path": path, "role": role, "index": index})
    # FEXTRA padding changes no decoded bytes and eliminates base64 '=' between members.
    extra += b"\0" * (-(len(compressed) + 2 + len(extra)) % 3)
    header = compressed[:3] + bytes([compressed[3] | 4]) + compressed[4:9] + b"\xff"
    aligned = header + struct.pack("<H", len(extra)) + extra + compressed[10:]
    return base64.b64encode(aligned).decode("ascii")


def build_checkpoint(state):
    """Keep schema 1 JSON identity, staging independent members for schema 2 transport."""
    durable = {key: value for key, value in state.items() if key not in REBUILDABLE_KEYS}
    raw = _json_bytes(durable)
    if len(raw) > MAX_DECODED_BYTES:
        raise ValueError("runtime checkpoint exceeds decoded safety limit")
    durable = json.loads(raw)
    runtime = durable.get("_runtime", {})
    if not isinstance(runtime, dict):
        raise ValueError("checkpoint runtime metadata must be a mapping")
    parts, refs, inline, members = [], [], [], []
    encoded_bytes, decoded_bytes = 0, 0
    content_digest = hashlib.sha256()
    for path, role, fragment in _fragments(durable):
        if len(_json_bytes(path)) > MAX_PATH_BYTES:
            raise ValueError("checkpoint record path exceeds safety limit")
        content_digest.update(fragment)
        decoded_bytes += len(fragment)
        metadata = path == ["_runtime"]
        if metadata and len(fragment) > INLINE_BYTES:
            raise ValueError("checkpoint metadata exceeds inline safety limit; evidence was not truncated")
        width = len(fragment) if metadata else RECORD_BYTES
        for index, start in enumerate(range(0, len(fragment), width)):
            chunk = fragment[start:start + width]
            encoded = _member(chunk, path, role, index)
            encoded_bytes += len(encoded)
            if len(encoded) > PART_BYTES or encoded_bytes > MAX_ENCODED_BYTES:
                raise ValueError("runtime checkpoint exceeds durable capacity; evidence was not truncated")
            members.append(encoded)
            digest = _digest(encoded.encode("ascii"))
            ref = {"id": digest, "bytes": len(encoded), "path": path, "role": role,
                   "index": index, "decoded_bytes": len(chunk), "raw_sha256": _digest(chunk)}
            if metadata:
                inline.append({**ref, "before": len(parts), "data": encoded})
            else:
                if len(parts) >= MAX_PARTS:
                    raise ValueError("checkpoint part capacity exceeded; evidence was not truncated")
                refs.append(ref)
                parts.append({"schema_version": 1, "encoding": "gzip+base64-part",
                              "sha256": digest, "encoded_bytes": len(encoded), "data": encoded})
    if decoded_bytes != len(raw) or content_digest.hexdigest() != _digest(raw):
        raise ValueError("checkpoint member layout changed canonical JSON")
    payload = {"schema_version": 1, "encoding": "gzip+base64", "sha256": _digest(raw),
               "decoded_bytes": len(raw), "data": "".join(members), "runtime": runtime,
               "_parts": parts, "_part_refs": refs, "_inline_members": inline}
    # Validate the root budget before the caller can stage any immutable blobs.
    checkpoint_documents(payload)
    return payload


def decode_checkpoint(payload):
    if payload.get("schema_version") != 1 or payload.get("encoding") != "gzip+base64":
        raise ValueError("unsupported runtime checkpoint")
    encoded = payload.get("data", "")
    if len(encoded) > MAX_ENCODED_BYTES:
        raise ValueError("encoded checkpoint exceeds safety limit")
    with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(encoded, validate=True))) as stream:
        raw = stream.read(MAX_DECODED_BYTES + 1)
    if len(raw) > MAX_DECODED_BYTES or len(raw) != payload.get("decoded_bytes"):
        raise ValueError("invalid checkpoint length")
    if _digest(raw) != payload.get("sha256"):
        raise ValueError("checkpoint digest mismatch")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("checkpoint must be a mapping")
    return result


def _validate_aligned_manifest(payload):
    if (payload.get("schema_version") != 2 or payload.get("encoding") != "gzip+base64+parts"
            or payload.get("part_layout") != MEMBER_LAYOUT or not _is_digest(payload.get("sha256"))
            or not _is_digest(payload.get("manifest_sha256"))
            or not _size(payload.get("decoded_bytes"), MAX_DECODED_BYTES)
            or not _size(payload.get("encoded_bytes"), MAX_ENCODED_BYTES)
            or not isinstance(payload.get("runtime"), dict)
            or len(_json_bytes(payload)) > MAX_MANIFEST_BYTES
            or _manifest_digest(payload) != payload["manifest_sha256"]):
        raise ValueError("checkpoint manifest integrity mismatch")
    refs, inline = payload.get("parts"), payload.get("inline_members")
    if (not isinstance(refs, list) or not 1 <= len(refs) <= MAX_PARTS
            or not isinstance(inline, list) or len(inline) > 1):
        raise ValueError("invalid checkpoint member manifest")
    total, decoded, seen, identities, previous, next_index = 0, 0, set(), set(), None, 0
    for ref in [*refs, *inline]:
        is_inline = "before" in ref if isinstance(ref, dict) else False
        path = ref.get("path") if isinstance(ref, dict) else None
        if (not isinstance(ref, dict) or not isinstance(path, list) or len(path) > 2
                or any(not isinstance(key, str) for key in path) or len(_json_bytes(path)) > MAX_PATH_BYTES
                or ref.get("role") not in ("open", "close", "value", "entry", "group")
                or not _size(ref.get("index"), MAX_PARTS, 0)
                or not _is_digest(ref.get("id")) or ref["id"] in seen
                or not _is_digest(ref.get("raw_sha256")) or not _size(ref.get("bytes"), PART_BYTES)
                or ref["bytes"] % 4 or not _size(ref.get("decoded_bytes"), INLINE_BYTES if is_inline else RECORD_BYTES)):
            raise ValueError("invalid or duplicate checkpoint member reference")
        seen.add(ref["id"])
        decoded += ref["decoded_bytes"]
        if is_inline:
            if (ref not in inline or path != ["_runtime"] or ref["role"] != "value" or ref["index"] != 0
                    or not _size(ref["before"], len(refs), 0)):
                raise ValueError("invalid inline checkpoint metadata")
        else:
            if path == ["_runtime"] or "data" in ref or ref in inline:
                raise ValueError("checkpoint metadata must be inline")
            identity = (tuple(path), ref["role"])
            if identity != previous:
                if identity in identities:
                    raise ValueError("reordered checkpoint members")
                identities.add(identity)
                previous, next_index = identity, 0
            if ref["index"] != next_index:
                raise ValueError("reordered checkpoint members")
            next_index += 1
            total += ref["bytes"]
    if (total != payload["encoded_bytes"] or decoded != payload["decoded_bytes"]
            or total + sum(ref["bytes"] for ref in inline) > MAX_ENCODED_BYTES):
        raise ValueError("checkpoint manifest length mismatch")


def _read_member(encoded, ref):
    if (not isinstance(encoded, str) or not encoded.isascii() or "=" in encoded
            or len(encoded) != ref["bytes"] or _digest(encoded.encode("ascii")) != ref["id"]):
        raise ValueError("checkpoint member integrity mismatch")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        if len(compressed) < 20 or len(compressed) % 3 or compressed[:4] != b"\x1f\x8b\x08\x04":
            raise ValueError("checkpoint member alignment mismatch")
        extra_size = struct.unpack("<H", compressed[10:12])[0]
        extra = _json_bytes({"path": ref["path"], "role": ref["role"], "index": ref["index"]})
        padding = extra_size - len(extra)
        if not 0 <= padding <= 2 or compressed[12:12 + extra_size] != extra + b"\0" * padding:
            raise ValueError("checkpoint member identity mismatch")
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(ref["decoded_bytes"] + 1)
    except (ValueError, OSError, EOFError, zlib.error) as exc:
        raise ValueError("invalid compressed checkpoint member") from exc
    if len(raw) != ref["decoded_bytes"] or _digest(raw) != ref["raw_sha256"]:
        raise ValueError("checkpoint member decoded integrity mismatch")
    return raw


def checkpoint_documents(payload):
    """Stage immutable pieces before replacing the last complete manifest."""
    if "_parts" in payload:
        parts, refs, inline = payload["_parts"], payload["_part_refs"], payload["_inline_members"]
        manifest = {key: payload[key] for key in ("sha256", "decoded_bytes", "runtime")}
        manifest.update(schema_version=2, encoding="gzip+base64+parts", part_layout=MEMBER_LAYOUT,
                        encoded_bytes=sum(ref["bytes"] for ref in refs), parts=refs, inline_members=inline)
        manifest["manifest_sha256"] = _manifest_digest(manifest)
        _validate_aligned_manifest(manifest)
        if len(parts) != len(refs):
            raise ValueError("missing checkpoint member")
        for part, ref in zip(parts, refs):
            _part_data(part, ref, aligned=True)
        return manifest, parts
    encoded = payload["data"]
    if len(encoded) <= INLINE_BYTES:
        return payload, []
    parts = []
    for start in range(0, len(encoded), PART_BYTES):
        data = encoded[start:start + PART_BYTES]
        parts.append({"schema_version": 1, "encoding": "gzip+base64-part",
                      "sha256": _digest(data.encode("ascii")), "encoded_bytes": len(data), "data": data})
    manifest = {key: value for key, value in payload.items() if key != "data"}
    manifest.update(schema_version=2, encoding="gzip+base64+parts", encoded_bytes=len(encoded),
                    parts=[{"id": part["sha256"], "bytes": part["encoded_bytes"]} for part in parts])
    return manifest, parts


def _part_data(part, ref, aligned=False):
    if not isinstance(part, dict):
        raise ValueError("missing checkpoint part")
    encoded = part.get("data", "")
    if (part.get("encoding") != "gzip+base64-part" or not isinstance(encoded, str) or not encoded.isascii()
            or not 0 < len(encoded) <= (PART_BYTES if aligned else 1024 * 1024)
            or len(encoded) != ref.get("bytes") or part.get("encoded_bytes") != len(encoded)
            or _digest(encoded.encode("ascii")) != ref["id"]
            or (aligned and (part.get("schema_version") != 1 or part.get("sha256") != ref["id"]))):
        raise ValueError("checkpoint part integrity mismatch")
    return encoded


def hydrate_checkpoint(payload, fetch_part):
    if payload.get("schema_version") != 2:
        return payload
    if ("part_layout" in payload or "inline_members" in payload) and payload.get("part_layout") != MEMBER_LAYOUT:
        raise ValueError("unsupported checkpoint member layout")
    if payload.get("part_layout") == MEMBER_LAYOUT:
        _validate_aligned_manifest(payload)
        data, raw, inline = [], [], payload["inline_members"]
        for index in range(len(payload["parts"]) + 1):
            for ref in inline:
                if ref["before"] == index:
                    raw.append(_read_member(ref.get("data"), ref))
                    data.append(ref["data"])
            if index < len(payload["parts"]):
                ref = payload["parts"][index]
                encoded = _part_data(fetch_part(ref["id"]), ref, aligned=True)
                raw.append(_read_member(encoded, ref))
                data.append(encoded)
        combined = b"".join(raw)
        if len(combined) != payload["decoded_bytes"] or _digest(combined) != payload["sha256"]:
            raise ValueError("checkpoint digest mismatch")
        result = json.loads(combined)
        if (not isinstance(result, dict) or _json_bytes(result) != combined
                or result.get("_runtime", {}) != payload["runtime"]):
            raise ValueError("checkpoint JSON or runtime metadata mismatch")
        return {**payload, "schema_version": 1, "encoding": "gzip+base64", "data": "".join(data)}
    refs = payload.get("parts")
    if payload.get("encoding") != "gzip+base64+parts" or not isinstance(refs, list) or not 1 <= len(refs) <= MAX_PARTS:
        raise ValueError("invalid checkpoint manifest")
    data, total = [], 0
    for ref in refs:
        encoded = _part_data(fetch_part(ref["id"]), ref)
        data.append(encoded)
        total += len(encoded)
        if total > MAX_ENCODED_BYTES:
            raise ValueError("checkpoint parts exceed safety limit")
    if total != payload.get("encoded_bytes"):
        raise ValueError("checkpoint manifest length mismatch")
    return {**payload, "schema_version": 1, "encoding": "gzip+base64", "data": "".join(data)}


def restore_checkpoint(local, payload):
    remote = decode_checkpoint(payload)
    left, right = local.get("_runtime", {}), remote.get("_runtime", {})
    if (right.get("updated_at", ""), int(right.get("revision") or 0)) <= (left.get("updated_at", ""), int(left.get("revision") or 0)):
        return local, False
    restored = copy.deepcopy(remote)
    for key in REBUILDABLE_KEYS:
        if key in local:
            restored[key] = local[key]
    restored.setdefault("wallet_cache", {})
    return restored, True
