"""Choose bounded publication work without importing the scanner or writing state."""
import json
import os
from datetime import datetime, timedelta, timezone


def scheduled_pages_slot(source, bucket, now):
    prefix = {"cloudflare-deep_scan": "deep_scan:", "cloudflare-watchdog": "watchdog:"}.get(source)
    if not prefix or not isinstance(bucket, str) or not bucket.startswith(prefix):
        return False
    try:
        value = bucket[len(prefix):]
        # The watchdog emits UTC hour buckets without an offset; Worker buckets
        # are full ISO timestamps. Do not use runner time for delayed jobs.
        if source == "cloudflare-watchdog":
            stamp = datetime.strptime(value, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
        else:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return False
        stamp = stamp.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return False
    return timedelta(0) <= now - stamp < timedelta(hours=2) and stamp.hour % 6 == 0


def workflow_plan(event, now=None):
    now = now or datetime.now(timezone.utc)
    inputs = event.get("inputs") or {}
    source = str(inputs.get("source") or "manual")
    event_name = event.get("event_name")
    dispatch = event_name == "workflow_dispatch"
    push = event_name == "push"
    targeted = dispatch and source in ("cloudflare-targeted", "manual-targeted")
    ui_only = dispatch and source == "manual-ui"
    recover_runtime = dispatch and source == "publication-recovery"
    scheduled = source.startswith("cloudflare-")
    pages = push or (dispatch and not targeted and not recover_runtime and (
        not scheduled or scheduled_pages_slot(source, inputs.get("dispatch_bucket"), now)))
    return {
        "targeted": targeted,
        "ui_only": ui_only,
        "recover_runtime": recover_runtime,
        "publish_pages": pages,
        "validate": push or (dispatch and not scheduled),
        "observe_robinhood": dispatch and not targeted and not ui_only and not recover_runtime,
    }


def main():
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
        event = json.load(handle)
    event["event_name"] = os.environ.get("GITHUB_EVENT_NAME", "")
    for key, value in workflow_plan(event).items():
        print(f"{key}={'true' if value else 'false'}")


if __name__ == "__main__":
    main()
