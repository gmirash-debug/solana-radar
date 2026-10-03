"""Compact durable recovery contract; caches remain reconstructible, evidence does not."""
import base64
import copy
import gzip
import hashlib
import json

MAX_ENCODED_BYTES = 7 * 1024 * 1024
MAX_DECODED_BYTES = 128 * 1024 * 1024
REBUILDABLE_KEYS = {"wallet_cache", "social_cache", "gmgn_cache", "enrichment_cache"}


def build_checkpoint(state):
    durable = {key: value for key, value in state.items() if key not in REBUILDABLE_KEYS}
    raw = json.dumps(durable, separators=(",", ":"), ensure_ascii=True).encode()
    if len(raw) > MAX_DECODED_BYTES:
        raise ValueError("runtime checkpoint exceeds decoded safety limit")
    encoded = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    if len(encoded) > MAX_ENCODED_BYTES:
        raise ValueError("runtime checkpoint exceeds durable capacity; evidence was not truncated")
    return {"schema_version": 1, "encoding": "gzip+base64", "sha256": hashlib.sha256(raw).hexdigest(),
            "decoded_bytes": len(raw), "data": encoded, "runtime": durable.get("_runtime", {})}


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
