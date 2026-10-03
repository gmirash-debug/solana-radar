"""Cloud notification backstop. This monitor never reads or writes R2 objects."""
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen

API = "https://solana-radar-scan-dispatcher.gmirash-solana-radar.workers.dev"
REPO = "gmirash-debug/solana-radar"
ISSUES = "https://api.github.com/repos/" + REPO + "/issues"
LIMITS = {"class_a": 1_000_000, "class_b": 10_000_000, "storage_bytes": 10_000_000_000}
EVENT_ID = re.compile(r"radar-r2-budget-\d{4}-\d{2}-(warning|paused|resumed)")
ISSUE_URL = re.compile(r"https://github\.com/gmirash-debug/solana-radar/issues/\d+")


def request_json(url, token=None, body=None, secret=None):
    headers = {"Accept": "application/json", "User-Agent": "SolanaRadar-R2-Budget"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if secret:
        headers["x-radar-ingest-secret"] = secret
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    with urlopen(Request(url, data=data, headers=headers), timeout=20) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("notification_response_too_large")
    return json.loads(raw)


def issue_body(event):
    labels = {"warning": "Warning: at least 80% of a free allowance is reserved.",
              "paused": "Protection activated: ALL billable scanner R2 operations are stopped.",
              "resumed": "R2 has resumed below the monthly, billing-cycle safety and storage thresholds.",
              "test": "Notification test: R2 protection is enabled. No quota was simulated or consumed.",
              "unavailable": "R2 budget monitoring is unavailable. Billable R2 access fails closed; investigate the guard."}
    lines = ["@gmirash-debug", "", labels[event["kind"]], ""]
    for key, label in [("storage_bytes", "Storage upper bound (GB)"), ("class_a", "Class A operations"), ("class_b", "Class B operations")]:
        used = event.get(key)
        if isinstance(used, int) and not isinstance(used, bool) and used >= 0:
            amount = f"{used / 1_000_000_000:.3f} / 10" if key == "storage_bytes" else f"{used:,} / {LIMITS[key]:,}"
            lines.append(f"- {label}: {amount} ({100 * used / LIMITS[key]:.2f}%)")
    lines.extend(["", "Stop threshold: 90% of ANY allowance; warning threshold: 80%.",
                  "Monthly operations reset on the first day at 00:00 UTC. Stored bytes do not reset.",
                  "A rolling 33-day safety cap prevents a calendar reset from overspending the actual provider billing cycle; it may extend the pause.",
                  "At 9 GB or more, R2 remains paused even after a new month begins.",
                  "Accounting is a conservative scanner reservation, not a Cloudflare invoice.",
                  "External uploads or other R2 clients are outside this guard.",
                  "", "[Open scanner](https://gmirash-debug.github.io/solana-radar/) > Diagnostics > R2 free-tier budget.",
                  "", f"<!-- {event['id']} -->"])
    return "\n".join(lines)


def ensure_issue(event, issues, call, token):
    marker = f"<!-- {event['id']} -->"
    for issue in issues:
        if not issue.get("pull_request") and marker in str(issue.get("body") or "") and ISSUE_URL.fullmatch(str(issue.get("html_url") or "")):
            return issue["html_url"], False
    titles = {"warning": "R2 free tier: 80% warning", "paused": "R2 paused to protect free quotas",
              "resumed": "R2 resumed within free-tier safety caps", "test": "R2 free-tier protection: notifications enabled",
              "unavailable": "R2 budget monitor needs attention"}
    period = re.search(r"\d{4}-\d{2}", event["id"]).group()
    issue = call(ISSUES, token=token, body={"title": f"{titles[event['kind']]} [{period}]",
                "body": issue_body(event), "assignees": ["gmirash-debug"]})
    url = str(issue.get("html_url") or "")
    if not ISSUE_URL.fullmatch(url):
        raise ValueError("notification_issue_not_confirmed")
    issues.append(issue)
    return url, True


def monitor(token, secret, test_notification=False, call=request_json, now=None):
    if not token or not secret:
        raise ValueError("notification_credentials_missing")
    now = now or datetime.now(timezone.utc)
    try:
        budget = call(API + "/api/storage/r2-budget")
        if budget.get("ok") is not True or budget.get("enabled") is not True or budget.get("initialized") is not True:
            raise ValueError("budget_not_ready")
        events = budget.get("notifications")
        if not isinstance(events, list):
            raise ValueError("budget_notifications_invalid")
        pending = [event for event in events if not event.get("notified_at")]
        for event in pending:
            if not EVENT_ID.fullmatch(str(event.get("id") or "")) or event.get("kind") != event["id"].rsplit("-", 1)[-1]:
                raise ValueError("budget_notification_invalid")
        if test_notification:
            pending.append({"id": f"radar-r2-budget-{now:%Y-%m}-test", "kind": "test", **budget.get("usage", {})})
    except Exception:
        if test_notification:
            raise ValueError("notification_test_budget_unavailable") from None
        pending = [{"id": f"radar-r2-budget-{now:%Y-%m-%d}-unavailable", "kind": "unavailable"}]
    if not pending:
        return {"checked": True, "created": 0, "acknowledged": 0}
    issues = call(ISSUES + "?state=all&per_page=100&sort=created&direction=desc", token=token)
    if not isinstance(issues, list):
        raise ValueError("notification_issue_list_unavailable")
    created = acknowledged = 0
    urls = []
    for event in pending:
        url, added = ensure_issue(event, issues, call, token)
        created += int(added)
        urls.append(url)
        if event["kind"] in {"warning", "paused", "resumed"}:
            ack = call(API + "/api/storage/r2-budget/ack", secret=secret, body={"id": event["id"], "issue_url": url})
            if ack.get("ok") is not True:
                raise ValueError("notification_ack_pending")
            acknowledged += 1
    return {"checked": True, "created": created, "acknowledged": acknowledged, "issues": urls}


def main():
    try:
        result = monitor(os.environ.get("GH_TOKEN"), os.environ.get("RADAR_INGEST_SECRET"),
                         test_notification=os.environ.get("TEST_NOTIFICATION") == "true")
        print(json.dumps(result))
    except Exception:
        # HTTP exceptions may contain credential-bearing transport details.
        print("R2 notification delivery failed; alert remains pending for retry.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
