"""Independent, bounded GitHub scheduler guard; no RPC calls or scan state writes."""
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scan_scheduling import scan_freshness_reference

DEEP_SOURCES = frozenset({"manual", "manual-http", "cloudflare-deep_scan", "cloudflare-watchdog"})
DEEP_PROFILES = frozenset({"deep", "deep_scan"})
RUN_TITLE = re.compile(r"Scan and deploy dashboard \[source=([a-z][a-z0-9_-]{0,63})\]")


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def deep_run(run):
    # GitHub's run-list response does not expose dispatch inputs. Never infer a
    # deep attempt from the generic workflow name or an untyped legacy status.
    title = run.get("display_title")
    marker = RUN_TITLE.fullmatch(title) if isinstance(title, str) else None
    return bool(marker and marker.group(1) in DEEP_SOURCES)


def watchdog_decision(snapshots, runs, now):
    if not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
        return False, "github_status_unavailable"
    active = [run for run in runs if run.get("event") == "workflow_dispatch"
              and run.get("status") != "completed"]
    if active:
        return False, "scan_already_queued_or_running"
    snapshots = [item for item in snapshots if isinstance(item, dict)]
    catches = [parse_time(scan_freshness_reference(
        item["report"] if isinstance(item.get("report"), dict) else {}, False)) for item in snapshots]
    catches = [value for value in catches if value and value <= now]
    if catches and (now - max(catches)).total_seconds() < 90 * 60:
        return False, "deep_scan_fresh"
    recent = [parse_time(run.get("created_at")) for run in runs
              if run.get("event") == "workflow_dispatch" and deep_run(run)]
    for item in snapshots:
        status = item.get("scan_status")
        profile = status.get("scan_profile") if isinstance(status, dict) else None
        if isinstance(profile, str) and profile in DEEP_PROFILES:
            recent.append(parse_time(status.get("last_attempt_at")))
    recent = [value for value in recent if value and value <= now]
    if recent and (now - max(recent)).total_seconds() < 50 * 60:
        return False, "dispatch_cooldown"
    return True, "deep_scan_stale_or_unreachable"


def fetch_json(url, token=None):
    headers = {"Accept": "application/json", "User-Agent": "solana-radar-watchdog"}
    if token:
        headers["Authorization"] = "Bearer " + token
    with urlopen(Request(url, headers=headers), timeout=15) as response:
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise ValueError("watchdog_response_too_large")
    return json.loads(body)


def main():
    repository = os.environ.get("GITHUB_REPOSITORY", "gmirash-debug/solana-radar")
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("watchdog_token_missing")
    snapshots = []
    now = datetime.now(timezone.utc)
    for url in [
        "https://solana-radar-scan-dispatcher.gmirash-solana-radar.workers.dev/api/dashboard?history_limit=1",
        "https://gmirash-debug.github.io/solana-radar/data/dashboard_fallback.json",
    ]:
        try:
            snapshots.append(fetch_json(url))
            decision = watchdog_decision(snapshots, [], now)
            if decision == (False, "deep_scan_fresh"):
                print(json.dumps({"dispatch": False, "reason": decision[1]}))
                return
        except Exception:
            pass
    try:
        runs = fetch_json(f"https://api.github.com/repos/{repository}/actions/workflows/scan-and-pages.yml/runs?per_page=20", token).get("workflow_runs")
    except Exception:
        runs = None
    dispatch, reason = watchdog_decision(snapshots, runs, now)
    print(json.dumps({"dispatch": dispatch, "reason": reason}))
    if dispatch:
        body = json.dumps({"ref": "main", "inputs": {"source": "cloudflare-watchdog",
                            "dispatch_bucket": datetime.now(timezone.utc).strftime("watchdog:%Y-%m-%dT%H")}}).encode()
        request = Request(f"https://api.github.com/repos/{repository}/actions/workflows/scan-and-pages.yml/dispatches",
                          data=body, method="POST", headers={"Authorization": "Bearer " + token,
                          "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
        with urlopen(request, timeout=15) as response:
            if response.status != 204:
                raise SystemExit("watchdog_dispatch_rejected")


if __name__ == "__main__":
    main()
