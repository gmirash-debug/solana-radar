"""Validate failure-time cache snapshots without importing scanner dependencies."""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


STATE_FILES = ("state.json", "discovery_state.json")
MAX_BYTES = 128 * 1024 * 1024


def reject_constant(value):
    raise ValueError(f"non-finite JSON constant: {value}")


def state_metadata(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("state exceeds cache safety limit")
    state = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    if not isinstance(state, dict):
        raise ValueError("state must be an object")
    runtime = state.get("_runtime")
    if (not isinstance(runtime, dict) or type(runtime.get("schema_version")) is not int
            or runtime["schema_version"] != 1):
        raise ValueError("missing supported runtime metadata")
    revision = runtime.get("revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("invalid runtime revision")
    timestamp = runtime.get("updated_at")
    if not isinstance(timestamp, str):
        raise ValueError("missing runtime timestamp")
    updated = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if updated.tzinfo is None:
        raise ValueError("runtime timestamp must include timezone")
    for key in ("pools", "market", "activity_baselines", "rpc_monthly_usage"):
        if key in state and not isinstance(state[key], dict):
            raise ValueError(f"invalid {key} section")
    if "discovery_queue" in state and not isinstance(state["discovery_queue"], list):
        raise ValueError("invalid discovery queue")
    return {"revision": revision, "updated_at": timestamp}


def capture(data_dir):
    result = {"states": {}, "present": [], "errors": []}
    for name in STATE_FILES:
        path = data_dir / name
        if path.exists():
            result["present"].append(name)
            try:
                result["states"][name] = state_metadata(path)
            except (OSError, ValueError, TypeError) as exc:
                result["errors"].append(f"{name}: {exc}")
    status = data_dir / "discovery_status.json"
    if status.exists():
        try:
            if status.stat().st_size > MAX_BYTES:
                raise ValueError("status exceeds cache safety limit")
            value = json.loads(status.read_text(encoding="utf-8"), parse_constant=reject_constant)
            if not isinstance(value, dict):
                raise ValueError("status must be an object")
        except (OSError, ValueError, TypeError) as exc:
            result["errors"].append(f"discovery_status.json: {exc}")
    return result


def validate(data_dir, baseline):
    current = capture(data_dir)
    # A previously corrupt checkpoint can be repaired by the scanner. Validate
    # the replacement while retaining rollback checks for readable predecessors.
    errors = list(current["errors"])
    if not current["states"]:
        errors.append("no versioned runtime state to preserve")
    for name in baseline.get("present", baseline.get("states", {})):
        if name not in current["states"]:
            errors.append(f"{name}: prior state disappeared or remains invalid")
    for name, before in (baseline.get("states") or {}).items():
        after = current["states"].get(name)
        if after is None:
            errors.append(f"{name}: prior state disappeared")
            continue
        old_time = datetime.fromisoformat(before["updated_at"].replace("Z", "+00:00"))
        new_time = datetime.fromisoformat(after["updated_at"].replace("Z", "+00:00"))
        if after["revision"] < before["revision"] or new_time < old_time:
            errors.append(f"{name}: state moved backward")
    return not errors, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "validate"))
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    if args.action == "capture":
        args.baseline.write_text(json.dumps(capture(args.data_dir)), encoding="utf-8")
        return
    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        valid, errors = validate(args.data_dir, baseline)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        valid, errors = False, [f"cache validation unavailable: {exc}"]
    for error in errors:
        print(f"Runtime cache not saved: {error}", file=sys.stderr)
    print(f"valid={'true' if valid else 'false'}")


if __name__ == "__main__":
    main()
