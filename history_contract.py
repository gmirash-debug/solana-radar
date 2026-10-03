"""Validate legacy outbox identities without rewriting source times or discarding evidence."""
from datetime import datetime, timezone


def source_time(value):
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except ValueError:
        return None


def history_event_error(row, now=None):
    if not isinstance(row, dict) or not isinstance(row.get("episode"), dict) or not isinstance(row.get("event"), dict):
        return "history_event_shape_required"
    episode, event = row["episode"], row["event"]
    caught, observed = source_time(episode.get("caught_at")), source_time(event.get("observed_at"))
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    if (not isinstance(episode.get("episode_id"), str) or not episode["episode_id"].strip()
            or not isinstance(episode.get("token_address"), str) or not episode["token_address"].strip()
            or not isinstance(event.get("event_type"), str) or not event["event_type"].strip()
            or caught is None or observed is None or observed < caught or observed > now + 300):
        return "history_source_identity_or_time_invalid"
    try:
        size = len(json_bytes(row))
    except (ValueError, TypeError):
        return "history_event_not_json"
    if size > 128 * 1024:
        return "history_event_oversize"
    return None


def json_bytes(value):
    import json
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
