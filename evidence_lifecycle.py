"""Bound stalled investigations without turning missing evidence into an exit."""
import hashlib
import json
import math
from datetime import datetime, timezone

VERSION = 1
PUBLIC_FIELDS = {
    "version", "status", "last_progress_at", "last_attempt_at", "stalled_days",
    "next_history_retry_at", "repeat_error_count", "last_error_category", "hot_bytes",
    "last_verified_balance_at", "archive_bytes", "archive_error", "archived_at",
}


def stamp(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) and value > 0 else 0
    if not isinstance(value, str):
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else 0
    except ValueError:
        return 0


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def bounded_number(config, key, default, minimum):
    try:
        value = float(config.get(key, default))
        return max(minimum, value) if math.isfinite(value) else default
    except (ValueError, TypeError):
        return default


def unresolved(thesis):
    if thesis.get("status") in {"closed", "invalidated"}:
        return False
    activity = thesis.get("wallet_activity") or {}
    return bool(thesis.get("status") in {"unknown", "weakening"}
        or thesis.get("original_sale_history_status") == "unknown"
        or thesis.get("balance_status") in {"pending", "partial", "outflow"}
        or activity and not activity.get("interpretation_complete"))


def balance_signature(thesis):
    # Fresh timestamps for the same balances are not progress in a stalled history.
    return digest([{
        "owner": row["owner"], "balance": row.get("current_balance"),
        "cap": row.get("current_retained_tokens"), "verified": bool(stamp(row.get("checked_at"))),
    } for row in thesis.get("cohort") or [] if isinstance(row, dict) and row.get("owner")])


def history_signature(thesis):
    legacy_window_missing = "legacy_original_sale_history_not_reconstructed" in (thesis.get("original_sale_history_issues") or [])
    return digest({"owners": {owner: {
        # Polling new empty windows cannot repair a missing original purchase window.
        "complete_through": None if legacy_window_missing else audit.get("complete_through"),
        "events": audit.get("archived_events_digest") or digest(audit.get("events") or {}),
    } for owner, audit in (thesis.get("wallet_activity_checks") or {}).items()
        if audit.get("events") or audit.get("archived_event_count")
            or not legacy_window_missing and audit.get("complete_through") is not None},
        "sales": thesis.get("proven_sales") or {},
        "movements": thesis.get("cohort_movement_events") or {},
        "history_status": thesis.get("original_sale_history_status"),
        "confirmation": (thesis.get("signal_confirmation") or {}).get("status")})


def ensure_health(thesis, observed_at):
    health = thesis.setdefault("analysis_health", {})
    if health.get("version") == VERSION:
        return health
    now = stamp(observed_at)
    # Migrate from actual evidence times, never updated_at/last_checked_at (attempts).
    times = [stamp(thesis.get("captured_at") or thesis.get("signal_at")),
             stamp(thesis.get("last_complete_check_at"))]
    times.extend(stamp(row.get("checked_at")) for row in thesis.get("cohort") or []
                 if isinstance(row, dict) and row.get("current_balance") is not None)
    times.extend(stamp(audit.get("checked_at")) for audit in (thesis.get("wallet_activity_checks") or {}).values()
                 if audit.get("complete_through") is not None)
    progress = max((value for value in times if 0 < value <= now), default=now)
    health.update(version=VERSION, status="active", last_progress_at=iso(progress),
        repeat_error_count=0, _balance_signature=balance_signature(thesis))
    health["_history_signature"] = history_signature(thesis)
    return health


