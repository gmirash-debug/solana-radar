"""Bounded background drain after live publication; never calls RPC or R2 directly."""
import argparse
import json
import os
import time
from urllib.parse import urlparse

import requests


def drain(url, secret, maximum=10, seconds=55, session=None, clock=time.monotonic):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid history API URL")
    if not secret or not 1 <= maximum <= 10 or not 1 <= seconds <= 55:
        raise ValueError("invalid history drain configuration")
    session = session or requests.Session()
    deadline = clock() + seconds
    summary = {"flushes": 0, "delivered": 0, "continued": 0, "pending": None, "status": "bounded"}
    for _ in range(maximum):
        remaining = deadline - clock()
        if remaining < 1:
            summary["status"] = "time_deferred"
            break
        try:
            response = session.post(url.rstrip("/") + "/api/runtime/history/flush",
                                    headers={"x-radar-ingest-secret": secret},
                                    timeout=min(remaining, 15), allow_redirects=False)
            body = response.json()
        except (requests.RequestException, ValueError):
            summary["status"] = "transport_deferred"
            break
        summary["flushes"] += 1
        if isinstance(body.get("pending"), int):
            summary["pending"] = body["pending"]
        for name in ("delivered", "continued"):
            if isinstance(body.get(name), int) and body[name] >= 0:
                summary[name] += body[name]
        if response.status_code in (401, 403):
            raise RuntimeError("history drain authorization unavailable")
        if not response.ok or not body.get("ok") or body.get("error"):
            summary["status"] = "queue_deferred"
            break
        if body.get("migration_pending"):
            summary["status"] = "legacy_migration_pending"
            if not body.get("migration_progressed"):
                break
            continue
        if summary["pending"] == 0:
            summary["status"] = "empty"
            break
        if not body.get("delivered") and not body.get("continued"):
            summary["status"] = "lease_or_retry_deferred"
            break
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-flushes", type=int, default=10)
    parser.add_argument("--max-seconds", type=int, default=55)
    args = parser.parse_args()
    try:
        result = drain(os.environ.get("RADAR_DATA_API_URL", ""), os.environ.get("RADAR_INGEST_SECRET", ""),
                       args.max_flushes, args.max_seconds)
    except (ValueError, RuntimeError):
        print("History drain unavailable; live publication and durable backlog are unchanged")
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
