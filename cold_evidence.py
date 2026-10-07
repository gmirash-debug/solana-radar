"""Immutable evidence transport; callers retain originals until their own policy permits removal."""
import gzip
import hashlib
import io
import json
import re
from datetime import datetime
from urllib.parse import urlsplit

import requests
from storage_generation import storage_epoch

MAX_BYTES = 16 * 1024 * 1024
MAX_DECODED_BYTES = 128 * 1024 * 1024
_FIELDS = {"version", "token_address", "pool_address", "cohort_id", "signal_at", "evidence"}
_IDENTITY = ("token_address", "pool_address", "cohort_id", "signal_at")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")


def _timestamp(value):
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError("cold evidence timestamp invalid; original retained")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("cold evidence timestamp invalid; original retained") from None


def _configuration(url, secret, epoch):
    try:
        parsed = urlsplit(url)
        if (not isinstance(url, str) or parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.port == 0
                or re.search(r"[\s\\\x00-\x1f\x7f]", url)
                or not isinstance(secret, str) or not secret or secret != secret.strip()
                or re.search(r"[\x00-\x1f\x7f]", secret)):
            raise ValueError
        selected_epoch = storage_epoch() if epoch is None else epoch
        if not isinstance(selected_epoch, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", selected_epoch):
            raise ValueError
    except Exception:
        raise ValueError("cold evidence configuration unavailable; original retained") from None
    return url.rstrip("/") + "/api/storage/evidence-archive", {
        "x-radar-ingest-secret": secret, "x-radar-storage-epoch": selected_epoch,
    }


def _reference(ref):
    if not isinstance(ref, dict):
        raise ValueError("cold evidence reference invalid; original retained")
    digest = ref.get("sha256")
    size = ref.get("bytes")
    if (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or ref.get("key") != f"evidence/v1/sha256/{digest[:2]}/{digest}.json.gz"
            or type(size) is not int or not 1 <= size <= MAX_BYTES):
        raise ValueError("cold evidence reference invalid; original retained")
    return {"key": ref["key"], "sha256": digest, "bytes": size}


def _snapshot(body):
    if (not isinstance(body, dict) or set(body) != _FIELDS
            or type(body.get("version")) is not int or body["version"] != 1
            or not isinstance(body.get("evidence"), dict)):
        raise ValueError("cold evidence schema invalid; original retained")
    for field in _IDENTITY:
        value = body[field]
        if (not isinstance(value, str) or not value or len(value) > 240 or value != value.strip()
                or re.search(r"[\x00-\x1f\x7f]", value)
                or (field in body["evidence"] and body["evidence"][field] != value)):
            raise ValueError("cold evidence identity invalid; original retained")
    _timestamp(body["signal_at"])
    return body


def _json_types(value):
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError
        for item in value.values():
            _json_types(item)
    elif isinstance(value, list):
        for item in value:
            _json_types(item)
    elif type(value) not in (str, int, float, bool, type(None)):
        raise ValueError


def _canonical(body):
    try:
        _json_types(body)
        return json.dumps(body, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except Exception:
        raise ValueError("cold evidence JSON invalid; original retained") from None


def archive_evidence(payload, url, secret, observed_at, *, session=None, timeout=25, epoch=None):
    """Return only a verified receipt, without changing or deleting the supplied evidence."""
    endpoint, headers = _configuration(url, secret, epoch)
    _timestamp(observed_at)
    if not isinstance(payload, dict):
        raise ValueError("cold evidence schema invalid; original retained")
    snapshot = _snapshot({"version": 1, **payload})
    raw = _canonical(snapshot)
    if len(raw) > MAX_DECODED_BYTES:
        raise ValueError("cold evidence decoded capacity exceeded; original retained")
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as handle:
        handle.write(raw)
    data = output.getvalue()
    if len(data) > MAX_BYTES:
        raise ValueError("cold evidence compressed capacity exceeded; original retained")
    digest = hashlib.sha256(data).hexdigest()
    expected = {"key": f"evidence/v1/sha256/{digest[:2]}/{digest}.json.gz",
                "sha256": digest, "bytes": len(data)}
    response = None
    try:
        response = (requests if session is None else session).post(
            endpoint, params={"id": digest}, data=data,
            headers={**headers, "content-type": "application/gzip", "x-radar-generated-at": observed_at},
            timeout=timeout, allow_redirects=False)
        if not response.ok or not 200 <= response.status_code < 300:
            raise ValueError
        result = response.json()
        if (not isinstance(result, dict) or result.get("ok") is not True
                or result.get("accepted") is not True or result.get("id") != digest
                or _reference(result.get("archive_ref")) != expected):
            raise ValueError
        return expected
    except Exception:
        raise RuntimeError("cold evidence archive unverified or unavailable; original retained") from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def _decode(data, ref):
    if len(data) != ref["bytes"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
        raise ValueError
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as handle:
        raw = handle.read(MAX_DECODED_BYTES + 1)
    if len(raw) > MAX_DECODED_BYTES:
        raise ValueError
    body = _snapshot(json.loads(raw.decode("utf-8")))
    if raw != _canonical(body):
        raise ValueError
    return body


def read_evidence(ref, url, secret, *, session=None, timeout=25, epoch=None):
    """Read a manifest-backed snapshot with bounded transport, gzip and identity validation."""
    checked = _reference(ref)
    endpoint, headers = _configuration(url, secret, epoch)
    response = None
    try:
        response = (requests if session is None else session).get(
            endpoint, params={"id": checked["sha256"]}, headers=headers,
            timeout=timeout, allow_redirects=False, stream=True)
        if not response.ok or response.status_code != 200:
            raise ValueError
        metadata = response.headers
        if (metadata.get("content-encoding", "identity") != "identity"
                or metadata.get("x-radar-sha256", checked["sha256"]) != checked["sha256"]
                or (metadata.get("content-length") is not None
                    and metadata["content-length"] != str(checked["bytes"]))):
            raise ValueError
        output = io.BytesIO()
        for chunk in response.iter_content(chunk_size=65536):
            if not isinstance(chunk, bytes) or output.tell() + len(chunk) > checked["bytes"]:
                raise ValueError
            output.write(chunk)
        return _decode(output.getvalue(), checked)
    except Exception:
        raise RuntimeError("cold evidence read unverified or unavailable; original retained") from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