def refresh_health(thesis, config, observed_at):
    health = ensure_health(thesis, observed_at)
    days = max(0, (stamp(observed_at) - stamp(health["last_progress_at"])) / 86400)
    stall_days = bounded_number(config, "analysis_stall_days", 7, 1)
    archive_days = max(stall_days, bounded_number(config, "analysis_archive_days", 30, 1))
    stalled = unresolved(thesis) and days >= stall_days
    health["stalled_days"] = round(days, 2) if stalled else 0
    health["status"] = "archived" if health.get("_cold") else "stalled" if stalled else "active"
    if health.get("archive_error") and (health.get("_cold") or stalled and days >= archive_days):
        health["status"] = "archive_pending"
    interval = bounded_number(config, "analysis_stalled_history_retry_minutes", 720, 60) * 60
    retry = max(stamp(health.get("_last_history_attempt_at")) + interval if stalled else 0,
                stamp(health.get("_restore_retry_after")))
    health["next_history_retry_at"] = iso(retry) if retry else None
    health["hot_bytes"] = len(encoded({key: value for key, value in thesis.items() if key != "analysis_health"}))
    return health


def record_attempt(thesis, config, observed_at, *, error=None, balance_success=False):
    health = ensure_health(thesis, observed_at)
    balance = balance_signature(thesis)
    history = history_signature(thesis)
    changed = balance != health.get("_balance_signature") or history != health.get("_history_signature")
    health["last_attempt_at"] = observed_at
    if changed:
        health["last_progress_at"] = observed_at
        health["repeat_error_count"] = 0
        if not health.get("_cold"):
            health.pop("archive_error", None)
    health["_balance_signature"], health["_history_signature"] = balance, history
    if error:
        # Categories only: never copy provider URLs, credentials or exception text.
        previous_error = health.get("last_error_category")
        health["last_error_category"] = error
        health["repeat_error_count"] = int(health.get("repeat_error_count") or 0) + 1 if previous_error == error else 1
    if balance_success:
        health["last_verified_balance_at"] = observed_at
        event_at = stamp(health.get("_retention_event_at"))
        daily = bounded_number(config, "analysis_balance_history_sample_hours", 24, 1) * 3600
        if changed or not event_at or stamp(observed_at) - event_at >= daily:
            health["_retention_event_at"] = observed_at
    refresh_health(thesis, config, observed_at)
    return changed


def retention_event_at(thesis):
    health = thesis.get("analysis_health") or {}
    if health.get("version") != VERSION:
        return thesis.get("last_checked_at")  # Legacy producers remain readable.
    at = health.get("_retention_event_at")
    # Old events already live in their durable outbox; never regenerate them with newer wallet rows.
    return at if stamp(at) == stamp(thesis.get("last_checked_at")) else None


def history_due(thesis, config, observed_at):
    health = refresh_health(thesis, config, observed_at)
    retry = max(stamp(health.get("next_history_retry_at")), stamp(health.get("_restore_retry_after")))
    return stamp(observed_at) >= retry


def mark_history_attempt(thesis, observed_at):
    ensure_health(thesis, observed_at)["_last_history_attempt_at"] = observed_at


def public_health(thesis):
    return {key: value for key, value in (thesis.get("analysis_health") or {}).items() if key in PUBLIC_FIELDS}


def event_payload(thesis):
    return {owner: audit["events"] for owner, audit in (thesis.get("wallet_activity_checks") or {}).items()
            if audit.get("events")}


def archive_due(thesis, config, observed_at):
    health = refresh_health(thesis, config, observed_at)
    days = bounded_number(config, "analysis_archive_days", 30, 1)
    if health["status"] == "active" or health.get("_cold") or health["stalled_days"] < days:
        return False
    retry = bounded_number(config, "analysis_archive_retry_minutes", 720, 60) * 60
    if stamp(observed_at) < stamp(health.get("_archive_attempt_at")) + retry:
        return False
    minimum = bounded_number(config, "analysis_archive_min_bytes", 16384, 1024)
    return len(encoded(event_payload(thesis))) >= minimum


def cold_snapshot(thesis):
    return {"version": 1, "token_address": thesis["token_address"],
        "pool_address": thesis["pool_address"], "cohort_id": thesis.get("cohort_id") or thesis["signal_at"],
        "signal_at": thesis["signal_at"], "evidence": {"wallet_activity_events": event_payload(thesis)}}


