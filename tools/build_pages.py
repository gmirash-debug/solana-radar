"""Keep UI-only publications from replacing a fresh scan with old Git data."""
import copy
import json
import hashlib
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


PUBLISHED_SNAPSHOT = "https://gmirash-debug.github.io/solana-radar/data/dashboard_fallback.json"
ROBINHOOD_SNAPSHOT = "https://gmirash-debug.github.io/solana-radar/data/robinhood.json"
TOKEN_DETAIL_PATH = re.compile(r"data/token-details/[a-f0-9]{64}\.json\Z")


def choose_robinhood(local, published):
    valid = []
    for payload in (local, published):
        try:
            if payload["chain_id"] != 4663 or not isinstance(payload.get("tokens"), list):
                continue
            updated = datetime.fromisoformat(payload["attempted_at"].replace("Z", "+00:00"))
            if updated.tzinfo and updated <= datetime.now(timezone.utc) + timedelta(minutes=5):
                valid.append((updated, payload))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return max(valid, key=lambda item: item[0])[1] if valid else None


def publish_robinhood():
    try:
        local = json.loads(Path("data/robinhood.json").read_text())
    except (OSError, ValueError):
        local = None
    try:
        response = requests.get(ROBINHOOD_SNAPSHOT, timeout=20)
        response.raise_for_status()
        published = response.json()
    except (requests.RequestException, ValueError):
        published = None
    selected = choose_robinhood(local, published)
    if selected:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from robinhood import guard_legacy_ordinary_row

        selected = copy.deepcopy(selected)
        for row in selected["tokens"]:
            guard_legacy_ordinary_row(row)
        Path(".pages/data/robinhood.json").write_text(json.dumps(selected, separators=(",", ":")))
    else:
        # Absence must not be rendered as a successful empty scan.
        Path(".pages/data/robinhood.json").write_text(json.dumps({"chain_id": 4663,
            "tokens": [], "status": "unavailable", "errors": ["No Robinhood scan has been published"]}))


def snapshot_time(payload):
    try:
        value = payload["report"]["generated_at"]
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def choose_snapshot(local, published, now=None):
    now = now or datetime.now(timezone.utc)
    local_at, published_at = snapshot_time(local), snapshot_time(published)
    if local_at and local_at > now + timedelta(minutes=5):
        local_at = None
    if published_at and published_at > now + timedelta(minutes=5):
        published_at = None
    if published_at and (not local_at or published_at > local_at):
        return published
    if local_at:
        if not published_at and not -timedelta(minutes=5) <= now - local_at <= timedelta(hours=3):
            raise ValueError("Published snapshot unavailable; refusing to publish stale Git data")
        return local
    raise ValueError("No valid dashboard snapshot is available for publication")


def publish_token_details(snapshot, runtime_state, destination):
    """Publish only wallet observations matching the selected scan's frozen cohort."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scanner import public_signal_thesis

    private = {}
    for pool in (runtime_state.get("pools") or {}).values():
        thesis = pool.get("signal_thesis") if isinstance(pool, dict) else None
        if isinstance(thesis, dict) and thesis.get("token_address"):
            private[thesis["token_address"]] = thesis
    generation = snapshot["report"]["generated_at"]
    files = {}
    destination.mkdir(parents=True, exist_ok=True)
    for summary in snapshot["report"].get("signal_theses", []):
        key = summary.get("token_address")
        raw = private.get(key)
        # Cached state can belong to a different scan or a replacement cohort.
        if not raw or not summary.get("cohort_id") or any(
            raw.get(field) != summary.get(field)
            for field in ("cohort_id", "signal_at", "signal_window_start", "signal_window_end", "last_checked_at", "updated_at")
        ):
            continue
        public = public_signal_thesis(raw)
        if not public.get("cohort_wallets"):
            continue
        thesis = {**summary, **public}
        detail = {
            "ok": True, "token_key": key, "thesis": thesis,
            "current_alerts": [], "history": [], "market": None,
            "report_source_updated_at": generation, "source": "published_scan",
        }
        encoded = json.dumps(detail, separators=(",", ":"), sort_keys=True)
        filename = hashlib.sha256(encoded.encode()).hexdigest() + ".json"
        (destination / filename).write_text(encoded)
        files[key] = f"data/token-details/{filename}"
    return {"generation": generation, "files": files}


def preserve_published_details(snapshot, manifest, destination, fetcher=requests.get, max_seconds=90, allow_partial=False):
    """Reuse exact-generation public details if the runtime cache is older."""
    previous = snapshot.get("token_details") or {}
    if previous.get("generation") != manifest["generation"]:
        return manifest
    summaries = {row.get("token_address"): row for row in snapshot["report"].get("signal_theses", [])}
    deadline = time.monotonic() + max_seconds
    for key, path in (previous.get("files") or {}).items():
        if key in manifest["files"] or key not in summaries:
            continue
        if not isinstance(path, str) or not TOKEN_DETAIL_PATH.fullmatch(path):
            raise ValueError("Unsafe published wallet detail path")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if allow_partial:
                manifest["preservation_status"] = "partial_time_budget"
                break
            raise ValueError("Wallet detail preservation exceeded its publication budget")
        try:
            response = fetcher(PUBLISHED_SNAPSHOT.rsplit("/data/", 1)[0] + "/" + path,
                               timeout=min(15, remaining))
            response.raise_for_status()
        except requests.RequestException:
            if not allow_partial: raise
            manifest["preservation_status"] = "partial_provider_unavailable"
            continue
        try:
            detail = response.json()
            summary = summaries[key]
            thesis = detail.get("thesis") if isinstance(detail, dict) else None
            if (not isinstance(thesis, dict) or detail.get("ok") is not True or detail.get("token_key") != key
                or detail.get("report_source_updated_at") != manifest["generation"]
                or not thesis.get("cohort_wallets") or not summary.get("cohort_id")
                or any(thesis.get(field) != summary.get(field)
                       for field in ("cohort_id", "signal_at", "signal_window_start", "signal_window_end", "last_checked_at", "updated_at"))):
                raise ValueError("Published wallet detail does not match the selected cohort")
            encoded = json.dumps(detail, separators=(",", ":"), sort_keys=True)
            if len(encoded.encode()) > 2 * 1024 * 1024:
                raise ValueError("Published wallet detail exceeds the size limit")
        except ValueError:
            if not allow_partial:
                raise
            manifest["preservation_status"] = "partial_detail_mismatch"
            continue
        # Also migrate legacy mint-only filenames without rewriting their URL.
        filename = hashlib.sha256(encoded.encode()).hexdigest() + ".json"
        (destination / filename).write_text(encoded)
        manifest["files"][key] = f"data/token-details/{filename}"
    return manifest


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
    try:
        runtime_state = json.loads(Path("data/state.json").read_text())
    except (OSError, ValueError):
        runtime_state = {}
    detail_dir = Path(".pages/data/token-details")
    manifest = publish_token_details(selected, runtime_state, detail_dir)
    # Ready operational data remains publishable while a bounded static-wallet
    # copy is incomplete. Missing details stay absent and use the live API.
    manifest = preserve_published_details(selected, manifest, detail_dir, allow_partial=True)
    selected = {**selected, "token_details": manifest}
    destination = Path(".pages/data/dashboard_fallback.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(selected, separators=(",", ":")))
    print(f"Pages snapshot: {selected['report']['generated_at']}")
    print(f"Published wallet details: {len(selected['token_details']['files'])}")
    publish_robinhood()


if __name__ == "__main__":
    main()
