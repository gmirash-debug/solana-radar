"""Compact durable recovery contract; caches remain reconstructible, evidence does not."""
import base64
import copy
import gzip
import hashlib
import json

MAX_ENCODED_BYTES = 192 * 1024 * 1024
MAX_DECODED_BYTES = 128 * 1024 * 1024
INLINE_BYTES = 256 * 1024
PART_BYTES = 256 * 1024
MAX_PARTS = MAX_ENCODED_BYTES // PART_BYTES
REBUILDABLE_KEYS = {"wallet_cache", "social_cache", "gmgn_cache", "enrichment_cache"}


def build_checkpoint(state):
    durable = {key: value for key, value in state.items() if key not in REBUILDABLE_KEYS}
    raw = json.dumps(durable, separators=(",", ":"), ensure_ascii=True, sort_keys=True, allow_nan=False).encode()
    if len(raw) > MAX_DECODED_BYTES:
        raise ValueError("runtime checkpoint exceeds decoded safety limit")
    encoded = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    if len(encoded) > MAX_ENCODED_BYTES:
        raise ValueError("runtime checkpoint exceeds durable capacity; evidence was not truncated")
    runtime = json.loads(json.dumps(durable.get("_runtime", {}),sort_keys=True,ensure_ascii=True,allow_nan=False))
    return {"schema_version": 1, "encoding": "gzip+base64", "sha256": hashlib.sha256(raw).hexdigest(),
            "decoded_bytes": len(raw), "data": encoded, "runtime": runtime}


def decode_checkpoint(payload):
    if payload.get("schema_version") != 1 or payload.get("encoding") != "gzip+base64":
        raise ValueError("unsupported runtime checkpoint")
    encoded = payload.get("data", "")
    if len(encoded) > MAX_ENCODED_BYTES:
        raise ValueError("encoded checkpoint exceeds safety limit")
    import io
    with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(encoded, validate=True))) as stream:
        raw = stream.read(MAX_DECODED_BYTES + 1)
    if len(raw) > MAX_DECODED_BYTES or len(raw) != payload.get("decoded_bytes"):
        raise ValueError("invalid checkpoint length")
    if hashlib.sha256(raw).hexdigest() != payload.get("sha256"):
        raise ValueError("checkpoint digest mismatch")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("checkpoint must be a mapping")
    return result


def checkpoint_documents(payload):
    """Stage immutable pieces before replacing the last complete manifest."""
    encoded = payload["data"]
    if len(encoded) <= INLINE_BYTES:
        return payload, []
    parts = []
    for start in range(0, len(encoded), PART_BYTES):
        data = encoded[start:start + PART_BYTES]
        parts.append({"schema_version": 1, "encoding": "gzip+base64-part",
                      "sha256": hashlib.sha256(data.encode("ascii")).hexdigest(),
                      "encoded_bytes": len(data), "data": data})
    manifest = {key: value for key, value in payload.items() if key != "data"}
    manifest.update(schema_version=2, encoding="gzip+base64+parts", encoded_bytes=len(encoded),
                    parts=[{"id": part["sha256"], "bytes": part["encoded_bytes"]} for part in parts])
    return manifest, parts


def hydrate_checkpoint(payload, fetch_part):
    if payload.get("schema_version") != 2:
        return payload
    refs = payload.get("parts")
    if payload.get("encoding") != "gzip+base64+parts" or not isinstance(refs, list) or not 1 <= len(refs) <= MAX_PARTS:
        raise ValueError("invalid checkpoint manifest")
    data = []
    total = 0
    for ref in refs:
        part = fetch_part(ref["id"])
        encoded = part.get("data", "")
        if (part.get("encoding") != "gzip+base64-part" or not isinstance(encoded, str)
                or not 0 < len(encoded) <= 1024 * 1024 or len(encoded) != ref.get("bytes")
                or part.get("encoded_bytes") != len(encoded)
                or hashlib.sha256(encoded.encode("ascii")).hexdigest() != ref["id"]):
            raise ValueError("checkpoint part integrity mismatch")
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