def prune_archived_events(thesis, ref, observed_at, fingerprint):
    health = ensure_health(thesis, observed_at)
    for audit in (thesis.get("wallet_activity_checks") or {}).values():
        events = audit.pop("events", {}) or {}
        audit["archived_event_count"] = len(events)
        audit["archived_events_digest"] = digest(events)
    health.update(archive_ref=ref, archived_at=observed_at, archive_bytes=ref["bytes"],
        _archive_fingerprint=fingerprint, _cold=True)
    health.pop("archive_error", None)


def restore_archived_events(thesis, snapshot):
    expected = cold_snapshot(thesis)
    if any(snapshot.get(key) != expected[key] for key in ("version", "token_address", "pool_address", "cohort_id", "signal_at")):
        raise ValueError("analysis_archive_identity_mismatch")
    owners = (snapshot.get("evidence") or {}).get("wallet_activity_events")
    if not isinstance(owners, dict) or digest(owners) != (thesis.get("analysis_health") or {}).get("_archive_fingerprint"):
        raise ValueError("analysis_archive_evidence_mismatch")
    audits = thesis.get("wallet_activity_checks") or {}
    if any(owner not in audits or not isinstance(events, dict) for owner, events in owners.items()):
        raise ValueError("analysis_archive_owner_mismatch")
    # Restore receipts only. Hot cursors, retry counters and new balance checks take precedence.
    for owner, audit in audits.items():
        audit["events"] = owners.get(owner, {})
        audit.pop("archived_event_count", None)
        audit.pop("archived_events_digest", None)
    thesis["analysis_health"]["_cold"] = False


def storage_health(state, config, observed_at):
    stats = {"version": 1, "checked_at": observed_at, "unresolved_count": 0, "stalled_count": 0,
             "archive_pending_count": 0, "archived_count": 0, "oldest_stalled_days": 0, "hot_bytes": 0}
    for entry in (state.get("pools") or {}).values():
        thesis = entry.get("signal_thesis") if isinstance(entry, dict) else None
        if not isinstance(thesis, dict) or thesis.get("status") in {"closed", "invalidated"}:
            continue
        health = refresh_health(thesis, config, observed_at)
        stats["hot_bytes"] += health["hot_bytes"]
        stats["unresolved_count"] += int(unresolved(thesis))
        stats["stalled_count"] += int(health["stalled_days"] > 0)
        stats["archive_pending_count"] += int(health["status"] == "archive_pending")
        stats["archived_count"] += int(health["status"] == "archived")
        stats["oldest_stalled_days"] = max(stats["oldest_stalled_days"], health["stalled_days"])
    previous = state.setdefault("maintenance", {}).get("analysis_daily_baseline") or {}
    day = iso(stamp(observed_at))[:10]
    growth = stats["hot_bytes"] - int(previous.get("hot_bytes", stats["hot_bytes"]))
    warnings = []
    if stats["stalled_count"]:
        warnings.append("Stalled history recovery; missing evidence does not prove a sale")
    if stats["archive_pending_count"]:
        warnings.append("Evidence archive unavailable; original receipts remain in hot storage")
    if growth > max(1024 * 1024, int(previous.get("hot_bytes") or 0) * 0.2):
        warnings.append("Investigation hot storage grew by more than 20% and 1 MiB")
    if stats["hot_bytes"] >= bounded_number(config, "analysis_hot_warning_bytes", 64 * 1024 * 1024, 1024):
        warnings.append("Investigation hot storage exceeds its operational target")
    if not previous or previous.get("day") != day:
        state["maintenance"]["analysis_daily_baseline"] = {"day": day, "hot_bytes": stats["hot_bytes"]}
    stats.update(warning=bool(warnings), warnings=warnings, growth_bytes=max(0, growth))
    state["maintenance"]["analysis_storage_health"] = stats
    return stats
