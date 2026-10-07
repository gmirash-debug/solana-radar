"""Move cold original snapshots into guarded R2 plus durable SQL replay metadata."""
import gzip
import hashlib
import json
from urllib.parse import urlparse

import requests
from storage_generation import storage_epoch

MAX_BYTES = 16 * 1024 * 1024
MAX_DECODED_BYTES = 128 * 1024 * 1024


def archive_snapshot(body, url, secret, timeout=25, session=None):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment or not secret:
        raise ValueError("cold archive configuration unavailable")
    # Progress is mutable and lives in SQL; the original source remains immutable.
    original = {key: value for key, value in body.items()
                if key not in {"_sync_progress", "_sync_deferred_reason", "_sync_rejected_history"}}
    data = gzip.compress(json.dumps(original, separators=(",", ":"), ensure_ascii=True).encode(), mtime=0)
    if len(data) > MAX_BYTES:
        raise ValueError("cold snapshot exceeds archive request capacity; original retained")
    digest = hashlib.sha256(data).hexdigest()
    session = session or requests
    response = session.post(url.rstrip("/") + "/api/storage/outbox", params={"id": digest}, data=data,
                            headers={"x-radar-ingest-secret": secret, "content-type": "application/gzip",
                                     "x-radar-storage-epoch": storage_epoch(),
                                     "x-radar-generated-at": body["report"]["generated_at"]},
                            timeout=timeout, allow_redirects=False)
    if not response.ok:
        raise RuntimeError("cold archive unavailable; original retained")
    result = response.json()
    ref = result.get("archive_ref") or {}
    if (result.get("ok") is not True or result.get("accepted") is not True
            or result.get("id") != digest or ref.get("sha256") != digest or ref.get("bytes") != len(data)
            or ref.get("key") != f"outbox/v1/sha256/{digest[:2]}/{digest}.json.gz"):
        raise RuntimeError("cold archive receipt is unverified; original retained")
    progress = body.get("_sync_progress") or {}
    if progress:
        saved = session.patch(url.rstrip("/") + "/api/storage/outbox",params={"id":digest},
                              headers={"x-radar-ingest-secret":secret, "x-radar-storage-epoch":storage_epoch()},
                              json={"progress":progress,"completed":False},timeout=timeout,allow_redirects=False)
        receipt = saved.json()
        if not saved.ok or receipt.get("ok") is not True or receipt.get("accepted") is not True:
            raise RuntimeError("cold archive progress is unacknowledged; original retained")
    return result


def decode_snapshot(data, ref):
    if len(data)>MAX_BYTES or len(data)!=ref.get("bytes") or hashlib.sha256(data).hexdigest()!=ref.get("sha256"):
        raise ValueError("cold snapshot transport integrity mismatch")
    import io
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as handle:
        raw = handle.read(MAX_DECODED_BYTES + 1)
    if len(raw)>MAX_DECODED_BYTES:
        raise ValueError("cold snapshot decoded capacity exceeded")
    body = json.loads(raw)
    if not isinstance(body,dict) or not isinstance(body.get("report"),dict):
        raise ValueError("cold snapshot report required")
    return body
