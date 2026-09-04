"""Keep UI-only publications from replacing a fresh scan with old Git data."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


PUBLISHED_SNAPSHOT = "https://gmirash-debug.github.io/solana-radar/data/dashboard_fallback.json"


def snapshot_time(payload):
    try:
        value = payload["report"]["generated_at"]
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def choose_snapshot(local, published, now=None):
    local_at, published_at = snapshot_time(local), snapshot_time(published)
    if published_at and (not local_at or published_at > local_at):
        return published
    if local_at:
        now = now or datetime.now(timezone.utc)
        if not published_at and not -timedelta(minutes=5) <= now - local_at <= timedelta(hours=3):
            raise ValueError("Published snapshot unavailable; refusing to publish stale Git data")
        return local
    raise ValueError("No valid dashboard snapshot is available for publication")


def main():
    try:
        local = json.loads(Path("data/dashboard_fallback.json").read_text())
    except (OSError, ValueError):
        local = None
    try:
        response = requests.get(PUBLISHED_SNAPSHOT, timeout=20)
        response.raise_for_status()
        published = response.json()
    except Exception as exc:
        print(f"Published snapshot unavailable: {type(exc).__name__}", file=sys.stderr)
        published = None
    selected = choose_snapshot(local, published)
    destination = Path(".pages/data/dashboard_fallback.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(selected, separators=(",", ":")))
    print(f"Pages snapshot: {selected['report']['generated_at']}")


if __name__ == "__main__":
    main()
