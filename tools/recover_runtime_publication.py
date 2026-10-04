"""Replay the latest saved dashboard without scanning or changing the outbox."""
import gzip
import json
import re
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAX_COMPRESSED_BYTES = 64 * 1024 * 1024
MAX_DECODED_BYTES = 128 * 1024 * 1024


def saved_dashboard(directory):
    paths = sorted(path for path in directory.glob("*.json.gz")
                   if re.fullmatch(r"\d{14,20}\.json\.gz", path.name))
    if not paths:
        raise ValueError("no saved dashboard in outbox")
    path = paths[-1]
    if path.stat().st_size > MAX_COMPRESSED_BYTES:
        raise ValueError("saved dashboard exceeds compressed limit")
    with gzip.open(path, "rb") as handle:
        data = handle.read(MAX_DECODED_BYTES + 1)
    if len(data) > MAX_DECODED_BYTES:
        raise ValueError("saved dashboard exceeds decoded limit")
    body = json.loads(data)
    if not isinstance(body, dict) or not isinstance(body.get("report"), dict):
        raise ValueError("invalid saved dashboard report")
    stamp = body["report"].get("generated_at")
    if not isinstance(stamp, str) or not datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo:
        raise ValueError("invalid saved dashboard timestamp")
    if re.sub(r"[^0-9]", "", stamp) + ".json.gz" != path.name:
        raise ValueError("saved dashboard timestamp mismatch")
    return body


def recover(directory, publish=None):
    body = saved_dashboard(directory)
    if publish is None:
        from scanner import publish_runtime_dashboard
        publish = publish_runtime_dashboard
    result = publish(body, {})
    if result.get("ok") is not True or result.get("accepted") is not True:
        raise RuntimeError("saved dashboard publication was not acknowledged")
    return {"ok": True, "source_generated_at": body["report"]["generated_at"],
            "outbox_unchanged": True, "rpc_calls": 0}


def main():
    try:
        result = recover(ROOT / "data" / "remote_outbox")
    except Exception:
        print("Saved dashboard recovery failed; the outbox and previous snapshot are unchanged")
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
