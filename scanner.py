#!/usr/bin/env python3
import argparse
import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
DATA_DIR = ROOT / "data"
STATE_PATH = DATA_DIR / "state.json"
ALERTS_PATH = DATA_DIR / "alerts.jsonl"
REPORT_PATH = DATA_DIR / "latest_report.md"
REPORT_JSON_PATH = DATA_DIR / "latest_report.json"
DASHBOARD_FALLBACK_PATH = DATA_DIR / "dashboard_fallback.json"
SCANNER_STATUS_PATH = DATA_DIR / "scanner_status.json"
DISCOVERY_STATUS_PATH = DATA_DIR / "discovery_status.json"
DISCOVERY_STATE_PATH = DATA_DIR / "discovery_state.json"
DELETED_TOKENS_PATH = DATA_DIR / "deleted_tokens.json"
CONFIG_PATH = ROOT / "config.json"
DEFAULT_CONFIG_PATH = ROOT / "config.example.json"
STATE_SCHEMA_VERSION = 1

SOL_MINT = "So11111111111111111111111111111111111111112"
SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def utc_now():
    return datetime.now(timezone.utc)


def iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value):
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return int(timestamp)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        if text.isdigit():
            return parse_timestamp(int(text))
        try:
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0
    return 0


def load_env():
    env_paths = [ROOT / ".env.local", ROOT / ".env", REPO_ROOT / ".env.local", REPO_ROOT / ".env"]
    for env_path in env_paths:
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def load_json(path, fallback):
    if not path.exists():
        return fallback
    return json.loads(path.read_text())


def clean_deleted_id(value):
    text = str(value or "").strip()
    return text if text else None


def clean_solana_address(value):
    text = clean_deleted_id(value)
    if not text or not SOLANA_ADDRESS_RE.match(text):
        return None
    return text


def deleted_id_set(values, keys):
    ids = set()
    if isinstance(values, dict):
        for value in values.values():
            if isinstance(value, dict):
                for item_key in keys:
                    cleaned = clean_deleted_id(value.get(item_key))
                    if cleaned:
                        ids.add(cleaned)
            else:
                cleaned = clean_deleted_id(value)
                if cleaned:
                    ids.add(cleaned)
        return ids
    if not isinstance(values, list):
        return ids
    for value in values:
        if isinstance(value, dict):
            for key in keys:
                cleaned = clean_deleted_id(value.get(key))
                if cleaned:
                    ids.add(cleaned)
        else:
            cleaned = clean_deleted_id(value)
            if cleaned:
                ids.add(cleaned)
    return ids


def load_deleted_tokens(path=DELETED_TOKENS_PATH):
    if not path.exists():
        return {"tokens": set(), "pools": set()}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"warn: ignored invalid deleted token file {path}: {exc}", file=sys.stderr)
        return {"tokens": set(), "pools": set()}
    if isinstance(data, list):
        return {"tokens": deleted_id_set(data, ("token_address", "token", "address", "id")), "pools": set()}
    if not isinstance(data, dict):
        return {"tokens": set(), "pools": set()}
    token_ids = set()
    pool_ids = set()
    for field in ("tokens", "token_addresses"):
        token_ids.update(deleted_id_set(data.get(field), ("token_address", "token", "address", "id")))
    for field in ("pools", "pool_addresses"):
        pool_ids.update(deleted_id_set(data.get(field), ("pool_address", "pool", "address", "id")))
    entries = data.get("entries")
    token_ids.update(deleted_id_set(entries, ("token_address", "token", "address", "id")))
    pool_ids.update(deleted_id_set(entries, ("pool_address", "pool", "address", "id")))
    return {"tokens": token_ids, "pools": pool_ids}


def pool_is_deleted(pool, deleted):
    token = clean_deleted_id(getattr(pool, "token_address", ""))
    pool_address = clean_deleted_id(getattr(pool, "pool_address", ""))
    return bool(
        (token and token in deleted.get("tokens", set()))
        or (pool_address and pool_address in deleted.get("pools", set()))
    )


def apply_mode(config, mode_name=None):
    config = dict(config)
    selected = mode_name or config.get("mode") or "balanced"
    modes = config.get("modes", {})
    if selected not in modes:
        raise SystemExit(f"Unknown mode '{selected}'. Available: {', '.join(sorted(modes))}")
    merged = dict(config)
    merged.update(modes[selected])
    merged["mode"] = selected
    return merged


def apply_lane(config, lane_name):
    config = dict(config)
    lanes = config.get("lanes", {})
    if lane_name not in lanes:
        raise SystemExit(f"Unknown lane '{lane_name}'. Available: {', '.join(sorted(lanes))}")
    merged = dict(config)
    merged.update(lanes[lane_name])
    merged["lane"] = lane_name
    return merged


def selected_lanes(config, lane_name=None):
    lanes = config.get("lanes", {})
    if not lanes:
        return []
    selected = lane_name or config.get("lane") or "all"
    if selected == "all":
        order = config.get("lane_order") or ["breakout", "reactivation"]
        return [
            lane
            for lane in order
            if lane in lanes and lanes[lane].get("enabled", True)
        ]
    if selected not in lanes:
        raise SystemExit(f"Unknown lane '{selected}'. Available: all, {', '.join(sorted(lanes))}")
    if not lanes[selected].get("enabled", True):
        raise SystemExit(f"Lane '{selected}' is disabled.")
    return [selected]


def save_json(path, value, compact=False):
    serialized = (
        json.dumps(value, separators=(",", ":"), sort_keys=True)
        if compact
        else json.dumps(value, indent=2, sort_keys=True)
    )
    atomic_write_text(path, serialized + "\n")


def atomic_write_text(path, text):
    """Replace a persisted scanner artifact only after its full payload is durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary_path = Path(handle.name)
        try:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
    os.replace(temporary_path, path)


class StaleStateRevisionError(RuntimeError):
    """Raised when an older cached state attempts to replace a newer revision."""


def next_runtime_metadata(state, persisted, writer, observed_at=None):
    """Create the next revision while refusing a stale in-memory state snapshot."""
    runtime = state.get("_runtime")
    runtime = dict(runtime) if isinstance(runtime, dict) else {}
    persisted_runtime = persisted.get("_runtime") if isinstance(persisted, dict) else {}
    persisted_runtime = (
        dict(persisted_runtime) if isinstance(persisted_runtime, dict) else {}
    )
    source_revision = max(0, int(runtime.get("revision") or 0))
    persisted_revision = max(0, int(persisted_runtime.get("revision") or 0))
    source_updated_at = parse_timestamp(runtime.get("updated_at"))
    persisted_updated_at = parse_timestamp(persisted_runtime.get("updated_at"))
    if persisted_revision > source_revision or (
        persisted_revision == source_revision
        and persisted_revision > 0
        and persisted_updated_at > source_updated_at
    ):
        raise StaleStateRevisionError(
            "refusing to overwrite newer runtime state "
            f"revision {persisted_revision} with stale revision {source_revision}"
        )
    runtime["schema_version"] = STATE_SCHEMA_VERSION
    runtime["revision"] = max(source_revision, persisted_revision) + 1
    runtime["writer"] = writer
    runtime["updated_at"] = observed_at or utc_now().isoformat().replace("+00:00", "Z")
    runtime["run_id"] = os.environ.get("GITHUB_RUN_ID") or None
    return runtime


def save_runtime_state(state, config, writer, observed_at=None):
    """Persist a versioned state revision so concurrent writers are observable."""
    if not isinstance(state, dict):
        raise TypeError("scanner runtime state must be a mapping")
    state["_runtime"] = next_runtime_metadata(
        state,
        load_json(STATE_PATH, {}),
        writer,
        observed_at,
    )
    save_json(STATE_PATH, state, compact=bool(config.get("state_json_compact", True)))
    return state["_runtime"]


def migrate_scanner_state(state):
    """Apply lossless state migrations before a scan mutates persisted data."""
    if not isinstance(state, dict):
        raise TypeError("scanner runtime state must be a mapping")
    legacy_remote_updates = state.pop("convex_discovery_updated_at", None)
    if isinstance(legacy_remote_updates, dict):
        remote_updates = state.setdefault("remote_discovery_updated_at", {})
        for token, updated_at in legacy_remote_updates.items():
            if parse_timestamp(updated_at) > parse_timestamp(remote_updates.get(token)):
                remote_updates[token] = updated_at
    for pool_state in (state.get("pools") or {}).values():
        if not isinstance(pool_state, dict):
            continue
        for key in ("signal_thesis", "pending_signal_thesis"):
            thesis = pool_state.get(key)
            if not isinstance(thesis, dict) or int(thesis.get("version") or 1) >= 2:
                continue
            cohort = [row for row in thesis.get("cohort") or [] if isinstance(row, dict)]
            holder_min_pct = max(
                0.0,
                float(thesis.get("holder_min_pct") or 10),
            )
            complete = bool(cohort)
            for row in cohort:
                attributed = max(
                    0.0,
                    float(row.get("attributed_tokens") or row.get("initial_balance") or 0),
                )
                if not attributed:
                    complete = False
                    continue
                row["attributed_tokens"] = attributed
                current = row.get("current_retained_tokens")
                if current is None:
                    current = row.get("current_balance")
                if current is None:
                    complete = False
                    continue
                retained = min(attributed, max(0.0, float(current)))
                row["current_retained_tokens"] = retained
                row["retention_pct"] = retained / attributed * 100
                row["is_holder"] = retained / attributed * 100 >= holder_min_pct
                row.setdefault("checked_at", thesis.get("last_checked_at"))
            if complete:
                totals = cohort_retention_totals(cohort)
                original = totals["attributed_tokens"]
                thesis["original_retained_tokens"] = original
                thesis["current_retained_tokens"] = totals["retained_tokens"]
                thesis["token_retention_pct"] = totals["retention_pct"]
                thesis["holders_remaining"] = totals["holders"]
                thesis["holder_retention_pct"] = (
                    totals["holders"] / len(cohort) * 100 if cohort else None
                )
                supply = max(0.0, float(thesis.get("supply") or 0))
                if supply:
                    thesis["original_retained_supply_pct"] = original / supply * 100
                    thesis["current_retained_supply_pct"] = (
                        totals["retained_tokens"] / supply * 100
                    )
            else:
                original = max(0.0, float(thesis.get("original_retained_tokens") or 0))
                thesis["current_retained_tokens"] = None
                thesis["token_retention_pct"] = None
                thesis["status"] = "unknown"
                thesis["reason"] = (
                    "legacy cohort needs a complete balance recheck before retention "
                    "or distribution is shown"
                )
            thesis["original_attributed_tokens"] = original
            thesis.setdefault("source_attributed_tokens", original)
            thesis["version"] = 2
    return state


def discovery_state_from(state):
    """Return the sections owned by the discovery pulse, without shared cursors."""
    state = state if isinstance(state, dict) else {}
    return {
        "market": copy.deepcopy(state.get("market") or {}),
        "activity_baselines": copy.deepcopy(state.get("activity_baselines") or {}),
        "discovery_queue": copy.deepcopy(state.get("discovery_queue") or []),
        "remote_discovery_updated_at": copy.deepcopy(
            state.get("remote_discovery_updated_at") or {}
        ),
        "maintenance": {},
    }


def load_discovery_state():
    if DISCOVERY_STATE_PATH.exists():
        return migrate_scanner_state(load_json(DISCOVERY_STATE_PATH, {}))
    return discovery_state_from(migrate_scanner_state(load_json(STATE_PATH, {})))


def save_discovery_state(state, config, observed_at=None):
    state["_runtime"] = next_runtime_metadata(
        state,
        load_json(DISCOVERY_STATE_PATH, {}),
        "discovery_pulse",
        observed_at,
    )
    save_json(
        DISCOVERY_STATE_PATH,
        state,
        compact=bool(config.get("state_json_compact", True)),
    )
    return state["_runtime"]


def merge_discovery_state(state, discovery_state):
    """Merge only newer discovery-owned records into a deep-scan state snapshot."""
    if not isinstance(state, dict) or not isinstance(discovery_state, dict):
        return {"market": 0, "baselines": 0, "queue": 0}
    market = state.setdefault("market", {})
    baselines = state.setdefault("activity_baselines", {})
    merged_market = 0
    merged_baselines = 0
    for token, incoming in (discovery_state.get("market") or {}).items():
        if not isinstance(incoming, dict):
            continue
        existing = market.get(token) or {}
        if parse_timestamp(incoming.get("latest_seen_at")) > parse_timestamp(
            existing.get("latest_seen_at")
        ):
            market[token] = copy.deepcopy(incoming)
            merged_market += 1
    for token, incoming in (discovery_state.get("activity_baselines") or {}).items():
        if not isinstance(incoming, dict):
            continue
        existing = baselines.get(token) or {}
        if int(incoming.get("last_snapshot_at") or 0) > int(
            existing.get("last_snapshot_at") or 0
        ):
            baselines[token] = copy.deepcopy(incoming)
            merged_baselines += 1

    queue_by_key = {}
    for item in [*(state.get("discovery_queue") or []), *(discovery_state.get("discovery_queue") or [])]:
        if not isinstance(item, dict):
            continue
        key = item.get("pool_address") or item.get("token_address")
        if not key:
            continue
        existing = queue_by_key.get(key)
        if not existing or parse_timestamp(item.get("observed_at")) >= parse_timestamp(
            existing.get("observed_at")
        ):
            queue_by_key[key] = copy.deepcopy(item)
    state["discovery_queue"] = sorted(
        queue_by_key.values(),
        key=lambda item: (
            bool(item.get("reactivation_confirmed")),
            float(item.get("activity_score") or 0),
            parse_timestamp(item.get("observed_at")),
        ),
        reverse=True,
    )
    remote_updates = state.setdefault("remote_discovery_updated_at", {})
    for token, updated_at in (discovery_state.get("remote_discovery_updated_at") or {}).items():
        if parse_timestamp(updated_at) > parse_timestamp(remote_updates.get(token)):
            remote_updates[token] = updated_at
    return {
        "market": merged_market,
        "baselines": merged_baselines,
        "queue": len(state["discovery_queue"]),
    }


def write_jsonl(path, records):
    atomic_write_text(
        path,
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
    )


def write_scanner_status(status, error=None, scan_health=None):
    previous = load_json(SCANNER_STATUS_PATH, {})
    now = utc_now().isoformat().replace("+00:00", "Z")
    payload = {
        "status": status,
        "last_attempt_at": now,
        "last_success_at": previous.get("last_success_at"),
        "error": str(error or "")[:500] or None,
        "scan_health": scan_health or {},
    }
    if status == "ok":
        payload["last_success_at"] = now
    save_json(SCANNER_STATUS_PATH, payload)
    return payload


def scanner_failure_class(status_payload, exit_code):
    """Classify a failed run without hiding programming or invariant failures."""
    if int(exit_code or 0) == 0:
        return "success"
    payload = status_payload if isinstance(status_payload, dict) else {}
    health = payload.get("scan_health") or {}
    categories = {
        str(name)
        for name, count in (health.get("scan_error_categories") or {}).items()
        if count
    }
    soft_categories = {"rpc_all_unavailable"}
    soft_suffixes = ("_rate_limit", "_quota", "_auth", "_circuit_open")
    if categories and all(
        category in soft_categories or category.endswith(soft_suffixes)
        for category in categories
    ):
        return "soft_provider_failure"
    error = str(payload.get("error") or "").lower()
    soft_markers = (
        "all rpc provider",
        "rate limit",
        "rate_limit",
        "quota",
        "credit",
        "timed out",
        "timeout",
        "http 429",
        "http 401",
        "http 403",
        "unauthorized",
        "forbidden",
    )
    if any(marker in error for marker in soft_markers):
        return "soft_provider_failure"
    return "hard_failure"


def effective_config_version(config):
    """Return a stable, non-secret fingerprint for the configuration that ran."""
    public_config = {
        key: value
        for key, value in (config or {}).items()
        if not str(key).startswith("_")
    }
    encoded = json.dumps(public_config, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()[:12]}"


def write_discovery_status(status, payload=None, error=None):
    previous = load_json(DISCOVERY_STATUS_PATH, {})
    now = utc_now().isoformat().replace("+00:00", "Z")
    body = {
        "status": status,
        "last_attempt_at": now,
        "last_success_at": previous.get("last_success_at"),
        "error": str(error or "")[:500] or None,
        **(payload or {}),
    }
    if status == "ok":
        body["last_success_at"] = now
    save_json(DISCOVERY_STATUS_PATH, body)
    return body


def remote_data_url_from_env():
    for key in ("RADAR_DATA_API_URL", "SOLANA_RADAR_DATA_API_URL"):
        value = os.environ.get(key)
        if value:
            return value.rstrip("/")
    return None


def remote_sync_required(config):
    raw = os.environ.get("RADAR_REMOTE_SYNC_REQUIRED")
    if raw is None:
        raw = config.get("remote_sync_required", False)
    return str(raw).strip().lower() in {"1", "true", "yes", "required"}


def remote_ingest_secret():
    return os.environ.get("RADAR_INGEST_SECRET") or os.environ.get("RADAR_REMOTE_INGEST_SECRET")


def compact_alert_for_dashboard(alert):
    """Keep list-level alert facts while deferring event-level evidence to D1."""
    if not isinstance(alert, dict):
        return {}
    detail_fields = {
        "events",
        "common_funders",
        "common_recipients",
        "common_executors",
    }
    compact = {
        key: value
        for key, value in alert.items()
        if key not in detail_fields
    }
    for field in detail_fields:
        value = alert.get(field)
        if isinstance(value, list):
            compact[f"{field}_count"] = len(value)
    wave = alert.get("wave")
    if isinstance(wave, dict):
        compact_wave = {
            key: value
            for key, value in wave.items()
            if key != "top_buyers"
        }
        top_buyers = wave.get("top_buyers")
        if isinstance(top_buyers, list):
            compact_wave["top_buyers_count"] = len(top_buyers)
        compact["wave"] = compact_wave
    return compact


def compact_signal_thesis_for_dashboard(thesis):
    """Keep thesis state in the list response, but not the wallet-by-wallet cohort."""
    if not isinstance(thesis, dict):
        return {}
    return {
        key: value
        for key, value in thesis.items()
        if key not in {"cohort", "cohort_wallets"}
    }


def compact_report_for_remote(report):
    pool_fields = {
        "pool_address",
        "token_address",
        "symbol",
        "name",
        "dex",
        "url",
        "pair_created_at",
        "mcap_usd",
        "price_usd",
        "liquidity_usd",
        "volume_5m_usd",
        "volume_1h_usd",
        "txns_5m",
        "txns_1h",
        "age_hours",
        "latest_mcap_usd",
        "latest_price_usd",
        "latest_liquidity_usd",
        "latest_seen_at",
        "market_snapshot_at",
        "market_snapshot_stale",
        "market_snapshot_error",
        "market_snapshot_checked_at",
        "market_source",
        "scan_mcap_usd",
        "scan_price_usd",
        "scan_liquidity_usd",
        "scan_mcap_at",
        "first_signal_at",
        "first_obs_mcap_usd",
        "first_obs_price_usd",
        "first_obs_liquidity_usd",
        "first_obs_mcap_at",
        "first_obs_lane",
        "first_obs_score",
        "ath_mcap_usd",
        "ath_mcap_at",
        "ath_price_usd",
        "ath_pool_address",
        "ath_source",
        "ath_status",
        "ath_validation_status",
        "ath_latest_checked_at",
        "ath_verified_at",
    }

    def compact_pool(pool):
        return {
            key: value
            for key, value in (pool or {}).items()
            if key in pool_fields
        }

    compact = {
        key: value
        for key, value in report.items()
        if key not in {"universe", "active_pools", "summaries"}
    }
    compact["universe"] = []
    compact["active_pools"] = []
    compact["summaries"] = [
        {
            "pool": compact_pool(summary.get("pool") or {}),
            "error": summary.get("error"),
            "scan_failed": bool(summary.get("scan_failed")),
        }
        for summary in report.get("summaries", [])
    ]
    compact["alerts"] = [
        compact_alert_for_dashboard(alert)
        for alert in report.get("alerts", [])
        if isinstance(alert, dict)
    ]
    compact["signal_theses"] = [
        compact_signal_thesis_for_dashboard(thesis)
        for thesis in report.get("signal_theses", [])
        if isinstance(thesis, dict)
    ]
    compact["remote_compact"] = True
    return compact


def dashboard_snapshot_token_keys(report, history):
    """Return the small, user-visible token set allowed into a dashboard snapshot."""
    keys = []
    seen = set()

    def add(value):
        token = clean_solana_address(value)
        if token and token not in seen:
            seen.add(token)
            keys.append(token)

    def add_pool(pool):
        if not isinstance(pool, dict):
            return
        add(pool.get("token_address"))

    for thesis in report.get("signal_theses") or []:
        if isinstance(thesis, dict):
            add(thesis.get("token_address"))
    for alert in [*(report.get("alerts") or []), *(history or [])]:
        if isinstance(alert, dict):
            add_pool(alert.get("pool") or {})
    for section in ("summaries", "active_pools", "universe"):
        for item in report.get(section) or []:
            add_pool(item.get("pool") if isinstance(item, dict) and "pool" in item else item)
    return keys


def compact_market_for_dashboard(entry):
    """Expose market facts only; runtime cursors and wallet caches stay private."""
    market_fields = {
        "token_address",
        "pool_address",
        "symbol",
        "name",
        "dex",
        "url",
        "pair_created_at",
        "pair_created_at_iso",
        "latest_mcap_usd",
        "latest_price_usd",
        "latest_liquidity_usd",
        "latest_volume_5m_usd",
        "latest_volume_1h_usd",
        "latest_volume_24h_usd",
        "latest_txns_5m",
        "latest_txns_1h",
        "latest_seen_at",
        "market_snapshot_at",
        "current_market_verified_at",
        "market_snapshot_stale",
        "market_snapshot_error",
        "market_snapshot_checked_at",
        "market_source",
        "scan_mcap_usd",
        "scan_price_usd",
        "scan_liquidity_usd",
        "scan_mcap_at",
        "scan_source",
        "first_signal_at",
        "first_obs_mcap_usd",
        "first_obs_price_usd",
        "first_obs_liquidity_usd",
        "first_obs_mcap_at",
        "first_obs_source",
        "first_obs_lane",
        "first_obs_score",
        "caught_obs_mcap_usd",
        "caught_obs_price_usd",
        "caught_obs_liquidity_usd",
        "caught_obs_mcap_at",
        "ath_mcap_usd",
        "ath_mcap_at",
        "ath_price_usd",
        "ath_pool_address",
        "ath_source",
        "ath_status",
        "ath_validation_status",
        "ath_error",
        "ath_error_checked_at",
        "ath_current_ratio",
        "ath_drawdown_pct",
        "ath_filter_checked_at",
        "ath_latest_checked_at",
        "ath_verified_at",
    }
    return {
        key: value
        for key, value in (entry or {}).items()
        if key in market_fields
    }


HISTORY_LEDGER_SCHEMA_VERSION = 1
HISTORY_LEDGER_HORIZONS = ("1h", "6h", "24h", "72h", "7d")


def history_ledger_event_id(episode_id, event_type, observed_at):
    # Prefix the immutable event id with its observation time. The Worker uses
    # this for deterministic chronological delivery when it first backfills a
    # ledger, preventing a later result from becoming a prior score.
    observed_epoch = max(0, int(parse_timestamp(observed_at) or 0))
    payload = "|".join(
        str(value or "") for value in (episode_id, event_type, observed_at)
    )
    return (
        f"history:{observed_epoch:010d}:"
        + hashlib.sha256(payload.encode()).hexdigest()[:24]
    )


def history_ledger_alert_index(alerts):
    index = defaultdict(list)
    for alert in alerts or []:
        if not isinstance(alert, dict) or alert.get("lane") != "reactivation":
            continue
        pool = alert.get("pool") or {}
        for key in (pool.get("token_address"), pool.get("pool_address")):
            if key:
                index[str(key)].append(alert)
    return index


def history_ledger_matching_alert(thesis, alert_index):
    candidates = []
    for key in (thesis.get("token_address"), thesis.get("pool_address")):
        candidates.extend(alert_index.get(str(key), []))
    if not candidates:
        return None
    signal_at = parse_timestamp(thesis.get("signal_at"))
    return min(
        candidates,
        key=lambda alert: abs(alert_history_timestamp(alert) - signal_at),
    )


def history_ledger_age_days(thesis):
    signal_at = parse_timestamp(thesis.get("signal_at"))
    created_at = parse_timestamp(thesis.get("pair_created_at"))
    if not signal_at or not created_at or signal_at < created_at:
        return None
    return (signal_at - created_at) / 86400


def history_ledger_wallets(thesis, market_entry, at_catch=False):
    """Return evidence-based wallet rows for the historical D1 ledger.

    A balance decrease only establishes that assets left the original wallet.
    It deliberately remains `reduced_unverified`; detecting a sale or transfer
    requires transaction-level attribution and is not inferred here.
    """
    cohort = thesis.get("cohort") or []
    supply = max(0.0, to_float(thesis.get("supply")))
    entry_price = to_float(thesis.get("signal_price_usd"))
    current_price = to_float(
        (market_entry or {}).get("latest_price_usd")
        or (market_entry or {}).get("scan_price_usd")
    )
    estimated_pnl_pct = (
        (current_price / entry_price - 1) * 100
        if current_price > 0 and entry_price > 0 and not at_catch
        else 0.0 if at_catch and entry_price > 0 else None
    )
    coverage = float(thesis.get("balance_coverage_pct") or 0)
    rows = []
    for row in cohort:
        if not isinstance(row, dict) or not row.get("owner"):
            continue
        bought_tokens = max(0.0, to_float(row.get("attributed_tokens")))
        held_at_catch = max(
            0.0,
            to_float(row.get("initial_balance") or row.get("attributed_tokens")),
        )
        checked_balance = row.get("current_balance")
        current_balance = (
            held_at_catch
            if at_catch
            else (
                max(0.0, to_float(checked_balance))
                if checked_balance is not None
                else None
            )
        )
        retained_pct = (
            100.0
            if at_catch and bought_tokens > 0
            else (
                max(0.0, min(100.0, to_float(row.get("retention_pct"))))
                if row.get("retention_pct") is not None
                else None
            )
        )
        if at_catch:
            behavior_status = "holding"
            coverage_status = "complete"
        elif current_balance is None or coverage < 80:
            behavior_status = "unknown"
            coverage_status = "partial"
        elif retained_pct is not None and retained_pct >= 99:
            behavior_status = "holding"
            coverage_status = "complete"
        else:
            behavior_status = "reduced_unverified"
            coverage_status = "complete"
        rows.append(
            {
                "wallet_address": row.get("owner"),
                "cohort_role": "at_catch",
                "wallet_class_at_signal": row.get("wallet_class"),
                "first_buy_at": iso(row.get("first_buy_time")),
                "last_buy_at": iso(row.get("first_buy_time")),
                "buy_count": 1,
                "buy_sol": max(0.0, to_float(row.get("buy_sol"))),
                "bought_tokens": bought_tokens,
                "average_entry_price": entry_price or None,
                "entry_mcap_usd": to_float(thesis.get("signal_mcap_usd")) or None,
                "supply_pct_bought": (
                    bought_tokens / supply * 100 if supply and bought_tokens else None
                ),
                "held_tokens_at_catch": held_at_catch or None,
                "held_supply_pct_at_catch": (
                    held_at_catch / supply * 100 if supply and held_at_catch else None
                ),
                "retained_pct_at_catch": 100.0 if bought_tokens else None,
                "common_funder": row.get("common_funder"),
                "common_executor": row.get("common_executor"),
                "evidence_status": "complete" if at_catch or coverage >= 80 else "partial",
                "current_token_balance": current_balance,
                "balance_retained_pct": retained_pct,
                "behavior_status": behavior_status,
                "estimated_pnl_pct": estimated_pnl_pct,
                # A net balance cannot prove a sale. Transaction-level work is
                # intentionally required before either field is populated.
                "additional_buy_tokens": None,
                "outbound_transfer_tokens": None,
                "coverage_status": coverage_status,
            }
        )
    return rows


def build_history_ledger(report_payload, state, config, generated_at):
    """Build immutable analytics events for the remote history database.

    This contract is only added to authenticated remote-sync payloads. It never
    reaches the static GitHub Pages fallback, which keeps private wallet detail
    out of the public emergency artifact.
    """
    if not config.get("history_ledger_enabled", True):
        return {"schema_version": HISTORY_LEDGER_SCHEMA_VERSION, "events": []}

    deleted = load_deleted_tokens()
    alert_index = history_ledger_alert_index(report_payload.get("alerts") or [])
    market = state.get("market") if isinstance(state, dict) else {}
    outcomes = state.get("signal_outcomes") if isinstance(state, dict) else {}
    pools = state.get("pools") if isinstance(state, dict) else {}
    candidates = []
    for pool_state in pools.values() if isinstance(pools, dict) else []:
        thesis = pool_state.get("signal_thesis") if isinstance(pool_state, dict) else None
        if not isinstance(thesis, dict):
            continue
        token = thesis.get("token_address") or thesis.get("pool_address")
        if not token or token in deleted.get("tokens", set()) or thesis.get("pool_address") in deleted.get("pools", set()):
            continue
        candidates.append(thesis)
    candidates.sort(
        key=lambda thesis: parse_timestamp(
            thesis.get("signal_at") or thesis.get("captured_at")
        ),
        reverse=True,
    )
    limit = max(0, int(config.get("history_ledger_max_theses", 120)))
    if limit:
        candidates = candidates[:limit]

    events = []
    for thesis in candidates:
        token = str(thesis.get("token_address") or thesis.get("pool_address"))
        pool_address = thesis.get("pool_address")
        caught_at = (
            thesis.get("signal_at")
            or thesis.get("captured_at")
            or generated_at
        )
        market_entry = market.get(token) if isinstance(market, dict) else {}
        market_entry = market_entry if isinstance(market_entry, dict) else {}
        matching_alert = history_ledger_matching_alert(thesis, alert_index)
        quality = ((matching_alert or {}).get("data_quality") or {}).get("status")
        if not quality:
            quality = "complete" if float(thesis.get("balance_coverage_pct") or 0) >= 80 else "partial"
        ath_mcap = trusted_ath_mcap(market_entry)
        caught_mcap = to_float(thesis.get("signal_mcap_usd"))
        episode_id = "episode:" + hashlib.sha256(
            "|".join(
                str(value or "")
                for value in (token, thesis.get("signal_family"), caught_at)
            ).encode()
        ).hexdigest()[:24]
        episode = {
            "episode_id": episode_id,
            "token_address": thesis.get("token_address") or token,
            "pool_address": pool_address,
            "symbol": thesis.get("symbol"),
            "name": thesis.get("name"),
            "lane": "reactivation",
            "signal_family": thesis.get("signal_family") or "reactivation_wave",
            "caught_at": caught_at,
            "last_signal_at": thesis.get("last_signal_at") or caught_at,
            "closed_at": thesis.get("invalidated_at"),
            "caught_tier": thesis.get("source_tier"),
            "caught_score": thesis.get("source_score"),
            "caught_price_usd": thesis.get("signal_price_usd"),
            "caught_mcap_usd": caught_mcap or None,
            "caught_liquidity_usd": thesis.get("signal_liquidity_usd"),
            "token_age_days": history_ledger_age_days(thesis),
            "ath_mcap_usd": ath_mcap or None,
            "ath_ratio": caught_mcap / ath_mcap if caught_mcap and ath_mcap else None,
            "market_stage": (matching_alert or {}).get("reactivation_stage"),
            "source_run_key": generated_at,
            "source_kind": "scanner_snapshot",
            "data_quality_status": quality,
            "schema_version": HISTORY_LEDGER_SCHEMA_VERSION,
        }
        outcome = outcomes.get(token, {}) if isinstance(outcomes, dict) else {}
        signal_wallets = history_ledger_wallets(thesis, market_entry, at_catch=True)
        signal_event = {
            "event_id": history_ledger_event_id(episode_id, "signal", caught_at),
            "episode": episode,
            "event": {
                "event_type": "signal",
                "observed_at": caught_at,
                "tier": thesis.get("source_tier"),
                "score": thesis.get("source_score"),
                "price_usd": thesis.get("signal_price_usd"),
                "mcap_usd": caught_mcap or None,
                "liquidity_usd": thesis.get("signal_liquidity_usd"),
                "retained_supply_pct": thesis.get("original_retained_supply_pct"),
                "cohort_retained_pct": 100.0,
                "thesis_status": "captured",
                "data_quality_status": quality,
            },
            "wallets": signal_wallets,
            "outcome": {},
        }
        events.append(signal_event)

        checked_at = thesis.get("last_checked_at")
        if parse_timestamp(checked_at) > parse_timestamp(caught_at):
            events.append(
                {
                    "event_id": history_ledger_event_id(
                        episode_id, "retention_check", checked_at
                    ),
                    "episode": episode,
                    "event": {
                        "event_type": "retention_check",
                        "observed_at": checked_at,
                        "tier": thesis.get("source_tier"),
                        "score": thesis.get("source_score"),
                        "price_usd": market_entry.get("latest_price_usd"),
                        "mcap_usd": market_entry.get("latest_mcap_usd"),
                        "liquidity_usd": market_entry.get("latest_liquidity_usd"),
                        "retained_supply_pct": thesis.get("current_retained_supply_pct"),
                        "cohort_retained_pct": thesis.get("token_retention_pct"),
                        "thesis_status": thesis.get("status"),
                        "data_quality_status": quality,
                    },
                    "wallets": history_ledger_wallets(
                        thesis, market_entry, at_catch=False
                    ),
                    "outcome": {},
                }
            )

        horizons = outcome.get("horizons") if isinstance(outcome, dict) else {}
        for horizon in HISTORY_LEDGER_HORIZONS:
            checkpoint = horizons.get(horizon) if isinstance(horizons, dict) else None
            checkpoint_at = checkpoint.get("at") if isinstance(checkpoint, dict) else None
            if not checkpoint_at:
                continue
            events.append(
                {
                    "event_id": history_ledger_event_id(
                        episode_id, f"outcome_{horizon}", checkpoint_at
                    ),
                    "episode": episode,
                    "event": {
                        "event_type": f"outcome_{horizon}",
                        "observed_at": checkpoint_at,
                        "tier": thesis.get("source_tier"),
                        "score": thesis.get("source_score"),
                        "price_usd": checkpoint.get("price_usd"),
                        "mcap_usd": checkpoint.get("mcap_usd"),
                        "liquidity_usd": checkpoint.get("liquidity_usd"),
                        "thesis_status": thesis.get("status"),
                        "data_quality_status": quality,
                    },
                    # A market horizon does not imply a fresh wallet balance
                    # check, so no stale wallet observation is written here.
                    "wallets": [],
                    "outcome": outcome,
                }
            )

    events.sort(
        key=lambda row: (
            parse_timestamp((row.get("event") or {}).get("observed_at")) or 0,
            str(row.get("event_id") or ""),
        )
    )
    return {
        "schema_version": HISTORY_LEDGER_SCHEMA_VERSION,
        "generated_at": generated_at,
        "events": events,
    }


def build_dashboard_snapshot(
    report_payload,
    state,
    config,
    scan_status=None,
    history_limit=None,
    include_detail=False,
):
    """Build the D1/Pages contract without publishing scanner runtime state."""
    if history_limit is None:
        history_limit = config.get("remote_sync_alert_history_limit", 40)
    history_limit = max(0, int(history_limit))
    detail_history = load_alert_history()[-history_limit:] if history_limit else []
    history = [compact_alert_for_dashboard(alert) for alert in detail_history]
    token_keys = dashboard_snapshot_token_keys(report_payload, history)
    market = state.get("market") if isinstance(state, dict) else {}
    compact_market = {
        token: compact_market_for_dashboard(market.get(token))
        for token in token_keys
        if isinstance(market, dict) and isinstance(market.get(token), dict)
    }
    generated_at = report_payload.get("generated_at") or utc_now().isoformat().replace("+00:00", "Z")
    fallback_scan_status = {
        "status": "ok",
        "last_attempt_at": generated_at,
        "last_success_at": generated_at,
        "error": None,
        "scan_health": ((report_payload.get("stats") or {}).get("scan_health") or {}),
    }
    snapshot = {
        "schema_version": 1,
        "generated_at": generated_at,
        "report": compact_report_for_remote(report_payload),
        "history": history,
        "market": compact_market,
        "deleted_tokens": load_json(
            DELETED_TOKENS_PATH,
            {"tokens": [], "pools": [], "entries": {}, "updated_at": None},
        ),
        "scan_status": scan_status or fallback_scan_status,
        "discovery_status": load_json(DISCOVERY_STATUS_PATH, {}),
    }
    if include_detail:
        snapshot["detail_signal_theses"] = [
            thesis
            for thesis in report_payload.get("signal_theses", [])
            if isinstance(thesis, dict)
        ]
        snapshot["detail_current_alerts"] = [
            alert
            for alert in report_payload.get("alerts", [])
            if isinstance(alert, dict)
        ]
        snapshot["detail_history"] = detail_history
        snapshot["history_ledger"] = build_history_ledger(
            report_payload,
            state,
            config,
            generated_at,
        )
    return snapshot


def write_dashboard_fallback(report_payload, state, config):
    """Persist the compact GitHub Pages fallback next to the normal report."""
    # The Pages file is an emergency fallback. Full raw history stays in D1,
    # which avoids putting a growing onchain event log into Git on every scan.
    snapshot = build_dashboard_snapshot(
        report_payload,
        state,
        config,
        history_limit=config.get("dashboard_fallback_alert_history_limit", 6),
    )
    save_json(DASHBOARD_FALLBACK_PATH, snapshot, compact=True)
    return snapshot


def remote_api_call(method, path, config, payload=None, params=None):
    base_url = remote_data_url_from_env()
    if not base_url:
        raise RuntimeError("RADAR_DATA_API_URL is missing")
    secret = remote_ingest_secret()
    if not secret:
        raise RuntimeError("RADAR_INGEST_SECRET is missing")
    headers = {
        "Content-Type": "application/json",
        "accept": "application/json",
        "x-radar-ingest-secret": secret,
    }
    response = requests.request(
        method,
        f"{base_url}{path}",
        headers=headers,
        json=payload,
        params=params,
        timeout=int(config.get("remote_sync_timeout_seconds", 25)),
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or f"unexpected remote response: {result}")
    return result


def sync_remote_snapshot(report_payload, state, config):
    base_url = remote_data_url_from_env()
    if not base_url:
        return
    secret = remote_ingest_secret()
    if not secret:
        message = "Remote sync skipped: RADAR_INGEST_SECRET is missing"
        if remote_sync_required(config):
            raise RuntimeError(message)
        print(message, file=sys.stderr)
        return

    body = build_dashboard_snapshot(report_payload, state, config, include_detail=True)
    try:
        result = remote_api_call("POST", "/api/ingest/snapshot", config, body)
        print(
            "Remote sync: "
            f"{result.get('alerts_synced', len(body.get('history') or []))} alerts, "
            f"generated_at={result.get('generated_at') or report_payload.get('generated_at')}"
        )
    except Exception as exc:
        if remote_sync_required(config):
            raise
        print(f"Remote sync failed: {exc}", file=sys.stderr)


def sync_remote_discovery_status(status, config):
    if not remote_data_url_from_env() or not remote_ingest_secret():
        return {}
    try:
        return remote_api_call(
            "POST",
            "/api/discovery/status",
            config,
            {"status": status},
        ) or {}
    except Exception as exc:
        print(f"Remote discovery status sync failed: {exc}", file=sys.stderr)
        return {"ok": False, "error": str(exc)[:300]}


def sync_remote_scan_status(status, config):
    """Persist a successful or failed scanner attempt without exposing runtime state."""
    if not remote_data_url_from_env() or not remote_ingest_secret():
        return {}
    try:
        return remote_api_call(
            "POST",
            "/api/scan/status",
            config,
            {"status": status},
        ) or {}
    except Exception as exc:
        # The main scan result is still usable through the Pages fallback. A
        # status-sync outage must not turn a completed scan into a hard failure.
        print(f"Remote scanner status sync failed: {exc}", file=sys.stderr)
        return {"ok": False, "error": str(exc)[:300]}


def load_remote_discovery_state(state, config):
    if not config.get("remote_discovery_state_enabled", True):
        return {}
    if not remote_data_url_from_env() or not remote_ingest_secret():
        return {}
    rows = []
    cursor = None
    max_pages = max(
        1,
        int(config.get("remote_discovery_load_max_pages", 10)),
    )
    page_size = max(
        1,
        min(100, int(config.get("remote_discovery_page_size", 50))),
    )
    try:
        for _page in range(max_pages):
            result = remote_api_call(
                "GET",
                "/api/discovery/state",
                config,
                params={"limit": page_size, "cursor": cursor or ""},
            ) or {}
            rows.extend(result.get("rows") or [])
            if result.get("is_done"):
                break
            cursor = result.get("next_cursor")
            if not cursor:
                break
    except Exception as exc:
        if remote_sync_required(config):
            raise
        print(f"Remote discovery load failed: {exc}", file=sys.stderr)
        return {"status": "error", "error": str(exc)[:300]}

    market = state.setdefault("market", {})
    baselines = state.setdefault("activity_baselines", {})
    outcomes = state.setdefault("signal_outcomes", {})
    queue_by_token = {
        item.get("token_address"): item
        for item in state.get("discovery_queue", []) or []
        if isinstance(item, dict) and item.get("token_address")
    }
    remote_updates = state.setdefault("remote_discovery_updated_at", {})
    merged_market = 0
    merged_baselines = 0
    merged_queue = 0
    merged_outcomes = 0
    now = int(time.time())
    for row in rows:
        if not isinstance(row, dict):
            continue
        token = clean_solana_address(row.get("tokenKey"))
        if not token:
            continue
        if row.get("updatedAt"):
            remote_updates[token] = row.get("updatedAt")
        remote_market = row.get("market")
        if isinstance(remote_market, dict):
            local_market = market.get(token) or {}
            if parse_timestamp(remote_market.get("latest_seen_at")) > parse_timestamp(
                local_market.get("latest_seen_at")
            ):
                market[token] = remote_market
                merged_market += 1
        remote_baseline = row.get("baseline")
        if isinstance(remote_baseline, dict):
            local_baseline = baselines.get(token) or {}
            if int(remote_baseline.get("last_snapshot_at") or 0) > int(
                local_baseline.get("last_snapshot_at") or 0
            ):
                baselines[token] = remote_baseline
                merged_baselines += 1
        remote_queue = row.get("queue")
        if (
            isinstance(remote_queue, dict)
            and parse_timestamp(remote_queue.get("expires_at")) > now
        ):
            existing = queue_by_token.get(token) or {}
            if parse_timestamp(remote_queue.get("detected_at")) >= parse_timestamp(
                existing.get("detected_at")
            ):
                queue_by_token[token] = remote_queue
                merged_queue += 1
        remote_outcome = row.get("outcome")
        if isinstance(remote_outcome, dict):
            local_outcome = outcomes.get(token) or {}
            if parse_timestamp(remote_outcome.get("updated_at")) > parse_timestamp(
                local_outcome.get("updated_at")
            ):
                outcomes[token] = remote_outcome
                merged_outcomes += 1
    state["discovery_queue"] = sorted(
        queue_by_token.values(),
        key=lambda item: (
            float(item.get("score") or 0),
            parse_timestamp(item.get("detected_at")),
        ),
        reverse=True,
    )[: int(config.get("discovery_queue_max_tokens", 250))]
    stats = {
        "status": "ok",
        "rows": len(rows),
        "market_merged": merged_market,
        "baselines_merged": merged_baselines,
        "queue_merged": merged_queue,
        "outcomes_merged": merged_outcomes,
    }
    state.setdefault("maintenance", {})["remote_discovery_load"] = stats
    return stats


def sync_remote_discovery_state(state, pools, config, observed_at):
    if not config.get("remote_discovery_state_enabled", True):
        return {}
    if not remote_data_url_from_env() or not remote_ingest_secret():
        return {}
    market = state.get("market") or {}
    baselines = state.get("activity_baselines") or {}
    outcomes = state.get("signal_outcomes") or {}
    queue_by_token = {
        item.get("token_address"): item
        for item in state.get("discovery_queue", []) or []
        if isinstance(item, dict) and item.get("token_address")
    }
    remote_updates = state.setdefault("remote_discovery_updated_at", {})
    now = parse_timestamp(observed_at) or int(time.time())
    full_sync_interval = max(
        300,
        int(
            float(
                config.get(
                    "remote_discovery_full_sync_interval_minutes",
                    60,
                )
            )
            * 60
        ),
    )
    hot_volume = float(
        config.get(
            "remote_discovery_hot_volume_5m_usd",
            config.get("discovery_queue_min_volume_5m_usd", 500),
        )
    )
    hot_txns = int(
        config.get(
            "remote_discovery_hot_txns_5m",
            config.get("discovery_queue_min_txns_5m", 5),
        )
    )
    rows = []
    seen = set()
    for pool in pools or []:
        token = clean_solana_address(pool.token_address or pool.pool_address)
        if not token or token in seen:
            continue
        seen.add(token)
        baseline = baselines.get(token) or {}
        outcome = outcomes.get(token) or {}
        queue = queue_by_token.get(token)
        remote_updated_at = parse_timestamp(remote_updates.get(token))
        full_sync_due = (
            not remote_updated_at
            or now - remote_updated_at >= full_sync_interval
        )
        hot = bool(
            queue
            or baseline.get("reactivation_confirmed")
            or float(pool.volume_5m_usd or 0) >= hot_volume
            or int(pool.txns_5m or 0) >= hot_txns
        )
        outcome_updated = parse_timestamp(outcome.get("updated_at"))
        if not full_sync_due and not hot and outcome_updated < now - 60:
            continue
        rows.append(
            {
                "tokenKey": token,
                "poolAddress": pool.pool_address or None,
                "market": market.get(token) or None,
                "baseline": baseline or None,
                "queue": queue or None,
                "outcome": outcome or None,
                "updatedAt": observed_at,
            }
        )
    batch_size = max(
        1,
        min(50, int(config.get("remote_discovery_sync_batch_size", 20))),
    )
    synced = 0
    try:
        for batch in chunked(rows, batch_size):
            result = remote_api_call(
                "POST",
                "/api/discovery/state",
                config,
                {"rows": batch},
            ) or {}
            synced += int(result.get("rows_synced") or len(batch))
            for row in batch:
                remote_updates[row["tokenKey"]] = observed_at
    except Exception as exc:
        if remote_sync_required(config):
            raise
        print(f"Remote discovery sync failed: {exc}", file=sys.stderr)
        return {"status": "error", "error": str(exc)[:300], "rows_synced": synced}
    stats = {
        "status": "ok",
        "rows_synced": synced,
        "rows": len(rows),
        "candidate_rows": len(seen),
        "full_sync_interval_minutes": full_sync_interval // 60,
    }
    state.setdefault("maintenance", {})["remote_discovery_sync"] = stats
    return stats


def to_float(value, default=0.0):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def chunked(items, size):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def rpc_token_amount(amount_info):
    amount_info = amount_info or {}
    amount = amount_info.get("amount")
    decimals = int(amount_info.get("decimals") or 0)
    if amount is not None:
        try:
            return int(amount) / (10**decimals)
        except (TypeError, ValueError):
            pass
    return to_float(amount_info.get("uiAmount"))


@dataclass
class Pool:
    pool_address: str
    token_address: str = ""
    symbol: str = ""
    name: str = ""
    dex: str = ""
    source: str = ""
    url: str = ""
    mcap_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_5m_usd: float = 0.0
    volume_1h_usd: float = 0.0
    volume_24h_usd: float = 0.0
    price_usd: float = 0.0
    txns_5m: int = 0
    txns_1h: int = 0
    pair_created_at: int = 0
    market_snapshot_at: int = 0
    market_snapshot_stale: bool = False

    def key(self):
        return self.pool_address

    def age_hours(self):
        if not self.pair_created_at:
            return None
        return max(0.0, (utc_now().timestamp() - self.pair_created_at) / 3600)

    def as_dict(self):
        age_hours = self.age_hours()
        payload = {
            "pool_address": self.pool_address,
            "token_address": self.token_address,
            "symbol": self.symbol,
            "name": self.name,
            "dex": self.dex,
            "source": self.source,
            "url": self.url,
            "mcap_usd": self.mcap_usd,
            "liquidity_usd": self.liquidity_usd,
            "volume_5m_usd": self.volume_5m_usd,
            "volume_1h_usd": self.volume_1h_usd,
            "volume_24h_usd": self.volume_24h_usd,
            "price_usd": self.price_usd,
            "txns_5m": self.txns_5m,
            "txns_1h": self.txns_1h,
            "pair_created_at": self.pair_created_at,
            "pair_created_at_iso": iso(self.pair_created_at),
            "age_hours": age_hours,
            "market_snapshot_at": self.market_snapshot_at,
            "market_snapshot_at_iso": iso(self.market_snapshot_at),
            "market_snapshot_stale": self.market_snapshot_stale,
        }
        baseline = getattr(self, "reactivation_baseline", None)
        if baseline:
            payload["reactivation_baseline"] = baseline
        return payload


class Http:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "solana-radar/0.1"})

    def get_json(self, url, params=None, headers=None, timeout=25):
        response = self.session.get(url, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()

    def post_json(self, url, body, headers=None, timeout=45):
        response = self.session.post(url, json=body, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()


class HeliusRpcError(RuntimeError):
    def __init__(self, method, category, detail, status=None, code=None, provider="helius"):
        self.method = method
        self.category = category
        self.status = status
        self.code = code
        self.provider = provider
        super().__init__(f"{provider}:{method}: {category}: {detail}")


class HeliusCircuitOpen(RuntimeError):
    pass


class WaveDataUnavailable(RuntimeError):
    pass


class RpcProvidersUnavailable(RuntimeError):
    pass


class EnhancedHistoryHeadMismatch(RuntimeError):
    pass


class SolanaRpcProvider:
    def __init__(
        self,
        provider_name,
        url,
        enhanced_history=False,
        credit_model="standard",
        timeout_seconds=30,
        transactions_timeout_seconds=25,
        max_retries=2,
        retry_base_seconds=0.75,
        retry_max_seconds=120,
        circuit_failure_threshold=4,
        min_interval_seconds=0,
        method_min_interval_seconds=None,
        unsupported_methods=None,
        credit_budget=0,
    ):
        self.provider_name = str(provider_name)
        self.url = str(url)
        self.enhanced_history = bool(enhanced_history)
        self.credit_model = str(credit_model)
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.calls = Counter()
        self.retries = Counter()
        self.estimated_credits = 0
        self.credit_budget = max(0, int(credit_budget or 0))
        self.latency_seconds = defaultdict(list)
        self.token_supply_cache = {}
        self.token_balance_cache = {}
        self.timeout_seconds = int(timeout_seconds)
        self.transactions_timeout_seconds = int(transactions_timeout_seconds)
        self.max_retries = max(0, int(max_retries))
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))
        self.retry_max_seconds = max(0.1, float(retry_max_seconds))
        self.circuit_failure_threshold = max(2, int(circuit_failure_threshold))
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.method_min_interval_seconds = {
            str(method): max(0.0, float(seconds))
            for method, seconds in (method_min_interval_seconds or {}).items()
        }
        self.unsupported_methods = {
            str(method) for method in (unsupported_methods or [])
        }
        self.last_request_started_at = None
        self.rate_limit_lock = threading.Lock()
        self.consecutive_failures = 0
        self.method_consecutive_failures = Counter()
        self.failures = Counter()
        self.last_error = None
        self.circuit_open_reason = None
        self.method_circuit_open_reasons = {}
        self.last_success_at = None

    def request_timeout(self, timeout):
        seconds = float(timeout if timeout is not None else self.timeout_seconds)
        connect_timeout = min(10.0, max(3.0, seconds / 3))
        return (connect_timeout, seconds)

    def retry_delay(self, attempt, retry_after=None):
        delay = retry_after or self.retry_base_seconds * (2**attempt)
        return min(self.retry_max_seconds, max(0.1, float(delay)))

    def wait_for_rate_slot(self, method):
        min_interval_seconds = max(
            self.min_interval_seconds,
            self.method_min_interval_seconds.get(str(method), 0.0),
        )
        if min_interval_seconds <= 0:
            return
        with self.rate_limit_lock:
            now = time.monotonic()
            if self.last_request_started_at is not None:
                wait_seconds = (
                    self.last_request_started_at + min_interval_seconds - now
                )
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
                    now += wait_seconds
            self.last_request_started_at = now

    def credit_cost(self, method, result):
        if self.credit_model == "helius":
            if method == "getTransactionsForAddress":
                data = result.get("data") if isinstance(result, dict) else None
                returned = len(data) if isinstance(data, list) else 0
                return max(10, ((returned + 99) // 100) * 10)
            if method == "getTransfersByAddress":
                return 10
            return 1
        if self.credit_model == "alchemy":
            return {
                "getAccountInfo": 10,
                "getBalance": 10,
                "getTokenAccountsByOwner": 10,
                "getHealth": 20,
                "getTokenAccountBalance": 20,
                "getTokenSupply": 20,
                "getSignaturesForAddress": 40,
                "getTransaction": 40,
                "getTransactionsForAddress": 100,
            }.get(method, 20)
        if self.credit_model == "chainstack":
            return 1
        if self.credit_model == "drpc":
            return 0 if method == "getHealth" else 20
        return 1

    def can_call(self, method):
        if (
            self.circuit_open_reason
            or method in self.method_circuit_open_reasons
            or method in self.unsupported_methods
        ):
            return False
        minimum_cost = self.credit_cost(method, None)
        return not self.credit_budget or (
            self.estimated_credits + minimum_cost <= self.credit_budget
        )

    def error_category(self, status=None, code=None, detail=""):
        detail = str(detail or "").lower()
        if status in (401, 403) or any(marker in detail for marker in ("unauthorized", "forbidden", "invalid api key")):
            return "auth"
        if status == 402 or code == 35 or any(
            marker in detail
            for marker in (
                "credit",
                "quota",
                "billing",
                "payment required",
                "max usage",
                "usage reached",
                "free plan",
                "please upgrade",
            )
        ):
            return "quota"
        if status == 429 or code in (-32429,) or "rate limit" in detail or "too many requests" in detail:
            return "rate_limit"
        if code == -32601 or "method is not available" in detail or "method not found" in detail:
            return "unsupported"
        if any(marker in detail for marker in ("timeout", "timed out", "temporarily unavailable")):
            return "temporary"
        if status and status >= 500:
            return "provider"
        return "rpc"

    def record_failure(self, error):
        category = getattr(error, "category", "rpc")
        method = str(getattr(error, "method", "unknown"))
        self.failures[category] += 1
        self.last_error = str(error)
        if category in {"auth", "quota"}:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.circuit_failure_threshold:
                self.circuit_open_reason = str(error)
        elif category in {"rate_limit", "provider", "temporary"}:
            self.method_consecutive_failures[method] += 1
            if (
                self.method_consecutive_failures[method]
                >= self.circuit_failure_threshold
            ):
                self.method_circuit_open_reasons[method] = str(error)

    def record_success(self, method):
        self.consecutive_failures = 0
        self.method_consecutive_failures[str(method)] = 0
        self.last_success_at = utc_now().isoformat().replace("+00:00", "Z")

    def call(self, method, params=None, timeout=None):
        if self.circuit_open_reason and method != "getHealth":
            raise HeliusCircuitOpen(f"{self.provider_name} circuit open: {self.circuit_open_reason}")
        if method in self.method_circuit_open_reasons:
            raise HeliusCircuitOpen(
                f"{self.provider_name} {method} circuit open: "
                f"{self.method_circuit_open_reasons[method]}"
            )
        minimum_cost = self.credit_cost(method, None)
        if (
            self.credit_budget
            and self.estimated_credits + minimum_cost > self.credit_budget
        ):
            raise HeliusRpcError(
                method,
                "quota",
                f"local per-scan credit budget {self.credit_budget} reached",
                provider=self.provider_name,
            )
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        retryable_statuses = {429, 500, 502, 503, 504}
        for attempt in range(self.max_retries + 1):
            self.wait_for_rate_slot(method)
            self.calls[method] += 1
            request_started = time.perf_counter()
            try:
                response = self.session.post(self.url, json=payload, timeout=self.request_timeout(timeout))
            except requests.RequestException as exc:
                self.latency_seconds[method].append(
                    time.perf_counter() - request_started
                )
                self.latency_seconds[method] = self.latency_seconds[method][-200:]
                if attempt >= self.max_retries:
                    error = HeliusRpcError(
                        method,
                        "temporary",
                        exc.__class__.__name__,
                        provider=self.provider_name,
                    )
                    self.record_failure(error)
                    raise error from exc
                self.retries[method] += 1
                time.sleep(self.retry_delay(attempt))
                continue
            self.latency_seconds[method].append(time.perf_counter() - request_started)
            self.latency_seconds[method] = self.latency_seconds[method][-200:]
            response_detail = (response.text or response.reason or "").lower()
            usage_exhausted = any(
                marker in response_detail
                for marker in (
                    "max usage",
                    "usage reached",
                    "quota",
                    "credits exhausted",
                    "credit limit",
                    "insufficient credit",
                )
            )
            if response.status_code in retryable_statuses and attempt < self.max_retries and not usage_exhausted:
                self.retries[method] += 1
                retry_after = to_float(response.headers.get("Retry-After"))
                time.sleep(self.retry_delay(attempt, retry_after=retry_after))
                continue
            if not response.ok:
                detail = (response.text or response.reason or "request failed").strip().replace("\n", " ")[:300]
                category = self.error_category(status=response.status_code, detail=detail)
                error = HeliusRpcError(
                    method,
                    category,
                    detail,
                    status=response.status_code,
                    provider=self.provider_name,
                )
                self.record_failure(error)
                raise error
            try:
                body = response.json()
            except ValueError as exc:
                error = HeliusRpcError(
                    method,
                    "provider",
                    "invalid JSON response",
                    provider=self.provider_name,
                )
                self.record_failure(error)
                raise error from exc
            if body.get("error"):
                error = body["error"]
                message = str(error.get("message") if isinstance(error, dict) else error)
                code = error.get("code") if isinstance(error, dict) else None
                usage_exhausted = any(
                    marker in message.lower()
                    for marker in ("max usage", "usage reached", "quota", "credits exhausted")
                )
                retryable_error = not usage_exhausted and (
                    code in (-32004, -32005, -32429) or any(
                    marker in message.lower() for marker in ("rate limit", "timeout", "temporarily unavailable")
                    )
                )
                if retryable_error and attempt < self.max_retries:
                    self.retries[method] += 1
                    time.sleep(self.retry_delay(attempt))
                    continue
                rpc_error = HeliusRpcError(
                    method,
                    self.error_category(code=code, detail=message),
                    message[:300],
                    code=code,
                    provider=self.provider_name,
                )
                self.record_failure(rpc_error)
                raise rpc_error
            result = body.get("result")
            self.estimated_credits += self.credit_cost(method, result)
            self.record_success(method)
            return result
        error = HeliusRpcError(
            method,
            "temporary",
            "retry budget exhausted",
            provider=self.provider_name,
        )
        self.record_failure(error)
        raise error

    def health(self):
        return self.call("getHealth")

    def signatures_for_address(self, address, limit=40, before=None):
        opts = {"limit": int(limit)}
        if before:
            opts["before"] = before
        return self.call("getSignaturesForAddress", [address, opts]) or []

    def transaction(self, signature):
        return self.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )

    def transactions_for_address(
        self,
        address,
        limit=100,
        sort_order="desc",
        pagination_token=None,
        block_time=None,
        status="succeeded",
    ):
        if not self.enhanced_history:
            raise HeliusRpcError(
                "getTransactionsForAddress",
                "unsupported",
                "enhanced address history is not supported",
                provider=self.provider_name,
            )
        filters = {"status": status}
        if block_time:
            filters["blockTime"] = block_time
        opts = {
            "transactionDetails": "full",
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
            "sortOrder": sort_order,
            "limit": min(max(1, int(limit)), 1000),
            "filters": filters,
        }
        if pagination_token:
            opts["paginationToken"] = pagination_token
        return self.call("getTransactionsForAddress", [address, opts], timeout=self.transactions_timeout_seconds) or {}

    def token_supply(self, mint):
        if mint in self.token_supply_cache:
            return self.token_supply_cache[mint]
        result = self.call("getTokenSupply", [mint]) or {}
        supply = rpc_token_amount(result.get("value"))
        self.token_supply_cache[mint] = supply
        return supply

    def token_balance(self, owner, mint):
        cache_key = (owner, mint)
        if cache_key in self.token_balance_cache:
            return self.token_balance_cache[cache_key]
        result = self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        ) or {}
        total = 0.0
        for item in result.get("value") or []:
            info = (
                item.get("account", {})
                .get("data", {})
                .get("parsed", {})
                .get("info", {})
            )
            total += rpc_token_amount(info.get("tokenAmount"))
        self.token_balance_cache[cache_key] = total
        return total


class HeliusRpc(SolanaRpcProvider):
    def __init__(
        self,
        api_key,
        timeout_seconds=30,
        transactions_timeout_seconds=25,
        max_retries=2,
        retry_base_seconds=0.75,
        retry_max_seconds=120,
        circuit_failure_threshold=4,
        min_interval_seconds=0,
        method_min_interval_seconds=None,
        credit_budget=0,
    ):
        super().__init__(
            "helius",
            f"https://mainnet.helius-rpc.com/?api-key={api_key}",
            enhanced_history=True,
            credit_model="helius",
            timeout_seconds=timeout_seconds,
            transactions_timeout_seconds=transactions_timeout_seconds,
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            circuit_failure_threshold=circuit_failure_threshold,
            min_interval_seconds=min_interval_seconds,
            method_min_interval_seconds=method_min_interval_seconds,
            credit_budget=credit_budget,
        )


class AlchemyRpc(SolanaRpcProvider):
    def __init__(self, url, **kwargs):
        super().__init__(
            "alchemy",
            url,
            enhanced_history=True,
            credit_model="alchemy",
            **kwargs,
        )


class ChainstackRpc(SolanaRpcProvider):
    def __init__(self, url, **kwargs):
        super().__init__(
            "chainstack",
            url,
            enhanced_history=False,
            credit_model="chainstack",
            unsupported_methods={
                "getSignaturesForAddress",
                "getTokenAccountsByOwner",
            },
            **kwargs,
        )


class DrpcRpc(SolanaRpcProvider):
    def __init__(self, url, **kwargs):
        super().__init__(
            "drpc",
            url,
            enhanced_history=False,
            credit_model="drpc",
            **kwargs,
        )


class PublicNodeRpc(SolanaRpcProvider):
    def __init__(self, url, **kwargs):
        unsupported_methods = set(kwargs.pop("unsupported_methods", []))
        unsupported_methods.add("getTokenAccountsByOwner")
        super().__init__(
            "publicnode",
            url,
            enhanced_history=False,
            credit_model="standard",
            unsupported_methods=unsupported_methods,
            **kwargs,
        )


class RoutedSolanaRpc:
    def __init__(
        self,
        providers,
        standard_order=None,
        enhanced_order=None,
        balance_order=None,
    ):
        self.providers = {provider.provider_name: provider for provider in providers}
        self.standard_order = self._ordered_names(
            standard_order
            or ["chainstack", "drpc", "publicnode", "alchemy", "helius"]
        )
        self.enhanced_order = self._ordered_names(
            enhanced_order or ["alchemy", "helius"],
            enhanced_only=True,
        )
        self.balance_order = self._ordered_names(
            balance_order
            or ["drpc", "publicnode", "alchemy", "helius", "chainstack"]
        )
        self.blocked_providers = {}
        self.unsupported_methods = defaultdict(set)
        for name, provider in self.providers.items():
            self.unsupported_methods[name].update(provider.unsupported_methods)
        self.route_failovers = Counter()
        self.last_provider_by_method = {}
        self.health_results = {}
        self.token_supply_cache = {}
        self.token_balance_cache = {}

    def _ordered_names(self, names, enhanced_only=False):
        ordered = []
        for name in names:
            provider = self.providers.get(str(name))
            if not provider or name in ordered:
                continue
            if enhanced_only and not provider.enhanced_history:
                continue
            ordered.append(str(name))
        for name, provider in self.providers.items():
            if name in ordered or (enhanced_only and not provider.enhanced_history):
                continue
            ordered.append(name)
        return ordered

    @property
    def calls(self):
        calls = Counter()
        for provider in self.providers.values():
            calls.update(provider.calls)
        return calls

    @property
    def retries(self):
        retries = Counter()
        for name, provider in self.providers.items():
            for method, count in provider.retries.items():
                retries[f"{name}:{method}"] += count
        return retries

    @property
    def failures(self):
        failures = Counter()
        for name, provider in self.providers.items():
            for category, count in provider.failures.items():
                failures[f"{name}:{category}"] += count
        return failures

    @property
    def estimated_credits(self):
        return sum(provider.estimated_credits for provider in self.providers.values())

    @property
    def circuit_open_reason(self):
        usable = [
            name
            for name in self.standard_order
            if name not in self.blocked_providers
            and not self.providers[name].circuit_open_reason
        ]
        if usable:
            return None
        details = [
            f"{name}: {self.blocked_providers.get(name) or self.providers[name].circuit_open_reason or 'unavailable'}"
            for name in self.standard_order
        ]
        return "all RPC providers unavailable: " + "; ".join(details)

    def _eligible_names(self, order, method, preferred=None, excluded=None):
        excluded = set(excluded or [])
        names = [preferred] if preferred else list(order)
        for name in names:
            provider = self.providers.get(name)
            if (
                not provider
                or name in excluded
                or name in self.blocked_providers
                or provider.circuit_open_reason
                or method in provider.method_circuit_open_reasons
                or method in self.unsupported_methods.get(name, set())
            ):
                continue
            yield name

    def has_available_provider(self, method, order=None):
        provider_order = self.standard_order if order is None else order
        return any(
            self.providers[name].can_call(method)
            for name in self._eligible_names(provider_order, method)
        )

    def available_history_mode(self):
        if self.has_available_provider(
            "getTransactionsForAddress",
            order=self.enhanced_order,
        ):
            return "enhanced"
        if self.has_available_provider(
            "getSignaturesForAddress",
            order=self.standard_order,
        ) and self.has_available_provider(
            "getTransaction",
            order=self.standard_order,
        ):
            return "standard"
        return None

    def _record_provider_error(self, provider_name, method, exc):
        category = getattr(exc, "category", "")
        if category == "unsupported":
            self.unsupported_methods[provider_name].add(method)
        elif category in {"auth", "quota"}:
            self.blocked_providers[provider_name] = str(exc)

    def _route_call(self, method, params=None, preferred=None, order=None, timeout=None, excluded=None):
        errors = []
        names = list(
            self._eligible_names(
                order or self.standard_order,
                method,
                preferred=preferred,
                excluded=excluded,
            )
        )
        for index, name in enumerate(names):
            provider = self.providers[name]
            try:
                provider_timeout = timeout
                if provider_timeout is None and method == "getTransactionsForAddress":
                    provider_timeout = provider.transactions_timeout_seconds
                result = provider.call(method, params=params, timeout=provider_timeout)
            except (HeliusRpcError, HeliusCircuitOpen) as exc:
                self._record_provider_error(name, method, exc)
                errors.append(f"{name}={getattr(exc, 'category', 'circuit')}")
                if index + 1 < len(names):
                    self.route_failovers[method] += 1
                continue
            self.last_provider_by_method[method] = name
            return result, name
        detail = ", ".join(errors) if errors else "no configured provider supports this method"
        raise RpcProvidersUnavailable(f"all RPC providers failed for {method}: {detail}")

    def call(self, method, params=None, timeout=None):
        result, _provider = self._route_call(method, params=params, timeout=timeout)
        return result

    def health(self):
        healthy = []
        for name in self.standard_order:
            provider = self.providers[name]
            try:
                result = provider.health()
                status = "ok" if result == "ok" else str(result or "unknown")
                self.health_results[name] = {"status": status}
                if result == "ok":
                    healthy.append(name)
            except (HeliusRpcError, HeliusCircuitOpen) as exc:
                self._record_provider_error(name, "getHealth", exc)
                if getattr(exc, "category", "") == "unsupported":
                    try:
                        provider.call("getSlot")
                        self.health_results[name] = {"status": "ok", "probe": "getSlot"}
                        healthy.append(name)
                        continue
                    except (HeliusRpcError, HeliusCircuitOpen) as slot_exc:
                        exc = slot_exc
                        self._record_provider_error(name, "getSlot", exc)
                self.health_results[name] = {
                    "status": getattr(exc, "category", "error"),
                    "error": str(exc)[:300],
                }
        if not healthy:
            raise RpcProvidersUnavailable("all RPC provider health checks failed")
        return "ok"

    def signatures_for_address(self, address, limit=40, before=None):
        opts = {"limit": int(limit)}
        if before:
            opts["before"] = before
        result, _provider = self._route_call(
            "getSignaturesForAddress",
            [address, opts],
            order=self.standard_order,
        )
        return result or []

    def transaction(self, signature):
        result, _provider = self._route_call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            order=self.standard_order,
        )
        return result

    def transactions_for_address(
        self,
        address,
        limit=100,
        sort_order="desc",
        pagination_token=None,
        block_time=None,
        status="succeeded",
        provider_name=None,
        excluded_providers=None,
    ):
        filters = {"status": status}
        if block_time:
            filters["blockTime"] = block_time
        opts = {
            "transactionDetails": "full",
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
            "sortOrder": sort_order,
            "limit": min(max(1, int(limit)), 1000),
            "filters": filters,
        }
        if pagination_token:
            opts["paginationToken"] = pagination_token
        result, provider = self._route_call(
            "getTransactionsForAddress",
            [address, opts],
            preferred=provider_name,
            order=self.enhanced_order,
            timeout=(
                self.providers[provider_name].transactions_timeout_seconds
                if provider_name in self.providers
                else None
            ),
            excluded=excluded_providers,
        )
        if not isinstance(result, dict):
            raise HeliusRpcError(
                "getTransactionsForAddress",
                "provider",
                "invalid paginated response",
                provider=provider,
            )
        routed_result = dict(result)
        routed_result["_provider"] = provider
        return routed_result

    def next_enhanced_provider(self, excluded=None):
        return next(
            self._eligible_names(
                self.enhanced_order,
                "getTransactionsForAddress",
                excluded=excluded,
            ),
            None,
        )

    def token_supply(self, mint):
        if mint in self.token_supply_cache:
            return self.token_supply_cache[mint]
        result, _provider = self._route_call(
            "getTokenSupply",
            [mint],
            order=self.standard_order,
        )
        supply = rpc_token_amount((result or {}).get("value"))
        self.token_supply_cache[mint] = supply
        return supply

    def token_balance(self, owner, mint):
        cache_key = (owner, mint)
        if cache_key in self.token_balance_cache:
            return self.token_balance_cache[cache_key]
        result, _provider = self._route_call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
            order=self.balance_order,
        )
        total = 0.0
        for item in (result or {}).get("value") or []:
            info = (
                item.get("account", {})
                .get("data", {})
                .get("parsed", {})
                .get("info", {})
            )
            total += rpc_token_amount(info.get("tokenAmount"))
        self.token_balance_cache[cache_key] = total
        return total

    def provider_stats(self):
        def percentile(values, ratio):
            if not values:
                return None
            ordered = sorted(float(value) for value in values)
            index = min(len(ordered) - 1, max(0, int(math.ceil(len(ordered) * ratio) - 1)))
            return round(ordered[index] * 1000, 1)

        stats = {}
        for name, provider in self.providers.items():
            health = self.health_results.get(name) or {}
            if name in self.blocked_providers or provider.circuit_open_reason:
                status = "blocked"
            elif provider.method_circuit_open_reasons:
                status = "degraded"
            elif (
                provider.credit_budget
                and provider.estimated_credits >= provider.credit_budget
            ):
                status = "budget_exhausted"
            elif sum(provider.calls.values()):
                status = "active"
            else:
                status = "ready"
            stats[name] = {
                "status": status,
                "health": health.get("status"),
                "enhanced_history": provider.enhanced_history,
                "calls": dict(provider.calls),
                "retries": dict(provider.retries),
                "failures": dict(provider.failures),
                "method_circuits": dict(provider.method_circuit_open_reasons),
                "estimated_credits": int(provider.estimated_credits),
                "credit_budget": int(provider.credit_budget),
                "credit_remaining": (
                    max(
                        0,
                        int(provider.credit_budget - provider.estimated_credits),
                    )
                    if provider.credit_budget
                    else None
                ),
                "credit_budget_used_pct": (
                    round(
                        provider.estimated_credits
                        / provider.credit_budget
                        * 100,
                        1,
                    )
                    if provider.credit_budget
                    else None
                ),
                "latency_ms": {
                    method: {
                        "p50": percentile(values, 0.50),
                        "p95": percentile(values, 0.95),
                        "samples": len(values),
                    }
                    for method, values in provider.latency_seconds.items()
                },
                "last_success_at": provider.last_success_at,
                "last_error": (
                    self.blocked_providers.get(name)
                    or provider.circuit_open_reason
                    or "; ".join(provider.method_circuit_open_reasons.values())
                    or provider.last_error
                ),
            }
        return stats


def _provider_order(value, default):
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list):
        value = list(default)
    return [str(item) for item in value]


def build_rpc_router(config):
    providers = []
    helius_key = os.environ.get("HELIUS_API_KEY")
    if helius_key:
        providers.append(
            HeliusRpc(
                helius_key,
                timeout_seconds=int(config.get("helius_rpc_timeout_seconds", 30)),
                transactions_timeout_seconds=int(
                    config.get("helius_transactions_timeout_seconds", 25)
                ),
                max_retries=int(config.get("helius_rpc_max_retries", 2)),
                retry_base_seconds=float(
                    config.get("helius_rpc_retry_base_seconds", 0.75)
                ),
                retry_max_seconds=float(
                    config.get("helius_rpc_retry_max_seconds", 120)
                ),
                circuit_failure_threshold=int(
                    config.get("helius_rpc_circuit_failure_threshold", 4)
                ),
                credit_budget=int(
                    config.get("helius_rpc_credit_budget_per_scan", 5000)
                ),
            )
        )

    alchemy_url = os.environ.get("ALCHEMY_SOLANA_RPC_URL")
    alchemy_key = os.environ.get("ALCHEMY_API_KEY")
    if not alchemy_url and alchemy_key:
        alchemy_url = f"https://solana-mainnet.g.alchemy.com/v2/{alchemy_key}"
    if alchemy_url:
        providers.append(
            AlchemyRpc(
                alchemy_url,
                timeout_seconds=int(config.get("alchemy_rpc_timeout_seconds", 20)),
                transactions_timeout_seconds=int(
                    config.get("alchemy_transactions_timeout_seconds", 35)
                ),
                max_retries=int(config.get("alchemy_rpc_max_retries", 4)),
                retry_base_seconds=float(
                    config.get("alchemy_rpc_retry_base_seconds", 1)
                ),
                retry_max_seconds=float(
                    config.get("alchemy_rpc_retry_max_seconds", 30)
                ),
                circuit_failure_threshold=int(
                    config.get("alchemy_rpc_circuit_failure_threshold", 2)
                ),
                min_interval_seconds=float(
                    config.get("alchemy_rpc_min_interval_seconds", 0.45)
                ),
                method_min_interval_seconds={
                    "getSignaturesForAddress": float(
                        config.get(
                            "alchemy_get_signatures_min_interval_seconds",
                            1.5,
                        )
                    )
                },
                credit_budget=int(
                    config.get("alchemy_rpc_credit_budget_per_scan", 25000)
                ),
            )
        )

    chainstack_url = os.environ.get("CHAINSTACK_SOLANA_RPC_URL")
    if chainstack_url:
        providers.append(
            ChainstackRpc(
                chainstack_url,
                timeout_seconds=int(config.get("chainstack_rpc_timeout_seconds", 12)),
                transactions_timeout_seconds=int(
                    config.get("chainstack_transactions_timeout_seconds", 20)
                ),
                max_retries=int(config.get("chainstack_rpc_max_retries", 2)),
                retry_base_seconds=float(
                    config.get("chainstack_rpc_retry_base_seconds", 0.5)
                ),
                retry_max_seconds=float(
                    config.get("chainstack_rpc_retry_max_seconds", 10)
                ),
                circuit_failure_threshold=int(
                    config.get("chainstack_rpc_circuit_failure_threshold", 3)
                ),
                min_interval_seconds=float(
                    config.get("chainstack_rpc_min_interval_seconds", 0.22)
                ),
                credit_budget=int(
                    config.get("chainstack_rpc_credit_budget_per_scan", 10000)
                ),
            )
        )

    drpc_url = os.environ.get("DRPC_SOLANA_RPC_URL")
    drpc_key = os.environ.get("DRPC_API_KEY")
    if not drpc_url and drpc_key:
        drpc_url = f"https://lb.drpc.live/solana/{drpc_key}"
    if drpc_url:
        providers.append(
            DrpcRpc(
                drpc_url,
                timeout_seconds=int(config.get("drpc_rpc_timeout_seconds", 15)),
                transactions_timeout_seconds=int(
                    config.get("drpc_transactions_timeout_seconds", 20)
                ),
                max_retries=int(config.get("drpc_rpc_max_retries", 2)),
                retry_base_seconds=float(
                    config.get("drpc_rpc_retry_base_seconds", 0.5)
                ),
                retry_max_seconds=float(
                    config.get("drpc_rpc_retry_max_seconds", 10)
                ),
                circuit_failure_threshold=int(
                    config.get("drpc_rpc_circuit_failure_threshold", 3)
                ),
                min_interval_seconds=float(
                    config.get("drpc_rpc_min_interval_seconds", 0.05)
                ),
                credit_budget=int(
                    config.get("drpc_rpc_credit_budget_per_scan", 100000)
                ),
            )
        )

    publicnode_url = os.environ.get("PUBLICNODE_SOLANA_RPC_URL")
    if publicnode_url:
        providers.append(
            PublicNodeRpc(
                publicnode_url,
                timeout_seconds=int(
                    config.get("publicnode_rpc_timeout_seconds", 15)
                ),
                transactions_timeout_seconds=int(
                    config.get("publicnode_transactions_timeout_seconds", 20)
                ),
                max_retries=int(config.get("publicnode_rpc_max_retries", 1)),
                retry_base_seconds=float(
                    config.get("publicnode_rpc_retry_base_seconds", 0.5)
                ),
                retry_max_seconds=float(
                    config.get("publicnode_rpc_retry_max_seconds", 5)
                ),
                circuit_failure_threshold=int(
                    config.get("publicnode_rpc_circuit_failure_threshold", 3)
                ),
                min_interval_seconds=float(
                    config.get("publicnode_rpc_min_interval_seconds", 0.2)
                ),
                credit_budget=int(
                    config.get("publicnode_rpc_request_budget_per_scan", 5000)
                ),
            )
        )

    if not providers:
        raise SystemExit(
            "No Solana RPC provider is configured. Set HELIUS_API_KEY, "
            "ALCHEMY_SOLANA_RPC_URL, CHAINSTACK_SOLANA_RPC_URL, or "
            "DRPC_SOLANA_RPC_URL/PUBLICNODE_SOLANA_RPC_URL."
        )
    return RoutedSolanaRpc(
        providers,
        standard_order=_provider_order(
            config.get("rpc_standard_provider_order"),
            ["chainstack", "drpc", "publicnode", "alchemy", "helius"],
        ),
        enhanced_order=_provider_order(
            config.get("rpc_enhanced_provider_order"),
            ["alchemy", "helius"],
        ),
        balance_order=_provider_order(
            config.get("rpc_balance_provider_order"),
            ["drpc", "publicnode", "alchemy", "helius", "chainstack"],
        ),
    )


def gecko_pool_from_item(item, source):
    attrs = item.get("attributes", {})
    rel = item.get("relationships", {})
    base = rel.get("base_token", {}).get("data", {}).get("id", "")
    token_address = base.split("solana_", 1)[-1] if base.startswith("solana_") else base
    dex = rel.get("dex", {}).get("data", {}).get("id", "")
    tx_m5 = attrs.get("transactions", {}).get("m5", {}) or {}
    tx_h1 = attrs.get("transactions", {}).get("h1", {}) or {}
    volume = attrs.get("volume_usd", {}) or {}
    return Pool(
        pool_address=attrs.get("address", ""),
        token_address=token_address,
        name=attrs.get("name", ""),
        symbol=(attrs.get("name", "").split("/", 1)[0].strip() if attrs.get("name") else ""),
        dex=dex,
        source=source,
        url=f"https://www.geckoterminal.com/solana/pools/{attrs.get('address', '')}",
        mcap_usd=to_float(attrs.get("market_cap_usd") or attrs.get("fdv_usd")),
        liquidity_usd=to_float(attrs.get("reserve_in_usd")),
        volume_5m_usd=to_float(volume.get("m5")),
        volume_1h_usd=to_float(volume.get("h1")),
        volume_24h_usd=to_float(volume.get("h24")),
        price_usd=to_float(attrs.get("base_token_price_usd")),
        txns_5m=int(to_float(tx_m5.get("buys")) + to_float(tx_m5.get("sells"))),
        txns_1h=int(to_float(tx_h1.get("buys")) + to_float(tx_h1.get("sells"))),
        pair_created_at=parse_timestamp(attrs.get("pool_created_at") or attrs.get("created_at")),
        market_snapshot_at=int(time.time()),
    )


def dexscreener_pool_from_pair(pair, source):
    tx_m5 = pair.get("txns", {}).get("m5", {}) or {}
    tx_h1 = pair.get("txns", {}).get("h1", {}) or {}
    volume = pair.get("volume", {}) or {}
    base = pair.get("baseToken", {}) or {}
    liquidity = pair.get("liquidity", {}) or {}
    return Pool(
        pool_address=pair.get("pairAddress", ""),
        token_address=base.get("address", ""),
        name=base.get("name", ""),
        symbol=base.get("symbol", ""),
        dex=pair.get("dexId", ""),
        source=source,
        url=pair.get("url", ""),
        mcap_usd=to_float(pair.get("marketCap") or pair.get("fdv")),
        liquidity_usd=to_float(liquidity.get("usd")),
        volume_5m_usd=to_float(volume.get("m5")),
        volume_1h_usd=to_float(volume.get("h1")),
        volume_24h_usd=to_float(volume.get("h24")),
        price_usd=to_float(pair.get("priceUsd")),
        txns_5m=int(to_float(tx_m5.get("buys")) + to_float(tx_m5.get("sells"))),
        txns_1h=int(to_float(tx_h1.get("buys")) + to_float(tx_h1.get("sells"))),
        pair_created_at=parse_timestamp(pair.get("pairCreatedAt")),
        market_snapshot_at=int(time.time()),
    )


def gmgn_pool_from_trenches_item(item):
    mint = clean_solana_address(item.get("address")) or ""
    pool_address = clean_solana_address(item.get("pool_address")) or ""
    exchange = clean_social_text(item.get("exchange"))
    if exchange == "pump_amm":
        exchange = "pumpfun-amm"
    total_supply = to_float(item.get("total_supply"))
    mcap_usd = to_float(item.get("usd_market_cap") or item.get("market_cap"))
    price_usd = mcap_usd / total_supply if mcap_usd > 0 and total_supply > 0 else 0.0
    return Pool(
        pool_address=pool_address,
        token_address=mint,
        name=clean_social_text(item.get("name")),
        symbol=clean_social_text(item.get("symbol")),
        dex=exchange,
        source="gmgn_trenches",
        url=f"https://gmgn.ai/sol/token/{mint}" if mint else "",
        mcap_usd=mcap_usd,
        liquidity_usd=to_float(item.get("liquidity")),
        volume_5m_usd=to_float(item.get("volume_5m")),
        volume_1h_usd=to_float(item.get("volume_1h")),
        volume_24h_usd=to_float(item.get("volume_24h")),
        price_usd=price_usd,
        txns_5m=int(to_float(item.get("swaps_5m"))),
        txns_1h=int(to_float(item.get("swaps_1h"))),
        pair_created_at=parse_timestamp(
            item.get("created_timestamp")
            or item.get("open_timestamp")
            or item.get("complete_timestamp")
        ),
        market_snapshot_at=int(time.time()),
    )


def gmgn_cli_command():
    if shutil.which("gmgn-cli"):
        return ["gmgn-cli"]
    if shutil.which("npx"):
        return ["npx", "-y", "gmgn-cli"]
    return None


def run_gmgn_cli(config, arguments, label):
    if not config.get("gmgn_enabled", True) or not os.environ.get("GMGN_API_KEY"):
        return None
    command_prefix = gmgn_cli_command()
    if not command_prefix:
        raise RuntimeError("GMGN_API_KEY is set but gmgn-cli/npx is unavailable")
    command = [*command_prefix, *arguments]
    if "--raw" not in command:
        command.append("--raw")
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=int(config.get("gmgn_timeout_seconds", 45)),
        check=False,
    )
    if completed.returncode != 0:
        api_key = os.environ.get("GMGN_API_KEY", "")
        message = (completed.stderr or completed.stdout or "request failed").strip()
        if api_key:
            message = message.replace(api_key, "***")
        raise RuntimeError(f"{label}: {message[:500]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label}: invalid JSON response") from exc


def fetch_gmgn_trending_token_addresses(config):
    if not config.get("gmgn_enabled", True):
        return set()
    if not os.environ.get("GMGN_API_KEY"):
        return set()
    addresses = set()
    platforms = config.get("gmgn_platforms") or ["Pump.fun"]
    filters = config.get("gmgn_filters") or []
    limit = int(config.get("gmgn_trending_limit", 100))
    configured_queries = config.get("gmgn_trending_queries")
    if configured_queries:
        queries = []
        for query in configured_queries:
            if not isinstance(query, dict):
                continue
            order_by = query.get("order_by") or "volume"
            for interval in query.get("intervals") or []:
                item = (str(interval), str(order_by))
                if item not in queries:
                    queries.append(item)
    else:
        intervals = config.get("gmgn_trending_intervals") or ["1m", "5m", "1h"]
        order_by = config.get("gmgn_trending_order_by", "volume")
        queries = [(str(interval), str(order_by)) for interval in intervals]

    for interval, order_by in queries:
        arguments = [
            "market",
            "trending",
            "--chain",
            "sol",
            "--interval",
            str(interval),
            "--order-by",
            str(order_by),
            "--direction",
            "desc",
            "--limit",
            str(limit),
        ]
        for platform in platforms:
            arguments.extend(["--platform", str(platform)])
        for item in filters:
            arguments.extend(["--filter", str(item)])

        try:
            label = f"gmgn trending {interval}/{order_by}"
            data = run_gmgn_cli(config, arguments, label)
        except Exception as exc:
            print(f"warn: gmgn trending {interval}/{order_by} failed: {exc}", file=sys.stderr)
            continue

        rank = data.get("data", {}).get("rank") if isinstance(data, dict) else None
        if rank is None and isinstance(data, dict):
            rank = data.get("rank")
        if not isinstance(rank, list):
            continue
        for item in rank:
            if not isinstance(item, dict):
                continue
            if str(item.get("chain") or "sol").lower() != "sol":
                continue
            address = clean_solana_address(item.get("address"))
            if address:
                addresses.add(address)
        time.sleep(float(config.get("gmgn_request_delay_seconds", 0.25)))

    if addresses:
        print(f"GMGN trending: {len(addresses)} Solana token candidates", flush=True)
    return addresses


def fetch_gmgn_trenches_universe(config):
    if not config.get("gmgn_enabled", True):
        return {}
    if not os.environ.get("GMGN_API_KEY"):
        return {}
    pools = {}
    queries = config.get("gmgn_trenches_queries") or [
        {
            "sort_by": config.get("gmgn_trenches_sort_by", "volume_1h"),
            "direction": "desc",
        }
    ]
    errors = []
    for query in queries:
        if not isinstance(query, dict):
            continue
        sort_by = str(query.get("sort_by") or "volume_1h")
        direction = str(query.get("direction") or "desc")
        arguments = [
            "market",
            "trenches",
            "--chain",
            "sol",
            "--type",
            "completed",
            "--limit",
            str(min(80, int(config.get("gmgn_trenches_limit", 80)))),
            "--sort-by",
            sort_by,
            "--direction",
            direction,
            "--min-marketcap",
            str(config["mcap_min_usd"]),
            "--max-marketcap",
            str(config["mcap_max_usd"]),
            "--min-liquidity",
            str(config["liquidity_min_usd"]),
        ]
        for platform in config.get("gmgn_launchpad_platforms") or ["Pump.fun"]:
            arguments.extend(["--launchpad-platform", str(platform)])
        try:
            data = run_gmgn_cli(
                config,
                arguments,
                f"gmgn trenches {sort_by}/{direction}",
            )
        except Exception as exc:
            errors.append(str(exc))
            print(
                f"warn: gmgn trenches {sort_by}/{direction} failed: {exc}",
                file=sys.stderr,
            )
            continue
        for item in (data or {}).get("completed", []) or []:
            if not isinstance(item, dict):
                continue
            pool = gmgn_pool_from_trenches_item(item)
            if pool.pool_address and pool.token_address:
                pools[pool.key()] = pool
        time.sleep(float(config.get("gmgn_request_delay_seconds", 0.25)))
    if pools:
        config.pop("_gmgn_error", None)
        print(f"GMGN trenches: {len(pools)} migrated Pump.fun pools", flush=True)
    elif errors:
        config["_gmgn_error"] = errors[-1]
    return pools


def fetch_gecko_universe(http, config):
    pools = {}
    delay = float(config.get("market_request_delay_seconds", 1.0))
    endpoints = [
        ("gecko_trending", "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"),
        ("gecko_new", "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"),
        ("gecko_pools", "https://api.geckoterminal.com/api/v2/networks/solana/pools"),
    ]
    page_count = int(config.get("gecko_pages", 2))
    for source, url in endpoints:
        pages = 1 if source == "gecko_trending" else page_count
        for page in range(1, pages + 1):
            params = {"page": page} if page > 1 or source == "gecko_pools" else None
            try:
                data = http.get_json(url, params=params)
            except Exception as exc:
                print(f"warn: {source} failed: {exc}", file=sys.stderr)
                if "429" in str(exc):
                    break
                continue
            for item in data.get("data", []):
                pool = gecko_pool_from_item(item, source)
                if pool.pool_address:
                    pools[pool.key()] = pool
            time.sleep(delay)
    return pools


def fetch_dex_token_addresses(http):
    addresses = set()
    urls = [
        "https://api.dexscreener.com/token-profiles/latest/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-boosts/top/v1",
    ]
    for url in urls:
        try:
            data = http.get_json(url)
        except Exception as exc:
            print(f"warn: {url} failed: {exc}", file=sys.stderr)
            continue
        if isinstance(data, dict):
            data = data.get("data", [])
        for item in data or []:
            address = clean_solana_address(item.get("tokenAddress"))
            if item.get("chainId") == "solana" and address:
                addresses.add(address)
        time.sleep(0.25)
    return addresses


def fetch_dex_pairs_for_tokens(http, token_addresses, source):
    pools = {}
    clean_addresses = {address for address in (clean_solana_address(item) for item in token_addresses) if address}
    for group in chunked(sorted(clean_addresses), 30):
        if not group:
            continue
        try:
            data = http.get_json("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(group))
        except Exception as exc:
            print(f"warn: dexscreener token batch failed: {exc}", file=sys.stderr)
            continue
        for pair in data.get("pairs") or []:
            if pair.get("chainId") != "solana":
                continue
            pool = dexscreener_pool_from_pair(pair, source)
            if pool.pool_address:
                pools[pool.key()] = pool
        time.sleep(0.25)
    return pools


def fetch_dex_pair_for_pool(http, pool_address):
    try:
        data = http.get_json(f"https://api.dexscreener.com/latest/dex/pairs/solana/{pool_address}")
    except Exception:
        return None
    pairs = data.get("pairs") or []
    if not pairs:
        return None
    return dexscreener_pool_from_pair(pairs[0], "manual_pool")


def normalize_dex_name(value):
    return clean_social_text(value).lower().replace("_", "-")


def pool_dex_allowed(pool, config):
    allowlist = [normalize_dex_name(item) for item in config.get("dex_allowlist", []) if item]
    if not allowlist:
        return True
    return normalize_dex_name(pool.dex) in allowlist


def registry_pool_from_market_entry(entry, now=None, activity_max_age_seconds=5_400):
    if not isinstance(entry, dict):
        return None
    pool_address = clean_solana_address(entry.get("pool_address"))
    token_address = clean_solana_address(entry.get("token_address"))
    if not pool_address or not token_address:
        return None
    now = int(now or time.time())
    snapshot_at = parse_timestamp(
        entry.get("market_snapshot_at")
        or entry.get("latest_seen_at")
        or entry.get("scan_mcap_at")
    )
    activity_stale = bool(
        snapshot_at
        and activity_max_age_seconds > 0
        and now - snapshot_at > int(activity_max_age_seconds)
    )
    return Pool(
        pool_address=pool_address,
        token_address=token_address,
        name=clean_social_text(entry.get("name")),
        symbol=clean_social_text(entry.get("symbol")),
        dex=clean_social_text(entry.get("dex")),
        source="registry",
        url=clean_social_text(entry.get("url")),
        mcap_usd=to_float(entry.get("latest_mcap_usd") or entry.get("scan_mcap_usd")),
        liquidity_usd=to_float(entry.get("latest_liquidity_usd") or entry.get("scan_liquidity_usd")),
        volume_5m_usd=0.0 if activity_stale else to_float(entry.get("latest_volume_5m_usd")),
        volume_1h_usd=0.0 if activity_stale else to_float(entry.get("latest_volume_1h_usd")),
        volume_24h_usd=0.0 if activity_stale else to_float(entry.get("latest_volume_24h_usd")),
        price_usd=to_float(entry.get("latest_price_usd") or entry.get("scan_price_usd")),
        txns_5m=0 if activity_stale else int(to_float(entry.get("latest_txns_5m"))),
        txns_1h=0 if activity_stale else int(to_float(entry.get("latest_txns_1h"))),
        pair_created_at=parse_timestamp(entry.get("pair_created_at")),
        market_snapshot_at=snapshot_at,
        market_snapshot_stale=activity_stale,
    )


def known_market_pools(state, config, observed_at=None):
    market = state.get("market") if isinstance(state, dict) else None
    if not isinstance(market, dict):
        return []
    now = parse_timestamp(observed_at) or int(time.time())
    retention_hours = float(config.get("registry_retention_hours", 720))
    cutoff = now - int(retention_hours * 3600) if retention_hours > 0 else 0
    pools = []
    for entry in market.values():
        if not isinstance(entry, dict):
            continue
        last_seen = parse_timestamp(entry.get("latest_seen_at") or entry.get("scan_mcap_at"))
        if cutoff and last_seen and last_seen < cutoff:
            continue
        pool = registry_pool_from_market_entry(
            entry,
            now=now,
            activity_max_age_seconds=max(
                0,
                int(float(config.get("registry_activity_max_age_minutes", 90)) * 60),
            ),
        )
        if not pool:
            continue
        if pool.dex:
            if not pool_dex_allowed(pool, config):
                continue
        elif not pool.token_address.lower().endswith("pump"):
            continue
        pools.append(pool)
    return pools


def merge_market_pools(registry_pools, discovered_pools):
    merged = {}
    for pool in registry_pools or []:
        if pool and pool.pool_address:
            merged[pool.pool_address] = pool
    for pool in discovered_pools or []:
        if pool and pool.pool_address:
            merged[pool.pool_address] = pool
    return list(merged.values())


def refresh_known_market_pools(http, state, config):
    registry = known_market_pools(state, config)
    limit = max(0, int(config.get("registry_refresh_max_tokens", 1000)))
    market = state.get("market") if isinstance(state, dict) else {}
    registry.sort(
        key=lambda pool: parse_timestamp((market.get(pool.token_address) or {}).get("registry_refreshed_at"))
    )
    tokens = []
    seen = set()
    for pool in registry:
        if not pool.token_address or pool.token_address in seen:
            continue
        seen.add(pool.token_address)
        tokens.append(pool.token_address)
        if limit and len(tokens) >= limit:
            break
    refreshed = fetch_dex_pairs_for_tokens(http, tokens, "registry_refresh") if tokens else {}
    refreshed_tokens = {pool.token_address for pool in refreshed.values() if pool.token_address}
    refreshed_at = utc_now().isoformat().replace("+00:00", "Z")
    for token in tokens:
        entry = market.get(token) if isinstance(market, dict) else None
        if not isinstance(entry, dict):
            continue
        entry["registry_refreshed_at"] = refreshed_at
        entry["registry_refresh_status"] = "ok" if token in refreshed_tokens else "missing"
    merged = merge_market_pools(registry, refreshed.values())
    return merged, {
        "stored_pools": len(registry),
        "requested_tokens": len(tokens),
        "refreshed_tokens": len(refreshed_tokens),
        "refreshed_pools": len(refreshed),
        "merged_pools": len(merged),
    }


def discover_market_pools(http, config):
    pools = fetch_gmgn_trenches_universe(config)
    trenches_tokens = {
        pool.token_address for pool in pools.values() if pool.token_address
    }
    pools.update(
        fetch_dex_pairs_for_tokens(http, trenches_tokens, "gmgn_trenches")
    )

    gmgn_tokens = fetch_gmgn_trending_token_addresses(config)
    pools.update(fetch_dex_pairs_for_tokens(http, gmgn_tokens, "gmgn_trending"))

    fallback_pools = fetch_gecko_universe(http, config)
    fallback_pools.update(pools)
    pools = fallback_pools

    token_addresses = fetch_dex_token_addresses(http)
    pools.update(fetch_dex_pairs_for_tokens(http, token_addresses, "dexscreener_tokens"))

    manual_tokens = config.get("manual_tokens", [])
    pools.update(fetch_dex_pairs_for_tokens(http, manual_tokens, "manual_token"))

    for pool_address in config.get("manual_pools", []):
        pool = fetch_dex_pair_for_pool(http, pool_address)
        if pool:
            pools[pool.key()] = pool

    return list(pools.values())


def pool_matches_config(pool, config):
    if not pool.pool_address:
        return False
    if not pool_dex_allowed(pool, config):
        return False
    is_manual = pool.source in ("manual_pool", "manual_token")
    if is_manual:
        return True
    if pool.mcap_usd <= 0:
        return False
    if not (config["mcap_min_usd"] <= pool.mcap_usd <= config["mcap_max_usd"]):
        return False
    if pool.liquidity_usd < config["liquidity_min_usd"]:
        return False
    age_hours = pool.age_hours()
    age_min = config.get("age_min_hours")
    age_max = config.get("age_max_hours")
    if age_min is not None or age_max is not None:
        if age_hours is None:
            return False
        if age_min is not None and age_hours < float(age_min):
            return False
        if age_max is not None and age_hours > float(age_max):
            return False
    volume_unknown_registry = pool.source == "registry" and pool.volume_1h_usd <= 0
    if (
        pool.volume_1h_usd < config["volume_1h_min_usd"]
        and pool.source != "dexscreener_tokens"
        and not volume_unknown_registry
    ):
        return False
    if config.get("volume_1h_max_usd") is not None and pool.volume_1h_usd > float(config["volume_1h_max_usd"]):
        return False
    if config.get("volume_1h_to_mcap_min") is not None:
        if not volume_unknown_registry and (
            pool.mcap_usd <= 0
            or (pool.volume_1h_usd / pool.mcap_usd) < float(config["volume_1h_to_mcap_min"])
        ):
            return False
    if config.get("volume_1h_to_liquidity_min") is not None:
        if not volume_unknown_registry and (
            pool.liquidity_usd <= 0
            or (pool.volume_1h_usd / pool.liquidity_usd) < float(config["volume_1h_to_liquidity_min"])
        ):
            return False
    if config.get("liquidity_to_mcap_min") is not None:
        if pool.mcap_usd <= 0 or (pool.liquidity_usd / pool.mcap_usd) < float(config["liquidity_to_mcap_min"]):
            return False
    return True


def reactivation_stage_config(pool, config):
    if (config.get("lane") or config.get("mode")) != "reactivation":
        return config
    mcap = float(getattr(pool, "mcap_usd", 0) or 0)
    for stage in config.get("reactivation_stages") or []:
        if not isinstance(stage, dict):
            continue
        max_mcap = float(stage.get("mcap_max_usd") or 0)
        if max_mcap and mcap > max_mcap:
            continue
        merged = dict(config)
        merged.update(
            {
                key: value
                for key, value in stage.items()
                if key not in {"name", "mcap_max_usd"}
            }
        )
        merged["reactivation_stage"] = stage.get("name") or "mature"
        return merged
    merged = dict(config)
    merged["reactivation_stage"] = "mature"
    return merged


def reactivation_activity_score(pool):
    mcap = max(1.0, float(pool.mcap_usd or 0))
    liquidity = max(1.0, float(pool.liquidity_usd or 0))
    volume_5m = max(0.0, float(pool.volume_5m_usd or 0))
    volume = max(0.0, float(pool.volume_1h_usd or 0))
    txns_5m = max(0, int(pool.txns_5m or 0))
    txns = max(0, int(pool.txns_1h or 0))
    volume_to_mcap = min(2.0, volume / mcap)
    volume_to_liquidity = min(4.0, volume / liquidity)
    burst_to_mcap = min(1.0, volume_5m / mcap)
    burst_acceleration = min(3.0, (volume_5m * 12.0) / max(1.0, volume))
    return (
        min(6.0, txns / 100.0)
        + volume_to_mcap * 8.0
        + volume_to_liquidity * 2.0
        + min(3.0, volume / 25_000.0)
        + min(5.0, txns_5m / 8.0)
        + burst_to_mcap * 12.0
        + burst_acceleration * 1.5
        + float(getattr(pool, "reactivation_baseline_score", 0) or 0)
    )


def market_activity_fingerprint(pool):
    return {
        "mcap_usd": round(float(pool.mcap_usd or 0), 2),
        "volume_5m_usd": round(float(pool.volume_5m_usd or 0), 2),
        "volume_1h_usd": round(float(pool.volume_1h_usd or 0), 2),
        "txns_5m": int(pool.txns_5m or 0),
        "txns_1h": int(pool.txns_1h or 0),
    }


def median_value(values):
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def compact_activity_bucket(pool, bucket_at):
    return [
        int(bucket_at),
        round(float(pool.mcap_usd or 0), 2),
        round(float(pool.liquidity_usd or 0), 2),
        round(float(pool.volume_5m_usd or 0), 2),
        round(float(pool.volume_1h_usd or 0), 2),
        int(pool.txns_5m or 0),
        int(pool.txns_1h or 0),
    ]


def upsert_compact_bucket(rows, bucket, cutoff, limit):
    rows = [
        list(item)
        for item in rows or []
        if isinstance(item, (list, tuple))
        and len(item) >= 7
        and int(item[0] or 0) >= cutoff
    ]
    if rows and int(rows[-1][0]) == int(bucket[0]):
        rows[-1] = bucket
    else:
        rows.append(bucket)
    rows.sort(key=lambda item: int(item[0] or 0))
    if limit > 0 and len(rows) > limit:
        rows = rows[-limit:]
    return rows


def baseline_history_stats(rows, now):
    rows = [
        item
        for item in rows or []
        if isinstance(item, (list, tuple)) and len(item) >= 7
    ]
    rows_24h = [item for item in rows if int(item[0] or 0) >= now - 24 * 3600]
    sample = rows_24h or rows
    return {
        "sample_count": len(rows),
        "sample_count_24h": len(rows_24h),
        "median_volume_1h_24h": median_value(item[4] for item in sample),
        "median_txns_1h_24h": median_value(item[6] for item in sample),
        "median_volume_1h_7d": median_value(item[4] for item in rows),
        "median_txns_1h_7d": median_value(item[6] for item in rows),
    }


def activity_context_from_history(entry, pool, now, config):
    stats = baseline_history_stats(entry.get("hourly"), now)
    median_volume = max(
        float(config.get("reactivation_baseline_volume_floor_usd", 250)),
        float(stats["median_volume_1h_24h"] or stats["median_volume_1h_7d"] or 0),
    )
    median_txns = max(
        float(config.get("reactivation_baseline_txn_floor", 3)),
        float(stats["median_txns_1h_24h"] or stats["median_txns_1h_7d"] or 0),
    )
    volume_ratio = float(pool.volume_1h_usd or 0) / median_volume
    txns_ratio = float(pool.txns_1h or 0) / median_txns
    burst_acceleration = (
        float(pool.volume_5m_usd or 0) * 12
        / max(1.0, float(pool.volume_1h_usd or 0))
    )
    last_snapshot_at = int(entry.get("last_snapshot_at") or 0)
    quiet_since = int(entry.get("quiet_since") or 0)
    if not quiet_since and last_snapshot_at and now - last_snapshot_at > 3600:
        quiet_since = last_snapshot_at + 3600
    quiet_hours = max(0.0, (now - quiet_since) / 3600) if quiet_since else 0.0
    min_samples = max(
        1,
        int(config.get("reactivation_baseline_min_samples", 12)),
    )
    ready = int(stats["sample_count"]) >= min_samples
    min_quiet_hours = float(
        config.get("reactivation_baseline_min_quiet_hours", 6)
    )
    confirmed = bool(
        ready
        and quiet_hours >= min_quiet_hours
        and (
            volume_ratio
            >= float(config.get("reactivation_baseline_min_volume_ratio", 3))
            or txns_ratio
            >= float(config.get("reactivation_baseline_min_txn_ratio", 3))
        )
        and (
            burst_acceleration
            >= float(
                config.get(
                    "reactivation_baseline_min_burst_acceleration",
                    1.25,
                )
            )
            or float(pool.volume_5m_usd or 0)
            >= float(config.get("reactivation_baseline_min_volume_5m_usd", 250))
        )
    )
    result = {
        **stats,
        "status": "ready" if ready else "warming",
        "observed_at": iso(now),
        "quiet_since": iso(quiet_since),
        "quiet_hours": quiet_hours,
        "volume_1h_ratio": volume_ratio,
        "txns_1h_ratio": txns_ratio,
        "burst_acceleration": burst_acceleration,
        "reactivation_confirmed": confirmed,
    }
    previous_context = entry.get("latest_context") or {}
    previous_context_at = parse_timestamp(previous_context.get("observed_at"))
    memory_seconds = int(
        float(
            config.get(
                "reactivation_baseline_activation_memory_minutes",
                60,
            )
        )
        * 60
    )
    if (
        not result["reactivation_confirmed"]
        and previous_context.get("reactivation_confirmed")
        and previous_context_at
        and now - previous_context_at <= memory_seconds
    ):
        result["reactivation_confirmed"] = True
        result["quiet_hours"] = max(
            result["quiet_hours"],
            float(previous_context.get("quiet_hours") or 0),
        )
        result["activation_observed_at"] = previous_context.get("observed_at")
    return result


def record_market_activity_baselines(state, pools, observed_at, config):
    if not config.get("reactivation_baseline_enabled", True):
        return {}
    now = parse_timestamp(observed_at) or int(time.time())
    section = state.setdefault("activity_baselines", {})
    five_minute_cutoff = now - int(
        float(config.get("reactivation_baseline_five_minute_retention_hours", 48))
        * 3600
    )
    hourly_cutoff = now - int(
        float(config.get("reactivation_baseline_hourly_retention_days", 7))
        * 86400
    )
    updated = 0
    confirmed = 0
    for pool in pools or []:
        token = pool.token_address or pool.pool_address
        if not token or pool.market_snapshot_stale:
            continue
        entry = section.setdefault(token, {})
        context = activity_context_from_history(entry, pool, now, config)
        if context["reactivation_confirmed"]:
            confirmed += 1

        quiet_volume_limit = max(
            float(config.get("reactivation_baseline_quiet_volume_floor_usd", 750)),
            float(context.get("median_volume_1h_24h") or 0)
            * float(config.get("reactivation_baseline_quiet_ratio", 1.25)),
        )
        quiet_txn_limit = max(
            float(config.get("reactivation_baseline_quiet_txn_floor", 8)),
            float(context.get("median_txns_1h_24h") or 0)
            * float(config.get("reactivation_baseline_quiet_ratio", 1.25)),
        )
        is_quiet = bool(
            float(pool.volume_1h_usd or 0) <= quiet_volume_limit
            and float(pool.txns_1h or 0) <= quiet_txn_limit
        )
        if is_quiet:
            entry["quiet_since"] = int(entry.get("quiet_since") or now)
        else:
            entry["quiet_since"] = 0

        five_bucket_at = now - now % 300
        hour_bucket_at = now - now % 3600
        bucket = compact_activity_bucket(pool, five_bucket_at)
        entry["five_minute"] = upsert_compact_bucket(
            entry.get("five_minute"),
            bucket,
            five_minute_cutoff,
            int(config.get("reactivation_baseline_max_five_minute_buckets", 576)),
        )
        hourly_bucket = compact_activity_bucket(pool, hour_bucket_at)
        entry["hourly"] = upsert_compact_bucket(
            entry.get("hourly"),
            hourly_bucket,
            hourly_cutoff,
            int(config.get("reactivation_baseline_max_hourly_buckets", 168)),
        )
        entry["last_snapshot_at"] = now
        entry["latest_context"] = context
        updated += 1

    stats = {
        "updated_at": observed_at,
        "tokens_updated": updated,
        "confirmed_reactivations": confirmed,
        "tracked_tokens": len(section),
    }
    state.setdefault("maintenance", {})["activity_baselines"] = stats
    return stats


def attach_reactivation_baselines(pools, state, config, observed_at=None):
    now = parse_timestamp(observed_at) or int(time.time())
    section = state.get("activity_baselines") if isinstance(state, dict) else {}
    section = section if isinstance(section, dict) else {}
    for pool in pools or []:
        token = pool.token_address or pool.pool_address
        entry = section.get(token) or {}
        context = activity_context_from_history(entry, pool, now, config)
        pool.reactivation_baseline = context
        score = 0.0
        if context.get("reactivation_confirmed"):
            score += 20.0
        score += min(8.0, max(0.0, float(context.get("volume_1h_ratio") or 0) - 1))
        score += min(8.0, max(0.0, float(context.get("txns_1h_ratio") or 0) - 1))
        score += min(4.0, float(context.get("quiet_hours") or 0) / 6)
        pool.reactivation_baseline_score = score
    return pools


def update_discovery_queue(state, pools, config, observed_at):
    now = parse_timestamp(observed_at) or int(time.time())
    ttl_seconds = int(
        float(config.get("discovery_queue_ttl_minutes", 90)) * 60
    )
    queue = {
        str(item.get("pool_address") or item.get("token_address")): dict(item)
        for item in state.get("discovery_queue", [])
        if isinstance(item, dict)
        and parse_timestamp(item.get("expires_at")) > now
        and (item.get("pool_address") or item.get("token_address"))
    }
    min_score = float(config.get("discovery_queue_min_activity_score", 12))
    for pool in pools or []:
        baseline = getattr(pool, "reactivation_baseline", {}) or {}
        activity_score = reactivation_activity_score(pool)
        burst_candidate = bool(
            int(pool.txns_5m or 0)
            >= int(config.get("discovery_queue_min_txns_5m", 5))
            or float(pool.volume_5m_usd or 0)
            >= float(config.get("discovery_queue_min_volume_5m_usd", 500))
        )
        if (
            activity_score < min_score
            and not baseline.get("reactivation_confirmed")
            and not burst_candidate
        ):
            continue
        key = pool.pool_address or pool.token_address
        queue[key] = {
            "pool_address": pool.pool_address,
            "token_address": pool.token_address,
            "symbol": pool.symbol,
            "observed_at": observed_at,
            "expires_at": iso(now + ttl_seconds),
            "activity_score": activity_score,
            "reactivation_confirmed": bool(
                baseline.get("reactivation_confirmed")
            ),
            "quiet_hours": float(baseline.get("quiet_hours") or 0),
            "volume_1h_ratio": float(baseline.get("volume_1h_ratio") or 0),
            "txns_1h_ratio": float(baseline.get("txns_1h_ratio") or 0),
            "mcap_usd": float(pool.mcap_usd or 0),
        }
    ordered = sorted(
        queue.values(),
        key=lambda item: (
            bool(item.get("reactivation_confirmed")),
            float(item.get("activity_score") or 0),
            parse_timestamp(item.get("observed_at")),
        ),
        reverse=True,
    )
    limit = max(1, int(config.get("discovery_queue_max_tokens", 250)))
    state["discovery_queue"] = ordered[:limit]
    return {
        "queued_tokens": len(state["discovery_queue"]),
        "confirmed_tokens": sum(
            1
            for item in state["discovery_queue"]
            if item.get("reactivation_confirmed")
        ),
    }


def market_activity_expected_head_lag_seconds(pool, config):
    txns_1h = max(0, int(pool.txns_1h or 0))
    high_threshold = max(
        1,
        int(config.get("market_activity_consistency_high_txns_1h", 100)),
    )
    medium_threshold = max(
        1,
        int(config.get("market_activity_consistency_medium_txns_1h", 20)),
    )
    if txns_1h >= high_threshold:
        return max(
            60,
            int(config.get("market_activity_consistency_high_max_lag_seconds", 600)),
        )
    if txns_1h >= medium_threshold:
        return max(
            60,
            int(config.get("market_activity_consistency_medium_max_lag_seconds", 1200)),
        )
    return max(
        60,
        int(config.get("market_activity_consistency_low_max_lag_seconds", 4200)),
    )


def market_activity_requires_head_check(pool, config):
    if not config.get("market_activity_consistency_enabled", True):
        return False
    return int(pool.txns_1h or 0) >= max(
        1,
        int(config.get("market_activity_consistency_min_txns_1h", 5)),
    )


def market_activity_head_probe(pool, signatures, config, now=None):
    now = int(now or time.time())
    expected_max_lag = market_activity_expected_head_lag_seconds(pool, config)
    latest = next(
        (
            item
            for item in signatures or []
            if not item.get("err") and int(item.get("blockTime") or 0) > 0
        ),
        None,
    )
    if not latest:
        return {
            "status": "stale",
            "expected_max_lag_seconds": expected_max_lag,
            "latest_signature": None,
            "latest_block_time": None,
            "head_lag_seconds": None,
        }
    latest_block_time = int(latest.get("blockTime") or 0)
    head_lag = max(0, now - latest_block_time)
    return {
        "status": "fresh" if head_lag <= expected_max_lag else "stale",
        "expected_max_lag_seconds": expected_max_lag,
        "latest_signature": latest.get("signature"),
        "latest_block_time": latest_block_time,
        "head_lag_seconds": head_lag,
    }


def market_activity_snapshot_changed(pool, fingerprint, config):
    if not isinstance(fingerprint, dict):
        return True
    ratio = max(
        0.05,
        float(config.get("market_activity_stale_rearm_change_ratio", 0.25)),
    )
    current = market_activity_fingerprint(pool)
    for key in ("mcap_usd", "volume_5m_usd", "volume_1h_usd", "txns_5m", "txns_1h"):
        previous = float(fingerprint.get(key) or 0)
        value = float(current.get(key) or 0)
        if abs(value - previous) / max(1.0, abs(previous)) >= ratio:
            return True
    return False


def mark_market_activity_head_state(pool_state, pool, probe, config, now=None):
    now = int(now or time.time())
    if probe.get("status") == "fresh":
        for key in (
            "market_activity_stale_at",
            "market_activity_stale_until",
            "market_activity_stale_reason",
            "market_activity_stale_fingerprint",
        ):
            pool_state.pop(key, None)
        return
    if probe.get("status") != "stale":
        return
    cooldown_minutes = max(
        1,
        int(config.get("market_activity_stale_cooldown_minutes", 360)),
    )
    pool_state.update(
        {
            "market_activity_stale_at": iso(now),
            "market_activity_stale_until": iso(now + cooldown_minutes * 60),
            "market_activity_stale_reason": "market activity is not present on the pool transaction head",
            "market_activity_stale_fingerprint": market_activity_fingerprint(pool),
        }
    )


def market_activity_priority_suppressed(pool, state, config, now=None):
    if (config.get("lane") or config.get("mode")) != "reactivation":
        return False
    pools_state = state.get("pools") if isinstance(state, dict) else None
    pool_state = (pools_state or {}).get(pool.pool_address) or {}
    stale_until = parse_timestamp(pool_state.get("market_activity_stale_until"))
    now = int(now or time.time())
    if not stale_until or stale_until <= now:
        return False
    return not market_activity_snapshot_changed(
        pool,
        pool_state.get("market_activity_stale_fingerprint"),
        config,
    )


def pool_priority_sort_key(pool, config):
    if (config.get("lane") or config.get("mode")) == "reactivation":
        return (
            0 if pool.market_snapshot_stale else 1,
            reactivation_activity_score(pool),
            int(pool.txns_5m or 0),
            float(pool.volume_5m_usd or 0),
            int(pool.txns_1h or 0),
            float(pool.volume_1h_usd or 0),
            float(pool.liquidity_usd or 0),
        )
    return (
        float(pool.volume_1h_usd or 0),
        int(pool.txns_1h or 0),
        float(pool.liquidity_usd or 0),
    )


def filter_universe_pools(pools, config):
    filtered = []
    for pool in pools:
        if pool_matches_config(pool, config):
            filtered.append(pool)

    by_token = {}
    for pool in filtered:
        key = pool.token_address or pool.pool_address
        current = by_token.get(key)
        if not current:
            by_token[key] = pool
            continue
        if (
            0 if pool.market_snapshot_stale else 1,
            int(pool.market_snapshot_at or 0),
            pool.volume_1h_usd,
            pool.liquidity_usd,
        ) > (
            0 if current.market_snapshot_stale else 1,
            int(current.market_snapshot_at or 0),
            current.volume_1h_usd,
            current.liquidity_usd,
        ):
            by_token[key] = pool

    filtered = list(by_token.values())
    filtered.sort(key=lambda pool: pool_priority_sort_key(pool, config), reverse=True)
    return filtered[: int(config["light_pool_limit"])]


def pool_last_scanned_at(state, pool):
    pools_state = state.get("pools") if isinstance(state, dict) else None
    if not isinstance(pools_state, dict):
        return 0
    entry = pools_state.get(pool.pool_address) or {}
    return max(
        parse_timestamp(entry.get("last_scanned_at")),
        parse_timestamp(entry.get("last_activity_probe_at")),
        parse_timestamp(entry.get("helius_latest_time")),
        parse_timestamp(entry.get("latest_time")),
    )


def reactivation_stage_counts(pools, config):
    counts = defaultdict(int)
    if (config.get("lane") or config.get("mode")) != "reactivation":
        return {}
    for pool in pools:
        stage = reactivation_stage_config(pool, config).get("reactivation_stage") or "mature"
        counts[stage] += 1
    return dict(counts)


def select_reactivation_priority(candidates, limit, config):
    if limit <= 0 or not candidates:
        return []
    shares = config.get("reactivation_stage_scan_shares")
    if (
        (config.get("lane") or config.get("mode")) != "reactivation"
        or not isinstance(shares, dict)
        or not shares
    ):
        return list(candidates[:limit])

    stages = [
        (str(name), max(0.0, float(share or 0)))
        for name, share in shares.items()
        if float(share or 0) > 0
    ]
    total_share = sum(share for _name, share in stages)
    if not stages or total_share <= 0:
        return list(candidates[:limit])

    buckets = {name: [] for name, _share in stages}
    for pool in candidates:
        stage = reactivation_stage_config(pool, config).get("reactivation_stage") or "mature"
        if stage in buckets:
            buckets[stage].append(pool)

    raw_quotas = [
        (name, limit * share / total_share)
        for name, share in stages
    ]
    quotas = {name: int(raw) for name, raw in raw_quotas}
    remaining_quota = limit - sum(quotas.values())
    for name, raw in sorted(
        raw_quotas,
        key=lambda item: (item[1] - int(item[1]), item[1]),
        reverse=True,
    )[:remaining_quota]:
        quotas[name] += 1

    selected = []
    selected_keys = set()
    for name, _share in stages:
        for pool in buckets.get(name, [])[: quotas.get(name, 0)]:
            selected.append(pool)
            selected_keys.add(pool.pool_address)

    for pool in candidates:
        if len(selected) >= limit:
            break
        if pool.pool_address in selected_keys:
            continue
        selected.append(pool)
        selected_keys.add(pool.pool_address)
    return selected


def due_signal_thesis_monitor_pools(state, config, now=None):
    now = int(now or time.time())
    latest_alert_pool = {}
    for alert in load_alert_history():
        pool = alert.get("pool") or {}
        pool_address = pool.get("pool_address")
        if not pool_address:
            continue
        existing = latest_alert_pool.get(pool_address)
        if (
            not existing
            or alert_history_timestamp(alert)
            >= existing[0]
        ):
            latest_alert_pool[pool_address] = (
                alert_history_timestamp(alert),
                pool,
            )

    monitor_pools = []
    for pool_address, pool_state in (state.get("pools") or {}).items():
        if not isinstance(pool_state, dict):
            continue
        thesis = pool_state.get("signal_thesis")
        due_at = parse_timestamp(pool_state.get("signal_recheck_due_at"))
        if (
            not isinstance(thesis, dict)
            or thesis.get("status") == "invalidated"
            or not due_at
            or due_at > now
        ):
            continue
        pool_payload = dict(
            (latest_alert_pool.get(pool_address) or (0, {}))[1]
        )
        pool_payload.update(
            {
                key: thesis.get(key)
                for key in (
                    "pool_address",
                    "token_address",
                    "symbol",
                    "name",
                    "dex",
                    "url",
                    "pair_created_at",
                )
                if thesis.get(key)
            }
        )
        pool_payload = apply_market_meta(pool_payload, state)
        if pool_payload.get("latest_mcap_usd") is not None:
            pool_payload["mcap_usd"] = pool_payload["latest_mcap_usd"]
        if pool_payload.get("latest_liquidity_usd") is not None:
            pool_payload["liquidity_usd"] = pool_payload[
                "latest_liquidity_usd"
            ]
        if pool_payload.get("latest_price_usd") is not None:
            pool_payload["price_usd"] = pool_payload["latest_price_usd"]
        if pool_payload.get("latest_seen_at"):
            pool_payload["market_snapshot_at"] = parse_timestamp(
                pool_payload["latest_seen_at"]
            )
        pool = Pool(
            **{
                key: value
                for key, value in pool_payload.items()
                if key in Pool.__dataclass_fields__
            }
        )
        if (
            not pool.pool_address
            or not pool.token_address
            or not pool_dex_allowed(pool, config)
        ):
            continue
        pool.source = "signal_thesis_monitor"
        monitor_pools.append(pool)
    return monitor_pools


def select_scan_targets(universe, state, config):
    limit = max(0, int(config.get("active_pool_limit", 0)))
    if not limit or not universe:
        return [], {
            "candidates": len(universe),
            "priority": 0,
            "rotation": 0,
            "never_scanned": 0,
            "market_stale_suppressed": 0,
            "market_stale_selected": 0,
        }
    now = int(time.time())
    suppressed_pool_keys = {
        pool.pool_address
        for pool in universe
        if market_activity_priority_suppressed(pool, state, config, now=now)
    }
    if len(universe) <= limit:
        never_scanned = sum(1 for pool in universe if not pool_last_scanned_at(state, pool))
        return list(universe), {
            "candidates": len(universe),
            "priority": len(universe),
            "rotation": 0,
            "never_scanned": never_scanned,
            "market_stale_suppressed": len(suppressed_pool_keys),
            "market_stale_selected": len(suppressed_pool_keys),
            "reactivation_stages": reactivation_stage_counts(universe, config),
        }

    priority_share = min(1.0, max(0.0, float(config.get("scan_priority_share", 0.6))))
    rotation_share = min(
        0.5,
        max(0.0, float(config.get("scan_rotation_min_share", 0.1))),
    )
    rotation_reserve = (
        min(limit, max(1, int(round(limit * rotation_share))))
        if rotation_share and len(universe) > limit
        else 0
    )
    priority_count = min(
        max(0, limit - rotation_reserve),
        max(1, int(round(limit * priority_share))),
    )

    def reserved_slots(setting, fallback):
        share = min(1.0, max(0.0, float(config.get(setting, fallback))))
        return min(priority_count, max(1, int(math.ceil(limit * share)))) if share else 0

    discovery_limit = reserved_slots("discovery_queue_min_share", 0.40)
    due_recheck_limit = reserved_slots("due_recheck_max_share", 0.25)
    gap_limit = reserved_slots("scan_gap_repair_share", 0.10)
    monitor_limit = reserved_slots("signal_monitor_share", 0.25)
    monitor_max_age = float(config.get("signal_monitor_max_age_hours", 48))
    monitor_cutoff = int(time.time() - monitor_max_age * 3600) if monitor_max_age > 0 else 0
    universe_by_key = {}
    for pool in universe:
        for key in (pool.pool_address, pool.token_address):
            if key:
                universe_by_key[key] = pool
    priority = []
    priority_keys = set()
    selection_reasons = {}

    def select(pool, reason):
        if pool.pool_address in priority_keys or len(priority) >= priority_count:
            return False
        priority.append(pool)
        priority_keys.add(pool.pool_address)
        selection_reasons[pool.pool_address] = reason
        return True

    pulse_selected = 0
    for item in state.get("discovery_queue", []) or []:
        if pulse_selected >= discovery_limit or len(priority) >= priority_count:
            break
        if not isinstance(item, dict) or parse_timestamp(item.get("expires_at")) <= now:
            continue
        pool = universe_by_key.get(item.get("pool_address")) or universe_by_key.get(
            item.get("token_address")
        )
        if pool and select(pool, "discovery_queue"):
            pulse_selected += 1

    due_rechecks = []
    pools_state = state.get("pools") if isinstance(state, dict) else {}
    if not isinstance(pools_state, dict):
        pools_state = {}
    if isinstance(state, dict):
        state["pools"] = pools_state
    for pool in universe:
        due_at = parse_timestamp(
            (pools_state.get(pool.pool_address) or {}).get("signal_recheck_due_at")
        )
        if due_at and due_at <= now:
            due_rechecks.append((due_at, pool))
    due_rechecks.sort(key=lambda item: (item[0], item[1].pool_address))
    due_selected = 0
    for _due_at, pool in due_rechecks:
        if due_selected >= due_recheck_limit or len(priority) >= priority_count:
            break
        if select(pool, "due_recheck"):
            due_selected += 1

    active_lane = config.get("lane")
    recent_monitor_selected = 0
    for alert in sorted(load_alert_history(), key=alert_history_sort_key, reverse=True):
        if recent_monitor_selected >= monitor_limit or len(priority) >= priority_count:
            break
        if active_lane and alert.get("lane") != active_lane:
            continue
        if monitor_cutoff and alert_history_timestamp(alert) < monitor_cutoff:
            continue
        alert_pool = alert.get("pool") or {}
        pool = universe_by_key.get(alert_pool.get("pool_address")) or universe_by_key.get(
            alert_pool.get("token_address")
        )
        if pool and select(pool, "recent_signal_monitor"):
            recent_monitor_selected += 1

    gap_candidates = []
    for pool in universe:
        if pool.pool_address in priority_keys:
            continue
        pool_state = pools_state.get(pool.pool_address) or {}
        backlogs = pool_state.get("helius_rolling_backlogs") or []
        if not backlogs and not pool_state.get("force_enhanced_next_scan"):
            continue
        oldest_gap = min(
            (
                int(item.get("from_timestamp") or 0)
                for item in backlogs
                if isinstance(item, dict)
            ),
            default=0,
        )
        gap_candidates.append((oldest_gap, pool_last_scanned_at(state, pool), pool))
    gap_candidates.sort(key=lambda item: (item[0], item[1], item[2].pool_address))
    gap_repairs = []
    for _oldest_gap, _last_scanned_at, pool in gap_candidates:
        if len(gap_repairs) >= gap_limit or len(priority) >= priority_count:
            break
        if select(pool, "history_gap_repair"):
            gap_repairs.append(pool)
    market_priority_limit = max(
        0,
        priority_count - len(priority),
    )
    market_candidates = [
        pool
        for pool in universe
        if pool.pool_address not in priority_keys
    ]
    unsuppressed_candidates = [
        pool
        for pool in market_candidates
        if pool.pool_address not in suppressed_pool_keys
    ]
    suppressed_candidates = [
        pool
        for pool in market_candidates
        if pool.pool_address in suppressed_pool_keys
    ]
    market_priority = select_reactivation_priority(
        unsuppressed_candidates,
        market_priority_limit,
        config,
    )
    if len(market_priority) < market_priority_limit:
        market_priority.extend(
            suppressed_candidates[: market_priority_limit - len(market_priority)]
        )
    for pool in market_priority:
        select(pool, "market_priority")
    rotation_candidates = [pool for pool in universe if pool.pool_address not in priority_keys]
    rotation_candidates.sort(
        key=lambda pool: (
            pool_last_scanned_at(state, pool),
            -float(pool.liquidity_usd or 0),
            pool.pool_address,
        )
    )
    rotation = rotation_candidates[: max(0, limit - len(priority))]
    for pool in rotation:
        selection_reasons[pool.pool_address] = "rotation"
    selected_at = iso(now)
    for pool in [*priority, *rotation]:
        pool_state = pools_state.setdefault(pool.pool_address, {})
        pool_state["last_selection_reason"] = selection_reasons.get(pool.pool_address)
        pool_state["last_selected_at"] = selected_at
    selected = [*priority, *rotation]
    return selected, {
        "candidates": len(universe),
        "priority": len(priority),
        "signal_monitor": due_selected + recent_monitor_selected,
        "discovery_queue": pulse_selected,
        "discovery_queue_reserved": discovery_limit,
        "due_rechecks": due_selected,
        "due_recheck_reserved": due_recheck_limit,
        "recent_signal_monitor": recent_monitor_selected,
        "gap_repairs": len(gap_repairs),
        "gap_repair_candidates": len(gap_candidates),
        "rotation": len(rotation),
        "rotation_reserve": rotation_reserve,
        "never_scanned": sum(1 for pool in selected if not pool_last_scanned_at(state, pool)),
        "market_stale_suppressed": len(suppressed_pool_keys),
        "market_stale_selected": sum(
            1 for pool in selected if pool.pool_address in suppressed_pool_keys
        ),
        "reactivation_stages": reactivation_stage_counts(selected, config),
    }


def build_universe(http, config):
    return filter_universe_pools(discover_market_pools(http, config), config)


def discovery_config_for_lanes(config, lane_list):
    lane_configs = [apply_lane(config, lane) if lane else config for lane in lane_list]
    discovery = dict(config)
    discovery["lane"] = "discovery"
    discovery["mcap_min_usd"] = min(float(item["mcap_min_usd"]) for item in lane_configs)
    discovery["mcap_max_usd"] = max(float(item["mcap_max_usd"]) for item in lane_configs)
    discovery["liquidity_min_usd"] = min(float(item["liquidity_min_usd"]) for item in lane_configs)
    discovery["volume_1h_min_usd"] = min(float(item.get("volume_1h_min_usd", 0) or 0) for item in lane_configs)
    discovery["volume_1h_max_usd"] = None
    discovery["age_min_hours"] = None
    discovery["age_max_hours"] = None
    discovery["volume_1h_to_mcap_min"] = None
    discovery["volume_1h_to_liquidity_min"] = None
    discovery["gmgn_trenches_limit"] = min(
        80,
        max(
            int(item.get("gmgn_trenches_limit", config.get("gmgn_trenches_limit", 80)))
            for item in lane_configs
        ),
    )
    discovery["gecko_pages"] = int(config.get("unified_gecko_pages", config.get("gecko_pages", 2)))
    return discovery


def bright_data_token():
    for name in ("BRIGHTDATA_API_KEY", "BRIGHT_DATA_API_KEY", "BRIGHT_DATA_API_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    return ""


def clean_social_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def x_author_from_url(url):
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    host = parsed.hostname or ""
    if host.startswith("www."):
        host = host[4:]
    if host not in ("x.com", "twitter.com", "nitter.net"):
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return ""
    author = parts[0]
    if author.lower() in {"i", "intent", "search", "home", "share", "hashtag"}:
        return ""
    return author


def x_status_id_from_url(url):
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in ("x.com", "twitter.com", "nitter.net"):
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[1].lower() == "status" and parts[2].isdigit():
        return parts[2]
    return ""


def to_int(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    multiplier = 1
    if text.lower().endswith("k"):
        multiplier = 1_000
        text = text[:-1]
    elif text.lower().endswith("m"):
        multiplier = 1_000_000
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return None


def token_terms(pool):
    values = [pool.symbol, pool.name, pool.token_address]
    terms = []
    for value in values:
        value = clean_social_text(value)
        if not value:
            continue
        terms.append(value.lower())
        if value.startswith("$"):
            terms.append(value[1:].lower())
        elif value.isascii() and len(value) <= 12:
            terms.append(f"${value}".lower())
    return {term for term in terms if len(term) >= 2}


def social_item_matches_pool(item, pool):
    haystack = clean_social_text(
        " ".join(
            clean_social_text(value)
            for value in [
                item.get("title"),
                item.get("description"),
                item.get("content"),
                item.get("url"),
                item.get("link"),
            ]
        )
    ).lower()
    terms = token_terms(pool)
    if not terms:
        return False
    if (pool.token_address or "").lower() in haystack:
        return True
    symbol = (pool.symbol or "").lower()
    if symbol and len(symbol) > 3 and (f"${symbol}" in haystack or f" {symbol} " in f" {haystack} "):
        return True
    return any(term in haystack for term in terms if len(term) >= 4)


def build_social_queries(pool, config):
    symbol = clean_social_text(pool.symbol).lstrip("$")
    name = clean_social_text(pool.name)
    mint = clean_social_text(pool.token_address)
    queries = []
    if symbol:
        queries.extend(
            [
                f'${symbol} Solana meme token x.com',
                f'{symbol} Solana token x.com',
                f'{symbol} Solana pump x.com',
            ]
        )
    if name and name.lower() != symbol.lower():
        queries.append(f'"{name}" Solana x.com')
    if mint:
        queries.append(f'"{mint}" x.com')
    extra = config.get("social_extra_queries", [])
    for query in extra:
        queries.append(query.format(symbol=symbol, name=name, mint=mint))
    deduped = []
    for query in queries:
        if query and query not in deduped:
            deduped.append(query)
    return deduped[: int(config.get("social_queries_per_token", 3))]


def request_bright_data_discover(http, query, pool, config, token):
    primary_keyword = clean_social_text(pool.symbol or pool.name)
    filter_keywords = [primary_keyword] if len(primary_keyword) >= 3 else []
    body = {
        "query": query,
        "intent": (
            f"Find recent public X posts about Solana token {pool.symbol or pool.name} "
            f"({pool.token_address}). Return direct x.com status URLs when available."
        ),
        "filter_keywords": filter_keywords,
        "num_results": int(config.get("social_num_results", 8)),
        "country": config.get("social_country", "US"),
        "language": config.get("social_language", "en"),
        "format": "json",
        "remove_duplicates": True,
        "start_date": config.get("social_start_date"),
    }
    body = {key: value for key, value in body.items() if value not in (None, "", [])}
    url = config.get("bright_data_discover_url", "https://api.brightdata.com/discover")
    headers = {"authorization": f"Bearer {token}"}
    initial = http.post_json(url, body, headers=headers, timeout=int(config.get("social_timeout_seconds", 45)))
    if isinstance(initial.get("results"), list):
        return initial
    task_id = initial.get("task_id")
    if not task_id:
        return {"results": [], "error": "Bright Data Discover did not return results or task_id"}

    deadline = time.time() + int(config.get("social_timeout_seconds", 45))
    poll_interval = float(config.get("social_poll_interval_seconds", 3))
    while time.time() < deadline:
        time.sleep(poll_interval)
        payload = http.get_json(f"{url}?task_id={task_id}", headers=headers, timeout=20)
        if payload.get("status") == "done":
            return payload
        if payload.get("status") == "failed":
            return {"results": [], "error": payload.get("error") or "Bright Data Discover task failed"}
    return {"results": [], "error": "Bright Data Discover task timed out"}


def scrape_x_post_metrics(http, urls, config, token):
    if not config.get("social_scrape_x_posts", True):
        return {}
    status_urls = []
    for url in urls:
        if x_status_id_from_url(url) and url not in status_urls:
            status_urls.append(url)
    status_urls = status_urls[: int(config.get("social_x_scrape_max_urls", 8))]
    if not status_urls:
        return {}

    dataset_id = config.get("social_x_scrape_dataset_id", "gd_lwxkxvnf1cynvib9co")
    endpoint = config.get("social_x_scrape_url", "https://api.brightdata.com/datasets/v3/scrape")
    params = f"dataset_id={dataset_id}&format=json&include_errors=true"
    headers = {"authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"input": [{"url": url} for url in status_urls]}
    try:
        payload = http.post_json(
            f"{endpoint}?{params}",
            body,
            headers=headers,
            timeout=int(config.get("social_x_scrape_timeout_seconds", 75)),
        )
    except Exception as exc:
        return {"_error": str(exc)}
    if not isinstance(payload, list):
        return {"_error": f"unexpected_x_scrape_payload:{type(payload).__name__}"}

    by_url = {}
    for item in payload:
        url = item.get("url") or (item.get("input") or {}).get("url")
        if not url:
            continue
        by_url[url] = {
            "post_id": str(item.get("id") or x_status_id_from_url(url) or ""),
            "url": url,
            "author": clean_social_text(item.get("user_posted") or x_author_from_url(url)),
            "name": clean_social_text(item.get("name")),
            "text": clean_social_text(item.get("description")),
            "date_posted": item.get("date_posted"),
            "followers": to_int(item.get("followers")),
            "following": to_int(item.get("following")),
            "profile_posts_count": to_int(item.get("posts_count")),
            "is_verified": bool(item.get("is_verified")),
            "verification_type": item.get("verification_type"),
            "likes": to_int(item.get("likes")) or 0,
            "reposts": to_int(item.get("reposts")) or 0,
            "replies": to_int(item.get("replies")) or 0,
            "quotes": to_int(item.get("quotes")) or 0,
            "bookmarks": to_int(item.get("bookmarks")) or 0,
            "views": to_int(item.get("views")) or 0,
            "profile_image_link": item.get("profile_image_link"),
            "biography": clean_social_text(item.get("biography")),
            "scraped_at": item.get("timestamp"),
        }
    return by_url


def build_caller_graph(results, watched_accounts):
    callers = {}
    watched = {name.lower().lstrip("@") for name in watched_accounts}
    for item in results:
        author = clean_social_text(item.get("author")).lstrip("@")
        if not author:
            continue
        key = author.lower()
        caller = callers.setdefault(
            key,
            {
                "author": author,
                "name": "",
                "followers": None,
                "following": None,
                "profile_posts_count": None,
                "is_verified": False,
                "verification_type": None,
                "posts": 0,
                "views": 0,
                "likes": 0,
                "reposts": 0,
                "replies": 0,
                "quotes": 0,
                "bookmarks": 0,
                "engagements": 0,
                "engagement_rate_views_pct": None,
                "engagement_rate_followers_pct": None,
                "influence_score": 0,
                "watched": key in watched,
                "top_post": None,
                "post_urls": [],
            },
        )
        caller["posts"] += 1
        metrics = item.get("metrics") or {}
        caller["name"] = caller["name"] or metrics.get("name") or item.get("title") or ""
        for field in ("followers", "following", "profile_posts_count"):
            if metrics.get(field) is not None:
                caller[field] = max(caller[field] or 0, metrics[field])
        if metrics.get("is_verified"):
            caller["is_verified"] = True
        if metrics.get("verification_type"):
            caller["verification_type"] = metrics.get("verification_type")
        for field in ("views", "likes", "reposts", "replies", "quotes", "bookmarks"):
            caller[field] += int(metrics.get(field) or 0)
        engagements = int(metrics.get("likes") or 0) + int(metrics.get("reposts") or 0) + int(metrics.get("replies") or 0) + int(metrics.get("quotes") or 0) + int(metrics.get("bookmarks") or 0)
        caller["engagements"] += engagements
        if item.get("url") and item["url"] not in caller["post_urls"]:
            caller["post_urls"].append(item["url"])
        top = caller.get("top_post")
        post_score = int(metrics.get("views") or 0) + engagements * 25 + float(item.get("relevance_score") or 0) * 100
        if not top or post_score > top.get("score", 0):
            caller["top_post"] = {
                "url": item.get("url"),
                "text": metrics.get("text") or item.get("description") or item.get("title") or "",
                "date_posted": metrics.get("date_posted"),
                "views": metrics.get("views") or 0,
                "likes": metrics.get("likes") or 0,
                "reposts": metrics.get("reposts") or 0,
                "replies": metrics.get("replies") or 0,
                "quotes": metrics.get("quotes") or 0,
                "bookmarks": metrics.get("bookmarks") or 0,
                "score": post_score,
            }
    output = []
    for caller in callers.values():
        if caller["views"]:
            caller["engagement_rate_views_pct"] = caller["engagements"] / caller["views"] * 100
        if caller["followers"]:
            caller["engagement_rate_followers_pct"] = caller["engagements"] / caller["followers"] * 100
        caller["influence_score"] = (
            (caller["followers"] or 0) / 1_000
            + caller["views"] / 100
            + caller["engagements"] * 2
            + (25 if caller["watched"] else 0)
            + (10 if caller["is_verified"] else 0)
        )
        output.append(caller)
    output.sort(key=lambda item: item["influence_score"], reverse=True)
    return output


def normalize_link(url, label="", kind="link"):
    url = clean_social_text(url)
    if not url:
        return None
    return {"url": url, "label": clean_social_text(label) or kind, "type": kind}


def decode_json_string(value):
    if value is None:
        return ""
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return clean_social_text(value)


def dedupe_links(links):
    output = []
    seen = set()
    for link in links:
        if not link or not link.get("url"):
            continue
        key = link["url"].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        output.append(link)
    return output


def gmgn_external_url(value, kind):
    value = clean_social_text(value)
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    value = value.lstrip("@/")
    if kind == "twitter":
        return f"https://x.com/{value}"
    if kind == "telegram":
        return f"https://t.me/{value}"
    return f"https://{value}"


def fetch_gmgn_raw_token_info(config, token_address):
    if not token_address or not os.environ.get("GMGN_API_KEY"):
        return {}
    cache = config.setdefault("_gmgn_token_info_cache", {})
    if token_address in cache:
        return cache[token_address]
    data = run_gmgn_cli(
        config,
        ["token", "info", "--chain", "sol", "--address", token_address],
        f"gmgn token info {token_address}",
    )
    if not isinstance(data, dict):
        return {}
    cache[token_address] = data
    return data


def fetch_gmgn_token_info(config, token_address):
    cache = config.setdefault("_gmgn_profile_cache", {})
    if token_address in cache:
        return cache[token_address]
    data = fetch_gmgn_raw_token_info(config, token_address)
    if not data:
        return {}
    link = data.get("link") or {}
    dev = data.get("dev") or {}
    links = [
        normalize_link(gmgn_external_url(link.get("website"), "website"), "Website", "website"),
        normalize_link(
            gmgn_external_url(
                link.get("twitter_username") or link.get("twitter"),
                "twitter",
            ),
            "X",
            "twitter",
        ),
        normalize_link(gmgn_external_url(link.get("telegram"), "telegram"), "Telegram", "telegram"),
        normalize_link(link.get("discord"), "Discord", "discord"),
        normalize_link(link.get("github"), "GitHub", "github"),
    ]
    profile = {
        "source": "gmgn",
        "name": clean_social_text(data.get("name")),
        "symbol": clean_social_text(data.get("symbol")),
        "description": clean_social_text(link.get("description")),
        "image": data.get("logo"),
        "created_on": clean_social_text(data.get("launchpad_platform") or data.get("launchpad")),
        "creator": dev.get("creator_address"),
        "created_tx": None,
        "created_time": parse_timestamp(data.get("creation_timestamp")),
        "launchpad_status": data.get("launchpad_status"),
        "migrated_pool": data.get("migrated_pool"),
        "migrated_time": parse_timestamp(data.get("migrated_timestamp")),
        "links": dedupe_links(links),
    }
    cache[token_address] = profile
    return profile


def fetch_dex_token_info(http, token_address):
    if not token_address:
        return {}
    try:
        data = http.get_json(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=25)
    except Exception:
        return {}
    pairs = [pair for pair in data.get("pairs") or [] if pair.get("chainId") == "solana"]
    if not pairs:
        return {}
    pairs.sort(key=lambda pair: to_float((pair.get("liquidity") or {}).get("usd")), reverse=True)
    best = pairs[0]
    info = best.get("info") or {}
    links = []
    for item in info.get("websites") or []:
        links.append(normalize_link(item.get("url"), item.get("label") or "Website", "website"))
    for item in info.get("socials") or []:
        links.append(normalize_link(item.get("url"), item.get("type") or "social", item.get("type") or "social"))
    return {
        "source": "dexscreener",
        "pair_url": best.get("url"),
        "image": info.get("imageUrl"),
        "header": info.get("header"),
        "links": dedupe_links(links),
        "pairs": [
            {
                "pair_address": pair.get("pairAddress"),
                "dex": pair.get("dexId"),
                "url": pair.get("url"),
                "liquidity_usd": to_float((pair.get("liquidity") or {}).get("usd")),
                "market_cap_usd": to_float(pair.get("marketCap") or pair.get("fdv")),
                "volume_24h_usd": to_float((pair.get("volume") or {}).get("h24")),
            }
            for pair in pairs[:5]
        ],
    }


def request_project_context(http, pool, config, token):
    if not config.get("token_intel_project_context_enabled", True) or not token:
        return []
    query = f'"{pool.name or pool.symbol}" {pool.symbol} Solana token project x.com website'
    body = {
        "query": query,
        "intent": (
            f"Find public sources that explain Solana token {pool.symbol} / {pool.name}, "
            f"including official website, X account, CoinGecko, Dexscreener, project writeups, or launchpad news."
        ),
        "filter_keywords": [value for value in [pool.symbol, pool.name] if value],
        "num_results": int(config.get("token_intel_context_results", 5)),
        "country": config.get("social_country", "US"),
        "language": config.get("social_language", "en"),
        "format": "json",
        "remove_duplicates": True,
    }
    body = {key: value for key, value in body.items() if value not in (None, "", [])}
    url = config.get("bright_data_discover_url", "https://api.brightdata.com/discover")
    headers = {"authorization": f"Bearer {token}"}
    try:
        payload = http.post_json(url, body, headers=headers, timeout=int(config.get("social_timeout_seconds", 45)))
    except Exception as exc:
        return [{"error": str(exc), "source": "bright_data"}]
    if not isinstance(payload.get("results"), list):
        task_id = payload.get("task_id")
        if task_id:
            deadline = time.time() + int(config.get("social_timeout_seconds", 45))
            poll_interval = float(config.get("social_poll_interval_seconds", 3))
            while time.time() < deadline:
                time.sleep(poll_interval)
                payload = http.get_json(f"{url}?task_id={task_id}", headers=headers, timeout=20)
                if payload.get("status") == "done":
                    break
                if payload.get("status") == "failed":
                    return [{"error": payload.get("error") or "Bright Data context task failed", "source": "bright_data"}]
    results = []
    for item in payload.get("results", []) or []:
        link = item.get("url") or item.get("link")
        if not link:
            continue
        normalized = {
            "url": link,
            "title": clean_social_text(item.get("title") or link),
            "description": clean_social_text(item.get("description") or item.get("content")),
            "relevance_score": to_float(item.get("relevance_score")),
            "source": "bright_data",
        }
        if not social_item_matches_pool(normalized, pool):
            continue
        results.append(
            normalized
        )
    return results


def official_x_links(profile, dex_info):
    links = []
    for link in [*profile.get("links", []), *dex_info.get("links", [])]:
        url = link.get("url") or ""
        link_type = (link.get("type") or link.get("label") or "").lower()
        if link_type in {"twitter", "x"} or x_author_from_url(url):
            author = x_author_from_url(url)
            if author and not x_status_id_from_url(url):
                links.append({"url": url, "author": author})
    by_author = {}
    for item in links:
        by_author.setdefault(item["author"].lower(), item)
    return list(by_author.values())


def fetch_official_x_profiles(http, profile, dex_info, config):
    if not config.get("token_intel_official_x_profile_enabled", True):
        return []
    profiles = []
    links = official_x_links(profile, dex_info)[: int(config.get("token_intel_official_x_profile_limit", 2))]
    for link in links:
        author = link["author"].lstrip("@")
        url = f"https://x.com/{author}"
        try:
            response = http.session.get(
                url,
                timeout=int(config.get("token_intel_official_x_profile_timeout_seconds", 12)),
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
        except Exception:
            continue
        marker = f'"screen_name":"{re.escape(author)}"'
        match = re.search(marker, response.text, re.I)
        if not match:
            continue
        window = response.text[max(0, match.start() - 3500) : match.end() + 1800]
        desc_match = re.search(r'"description":"((?:\\.|[^"\\])*)"', window)
        if not desc_match:
            continue
        description = clean_social_text(decode_json_string(desc_match.group(1)))
        if not description:
            continue
        followers_match = re.search(r'"followers_count":(\d+)', window)
        name_match = re.search(r'"name":"((?:\\.|[^"\\])*)"', window)
        profiles.append(
            {
                "url": url,
                "title": f"Official X @{author}",
                "description": description,
                "relevance_score": 0.95,
                "source": "official_x_profile",
                "author": author,
                "name": clean_social_text(decode_json_string(name_match.group(1))) if name_match else author,
                "followers": to_int(followers_match.group(1)) if followers_match else None,
            }
        )
    return profiles


def short_excerpt(value, max_len=260):
    text = clean_social_text(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "..."


def source_metrics_label(metrics):
    if not metrics:
        return None
    parts = []
    if metrics.get("views") is not None:
        parts.append(f"{to_int(metrics.get('views')) or 0} views")
    if metrics.get("likes") is not None:
        parts.append(f"{to_int(metrics.get('likes')) or 0} likes")
    if metrics.get("reposts") is not None:
        parts.append(f"{to_int(metrics.get('reposts')) or 0} reposts")
    if metrics.get("replies") is not None:
        parts.append(f"{to_int(metrics.get('replies')) or 0} replies")
    if metrics.get("quotes") is not None:
        parts.append(f"{to_int(metrics.get('quotes')) or 0} quotes")
    return ", ".join(parts[:5]) if parts else None


def lore_official_claims(profile, context):
    claims = []
    if profile.get("description"):
        claims.append(profile.get("description"))
    for item in context:
        if item.get("source") == "official_x_profile" and item.get("description"):
            claims.append(f"Official X: {item.get('description')}")
    output = []
    seen = set()
    for claim in claims:
        text = short_excerpt(claim, 220)
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            output.append(text)
    return output[:3]


def build_lore_analysis(pool, primary, secondary, ranked, profile, dex_info, context, social, evidence, sources):
    social_results = (social or {}).get("results", [])
    ranked_names = [name for name, _ in ranked]
    text_parts = [
        pool.symbol,
        pool.name,
        profile.get("description"),
        " ".join(f"{item.get('title')} {item.get('description')}" for item in context),
        " ".join(f"{item.get('title')} {item.get('description')}" for item in social_results),
        " ".join(f"{(item.get('metrics') or {}).get('text')}" for item in social_results),
    ]
    full_text = clean_social_text(" ".join(str(part or "") for part in text_parts))
    lower_text = full_text.lower()

    evidence_rows = []

    def add_row(kind, claim, source, url=None, metrics=None, confidence="supporting"):
        row = {
            "kind": kind,
            "claim": short_excerpt(claim),
            "source": short_excerpt(source, 90),
            "confidence": confidence,
        }
        if url:
            row["url"] = url
        metric_text = source_metrics_label(metrics)
        if metric_text:
            row["metrics"] = metric_text
        evidence_rows.append(row)

    if profile.get("description"):
        add_row(
            "official_profile",
            profile.get("description"),
            "GMGN token profile",
            f"https://gmgn.ai/sol/token/{pool.token_address}",
            confidence="strong" if primary != "Unclear" else "supporting",
        )
    for item in context[:5]:
        if item.get("description") or item.get("title"):
            kind = "official_x_profile" if item.get("source") == "official_x_profile" else "public_context"
            source = (
                f"Official X @{item.get('author')}"
                if kind == "official_x_profile" and item.get("author")
                else item.get("title") or item.get("url") or "Public context"
            )
            add_row(
                kind,
                f"{item.get('title')}: {item.get('description')}",
                source,
                item.get("url"),
                confidence="strong" if kind == "official_x_profile" or (item.get("relevance_score", 0) and item.get("relevance_score", 0) >= 0.64) else "supporting",
            )
    for item in social_results[:5]:
        metrics = item.get("metrics") or {}
        social_text = metrics.get("text") or item.get("description") or item.get("title")
        if social_text:
            add_row(
                "social_post",
                social_text,
                f"@{item.get('author')}" if item.get("author") else item.get("title") or "X post",
                item.get("url"),
                metrics,
                confidence="strong" if metrics.get("views") and metrics.get("views") >= 10_000 else "supporting",
            )

    symbol = clean_social_text(pool.symbol).lstrip("$")
    hygiene = []
    if len(symbol) <= 3:
        hygiene.append(
            f"Short ticker guard: ${symbol} alone is not enough; matches must include mint, full token name, or official project link."
        )
    if social_results:
        hygiene.append(f"Social graph filtered to {len(social_results)} token-matched X results.")

    conflicts = []
    if "Animals" in ranked_names and primary != "Animals":
        conflicts.append("Animal/mascot branding is treated as secondary packaging, not the core market lore.")
    if "Classic Meme" in ranked_names and primary != "Classic Meme":
        conflicts.append("Meme phrasing exists, but source evidence did not beat the primary narrative.")

    lore_patterns = {
        "roaring_kitty_psyop": re.search(
            r"roaring kitty|kevin14|tsuki|0\.14|buyback-burn|buyback burn|burning the supply|control the memes|psyop|mystery dev",
            lower_text,
            re.I,
        )
        is not None,
        "hantavirus": re.search(r"hanta|hantavirus|outbreak", lower_text, re.I) is not None,
        "gaming_creator": re.search(r"bloxapi|roblox|game creator|game launchpad|creator studio|studio plugin", lower_text, re.I) is not None,
        "privacy_compute": re.search(
            r"confidential computing|confidential execution|zero[- ]knowledge|\bmpc\b|privacy-first|secure enclave|secure enclaves|intel sgx|amd sev|trustzone|end-to-end encryption|encrypt, compute",
            lower_text,
            re.I,
        )
        is not None,
    }
    official_claims = lore_official_claims(profile, context)

    if primary == "Classic Meme" and lore_patterns["roaring_kitty_psyop"]:
        headline = "Roaring Kitty / Kevin14 psyop meme lore"
        driver = "social-lore catalyst"
        summary = (
            f"{pool.symbol} is not just an animal mascot meme. Official branding is puppy/dog, "
            "but stronger public and social context centers on Roaring Kitty, Kevin14, TSUKI, "
            "0.14 buyback-burn claims, and psyop speculation."
        )
        confidence = "high" if any(row["kind"] == "social_post" and row.get("metrics") for row in evidence_rows) else "medium"
    elif primary == "Health/Bio" and lore_patterns["hantavirus"]:
        headline = "Health/news-cycle meme lore"
        driver = "external news cycle"
        summary = (
            f"{pool.symbol} is a Health/Bio news-cycle meme: project and public sources frame it as "
            "Hanta-Kun, an anime mascot created around the hantavirus theme and reused for the current "
            "hantavirus narrative. Anime/mascot wording is packaging; Health/Bio is the lead thesis."
        )
        confidence = "high" if profile.get("description") and context else "medium"
    elif primary == "Gaming/Creator Infra" and lore_patterns["gaming_creator"]:
        headline = "Gaming creator-infrastructure project lore"
        driver = "project/product catalyst"
        summary = (
            f"{pool.symbol} is a Gaming/Creator Infra project thesis: project/profile sources point to "
            "game creator tools, Roblox-style creator infrastructure, analytics, or launchpad language."
        )
        confidence = "high" if context or profile.get("description") else "medium"
    elif primary == "Privacy/Compute Infra" and lore_patterns["privacy_compute"]:
        headline = "Confidential compute / privacy infra"
        driver = "project/product catalyst"
        claim_text = "; ".join(official_claims)
        summary = (
            f"{pool.symbol} is a Privacy/Compute Infra thesis: official/project sources frame {pool.name or pool.symbol} as "
            "privacy-first compute infrastructure for encrypted workloads, zero-knowledge/MPC flows, and "
            f"confidential execution. {claim_text}"
        ).strip()
        confidence = "high" if any(row["kind"] == "official_x_profile" for row in evidence_rows) else "medium"
    elif primary == "DevTool/Infra":
        headline = "Project infrastructure thesis"
        driver = "project/product catalyst"
        claim_text = "; ".join(official_claims) or "project/profile sources describe developer or infrastructure tooling."
        summary = f"{pool.symbol} is a DevTool/Infra thesis: {claim_text}"
        confidence = "high" if official_claims else "medium"
    elif primary == "Animals":
        headline = "Animal mascot meme lore"
        driver = "mascot/community meme"
        summary = (
            f"{pool.symbol} is an animal mascot/community meme: the strongest available evidence is "
            "the token name/profile mascot, with no stronger project, news, or social-lore catalyst detected."
        )
        confidence = "medium" if profile.get("description") or context else "low"
    elif primary == "Unclear":
        headline = "Lore not established"
        driver = "not classified"
        summary = "No dominant lore was established from official metadata, public context, and social evidence."
        confidence = "low"
    else:
        headline = f"{primary} lore"
        driver = "source-backed narrative"
        summary = f"{pool.symbol} is a {primary} thesis backed by scanner token-intel evidence."
        confidence = "medium" if evidence_rows else "low"

    return {
        "headline": headline,
        "summary": summary,
        "driver": driver,
        "confidence": confidence,
        "primary_evidence": evidence.get(primary, []),
        "evidence": evidence_rows[:8],
        "conflicts": conflicts[:4],
        "source_hygiene": hygiene[:4],
    }


def classify_token_narrative(pool, profile, dex_info, context, social):
    text_parts = [
        pool.symbol,
        pool.name,
        pool.token_address,
        profile.get("name"),
        profile.get("symbol"),
        profile.get("description"),
        " ".join(f"{link.get('label')} {link.get('url')}" for link in profile.get("links", [])),
        " ".join(f"{link.get('label')} {link.get('url')}" for link in dex_info.get("links", [])),
        " ".join(f"{item.get('title')} {item.get('description')} {item.get('url')}" for item in context),
        " ".join(f"{item.get('title')} {item.get('description')} {item.get('url')}" for item in (social or {}).get("results", [])),
        " ".join(f"{(item.get('metrics') or {}).get('text')}" for item in (social or {}).get("results", [])),
    ]
    text = clean_social_text(" ".join(str(part or "") for part in text_parts)).lower()
    scores = Counter()
    evidence = {}

    def add(name, points, reason):
        scores[name] += points
        evidence.setdefault(name, [])
        if reason not in evidence[name]:
            evidence[name].append(reason)

    def has(pattern):
        return re.search(pattern, text, re.I) is not None

    if has(r"hanta|hantavirus|covid|plague|vaccine|cancer|outbreak|infection|healthcare|biotech"):
        add("Health/Bio", 6, "token/profile/social text matches health or virus narrative")
    if re.search(r"hanta|hantavirus", f"{pool.symbol} {pool.name} {profile.get('description') or ''}", re.I):
        add("Health/Bio", 2, "health narrative appears in token name or official profile")
    if has(r"bloxapi|bloxxbuilder|streamerconnect|trustconnect|roblox|game creator|game launchpad|creator studio|studio plugin|gamefi|gaming|player engagement|retention|monetization"):
        add("Gaming/Creator Infra", 7, "project/profile sources point to gaming creator infrastructure")
    if has(r"confidential computing|confidential execution|zero[- ]knowledge|\bmpc\b|privacy-first|secure enclave|secure enclaves|intel sgx|amd sev|trustzone|end-to-end encryption|encrypt, compute"):
        add("Privacy/Compute Infra", 7, "official/project sources describe confidential compute or privacy infrastructure")
    if has(r"\b(api|sdk|dashboard|analytics|plugin|toolset|infrastructure|builder|build in public|hackathon)\b"):
        add("DevTool/Infra", 4, "project sources include developer or infrastructure terms")
    if has(r"\b(ai agent|ai agents|artificial intelligence|machine learning|neural|gpt|robot|bot|grok|openai|google ai|agi)\b"):
        add("AI", 4, "explicit AI or automation terms found in token/project context")
    if has(r"\b(anime|waifu|hentai|manga|neko|samurai|senpai|kawaii|japan|japanese|kantaro)\b|san chan"):
        add("Anime/Asia", 6, "profile or social context uses anime/Japan/kawaii branding")
    elif has(r"\b(kun|chan)\b"):
        add("Anime/Asia", 3, "token/profile includes anime-style honorific branding")
    if has(r"\b(dog|puppy|barking|shiba|cat|kitty|frog|pepe|bear|ape|monkey|giraffe|meow|pock|bird|fish|bull|whale|whally|underdog)\b"):
        add("Animals", 6, "profile/name uses animal mascot narrative")
    if re.search(r"whally|whale|puppy|barking|shiba", f"{pool.symbol} {pool.name} {profile.get('description') or ''}", re.I):
        add("Animals", 2, "animal mascot appears in token name or official profile")
    if has(r"trump|biden|maga|president|politic|party|tax|government|kalshi"):
        add("Politics/Prediction", 4, "political or prediction-market terms found")
    if has(r"worldcup|football|fifa|nba|ufc|suarez|cup|goal|sport"):
        add("Sports", 4, "sports terms found")
    if has(r"\b(defi|rwa|yield|liquidity protocol|staking|lending|borrow|perps|perpetual|amm|dex aggregator|tokenomics|treasury|revenue share|stablecoin)\b|ace round"):
        add("Finance/DeFi", 4, "explicit DeFi, protocol, or token-economics terms found")
    if has(r"roaring kitty|kevin14|tsuki|0\.14|buyback-burn|buyback burn|burning the supply|control the memes|psyop|mystery dev"):
        add("Classic Meme", 7, "Roaring Kitty, buyback-burn, or psyop lore found in public context")
    if has(r"troll|rage|wojak|meme|chud|incel|retard|lol|psyop"):
        add("Classic Meme", 3, "classic meme or psyop terms found")

    if not scores:
        primary = "Unclear"
        score = 1
        ranked = [("Unclear", 1)]
        evidence["Unclear"] = ["no dominant narrative after source enrichment"]
    else:
        priority = {
            "Gaming/Creator Infra": 90,
            "Privacy/Compute Infra": 85,
            "DevTool/Infra": 80,
            "Health/Bio": 75,
            "Animals": 66,
            "Anime/Asia": 65,
            "AI": 55,
            "Finance/DeFi": 50,
            "Politics/Prediction": 45,
            "Sports": 40,
            "Classic Meme": 10,
        }
        ranked = sorted(scores.items(), key=lambda item: (item[1], priority.get(item[0], 0)), reverse=True)
        primary, score = ranked[0]

    secondary = []
    for name, value in ranked[1:]:
        if value < 3:
            continue
        if primary in ("Gaming/Creator Infra", "Privacy/Compute Infra", "DevTool/Infra") and name in ("Animals", "Classic Meme") and value < 7:
            continue
        if primary != "Finance/DeFi" and name == "Finance/DeFi" and value < 5:
            continue
        if primary == "Health/Bio" and name == "Finance/DeFi" and value < 5:
            continue
        secondary.append(name)
    gap = score - (ranked[1][1] if len(ranked) > 1 else 0)
    tilt = "strong tilt" if score >= 6 and gap >= 2 else "medium tilt" if score >= 4 else "weak tilt"
    sources = []
    sources.append({"label": "GMGN profile", "url": f"https://gmgn.ai/sol/token/{pool.token_address}"})
    if pool.url:
        sources.append({"label": "Primary pair", "url": pool.url})
    for link in [*profile.get("links", []), *dex_info.get("links", [])]:
        sources.append({"label": link.get("label") or link.get("type") or "Source", "url": link.get("url")})
    for item in context[:3]:
        if item.get("url"):
            sources.append({"label": item.get("title") or "Context", "url": item["url"]})
    sources = dedupe_links(sources)

    overlay_type = "project" if primary in ("Gaming/Creator Infra", "Privacy/Compute Infra", "DevTool/Infra", "AI", "Finance/DeFi") else "news" if primary == "Health/Bio" else "narrative"
    overlay = {
        "headline": f"{overlay_type.title()} overlay: {primary}",
        "summary": (
            f"Scanner enriched this token with GMGN/Dexscreener metadata, official/social links, "
            f"and public context. Primary narrative is {primary} because: {'; '.join(evidence.get(primary, []))}."
        ),
        "sources": sources[:8],
        "type": overlay_type,
    }
    lore = build_lore_analysis(pool, primary, secondary, ranked, profile, dex_info, context, social, evidence, sources)
    overlay["headline"] = lore.get("headline") or overlay["headline"]
    overlay["summary"] = lore.get("summary") or overlay["summary"]
    return {
        "primary": primary,
        "secondary": secondary,
        "tilt": tilt,
        "score": score,
        "evidence": evidence.get(primary, []),
        "ranked": [{"name": name, "score": value, "evidence": evidence.get(name, [])} for name, value in ranked],
        "overlay": overlay,
        "lore": lore,
    }


def build_token_intel(http, pool, config, state, social=None):
    if not config.get("token_intel_enabled", True):
        return None
    token_key = pool.token_address or pool.pool_address
    if not token_key:
        return None
    cache = state.setdefault("token_intel_cache", {})
    now = int(time.time())
    ttl = int(config.get("token_intel_cache_ttl_minutes", 360)) * 60
    cached = cache.get(token_key)
    if cached and now - int(cached.get("cached_at", 0)) < ttl:
        intel = dict(cached.get("intel") or {})
        intel["cache"] = "hit"
        return intel

    profile = {}
    dex_info = {}
    context = []
    failures = []
    try:
        profile = fetch_gmgn_token_info(config, token_key)
    except Exception as exc:
        failures.append(f"gmgn_profile: {exc}")
    try:
        dex_info = fetch_dex_token_info(http, token_key)
    except Exception as exc:
        failures.append(f"dexscreener_profile: {exc}")
    official_profiles = []
    try:
        official_profiles = fetch_official_x_profiles(http, profile, dex_info, config)
    except Exception as exc:
        failures.append(f"official_x_profile: {exc}")
    bd_token = bright_data_token()
    if bd_token:
        context = request_project_context(http, pool, config, bd_token)
    context = dedupe_links([*official_profiles, *context])
    narrative = classify_token_narrative(pool, profile, dex_info, context, social)
    intel = {
        "enabled": True,
        "cache": "miss",
        "created_at": utc_now().isoformat().replace("+00:00", "Z"),
        "profile": profile,
        "dex": dex_info,
        "official_profiles": official_profiles,
        "context": context,
        "narrative": narrative,
        "failures": failures[:5],
    }
    cache[token_key] = {"cached_at": now, "intel": intel}
    return intel


def fetch_social_snapshot(http, pool, config, state):
    if not config.get("social_enabled", True):
        return None
    token = bright_data_token()
    if not token:
        return {"enabled": False, "reason": "missing_bright_data_api_key"}

    cache = state.setdefault("social_cache", {})
    cache_key = pool.token_address or pool.pool_address or pool.symbol
    now = int(time.time())
    ttl = int(config.get("social_cache_ttl_minutes", 120)) * 60
    cached = cache.get(cache_key)
    if cached and now - int(cached.get("cached_at", 0)) < ttl:
        snapshot = dict(cached.get("snapshot") or {})
        snapshot["cache"] = "hit"
        return snapshot

    results = []
    failures = []
    for query in build_social_queries(pool, config):
        try:
            payload = request_bright_data_discover(http, query, pool, config, token)
        except Exception as exc:
            failures.append(f"{query}: {exc}")
            continue
        if payload.get("error"):
            failures.append(f"{query}: {payload['error']}")
        for item in payload.get("results", []) or []:
            url = item.get("url") or item.get("link") or ""
            author = x_author_from_url(url)
            if not author:
                continue
            normalized = {
                "url": url,
                "author": author,
                "title": clean_social_text(item.get("title") or url),
                "description": clean_social_text(item.get("description") or item.get("content")),
                "relevance_score": to_float(item.get("relevance_score")),
            }
            if social_item_matches_pool(normalized, pool):
                results.append(normalized)
        time.sleep(float(config.get("social_request_delay_seconds", 0.5)))

    by_url = {}
    for item in results:
        by_url.setdefault(item["url"], item)
    results = sorted(
        by_url.values(),
        key=lambda item: (item.get("relevance_score") or 0.0, item.get("url") or ""),
        reverse=True,
    )[: int(config.get("social_max_results_per_token", 12))]
    metrics_by_url = scrape_x_post_metrics(http, [item["url"] for item in results], config, token)
    if metrics_by_url.get("_error"):
        failures.append(f"x_scrape: {metrics_by_url['_error']}")
        metrics_by_url = {}
    for item in results:
        metrics = metrics_by_url.get(item["url"])
        if metrics:
            item["metrics"] = metrics

    authors = Counter(item["author"] for item in results)
    watched = {name.lower().lstrip("@") for name in config.get("social_watched_accounts", [])}
    watched_hits = sorted({item["author"] for item in results if item["author"].lower().lstrip("@") in watched})
    post_count = len(results)
    unique_authors = len(authors)
    heat = "none"
    if post_count >= int(config.get("social_hot_results", 8)) and unique_authors >= 4:
        heat = "hot"
    elif post_count >= int(config.get("social_warming_results", 3)):
        heat = "warming"
    elif post_count:
        heat = "quiet"

    score = 0
    if post_count:
        score += 10
    if unique_authors >= 2:
        score += 10
    if watched_hits:
        score += 15
    if any((pool.token_address or "").lower() in (item.get("description", "") + item.get("title", "") + item.get("url", "")).lower() for item in results):
        score += 10
    if heat == "hot":
        score -= 10
    caller_graph = build_caller_graph(results, config.get("social_watched_accounts", []))
    if any((caller.get("followers") or 0) >= int(config.get("social_influencer_followers", 10_000)) for caller in caller_graph):
        score += 10

    snapshot = {
        "enabled": True,
        "cache": "miss",
        "created_at": utc_now().isoformat().replace("+00:00", "Z"),
        "heat": heat,
        "score": max(0, score),
        "x_posts": post_count,
        "unique_authors": unique_authors,
        "top_authors": [{"author": author, "posts": count} for author, count in authors.most_common(8)],
        "caller_graph": caller_graph[: int(config.get("social_caller_report_limit", 12))],
        "caller_metrics_status": "enriched" if any(item.get("metrics") for item in results) else "discover_only",
        "watched_account_hits": watched_hits,
        "results": results[: int(config.get("social_report_results", 5))],
        "failures": failures[:3],
    }
    cache[cache_key] = {"cached_at": now, "snapshot": snapshot}
    return snapshot


def enrich_alerts_with_social(http, alerts, config, state):
    if not alerts:
        return alerts
    social_max_tokens = int(config.get("social_max_tokens_per_scan", 5))
    token_intel_max_tokens = int(config.get("token_intel_max_tokens_per_scan", 50))
    enriched = []
    seen_tokens = {}
    social_tokens = 0
    token_intel_tokens = 0

    def within_limit(count, limit):
        return limit <= 0 or count < limit

    for alert in sorted(alerts, key=lambda item: item.get("score", 0), reverse=True):
        pool = Pool(**{key: value for key, value in alert["pool"].items() if key in Pool.__dataclass_fields__})
        token_key = pool.token_address or pool.pool_address
        if token_key in seen_tokens:
            alert["social"] = seen_tokens[token_key].get("social")
            alert["token_intel"] = seen_tokens[token_key].get("token_intel")
        else:
            snapshot = None
            if within_limit(social_tokens, social_max_tokens):
                snapshot = fetch_social_snapshot(http, pool, config, state)
                social_tokens += 1
            else:
                snapshot = {"enabled": True, "heat": "unchecked", "reason": "social_scan_limit"}
            token_intel = None
            if within_limit(token_intel_tokens, token_intel_max_tokens):
                token_intel = build_token_intel(http, pool, config, state, snapshot)
                token_intel_tokens += 1
            else:
                token_intel = {
                    "enabled": False,
                    "reason": "token_intel_scan_limit",
                    "created_at": utc_now().isoformat().replace("+00:00", "Z"),
                }
            seen_tokens[token_key] = {"social": snapshot, "token_intel": token_intel}
            alert["social"] = snapshot
            alert["token_intel"] = token_intel
        enriched.append(alert)
    return enriched


def token_amount(balance):
    amount = balance.get("uiTokenAmount", {}).get("amount")
    decimals = int(balance.get("uiTokenAmount", {}).get("decimals", 0))
    if amount is None:
        return to_float(balance.get("uiTokenAmount", {}).get("uiAmount"))
    return int(amount) / (10**decimals)


def parse_pool_swap(tx, pool):
    if not tx or tx.get("meta", {}).get("err"):
        return None
    meta = tx.get("meta", {})
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    signer = next((key.get("pubkey") for key in keys if key.get("signer")), keys[0].get("pubkey") if keys else "")

    pre = {}
    post = {}
    for balance in meta.get("preTokenBalances", []) or []:
        pre[(balance.get("accountIndex"), balance.get("mint"), balance.get("owner"))] = token_amount(balance)
    for balance in meta.get("postTokenBalances", []) or []:
        post[(balance.get("accountIndex"), balance.get("mint"), balance.get("owner"))] = token_amount(balance)

    token_mint = pool.token_address
    if not token_mint:
        token_mint = next((mint for _, mint, owner in set(pre) | set(post) if owner == pool.pool_address and mint != SOL_MINT), "")

    pool_token_delta = 0.0
    pool_sol_delta = 0.0
    signer_token_delta = 0.0
    token_owner_deltas = defaultdict(float)
    for key in set(pre) | set(post):
        _, mint, owner = key
        delta = post.get(key, 0.0) - pre.get(key, 0.0)
        if owner == pool.pool_address and mint == token_mint:
            pool_token_delta += delta
        if owner == pool.pool_address and mint == SOL_MINT:
            pool_sol_delta += delta
        if owner == signer and mint == token_mint:
            signer_token_delta += delta
        if owner and owner != pool.pool_address and mint == token_mint:
            token_owner_deltas[owner] += delta

    signer_lamport_delta = 0.0
    for index, key in enumerate(keys):
        if key.get("pubkey") != signer:
            continue
        if index < len(meta.get("preBalances", [])) and index < len(meta.get("postBalances", [])):
            signer_lamport_delta += (meta["postBalances"][index] - meta["preBalances"][index]) / 1_000_000_000

    kind = None
    sol_amount = 0.0
    token_amount_value = 0.0
    if pool_token_delta < -1e-8 and pool_sol_delta > 1e-10:
        kind = "buy"
        sol_amount = pool_sol_delta
        token_amount_value = -pool_token_delta
    elif pool_token_delta > 1e-8 and pool_sol_delta < -1e-10:
        kind = "sell"
        sol_amount = -pool_sol_delta
        token_amount_value = pool_token_delta
    elif signer_token_delta > 1e-8:
        kind = "buy"
        token_amount_value = signer_token_delta
        sol_amount = max(0.0, -signer_lamport_delta)
    elif signer_token_delta < -1e-8:
        kind = "sell"
        token_amount_value = -signer_token_delta
        sol_amount = max(0.0, signer_lamport_delta)

    if not kind:
        return None

    positive_token_owners = sorted(
        ((owner, delta) for owner, delta in token_owner_deltas.items() if delta > 1e-8),
        key=lambda item: item[1],
        reverse=True,
    )
    negative_token_owners = sorted(
        ((owner, -delta) for owner, delta in token_owner_deltas.items() if delta < -1e-8),
        key=lambda item: item[1],
        reverse=True,
    )
    token_recipient = positive_token_owners[0][0] if positive_token_owners else signer
    token_recipient_amount = positive_token_owners[0][1] if positive_token_owners else max(0.0, signer_token_delta)
    token_sender = negative_token_owners[0][0] if negative_token_owners else signer
    token_sender_amount = negative_token_owners[0][1] if negative_token_owners else max(0.0, -signer_token_delta)
    routed = kind == "buy" and token_recipient != signer
    recipient_share = token_recipient_amount / token_amount_value if token_amount_value else 0.0
    if kind != "buy":
        owner_resolution = "not_applicable"
    elif token_recipient == signer:
        owner_resolution = "signer"
    elif token_recipient and recipient_share >= 0.80:
        owner_resolution = "token_recipient"
    else:
        owner_resolution = "unresolved"

    return {
        "signature": tx.get("transaction", {}).get("signatures", [""])[0],
        "block_time": tx.get("blockTime"),
        "time": iso(tx.get("blockTime")),
        "pool_address": pool.pool_address,
        "token_address": token_mint,
        "symbol": pool.symbol,
        "kind": kind,
        "signer": signer,
        "token_recipient": token_recipient,
        "token_recipient_amount": token_recipient_amount,
        "token_sender": token_sender,
        "token_sender_amount": token_sender_amount,
        "recipient_share": recipient_share,
        "routed": routed,
        "owner_resolution": owner_resolution,
        "sol_amount": sol_amount,
        "token_amount": token_amount_value,
        "price_native": sol_amount / token_amount_value if token_amount_value else 0.0,
    }


def wallet_cache_key(wallet, before_signature, buy_time=None, bucket_hours=6):
    if buy_time and bucket_hours:
        bucket_seconds = max(1, int(float(bucket_hours) * 3600))
        return f"{wallet}:t{int(buy_time) // bucket_seconds}"
    return f"{wallet}:{before_signature}"


def classify_wallet(rpc, wallet, before_signature, buy_time, config, state):
    cache_key = wallet_cache_key(
        wallet,
        before_signature,
        buy_time,
        config.get("wallet_cache_bucket_hours", 6),
    )
    wallet_cache = state.setdefault("wallet_cache", {})
    if cache_key in wallet_cache:
        return wallet_cache[cache_key]

    previous = rpc.signatures_for_address(wallet, limit=50, before=before_signature)
    count = len(previous)
    prev = previous[0] if previous else None
    gap = None
    if prev and prev.get("blockTime") and buy_time:
        gap = buy_time - prev["blockTime"]

    if count == 0:
        wallet_class = "fresh"
    elif count <= int(config["freshish_max_previous_txs"]):
        wallet_class = "freshish"
    elif gap is not None and gap >= int(config["dormant_gap_days"]) * 86400:
        wallet_class = "dormant"
    elif count <= int(config["low_tx_max_previous_txs"]):
        wallet_class = "low_tx"
    else:
        wallet_class = "normal"

    funding_source = None
    funding_sol = 0.0
    if wallet_class in ("fresh", "freshish") and prev:
        funding_source, funding_sol = extract_funding_source(rpc, prev.get("signature"), wallet)

    result = {
        "wallet": wallet,
        "cached_at": int(time.time()),
        "as_of_signature": before_signature,
        "as_of_time": int(buy_time or 0),
        "wallet_class": wallet_class,
        "previous_tx_count_50": count,
        "previous_gap_seconds": gap,
        "previous_signature": prev.get("signature") if prev else None,
        "previous_time": iso(prev.get("blockTime")) if prev else None,
        "funding_source": funding_source,
        "funding_sol": funding_sol,
    }
    wallet_cache[cache_key] = result
    return result


def extract_funding_source(rpc, signature, target_wallet):
    if not signature:
        return None, 0.0
    try:
        tx = rpc.transaction(signature)
    except Exception:
        return None, 0.0
    if not tx or tx.get("meta", {}).get("err"):
        return None, 0.0

    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    meta = tx.get("meta", {})
    deltas = []
    target_delta = 0.0
    for index, key in enumerate(keys):
        if index >= len(meta.get("preBalances", [])) or index >= len(meta.get("postBalances", [])):
            continue
        delta = (meta["postBalances"][index] - meta["preBalances"][index]) / 1_000_000_000
        pubkey = key.get("pubkey")
        if pubkey == target_wallet:
            target_delta += delta
        if abs(delta) >= 0.01:
            deltas.append((pubkey, delta))
    if target_delta <= 0.1:
        return None, 0.0
    senders = sorted((item for item in deltas if item[1] < -0.01), key=lambda item: item[1])
    if not senders:
        return None, target_delta
    return senders[0][0], target_delta


HARD_WALLET_CLASSES = {"fresh", "freshish", "dormant"}
SUPPORT_WALLET_CLASSES = {"low_tx"}


def wallet_count(events):
    return len({event.get("signer") for event in events if event.get("signer")})


def sol_sum(events):
    return sum(event.get("sol_amount", 0.0) for event in events)


def dedupe_pool_alerts(alerts, limit=5):
    deduped = {}
    for alert in alerts:
        pool = alert.get("pool") or {}
        key = (
            pool.get("pool_address") or pool.get("token_address") or "pool",
            alert.get("signal_family") or "classified_wallets",
            alert.get("window_start") or alert.get("created_at") or "",
            alert.get("window_end") or "",
        )
        existing = deduped.get(key)
        if not existing:
            deduped[key] = alert
            continue
        if (alert["score"], alert["suspicious_sol"], alert["suspicious_wallets"]) > (
            existing["score"],
            existing["suspicious_sol"],
            existing["suspicious_wallets"],
        ):
            deduped[key] = alert
    return sorted(deduped.values(), key=lambda alert: alert["score"], reverse=True)[:limit]


def score_events(events, config):
    hard_events = [event for event in events if event.get("wallet_class") in HARD_WALLET_CLASSES]
    support_events = [event for event in events if event.get("wallet_class") in SUPPORT_WALLET_CLASSES]
    suspicious = [*hard_events, *support_events]
    hard_wallet_count = wallet_count(hard_events)
    support_wallet_count = wallet_count(support_events)
    hard_sol = sol_sum(hard_events)
    support_sol = sol_sum(support_events)
    hard_classes = Counter(event.get("wallet_class") for event in hard_events)
    support_classes = Counter(event.get("wallet_class") for event in support_events)
    min_wallets = int(config["alert_min_suspicious_wallets"])
    min_sol = float(config["alert_min_suspicious_sol"])

    funding_sources = Counter(event.get("funding_source") for event in suspicious if event.get("funding_source"))
    token_recipients = Counter(
        event.get("token_recipient")
        for event in suspicious
        if event.get("token_recipient") and not event.get("routed")
    )
    common_funders = [
        {"source": source, "wallets": count}
        for source, count in funding_sources.items()
        if count >= 2
    ]
    common_recipients = [
        {"recipient": recipient, "txs": count}
        for recipient, count in token_recipients.items()
        if count >= 2
    ]

    score = 0
    if hard_classes.get("dormant", 0):
        score += 35
    if hard_wallet_count >= min_wallets:
        score += 25
    elif hard_wallet_count >= max(2, min_wallets - 1):
        score += 15
    if hard_sol >= min_sol:
        score += 25
    elif hard_sol >= min_sol * 0.5:
        score += 10
    if common_funders:
        score += 25
    if common_recipients:
        score += 20
    if any(event.get("sol_amount", 0.0) >= float(config["big_buy_sol"]) for event in hard_events):
        score += 10
    if support_wallet_count >= min_wallets and (hard_wallet_count or common_funders or common_recipients):
        score += 10
    elif support_wallet_count >= min_wallets * 2:
        score += 5

    evidence = {
        "hard_wallets": hard_wallet_count,
        "support_wallets": support_wallet_count,
        "hard_sol": hard_sol,
        "support_sol": support_sol,
        "hard_classes": dict(hard_classes),
        "support_classes": dict(support_classes),
        "support_only": bool(support_events) and not hard_events and not common_funders and not common_recipients,
    }
    return min(score, 100), suspicious, common_funders, common_recipients, evidence


def pool_ath_ratio(pool):
    existing_ratio = getattr(pool, "ath_current_ratio", None)
    if existing_ratio is not None:
        return float(existing_ratio)
    ath_mcap = float(getattr(pool, "ath_mcap_usd", 0) or 0)
    current_mcap = float(getattr(pool, "mcap_usd", 0) or 0)
    return current_mcap / ath_mcap if ath_mcap and current_mcap else None


def is_hot_reactivation_signal(pool, alert, config):
    if (config.get("lane") or config.get("mode")) != "reactivation":
        return False
    if alert.get("signal_family") != "reactivation_wave":
        return False
    wave = alert.get("wave") or {}
    baseline = alert.get("reactivation_baseline") or {}
    baseline_allows_hot = bool(
        baseline.get("status") != "ready"
        or baseline.get("reactivation_confirmed")
        or not config.get("reactivation_baseline_required_for_actionable", True)
    )
    mcap = float(getattr(pool, "mcap_usd", 0) or 0)
    balance_coverage = float(wave.get("balance_coverage_pct") or 0)
    return bool(
        0 < mcap <= float(config.get("reactivation_hot_max_mcap_usd", 250_000))
        and int(alert.get("score") or 0)
        >= int(config.get("reactivation_hot_min_score", 75))
        and float(wave.get("net_buy_sol") or 0)
        >= float(config.get("reactivation_hot_min_net_buy_sol", 25))
        and int(wave.get("unique_buyers") or 0)
        >= int(config.get("reactivation_hot_min_unique_buyers", 15))
        and int(
            wave.get("effective_unique_buyers")
            or wave.get("unique_buyers")
            or 0
        )
        >= int(config.get("reactivation_wallet_graph_min_effective_buyers", 6))
        and int(wave.get("sticky_wallets") or 0)
        >= int(config.get("reactivation_hot_min_sticky_wallets", 8))
        and float(wave.get("sticky_supply_pct") or 0)
        >= float(config.get("reactivation_hot_min_sticky_supply_pct", 5))
        and float(wave.get("sticky_bought_pct") or 0)
        >= float(config.get("reactivation_hot_min_sticky_bought_pct", 40))
        and float(wave.get("net_token_retention_pct") or 0)
        >= float(config.get("reactivation_hot_min_net_token_retention_pct", 50))
        and float(wave.get("top_buyer_share") or 0)
        <= float(config.get("reactivation_hot_max_top_buyer_share", 0.35))
        and float(wave.get("top3_buyer_share") or 0)
        <= float(config.get("reactivation_hot_max_top3_buyer_share", 0.6))
        and float(wave.get("max_linked_cluster_share") or 0)
        <= float(
            config.get(
                "reactivation_wallet_graph_actionable_max_cluster_share",
                0.5,
            )
        )
        and balance_coverage
        >= float(config.get("actionable_min_balance_coverage_pct", 80))
        and baseline_allows_hot
    )


def classify_alert_tier(pool, alert, evidence, config):
    lane = config.get("lane") or config.get("mode") or "scan"
    mcap = float(getattr(pool, "mcap_usd", 0) or 0)
    volume_1h = float(getattr(pool, "volume_1h_usd", 0) or 0)
    volume_to_mcap = volume_1h / mcap if mcap else None
    min_wallets = int(config["alert_min_suspicious_wallets"])
    min_sol = float(config["alert_min_suspicious_sol"])
    actionable_mcap = float(config.get("actionable_mcap_max_usd") or 0)
    watch_mcap = float(config.get("watch_mcap_max_usd") or config.get("mcap_max_usd") or 0)
    action_volume_ratio = config.get("volume_1h_to_mcap_max_actionable")
    watch_volume_ratio = config.get("volume_1h_to_mcap_max_watch")
    action_volume_ratio = float(action_volume_ratio) if action_volume_ratio is not None else None
    watch_volume_ratio = float(watch_volume_ratio) if watch_volume_ratio is not None else None
    reasons = []
    penalties = []

    hard_wallets = int(evidence.get("hard_wallets") or 0)
    support_wallets = int(evidence.get("support_wallets") or 0)
    hard_sol = float(evidence.get("hard_sol") or 0)
    hard_classes = evidence.get("hard_classes") or {}
    coordination = bool(
        alert.get("common_funders")
        or alert.get("common_recipients")
        or alert.get("common_executors")
    )
    hard_cluster = hard_wallets >= min_wallets
    hard_flow = hard_sol >= min_sol
    dormant = bool(hard_classes.get("dormant"))
    support_only = bool(evidence.get("support_only"))
    wave = alert.get("wave") or {}
    wave_signal = bool(alert.get("signal_family") == "reactivation_wave" or wave)
    sticky_supply_pct = float(wave.get("sticky_supply_pct") or 0)
    sticky_bought_pct = float(wave.get("sticky_bought_pct") or 0)
    net_retention_pct = float(wave.get("net_token_retention_pct") or 0)
    sticky_net_sol = float(wave.get("sticky_net_sol") or 0)
    top_buyer_share = float(wave.get("top_buyer_share") or 0)
    top3_buyer_share = float(wave.get("top3_buyer_share") or 0)
    effective_unique_buyers = int(
        wave.get("effective_unique_buyers")
        or wave.get("unique_buyers")
        or 0
    )
    max_linked_cluster_share = float(wave.get("max_linked_cluster_share") or 0)
    hold_age_minutes = float(wave.get("hold_age_minutes") or 0)
    min_hold_minutes = float(wave.get("min_hold_minutes") or 0)
    retention_seasoned = not wave_signal or hold_age_minutes >= min_hold_minutes
    balance_coverage_value = wave.get("balance_coverage_pct")
    balance_coverage_pct = 100.0 if balance_coverage_value is None else float(balance_coverage_value)
    concentration_prefix = "sticky_accumulation" if alert.get("signal_family") == "sticky_accumulation" else "reactivation_wave"
    max_top_buyer_share = float(config.get(f"{concentration_prefix}_max_top_buyer_share", 1.0))
    max_top3_buyer_share = float(config.get(f"{concentration_prefix}_max_top3_buyer_share", 1.0))
    concentrated_wave = bool(
        wave_signal
        and (top_buyer_share > max_top_buyer_share or top3_buyer_share > max_top3_buyer_share)
    )
    linked_concentration = bool(
        wave_signal
        and max_linked_cluster_share
        > float(config.get("reactivation_wallet_graph_actionable_max_cluster_share", 0.5))
    )
    linked_noise = bool(
        wave_signal
        and max_linked_cluster_share
        > float(config.get("reactivation_wallet_graph_noise_cluster_share", 0.75))
    )
    hot_reactivation = is_hot_reactivation_signal(pool, alert, config)
    baseline = alert.get("reactivation_baseline") or {}
    baseline_ready = baseline.get("status") == "ready"
    baseline_confirmed = bool(baseline.get("reactivation_confirmed"))

    if hard_cluster:
        reasons.append("hard wallet cluster")
    if hard_flow:
        reasons.append("hard flow")
    if dormant:
        reasons.append("dormant wallet")
    if coordination:
        reasons.append("linked wallets")
    if support_wallets:
        reasons.append("low_tx support")
    if wave_signal:
        if alert.get("signal_family") == "sticky_accumulation":
            reasons.append("sticky cheap accumulation")
        else:
            reasons.append("market-wide buying wave")
        if sticky_supply_pct:
            reasons.append("sticky buyers")
    if hot_reactivation:
        reasons.append("early reactivation ignition")
    if baseline_confirmed:
        reasons.append("quiet-regime break")

    if support_only:
        penalties.append("low_tx only")
    if concentrated_wave:
        penalties.append("concentrated buyer flow")
    if linked_concentration:
        penalties.append("linked wallet concentration")
    if wave_signal and not retention_seasoned:
        penalties.append("retention not seasoned")
    if wave_signal and balance_coverage_pct < float(config.get("actionable_min_balance_coverage_pct", 80)):
        penalties.append("partial balance coverage")
    if (
        lane == "reactivation"
        and baseline_ready
        and not baseline_confirmed
        and config.get("reactivation_baseline_required_for_actionable", True)
    ):
        penalties.append("no confirmed quiet-regime break")
    if actionable_mcap and mcap > actionable_mcap:
        penalties.append("above actionable mcap")
    if watch_mcap and mcap > watch_mcap:
        penalties.append("late mcap")
    if volume_to_mcap is not None and watch_volume_ratio is not None and volume_to_mcap > watch_volume_ratio:
        penalties.append("high-velocity ignition" if hot_reactivation else "blowoff volume")
    elif volume_to_mcap is not None and action_volume_ratio is not None and volume_to_mcap > action_volume_ratio:
        penalties.append("hot volume")
    if (
        float(alert.get("suspicious_sol") or 0) >= float(config.get("excess_flow_sol", 100))
        and actionable_mcap
        and mcap > actionable_mcap
        and not coordination
    ):
        penalties.append("excess flow after move")

    ath_ratio = pool_ath_ratio(pool)
    if lane == "reactivation" and ath_ratio is not None:
        action_ratio = float(config.get("ath_actionable_max_current_ratio", 0.25))
        watch_ratio = float(config.get("ath_watch_max_current_ratio", config.get("ath_max_current_ratio", 0.4)))
        if ath_ratio > watch_ratio:
            penalties.append("too close to ATH")
        elif ath_ratio > action_ratio:
            penalties.append("mid-range, not deep correction")
    elif lane == "reactivation":
        penalties.append("ath unverified")

    wave_actionable = (
        wave_signal
        and retention_seasoned
        and "partial balance coverage" not in penalties
        and sticky_supply_pct >= float(config.get("reactivation_wave_actionable_sticky_supply_pct", 5.0))
        and sticky_bought_pct >= float(config.get("reactivation_wave_actionable_sticky_bought_pct", 50.0))
        and net_retention_pct >= float(config.get("reactivation_wave_actionable_net_token_retention_pct", 75.0))
        and effective_unique_buyers
        >= int(config.get("reactivation_wallet_graph_min_effective_buyers", 6))
        and not linked_concentration
    )
    hard_signal = hard_cluster or hard_flow or dormant or coordination or wave_signal
    if concentrated_wave or linked_noise:
        tier = "noise"
    elif not hard_signal:
        tier = "noise" if support_only else "watch"
    elif hot_reactivation:
        tier = "hot_reactivation"
    elif "late mcap" in penalties or "blowoff volume" in penalties or "excess flow after move" in penalties:
        tier = "late_chase"
    elif lane == "reactivation":
        clean_ath = (
            "mid-range, not deep correction" not in penalties
            and "too close to ATH" not in penalties
            and "ath unverified" not in penalties
        )
        classic_actionable = coordination and (hard_cluster or hard_flow or dormant)
        baseline_allows_actionable = (
            not baseline_ready
            or baseline_confirmed
            or not config.get("reactivation_baseline_required_for_actionable", True)
        )
        tier = (
            "actionable"
            if clean_ath
            and baseline_allows_actionable
            and (classic_actionable or wave_actionable)
            else "watch"
        )
    elif lane in ("micro_sticky", "cheap_sticky"):
        sticky_actionable = (
            wave_signal
            and retention_seasoned
            and "partial balance coverage" not in penalties
            and sticky_supply_pct >= float(config.get("sticky_accumulation_actionable_sticky_supply_pct", 5.0))
            and sticky_net_sol >= float(config.get("sticky_accumulation_min_sticky_net_sol", 0.0))
            and sticky_bought_pct >= float(config.get("sticky_accumulation_actionable_sticky_bought_pct", 50.0))
            and net_retention_pct >= float(config.get("sticky_accumulation_actionable_net_token_retention_pct", 70.0))
        )
        classic_actionable = coordination and (hard_cluster or hard_flow or dormant)
        clean_entry = "above actionable mcap" not in penalties and "hot volume" not in penalties
        tier = "actionable" if clean_entry and (sticky_actionable or classic_actionable) else "watch"
    elif actionable_mcap and mcap > actionable_mcap:
        tier = "watch"
    elif "hot volume" in penalties and not coordination:
        tier = "watch"
    else:
        tier = "actionable"

    return tier, reasons, penalties, {
        "volume_1h_to_mcap": volume_to_mcap,
        "ath_current_ratio": ath_ratio,
        "top_buyer_share": top_buyer_share,
        "top3_buyer_share": top3_buyer_share,
        "effective_unique_buyers": effective_unique_buyers,
        "max_linked_cluster_share": max_linked_cluster_share,
        "hold_age_minutes": hold_age_minutes,
        "min_hold_minutes": min_hold_minutes,
        "balance_coverage_pct": balance_coverage_pct,
        "hot_reactivation": hot_reactivation,
        "reactivation_stage": config.get("reactivation_stage"),
        "reactivation_baseline_status": baseline.get("status"),
        "reactivation_baseline_confirmed": baseline_confirmed,
        "reactivation_quiet_hours": float(baseline.get("quiet_hours") or 0),
        "reactivation_volume_ratio": float(baseline.get("volume_1h_ratio") or 0),
        "reactivation_txn_ratio": float(baseline.get("txns_1h_ratio") or 0),
    }


def build_alerts(pool, events, config):
    if not events:
        return []
    window_seconds = int(config["alert_window_minutes"]) * 60
    events = sorted(events, key=lambda event: event.get("block_time") or 0)
    alerts = []
    for index, event in enumerate(events):
        start = event.get("block_time") or 0
        end = start + window_seconds
        window = [item for item in events[index:] if (item.get("block_time") or 0) <= end]
        score, suspicious, common_funders, common_recipients, evidence = score_events(window, config)
        suspicious_wallet_count = len({item["signer"] for item in suspicious})
        suspicious_sol = sum(item.get("sol_amount", 0.0) for item in suspicious)
        if score < 40:
            continue
        hard_signal = (
            evidence["hard_wallets"] >= int(config["alert_min_suspicious_wallets"])
            or evidence["hard_sol"] >= float(config["alert_min_suspicious_sol"])
            or bool(evidence["hard_classes"].get("dormant"))
            or bool(common_funders)
            or bool(common_recipients)
        )
        if evidence["support_only"] and not config.get("low_tx_support_only_alerts", False):
            continue
        if (
            not hard_signal
            and suspicious_wallet_count < int(config["alert_min_suspicious_wallets"])
            and suspicious_sol < float(config["alert_min_suspicious_sol"])
        ):
            continue
        created_at = utc_now().isoformat().replace("+00:00", "Z")
        alert = {
            "created_at": created_at,
            "score": score,
            "lane": config.get("lane") or config.get("mode"),
            "reactivation_baseline": getattr(
                pool,
                "reactivation_baseline",
                {"status": "warming", "reactivation_confirmed": False},
            ),
            "pool": pool.as_dict(),
            "obs_mcap_usd": pool.mcap_usd,
            "obs_price_usd": pool.price_usd,
            "obs_liquidity_usd": pool.liquidity_usd,
            "obs_mcap_at": created_at,
            "window_start": iso(start),
            "window_end": iso(end),
            "suspicious_wallets": suspicious_wallet_count,
            "suspicious_sol": suspicious_sol,
            "hard_wallets": evidence["hard_wallets"],
            "support_wallets": evidence["support_wallets"],
            "hard_sol": evidence["hard_sol"],
            "support_sol": evidence["support_sol"],
            "classes": dict(Counter(item.get("wallet_class") for item in suspicious)),
            "hard_classes": evidence["hard_classes"],
            "support_classes": evidence["support_classes"],
            "common_funders": common_funders,
            "common_recipients": common_recipients,
            "routed_buys": sum(1 for item in suspicious if item.get("routed")),
            "events": suspicious[: int(config.get("alert_event_export_limit", 80))],
        }
        tier, reasons, penalties, quality_metrics = classify_alert_tier(pool, alert, evidence, config)
        alert.update(
            {
                "action_tier": tier,
                "quality_reasons": reasons,
                "quality_penalties": penalties,
                "quality_metrics": quality_metrics,
            }
        )
        alerts.append(alert)
    return dedupe_pool_alerts(alerts)


WAVE_SWAP_FIELDS = (
    "signature",
    "block_time",
    "kind",
    "signer",
    "token_recipient",
    "token_recipient_amount",
    "token_sender",
    "token_sender_amount",
    "recipient_share",
    "routed",
    "owner_resolution",
    "sol_amount",
    "token_amount",
    "price_native",
)


def reactivation_wave_enabled(config):
    return (
        (config.get("lane") or config.get("mode")) == "reactivation"
        and bool(config.get("reactivation_wave_enabled", False))
    )


def compact_wave_swap(swap):
    return {key: swap.get(key) for key in WAVE_SWAP_FIELDS if swap.get(key) not in (None, "")}


def merge_reactivation_wave_swaps(pool_state, swaps, config):
    if not reactivation_wave_enabled(config):
        return swaps
    buffer_seconds = int(config.get("reactivation_wave_buffer_minutes", config["alert_window_minutes"])) * 60
    max_swaps = int(config.get("reactivation_wave_buffer_max_swaps", 800))
    recent_swaps = [compact_wave_swap(swap) for swap in swaps if swap.get("kind") in ("buy", "sell")]
    existing_swaps = [
        item
        for item in pool_state.get("reactivation_wave_swaps", [])
        if isinstance(item, dict) and item.get("kind") in ("buy", "sell")
    ]
    newest = max(
        [int(time.time())]
        + [int(item.get("block_time") or 0) for item in [*existing_swaps, *recent_swaps] if item.get("block_time")]
    )
    cutoff = newest - buffer_seconds if buffer_seconds > 0 else 0
    by_signature = {}
    fallback_index = 0
    for item in [*existing_swaps, *recent_swaps]:
        block_time = int(item.get("block_time") or 0)
        if cutoff and block_time and block_time < cutoff:
            continue
        signature = item.get("signature") or f"fallback:{fallback_index}"
        fallback_index += 1
        by_signature[signature] = item
    merged = sorted(by_signature.values(), key=lambda item: (item.get("block_time") or 0, item.get("signature") or ""))
    buffer_truncated = bool(max_swaps > 0 and len(merged) > max_swaps)
    if buffer_truncated:
        merged = merged[-max_swaps:]
    pool_state["reactivation_wave_swaps"] = merged
    pool_state["reactivation_wave_buffer_truncated"] = buffer_truncated
    return merged


def wave_buy_owner(swap):
    recipient = swap.get("token_recipient") or ""
    signer = swap.get("signer") or ""
    if not swap.get("routed"):
        return recipient or signer
    resolution = swap.get("owner_resolution")
    recipient_share = float(swap.get("recipient_share") or 0)
    if resolution == "token_recipient" or (recipient and recipient_share >= 0.80):
        return recipient
    return ""


def wave_sell_owner(swap, known_owners=None):
    signer = swap.get("signer") or ""
    token_sender = swap.get("token_sender") or ""
    if known_owners:
        known_owners = set(known_owners)
        if token_sender in known_owners:
            return token_sender
        if signer in known_owners:
            return signer
    return signer or token_sender


def attributed_wave_retention(balance, bought_tokens, sold_tokens, min_retention_pct=0.0):
    balance = max(0.0, float(balance or 0))
    bought_tokens = max(0.0, float(bought_tokens or 0))
    sold_tokens = max(0.0, float(sold_tokens or 0))
    net_tokens = max(0.0, bought_tokens - sold_tokens)
    retained_tokens = min(balance, net_tokens)
    retention_pct = retained_tokens / bought_tokens * 100 if bought_tokens else 0.0
    net_coverage_pct = retained_tokens / net_tokens * 100 if net_tokens else 0.0
    return {
        "net_tokens": net_tokens,
        "retained_tokens": retained_tokens,
        "retention_pct": retention_pct,
        "net_coverage_pct": net_coverage_pct,
        "qualified": bool(retained_tokens > 0 and retention_pct >= float(min_retention_pct or 0)),
    }


def cohort_retention_totals(cohort):
    """Return aggregates from exactly the cohort rows exposed to the dashboard."""
    attributed = 0.0
    retained = 0.0
    holders = 0
    for row in cohort or []:
        if not isinstance(row, dict):
            continue
        row_attributed = max(0.0, float(row.get("attributed_tokens") or 0))
        row_retained = min(
            row_attributed,
            max(0.0, float(row.get("current_retained_tokens") or 0)),
        )
        attributed += row_attributed
        retained += row_retained
        holders += int(bool(row.get("is_holder")))
    return {
        "attributed_tokens": attributed,
        "retained_tokens": retained,
        "holders": holders,
        "retention_pct": retained / attributed * 100 if attributed else 0.0,
    }


def classified_alert_cohort(alert, wallet_limit):
    by_owner = {}
    for event in alert.get("events") or []:
        if (
            not isinstance(event, dict)
            or event.get("kind") != "buy"
            or event.get("wallet_class")
            not in HARD_WALLET_CLASSES | SUPPORT_WALLET_CLASSES
        ):
            continue
        owner = wave_buy_owner(event)
        attributed_tokens = max(
            0.0,
            float(
                event.get("token_amount")
                or event.get("token_recipient_amount")
                or 0
            ),
        )
        if not owner or attributed_tokens <= 0:
            continue
        row = by_owner.setdefault(
            owner,
            {
                "owner": owner,
                "attributed_tokens": 0.0,
                "initial_balance": 0.0,
                "buy_sol": 0.0,
                "first_buy_time": 0,
                "wallet_class": event.get("wallet_class"),
            },
        )
        row["attributed_tokens"] += attributed_tokens
        row["initial_balance"] += attributed_tokens
        row["buy_sol"] += max(0.0, float(event.get("sol_amount") or 0))
        event_time = parse_timestamp(event.get("block_time") or event.get("time"))
        if event_time and (
            not row["first_buy_time"]
            or event_time < row["first_buy_time"]
        ):
            row["first_buy_time"] = event_time
    return sorted(
        by_owner.values(),
        key=lambda row: (row["buy_sol"], row["attributed_tokens"]),
        reverse=True,
    )[:wallet_limit]


def verified_alert_cluster_members(alert):
    """Map only explicit common funder/executor evidence onto cohort wallets."""
    funders = {}
    executors = {}
    for group in alert.get("common_funders") or []:
        if not isinstance(group, dict):
            continue
        source = str(group.get("source") or "").strip()
        members = group.get("members") or []
        if not source or not isinstance(members, list):
            continue
        for owner in members:
            if owner:
                funders[str(owner)] = source
    for group in alert.get("common_executors") or []:
        if not isinstance(group, dict):
            continue
        executor = str(group.get("executor") or "").strip()
        members = group.get("members") or []
        if not executor or not isinstance(members, list):
            continue
        for owner in members:
            if owner:
                executors[str(owner)] = executor
    return funders, executors


def signal_thesis_from_alert(alert, config, captured_at=None):
    if not isinstance(alert, dict):
        return None
    wave = alert.get("wave") or {}
    wave_rows = wave.get("top_buyers") or []
    cohort = []
    common_funders, common_executors = verified_alert_cluster_members(alert)
    wallet_limit = max(1, int(config.get("signal_thesis_wallet_limit", 40)))
    holder_min_pct = max(
        0.0,
        float(config.get("signal_thesis_wallet_holder_min_pct", 10)),
    )
    if wave_rows:
        for row in wave_rows[:wallet_limit]:
            if not isinstance(row, dict):
                continue
            owner = str(row.get("owner") or "").strip()
            if not owner:
                continue
            attributed_tokens = max(
                0.0,
                float(
                    row.get("token_bought")
                    or row.get("retained_from_wave")
                    or row.get("current_balance")
                    or 0
                ),
            )
            if attributed_tokens <= 0:
                continue
            current_balance = max(0.0, float(row.get("current_balance") or 0))
            retained_tokens = min(
                attributed_tokens,
                max(
                    0.0,
                    float(row.get("retained_from_wave") or current_balance),
                ),
            )
            retention_pct = (
                retained_tokens / attributed_tokens * 100
                if attributed_tokens
                else 0.0
            )
            cohort.append(
                {
                    "owner": owner,
                    "attributed_tokens": attributed_tokens,
                    "initial_balance": current_balance,
                    "buy_sol": max(0.0, float(row.get("buy_sol") or 0)),
                    "first_buy_time": parse_timestamp(
                        row.get("first_buy_time")
                    ),
                    "wallet_class": row.get("wallet_class"),
                    "common_funder": common_funders.get(owner),
                    "common_executor": common_executors.get(owner),
                    "current_balance": current_balance,
                    "current_retained_tokens": retained_tokens,
                    "retention_pct": retention_pct,
                    "is_holder": retention_pct >= holder_min_pct,
                }
            )
    else:
        cohort = classified_alert_cohort(alert, wallet_limit)
        for row in cohort:
            owner = str(row.get("owner") or "")
            row["common_funder"] = common_funders.get(owner)
            row["common_executor"] = common_executors.get(owner)
    original_tokens = sum(row["attributed_tokens"] for row in cohort)
    if not cohort or original_tokens <= 0:
        return None

    wave_signal = bool(wave_rows)
    pool = alert.get("pool") or {}
    supply = max(0.0, float(wave.get("supply") or 0))
    source_attributed_tokens = max(
        original_tokens,
        float(wave.get("checked_bought_tokens") or 0),
    )
    source_signal_wallets = max(
        len(cohort),
        int(
            wave.get("checked_wallets")
            or wave.get("unique_buyers")
            or wave.get("sticky_wallets")
            or alert.get("suspicious_wallets")
            or len(cohort)
        ),
    )
    signal_at = (
        alert.get("created_at")
        or alert.get("window_end")
        or captured_at
        or utc_now().isoformat().replace("+00:00", "Z")
    )
    if wave_signal:
        for row in cohort:
            row["checked_at"] = signal_at
    initial_totals = cohort_retention_totals(cohort)
    initial_balance_coverage = float(
        wave.get("balance_coverage_pct") or (100.0 if wave_signal else 0.0)
    )
    initial_holder_retention_pct = (
        initial_totals["holders"] / len(cohort) * 100 if cohort else 0.0
    )
    initial_status = "intact" if wave_signal and initial_balance_coverage >= 80 else "unknown"
    return {
        "version": 2,
        "cohort_id": "|".join(
            str(value or "")
            for value in (
                pool.get("token_address") or pool.get("pool_address"),
                alert.get("window_start"),
                alert.get("window_end"),
            )
        ),
        "pool_address": pool.get("pool_address"),
        "token_address": pool.get("token_address"),
        "symbol": pool.get("symbol"),
        "name": pool.get("name"),
        "dex": pool.get("dex"),
        "url": pool.get("url"),
        "pair_created_at": parse_timestamp(
            pool.get("pair_created_at")
            or pool.get("pair_created_at_iso")
        ),
        "signal_mcap_usd": float(
            alert.get("obs_mcap_usd")
            or pool.get("mcap_usd")
            or pool.get("fdv_usd")
            or 0
        ),
        "signal_price_usd": float(pool.get("price_usd") or 0),
        "signal_liquidity_usd": float(pool.get("liquidity_usd") or 0),
        "signal_family": alert.get("signal_family") or "classified_wallets",
        "source_tier": alert.get("action_tier"),
        "source_score": float(alert.get("score") or 0),
        "source_flow_sol": float(
            alert.get("suspicious_sol")
            or wave.get("net_buy_sol")
            or 0
        ),
        "source_wallets": int(
            alert.get("suspicious_wallets")
            or wave.get("effective_unique_buyers")
            or wave.get("unique_buyers")
            or len(cohort)
        ),
        "source_hard_wallets": int(alert.get("hard_wallets") or 0),
        "source_support_wallets": int(alert.get("support_wallets") or 0),
        "signal_at": signal_at,
        "signal_window_start": alert.get("window_start"),
        "signal_window_end": alert.get("window_end"),
        "captured_at": captured_at or signal_at,
        "last_signal_at": signal_at,
        "last_checked_at": signal_at if wave_signal else None,
        "updated_at": captured_at or signal_at,
        "status": initial_status,
        "status_changed_at": signal_at,
        "reason": (
            "initial retention is calculated from the stored signal cohort"
            if initial_status == "intact"
            else "original classified-wallet cohort requires a current balance recheck"
        ),
        "original_wallets": len(cohort),
        "source_signal_wallets": source_signal_wallets,
        "cohort_wallet_coverage_pct": (
            len(cohort) / source_signal_wallets * 100
            if source_signal_wallets
            else 0.0
        ),
        "holders_remaining": initial_totals["holders"] if wave_signal else 0,
        "holder_retention_pct": initial_holder_retention_pct if wave_signal else None,
        "holder_min_pct": holder_min_pct,
        "original_retained_tokens": original_tokens,
        "original_attributed_tokens": original_tokens,
        "source_attributed_tokens": source_attributed_tokens,
        "current_retained_tokens": initial_totals["retained_tokens"] if wave_signal else None,
        "token_retention_pct": initial_totals["retention_pct"] if wave_signal else None,
        "cohort_token_coverage_pct": (
            original_tokens / source_attributed_tokens * 100
            if source_attributed_tokens
            else 0.0
        ),
        "supply": supply,
        "original_retained_supply_pct": (
            original_tokens / supply * 100 if supply else None
        ),
        "current_retained_supply_pct": (
            initial_totals["retained_tokens"] / supply * 100
            if supply and wave_signal
            else None
        ),
        "balance_coverage_pct": initial_balance_coverage,
        "invalidation_streak": 0,
        "cohort": cohort,
    }


def capture_signal_thesis(
    pool_state,
    alerts,
    config,
    captured_at=None,
):
    existing = pool_state.get("signal_thesis")
    pending = pool_state.get("pending_signal_thesis")
    promoted = False
    if (
        isinstance(existing, dict)
        and existing.get("status") == "invalidated"
        and isinstance(pending, dict)
        and parse_timestamp(pending.get("signal_at"))
        > parse_timestamp(existing.get("signal_at"))
    ):
        pool_state["signal_thesis"] = pending
        pool_state.pop("pending_signal_thesis", None)
        existing = pending
        promoted = True

    candidates = []
    for alert in alerts or []:
        incoming = signal_thesis_from_alert(
            alert,
            config,
            captured_at=captured_at,
        )
        if incoming:
            candidates.append((alert_history_timestamp(alert), incoming))
    if not candidates:
        return existing, promoted
    incoming = (
        min(candidates, key=lambda item: item[0])[1]
        if not isinstance(existing, dict)
        else max(candidates, key=lambda item: item[0])[1]
    )
    replace = not isinstance(existing, dict)
    if isinstance(existing, dict):
        replace = bool(
            existing.get("status") == "invalidated"
            and parse_timestamp(incoming.get("signal_at"))
            > parse_timestamp(existing.get("signal_at"))
        )
    if replace:
        pool_state["signal_thesis"] = incoming
        pool_state.pop("pending_signal_thesis", None)
        if not isinstance(existing, dict):
            latest = max(candidates, key=lambda item: item[0])[1]
            if (
                parse_timestamp(latest.get("signal_at"))
                > parse_timestamp(incoming.get("signal_at"))
            ):
                pool_state["pending_signal_thesis"] = latest
        return incoming, True

    if (
        parse_timestamp(incoming.get("signal_at"))
        > parse_timestamp(existing.get("signal_at"))
    ):
        current_pending = pool_state.get("pending_signal_thesis")
        if (
            not isinstance(current_pending, dict)
            or parse_timestamp(incoming.get("signal_at"))
            > parse_timestamp(current_pending.get("signal_at"))
        ):
            pool_state["pending_signal_thesis"] = incoming

    existing["last_signal_at"] = incoming.get("last_signal_at")
    existing["updated_at"] = captured_at or incoming.get("last_signal_at")
    existing["source_tier"] = incoming.get("source_tier") or existing.get(
        "source_tier"
    )
    return existing, False


def recheck_signal_thesis(rpc, pool, pool_state, config, checked_at=None):
    thesis = pool_state.get("signal_thesis")
    if not isinstance(thesis, dict) or thesis.get("status") == "invalidated":
        return thesis
    cohort = [
        row
        for row in thesis.get("cohort") or []
        if isinstance(row, dict) and row.get("owner")
    ]
    if not cohort or not pool.token_address:
        return thesis

    checked_at = checked_at or utc_now().isoformat().replace("+00:00", "Z")
    checked = []
    errors = 0
    holder_min_pct = max(
        0.0,
        float(config.get("signal_thesis_wallet_holder_min_pct", 10)),
    )
    for row in cohort:
        attributed = max(0.0, float(row.get("attributed_tokens") or 0))
        try:
            balance = max(
                0.0,
                float(rpc.token_balance(row["owner"], pool.token_address) or 0),
            )
        except Exception:
            errors += 1
            continue
        retained = min(balance, attributed)
        is_holder = bool(
            attributed > 0 and retained / attributed * 100 >= holder_min_pct
        )
        row["current_balance"] = balance
        row["current_retained_tokens"] = retained
        row["retention_pct"] = (
            retained / attributed * 100 if attributed else 0.0
        )
        row["is_holder"] = is_holder
        row["checked_at"] = checked_at
        checked.append((row, attributed, retained, is_holder))

    balance_coverage_pct = len(checked) / len(cohort) * 100 if cohort else 0.0
    original_tokens = max(
        0.0,
        float(thesis.get("original_retained_tokens") or 0),
    )
    checked_original = sum(item[1] for item in checked)
    token_balance_coverage_pct = (
        checked_original / original_tokens * 100 if original_tokens else 0.0
    )
    thesis["balance_coverage_pct"] = balance_coverage_pct
    thesis["token_balance_coverage_pct"] = token_balance_coverage_pct
    thesis["balance_errors"] = errors
    thesis["holder_min_pct"] = holder_min_pct
    thesis["last_checked_at"] = checked_at
    thesis["updated_at"] = checked_at
    min_coverage = float(
        config.get("signal_thesis_min_balance_coverage_pct", 80)
    )
    min_token_coverage = float(
        config.get("signal_thesis_min_token_balance_coverage_pct", 80)
    )
    if (
        not checked
        or balance_coverage_pct < min_coverage
        or token_balance_coverage_pct < min_token_coverage
    ):
        previous_status = thesis.get("status")
        thesis["status"] = "unknown"
        thesis["invalidation_streak"] = 0
        thesis["reason"] = (
            f"wallet balance coverage is {balance_coverage_pct:.0f}% and "
            f"tracked-token coverage is {token_balance_coverage_pct:.0f}%; "
            "the original accumulation thesis cannot be invalidated"
        )
        if previous_status != thesis["status"]:
            thesis["status_changed_at"] = checked_at
        return thesis

    current_retained = sum(item[2] for item in checked)
    retention_pct = (
        current_retained / original_tokens * 100 if original_tokens else 0.0
    )
    holders_remaining = sum(1 for item in checked if item[3])
    holder_retention_pct = holders_remaining / len(cohort) * 100
    supply = max(0.0, float(thesis.get("supply") or 0))
    if not supply:
        try:
            supply = max(
                0.0,
                float(rpc.token_supply(pool.token_address) or 0),
            )
        except Exception:
            supply = 0.0
        if supply:
            thesis["supply"] = supply
            thesis["original_retained_supply_pct"] = (
                original_tokens / supply * 100
            )
    intact_min_retention = float(
        config.get("signal_thesis_intact_min_retention_pct", 60)
    )
    intact_min_holders = float(
        config.get("signal_thesis_intact_min_holder_pct", 50)
    )
    invalidated_max_retention = float(
        config.get("signal_thesis_invalidated_max_retention_pct", 20)
    )
    invalidated_max_holders = float(
        config.get("signal_thesis_invalidated_max_holder_pct", 25)
    )
    min_cohort_coverage = float(
        config.get("signal_thesis_min_cohort_token_coverage_pct", 70)
    )
    min_cohort_wallet_coverage = float(
        config.get("signal_thesis_min_cohort_wallet_coverage_pct", 70)
    )
    cohort_coverage = float(thesis.get("cohort_token_coverage_pct") or 0)
    cohort_wallet_coverage_value = thesis.get("cohort_wallet_coverage_pct")
    cohort_wallet_coverage = (
        100.0
        if cohort_wallet_coverage_value is None
        else float(cohort_wallet_coverage_value)
    )
    confirmations_required = max(
        1,
        int(
            config.get(
                "signal_thesis_invalidation_confirmations_required",
                2,
            )
        ),
    )
    previous_status = thesis.get("status")
    can_invalidate = (
        cohort_coverage >= min_cohort_coverage
        and cohort_wallet_coverage >= min_cohort_wallet_coverage
    )
    invalidation_candidate = bool(
        can_invalidate
        and retention_pct <= invalidated_max_retention
        and holder_retention_pct <= invalidated_max_holders
    )
    invalidation_streak = (
        int(thesis.get("invalidation_streak") or 0) + 1
        if invalidation_candidate
        else 0
    )
    if (
        invalidation_candidate
        and invalidation_streak >= confirmations_required
    ):
        status = "invalidated"
        reason = (
            f"the original cohort retains only {retention_pct:.0f}% of its "
            f"signal-attributed tokens across {holder_retention_pct:.0f}% of wallets "
            f"on {invalidation_streak} consecutive complete checks"
        )
    elif (
        retention_pct >= intact_min_retention
        and holder_retention_pct >= intact_min_holders
    ):
        status = "intact"
        reason = (
            f"the original cohort still retains {retention_pct:.0f}% of its "
            f"signal-attributed tokens across {holder_retention_pct:.0f}% of wallets"
        )
    elif invalidation_candidate:
        status = "weakening"
        reason = (
            f"possible thesis invalidation is awaiting confirmation "
            f"({invalidation_streak}/{confirmations_required} complete checks); "
            f"{retention_pct:.0f}% of signal tokens remain across "
            f"{holder_retention_pct:.0f}% of wallets"
        )
    elif can_invalidate:
        status = "weakening"
        reason = (
            f"the original cohort retains {retention_pct:.0f}% of its "
            f"signal-attributed tokens across {holder_retention_pct:.0f}% of wallets"
        )
    else:
        status = "unknown"
        reason = (
            f"the stored cohort covers {cohort_coverage:.0f}% of the original "
            f"retained tokens and {cohort_wallet_coverage:.0f}% of signal wallets; "
            "the thesis cannot be invalidated safely"
        )

    thesis.update(
        {
            "status": status,
            "reason": reason,
            "holders_remaining": holders_remaining,
            "holder_retention_pct": holder_retention_pct,
            "current_retained_tokens": current_retained,
            "token_retention_pct": retention_pct,
            "invalidation_candidate": invalidation_candidate,
            "invalidation_streak": invalidation_streak,
            "current_retained_supply_pct": (
                current_retained / supply * 100 if supply else None
            ),
        }
    )
    if previous_status != status:
        thesis["status_changed_at"] = checked_at
    if status == "invalidated":
        thesis["invalidated_at"] = checked_at
    return thesis


def public_signal_thesis(thesis):
    if not isinstance(thesis, dict):
        return None
    public_fields = {
        "version",
        "cohort_id",
        "pool_address",
        "token_address",
        "symbol",
        "name",
        "dex",
        "url",
        "pair_created_at",
        "signal_mcap_usd",
        "signal_price_usd",
        "signal_liquidity_usd",
        "signal_family",
        "source_tier",
        "source_score",
        "source_flow_sol",
        "source_wallets",
        "source_hard_wallets",
        "source_support_wallets",
        "signal_at",
        "signal_window_start",
        "signal_window_end",
        "captured_at",
        "last_signal_at",
        "last_checked_at",
        "updated_at",
        "next_check_at",
        "status",
        "status_changed_at",
        "invalidated_at",
        "reason",
        "original_wallets",
        "source_signal_wallets",
        "cohort_wallet_coverage_pct",
        "holders_remaining",
        "holder_retention_pct",
        "holder_min_pct",
        "original_retained_tokens",
        "current_retained_tokens",
        "token_retention_pct",
        "cohort_token_coverage_pct",
        "token_balance_coverage_pct",
        "original_retained_supply_pct",
        "current_retained_supply_pct",
        "balance_coverage_pct",
        "balance_errors",
        "invalidation_candidate",
        "invalidation_streak",
    }
    public = {
        key: value
        for key, value in thesis.items()
        if key in public_fields
    }
    cohort_fields = {
        "owner",
        "wallet_class",
        "attributed_tokens",
        "buy_sol",
        "first_buy_time",
        "current_balance",
        "current_retained_tokens",
        "retention_pct",
        "is_holder",
        "checked_at",
    }
    holder_min_pct = max(
        0.0,
        float(thesis.get("holder_min_pct") or 10),
    )
    public["cohort_wallets"] = []
    for row in thesis.get("cohort") or []:
        if not isinstance(row, dict) or not row.get("owner"):
            continue
        public_row = {
            key: value
            for key, value in row.items()
            if key in cohort_fields and value is not None
        }
        if "is_holder" not in public_row and row.get("retention_pct") is not None:
            public_row["is_holder"] = (
                float(row.get("retention_pct") or 0) >= holder_min_pct
            )
        public["cohort_wallets"].append(public_row)
    return public


def signal_thesis_recheck_interval_minutes(thesis, config):
    status = (thesis or {}).get("status")
    if status in ("weakening", "unknown"):
        return max(
            5.0,
            float(
                config.get(
                    "signal_thesis_priority_recheck_minutes",
                    config.get("signal_thesis_recheck_minutes", 60),
                )
            ),
        )
    return max(
        5.0,
        float(config.get("signal_thesis_recheck_minutes", 60)),
    )


def refresh_signal_thesis(
    rpc,
    pool,
    pool_state,
    alerts,
    config,
    checked_at=None,
):
    if not config.get("signal_thesis_tracking_enabled", True):
        return None
    checked_at = checked_at or utc_now().isoformat().replace("+00:00", "Z")
    thesis, captured = capture_signal_thesis(
        pool_state,
        alerts,
        config,
        captured_at=checked_at,
    )
    due_at = parse_timestamp(pool_state.get("signal_recheck_due_at"))
    should_recheck = bool(
        isinstance(thesis, dict)
        and (
            captured
            or bool(alerts)
            or not thesis.get("last_checked_at")
            or (due_at and due_at <= int(time.time()))
        )
    )
    if should_recheck:
        thesis = recheck_signal_thesis(
            rpc,
            pool,
            pool_state,
            config,
            checked_at=checked_at,
        )
        if thesis.get("status") == "invalidated":
            thesis, replacement_captured = capture_signal_thesis(
                pool_state,
                alerts,
                config,
                captured_at=checked_at,
            )
            if replacement_captured:
                thesis = recheck_signal_thesis(
                    rpc,
                    pool,
                    pool_state,
                    config,
                    checked_at=checked_at,
                )
        schedule_signal_recheck(pool_state, alerts, config)
    elif not isinstance(thesis, dict):
        schedule_signal_recheck(pool_state, alerts, config)
    return public_signal_thesis(thesis)


def bootstrap_signal_theses(state, alerts, config, now=None):
    if not config.get("signal_thesis_tracking_enabled", True):
        return {"created": 0, "tracked": 0}
    now = int(now or time.time())
    grouped = defaultdict(list)
    deleted = load_deleted_tokens()
    for alert in alerts or []:
        if (
            not isinstance(alert, dict)
            or (
                alert.get("lane")
                and alert.get("lane") != "reactivation"
            )
            or alert_is_deleted(alert, deleted)
        ):
            continue
        pool = alert.get("pool") or {}
        pool_address = pool.get("pool_address")
        if pool_address:
            grouped[pool_address].append(alert)

    created = 0
    pools_state = state.setdefault("pools", {})
    for pool_address, pool_alerts in grouped.items():
        pool_state = pools_state.setdefault(pool_address, {})
        thesis, was_created = capture_signal_thesis(
            pool_state,
            pool_alerts,
            config,
            captured_at=iso(now),
        )
        if not isinstance(thesis, dict):
            continue
        created += int(was_created)
        if thesis.get("status") == "invalidated":
            pool_state.pop("signal_recheck_due_at", None)
            thesis.pop("next_check_at", None)
            continue
        if pool_state.get("signal_recheck_due_at"):
            continue
        interval = signal_thesis_recheck_interval_minutes(thesis, config)
        last_checked = parse_timestamp(
            thesis.get("last_checked_at") or thesis.get("signal_at")
        )
        due_at = min(
            now,
            last_checked + int(interval * 60) if last_checked else now,
        )
        pool_state["signal_recheck_due_at"] = iso(due_at)
        thesis["next_check_at"] = iso(due_at)
    return {
        "created": created,
        "tracked": sum(
            1
            for value in pools_state.values()
            if isinstance(value, dict)
            and isinstance(value.get("signal_thesis"), dict)
        ),
    }


def signal_theses_for_report(state, deleted=None):
    deleted = deleted or {"tokens": set(), "pools": set()}
    theses = []
    for pool_state in (state.get("pools") or {}).values():
        private_thesis = (
            pool_state.get("signal_thesis")
            if isinstance(pool_state, dict)
            else None
        )
        if not isinstance(private_thesis, dict):
            continue
        if (
            private_thesis.get("token_address") in deleted.get("tokens", set())
            or private_thesis.get("pool_address") in deleted.get("pools", set())
        ):
            continue
        thesis = public_signal_thesis(private_thesis)
        if thesis:
            theses.append(thesis)
    return sorted(
        theses,
        key=lambda item: parse_timestamp(
            item.get("updated_at")
            or item.get("last_checked_at")
            or item.get("signal_at")
        ),
        reverse=True,
    )


def buyer_concentration(buyers):
    buy_sol = sorted((max(0.0, float(row.get("buy_sol") or 0)) for row in buyers), reverse=True)
    total = sum(buy_sol)
    if total <= 0:
        return 0.0, 0.0
    return buy_sol[0] / total, sum(buy_sol[:3]) / total


def scaled_score(value, threshold, points, full_ratio=2.0):
    value = max(0.0, float(value or 0))
    threshold = max(0.0, float(threshold or 0))
    if threshold <= 0:
        return float(points) if value > 0 else 0.0
    return float(points) * min(1.0, value / (threshold * max(1.0, float(full_ratio))))


def wave_quality_score(alert, config):
    wave = alert.get("wave") or {}
    family = alert.get("signal_family")
    prefix = "sticky_accumulation" if family == "sticky_accumulation" else "reactivation_wave"
    score = 15.0
    score += scaled_score(
        wave.get("net_buy_sol"),
        config.get(f"{prefix}_min_net_buy_sol", 10),
        15,
        full_ratio=4,
    )
    score += scaled_score(
        wave.get("effective_unique_buyers") or wave.get("unique_buyers"),
        config.get(f"{prefix}_min_unique_buyers", 5),
        10,
        full_ratio=5,
    )
    score += scaled_score(
        wave.get("sticky_supply_pct"),
        config.get(f"{prefix}_actionable_sticky_supply_pct", 5),
        15,
        full_ratio=2,
    )
    score += scaled_score(
        wave.get("sticky_bought_pct"),
        100,
        15,
        full_ratio=1,
    )
    score += scaled_score(
        wave.get("net_token_retention_pct"),
        100,
        10,
        full_ratio=1,
    )
    min_sticky_wallets = config.get(
        f"{prefix}_min_sticky_wallets",
        max(2, int(float(config.get(f"{prefix}_min_unique_buyers", 5)) / 2)),
    )
    score += scaled_score(wave.get("sticky_wallets"), min_sticky_wallets, 10, full_ratio=4)
    score += min(5.0, max(0.0, float(wave.get("balance_coverage_pct") or 0)) / 20)
    min_hold = max(
        0.0,
        float(
            wave.get("min_hold_minutes")
            or config.get(f"{prefix}_min_hold_minutes")
            or 0
        ),
    )
    hold_age = max(0.0, float(wave.get("hold_age_minutes") or 0))
    score += 10.0 if min_hold <= 0 else min(10.0, hold_age / min_hold * 10)

    top_buyer = float(wave.get("top_buyer_share") or 0)
    top3_buyers = float(wave.get("top3_buyer_share") or 0)
    if top_buyer > float(config.get(f"{prefix}_max_top_buyer_share", 1.0)):
        score -= 10
    if top3_buyers > float(config.get(f"{prefix}_max_top3_buyer_share", 1.0)):
        score -= 10
    linked_cluster_share = float(wave.get("max_linked_cluster_share") or 0)
    if linked_cluster_share > float(
        config.get("reactivation_wallet_graph_actionable_max_cluster_share", 0.5)
    ):
        score -= 10
    if linked_cluster_share > float(
        config.get("reactivation_wallet_graph_noise_cluster_share", 0.75)
    ):
        score -= 15
    if float(wave.get("balance_coverage_pct") or 0) < float(
        config.get("actionable_min_balance_coverage_pct", 80)
    ):
        score -= 10
    return max(0, min(95, int(round(score))))


def owner_activity_since(swaps, start_time, owners):
    owners = {owner for owner in owners if owner}
    activity = {
        owner: {
            "buy_sol": 0.0,
            "sell_sol": 0.0,
            "token_bought": 0.0,
            "token_sold": 0.0,
            "last_buy_time": 0,
            "last_sell_time": 0,
        }
        for owner in owners
    }
    for swap in swaps or []:
        block_time = int(swap.get("block_time") or 0)
        if block_time < int(start_time or 0):
            continue
        kind = swap.get("kind")
        owner = (
            wave_buy_owner(swap)
            if kind == "buy"
            else wave_sell_owner(swap, owners)
            if kind == "sell"
            else ""
        )
        if owner not in activity:
            continue
        row = activity[owner]
        if kind == "buy":
            row["buy_sol"] += float(swap.get("sol_amount") or 0)
            row["token_bought"] += float(swap.get("token_amount") or swap.get("token_recipient_amount") or 0)
            row["last_buy_time"] = max(row["last_buy_time"], block_time)
        elif kind == "sell":
            row["sell_sol"] += float(swap.get("sol_amount") or 0)
            row["token_sold"] += float(swap.get("token_amount") or swap.get("token_sender_amount") or 0)
            row["last_sell_time"] = max(row["last_sell_time"], block_time)
    return activity


def reactivation_wave_window_metrics(window_swaps, config, relaxed=False):
    min_trade_sol = float(config.get("reactivation_wave_min_trade_sol", 0.25))
    observed_buy_swaps = [
        swap
        for swap in window_swaps
        if (
            swap.get("kind") == "buy"
            and float(swap.get("sol_amount") or 0) >= min_trade_sol
        )
    ]
    buy_swaps = [swap for swap in observed_buy_swaps if wave_buy_owner(swap)]
    unresolved_buy_swaps = [
        swap for swap in observed_buy_swaps if not wave_buy_owner(swap)
    ]
    sell_swaps = [
        swap
        for swap in window_swaps
        if swap.get("kind") == "sell" and float(swap.get("sol_amount") or 0) >= min_trade_sol
    ]
    buy_sol = sum(float(swap.get("sol_amount") or 0) for swap in buy_swaps)
    unresolved_buy_sol = sum(
        float(swap.get("sol_amount") or 0) for swap in unresolved_buy_swaps
    )
    sell_sol = sum(float(swap.get("sol_amount") or 0) for swap in sell_swaps)
    net_buy_sol = buy_sol - sell_sol
    if buy_sol <= 0 or net_buy_sol <= 0:
        return None

    buyer_rows = {}
    for swap in buy_swaps:
        owner = wave_buy_owner(swap)
        if not owner:
            continue
        row = buyer_rows.setdefault(
            owner,
            {
                "owner": owner,
                "buy_sol": 0.0,
                "sell_sol": 0.0,
                "token_bought": 0.0,
                "token_sold": 0.0,
                "buy_count": 0,
                "sell_count": 0,
                "first_buy_time": swap.get("time") or iso(swap.get("block_time")),
                "top_buy": swap,
            },
        )
        sol_amount = float(swap.get("sol_amount") or 0)
        token_amount_value = float(swap.get("token_amount") or swap.get("token_recipient_amount") or 0)
        row["buy_sol"] += sol_amount
        row["token_bought"] += token_amount_value
        row["buy_count"] += 1
        if sol_amount > float(row["top_buy"].get("sol_amount") or 0):
            row["top_buy"] = swap
        if parse_timestamp(swap.get("time")) and parse_timestamp(swap.get("time")) < parse_timestamp(row["first_buy_time"]):
            row["first_buy_time"] = swap.get("time")

    for swap in sell_swaps:
        owner = wave_sell_owner(swap, buyer_rows)
        if not owner or owner not in buyer_rows:
            continue
        row = buyer_rows[owner]
        row["sell_sol"] += float(swap.get("sol_amount") or 0)
        row["token_sold"] += float(swap.get("token_amount") or swap.get("token_sender_amount") or 0)
        row["sell_count"] += 1

    buyers = [row for row in buyer_rows.values() if row["buy_sol"] > 0]
    large_buy_min_sol = float(config.get("reactivation_wave_large_buy_min_sol", 1.0))
    unique_buyers = len(buyers)
    large_buyers = sum(1 for row in buyers if row["buy_sol"] >= large_buy_min_sol)
    net_buy_ratio = net_buy_sol / buy_sol if buy_sol else 0.0
    top_buyer_share, top3_buyer_share = buyer_concentration(buyers)

    scale = 0.6 if relaxed else 1.0

    def scaled_float(key, default):
        return float(config.get(key, default)) * scale

    def scaled_int(key, default):
        return max(1, int(float(config.get(key, default)) * scale))

    if buy_sol < scaled_float("reactivation_wave_min_buy_sol", 75):
        return None
    if net_buy_sol < scaled_float("reactivation_wave_min_net_buy_sol", 25):
        return None
    if net_buy_ratio < float(config.get("reactivation_wave_min_net_buy_ratio", 0.20)):
        return None
    if unique_buyers < scaled_int("reactivation_wave_min_unique_buyers", 20):
        return None
    if large_buyers < scaled_int("reactivation_wave_min_large_buyers", 8):
        return None
    return {
        "buy_sol": buy_sol,
        "sell_sol": sell_sol,
        "net_buy_sol": net_buy_sol,
        "net_buy_ratio": net_buy_ratio,
        "unique_buyers": unique_buyers,
        "large_buyers": large_buyers,
        "top_buyer_share": top_buyer_share,
        "top3_buyer_share": top3_buyer_share,
        "last_buy_time": max((int(swap.get("block_time") or 0) for swap in buy_swaps), default=0),
        "buyers": sorted(buyers, key=lambda row: row["buy_sol"], reverse=True),
        "buy_count": len(buy_swaps),
        "sell_count": len(sell_swaps),
        "unresolved_buy_count": len(unresolved_buy_swaps),
        "unresolved_buy_sol": unresolved_buy_sol,
        "owner_resolution_coverage_pct": (
            buy_sol / (buy_sol + unresolved_buy_sol) * 100
            if buy_sol + unresolved_buy_sol
            else 100.0
        ),
    }


def reactivation_wave_window_candidates(swaps, config, relaxed=False):
    if not reactivation_wave_enabled(config) or not swaps:
        return []
    window_seconds = int(config["alert_window_minutes"]) * 60
    ordered = sorted(
        [swap for swap in swaps if swap.get("kind") in ("buy", "sell") and swap.get("block_time")],
        key=lambda swap: swap.get("block_time") or 0,
    )
    candidates = []
    for index, swap in enumerate(ordered):
        if swap.get("kind") != "buy":
            continue
        start = int(swap.get("block_time") or 0)
        end = start + window_seconds
        window = [item for item in ordered[index:] if int(item.get("block_time") or 0) <= end]
        metrics = reactivation_wave_window_metrics(window, config, relaxed=relaxed)
        if not metrics:
            continue
        candidates.append({"start": start, "end": end, "window": window, "metrics": metrics})
    candidates.sort(
        key=lambda item: (
            item["metrics"]["net_buy_sol"],
            item["metrics"]["buy_sol"],
            item["metrics"]["unique_buyers"],
        ),
        reverse=True,
    )
    return candidates


def reactivation_wave_precheck(swaps, config):
    return bool(reactivation_wave_window_candidates(swaps, config, relaxed=True))


def analyze_wave_wallet_graph(rpc, buyers, config, state):
    if not config.get("reactivation_wallet_graph_enabled", True) or state is None:
        return {
            "checked_wallets": 0,
            "errors": 0,
            "classes": {},
            "common_funders": [],
            "common_executors": [],
            "effective_wallets": len(buyers or []),
            "max_cluster_share": 0.0,
            "wallets": {},
        }
    limit = max(0, int(config.get("reactivation_wallet_graph_limit", 12)))
    classifications = {}
    errors = 0
    funding_groups = defaultdict(list)
    executor_groups = defaultdict(list)
    buyer_sol = {
        row.get("owner"): float(row.get("buy_sol") or 0)
        for row in buyers or []
        if row.get("owner")
    }
    for row in (buyers or [])[:limit]:
        owner = row.get("owner")
        top_buy = row.get("top_buy") or {}
        signature = top_buy.get("signature")
        if not owner or not signature:
            continue
        try:
            result = classify_wallet(
                rpc,
                owner,
                signature,
                int(top_buy.get("block_time") or 0),
                config,
                state,
            )
        except Exception as exc:
            errors += 1
            classifications[owner] = {"error": str(exc)[:200]}
            continue
        classifications[owner] = result
        funder = result.get("funding_source")
        if funder:
            funding_groups[funder].append(owner)
        if top_buy.get("routed") and top_buy.get("signer"):
            executor_groups[top_buy["signer"]].append(owner)

    common_funders = [
        {
            "source": funder,
            "wallets": len(set(wallets)),
            "members": sorted(set(wallets)),
        }
        for funder, wallets in funding_groups.items()
        if len(set(wallets)) >= 2
    ]
    common_executors = [
        {
            "executor": executor,
            "wallets": len(set(wallets)),
            "members": sorted(set(wallets)),
        }
        for executor, wallets in executor_groups.items()
        if len(set(wallets)) >= 2
    ]
    parents = {owner: owner for owner in buyer_sol}

    def find(owner):
        while parents[owner] != owner:
            parents[owner] = parents[parents[owner]]
            owner = parents[owner]
        return owner

    def union(left, right):
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for group in [*common_funders, *common_executors]:
        members = [owner for owner in group["members"] if owner in parents]
        for owner in members[1:]:
            union(members[0], owner)

    clusters = defaultdict(list)
    for owner in buyer_sol:
        clusters[find(owner)].append(owner)
    cluster_rows = sorted(
        (
            {
                "wallets": len(members),
                "members": sorted(members),
                "buy_sol": sum(buyer_sol.get(owner, 0.0) for owner in members),
            }
            for members in clusters.values()
        ),
        key=lambda row: row["buy_sol"],
        reverse=True,
    )
    total_sol = sum(buyer_sol.values())
    for row in cluster_rows:
        row["buy_share"] = row["buy_sol"] / total_sol if total_sol else 0.0
    max_cluster_share = max(
        (row["buy_share"] for row in cluster_rows if row["wallets"] > 1),
        default=0.0,
    )
    effective_wallets = len(cluster_rows)
    classes = Counter(
        item.get("wallet_class")
        for item in classifications.values()
        if item.get("wallet_class")
    )
    return {
        "checked_wallets": len(classifications),
        "errors": errors,
        "classes": dict(classes),
        "common_funders": common_funders,
        "common_executors": common_executors,
        "effective_wallets": effective_wallets,
        "max_cluster_share": max_cluster_share,
        "clusters": [row for row in cluster_rows if row["wallets"] > 1],
        "wallets": classifications,
    }


def build_reactivation_wave_alerts(pool, swaps, config, rpc, state=None):
    if not reactivation_wave_enabled(config) or not pool.token_address:
        return []
    candidates = reactivation_wave_window_candidates(swaps, config, relaxed=False)
    if not candidates:
        return []
    max_candidates = int(config.get("reactivation_wave_candidate_windows", 3))
    balance_limit = int(config.get("reactivation_wave_balance_wallet_limit", 50))
    min_sticky_wallets = int(config.get("reactivation_wave_min_sticky_wallets", 1))
    min_sticky_supply_pct = float(config.get("reactivation_wave_min_sticky_supply_pct", 3.0))
    min_sticky_bought_pct = float(config.get("reactivation_wave_min_sticky_bought_pct", 40.0))
    min_net_retention_pct = float(config.get("reactivation_wave_min_net_token_retention_pct", 60.0))
    min_wallet_retention_pct = float(config.get("reactivation_wave_min_wallet_retention_pct", 15.0))
    min_hold_minutes = float(config.get("reactivation_wave_min_hold_minutes", 90))
    min_balance_coverage_pct = float(config.get("actionable_min_balance_coverage_pct", 80))
    try:
        supply = rpc.token_supply(pool.token_address)
    except Exception as exc:
        raise WaveDataUnavailable(
            f"reactivation supply unavailable for {pool.token_address}: {exc}"
        ) from exc
    if not supply:
        raise WaveDataUnavailable(
            f"reactivation supply unavailable for {pool.token_address}"
        )

    alerts = []
    coverage_available = False
    created_at = utc_now().isoformat().replace("+00:00", "Z")
    for candidate in candidates[:max_candidates]:
        metrics = candidate["metrics"]
        buyers = metrics["buyers"][:balance_limit]
        wallet_graph = analyze_wave_wallet_graph(
            rpc,
            buyers,
            config,
            state,
        )
        owner_activity = owner_activity_since(swaps, candidate["start"], [row.get("owner") for row in buyers])
        checked = []
        sticky_tokens = 0.0
        sticky_wallets = 0
        checked_bought_tokens = 0.0
        checked_net_tokens = 0.0
        balance_errors = 0
        for row in buyers:
            owner = row.get("owner")
            if not owner:
                continue
            try:
                balance = rpc.token_balance(owner, pool.token_address)
            except Exception:
                balance = 0.0
                balance_errors += 1
            bought_tokens = float(row.get("token_bought") or 0)
            activity = owner_activity.get(owner) or {}
            sold_tokens = max(
                float(row.get("token_sold") or 0),
                float(activity.get("token_sold") or 0),
            )
            retention = attributed_wave_retention(
                balance,
                bought_tokens,
                sold_tokens,
                min_retention_pct=min_wallet_retention_pct,
            )
            net_tokens = retention["net_tokens"]
            retained_tokens = retention["retained_tokens"]
            checked_bought_tokens += bought_tokens
            checked_net_tokens += net_tokens
            if retention["qualified"]:
                sticky_tokens += retained_tokens
                sticky_wallets += 1
            checked.append(
                {
                    "owner": owner,
                    "buy_sol": row["buy_sol"],
                    "sell_sol": row["sell_sol"],
                    "net_sol": row["buy_sol"] - float(activity.get("sell_sol") or row["sell_sol"]),
                    "token_bought": bought_tokens,
                    "token_sold": sold_tokens,
                    "post_window_sell_sol": max(0.0, float(activity.get("sell_sol") or 0) - float(row["sell_sol"])),
                    "retained_from_wave": retained_tokens,
                    "wave_retention_pct": retention["retention_pct"],
                    "wave_net_coverage_pct": retention["net_coverage_pct"],
                    "retained_supply_pct": (retained_tokens / supply * 100) if supply else 0.0,
                    "current_balance": balance,
                    "current_supply_pct": (balance / supply * 100) if supply else 0.0,
                    "buy_count": row["buy_count"],
                    "sell_count": row["sell_count"],
                    "first_buy_time": row["first_buy_time"],
                    "wallet_class": (
                        wallet_graph.get("wallets", {})
                        .get(owner, {})
                        .get("wallet_class")
                    ),
                    "funding_source": (
                        wallet_graph.get("wallets", {})
                        .get(owner, {})
                        .get("funding_source")
                    ),
                }
            )

        balance_coverage_pct = (
            (len(checked) - balance_errors) / len(checked) * 100
            if checked
            else 0.0
        )
        if balance_coverage_pct < min_balance_coverage_pct:
            continue
        coverage_available = True
        sticky_supply_pct = sticky_tokens / supply * 100 if supply else 0.0
        sticky_bought_pct = sticky_tokens / checked_bought_tokens * 100 if checked_bought_tokens else 0.0
        net_retention_pct = sticky_tokens / checked_net_tokens * 100 if checked_net_tokens else 0.0
        if sticky_wallets < min_sticky_wallets:
            continue
        if sticky_supply_pct < min_sticky_supply_pct:
            continue
        if sticky_bought_pct < min_sticky_bought_pct or net_retention_pct < min_net_retention_pct:
            continue

        events = []
        for row in metrics["buyers"][: int(config.get("alert_event_export_limit", 80))]:
            event = dict(row["top_buy"])
            event["time"] = event.get("time") or iso(event.get("block_time"))
            event["wallet_class"] = "wave_buyer"
            event["token_recipient"] = row["owner"]
            event["wave_buy_sol"] = row["buy_sol"]
            event["wave_sell_sol"] = row["sell_sol"]
            event["wave_buy_count"] = row["buy_count"]
            event["wave_sell_count"] = row["sell_count"]
            events.append(event)

        alert = {
            "created_at": created_at,
            "score": 0,
            "lane": "reactivation",
            "reactivation_stage": config.get("reactivation_stage") or "mature",
            "reactivation_baseline": getattr(
                pool,
                "reactivation_baseline",
                {"status": "warming", "reactivation_confirmed": False},
            ),
            "signal_family": "reactivation_wave",
            "pool": pool.as_dict(),
            "obs_mcap_usd": pool.mcap_usd,
            "obs_price_usd": pool.price_usd,
            "obs_liquidity_usd": pool.liquidity_usd,
            "obs_mcap_at": created_at,
            "window_start": iso(candidate["start"]),
            "window_end": iso(candidate["end"]),
            "suspicious_wallets": sticky_wallets,
            "suspicious_sol": metrics["net_buy_sol"],
            "hard_wallets": 0,
            "support_wallets": 0,
            "hard_sol": 0.0,
            "support_sol": 0.0,
            "classes": {"market_wave": sticky_wallets},
            "hard_classes": {},
            "support_classes": {},
            "common_funders": wallet_graph.get("common_funders") or [],
            "common_recipients": [],
            "common_executors": wallet_graph.get("common_executors") or [],
            "wallet_graph": wallet_graph,
            "routed_buys": sum(1 for item in events if item.get("routed")),
            "wave": {
                "buy_sol": metrics["buy_sol"],
                "sell_sol": metrics["sell_sol"],
                "net_buy_sol": metrics["net_buy_sol"],
                "net_buy_ratio": metrics["net_buy_ratio"],
                "buy_count": metrics["buy_count"],
                "sell_count": metrics["sell_count"],
                "unresolved_buy_count": metrics["unresolved_buy_count"],
                "unresolved_buy_sol": metrics["unresolved_buy_sol"],
                "owner_resolution_coverage_pct": metrics[
                    "owner_resolution_coverage_pct"
                ],
                "unique_buyers": metrics["unique_buyers"],
                "effective_unique_buyers": wallet_graph.get(
                    "effective_wallets",
                    metrics["unique_buyers"],
                ),
                "max_linked_cluster_share": wallet_graph.get(
                    "max_cluster_share",
                    0.0,
                ),
                "large_buyers": metrics["large_buyers"],
                "top_buyer_share": metrics["top_buyer_share"],
                "top3_buyer_share": metrics["top3_buyer_share"],
                "checked_wallets": len(checked),
                "checked_bought_tokens": checked_bought_tokens,
                "checked_net_tokens": checked_net_tokens,
                "balance_errors": balance_errors,
                "balance_coverage_pct": balance_coverage_pct,
                "hold_age_minutes": max(
                    0.0,
                    (time.time() - int(metrics.get("last_buy_time") or candidate["start"])) / 60,
                ),
                "min_hold_minutes": min_hold_minutes,
                "sticky_wallets": sticky_wallets,
                "sticky_tokens": sticky_tokens,
                "sticky_supply_pct": sticky_supply_pct,
                "sticky_bought_pct": sticky_bought_pct,
                "net_token_retention_pct": net_retention_pct,
                "supply": supply,
                "top_buyers": checked[
                    : min(
                        int(config.get("signal_thesis_wallet_limit", 40)),
                        len(checked),
                    )
                ],
            },
            "events": events,
        }
        alert["score"] = wave_quality_score(alert, config)
        tier, reasons, penalties, quality_metrics = classify_alert_tier(
            pool,
            alert,
            {
                "hard_wallets": 0,
                "support_wallets": 0,
                "hard_sol": 0.0,
                "support_sol": 0.0,
                "hard_classes": {},
                "support_classes": {},
                "support_only": False,
            },
            config,
        )
        alert.update(
            {
                "action_tier": tier,
                "quality_reasons": reasons,
                "quality_penalties": penalties,
                "quality_metrics": quality_metrics,
            }
        )
        alerts.append(alert)
    if not coverage_available:
        raise WaveDataUnavailable(
            f"reactivation balance coverage below {min_balance_coverage_pct:.0f}% "
            f"for {pool.token_address}"
        )
    return dedupe_pool_alerts(alerts)


def sticky_accumulation_enabled(config):
    return bool(config.get("sticky_accumulation_enabled", False))


def merge_sticky_accumulation_swaps(pool_state, swaps, config):
    if not sticky_accumulation_enabled(config):
        return []
    buffer_seconds = int(config.get("sticky_accumulation_buffer_minutes", config["alert_window_minutes"])) * 60
    max_swaps = int(config.get("sticky_accumulation_buffer_max_swaps", 800))
    recent_swaps = [compact_wave_swap(swap) for swap in swaps if swap.get("kind") in ("buy", "sell")]
    existing_swaps = [
        item
        for item in pool_state.get("sticky_accumulation_swaps", [])
        if isinstance(item, dict) and item.get("kind") in ("buy", "sell")
    ]
    newest = max(
        [int(time.time())]
        + [int(item.get("block_time") or 0) for item in [*existing_swaps, *recent_swaps] if item.get("block_time")]
    )
    cutoff = newest - buffer_seconds if buffer_seconds > 0 else 0
    by_signature = {}
    fallback_index = 0
    for item in [*existing_swaps, *recent_swaps]:
        block_time = int(item.get("block_time") or 0)
        if cutoff and block_time and block_time < cutoff:
            continue
        signature = item.get("signature") or f"fallback:{fallback_index}"
        fallback_index += 1
        by_signature[signature] = item
    merged = sorted(by_signature.values(), key=lambda item: (item.get("block_time") or 0, item.get("signature") or ""))
    buffer_truncated = bool(max_swaps > 0 and len(merged) > max_swaps)
    if buffer_truncated:
        merged = merged[-max_swaps:]
    pool_state["sticky_accumulation_swaps"] = merged
    pool_state["sticky_accumulation_buffer_truncated"] = buffer_truncated
    return merged


def sticky_accumulation_window_metrics(window_swaps, config, relaxed=False):
    min_trade_sol = float(config.get("sticky_accumulation_min_trade_sol", 0.25))
    buy_swaps = [
        swap
        for swap in window_swaps
        if (
            swap.get("kind") == "buy"
            and float(swap.get("sol_amount") or 0) >= min_trade_sol
            and wave_buy_owner(swap)
        )
    ]
    sell_swaps = [
        swap
        for swap in window_swaps
        if swap.get("kind") == "sell" and float(swap.get("sol_amount") or 0) >= min_trade_sol
    ]
    buy_sol = sum(float(swap.get("sol_amount") or 0) for swap in buy_swaps)
    sell_sol = sum(float(swap.get("sol_amount") or 0) for swap in sell_swaps)
    net_buy_sol = buy_sol - sell_sol
    if buy_sol <= 0 or net_buy_sol <= 0:
        return None

    buyer_rows = {}
    for swap in buy_swaps:
        owner = wave_buy_owner(swap)
        if not owner:
            continue
        row = buyer_rows.setdefault(
            owner,
            {
                "owner": owner,
                "buy_sol": 0.0,
                "sell_sol": 0.0,
                "token_bought": 0.0,
                "token_sold": 0.0,
                "buy_count": 0,
                "sell_count": 0,
                "first_buy_time": swap.get("time") or iso(swap.get("block_time")),
                "top_buy": swap,
            },
        )
        sol_amount = float(swap.get("sol_amount") or 0)
        token_amount_value = float(swap.get("token_amount") or swap.get("token_recipient_amount") or 0)
        row["buy_sol"] += sol_amount
        row["token_bought"] += token_amount_value
        row["buy_count"] += 1
        if sol_amount > float(row["top_buy"].get("sol_amount") or 0):
            row["top_buy"] = swap
        if parse_timestamp(swap.get("time")) and parse_timestamp(swap.get("time")) < parse_timestamp(row["first_buy_time"]):
            row["first_buy_time"] = swap.get("time")

    for swap in sell_swaps:
        owner = wave_sell_owner(swap, buyer_rows)
        if not owner or owner not in buyer_rows:
            continue
        row = buyer_rows[owner]
        row["sell_sol"] += float(swap.get("sol_amount") or 0)
        row["token_sold"] += float(swap.get("token_amount") or swap.get("token_sender_amount") or 0)
        row["sell_count"] += 1

    buyers = [row for row in buyer_rows.values() if row["buy_sol"] > 0]
    large_buy_min_sol = float(config.get("sticky_accumulation_large_buy_min_sol", 1.0))
    unique_buyers = len(buyers)
    large_buyers = sum(1 for row in buyers if row["buy_sol"] >= large_buy_min_sol)
    net_buy_ratio = net_buy_sol / buy_sol if buy_sol else 0.0
    top_buyer_share, top3_buyer_share = buyer_concentration(buyers)

    scale = 0.6 if relaxed else 1.0

    def scaled_float(key, default):
        return float(config.get(key, default)) * scale

    def scaled_int(key, default):
        return max(1, int(float(config.get(key, default)) * scale))

    if buy_sol < scaled_float("sticky_accumulation_min_buy_sol", 30):
        return None
    if net_buy_sol < scaled_float("sticky_accumulation_min_net_buy_sol", 10):
        return None
    if net_buy_ratio < float(config.get("sticky_accumulation_min_net_buy_ratio", 0.12)):
        return None
    if unique_buyers < scaled_int("sticky_accumulation_min_unique_buyers", 5):
        return None
    if large_buyers < scaled_int("sticky_accumulation_min_large_buyers", 3):
        return None
    return {
        "buy_sol": buy_sol,
        "sell_sol": sell_sol,
        "net_buy_sol": net_buy_sol,
        "net_buy_ratio": net_buy_ratio,
        "unique_buyers": unique_buyers,
        "large_buyers": large_buyers,
        "top_buyer_share": top_buyer_share,
        "top3_buyer_share": top3_buyer_share,
        "last_buy_time": max((int(swap.get("block_time") or 0) for swap in buy_swaps), default=0),
        "buyers": sorted(buyers, key=lambda row: row["buy_sol"], reverse=True),
        "buy_count": len(buy_swaps),
        "sell_count": len(sell_swaps),
    }


def sticky_accumulation_window_candidates(swaps, config, relaxed=False):
    if not sticky_accumulation_enabled(config) or not swaps:
        return []
    window_seconds = int(config["alert_window_minutes"]) * 60
    ordered = sorted(
        [swap for swap in swaps if swap.get("kind") in ("buy", "sell") and swap.get("block_time")],
        key=lambda swap: swap.get("block_time") or 0,
    )
    candidates = []
    for index, swap in enumerate(ordered):
        if swap.get("kind") != "buy":
            continue
        start = int(swap.get("block_time") or 0)
        end = start + window_seconds
        window = [item for item in ordered[index:] if int(item.get("block_time") or 0) <= end]
        metrics = sticky_accumulation_window_metrics(window, config, relaxed=relaxed)
        if not metrics:
            continue
        candidates.append({"start": start, "end": end, "window": window, "metrics": metrics})
    candidates.sort(
        key=lambda item: (
            item["metrics"]["net_buy_sol"],
            item["metrics"]["buy_sol"],
            item["metrics"]["unique_buyers"],
        ),
        reverse=True,
    )
    return candidates


def sticky_accumulation_precheck(swaps, config):
    return bool(sticky_accumulation_window_candidates(swaps, config, relaxed=True))


def build_sticky_accumulation_alerts(pool, swaps, config, rpc):
    if not sticky_accumulation_enabled(config) or not pool.token_address:
        return []
    candidates = sticky_accumulation_window_candidates(swaps, config, relaxed=False)
    if not candidates:
        return []
    max_candidates = int(config.get("sticky_accumulation_candidate_windows", 3))
    balance_limit = int(config.get("sticky_accumulation_balance_wallet_limit", 35))
    min_sticky_wallets = int(config.get("sticky_accumulation_min_sticky_wallets", 0))
    min_sticky_net_sol = float(config.get("sticky_accumulation_min_sticky_net_sol", 0.0))
    min_sticky_supply_pct = float(config.get("sticky_accumulation_min_sticky_supply_pct", 3.0))
    min_sticky_bought_pct = float(config.get("sticky_accumulation_min_sticky_bought_pct", 40.0))
    min_net_retention_pct = float(config.get("sticky_accumulation_min_net_token_retention_pct", 55.0))
    min_wallet_retention_pct = float(config.get("sticky_accumulation_min_wallet_retention_pct", 20.0))
    min_hold_minutes = float(config.get("sticky_accumulation_min_hold_minutes", 90))
    min_balance_coverage_pct = float(config.get("actionable_min_balance_coverage_pct", 80))
    try:
        supply = rpc.token_supply(pool.token_address)
    except Exception as exc:
        raise WaveDataUnavailable(
            f"sticky supply unavailable for {pool.token_address}: {exc}"
        ) from exc
    if not supply:
        raise WaveDataUnavailable(
            f"sticky supply unavailable for {pool.token_address}"
        )

    alerts = []
    coverage_available = False
    created_at = utc_now().isoformat().replace("+00:00", "Z")
    lane = config.get("lane") or config.get("mode")
    for candidate in candidates[:max_candidates]:
        metrics = candidate["metrics"]
        buyers = metrics["buyers"][:balance_limit]
        owner_activity = owner_activity_since(swaps, candidate["start"], [row.get("owner") for row in buyers])
        checked = []
        sticky_tokens = 0.0
        sticky_wallets = 0
        checked_bought_tokens = 0.0
        checked_net_tokens = 0.0
        sticky_net_sol = 0.0
        balance_errors = 0
        for row in buyers:
            owner = row.get("owner")
            if not owner:
                continue
            try:
                balance = rpc.token_balance(owner, pool.token_address)
            except Exception:
                balance = 0.0
                balance_errors += 1
            bought_tokens = float(row.get("token_bought") or 0)
            activity = owner_activity.get(owner) or {}
            sold_tokens = max(
                float(row.get("token_sold") or 0),
                float(activity.get("token_sold") or 0),
            )
            retention = attributed_wave_retention(
                balance,
                bought_tokens,
                sold_tokens,
                min_retention_pct=min_wallet_retention_pct,
            )
            net_tokens = retention["net_tokens"]
            retained_tokens = retention["retained_tokens"]
            checked_bought_tokens += bought_tokens
            checked_net_tokens += net_tokens
            if retention["qualified"]:
                sticky_tokens += retained_tokens
                sticky_wallets += 1
                sticky_net_sol += max(
                    0.0,
                    float(row["buy_sol"]) - float(activity.get("sell_sol") or row["sell_sol"]),
                ) * min(
                    1.0,
                    retention["retention_pct"] / 100,
                )
            checked.append(
                {
                    "owner": owner,
                    "buy_sol": row["buy_sol"],
                    "sell_sol": row["sell_sol"],
                    "net_sol": row["buy_sol"] - float(activity.get("sell_sol") or row["sell_sol"]),
                    "token_bought": bought_tokens,
                    "token_sold": sold_tokens,
                    "post_window_sell_sol": max(0.0, float(activity.get("sell_sol") or 0) - float(row["sell_sol"])),
                    "retained_from_wave": retained_tokens,
                    "wave_retention_pct": retention["retention_pct"],
                    "wave_net_coverage_pct": retention["net_coverage_pct"],
                    "retained_supply_pct": (retained_tokens / supply * 100) if supply else 0.0,
                    "current_balance": balance,
                    "current_supply_pct": (balance / supply * 100) if supply else 0.0,
                    "buy_count": row["buy_count"],
                    "sell_count": row["sell_count"],
                    "first_buy_time": row["first_buy_time"],
                }
            )

        balance_coverage_pct = (
            (len(checked) - balance_errors) / len(checked) * 100
            if checked
            else 0.0
        )
        if balance_coverage_pct < min_balance_coverage_pct:
            continue
        coverage_available = True
        sticky_supply_pct = sticky_tokens / supply * 100 if supply else 0.0
        sticky_bought_pct = sticky_tokens / checked_bought_tokens * 100 if checked_bought_tokens else 0.0
        net_retention_pct = sticky_tokens / checked_net_tokens * 100 if checked_net_tokens else 0.0
        if sticky_wallets < min_sticky_wallets:
            continue
        if sticky_net_sol < min_sticky_net_sol:
            continue
        if sticky_supply_pct < min_sticky_supply_pct:
            continue
        if sticky_bought_pct < min_sticky_bought_pct or net_retention_pct < min_net_retention_pct:
            continue

        events = []
        for row in metrics["buyers"][: int(config.get("alert_event_export_limit", 80))]:
            event = dict(row["top_buy"])
            event["time"] = event.get("time") or iso(event.get("block_time"))
            event["wallet_class"] = "sticky_buyer"
            event["token_recipient"] = row["owner"]
            event["wave_buy_sol"] = row["buy_sol"]
            event["wave_sell_sol"] = row["sell_sol"]
            event["wave_buy_count"] = row["buy_count"]
            event["wave_sell_count"] = row["sell_count"]
            events.append(event)

        alert = {
            "created_at": created_at,
            "score": 0,
            "lane": lane,
            "signal_family": "sticky_accumulation",
            "pool": pool.as_dict(),
            "obs_mcap_usd": pool.mcap_usd,
            "obs_price_usd": pool.price_usd,
            "obs_liquidity_usd": pool.liquidity_usd,
            "obs_mcap_at": created_at,
            "window_start": iso(candidate["start"]),
            "window_end": iso(candidate["end"]),
            "suspicious_wallets": sticky_wallets,
            "suspicious_sol": metrics["net_buy_sol"],
            "hard_wallets": 0,
            "support_wallets": 0,
            "hard_sol": 0.0,
            "support_sol": 0.0,
            "classes": {"sticky_buyer": sticky_wallets},
            "hard_classes": {},
            "support_classes": {},
            "common_funders": [],
            "common_recipients": [],
            "routed_buys": sum(1 for item in events if item.get("routed")),
            "wave": {
                "buy_sol": metrics["buy_sol"],
                "sell_sol": metrics["sell_sol"],
                "net_buy_sol": metrics["net_buy_sol"],
                "net_buy_ratio": metrics["net_buy_ratio"],
                "buy_count": metrics["buy_count"],
                "sell_count": metrics["sell_count"],
                "unique_buyers": metrics["unique_buyers"],
                "large_buyers": metrics["large_buyers"],
                "top_buyer_share": metrics["top_buyer_share"],
                "top3_buyer_share": metrics["top3_buyer_share"],
                "checked_wallets": len(checked),
                "checked_bought_tokens": checked_bought_tokens,
                "checked_net_tokens": checked_net_tokens,
                "balance_errors": balance_errors,
                "balance_coverage_pct": balance_coverage_pct,
                "hold_age_minutes": max(
                    0.0,
                    (time.time() - int(metrics.get("last_buy_time") or candidate["start"])) / 60,
                ),
                "min_hold_minutes": min_hold_minutes,
                "sticky_wallets": sticky_wallets,
                "sticky_net_sol": sticky_net_sol,
                "sticky_tokens": sticky_tokens,
                "sticky_supply_pct": sticky_supply_pct,
                "sticky_bought_pct": sticky_bought_pct,
                "net_token_retention_pct": net_retention_pct,
                "supply": supply,
                "top_buyers": checked[: min(20, len(checked))],
            },
            "events": events,
        }
        alert["score"] = wave_quality_score(alert, config)
        tier, reasons, penalties, quality_metrics = classify_alert_tier(
            pool,
            alert,
            {
                "hard_wallets": 0,
                "support_wallets": 0,
                "hard_sol": 0.0,
                "support_sol": 0.0,
                "hard_classes": {},
                "support_classes": {},
                "support_only": False,
            },
            config,
        )
        alert.update(
            {
                "action_tier": tier,
                "quality_reasons": reasons,
                "quality_penalties": penalties,
                "quality_metrics": quality_metrics,
            }
        )
        alerts.append(alert)
    if not coverage_available:
        raise WaveDataUnavailable(
            f"sticky balance coverage below {min_balance_coverage_pct:.0f}% "
            f"for {pool.token_address}"
        )
    return dedupe_pool_alerts(alerts)


def helius_page_budget(pool, config, kind, phase=None):
    phase_prefix = f"helius_{phase}_" if phase else "helius_"
    pages = int(
        config.get(
            f"{phase_prefix}{kind}_pages",
            config.get(f"helius_{kind}_pages", config.get("helius_transactions_pages", 4)),
        )
    )
    txns_1h = int(pool.txns_1h or 0)
    high_threshold = int(config.get("helius_high_txn_threshold", 10_000))
    medium_threshold = int(config.get("helius_medium_txn_threshold", 1_000))
    if txns_1h >= high_threshold:
        pages = max(
            pages,
            int(
                config.get(
                    f"{phase_prefix}{kind}_high_tx_pages",
                    config.get(f"helius_{kind}_high_tx_pages", config.get("helius_high_tx_pages", pages)),
                )
            ),
        )
    elif txns_1h >= medium_threshold:
        pages = max(
            pages,
            int(
                config.get(
                    f"{phase_prefix}{kind}_medium_tx_pages",
                    config.get(f"helius_{kind}_medium_tx_pages", config.get("helius_medium_tx_pages", pages)),
                )
            ),
        )
    if config.get("helius_dynamic_page_budget_enabled", True) and kind in ("recent", "incremental"):
        limit = max(1, int(config.get("helius_transactions_limit", 100)))
        if kind == "incremental":
            target_hours = float(config.get("helius_dynamic_incremental_target_hours", 1.25))
        else:
            lookback_hours = float(config.get("helius_recent_lookback_minutes", 360)) / 60
            target_hours = min(
                lookback_hours,
                float(config.get("helius_dynamic_recent_target_hours", 6)),
            )
        safety = max(1.0, float(config.get("helius_dynamic_page_safety_factor", 1.15)))
        estimated_transactions = max(0.0, txns_1h * target_hours * safety)
        estimated_pages = max(1, int((estimated_transactions + limit - 1) // limit))
        dynamic_max_key = f"helius_{phase}_dynamic_max_pages" if phase else "helius_dynamic_max_pages"
        dynamic_max = max(1, int(config.get(dynamic_max_key, config.get("helius_dynamic_max_pages", 4))))
        pages = max(pages, min(dynamic_max, estimated_pages))
    phase_max_key = f"helius_{phase}_max_pages" if phase else "helius_max_pages"
    if config.get(phase_max_key) is not None:
        pages = min(pages, max(1, int(config[phase_max_key])))
    return max(1, pages)


def decode_provider_cursor(value, default_provider="helius"):
    if isinstance(value, dict):
        provider = str(value.get("provider") or "").strip() or default_provider
        token = value.get("token")
        return provider, token
    if value:
        return default_provider, value
    return None, None


def encode_provider_cursor(provider, token):
    if not token:
        return None
    return {
        "provider": str(provider or "helius"),
        "token": token,
    }


def fetch_helius_pool_transactions(rpc, pool, config, pool_state, phase=None):
    now = int(time.time())
    limit = int(config.get("helius_transactions_limit", 100))
    lookback_minutes = int(config.get("helius_recent_lookback_minutes", max(60, int(config["alert_window_minutes"]))))
    recent_from = max(0, now - lookback_minutes * 60)
    previous_time = int(
        pool_state.get("rpc_latest_block_time")
        or pool_state.get("helius_latest_block_time")
        or 0
    )
    live_lookback_minutes = int(config.get("helius_live_lookback_minutes", min(lookback_minutes, 90)))
    if previous_time:
        recovery_hours = max(
            live_lookback_minutes / 60,
            float(config.get("helius_incremental_recovery_max_hours", 24)),
        )
        live_from = max(
            now - int(recovery_hours * 3600),
            previous_time - int(config.get("helius_incremental_overlap_seconds", 30)),
        )
        live_budget_kind = "incremental"
    else:
        live_from = recent_from
        live_budget_kind = "recent"
    live_cursor_record = pool_state.get("helius_live_cursor")
    live_cursor_provider, live_cursor = decode_provider_cursor(live_cursor_record)
    pending_signature_record = pool_state.get("helius_live_pending_signature")
    pending_block_time_record = int(pool_state.get("helius_live_pending_block_time") or 0)
    rolling_backlogs = [
        dict(item)
        for item in pool_state.get("helius_rolling_backlogs", [])
        if isinstance(item, dict) and item.get("cursor")
    ]
    if live_cursor:
        rolling_backlogs.append(
            {
                "provider": live_cursor_provider,
                "cursor": live_cursor,
                "from": int(pool_state.get("helius_live_from") or live_from),
                "head_signature": pending_signature_record,
                "head_block_time": pending_block_time_record,
                "created_at": pool_state.get("helius_live_cursor_created_at")
                or iso(now),
            }
        )
    rolling_backlogs.sort(
        key=lambda item: (
            int(item.get("head_block_time") or 0),
            int(item.get("from") or 0),
        )
    )
    had_rolling_backlog = bool(rolling_backlogs)
    cursor_head_age_seconds = (
        max(0, now - pending_block_time_record)
        if pending_block_time_record
        else 0
    )
    transactions = []
    seen = set()
    staged_updates = {}
    staged_deletes = {
        "helius_live_cursor",
        "helius_live_cursor_complete",
        "helius_live_pending_signature",
        "helius_live_pending_block_time",
        "helius_live_from",
        "helius_live_cursor_created_at",
        "helius_live_cursor_created_at",
    }
    stats = {
        "source": "enhanced_transactions",
        "phase": phase or "full",
        "pages": 0,
        "transactions": 0,
        "passes": [],
        "truncated": False,
        "live_truncated": False,
        "backfill_pending": False,
        "had_previous_state": bool(previous_time),
        "live_from": live_from,
        "live_resumed": had_rolling_backlog,
        "live_cursor_reset": False,
        "live_head_refreshed": had_rolling_backlog,
        "live_cursor_head_age_seconds": cursor_head_age_seconds,
        "rolling_backlog_segments_before": len(rolling_backlogs),
        "providers_used": [],
        "provider_failovers": [],
        "history_gap_seconds": max(0, live_from - previous_time) if previous_time else 0,
    }

    def add_batch(batch):
        added = 0
        for tx in batch:
            signature = (tx.get("transaction") or {}).get("signatures", [""])[0]
            if not signature or signature in seen:
                continue
            seen.add(signature)
            transactions.append(tx)
            added += 1
        return added

    def stage_cursor(save_cursor_key, cursor, provider=None):
        if not save_cursor_key:
            return
        complete_key = f"{save_cursor_key}_complete"
        if cursor:
            staged_updates[save_cursor_key] = encode_provider_cursor(provider, cursor)
            staged_deletes.add(complete_key)
        else:
            staged_deletes.add(save_cursor_key)
            staged_updates[complete_key] = True

    def run_pass(
        name,
        sort_order,
        max_pages,
        block_time=None,
        pagination_token=None,
        pagination_provider=None,
        save_cursor_key=None,
        target_from=None,
    ):
        cursor = pagination_token
        provider_name = pagination_provider
        pages = 0
        attempted_providers = set()
        pass_stats = {
            "name": name,
            "sort_order": sort_order,
            "pages": 0,
            "transactions": 0,
            "added": 0,
            "truncated": False,
            "oldest_block_time": None,
            "newest_block_time": None,
            "provider": provider_name,
            "provider_failovers": [],
        }
        if target_from is not None:
            pass_stats["target_from"] = int(target_from)
        if max_pages <= 0:
            stats["passes"].append(pass_stats)
            return pass_stats
        while True:
            try:
                while pages < max_pages:
                    kwargs = {
                        "limit": limit,
                        "sort_order": sort_order,
                        "pagination_token": cursor,
                        "block_time": block_time,
                    }
                    if isinstance(rpc, RoutedSolanaRpc):
                        kwargs["provider_name"] = provider_name
                        kwargs["excluded_providers"] = attempted_providers
                    result = rpc.transactions_for_address(pool.pool_address, **kwargs)
                    actual_provider = (
                        result.get("_provider")
                        or provider_name
                        or getattr(rpc, "provider_name", "helius")
                    )
                    provider_name = str(actual_provider)
                    pass_stats["provider"] = provider_name
                    if provider_name not in stats["providers_used"]:
                        stats["providers_used"].append(provider_name)
                    batch = result.get("data") or []
                    pages += 1
                    stats["pages"] += 1
                    pass_stats["pages"] += 1
                    pass_stats["transactions"] += len(batch)
                    pass_stats["added"] += add_batch(batch)
                    block_times = [
                        int(tx.get("blockTime") or 0)
                        for tx in batch
                        if int(tx.get("blockTime") or 0) > 0
                    ]
                    if block_times:
                        oldest = min(block_times)
                        newest = max(block_times)
                        current_oldest = pass_stats.get("oldest_block_time")
                        current_newest = pass_stats.get("newest_block_time")
                        pass_stats["oldest_block_time"] = (
                            oldest
                            if current_oldest is None
                            else min(current_oldest, oldest)
                        )
                        pass_stats["newest_block_time"] = (
                            newest
                            if current_newest is None
                            else max(current_newest, newest)
                        )
                    cursor = result.get("paginationToken")
                    if len(batch) < limit:
                        cursor = None
                    if not cursor or not batch:
                        stage_cursor(save_cursor_key, None)
                        break
                else:
                    pass_stats["truncated"] = True
                    stats["truncated"] = True
                    stage_cursor(save_cursor_key, cursor, provider_name)
                break
            except Exception as exc:
                if not isinstance(rpc, RoutedSolanaRpc):
                    raise
                if provider_name:
                    attempted_providers.add(provider_name)
                next_provider = rpc.next_enhanced_provider(excluded=attempted_providers)
                if not next_provider:
                    raise
                failover = {
                    "from": provider_name,
                    "to": next_provider,
                    "reason": str(exc)[:200],
                }
                pass_stats["provider_failovers"].append(failover)
                stats["provider_failovers"].append(failover)
                provider_name = next_provider
                cursor = None
                pages = 0
        pass_stats["pagination_remaining"] = bool(cursor)
        pass_stats["pagination_token"] = cursor
        pass_stats["pagination_provider"] = provider_name
        if target_from is not None:
            pass_stats["coverage_complete"] = not bool(cursor)
        stats["passes"].append(pass_stats)
        return pass_stats

    head_page_budget = helius_page_budget(
        pool,
        config,
        live_budget_kind,
        phase=phase,
    )
    if rolling_backlogs:
        head_page_budget = min(
            head_page_budget,
            max(
                1,
                int(
                    config.get(
                        f"helius_{phase}_rolling_head_pages" if phase else "helius_rolling_head_pages",
                        config.get("helius_rolling_head_pages", 2),
                    )
                ),
            ),
        )
    live_pass = run_pass(
        "live_head",
        "desc",
        head_page_budget,
        block_time={"gte": live_from},
        target_from=live_from,
    )
    newest_head = (
        max(
            transactions,
            key=lambda tx: (
                int(tx.get("blockTime") or 0),
                int(tx.get("transactionIndex") or 0),
            ),
        )
        if transactions
        else None
    )
    pending_signature = (
        (newest_head.get("transaction") or {}).get("signatures", [""])[0]
        if newest_head
        else None
    )
    pending_block_time = int((newest_head or {}).get("blockTime") or 0)
    completed_checkpoint = None

    if live_pass.get("pagination_remaining"):
        segment = {
            "provider": live_pass.get("pagination_provider"),
            "cursor": live_pass.get("pagination_token"),
            "from": live_from,
            "head_signature": pending_signature,
            "head_block_time": pending_block_time,
            "created_at": iso(now),
        }
        if segment["cursor"] and not any(
            item.get("cursor") == segment["cursor"]
            and item.get("provider") == segment["provider"]
            for item in rolling_backlogs
        ):
            rolling_backlogs.append(segment)
            rolling_backlogs.sort(
                key=lambda item: (
                    int(item.get("head_block_time") or 0),
                    int(item.get("from") or 0),
                )
            )
    elif pending_signature:
        completed_checkpoint = {
            "signature": pending_signature,
            "block_time": pending_block_time,
        }
        rolling_backlogs = []

    if had_rolling_backlog and rolling_backlogs:
        backlog = rolling_backlogs[0]
        backlog_pages = max(
            1,
            int(
                config.get(
                    f"helius_{phase}_rolling_backlog_pages" if phase else "helius_rolling_backlog_pages",
                    config.get("helius_rolling_backlog_pages", 2),
                )
            ),
        )
        backlog_pass = run_pass(
            "rolling_backlog",
            "desc",
            backlog_pages,
            block_time={"gte": int(backlog.get("from") or live_from)},
            pagination_token=backlog.get("cursor"),
            pagination_provider=backlog.get("provider"),
            target_from=int(backlog.get("from") or live_from),
        )
        if backlog_pass.get("pagination_remaining"):
            backlog["cursor"] = backlog_pass.get("pagination_token")
            backlog["provider"] = backlog_pass.get("pagination_provider")
        else:
            completed_head_time = int(backlog.get("head_block_time") or 0)
            completed_head_signature = backlog.get("head_signature")
            if completed_head_time and completed_head_signature:
                completed_checkpoint = {
                    "signature": completed_head_signature,
                    "block_time": completed_head_time,
                }
            rolling_backlogs = [
                item
                for item in rolling_backlogs[1:]
                if int(item.get("head_block_time") or 0) > completed_head_time
            ]

    max_segments = max(1, int(config.get("helius_rolling_backlog_max_segments", 12)))
    if len(rolling_backlogs) > max_segments:
        rolling_backlogs = rolling_backlogs[-max_segments:]
        stats["rolling_backlog_segments_dropped"] = True
    if rolling_backlogs:
        staged_updates["helius_rolling_backlogs"] = rolling_backlogs
    else:
        staged_deletes.add("helius_rolling_backlogs")

    pass_oldest = [
        int(item.get("oldest_block_time") or 0)
        for item in stats["passes"]
        if item.get("oldest_block_time")
    ]
    pass_newest = [
        int(item.get("newest_block_time") or 0)
        for item in stats["passes"]
        if item.get("newest_block_time")
    ]
    stats["rolling_backlog_segments_after"] = len(rolling_backlogs)
    stats["rolling_gap_pending"] = bool(rolling_backlogs)
    stats["live_truncated"] = bool(rolling_backlogs)
    stats["live_oldest_block_time"] = min(pass_oldest) if pass_oldest else None
    stats["live_newest_block_time"] = max(pass_newest) if pass_newest else pending_block_time
    if stats.get("live_newest_block_time"):
        stats["live_head_lag_seconds"] = max(0, now - int(stats["live_newest_block_time"]))
    if completed_checkpoint:
        stats["live_checkpoint"] = completed_checkpoint
    if rolling_backlogs:
        stats["history_gap_seconds"] = max(
            int(stats.get("history_gap_seconds") or 0),
            max(0, now - int(previous_time or live_from)),
        )

    market_head = None
    if market_activity_requires_head_check(pool, config):
        expected_max_lag = market_activity_expected_head_lag_seconds(pool, config)
        enhanced_head = int(stats.get("live_newest_block_time") or 0)
        enhanced_lag = (
            max(0, now - enhanced_head)
            if enhanced_head
            else expected_max_lag + 1
        )
        if enhanced_lag <= expected_max_lag:
            mark_market_activity_head_state(
                pool_state,
                pool,
                {"status": "fresh"},
                config,
                now=now,
            )
        else:
            try:
                head_signatures = rpc.signatures_for_address(
                    pool.pool_address,
                    limit=max(
                        1,
                        int(config.get("market_activity_consistency_signature_limit", 5)),
                    ),
                )
                market_head = market_activity_head_probe(
                    pool,
                    head_signatures,
                    config,
                    now=now,
                )
            except Exception as exc:
                market_head = {
                    "status": "unverified",
                    "expected_max_lag_seconds": expected_max_lag,
                    "error": str(exc)[:200],
                }
            stats["market_activity_probe"] = market_head
            if market_head.get("status") == "stale":
                stats["market_activity_stale"] = True
                mark_market_activity_head_state(
                    pool_state,
                    pool,
                    market_head,
                    config,
                    now=now,
                )
            elif market_head.get("status") == "unverified":
                stats["market_activity_unverified"] = True
            elif market_head.get("status") == "fresh":
                mark_market_activity_head_state(
                    pool_state,
                    pool,
                    market_head,
                    config,
                    now=now,
                )
                standard_head = int(market_head.get("latest_block_time") or 0)
                mismatch_seconds = max(
                    0,
                    int(config.get("market_activity_consistency_head_mismatch_seconds", 60)),
                )
                if not enhanced_head or standard_head - enhanced_head > mismatch_seconds:
                    raise EnhancedHistoryHeadMismatch(
                        "enhanced transaction head lagged the standard RPC head "
                        f"by {max(0, standard_head - enhanced_head)}s"
                    )

    age_hours = pool.age_hours()
    initial_max_age = float(config.get("helius_initial_backfill_max_age_hours", 96))
    retention_hours = float(config.get("state_swap_buffer_retention_hours", 24))
    should_backfill = (
        config.get("helius_initial_backfill_enabled", True)
        and phase != "probe"
        and pool.pair_created_at
        and age_hours is not None
        and age_hours <= min(initial_max_age, retention_hours)
        and not pool_state.get("helius_initial_backfill_cursor_complete")
    )
    if should_backfill:
        launch_from = max(
            0,
            int(pool.pair_created_at) - int(config.get("helius_launch_time_cushion_seconds", 120)),
            int(now - retention_hours * 3600),
        )
        backfill_cursor = pool_state.get("helius_initial_backfill_cursor")
        backfill_provider, backfill_cursor = decode_provider_cursor(backfill_cursor)
        if int(pool_state.get("helius_initial_backfill_from") or 0) != launch_from:
            backfill_cursor = None
            backfill_provider = None
            staged_deletes.add("helius_initial_backfill_cursor")
            staged_deletes.add("helius_initial_backfill_cursor_complete")
            staged_updates["helius_initial_backfill_from"] = launch_from
        backfill_pages = min(
            helius_page_budget(pool, config, "initial_backfill", phase=phase),
            max(1, int(config.get("helius_backfill_max_pages_per_scan", 1))),
        )
        backfill_pass = run_pass(
            "launch_backfill",
            "asc",
            backfill_pages,
            block_time={"gte": launch_from},
            pagination_token=backfill_cursor,
            pagination_provider=backfill_provider,
            save_cursor_key="helius_initial_backfill_cursor",
            target_from=launch_from,
        )
        stats["backfill_pending"] = bool(backfill_pass.get("truncated"))

    if int(pool.txns_1h or 0) >= int(config.get("helius_high_txn_threshold", 10_000)):
        tail_pages_key = f"helius_{phase}_high_tx_tail_pages" if phase else "helius_high_tx_tail_pages"
        run_pass(
            "high_tx_tail",
            "desc",
            int(config.get(tail_pages_key, config.get("helius_high_tx_tail_pages", 4))),
            block_time={"gte": recent_from},
        )

    for key in staged_deletes:
        pool_state.pop(key, None)
    pool_state.update(staged_updates)
    stats["transactions"] = len(transactions)
    return sorted(transactions, key=lambda tx: (tx.get("blockTime") or 0, tx.get("transactionIndex") or 0)), stats


def merge_transactions(*transaction_groups):
    by_signature = {}
    for group in transaction_groups:
        for tx in group or []:
            signature = (tx.get("transaction") or {}).get("signatures", [""])[0]
            if not signature:
                continue
            by_signature[signature] = tx
    return sorted(
        by_signature.values(),
        key=lambda tx: (tx.get("blockTime") or 0, tx.get("transactionIndex") or 0),
    )


def update_pool_transaction_state(pool_state, pool, txs, checkpoint=None):
    checkpoint = checkpoint or {}
    if checkpoint:
        signature = checkpoint.get("signature")
        block_time = int(checkpoint.get("block_time") or 0)
    elif txs:
        latest = max(txs, key=lambda tx: ((tx.get("blockTime") or 0), tx.get("transactionIndex") or 0))
        signature = (latest.get("transaction") or {}).get("signatures", [""])[0]
        block_time = int(latest.get("blockTime") or 0)
    else:
        return
    if signature:
        pool_state["latest_signature"] = signature
        pool_state["rpc_latest_signature"] = signature
        pool_state["helius_latest_signature"] = signature
    if block_time:
        pool_state["latest_time"] = iso(block_time)
        pool_state["rpc_latest_time"] = iso(block_time)
        pool_state["rpc_latest_block_time"] = block_time
        pool_state["helius_latest_time"] = iso(block_time)
        pool_state["helius_latest_block_time"] = block_time
    pool_state["symbol"] = pool.symbol
    for key in (
        "helius_live_cursor",
        "helius_live_cursor_complete",
        "helius_live_pending_signature",
        "helius_live_pending_block_time",
        "helius_live_from",
    ):
        pool_state.pop(key, None)


def select_buy_swaps_for_classification(candidates, config, budget_limit):
    if budget_limit <= 0:
        return []
    by_signature = {}
    by_wallet = set()
    ordered = []
    dedupe_wallets = bool(config.get("helius_dedupe_classification_wallets", True))

    def add(swaps):
        for swap in swaps:
            signature = swap.get("signature")
            if not signature or signature in by_signature:
                continue
            signer = swap.get("signer")
            if dedupe_wallets and signer:
                if signer in by_wallet:
                    continue
                by_wallet.add(signer)
            by_signature[signature] = swap
            ordered.append(swap)
            if len(ordered) >= budget_limit:
                return

    global_limit = min(
        budget_limit,
        int(config.get("helius_classify_global_buy_limit", max(20, budget_limit // 3))),
    )
    top_global = sorted(
        candidates,
        key=lambda swap: (swap.get("sol_amount", 0.0), swap.get("block_time") or 0),
        reverse=True,
    )[:global_limit]
    add(top_global)

    window_seconds = int(config.get("helius_classify_window_minutes", config["alert_window_minutes"])) * 60
    per_window = int(config.get("helius_classify_top_buys_per_window", 12))
    buckets = defaultdict(list)
    for swap in candidates:
        block_time = int(swap.get("block_time") or 0)
        bucket = block_time // window_seconds if window_seconds else 0
        buckets[bucket].append(swap)
    for bucket in sorted(buckets):
        if len(ordered) >= budget_limit:
            break
        add(
            sorted(
                buckets[bucket],
                key=lambda swap: (swap.get("sol_amount", 0.0), swap.get("block_time") or 0),
                reverse=True,
            )[:per_window]
        )

    return ordered[:budget_limit]


def buy_swap_candidates(swaps, config):
    min_sol = float(config["classify_buy_min_sol"])
    return [swap for swap in swaps if swap.get("kind") == "buy" and swap.get("sol_amount", 0.0) >= min_sol]


def classify_buy_swaps(rpc, swaps, config, state, classification_budget):
    candidates = buy_swap_candidates(swaps, config)
    if config.get("helius_dedupe_classification_wallets", True):
        candidate_count = len(
            {
                swap.get("signer") or swap.get("signature")
                for swap in candidates
                if swap.get("signer") or swap.get("signature")
            }
        )
    else:
        candidate_count = len(candidates)
    per_pool_limit = int(config.get("max_wallet_classifications_per_pool", classification_budget["remaining"]))
    budget_limit = min(classification_budget["remaining"], per_pool_limit)
    selected = select_buy_swaps_for_classification(candidates, config, budget_limit)
    events = []
    classification_errors = 0
    for swap in selected:
        cache_key = wallet_cache_key(
            swap["signer"],
            swap["signature"],
            swap.get("block_time"),
            config.get("wallet_cache_bucket_hours", 6),
        )
        cache_hit = cache_key in state.setdefault("wallet_cache", {})
        if not cache_hit and classification_budget["remaining"] <= 0:
            break
        if not cache_hit:
            classification_budget["remaining"] -= 1
        try:
            wallet_info = classify_wallet(rpc, swap["signer"], swap["signature"], swap["block_time"], config, state)
        except Exception as exc:
            classification_errors += 1
            print(
                f"warn: wallet classification failed for {swap.get('signer')} "
                f"on {swap.get('signature')}: {exc}",
                file=sys.stderr,
            )
            continue
        swap.update(wallet_info)
        events.append(swap)
    return events, candidate_count, classification_errors


def parse_helius_swaps(txs, pool):
    swaps = []
    parse_errors = 0
    for tx in txs:
        try:
            swap = parse_pool_swap(tx, pool)
        except Exception:
            parse_errors += 1
            continue
        if swap:
            swaps.append(swap)
    return swaps, parse_errors


def merge_events(*event_groups, dedupe_wallets=False):
    by_key = {}
    for group in event_groups:
        for event in group or []:
            signature = event.get("signature")
            if not signature:
                continue
            signer = event.get("signer")
            key = signer if dedupe_wallets and signer else signature
            previous = by_key.get(key)
            if previous is None or (
                float(event.get("sol_amount") or 0),
                int(event.get("block_time") or 0),
                str(signature),
            ) >= (
                float(previous.get("sol_amount") or 0),
                int(previous.get("block_time") or 0),
                str(previous.get("signature") or ""),
            ):
                by_key[key] = event
    return sorted(by_key.values(), key=lambda event: event.get("block_time") or 0)


def probe_classification_config(config):
    probe_config = dict(config)
    probe_limit = int(config.get("helius_probe_wallet_limit", config.get("max_wallet_classifications_per_pool", 60)))
    probe_config["max_wallet_classifications_per_pool"] = min(
        probe_limit,
        int(config.get("max_wallet_classifications_per_pool", probe_limit)),
    )
    if config.get("helius_probe_classify_global_buy_limit") is not None:
        probe_config["helius_classify_global_buy_limit"] = int(config["helius_probe_classify_global_buy_limit"])
    else:
        probe_config["helius_classify_global_buy_limit"] = min(
            int(config.get("helius_classify_global_buy_limit", probe_limit)),
            probe_limit,
        )
    if config.get("helius_probe_classify_top_buys_per_window") is not None:
        probe_config["helius_classify_top_buys_per_window"] = int(config["helius_probe_classify_top_buys_per_window"])
    return probe_config


def should_deep_scan(pool, config, pool_state, events, candidate_buys, alerts, classification_budget, swaps=None):
    if not config.get("helius_deep_scan_enabled", True):
        return False, "deep_disabled"
    if not config.get("helius_probe_enabled", True):
        return True, "probe_disabled"
    if alerts:
        return True, "probe_alert"

    score, suspicious, common_funders, common_recipients, evidence = score_events(events, config)
    high_conviction_wallets = {
        event["signer"]
        for event in suspicious
        if event.get("signer") and event.get("wallet_class") in ("fresh", "freshish", "dormant")
    }
    hard_sol = float(evidence.get("hard_sol") or 0)
    support_sol = float(evidence.get("support_sol") or 0)
    if common_funders or common_recipients:
        return True, "linked_wallets"
    if any(event.get("wallet_class") == "dormant" for event in suspicious):
        return True, "dormant_wallet"
    if len(high_conviction_wallets) >= int(config.get("helius_deep_min_suspicious_wallets", 2)):
        return True, "suspicious_wallet_probe"
    if hard_sol >= float(config.get("helius_deep_min_suspicious_sol", 8)):
        return True, "suspicious_flow_probe"
    if high_conviction_wallets and hard_sol + support_sol >= float(config.get("helius_deep_min_suspicious_sol", 8)):
        return True, "supported_suspicious_flow_probe"

    min_candidates = int(config.get("helius_deep_min_candidate_buys", 20))
    probe_buy_sol = sum(event.get("sol_amount", 0.0) for event in events)
    if (
        candidate_buys >= min_candidates
        and probe_buy_sol >= float(config.get("helius_deep_min_probe_buy_sol", 15))
        and score > 0
    ):
        return True, "flow_probe"
    if reactivation_wave_precheck(swaps or [], config):
        return True, "reactivation_wave_probe"
    if sticky_accumulation_precheck(swaps or [], config):
        return True, "sticky_accumulation_probe"

    audit_interval = float(config.get("helius_deep_audit_interval_hours", 0))
    audits_remaining = int(classification_budget.get("deep_audits_remaining", 0))
    if audit_interval > 0 and audits_remaining > 0 and candidate_buys >= min_candidates:
        last_deep = parse_timestamp(pool_state.get("helius_deep_scanned_at"))
        if not last_deep or time.time() - last_deep >= audit_interval * 3600:
            classification_budget["deep_audits_remaining"] = audits_remaining - 1
            return True, "scheduled_deep_audit"

    return False, "probe_clean"


def combine_fetch_stats(probe_stats, deep_stats):
    combined = dict(deep_stats or probe_stats or {})
    if not probe_stats or not deep_stats:
        return combined
    combined["phase"] = "probe_plus_deep"
    combined["pages"] = int(probe_stats.get("pages", 0)) + int(deep_stats.get("pages", 0))
    combined["transactions"] = int(probe_stats.get("transactions", 0)) + int(deep_stats.get("transactions", 0))
    combined["passes"] = [*(probe_stats.get("passes") or []), *(deep_stats.get("passes") or [])]
    combined["truncated"] = bool(probe_stats.get("truncated") or deep_stats.get("truncated"))
    if deep_stats.get("live_resumed"):
        combined["live_truncated"] = bool(deep_stats.get("live_truncated"))
    else:
        combined["live_truncated"] = bool(
            probe_stats.get("live_truncated") or deep_stats.get("live_truncated")
        )
    combined["backfill_pending"] = bool(
        probe_stats.get("backfill_pending") or deep_stats.get("backfill_pending")
    )
    combined["rolling_gap_pending"] = bool(
        probe_stats.get("rolling_gap_pending")
        or deep_stats.get("rolling_gap_pending")
    )
    combined["rolling_backlog_segments_after"] = max(
        int(probe_stats.get("rolling_backlog_segments_after") or 0),
        int(deep_stats.get("rolling_backlog_segments_after") or 0),
    )
    combined["live_cursor_reset"] = bool(
        probe_stats.get("live_cursor_reset") or deep_stats.get("live_cursor_reset")
    )
    combined["market_activity_stale"] = bool(
        probe_stats.get("market_activity_stale") or deep_stats.get("market_activity_stale")
    )
    combined["market_activity_unverified"] = bool(
        probe_stats.get("market_activity_unverified")
        or deep_stats.get("market_activity_unverified")
    )
    combined["market_activity_probe"] = (
        deep_stats.get("market_activity_probe")
        or probe_stats.get("market_activity_probe")
    )
    combined["live_cursor_head_age_seconds"] = max(
        int(probe_stats.get("live_cursor_head_age_seconds") or 0),
        int(deep_stats.get("live_cursor_head_age_seconds") or 0),
    )
    combined["had_previous_state"] = bool(probe_stats.get("had_previous_state"))
    combined["history_gap_seconds"] = max(
        int(probe_stats.get("history_gap_seconds") or 0),
        int(deep_stats.get("history_gap_seconds") or 0),
    )
    live_newest = [
        int(value)
        for value in (probe_stats.get("live_newest_block_time"), deep_stats.get("live_newest_block_time"))
        if value
    ]
    live_oldest = [
        int(value)
        for value in (probe_stats.get("live_oldest_block_time"), deep_stats.get("live_oldest_block_time"))
        if value
    ]
    if live_newest:
        combined["live_newest_block_time"] = max(live_newest)
    if live_oldest:
        combined["live_oldest_block_time"] = min(live_oldest)
    live_lags = [
        int(value)
        for value in (probe_stats.get("live_head_lag_seconds"), deep_stats.get("live_head_lag_seconds"))
        if value is not None
    ]
    if live_lags:
        combined["live_head_lag_seconds"] = min(live_lags)
    live_checkpoint = deep_stats.get("live_checkpoint") or probe_stats.get("live_checkpoint")
    if live_checkpoint:
        combined["live_checkpoint"] = live_checkpoint
    combined["probe"] = probe_stats
    combined["deep"] = deep_stats
    return combined


def apply_alert_data_quality(
    alerts,
    fetch_stats,
    candidate_buys,
    classified_buys,
    classification_errors,
    config,
):
    fetch_stats = fetch_stats or {}
    partial_reasons = []
    if fetch_stats.get("live_truncated"):
        partial_reasons.append("partial live transaction window")
    if int(fetch_stats.get("transaction_errors") or 0):
        partial_reasons.append("transaction detail fetch errors")
    if int(fetch_stats.get("parse_errors") or 0):
        partial_reasons.append("transaction parse errors")
    if fetch_stats.get("wave_buffer_truncated"):
        partial_reasons.append("partial accumulation buffer")
    if fetch_stats.get("market_activity_unverified"):
        partial_reasons.append("market activity head could not be verified")
    if int(fetch_stats.get("history_gap_seconds") or 0) > int(
        config.get("actionable_max_history_gap_seconds", 60)
    ):
        partial_reasons.append("onchain history gap")
    if classification_errors:
        partial_reasons.append("wallet classification errors")

    if classified_buys > candidate_buys:
        raise ValueError(
            "classification coverage invariant violated: "
            f"{classified_buys} classified candidates exceeds {candidate_buys} candidates"
        )
    classification_coverage_pct = (
        classified_buys / candidate_buys * 100 if candidate_buys else 100.0
    )
    for alert in alerts or []:
        reasons = list(partial_reasons)
        if (
            alert.get("signal_family") not in ("sticky_accumulation", "reactivation_wave")
            and classification_coverage_pct < float(config.get("actionable_min_classification_coverage_pct", 35))
        ):
            reasons.append("partial wallet classification")
        wave = alert.get("wave") or {}
        balance_coverage_value = wave.get("balance_coverage_pct")
        balance_coverage_pct = 100.0 if balance_coverage_value is None else float(balance_coverage_value)
        if balance_coverage_pct < float(
            config.get("actionable_min_balance_coverage_pct", 80)
        ):
            reasons.append("partial balance coverage")
        owner_resolution_coverage_pct = float(
            wave.get("owner_resolution_coverage_pct", 100.0)
        )
        if owner_resolution_coverage_pct < float(
            config.get("actionable_min_owner_resolution_coverage_pct", 80)
        ):
            reasons.append("partial routed buyer attribution")
        reasons = list(dict.fromkeys(reasons))
        alert["data_quality"] = {
            "status": "partial" if reasons else "complete",
            "reasons": reasons,
            "live_truncated": bool(fetch_stats.get("live_truncated")),
            "history_gap_seconds": int(fetch_stats.get("history_gap_seconds") or 0),
            "transaction_errors": int(fetch_stats.get("transaction_errors") or 0),
            "parse_errors": int(fetch_stats.get("parse_errors") or 0),
            "wave_buffer_truncated": bool(fetch_stats.get("wave_buffer_truncated")),
            "classification_coverage_pct": classification_coverage_pct,
            "balance_coverage_pct": balance_coverage_pct,
            "owner_resolution_coverage_pct": owner_resolution_coverage_pct,
        }
        if reasons and alert.get("action_tier") in ("actionable", "hot_reactivation"):
            alert["action_tier"] = "watch"
            penalties = list(alert.get("quality_penalties") or [])
            if "partial onchain coverage" not in penalties:
                penalties.append("partial onchain coverage")
            alert["quality_penalties"] = penalties
    return alerts


def schedule_signal_recheck(pool_state, alerts, config):
    thesis = pool_state.get("signal_thesis")
    if (
        config.get("signal_thesis_tracking_enabled", True)
        and isinstance(thesis, dict)
    ):
        if thesis.get("status") == "invalidated":
            pool_state.pop("signal_recheck_due_at", None)
            thesis.pop("next_check_at", None)
            return None
        interval = signal_thesis_recheck_interval_minutes(thesis, config)
        due_at = int(time.time() + interval * 60)
        pool_state["signal_recheck_due_at"] = iso(due_at)
        thesis["next_check_at"] = iso(due_at)
        return due_at

    wave_alerts = [alert for alert in alerts or [] if alert.get("wave")]
    if not wave_alerts:
        pool_state.pop("signal_recheck_due_at", None)
        return None

    pending_minutes = []
    for alert in wave_alerts:
        wave = alert.get("wave") or {}
        remaining = float(wave.get("min_hold_minutes") or 0) - float(
            wave.get("hold_age_minutes") or 0
        )
        if remaining > 0:
            pending_minutes.append(remaining)
    if not pending_minutes:
        pending_minutes.append(
            max(15.0, float(config.get("signal_retention_recheck_minutes", 60)))
        )
    due_at = int(time.time() + max(60, min(pending_minutes) * 60))
    pool_state["signal_recheck_due_at"] = iso(due_at)
    return due_at


def pool_backfill_pending(pool, pool_state, config):
    age_hours = pool.age_hours()
    initial_max_age = float(config.get("helius_initial_backfill_max_age_hours", 96))
    retention_hours = float(config.get("state_swap_buffer_retention_hours", 24))
    return bool(
        config.get("helius_initial_backfill_enabled", True)
        and pool.pair_created_at
        and age_hours is not None
        and age_hours <= min(initial_max_age, retention_hours)
        and not pool_state.get("helius_initial_backfill_cursor_complete")
    )


def pool_has_new_activity(rpc, pool, pool_state, config):
    recheck_due = parse_timestamp(pool_state.get("signal_recheck_due_at"))
    thesis_recheck_due = bool(
        recheck_due and int(time.time()) >= recheck_due
    )
    previous_signature = (
        pool_state.get("rpc_latest_signature")
        or pool_state.get("helius_latest_signature")
        or pool_state.get("latest_signature")
    )
    if not config.get("helius_activity_probe_enabled", True) or not previous_signature:
        return True, {"reason": "first_scan_or_probe_disabled"}
    market_active_threshold = max(
        0,
        int(config.get("helius_activity_probe_market_active_txns_1h", 5)),
    )
    if market_active_threshold and int(pool.txns_1h or 0) >= market_active_threshold:
        return True, {
            "reason": "market_activity",
            "txns_1h": int(pool.txns_1h or 0),
        }
    if config.get("helius_backfill_on_idle", False) and pool_backfill_pending(pool, pool_state, config):
        return True, {"reason": "launch_backfill_pending"}
    try:
        signatures = rpc.signatures_for_address(
            pool.pool_address,
            limit=max(1, int(config.get("helius_activity_probe_signature_limit", 5))),
        )
    except Exception as exc:
        return True, {"reason": "probe_failed", "error": str(exc)}
    latest_successful = next((item.get("signature") for item in signatures if not item.get("err")), None)
    pool_state["last_activity_probe_at"] = utc_now().isoformat().replace("+00:00", "Z")
    if latest_successful:
        pool_state["last_activity_signature"] = latest_successful
    if not latest_successful:
        return True, {"reason": "probe_empty"}
    if latest_successful == previous_signature and thesis_recheck_due:
        return False, {
            "reason": "signal_thesis_recheck",
            "latest_signature": latest_successful,
        }
    return latest_successful != previous_signature, {
        "reason": "new_signature" if latest_successful != previous_signature else "unchanged",
        "latest_signature": latest_successful,
    }


def scan_pool_helius_transactions(rpc, pool, config, state, classification_budget):
    pool_state = state.setdefault("pools", {}).setdefault(pool.pool_address, {})
    has_new_activity, activity_probe = pool_has_new_activity(rpc, pool, pool_state, config)
    if not has_new_activity:
        signal_thesis = refresh_signal_thesis(
            rpc,
            pool,
            pool_state,
            [],
            config,
        )
        return [], {
            "pool": pool.as_dict(),
            "lane": config.get("lane") or config.get("mode"),
            "trade_source": "helius_activity_probe",
            "new_signatures": 0,
            "transactions_scanned": 0,
            "parsed_swaps": 0,
            "candidate_buys": 0,
            "classified_buys": 0,
            "classes": {},
            "buy_sol": 0.0,
            "wave_alerts": 0,
            "wave_net_buy_sol": 0.0,
            "wave_sticky_supply_pct": 0.0,
            "parse_errors": 0,
            "classification_errors": 0,
            "signal_thesis": signal_thesis,
            "activity_probe": activity_probe,
            "trade_fetch": {
                "source": "helius_activity_probe",
                "phase": "probe_only",
                "pages": 0,
                "transactions": 0,
                "passes": [],
                "truncated": False,
                "activity_unchanged": True,
            },
        }
    preclassified = False
    classify_wallets = bool(config.get("classic_alerts_enabled", True))
    swaps = []
    parse_errors = 0
    events = []
    seed_events = []
    candidate_buys = 0
    classification_errors = 0
    try:
        if config.get("helius_probe_enabled", True):
            probe_txs, probe_fetch_stats = fetch_helius_pool_transactions(rpc, pool, config, pool_state, phase="probe")
            if probe_fetch_stats.get("market_activity_stale"):
                signal_thesis = refresh_signal_thesis(
                    rpc,
                    pool,
                    pool_state,
                    [],
                    config,
                )
                return [], {
                    "pool": pool.as_dict(),
                    "lane": config.get("lane") or config.get("mode"),
                    "trade_source": "enhanced_transactions",
                    "new_signatures": len(probe_txs),
                    "transactions_scanned": len(probe_txs),
                    "parsed_swaps": 0,
                    "candidate_buys": 0,
                    "classified_buys": 0,
                    "classes": {},
                    "buy_sol": 0.0,
                    "wave_alerts": 0,
                    "wave_net_buy_sol": 0.0,
                    "wave_sticky_supply_pct": 0.0,
                    "parse_errors": 0,
                    "classification_errors": 0,
                    "signal_thesis": signal_thesis,
                    "market_snapshot_suppressed": True,
                    "trade_fetch": probe_fetch_stats,
                }
            probe_swaps, probe_parse_errors = parse_helius_swaps(probe_txs, pool)
            if classify_wallets:
                probe_config = probe_classification_config(config)
                probe_events, probe_candidate_buys, probe_classification_errors = classify_buy_swaps(
                    rpc,
                    probe_swaps,
                    probe_config,
                    state,
                    classification_budget,
                )
                probe_alerts = build_alerts(pool, probe_events, config)
            else:
                probe_events = []
                probe_candidate_buys = len(buy_swap_candidates(probe_swaps, config))
                probe_classification_errors = 0
                probe_alerts = []
            probe_signal_swaps = probe_swaps
            if config.get("reactivation_wave_enabled"):
                probe_signal_swaps = merge_reactivation_wave_swaps(pool_state, probe_signal_swaps, config)
            if config.get("sticky_accumulation_enabled"):
                probe_signal_swaps = merge_sticky_accumulation_swaps(pool_state, probe_signal_swaps, config)
            deepen, deep_reason = should_deep_scan(
                pool,
                config,
                pool_state,
                probe_events,
                probe_candidate_buys,
                probe_alerts,
                classification_budget,
                swaps=probe_signal_swaps,
            )
            if deepen:
                deep_txs, deep_fetch_stats = fetch_helius_pool_transactions(rpc, pool, config, pool_state, phase="deep")
                txs = merge_transactions(probe_txs, deep_txs)
                fetch_stats = combine_fetch_stats(probe_fetch_stats, deep_fetch_stats)
                fetch_stats["deep_reason"] = deep_reason
                pool_state["helius_deep_scanned_at"] = utc_now().isoformat().replace("+00:00", "Z")
                seed_events = probe_events
            else:
                txs = probe_txs
                fetch_stats = dict(probe_fetch_stats)
                fetch_stats["deep_skipped"] = True
                fetch_stats["deep_reason"] = deep_reason
                swaps = probe_swaps
                parse_errors = probe_parse_errors
                events = probe_events
                candidate_buys = probe_candidate_buys
                classification_errors = probe_classification_errors
                preclassified = True
        else:
            txs, fetch_stats = fetch_helius_pool_transactions(rpc, pool, config, pool_state, phase="deep")
    except Exception as exc:
        if not config.get("helius_transactions_fallback_signatures", True):
            return [], {"pool": pool.as_dict(), "error": str(exc), "trade_source": "enhanced_transactions"}
        return scan_pool_signatures(rpc, pool, config, state, classification_budget, fallback_error=str(exc))

    if not preclassified:
        swaps, parse_errors = parse_helius_swaps(txs, pool)
        if classify_wallets:
            classified_events, candidate_buys, classification_errors = classify_buy_swaps(
                rpc,
                swaps,
                config,
                state,
                classification_budget,
            )
            events = merge_events(
                seed_events,
                classified_events,
                dedupe_wallets=bool(config.get("helius_dedupe_classification_wallets", True)),
            )
        else:
            candidate_buys = len(buy_swap_candidates(swaps, config))
            classification_errors = 0
            events = []
    wave_swaps = merge_reactivation_wave_swaps(pool_state, swaps, config)
    sticky_swaps = merge_sticky_accumulation_swaps(pool_state, swaps, config)
    fetch_stats["parse_errors"] = parse_errors
    fetch_stats["wave_buffer_truncated"] = bool(
        pool_state.get("reactivation_wave_buffer_truncated")
        or pool_state.get("sticky_accumulation_buffer_truncated")
    )
    classic_alerts = build_alerts(pool, events, config) if config.get("classic_alerts_enabled", True) else []
    wave_alerts = build_reactivation_wave_alerts(
        pool,
        wave_swaps,
        config,
        rpc,
        state,
    )
    sticky_alerts = build_sticky_accumulation_alerts(pool, sticky_swaps, config, rpc)
    alerts = dedupe_pool_alerts([*classic_alerts, *wave_alerts, *sticky_alerts])
    alerts = apply_alert_data_quality(
        alerts,
        fetch_stats,
        candidate_buys,
        len(events),
        classification_errors,
        config,
    )
    if parse_errors:
        pool_state["force_enhanced_next_scan"] = True
    else:
        live_checkpoint = fetch_stats.get("live_checkpoint")
        if live_checkpoint:
            update_pool_transaction_state(pool_state, pool, txs, checkpoint=live_checkpoint)
            pool_state.pop("force_enhanced_next_scan", None)
        elif not fetch_stats.get("live_truncated"):
            update_pool_transaction_state(pool_state, pool, txs)
            pool_state.pop("force_enhanced_next_scan", None)
    signal_thesis = refresh_signal_thesis(
        rpc,
        pool,
        pool_state,
        alerts,
        config,
    )
    return alerts, {
        "pool": pool.as_dict(),
        "lane": config.get("lane") or config.get("mode"),
        "trade_source": "enhanced_transactions",
        "new_signatures": len(txs),
        "transactions_scanned": len(txs),
        "parsed_swaps": len(swaps),
        "candidate_buys": candidate_buys,
        "classified_buys": len(events),
        "classes": dict(Counter(event.get("wallet_class") for event in events)),
        "buy_sol": sum(event.get("sol_amount", 0.0) for event in events),
        "wave_alerts": len(wave_alerts) + len(sticky_alerts),
        "wave_net_buy_sol": sum((alert.get("wave") or {}).get("net_buy_sol", 0.0) for alert in [*wave_alerts, *sticky_alerts]),
        "wave_sticky_supply_pct": max(
            [(alert.get("wave") or {}).get("sticky_supply_pct", 0.0) for alert in [*wave_alerts, *sticky_alerts]] or [0.0]
        ),
        "parse_errors": parse_errors,
        "classification_errors": classification_errors,
        "signal_thesis": signal_thesis,
        "trade_fetch": fetch_stats,
    }


def scan_pool_signatures(rpc, pool, config, state, classification_budget, fallback_error=None):
    pool_state = state.setdefault("pools", {}).setdefault(pool.pool_address, {})
    previous_latest = pool_state.get("latest_signature")
    limit = (
        int(config.get("helius_standard_incremental_signature_limit", 100))
        if previous_latest
        else int(config["initial_backfill_signatures"])
    )

    try:
        signatures = rpc.signatures_for_address(pool.pool_address, limit=limit)
    except Exception as exc:
        error = str(exc)
        if fallback_error:
            error = f"transactions={fallback_error}; signatures={error}"
        return [], {
            "pool": pool.as_dict(),
            "error": error,
            "fallback_error": fallback_error,
            "trade_source": "pool_signatures",
        }

    market_head = None
    if market_activity_requires_head_check(pool, config):
        market_head = market_activity_head_probe(
            pool,
            signatures,
            config,
        )
        mark_market_activity_head_state(
            pool_state,
            pool,
            market_head,
            config,
        )
        if market_head.get("status") == "stale":
            signal_thesis = refresh_signal_thesis(
                rpc,
                pool,
                pool_state,
                [],
                config,
            )
            fetch_stats = {
                "source": "pool_signatures",
                "phase": "market_activity_check",
                "pages": 1,
                "transactions_requested": 0,
                "transactions": 0,
                "truncated": False,
                "live_truncated": False,
                "history_gap_seconds": 0,
                "market_activity_probe": market_head,
                "market_activity_stale": True,
            }
            return [], {
                "pool": pool.as_dict(),
                "lane": config.get("lane") or config.get("mode"),
                "trade_source": "pool_signatures",
                "new_signatures": 0,
                "transactions_scanned": 0,
                "parsed_swaps": 0,
                "candidate_buys": 0,
                "classified_buys": 0,
                "classes": {},
                "buy_sol": 0.0,
                "wave_alerts": 0,
                "wave_net_buy_sol": 0.0,
                "wave_sticky_supply_pct": 0.0,
                "classification_errors": 0,
                "parse_errors": 0,
                "signal_thesis": signal_thesis,
                "market_snapshot_suppressed": True,
                "trade_fetch": fetch_stats,
            }

    new_signatures = []
    previous_found = not previous_latest
    for item in signatures:
        if previous_latest and item["signature"] == previous_latest:
            previous_found = True
            break
        if item.get("err"):
            continue
        new_signatures.append(item["signature"])
    live_truncated = bool(previous_latest and not previous_found and len(signatures) >= limit)
    swaps = []
    transaction_errors = 0
    parse_errors = 0
    for signature in reversed(new_signatures):
        try:
            tx = rpc.transaction(signature)
        except Exception as exc:
            transaction_errors += 1
            print(
                f"warn: transaction detail failed for {signature}: {exc}",
                file=sys.stderr,
            )
            continue
        try:
            swap = parse_pool_swap(tx, pool)
        except Exception:
            parse_errors += 1
            continue
        if swap:
            swaps.append(swap)

    classify_wallets = bool(config.get("classic_alerts_enabled", True))
    if classify_wallets:
        events, candidate_buys, classification_errors = classify_buy_swaps(
            rpc,
            swaps,
            config,
            state,
            classification_budget,
        )
    else:
        events = []
        candidate_buys = len(buy_swap_candidates(swaps, config))
        classification_errors = 0

    wave_swaps = merge_reactivation_wave_swaps(pool_state, swaps, config)
    sticky_swaps = merge_sticky_accumulation_swaps(pool_state, swaps, config)
    classic_alerts = build_alerts(pool, events, config) if classify_wallets else []
    wave_alerts = build_reactivation_wave_alerts(
        pool,
        wave_swaps,
        config,
        rpc,
        state,
    )
    sticky_alerts = build_sticky_accumulation_alerts(pool, sticky_swaps, config, rpc)
    fetch_stats = {
        "source": "pool_signatures",
        "phase": "standard_incremental",
        "pages": 1,
        "transactions_requested": len(new_signatures),
        "transactions": max(0, len(new_signatures) - transaction_errors),
        "truncated": live_truncated,
        "live_truncated": live_truncated,
        "transaction_errors": transaction_errors,
        "parse_errors": parse_errors,
        "wave_buffer_truncated": bool(
            pool_state.get("reactivation_wave_buffer_truncated")
            or pool_state.get("sticky_accumulation_buffer_truncated")
        ),
        "history_gap_seconds": 0,
    }
    if market_head:
        fetch_stats["market_activity_probe"] = market_head
    alerts = dedupe_pool_alerts([*classic_alerts, *wave_alerts, *sticky_alerts])
    alerts = apply_alert_data_quality(
        alerts,
        fetch_stats,
        candidate_buys,
        len(events),
        classification_errors,
        config,
    )

    complete_window = not live_truncated and not transaction_errors and not parse_errors
    latest_successful_item = next((item for item in signatures if not item.get("err")), None)
    if latest_successful_item and complete_window:
        pool_state["latest_signature"] = latest_successful_item["signature"]
        pool_state["rpc_latest_signature"] = latest_successful_item["signature"]
        pool_state["helius_latest_signature"] = latest_successful_item["signature"]
        pool_state["latest_time"] = iso(latest_successful_item.get("blockTime"))
        pool_state["rpc_latest_time"] = iso(latest_successful_item.get("blockTime"))
        pool_state["rpc_latest_block_time"] = int(latest_successful_item.get("blockTime") or 0)
        pool_state["helius_latest_time"] = iso(latest_successful_item.get("blockTime"))
        pool_state["helius_latest_block_time"] = int(latest_successful_item.get("blockTime") or 0)
        pool_state["symbol"] = pool.symbol
        pool_state.pop("force_enhanced_next_scan", None)
        if isinstance(fallback_error, str) and "enhanced transaction head lagged" in fallback_error.lower():
            for key in (
                "helius_live_cursor",
                "helius_live_cursor_complete",
                "helius_live_pending_signature",
                "helius_live_pending_block_time",
                "helius_live_from",
                "helius_live_cursor_created_at",
                "helius_rolling_backlogs",
            ):
                pool_state.pop(key, None)
    elif live_truncated or transaction_errors or parse_errors:
        pool_state["force_enhanced_next_scan"] = True

    signal_thesis = refresh_signal_thesis(
        rpc,
        pool,
        pool_state,
        alerts,
        config,
    )

    summary = {
        "pool": pool.as_dict(),
        "lane": config.get("lane") or config.get("mode"),
        "trade_source": "pool_signatures",
        "new_signatures": len(new_signatures),
        "transactions_scanned": max(0, len(new_signatures) - transaction_errors),
        "parsed_swaps": len(swaps),
        "candidate_buys": candidate_buys,
        "classified_buys": len(events),
        "classes": dict(Counter(event.get("wallet_class") for event in events)),
        "buy_sol": sum(event.get("sol_amount", 0.0) for event in events),
        "wave_alerts": len(wave_alerts) + len(sticky_alerts),
        "wave_net_buy_sol": sum(
            (alert.get("wave") or {}).get("net_buy_sol", 0.0)
            for alert in [*wave_alerts, *sticky_alerts]
        ),
        "wave_sticky_supply_pct": max(
            [(alert.get("wave") or {}).get("sticky_supply_pct", 0.0) for alert in [*wave_alerts, *sticky_alerts]]
            or [0.0]
        ),
        "classification_errors": classification_errors,
        "parse_errors": parse_errors,
        "signal_thesis": signal_thesis,
        "trade_fetch": fetch_stats,
    }
    if fallback_error:
        summary["fallback_error"] = fallback_error
    return alerts, summary


def scan_pool(rpc, pool, config, state, classification_budget):
    if config.get("helius_transactions_enabled", True):
        pool_state = state.setdefault("pools", {}).setdefault(pool.pool_address, {})
        standard_threshold = int(config.get("helius_standard_incremental_max_txns_1h", 30))
        if (
            config.get("helius_standard_incremental_enabled", True)
            and pool_state.get("latest_signature")
            and not pool_state.get("force_enhanced_next_scan")
            and int(pool.txns_1h or 0) <= standard_threshold
        ):
            return scan_pool_signatures(rpc, pool, config, state, classification_budget)
        return scan_pool_helius_transactions(rpc, pool, config, state, classification_budget)
    return scan_pool_signatures(rpc, pool, config, state, classification_budget)


def alert_history_key(alert):
    pool = alert.get("pool") or {}
    return ":".join(
        str(part)
        for part in (
            pool.get("pool_address") or pool.get("token_address") or "pool",
            alert.get("signal_family") or "classified_wallets",
            alert.get("window_start") or alert.get("created_at") or "",
            alert.get("window_end") or "",
        )
    )


def alert_history_token_key(alert):
    pool = alert.get("pool") or {}
    return clean_deleted_id(pool.get("token_address") or pool.get("pool_address"))


def alert_history_timestamp(alert):
    return max(
        parse_timestamp(alert.get("created_at")),
        parse_timestamp(alert.get("window_start")),
        parse_timestamp(alert.get("obs_mcap_at")),
    )


def alert_history_sort_key(alert):
    return (
        alert_history_timestamp(alert),
        int(alert.get("score") or 0),
        float(alert.get("suspicious_sol") or 0),
    )


def load_alert_history(path=ALERTS_PATH):
    if not path.exists():
        return []
    alerts = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            alert = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(alert, dict):
            alerts.append(alert)
    return alerts


def alert_is_deleted(alert, deleted):
    pool = alert.get("pool") or {}
    token = clean_deleted_id(pool.get("token_address"))
    pool_address = clean_deleted_id(pool.get("pool_address"))
    return bool(
        (token and token in deleted.get("tokens", set()))
        or (pool_address and pool_address in deleted.get("pools", set()))
    )


def keep_strong_late_reactivation(alert, config):
    if alert.get("action_tier") != "late_chase":
        return False
    if alert.get("lane") != "reactivation":
        return False
    if alert.get("signal_family") != "reactivation_wave":
        return False
    if (alert.get("data_quality") or {}).get("status") == "partial":
        return False
    pool = alert.get("pool") or {}
    mcap = float(alert.get("obs_mcap_usd") or pool.get("mcap_usd") or 0)
    return bool(
        int(alert.get("score") or 0)
        >= int(config.get("alert_history_keep_late_reactivation_min_score", 75))
        and 0
        < mcap
        <= float(
            config.get(
                "alert_history_keep_late_reactivation_max_mcap_usd",
                250_000,
            )
        )
    )


def compact_alert_history(existing_alerts, new_alerts, config):
    keep_tiers = set(
        config.get("alert_history_keep_tiers")
        or ["actionable", "hot_reactivation", "watch"]
    )
    retention_hours = float(config.get("alert_history_retention_hours", 168))
    max_tokens = int(config.get("alert_history_max_tokens", 40))
    max_alerts = int(config.get("alert_history_max_alerts", 160))
    max_per_token = int(config.get("alert_history_max_alerts_per_token", 3))
    keep_missing_tier = bool(config.get("alert_history_keep_missing_tier", False))
    cutoff = int(time.time() - retention_hours * 3600) if retention_hours > 0 else 0
    deleted = load_deleted_tokens()
    active_lanes = set(selected_lanes(config, "all")) if config.get("lanes") else set()
    by_id = {}

    for alert in [*(existing_alerts or []), *(new_alerts or [])]:
        if not isinstance(alert, dict):
            continue
        if active_lanes and alert.get("lane") not in active_lanes:
            continue
        if alert_is_deleted(alert, deleted):
            continue
        token = alert_history_token_key(alert)
        if not token:
            continue
        timestamp = alert_history_timestamp(alert)
        if cutoff and timestamp and timestamp < cutoff:
            continue
        tier = alert.get("action_tier")
        if not tier and not keep_missing_tier:
            continue
        if (
            tier
            and keep_tiers
            and tier not in keep_tiers
            and not keep_strong_late_reactivation(alert, config)
        ):
            continue
        key = alert_history_key(alert)
        existing = by_id.get(key)
        if not existing or alert_history_sort_key(alert) >= alert_history_sort_key(existing):
            by_id[key] = alert

    by_token = defaultdict(list)
    for alert in by_id.values():
        by_token[alert_history_token_key(alert)].append(alert)

    token_rank = []
    for token, token_alerts in by_token.items():
        by_family = defaultdict(list)
        for alert in token_alerts:
            by_family[alert.get("signal_family") or "classified_wallets"].append(alert)
        kept = []
        for family_alerts in by_family.values():
            latest = max(family_alerts, key=alert_history_sort_key)
            kept.append(latest)
        ranked_alerts = sorted(token_alerts, key=alert_history_sort_key, reverse=True)
        for alert in ranked_alerts:
            if max_per_token > 0 and len(kept) >= max_per_token:
                break
            if alert not in kept:
                kept.append(alert)
        if max_per_token > 0 and len(kept) > max_per_token:
            kept.sort(key=alert_history_sort_key, reverse=True)
            kept = kept[:max_per_token]
        by_token[token] = kept
        token_rank.append(
            (
                token,
                max(alert_history_sort_key(alert) for alert in kept),
                max(int(alert.get("score") or 0) for alert in kept),
            )
        )

    token_rank.sort(key=lambda item: (item[2], item[1]), reverse=True)
    allowed_tokens = {token for token, _, _ in token_rank[:max_tokens]} if max_tokens > 0 else set(by_token)
    compacted = []
    for token in allowed_tokens:
        compacted.extend(by_token.get(token, []))
    compacted.sort(key=alert_history_sort_key)
    if max_alerts > 0 and len(compacted) > max_alerts:
        compacted = compacted[-max_alerts:]
    return compacted


def write_alerts(alerts, config):
    history = compact_alert_history(load_alert_history(), alerts, config)
    write_jsonl(ALERTS_PATH, history)


def recent_alert_token_addresses(limit=250, lanes=None):
    if not ALERTS_PATH.exists():
        return []
    tokens = []
    for line in ALERTS_PATH.read_text().splitlines()[-limit:]:
        if not line.strip():
            continue
        try:
            alert = json.loads(line)
        except json.JSONDecodeError:
            continue
        if lanes and alert.get("lane") not in lanes:
            continue
        token = (alert.get("pool") or {}).get("token_address")
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def caught_market_token_addresses(state, alerts=None, limit=80, lanes=None):
    deleted = load_deleted_tokens()
    priority_candidates = []
    candidates = []

    def add(token, timestamp, priority=False):
        token = clean_solana_address(token)
        if not token or token in deleted.get("tokens", set()):
            return
        (priority_candidates if priority else candidates).append(
            (parse_timestamp(timestamp), token)
        )

    for pool_state in (state.get("pools") or {}).values():
        if not isinstance(pool_state, dict):
            continue
        thesis = pool_state.get("signal_thesis")
        if not isinstance(thesis, dict):
            continue
        if thesis.get("status") not in {"intact", "unknown", "weakening"}:
            continue
        add(
            thesis.get("token_address") or pool_state.get("token_address"),
            thesis.get("updated_at") or thesis.get("last_checked_at") or thesis.get("signal_at"),
            priority=True,
        )

    for alert in load_alert_history():
        if lanes and alert.get("lane") not in lanes:
            continue
        pool = alert.get("pool") or {}
        if pool.get("pool_address") in deleted.get("pools", set()):
            continue
        add(pool.get("token_address"), alert_history_timestamp(alert))

    for alert in alerts or []:
        if lanes and alert.get("lane") not in lanes:
            continue
        pool = alert.get("pool") or {}
        if pool.get("pool_address") in deleted.get("pools", set()):
            continue
        add(pool.get("token_address"), alert_history_timestamp(alert))

    for token, entry in (state.get("market") or {}).items():
        if not isinstance(entry, dict) or not entry.get("first_signal_at"):
            continue
        if lanes and entry.get("first_obs_lane") not in lanes:
            continue
        add(entry.get("token_address") or token, entry.get("first_signal_at"))

    ordered = []
    seen = set()
    for _, token in [*sorted(priority_candidates, reverse=True), *sorted(candidates, reverse=True)]:
        if token in seen:
            continue
        seen.add(token)
        ordered.append(token)
        if limit > 0 and len(ordered) >= limit:
            break
    return ordered


def fetch_gmgn_kline(config, token_address, resolution, from_timestamp, to_timestamp):
    data = run_gmgn_cli(
        config,
        [
            "market",
            "kline",
            "--chain",
            "sol",
            "--address",
            token_address,
            "--resolution",
            resolution,
            "--from",
            str(int(from_timestamp)),
            "--to",
            str(int(to_timestamp)),
        ],
        f"gmgn kline {resolution} {token_address}",
    )
    rows = data.get("list") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else []


def gmgn_peak_candle(rows):
    candidates = [
        row
        for row in rows
        if isinstance(row, dict) and parse_timestamp(row.get("time"))
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (to_float(row.get("high")), -parse_timestamp(row.get("time"))),
    )


def fetch_gmgn_ath_timestamp(config, token_address, creation_timestamp=0):
    now = int(time.time())
    start = parse_timestamp(creation_timestamp) or now - 1_000 * 24 * 60 * 60
    day = gmgn_peak_candle(
        fetch_gmgn_kline(config, token_address, "1d", start, now + 60)
    )
    if not day:
        return 0
    day_start = parse_timestamp(day.get("time"))
    hour = gmgn_peak_candle(
        fetch_gmgn_kline(
            config,
            token_address,
            "1h",
            max(start, day_start - 60),
            min(now + 60, day_start + 24 * 60 * 60 + 60),
        )
    )
    if not hour:
        return day_start
    hour_start = parse_timestamp(hour.get("time"))
    five_minute = gmgn_peak_candle(
        fetch_gmgn_kline(
            config,
            token_address,
            "5m",
            max(start, hour_start - 60),
            min(now + 60, hour_start + 60 * 60 + 60),
        )
    )
    return parse_timestamp((five_minute or hour).get("time"))


def fetch_gmgn_ath(config, token_address, include_timestamp=True):
    result_cache = config.setdefault("_gmgn_ath_result_cache", {})
    cached = result_cache.get(token_address)
    if cached and (not include_timestamp or cached.get("timestamp")):
        return dict(cached)
    data = fetch_gmgn_raw_token_info(config, token_address)
    if not data:
        return None
    dev = data.get("dev") or {}
    ath_token_info = dev.get("ath_token_info") or {}
    ath_price = to_float(data.get("ath_price"))
    ath_mcap = to_float(ath_token_info.get("ath_mc"))
    supply = to_float(
        data.get("circulating_supply")
        or data.get("total_supply")
        or data.get("max_supply")
    )
    if ath_mcap <= 0 and ath_price > 0 and supply > 0:
        ath_mcap = ath_price * supply
    if ath_mcap <= 0:
        return None
    ath = {
        "highest_market_cap": ath_mcap,
        "highest_price": ath_price,
        "supply": supply,
        "pool_id": data.get("biggest_pool_address") or data.get("migrated_pool"),
    }
    if include_timestamp:
        try:
            ath["timestamp"] = fetch_gmgn_ath_timestamp(
                config,
                token_address,
                data.get("creation_timestamp"),
            )
        except Exception as exc:
            ath["timestamp_error"] = str(exc)
    result_cache[token_address] = dict(ath)
    return ath


def validate_ath_candidate(entry, ath, current_mcap=0, config=None):
    config = config or {}
    ath_mcap = to_float(ath.get("highest_market_cap"))
    if not math.isfinite(ath_mcap) or ath_mcap <= 0:
        return False, "ATH market cap is not a positive finite number"

    max_ath_usd = float(config.get("ath_validation_max_usd", 100_000_000_000))
    if max_ath_usd > 0 and ath_mcap > max_ath_usd:
        return False, f"ATH market cap exceeds ${max_ath_usd:.0f} plausibility limit"

    current = max(
        float(current_mcap or 0),
        to_float(entry.get("latest_mcap_usd")),
        to_float(entry.get("scan_mcap_usd")),
        to_float(entry.get("first_obs_mcap_usd")),
    )
    min_current_ratio = float(config.get("ath_validation_min_current_ratio", 0.90))
    if current > 0 and ath_mcap < current * min_current_ratio:
        return False, "ATH market cap is below the observed current/local high"

    max_current_multiple = float(
        config.get("ath_validation_max_current_multiple", 10_000)
    )
    if (
        current > 0
        and max_current_multiple > 0
        and ath_mcap / current > max_current_multiple
    ):
        return False, "ATH/current market-cap multiple is implausibly large"

    ath_price = to_float(ath.get("highest_price"))
    supply = to_float(ath.get("supply"))
    derived_mcap = ath_price * supply if ath_price > 0 and supply > 0 else 0.0
    max_derived_ratio = float(config.get("ath_validation_max_derived_ratio", 5))
    if derived_mcap > 0 and max_derived_ratio > 1:
        ratio = max(ath_mcap, derived_mcap) / max(1.0, min(ath_mcap, derived_mcap))
        if ratio > max_derived_ratio:
            return False, "ATH market cap conflicts with ATH price multiplied by supply"
    return True, None


def apply_gmgn_ath(entry, ath, observed_at, current_mcap=0, config=None):
    previous_source = entry.get("ath_source")
    valid, validation_error = validate_ath_candidate(
        entry,
        ath,
        current_mcap=current_mcap,
        config=config,
    )
    if not valid:
        entry["ath_candidate_mcap_usd"] = to_float(ath.get("highest_market_cap"))
        entry["ath_candidate_price_usd"] = to_float(ath.get("highest_price"))
        entry["ath_candidate_source"] = "gmgn"
        entry["ath_candidate_status"] = "suspect"
        entry["ath_candidate_error"] = validation_error
        entry["ath_latest_checked_at"] = observed_at
        if not trusted_ath_mcap(entry):
            entry["ath_source"] = "gmgn"
            entry["ath_status"] = "suspect"
        return False

    if ath.get("highest_market_cap") is not None:
        entry["ath_mcap_usd"] = to_float(ath.get("highest_market_cap"))
    if ath.get("highest_price") is not None:
        entry["ath_price_usd"] = to_float(ath.get("highest_price"))
    if ath.get("timestamp"):
        timestamp = int(ath["timestamp"])
        if timestamp > 10_000_000_000:
            timestamp = timestamp // 1000
        entry["ath_mcap_at"] = iso(timestamp)
    elif previous_source != "gmgn":
        entry.pop("ath_mcap_at", None)
    if ath.get("pool_id"):
        entry["ath_pool_address"] = ath.get("pool_id")
    entry["ath_source"] = "gmgn"
    entry["ath_status"] = "ready" if entry.get("ath_mcap_at") else "partial"
    entry["ath_validation_status"] = "valid"
    entry["ath_latest_checked_at"] = observed_at
    entry["ath_verified_at"] = observed_at
    if ath.get("timestamp_error"):
        entry["ath_timestamp_error"] = ath.get("timestamp_error")
    else:
        entry.pop("ath_timestamp_error", None)
    entry.pop("ath_error", None)
    entry.pop("ath_error_checked_at", None)
    entry.pop("ath_error_source", None)
    for key in (
        "ath_candidate_mcap_usd",
        "ath_candidate_price_usd",
        "ath_candidate_source",
        "ath_candidate_status",
        "ath_candidate_error",
    ):
        entry.pop(key, None)
    return True


def trusted_ath_mcap(entry):
    if not entry:
        return 0.0
    if entry.get("ath_source") not in ("gmgn", "solana_tracker", "ohlcv_high"):
        return 0.0
    if entry.get("ath_status") == "suspect":
        return 0.0
    value = to_float(entry.get("ath_mcap_usd"))
    if not math.isfinite(value) or value <= 0 or value > 100_000_000_000:
        return 0.0
    current = max(
        to_float(entry.get("latest_mcap_usd")),
        to_float(entry.get("scan_mcap_usd")),
        to_float(entry.get("first_obs_mcap_usd")),
    )
    if current > 0 and (value < current * 0.90 or value / current > 10_000):
        return 0.0
    return value


def filter_reactivation_by_ath(http, state, pools, config, observed_at):
    max_ratio = config.get("ath_max_current_ratio")
    if config.get("lane") != "reactivation":
        return pools
    if max_ratio is None:
        market = state.get("market") if isinstance(state, dict) else {}
        preload_limit = max(
            0,
            int(config.get("reactivation_ath_preload_max_tokens_per_scan", 16)),
        )
        api_key = os.environ.get("GMGN_API_KEY")
        fetched = 0
        stats = Counter()
        for pool in pools:
            token = pool.token_address or pool.pool_address
            entry = market.get(token) if isinstance(market, dict) else None
            ath_mcap = trusted_ath_mcap(entry)
            if (
                not ath_mcap
                and api_key
                and fetched < preload_limit
                and isinstance(market, dict)
            ):
                entry = market.setdefault(token, {"token_address": token})
                try:
                    ath = fetch_gmgn_ath(config, token, include_timestamp=False)
                    fetched += 1
                    if ath and not ath.get("error"):
                        apply_gmgn_ath(
                            entry,
                            ath,
                            observed_at,
                            current_mcap=pool.mcap_usd,
                            config=config,
                        )
                        ath_mcap = trusted_ath_mcap(entry)
                    else:
                        entry["ath_status"] = "error"
                        entry["ath_error"] = (
                            (ath or {}).get("error") or "empty_ath_response"
                        )
                        entry["ath_error_checked_at"] = int(time.time())
                        entry["ath_error_source"] = "gmgn"
                    time.sleep(float(config.get("ath_request_delay_seconds", 0.25)))
                except Exception as exc:
                    fetched += 1
                    entry["ath_status"] = "error"
                    entry["ath_error"] = str(exc)[:500]
                    entry["ath_error_checked_at"] = int(time.time())
                    entry["ath_error_source"] = "gmgn"
            if not ath_mcap or pool.mcap_usd <= 0:
                stats["missing_or_suspect_ath"] += 1
                continue
            pool.ath_mcap_usd = ath_mcap
            pool.ath_current_ratio = pool.mcap_usd / ath_mcap
            stats["trusted_ath"] += 1
        stats["input_pools"] = len(pools)
        stats["kept_pools"] = len(pools)
        stats["ath_fetches"] = fetched
        config["_ath_filter_stats"] = dict(stats)
        return pools

    max_ratio = float(max_ratio)
    market = state.setdefault("market", {})
    now = int(time.time())
    error_ttl = int(config.get("ath_error_cache_ttl_minutes", 20)) * 60
    delay = float(config.get("ath_request_delay_seconds", 0.25))
    fetch_limit = int(config.get("ath_filter_max_tokens_per_scan", config.get("ath_max_tokens_per_scan", 25)))
    require_trusted = bool(config.get("ath_require_trusted", True))
    api_key = os.environ.get("GMGN_API_KEY")
    fetched = 0
    rate_limited = False
    kept = []
    unverified = []
    stats = Counter()

    for pool in pools:
        token = pool.token_address or pool.pool_address
        if not token or pool.mcap_usd <= 0:
            stats["missing_token_or_mcap"] += 1
            continue
        entry = market.setdefault(token, {"token_address": token})
        ath_mcap = trusted_ath_mcap(entry)

        if not ath_mcap and api_key and not rate_limited:
            recent_error = (
                entry.get("ath_error_source") == "gmgn"
                and entry.get("ath_error_checked_at")
                and now - int(entry.get("ath_error_checked_at", 0)) < error_ttl
            )
            if not recent_error and fetched < fetch_limit:
                try:
                    ath = fetch_gmgn_ath(config, token, include_timestamp=False)
                    fetched += 1
                    if ath and not ath.get("error"):
                        entry["ath_checked_at"] = now
                        apply_gmgn_ath(
                            entry,
                            ath,
                            observed_at,
                            current_mcap=pool.mcap_usd,
                            config=config,
                        )
                        ath_mcap = trusted_ath_mcap(entry)
                    elif ath and ath.get("error"):
                        entry["ath_error"] = ath.get("error")
                        entry["ath_error_checked_at"] = now
                        entry["ath_error_source"] = "gmgn"
                        entry["ath_status"] = "error"
                    else:
                        entry["ath_error"] = "empty_ath_response"
                        entry["ath_error_checked_at"] = now
                        entry["ath_error_source"] = "gmgn"
                        entry["ath_status"] = "error"
                    time.sleep(delay)
                except Exception as exc:
                    message = str(exc)
                    print(f"warn: reactivation ath filter failed for {token}: {message}", file=sys.stderr)
                    entry["ath_error"] = message
                    entry["ath_error_checked_at"] = now
                    entry["ath_error_source"] = "gmgn"
                    entry["ath_status"] = "error"
                    fetched += 1
                    if "429" in message or "Too Many" in message:
                        rate_limited = True

        if not ath_mcap:
            if require_trusted:
                stats["missing_ath"] += 1
                if rate_limited or not api_key or fetched >= fetch_limit:
                    entry["ath_status"] = "unverified"
                    entry["ath_error"] = entry.get("ath_error") or "ath_unavailable_for_reactivation_filter"
                    entry["ath_error_checked_at"] = now
                    entry["ath_error_source"] = "gmgn"
                    unverified.append(pool)
                continue
            kept.append(pool)
            stats["kept_without_ath"] += 1
            continue

        ratio = pool.mcap_usd / ath_mcap
        pool.ath_mcap_usd = ath_mcap
        pool.ath_current_ratio = ratio
        entry["ath_current_ratio"] = ratio
        entry["ath_drawdown_pct"] = max(0.0, (1 - ratio) * 100)
        entry["ath_filter_checked_at"] = observed_at
        if ratio <= max_ratio:
            kept.append(pool)
            stats["kept_corrected"] += 1
        else:
            stats["too_close_to_ath"] += 1

    unknown_limit = int(config.get("ath_unknown_reactivation_scan_limit", 0) or 0)
    if unknown_limit and unverified:
        unverified.sort(key=lambda item: (item.volume_1h_usd, item.liquidity_usd), reverse=True)
        selected = unverified[:unknown_limit]
        kept.extend(selected)
        stats["kept_unverified_ath"] = len(selected)

    stats["input_pools"] = len(pools)
    stats["kept_pools"] = len(kept)
    stats["ath_fetches"] = fetched
    if rate_limited:
        stats["rate_limited"] = 1
    config["_ath_filter_stats"] = dict(stats)
    return kept


def enrich_market_ath(http, state, pools, alerts, config, observed_at):
    if not config.get("ath_enabled", True):
        return
    market = state.setdefault("market", {})
    provider_stats = {
        "checked_at": observed_at,
        "status": "ok",
        "fetched": 0,
    }
    pool_tokens = [pool.token_address for pool in pools if pool.token_address]
    alert_tokens = [(alert.get("pool") or {}).get("token_address") for alert in alerts]
    active_lanes = set(selected_lanes(config, "all")) if config.get("lanes") else None
    recent_tokens = recent_alert_token_addresses(
        int(config.get("ath_recent_alert_limit", 100)),
        lanes=active_lanes,
    )
    priority_tokens = []
    for token in alert_tokens:
        if token and token not in priority_tokens:
            priority_tokens.append(token)
    required_tokens = []
    for token in [*priority_tokens, *recent_tokens]:
        if token and token not in required_tokens:
            required_tokens.append(token)
    candidates = []
    for token in [*required_tokens, *pool_tokens]:
        if token and token not in candidates:
            candidates.append(token)
    max_tokens = int(config.get("ath_max_tokens_per_scan", 25))
    ttl = int(config.get("ath_cache_ttl_minutes", 360)) * 60
    error_ttl = int(config.get("ath_error_cache_ttl_minutes", 20)) * 60
    delay = float(config.get("ath_request_delay_seconds", 0.25))
    now = int(time.time())
    fetched = 0
    priority_fetched = 0
    broad_fetched = 0
    priority_set = set(priority_tokens)
    if not os.environ.get("GMGN_API_KEY"):
        for token in required_tokens:
            entry = market.setdefault(token, {"token_address": token})
            entry["ath_status"] = "missing_api_key"
            entry["ath_error"] = "missing_gmgn_api_key"
            entry["ath_error_checked_at"] = now
            entry["ath_error_source"] = "gmgn"
        provider_stats.update({"status": "missing_api_key", "error": "missing_gmgn_api_key"})
        state.setdefault("maintenance", {})["gmgn_ath"] = provider_stats
        return
    for token in candidates:
        entry = market.setdefault(token, {"token_address": token})
        has_gmgn_ath = entry.get("ath_source") == "gmgn" and entry.get("ath_mcap_usd")
        if has_gmgn_ath and not entry.get("ath_status"):
            entry["ath_status"] = "ready"
        if (
            has_gmgn_ath
            and entry.get("ath_mcap_at")
            and entry.get("ath_checked_at")
            and now - int(entry.get("ath_checked_at", 0)) < ttl
        ):
            continue
        if (
            entry.get("ath_error_source") == "gmgn"
            and entry.get("ath_error_checked_at")
            and now - int(entry.get("ath_error_checked_at", 0)) < error_ttl
        ):
            continue
        is_priority = token in priority_set
        if not is_priority and broad_fetched >= max_tokens:
            continue
        try:
            ath = fetch_gmgn_ath(config, token, include_timestamp=True)
        except Exception as exc:
            message = str(exc)
            print(f"warn: gmgn ath failed for {token}: {message}", file=sys.stderr)
            entry["ath_error"] = message
            entry["ath_error_checked_at"] = now
            entry["ath_error_source"] = "gmgn"
            entry["ath_status"] = "error"
            fetched += 1
            if is_priority:
                priority_fetched += 1
            else:
                broad_fetched += 1
            if any(marker in message for marker in ("401", "403", "Forbidden", "Unauthorized")):
                config["_gmgn_error"] = message
                provider_stats.update({"status": "auth_failed", "error": message})
                break
            if "429" in message or "Too Many" in message:
                config["_gmgn_error"] = message
                provider_stats.update({"status": "rate_limited", "error": message})
                break
            continue
        fetched += 1
        if is_priority:
            priority_fetched += 1
        else:
            broad_fetched += 1
        if ath and not ath.get("error"):
            entry["ath_checked_at"] = now
            apply_gmgn_ath(
                entry,
                ath,
                observed_at,
                current_mcap=(
                    0.0
                    if entry.get("market_snapshot_stale")
                    else to_float(entry.get("latest_mcap_usd"))
                ),
                config=config,
            )
        elif ath and ath.get("error"):
            entry["ath_error"] = ath.get("error")
            entry["ath_error_checked_at"] = now
            entry["ath_error_source"] = "gmgn"
            entry["ath_status"] = "error"
        else:
            entry["ath_error"] = "empty_ath_response"
            entry["ath_error_checked_at"] = now
            entry["ath_error_source"] = "gmgn"
            entry["ath_status"] = "error"
        time.sleep(delay)
    provider_stats["fetched"] = fetched
    provider_stats["priority_fetched"] = priority_fetched
    provider_stats["broad_fetched"] = broad_fetched
    state.setdefault("maintenance", {})["gmgn_ath"] = provider_stats


def record_market_observations(state, pools, observed_at):
    market = state.setdefault("market", {})
    for pool in pools:
        # A registry-only pool carries the previous snapshot. Do not refresh its
        # TTL unless a live market provider returned it in this run.
        if pool.source == "registry":
            continue
        key = pool.token_address or pool.pool_address
        if not key:
            continue
        entry = market.setdefault(key, {})
        entry.update(
            {
                "token_address": pool.token_address,
                "pool_address": pool.pool_address,
                "symbol": pool.symbol,
                "name": pool.name,
                "dex": pool.dex,
                "url": pool.url,
                "latest_mcap_usd": pool.mcap_usd,
                "latest_price_usd": pool.price_usd,
                "latest_liquidity_usd": pool.liquidity_usd,
                "latest_volume_5m_usd": pool.volume_5m_usd,
                "latest_volume_1h_usd": pool.volume_1h_usd,
                "latest_volume_24h_usd": pool.volume_24h_usd,
                "latest_txns_5m": pool.txns_5m,
                "latest_txns_1h": pool.txns_1h,
                "latest_seen_at": observed_at,
                "market_snapshot_at": observed_at,
                "current_market_verified_at": observed_at,
                "market_snapshot_checked_at": observed_at,
                "market_snapshot_stale": False,
                "market_source": pool.source,
                "scan_mcap_usd": pool.mcap_usd,
                "scan_price_usd": pool.price_usd,
                "scan_liquidity_usd": pool.liquidity_usd,
                "scan_mcap_at": observed_at,
                "scan_source": pool.source or "scanner_snapshot",
            }
        )
        entry.pop("market_snapshot_error", None)
        if pool.pair_created_at and (
            not entry.get("pair_created_at") or pool.pair_created_at < int(entry.get("pair_created_at", 0))
        ):
            entry["pair_created_at"] = pool.pair_created_at
            entry["pair_created_at_iso"] = iso(pool.pair_created_at)


def best_pool_per_token(pools, config):
    by_token = {}
    for pool in pools or []:
        token = pool.token_address
        if not token or not pool_dex_allowed(pool, config):
            continue
        current = by_token.get(token)
        if not current or (pool.liquidity_usd, pool.volume_1h_usd, pool.mcap_usd) > (
            current.liquidity_usd,
            current.volume_1h_usd,
            current.mcap_usd,
        ):
            by_token[token] = pool
    return list(by_token.values())


def refresh_caught_market_observations(http, state, alerts, config, observed_at):
    if not config.get("caught_market_refresh_enabled", True):
        return []

    limit = int(config.get("caught_market_refresh_max_tokens", 80))
    ttl_seconds = int(config.get("caught_market_refresh_ttl_minutes", 50)) * 60
    now = parse_timestamp(observed_at) or int(time.time())
    active_lanes = set(selected_lanes(config, "all")) if config.get("lanes") else None
    tokens = caught_market_token_addresses(
        state,
        alerts,
        limit=limit,
        lanes=active_lanes,
    )
    market = state.setdefault("market", {})
    due_tokens = []
    for token in tokens:
        entry = market.get(token) or {}
        latest_seen = parse_timestamp(entry.get("latest_seen_at"))
        if ttl_seconds > 0 and latest_seen and now - latest_seen < ttl_seconds:
            continue
        due_tokens.append(token)

    stats = {
        "checked_at": observed_at,
        "candidate_tokens": len(tokens),
        "due_tokens": len(due_tokens),
        "refreshed_tokens": 0,
        "source": "dexscreener_caught_market_refresh",
    }
    if not due_tokens:
        state.setdefault("maintenance", {})["caught_market_refresh"] = stats
        return []

    pools = fetch_dex_pairs_for_tokens(http, due_tokens, "caught_market_refresh")
    refreshed = best_pool_per_token(pools.values(), config)
    for pool in refreshed:
        existing = market.setdefault(pool.token_address or pool.pool_address, {})
        if existing.get("first_obs_mcap_usd") and not existing.get("caught_obs_mcap_usd"):
            existing["caught_obs_mcap_usd"] = existing.get("first_obs_mcap_usd")
            existing["caught_obs_price_usd"] = existing.get("first_obs_price_usd")
            existing["caught_obs_liquidity_usd"] = existing.get("first_obs_liquidity_usd")
            existing["caught_obs_mcap_at"] = existing.get("first_obs_mcap_at")
    record_market_observations(state, refreshed, observed_at)
    stats["refreshed_tokens"] = len(refreshed)
    stats["missing_tokens"] = len(due_tokens) - len(refreshed)
    refreshed_tokens = {
        pool.token_address or pool.pool_address
        for pool in refreshed
        if pool.token_address or pool.pool_address
    }
    for token in due_tokens:
        if token in refreshed_tokens:
            continue
        entry = market.setdefault(token, {"token_address": token})
        entry["market_snapshot_stale"] = True
        entry["market_snapshot_error"] = "market_refresh_missing"
        entry["market_snapshot_checked_at"] = observed_at
    state.setdefault("maintenance", {})["caught_market_refresh"] = stats
    if refreshed:
        print(f"caught market refresh: {len(refreshed)}/{len(due_tokens)} tokens", flush=True)
    return refreshed


def record_alert_observations(state, alerts):
    market = state.setdefault("market", {})
    for alert in alerts:
        pool = alert.get("pool") or {}
        key = pool.get("token_address") or pool.get("pool_address")
        if not key:
            continue
        signal_at = alert.get("window_start") or alert.get("created_at")
        obs_at = alert.get("obs_mcap_at") or alert.get("created_at") or signal_at
        entry = market.setdefault(key, {"token_address": pool.get("token_address")})
        existing_at = entry.get("first_signal_at")
        if existing_at and signal_at and parse_timestamp(signal_at) >= parse_timestamp(existing_at):
            continue
        entry.update(
            {
                "first_signal_at": signal_at,
                "first_obs_mcap_usd": alert.get("obs_mcap_usd") or pool.get("mcap_usd"),
                "first_obs_price_usd": alert.get("obs_price_usd") or pool.get("price_usd"),
                "first_obs_liquidity_usd": alert.get("obs_liquidity_usd") or pool.get("liquidity_usd"),
                "first_obs_mcap_at": obs_at,
                "first_obs_source": "first_alert_snapshot",
                "first_obs_lane": alert.get("lane"),
                "first_obs_score": alert.get("score"),
            }
        )


SIGNAL_OUTCOME_HORIZONS = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "72h": 72 * 3600,
    "7d": 7 * 24 * 3600,
}


def outcome_market_snapshot(entry, observed_at):
    if not isinstance(entry, dict):
        return None
    if entry.get("market_snapshot_stale"):
        return None
    at = (
        entry.get("latest_seen_at")
        or entry.get("market_snapshot_at")
        or observed_at
    )
    mcap = to_float(entry.get("latest_mcap_usd"))
    price = to_float(entry.get("latest_price_usd"))
    liquidity = to_float(entry.get("latest_liquidity_usd"))
    if mcap <= 0 and price <= 0:
        return None
    return {
        "at": at,
        "mcap_usd": mcap,
        "price_usd": price,
        "liquidity_usd": liquidity,
    }


def outcome_return_pct(outcome, snapshot):
    caught_mcap = to_float(outcome.get("caught_mcap_usd"))
    current_mcap = to_float(snapshot.get("mcap_usd"))
    if caught_mcap > 0 and current_mcap > 0:
        return (current_mcap / caught_mcap - 1) * 100
    caught_price = to_float(outcome.get("caught_price_usd"))
    current_price = to_float(snapshot.get("price_usd"))
    if caught_price > 0 and current_price > 0:
        return (current_price / caught_price - 1) * 100
    return None


def summarize_signal_outcomes(outcomes):
    rows = list((outcomes or {}).values())

    def metrics(group):
        returns_24h = [
            to_float((row.get("horizons") or {}).get("24h", {}).get("return_pct"))
            for row in group
            if (row.get("horizons") or {}).get("24h", {}).get("return_pct")
            is not None
        ]
        returns_72h = [
            to_float((row.get("horizons") or {}).get("72h", {}).get("return_pct"))
            for row in group
            if (row.get("horizons") or {}).get("72h", {}).get("return_pct")
            is not None
        ]
        return {
            "tracked": len(group),
            "with_24h": len(returns_24h),
            "with_72h": len(returns_72h),
            "median_return_24h_pct": (
                median_value(returns_24h) if returns_24h else None
            ),
            "positive_24h_pct": (
                sum(value > 0 for value in returns_24h)
                / len(returns_24h)
                * 100
                if returns_24h
                else None
            ),
            "loss_30pct_24h_pct": (
                sum(value <= -30 for value in returns_24h)
                / len(returns_24h)
                * 100
                if returns_24h
                else None
            ),
            "double_72h_pct": (
                sum(value >= 100 for value in returns_72h)
                / len(returns_72h)
                * 100
                if returns_72h
                else None
            ),
        }

    summary = {
        "tracked": len(rows),
        "with_1h": 0,
        "with_6h": 0,
        "with_24h": 0,
        "with_72h": 0,
        "median_return_24h_pct": None,
        "positive_24h_pct": None,
        "double_72h_pct": None,
    }
    for horizon in SIGNAL_OUTCOME_HORIZONS:
        summary[f"with_{horizon}"] = sum(
            1
            for row in rows
            if (row.get("horizons") or {}).get(horizon)
        )
    overall = metrics(rows)
    summary.update(
        {
            key: value
            for key, value in overall.items()
            if key not in {"tracked", "with_24h", "with_72h"}
        }
    )
    by_tier = defaultdict(list)
    by_stage = defaultdict(list)
    for row in rows:
        by_tier[row.get("caught_tier") or "unknown"].append(row)
        by_stage[row.get("caught_stage") or "unknown"].append(row)
    summary["by_tier"] = {
        key: metrics(group)
        for key, group in sorted(by_tier.items())
    }
    summary["by_stage"] = {
        key: metrics(group)
        for key, group in sorted(by_stage.items())
    }
    return summary


def update_signal_outcomes(state, alerts, observed_at, config):
    if not config.get("signal_outcomes_enabled", True):
        return {}
    outcomes = state.setdefault("signal_outcomes", {})
    market = state.get("market") or {}
    now = parse_timestamp(observed_at) or int(time.time())

    for alert in sorted(alerts or [], key=alert_history_sort_key):
        pool = alert.get("pool") or {}
        token = clean_solana_address(
            pool.get("token_address") or pool.get("pool_address")
        )
        if not token:
            continue
        caught_at = (
            alert.get("obs_mcap_at")
            or alert.get("created_at")
            or alert.get("window_start")
            or observed_at
        )
        caught_ts = parse_timestamp(caught_at)
        existing = outcomes.get(token)
        if existing and parse_timestamp(existing.get("caught_at")) <= caught_ts:
            continue
        outcomes[token] = {
            "token_address": pool.get("token_address"),
            "pool_address": pool.get("pool_address"),
            "symbol": pool.get("symbol"),
            "caught_at": caught_at,
            "caught_mcap_usd": to_float(
                alert.get("obs_mcap_usd") or pool.get("mcap_usd")
            ),
            "caught_price_usd": to_float(
                alert.get("obs_price_usd") or pool.get("price_usd")
            ),
            "caught_liquidity_usd": to_float(
                alert.get("obs_liquidity_usd") or pool.get("liquidity_usd")
            ),
            "caught_tier": alert.get("action_tier"),
            "caught_stage": alert.get("reactivation_stage"),
            "caught_score": int(alert.get("score") or 0),
            "signal_family": alert.get("signal_family"),
            "horizons": {},
        }

    for token, outcome in list(outcomes.items()):
        entry = market.get(token) or {}
        snapshot = outcome_market_snapshot(entry, observed_at)
        if not snapshot:
            continue
        caught_ts = parse_timestamp(outcome.get("caught_at"))
        snapshot_ts = parse_timestamp(snapshot.get("at"))
        if not caught_ts or snapshot_ts < caught_ts:
            continue
        snapshot["return_pct"] = outcome_return_pct(outcome, snapshot)
        outcome["latest"] = snapshot
        outcome["updated_at"] = observed_at
        current_return = snapshot.get("return_pct")
        if current_return is not None:
            peak = outcome.get("max_favorable")
            if not peak or current_return > to_float(peak.get("return_pct")):
                outcome["max_favorable"] = dict(snapshot)
            trough = outcome.get("max_adverse")
            if not trough or current_return < to_float(trough.get("return_pct")):
                outcome["max_adverse"] = dict(snapshot)
            elapsed_minutes = max(0.0, (snapshot_ts - caught_ts) / 60)
            for threshold, field in (
                (50, "time_to_1_5x_minutes"),
                (100, "time_to_2x_minutes"),
                (400, "time_to_5x_minutes"),
            ):
                if current_return >= threshold and outcome.get(field) is None:
                    outcome[field] = elapsed_minutes
        horizons = outcome.setdefault("horizons", {})
        for name, seconds in SIGNAL_OUTCOME_HORIZONS.items():
            if name in horizons or snapshot_ts < caught_ts + seconds:
                continue
            horizons[name] = {
                **snapshot,
                "target_at": iso(caught_ts + seconds),
                "delay_seconds": max(0, snapshot_ts - caught_ts - seconds),
                # These fields are frozen at the first observation after the
                # horizon is due. Later performance cannot leak backward into
                # a historical 1h/6h/24h result.
                "max_return_pct": to_float(
                    (outcome.get("max_favorable") or {}).get("return_pct"),
                    None,
                ),
                "max_drawdown_pct": to_float(
                    (outcome.get("max_adverse") or {}).get("return_pct"),
                    None,
                ),
                "time_to_1_5x_minutes": outcome.get("time_to_1_5x_minutes"),
                "time_to_2x_minutes": outcome.get("time_to_2x_minutes"),
                "time_to_5x_minutes": outcome.get("time_to_5x_minutes"),
            }

    retention_days = float(config.get("signal_outcomes_retention_days", 180))
    cutoff = now - int(retention_days * 86400) if retention_days > 0 else 0
    max_tokens = max(0, int(config.get("signal_outcomes_max_tokens", 500)))
    ordered = sorted(
        (
            (token, row)
            for token, row in outcomes.items()
            if not cutoff or parse_timestamp(row.get("caught_at")) >= cutoff
        ),
        key=lambda item: parse_timestamp(item[1].get("caught_at")),
        reverse=True,
    )
    if max_tokens:
        ordered = ordered[:max_tokens]
    state["signal_outcomes"] = dict(ordered)
    stats = summarize_signal_outcomes(state["signal_outcomes"])
    stats["updated_at"] = observed_at
    state.setdefault("maintenance", {})["signal_outcomes"] = stats
    return stats


def refresh_alert_tiers_with_market_ath(state, alerts, config):
    market = state.get("market") if isinstance(state, dict) else {}
    if not isinstance(market, dict):
        return alerts
    lanes = config.get("lanes") or {}
    for alert in alerts or []:
        pool_payload = alert.get("pool") or {}
        token = pool_payload.get("token_address") or pool_payload.get("pool_address")
        entry = market.get(token) or {}
        ath_mcap = trusted_ath_mcap(entry)
        if not ath_mcap:
            continue
        pool = Pool(
            **{
                key: value
                for key, value in pool_payload.items()
                if key in Pool.__dataclass_fields__
            }
        )
        pool.ath_mcap_usd = ath_mcap
        pool.ath_current_ratio = (
            float(pool.mcap_usd or alert.get("obs_mcap_usd") or 0) / ath_mcap
            if ath_mcap
            else None
        )
        lane_name = alert.get("lane")
        lane_config = apply_lane(config, lane_name) if lane_name in lanes else dict(config)
        pool_config = reactivation_stage_config(pool, lane_config)
        evidence = {
            "hard_wallets": int(alert.get("hard_wallets") or 0),
            "support_wallets": int(alert.get("support_wallets") or 0),
            "hard_sol": float(alert.get("hard_sol") or 0),
            "support_sol": float(alert.get("support_sol") or 0),
            "hard_classes": alert.get("hard_classes") or {},
            "support_classes": alert.get("support_classes") or {},
            "support_only": bool(
                alert.get("support_wallets")
                and not alert.get("hard_wallets")
                and not alert.get("common_funders")
                and not alert.get("common_recipients")
            ),
        }
        tier, reasons, penalties, quality_metrics = classify_alert_tier(
            pool,
            alert,
            evidence,
            pool_config,
        )
        if (
            (alert.get("data_quality") or {}).get("status") == "partial"
            and tier in ("actionable", "hot_reactivation")
        ):
            tier = "watch"
            if "partial onchain coverage" not in penalties:
                penalties.append("partial onchain coverage")
        alert["action_tier"] = tier
        alert["quality_reasons"] = reasons
        alert["quality_penalties"] = penalties
        alert["quality_metrics"] = quality_metrics
        alert["ath_recalculated_in_scan"] = True
    return alerts


def prune_wallet_cache(state, config):
    wallet_cache = state.get("wallet_cache")
    if not isinstance(wallet_cache, dict):
        return None

    normal_limit = int(config.get("wallet_cache_normal_limit", 20000))
    if normal_limit < 0:
        return None
    now = int(time.time())
    retention_days = float(config.get("wallet_cache_retention_days", 30))
    cutoff = now - int(retention_days * 86400) if retention_days > 0 else 0
    max_versions = max(1, int(config.get("wallet_cache_max_versions_per_wallet", 4)))
    version_buckets = defaultdict(list)
    for key, value in wallet_cache.items():
        if not isinstance(value, dict):
            continue
        wallet = value.get("wallet") or key.split(":", 1)[0]
        version_buckets[wallet].append((key, value))
    allowed_version_keys = set()
    for entries in version_buckets.values():
        entries.sort(
            key=lambda item: max(
                int(item[1].get("cached_at") or 0),
                int(item[1].get("as_of_time") or 0),
                parse_timestamp(item[1].get("previous_time")),
            ),
            reverse=True,
        )
        allowed_version_keys.update(key for key, _ in entries[:max_versions])

    keep_classes = set(config.get("wallet_cache_keep_classes") or ["fresh", "freshish", "dormant", "low_tx"])
    class_limits = config.get("wallet_cache_class_limits") or {}
    normal_keys = [
        key
        for key, value in wallet_cache.items()
        if (
            isinstance(value, dict)
            and value.get("wallet_class") == "normal"
            and key in allowed_version_keys
            and (
                not cutoff
                or max(
                    int(value.get("cached_at") or 0),
                    int(value.get("as_of_time") or 0),
                )
                >= cutoff
            )
        )
    ]
    normal_keys.sort(
        key=lambda key: max(
            int(wallet_cache[key].get("cached_at") or 0),
            int(wallet_cache[key].get("as_of_time") or 0),
        ),
        reverse=True,
    )
    keep_normal_keys = set(normal_keys[:normal_limit]) if normal_limit else set()
    before = len(wallet_cache)
    pruned = {}
    class_counts = Counter()

    class_buckets = defaultdict(list)
    for key, value in wallet_cache.items():
        if not isinstance(value, dict):
            continue
        if key not in allowed_version_keys:
            continue
        if cutoff and max(
            int(value.get("cached_at") or 0),
            int(value.get("as_of_time") or 0),
        ) < cutoff:
            continue
        wallet_class = value.get("wallet_class") or "unknown"
        class_buckets[wallet_class].append((key, value))

    keep_class_keys = set()
    for wallet_class, entries in class_buckets.items():
        if wallet_class == "normal":
            continue
        limit = int(class_limits.get(wallet_class, 0) or 0)
        if limit <= 0:
            continue
        entries.sort(
            key=lambda item: max(
                parse_timestamp(item[1].get("previous_time")),
                parse_timestamp(item[1].get("funding_time")),
                int(item[1].get("cached_at") or 0),
                int(item[1].get("as_of_time") or 0),
            ),
            reverse=True,
        )
        keep_class_keys.update(key for key, _ in entries[:limit])

    for key, value in wallet_cache.items():
        if key not in allowed_version_keys:
            continue
        if cutoff and max(
            int(value.get("cached_at") or 0),
            int(value.get("as_of_time") or 0),
        ) < cutoff:
            continue
        wallet_class = value.get("wallet_class") if isinstance(value, dict) else None
        if wallet_class == "normal" and key not in keep_normal_keys:
            continue
        if wallet_class != "normal":
            if wallet_class not in keep_classes:
                # Unknown classes are kept; they are rare and can indicate a schema change.
                pass
            elif wallet_class in class_limits and key not in keep_class_keys:
                continue
        pruned[key] = value
        class_counts[wallet_class or "unknown"] += 1

    state["wallet_cache"] = pruned
    stats = {
        "pruned_at": utc_now().isoformat().replace("+00:00", "Z"),
        "before_entries": before,
        "after_entries": len(pruned),
        "removed_entries": before - len(pruned),
        "normal_limit": normal_limit,
        "retention_days": retention_days,
        "max_versions_per_wallet": max_versions,
        "class_limits": class_limits,
        "kept_classes": sorted(keep_classes),
        "after_class_counts": dict(class_counts),
    }
    state.setdefault("maintenance", {})["wallet_cache_prune"] = stats
    return stats


def dict_item_timestamp(value, keys):
    if not isinstance(value, dict):
        return 0
    return max(parse_timestamp(value.get(key)) for key in keys)


def compact_pool_swap_buffers(pools_state, config, now):
    if not isinstance(pools_state, dict):
        return {
            "pools_checked": 0,
            "before_swaps": 0,
            "after_swaps": 0,
            "removed_swaps": 0,
        }
    retention_hours = float(config.get("state_swap_buffer_retention_hours", 24))
    max_swaps = max(0, int(config.get("state_swap_buffer_max_swaps", 900)))
    cutoff = now - int(retention_hours * 3600) if retention_hours > 0 else 0
    before = 0
    after = 0
    pools_with_buffers = 0
    lanes = config.get("lanes") or {}
    enabled_lane_configs = [
        lane
        for lane in lanes.values()
        if isinstance(lane, dict) and lane.get("enabled", True)
    ]
    sticky_enabled = bool(
        config.get("sticky_accumulation_enabled", False)
        or any(
            lane.get("sticky_accumulation_enabled", False)
            for lane in enabled_lane_configs
        )
    )
    reactivation_enabled = bool(
        config.get("reactivation_wave_enabled", False)
        or any(
            lane.get("reactivation_wave_enabled", False)
            for lane in enabled_lane_configs
        )
    )
    for pool_state in pools_state.values():
        if not isinstance(pool_state, dict):
            continue
        pool_has_buffer = False
        for key in ("sticky_accumulation_swaps", "reactivation_wave_swaps"):
            raw = pool_state.get(key)
            if not isinstance(raw, list):
                continue
            pool_has_buffer = True
            before += len(raw)
            if (
                (key == "sticky_accumulation_swaps" and not sticky_enabled)
                or (key == "reactivation_wave_swaps" and not reactivation_enabled)
            ):
                pool_state.pop(key, None)
                continue
            compacted = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                block_time = int(to_float(item.get("block_time")) or parse_timestamp(item.get("time")))
                if not block_time or (cutoff and block_time < cutoff):
                    continue
                item = compact_wave_swap(item)
                if item:
                    compacted.append(item)
            compacted.sort(key=lambda item: (int(item.get("block_time") or 0), item.get("signature") or ""))
            if max_swaps and len(compacted) > max_swaps:
                compacted = compacted[-max_swaps:]
            if compacted:
                pool_state[key] = compacted
            else:
                pool_state.pop(key, None)
            after += len(compacted)
        if pool_has_buffer:
            pools_with_buffers += 1
    return {
        "pools_checked": len(pools_state),
        "pools_with_buffers": pools_with_buffers,
        "retention_hours": retention_hours,
        "max_swaps_per_buffer": max_swaps,
        "before_swaps": before,
        "after_swaps": after,
        "removed_swaps": before - after,
    }


def compact_state(state, pools, alerts, config, observed_at):
    now = parse_timestamp(observed_at) or int(time.time())
    token_keys = set()
    pool_keys = set()
    for pool in pools or []:
        if pool.token_address:
            token_keys.add(pool.token_address)
        if pool.pool_address:
            pool_keys.add(pool.pool_address)
    for alert in alerts or []:
        pool = alert.get("pool") or {}
        token = pool.get("token_address")
        pool_address = pool.get("pool_address")
        if token:
            token_keys.add(token)
        if pool_address:
            pool_keys.add(pool_address)

    market_ttl = float(config.get("state_market_retention_hours", 96))
    pool_ttl = float(config.get("state_pool_retention_hours", 168))
    enrichment_ttl = float(config.get("state_enrichment_cache_retention_hours", 72))

    maintenance = state.setdefault("maintenance", {})
    stats = {"compacted_at": observed_at}

    market = state.get("market")
    if isinstance(market, dict):
        before = len(market)
        cutoff = now - int(market_ttl * 3600) if market_ttl > 0 else 0
        pruned_market = {}
        for key, value in market.items():
            pool_address = value.get("pool_address") if isinstance(value, dict) else None
            last_seen = dict_item_timestamp(
                value,
                (
                    "latest_seen_at",
                    "scan_mcap_at",
                    "first_signal_at",
                    "ath_latest_checked_at",
                    "ath_filter_checked_at",
                ),
            )
            if key in token_keys or pool_address in pool_keys or (cutoff and last_seen >= cutoff):
                pruned_market[key] = value
        state["market"] = pruned_market
        stats["market_before"] = before
        stats["market_after"] = len(pruned_market)

    pools_state = state.get("pools")
    if isinstance(pools_state, dict):
        stats["swap_buffers"] = compact_pool_swap_buffers(pools_state, config, now)
        before = len(pools_state)
        cutoff = now - int(pool_ttl * 3600) if pool_ttl > 0 else 0
        thesis_retention_days = float(
            config.get("signal_thesis_state_retention_days", 30)
        )
        thesis_cutoff = (
            now - int(thesis_retention_days * 86400)
            if thesis_retention_days > 0
            else 0
        )
        pruned_pools = {}
        for key, value in pools_state.items():
            last_seen = dict_item_timestamp(
                value,
                (
                    "last_scanned_at",
                    "last_scan_failed_at",
                    "helius_latest_time",
                    "latest_time",
                    "helius_deep_scanned_at",
                ),
            )
            thesis = (
                value.get("signal_thesis")
                if isinstance(value, dict)
                else None
            )
            thesis_seen = dict_item_timestamp(
                thesis,
                (
                    "updated_at",
                    "last_checked_at",
                    "last_signal_at",
                    "signal_at",
                ),
            )
            keep_thesis = bool(
                isinstance(thesis, dict)
                and (
                    not thesis_cutoff
                    or thesis_seen >= thesis_cutoff
                )
            )
            if key in pool_keys or (cutoff and last_seen >= cutoff) or keep_thesis:
                pruned_pools[key] = value
        state["pools"] = pruned_pools
        stats["pools_before"] = before
        stats["pools_after"] = len(pruned_pools)

    baselines = state.get("activity_baselines")
    if isinstance(baselines, dict):
        before = len(baselines)
        retention_days = float(
            config.get("reactivation_baseline_state_retention_days", 120)
        )
        cutoff = now - int(retention_days * 86400) if retention_days > 0 else 0
        five_cutoff = now - int(
            float(
                config.get(
                    "reactivation_baseline_five_minute_retention_hours",
                    48,
                )
            )
            * 3600
        )
        hourly_cutoff = now - int(
            float(config.get("reactivation_baseline_hourly_retention_days", 7))
            * 86400
        )
        pruned_baselines = {}
        for key, value in baselines.items():
            if not isinstance(value, dict):
                continue
            last_snapshot = int(value.get("last_snapshot_at") or 0)
            if cutoff and last_snapshot and last_snapshot < cutoff and key not in token_keys:
                continue
            value["five_minute"] = [
                item
                for item in value.get("five_minute", [])
                if isinstance(item, list)
                and item
                and int(item[0] or 0) >= five_cutoff
            ][-int(config.get("reactivation_baseline_max_five_minute_buckets", 576)) :]
            value["hourly"] = [
                item
                for item in value.get("hourly", [])
                if isinstance(item, list)
                and item
                and int(item[0] or 0) >= hourly_cutoff
            ][-int(config.get("reactivation_baseline_max_hourly_buckets", 168)) :]
            pruned_baselines[key] = value
        state["activity_baselines"] = pruned_baselines
        stats["activity_baselines_before"] = before
        stats["activity_baselines_after"] = len(pruned_baselines)

    cutoff = now - int(enrichment_ttl * 3600) if enrichment_ttl > 0 else 0
    for section in ("social_cache", "token_intel_cache"):
        cache = state.get(section)
        if not isinstance(cache, dict):
            continue
        before = len(cache)
        state[section] = {
            key: value
            for key, value in cache.items()
            if key in token_keys or (cutoff and dict_item_timestamp(value, ("cached_at",)) >= cutoff)
        }
        stats[f"{section}_before"] = before
        stats[f"{section}_after"] = len(state[section])

    remote_updates = state.get("remote_discovery_updated_at")
    if isinstance(remote_updates, dict):
        before = len(remote_updates)
        remote_cutoff = now - int(
            float(config.get("reactivation_baseline_state_retention_days", 120))
            * 86400
        )
        retained_tokens = set(state.get("market") or {}) | set(
            state.get("activity_baselines") or {}
        ) | set(state.get("signal_outcomes") or {})
        state["remote_discovery_updated_at"] = {
            key: value
            for key, value in remote_updates.items()
            if key in retained_tokens or parse_timestamp(value) >= remote_cutoff
        }
        stats["remote_discovery_updates_before"] = before
        stats["remote_discovery_updates_after"] = len(
            state["remote_discovery_updated_at"]
        )

    maintenance["state_compaction"] = stats
    return stats


def apply_market_meta(pool_dict, state):
    if not pool_dict:
        return pool_dict
    market = state.get("market", {}) if state else {}
    meta = market.get(pool_dict.get("token_address") or pool_dict.get("pool_address"))
    if not meta:
        return pool_dict
    enriched = dict(pool_dict)
    for key in (
        "ath_mcap_usd",
        "ath_mcap_at",
        "ath_price_usd",
        "ath_pool_address",
        "ath_source",
        "ath_status",
        "ath_validation_status",
        "ath_candidate_mcap_usd",
        "ath_candidate_price_usd",
        "ath_candidate_source",
        "ath_candidate_status",
        "ath_candidate_error",
        "ath_error",
        "ath_error_checked_at",
        "ath_current_ratio",
        "ath_drawdown_pct",
        "ath_filter_checked_at",
        "ath_latest_checked_at",
        "ath_verified_at",
        "latest_mcap_usd",
        "latest_price_usd",
        "latest_liquidity_usd",
        "latest_seen_at",
        "market_snapshot_at",
        "current_market_verified_at",
        "market_snapshot_stale",
        "market_snapshot_error",
        "market_snapshot_checked_at",
        "market_source",
        "scan_mcap_usd",
        "scan_price_usd",
        "scan_liquidity_usd",
        "scan_mcap_at",
        "scan_source",
        "first_signal_at",
        "first_obs_mcap_usd",
        "first_obs_price_usd",
        "first_obs_liquidity_usd",
        "first_obs_mcap_at",
        "first_obs_source",
        "first_obs_lane",
        "first_obs_score",
    ):
        if meta.get(key) is not None:
            enriched[key] = meta.get(key)
    if not enriched.get("pair_created_at") and meta.get("pair_created_at"):
        enriched["pair_created_at"] = meta.get("pair_created_at")
        enriched["pair_created_at_iso"] = meta.get("pair_created_at_iso")
    return enriched


def apply_market_meta_to_summary(summary, state):
    enriched = dict(summary)
    if "pool" in enriched:
        enriched["pool"] = apply_market_meta(enriched["pool"], state)
    return enriched


def apply_market_meta_to_alert(alert, state):
    enriched = dict(alert)
    if "pool" in enriched:
        enriched["pool"] = apply_market_meta(enriched["pool"], state)
    return enriched


def build_scan_health(summaries, lane_stats, config):
    scanned = len(summaries)
    candidate_pools = sum(int(item.get("universe_pools") or 0) for item in lane_stats.values())
    budget_deferred = sum(
        int((item.get("selection") or {}).get("rpc_budget_deferred") or 0)
        for item in lane_stats.values()
    )
    failed = sum(1 for item in summaries if item.get("scan_failed") or item.get("error"))
    truncated = sum(1 for item in summaries if (item.get("trade_fetch") or {}).get("truncated"))
    coverage_fetch_items = [
        (item, item.get("trade_fetch") or {})
        for item in summaries
        if (item.get("trade_fetch") or {}).get("source")
        in ("enhanced_transactions", "helius_transactions", "pool_signatures")
    ]
    live_fetch_items = [
        (item, fetch)
        for item, fetch in coverage_fetch_items
        if fetch.get("source") in ("enhanced_transactions", "helius_transactions")
    ]
    live_fetches = [fetch for _item, fetch in coverage_fetch_items]
    live_truncated = sum(1 for fetch in live_fetches if fetch.get("live_truncated"))
    backfill_pending = sum(1 for fetch in live_fetches if fetch.get("backfill_pending"))
    rolling_gap_pending = sum(
        1 for fetch in live_fetches if fetch.get("rolling_gap_pending")
    )
    history_gap = sum(1 for fetch in live_fetches if int(fetch.get("history_gap_seconds") or 0) > 60)
    stale_market_snapshots = sum(
        1 for fetch in live_fetches if fetch.get("market_activity_stale")
    )
    enhanced_head_fallbacks = sum(
        1
        for item in summaries
        if "enhanced transaction head lagged the standard rpc head"
        in str(item.get("fallback_error") or "").lower()
    )
    max_live_lag_seconds = int(float(config.get("scan_health_max_live_lag_minutes", 20)) * 60)
    min_live_activity = int(config.get("scan_health_live_activity_txns_1h_min", 10))
    active_live_fetches = [
        (item, fetch)
        for item, fetch in live_fetch_items
        if int((item.get("pool") or {}).get("txns_1h") or 0) >= min_live_activity
    ]
    stale_live = sum(
        1
        for _item, fetch in active_live_fetches
        if fetch.get("live_head_lag_seconds") is not None
        and int(fetch.get("live_head_lag_seconds") or 0) > max_live_lag_seconds
    )
    transactions = sum(int(item.get("transactions_scanned") or item.get("new_signatures") or 0) for item in summaries)
    parsed_swaps = sum(int(item.get("parsed_swaps") or 0) for item in summaries)
    candidate_buys = sum(int(item.get("candidate_buys") or 0) for item in summaries)
    classified_buys = sum(int(item.get("classified_buys") or 0) for item in summaries)
    classification_errors = sum(int(item.get("classification_errors") or 0) for item in summaries)
    transaction_errors = sum(
        int((item.get("trade_fetch") or {}).get("transaction_errors") or 0)
        for item in summaries
    )
    parse_errors = sum(
        int(item.get("parse_errors") or (item.get("trade_fetch") or {}).get("parse_errors") or 0)
        for item in summaries
    )
    scan_errors = Counter()
    for item in summaries:
        error = str(item.get("error") or "")
        if not error:
            continue
        lower_error = error.lower()
        provider = next(
            (
                name
                for name in (
                    "chainstack",
                    "drpc",
                    "publicnode",
                    "alchemy",
                    "helius",
                )
                if f"{name}:" in lower_error
            ),
            "rpc",
        )
        if "all rpc providers" in lower_error:
            scan_errors["rpc_all_unavailable"] += 1
        elif "rate_limit" in lower_error or "http 429" in lower_error:
            scan_errors[f"{provider}_rate_limit"] += 1
        elif "quota" in lower_error or "credit" in lower_error or "http 402" in lower_error:
            scan_errors[f"{provider}_quota"] += 1
        elif "auth" in lower_error or "http 401" in lower_error or "http 403" in lower_error:
            scan_errors[f"{provider}_auth"] += 1
        elif "circuit open" in lower_error:
            scan_errors[f"{provider}_circuit_open"] += 1
        else:
            scan_errors["other"] += 1
    zero_parse_pools = sum(
        1
        for item in summaries
        if not item.get("scan_failed")
        and not item.get("error")
        and int(item.get("transactions_scanned") or item.get("new_signatures") or 0) > 0
        and int(item.get("parsed_swaps") or 0) == 0
    )
    failed_ratio = failed / scanned if scanned else 0.0
    truncated_ratio = truncated / scanned if scanned else 0.0
    live_fetch_count = len(live_fetches)
    enhanced_fetch_count = len(live_fetch_items)
    live_truncated_ratio = live_truncated / live_fetch_count if live_fetch_count else 0.0
    history_gap_ratio = history_gap / enhanced_fetch_count if enhanced_fetch_count else 0.0
    active_live_fetch_count = len(active_live_fetches)
    stale_live_ratio = stale_live / active_live_fetch_count if active_live_fetch_count else 0.0
    zero_parse_ratio = zero_parse_pools / scanned if scanned else 0.0
    transaction_attempts = transactions + transaction_errors
    transaction_error_ratio = transaction_errors / transaction_attempts if transaction_attempts else 0.0
    min_scanned = max(0, int(config.get("scan_health_min_scanned_pools", 5)))
    max_failed_ratio = float(config.get("scan_health_max_failed_ratio", 0.25))
    max_zero_parse_ratio = float(config.get("scan_health_max_zero_parse_ratio", 0.25))
    max_live_truncated_ratio = float(config.get("scan_health_max_truncated_ratio", 0.25))
    max_history_gap_ratio = float(config.get("scan_health_max_history_gap_ratio", 0.25))
    max_stale_live_ratio = float(config.get("scan_health_max_stale_live_ratio", 0.25))
    max_transaction_error_ratio = float(config.get("scan_health_max_transaction_error_ratio", 0.1))
    reasons = []
    status = "healthy"

    discovery = config.get("_discovery_stats") or {}
    live_discovered = int(discovery.get("discovered_pools") or 0)
    if candidate_pools >= min_scanned and scanned < min_scanned:
        status = "unhealthy"
        reasons.append(f"only {scanned}/{candidate_pools} candidate pools were scanned")
    elif scanned == 0 and live_discovered == 0:
        status = "unhealthy"
        reasons.append("market discovery and scan both returned zero pools")
    if scanned and failed_ratio > max_failed_ratio:
        status = "unhealthy"
        reasons.append(f"pool failure ratio {failed_ratio:.0%} exceeds {max_failed_ratio:.0%}")

    if status != "unhealthy":
        if live_fetch_count and stale_live_ratio > max_stale_live_ratio:
            status = "degraded"
            reasons.append(f"live transaction head is stale for {stale_live_ratio:.0%} of active pools")
        if live_fetch_count and live_truncated_ratio > max_live_truncated_ratio:
            status = "degraded"
            reasons.append(
                f"live transaction coverage is partial for {live_truncated_ratio:.0%} of scanned pools"
            )
        if live_fetch_count and history_gap_ratio > max_history_gap_ratio:
            status = "degraded"
            reasons.append(f"rolling buffer has a history gap for {history_gap_ratio:.0%} of active pools")
        if transaction_error_ratio > max_transaction_error_ratio:
            status = "degraded"
            reasons.append(
                f"transaction detail errors reached {transaction_error_ratio:.0%} of requested transactions"
            )
        if scanned and zero_parse_ratio > max_zero_parse_ratio:
            status = "degraded"
            reasons.append(f"no swaps parsed for {zero_parse_ratio:.0%} of scanned pools with transactions")
        if classification_errors:
            status = "degraded"
            reasons.append(f"{classification_errors} wallet classifications failed")
        if parse_errors:
            status = "degraded"
            reasons.append(f"{parse_errors} transactions could not be parsed")
        if config.get("_gmgn_error"):
            status = "degraded"
            reasons.append("GMGN unavailable; registry/fallback discovery and cached ATH only")
        if budget_deferred:
            status = "degraded"
            reasons.append(
                f"{budget_deferred} pools deferred after the hourly RPC budget was exhausted"
            )

    return {
        "status": status,
        "reasons": reasons,
        "candidate_pools": candidate_pools,
        "scanned_pools": scanned,
        "failed_pools": failed,
        "failed_ratio": failed_ratio,
        "rpc_budget_deferred_pools": budget_deferred,
        "truncated_pools": truncated,
        "truncated_ratio": truncated_ratio,
        "live_fetch_pools": live_fetch_count,
        "active_live_fetch_pools": active_live_fetch_count,
        "partial_live_windows": live_truncated,
        "partial_live_ratio": live_truncated_ratio,
        "backfill_pending_pools": backfill_pending,
        "rolling_gap_pending_pools": rolling_gap_pending,
        "history_gap_pools": history_gap,
        "history_gap_ratio": history_gap_ratio,
        "stale_market_snapshot_pools": stale_market_snapshots,
        "enhanced_head_fallback_pools": enhanced_head_fallbacks,
        "stale_live_pools": stale_live,
        "stale_live_ratio": stale_live_ratio,
        "zero_parse_pools": zero_parse_pools,
        "zero_parse_ratio": zero_parse_ratio,
        "transactions_scanned": transactions,
        "parsed_swaps": parsed_swaps,
        "candidate_buys": candidate_buys,
        "classified_buys": classified_buys,
        "classification_errors": classification_errors,
        "transaction_errors": transaction_errors,
        "transaction_error_ratio": transaction_error_ratio,
        "parse_errors": parse_errors,
        "scan_error_categories": dict(scan_errors),
        "estimated_rpc_credits": int(config.get("_rpc_estimated_credits") or 0),
        "rpc_providers": config.get("_rpc_providers", {}),
        "gmgn_status": "error" if config.get("_gmgn_error") else "ok",
    }


def build_report_payload(universe, summaries, alerts, rpc_calls, config, generated_at, state):
    enriched_summaries = [apply_market_meta_to_summary(summary, state) for summary in summaries]
    enriched_alerts = [apply_market_meta_to_alert(alert, state) for alert in alerts]
    deleted_tokens = load_deleted_tokens()
    active = [summary for summary in enriched_summaries if summary.get("classified_buys")]
    active.sort(key=lambda item: item.get("buy_sol", 0.0), reverse=True)
    return {
        "generated_at": generated_at,
        "mode": config.get("mode"),
        "lane": config.get("lane"),
        "profile": config.get("lane") or config.get("mode"),
        "config": {
            "config_version": effective_config_version(config),
            "mcap_min_usd": config["mcap_min_usd"],
            "mcap_max_usd": config["mcap_max_usd"],
            "liquidity_min_usd": config["liquidity_min_usd"],
            "classify_buy_min_sol": config["classify_buy_min_sol"],
            "alert_window_minutes": config["alert_window_minutes"],
            "low_tx_support_only_alerts": config.get("low_tx_support_only_alerts", False),
            "alert_event_export_limit": config.get("alert_event_export_limit", 80),
            "actionable_mcap_max_usd": config.get("actionable_mcap_max_usd"),
            "watch_mcap_max_usd": config.get("watch_mcap_max_usd"),
            "dex_allowlist": config.get("dex_allowlist", []),
            "caught_market_refresh_enabled": config.get("caught_market_refresh_enabled", True),
            "caught_market_refresh_ttl_minutes": config.get("caught_market_refresh_ttl_minutes", 50),
            "caught_market_refresh_max_tokens": config.get("caught_market_refresh_max_tokens", 80),
            "reactivation_wave_enabled": config.get("reactivation_wave_enabled", False),
            "reactivation_wave_min_buy_sol": config.get("reactivation_wave_min_buy_sol"),
            "reactivation_wave_min_net_buy_sol": config.get("reactivation_wave_min_net_buy_sol"),
            "reactivation_wave_min_unique_buyers": config.get("reactivation_wave_min_unique_buyers"),
            "reactivation_wave_min_sticky_supply_pct": config.get("reactivation_wave_min_sticky_supply_pct"),
            "signal_thesis_tracking_enabled": config.get(
                "signal_thesis_tracking_enabled",
                True,
            ),
            "signal_thesis_recheck_minutes": config.get(
                "signal_thesis_recheck_minutes",
                60,
            ),
            "signal_thesis_recheck_grace_minutes": config.get(
                "signal_thesis_recheck_grace_minutes",
                15,
            ),
            "signal_thesis_invalidation_confirmations_required": config.get(
                "signal_thesis_invalidation_confirmations_required",
                2,
            ),
        },
        "stats": {
            "universe_pools": len(universe),
            "scanned_pools": len(summaries),
            "alerts": len(alerts),
            "rpc_calls": dict(rpc_calls),
            "rpc_retries": config.get("_rpc_retries", {}),
            "rpc_failures": config.get("_rpc_failures", {}),
            "estimated_rpc_credits": int(config.get("_rpc_estimated_credits") or 0),
            "rpc_providers": config.get("_rpc_providers", {}),
            "rpc_failovers": config.get("_rpc_failovers", {}),
            "scan_health": config.get("_scan_health", {}),
            "discovery": config.get("_discovery_stats", {}),
            "gmgn_ath": (state.get("maintenance") or {}).get("gmgn_ath", {}),
            "caught_market_refresh": (state.get("maintenance") or {}).get("caught_market_refresh", {}),
            "signal_outcomes": (state.get("maintenance") or {}).get("signal_outcomes", {}),
            "deleted_tokens": {
                "tokens": len(deleted_tokens["tokens"]),
                "pools": len(deleted_tokens["pools"]),
            },
        },
        "alerts": enriched_alerts,
        "signal_theses": signal_theses_for_report(
            state,
            deleted_tokens,
        ),
        "active_pools": active[:100],
        "universe": [apply_market_meta(pool.as_dict(), state) for pool in universe[:250]],
        "summaries": enriched_summaries[:250],
    }


def report_config_for_lanes(config, lane_configs):
    """Export the actual effective lane config, plus run-time diagnostics."""
    if len(lane_configs) == 1:
        report_config = dict(next(iter(lane_configs.values())))
    else:
        report_config = dict(config)
    for key in (
        "_rpc_retries",
        "_rpc_failures",
        "_rpc_failovers",
        "_rpc_estimated_credits",
        "_rpc_providers",
        "_scan_health",
        "_discovery_stats",
    ):
        if key in config:
            report_config[key] = config[key]
    return report_config


def write_report_json(payload):
    save_json(REPORT_JSON_PATH, payload)


def render_report(payload):
    config = payload["config"]
    stats = payload["stats"]
    alerts = payload["alerts"]
    lines = []
    lines.append(f"# Solana Radar Report")
    lines.append("")
    lines.append(f"- generated_at: {payload['generated_at']}")
    lines.append(f"- profile: {payload.get('profile') or payload.get('mode')}")
    if payload.get("lane_stats"):
        lines.append(f"- lanes_scanned: {', '.join(payload.get('lanes_scanned') or [])}")
    else:
        lines.append(f"- mcap_filter: ${config['mcap_min_usd']:,}-${config['mcap_max_usd']:,}")
        lines.append(f"- liquidity_min_usd: ${config['liquidity_min_usd']:,}")
        lines.append(f"- classify_buy_min_sol: {config['classify_buy_min_sol']}")
    lines.append(f"- universe_pools: {stats['universe_pools']}")
    lines.append(f"- scanned_pools: {stats['scanned_pools']}")
    lines.append(f"- alerts: {stats['alerts']}")
    lines.append(f"- rpc_calls: {stats['rpc_calls']}")
    lines.append("")

    if alerts:
        lines.append("## Alerts")
        for alert in alerts[:20]:
            pool = alert["pool"]
            lines.append("")
            lines.append(
                f"### {pool.get('symbol') or pool.get('name') or pool['pool_address']} "
                f"score {alert['score']}"
            )
            lines.append(f"- pool: {pool['pool_address']}")
            lines.append(f"- url: {pool.get('url')}")
            lines.append(f"- mcap_usd: {pool.get('mcap_usd'):.0f}")
            lines.append(f"- liquidity_usd: {pool.get('liquidity_usd'):.0f}")
            lines.append(f"- window: {alert['window_start']} - {alert['window_end']}")
            lines.append(f"- suspicious_wallets: {alert['suspicious_wallets']}")
            lines.append(f"- suspicious_sol: {alert['suspicious_sol']:.2f}")
            lines.append(f"- classes: {alert['classes']}")
            wave = alert.get("wave") or {}
            if wave:
                lines.append(
                    "- reactivation_wave: "
                    f"buy={wave.get('buy_sol', 0):.2f} SOL "
                    f"sell={wave.get('sell_sol', 0):.2f} SOL "
                    f"net={wave.get('net_buy_sol', 0):.2f} SOL "
                    f"buyers={wave.get('unique_buyers', 0)} "
                    f"sticky_supply={wave.get('sticky_supply_pct', 0):.2f}%"
                )
            if alert["common_funders"]:
                lines.append(f"- common_funders: {alert['common_funders']}")
            if alert.get("common_recipients"):
                lines.append(f"- common_recipients: {alert['common_recipients']}")
            if alert.get("routed_buys"):
                lines.append(f"- routed_buys: {alert['routed_buys']}")
            token_intel = alert.get("token_intel") or {}
            narrative = token_intel.get("narrative") or {}
            if narrative:
                lines.append(
                    f"- narrative: {narrative.get('primary')} ({narrative.get('tilt')}) "
                    f"score={narrative.get('score')}"
                )
                if narrative.get("secondary"):
                    lines.append(f"- secondary_flavor: {narrative.get('secondary')}")
            social = alert.get("social")
            if social:
                if not social.get("enabled", True):
                    lines.append(f"- social: disabled ({social.get('reason')})")
                else:
                    lines.append(
                        f"- social: heat={social.get('heat')} score={social.get('score')} "
                        f"x_posts={social.get('x_posts')} authors={social.get('unique_authors')} "
                        f"cache={social.get('cache')}"
                    )
                    if social.get("watched_account_hits"):
                        lines.append(f"- social_watched_hits: {social['watched_account_hits']}")
                    if social.get("top_authors"):
                        lines.append(f"- social_top_authors: {social['top_authors'][:5]}")
                    if social.get("results"):
                        lines.append("- social_results:")
                        for item in social["results"][:5]:
                            lines.append(
                                f"  - @{item.get('author')}: {item.get('title')} {item.get('url')}"
                            )
            lines.append("- top_events:")
            for event in sorted(alert["events"], key=lambda item: item.get("sol_amount", 0.0), reverse=True)[:8]:
                lines.append(
                    f"  - {event['time']} {event['wallet_class']} "
                    f"{event['sol_amount']:.2f} SOL signer={event['signer']} "
                    f"recipient={event.get('token_recipient')}"
                )
    else:
        lines.append("## Alerts")
        lines.append("")
        lines.append("No alerts in this scan.")

    lines.append("")
    lines.append("## Active Pools")
    active = payload["active_pools"]
    if not active:
        lines.append("")
        lines.append("No classified buys above threshold.")
    for summary in active[:30]:
        pool = summary["pool"]
        lines.append(
            f"- {pool.get('symbol') or pool.get('name') or pool['pool_address']}: "
            f"{summary['classified_buys']} buys, {summary['buy_sol']:.2f} SOL, "
            f"classes={summary['classes']}, mcap=${pool.get('mcap_usd'):.0f}, "
            f"pool={pool['pool_address']}"
        )
    atomic_write_text(REPORT_PATH, "\n".join(lines) + "\n")


def ath_filter_log_message(label, kept_pools, input_pools, max_ratio):
    if max_ratio is None:
        return (
            f"{label}: ATH context enriched for {kept_pools}/{input_pools} pools "
            "(correction gate disabled)"
        )
    return (
        f"{label}: ATH correction filter kept {kept_pools}/{input_pools} pools "
        f"(max current/ATH {float(max_ratio) * 100:.0f}%)"
    )


def scan_with_config(http, rpc, state, config, base_universe=None):
    label = config.get("lane") or config.get("mode") or "scan"
    if base_universe is None:
        print(f"Building market universe for {label}...", flush=True)
        discovered = discover_market_pools(http, config)
        registry, registry_stats = refresh_known_market_pools(http, state, config)
        universe = filter_universe_pools(merge_market_pools(registry, discovered), config)
        config["_discovery_stats"] = {
            "discovered_pools": len(discovered),
            "registry_pools": len(registry),
            "merged_filtered_pools": len(universe),
            "registry_refresh": registry_stats,
        }
    else:
        universe = filter_universe_pools(base_universe, config)
        print(f"{label}: filtered {len(universe)} pools from shared discovery universe", flush=True)
    deleted_tokens = load_deleted_tokens()
    observed_at = utc_now().isoformat().replace("+00:00", "Z")
    before_ath_filter = len(universe)
    universe = filter_reactivation_by_ath(http, state, universe, config, observed_at)
    ath_filter_stats = config.get("_ath_filter_stats") or {}
    if ath_filter_stats:
        print(
            ath_filter_log_message(
                label,
                len(universe),
                before_ath_filter,
                config.get("ath_max_current_ratio"),
            ),
            flush=True,
        )
    before_deleted_filter = len(universe)
    universe = [pool for pool in universe if not pool_is_deleted(pool, deleted_tokens)]
    deleted_skipped = before_deleted_filter - len(universe)
    config["_deleted_tokens_skipped"] = deleted_skipped
    if deleted_skipped:
        print(f"{label}: skipped {deleted_skipped} deleted pools before on-chain scan", flush=True)
    attach_reactivation_baselines(universe, state, config, observed_at)
    config["_discovery_queue_stats"] = {
        "queued_tokens": len(state.get("discovery_queue") or []),
        "confirmed_tokens": sum(
            1
            for item in state.get("discovery_queue") or []
            if isinstance(item, dict) and item.get("reactivation_confirmed")
        ),
    }
    existing_pool_addresses = {pool.pool_address for pool in universe}
    thesis_monitor_pools = [
        pool
        for pool in due_signal_thesis_monitor_pools(state, config)
        if pool.pool_address not in existing_pool_addresses
        and not pool_is_deleted(pool, deleted_tokens)
    ]
    if thesis_monitor_pools:
        attach_reactivation_baselines(
            thesis_monitor_pools,
            state,
            config,
            observed_at,
        )
        universe.extend(thesis_monitor_pools)
    scan_targets, selection_stats = select_scan_targets(universe, state, config)
    selection_stats["thesis_monitor_universe"] = len(thesis_monitor_pools)
    config["_selection_stats"] = selection_stats
    print(f"{label}: universe {len(universe)} pools, scanning {len(scan_targets)}", flush=True)
    classification_budget = {
        "remaining": int(config["max_wallet_classifications_per_scan"]),
        "deep_audits_remaining": int(config.get("helius_deep_audit_max_pools_per_scan", 0)),
    }

    all_alerts = []
    summaries = []
    for index, pool in enumerate(scan_targets, start=1):
        history_mode = rpc.available_history_mode()
        if not history_mode:
            deferred_pools = scan_targets[index - 1 :]
            retry_minutes = max(
                5.0,
                float(config.get("signal_data_retry_minutes", 15)),
            )
            retry_at = iso(time.time() + retry_minutes * 60)
            for deferred_pool in deferred_pools:
                deferred_state = state.setdefault("pools", {}).setdefault(
                    deferred_pool.pool_address,
                    {},
                )
                deferred_state["signal_recheck_due_at"] = retry_at
                deferred_state["rpc_budget_deferred_at"] = observed_at
                deferred_state["force_enhanced_next_scan"] = True
            selection_stats["rpc_budget_deferred"] = len(deferred_pools)
            print(
                f"warn: {label}: deferred {len(deferred_pools)} pools after all "
                "transaction-history routes reached their scan budget",
                file=sys.stderr,
                flush=True,
            )
            break

        pool_config = dict(reactivation_stage_config(pool, config))
        if history_mode == "standard":
            pool_config["helius_transactions_enabled"] = False
        stage = pool_config.get("reactivation_stage")
        stage_label = f" [{stage}]" if stage else ""
        print(
            f"{label}: scanning {index}/{len(scan_targets)} {pool.symbol}{stage_label}",
            flush=True,
        )
        pool_state = state.setdefault("pools", {}).setdefault(pool.pool_address, {})
        try:
            alerts, summary = scan_pool(
                rpc,
                pool,
                pool_config,
                state,
                classification_budget,
            )
        except Exception as exc:
            print(
                f"warn: {label}: pool scan failed for {pool.symbol} "
                f"{pool.pool_address}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            alerts = []
            summary = {
                "pool": pool.as_dict(),
                "lane": label,
                "trade_source": "scan_pool",
                "scan_failed": True,
                "error": str(exc),
                "new_signatures": 0,
                "classified_buys": 0,
                "classes": {},
                "buy_sol": 0.0,
            }
            if isinstance(exc, WaveDataUnavailable):
                retry_minutes = max(
                    5.0,
                    float(pool_config.get("signal_data_retry_minutes", 15)),
                )
                pool_state["signal_recheck_due_at"] = iso(
                    time.time() + retry_minutes * 60
                )
                pool_state["force_enhanced_next_scan"] = True
                summary["data_unavailable"] = True
        if stage:
            summary["reactivation_stage"] = stage
        if summary.get("error") or summary.get("scan_failed"):
            pool_state["last_scan_failed_at"] = observed_at
            pool_state["last_scan_error"] = str(summary.get("error") or "pool scan failed")[:500]
        else:
            pool_state["last_scanned_at"] = observed_at
            pool_state.pop("last_scan_failed_at", None)
            pool_state.pop("last_scan_error", None)
        summaries.append(summary)
        all_alerts.extend(alerts)
        if summary.get("error") or summary.get("scan_failed"):
            print(
                f"warn: {label}: {pool.symbol or pool.pool_address} scan returned "
                f"{str(summary.get('error') or 'pool scan failed')[:500]}",
                file=sys.stderr,
                flush=True,
            )
        if getattr(rpc, "circuit_open_reason", None):
            print(
                f"warn: {label}: stopping lane after all RPC providers became unavailable: "
                f"{rpc.circuit_open_reason}",
                file=sys.stderr,
                flush=True,
            )
            break
        if index % 25 == 0:
            print(f"{label}: scanned {index}/{len(scan_targets)} pools", flush=True)
        time.sleep(0.05)

    selection_stats["completed"] = len(summaries)
    all_alerts = enrich_alerts_with_social(http, all_alerts, config, state)
    return universe, summaries, all_alerts


def run_once(config, lane_name=None):
    load_env()
    config["_gmgn_token_info_cache"] = {}
    config["_gmgn_profile_cache"] = {}
    config["_gmgn_ath_result_cache"] = {}
    http = Http()
    rpc = build_rpc_router(config)
    config["_rpc_router"] = rpc
    health = rpc.health()
    config["_rpc_providers"] = rpc.provider_stats()
    if health != "ok":
        raise SystemExit(f"Solana RPC health is not ok: {health}")

    state = migrate_scanner_state(
        load_json(STATE_PATH, {"pools": {}, "wallet_cache": {}})
    )
    config["_discovery_state_merge"] = merge_discovery_state(
        state,
        load_discovery_state(),
    )
    load_remote_discovery_state(state, config)
    config["_signal_thesis_bootstrap"] = bootstrap_signal_theses(
        state,
        load_alert_history(),
        config,
    )
    lane_list = selected_lanes(config, lane_name) if config.get("lanes") else []
    if not lane_list:
        lane_list = [None]
    shared_universe = None
    if config.get("unified_discovery_enabled", True) and len(lane_list) > 1:
        discovery_config = discovery_config_for_lanes(config, lane_list)
        print("Building shared market discovery universe...", flush=True)
        discovered_universe = discover_market_pools(http, discovery_config)
        for key in ("_gmgn_error",):
            if discovery_config.get(key):
                config[key] = discovery_config[key]
        registry_universe, registry_stats = refresh_known_market_pools(http, state, discovery_config)
        shared_universe = merge_market_pools(registry_universe, discovered_universe)
        shared_universe = filter_universe_pools(shared_universe, discovery_config)
        config["_discovery_stats"] = {
            "discovered_pools": len(discovered_universe),
            "registry_pools": len(registry_universe),
            "merged_filtered_pools": len(shared_universe),
            "registry_refresh": registry_stats,
        }
        print(
            f"shared discovery: {len(shared_universe)} candidate pools "
            f"({len(discovered_universe)} live, {len(registry_universe)} registry)",
            flush=True,
        )
    all_alerts = []
    summaries = []
    universe = []
    lane_stats = {}
    lane_configs = {}
    for lane in lane_list:
        lane_config = apply_lane(config, lane) if lane else config
        lane_key = lane_config.get("lane") or lane_config.get("mode") or "scan"
        lane_configs[lane_key] = dict(lane_config)
        lane_universe, lane_summaries, lane_alerts = scan_with_config(
            http,
            rpc,
            state,
            lane_config,
            base_universe=shared_universe,
        )
        all_alerts.extend(lane_alerts)
        for key in ("_gmgn_error", "_discovery_stats"):
            if lane_config.get(key):
                config[key] = lane_config[key]
        summaries.extend(lane_summaries)
        universe.extend(lane_universe)
        lane_stats[lane_key] = {
            "universe_pools": len(lane_universe),
            "scanned_pools": len(lane_summaries),
            "alerts": len(lane_alerts),
            "mcap_min_usd": lane_config["mcap_min_usd"],
            "mcap_max_usd": lane_config["mcap_max_usd"],
            "liquidity_min_usd": lane_config["liquidity_min_usd"],
            "age_min_hours": lane_config.get("age_min_hours"),
            "age_max_hours": lane_config.get("age_max_hours"),
            "volume_1h_min_usd": lane_config.get("volume_1h_min_usd"),
            "volume_1h_max_usd": lane_config.get("volume_1h_max_usd"),
            "volume_1h_to_mcap_min": lane_config.get("volume_1h_to_mcap_min"),
            "volume_1h_to_liquidity_min": lane_config.get("volume_1h_to_liquidity_min"),
            "ath_max_current_ratio": lane_config.get("ath_max_current_ratio"),
            "ath_require_trusted": lane_config.get("ath_require_trusted"),
            "ath_filter_stats": lane_config.get("_ath_filter_stats"),
            "deleted_tokens_skipped": lane_config.get("_deleted_tokens_skipped", 0),
            "selection": lane_config.get("_selection_stats", {}),
        }
        if rpc.circuit_open_reason:
            break

    config["_rpc_retries"] = dict(rpc.retries)
    config["_rpc_failures"] = dict(rpc.failures)
    config["_rpc_failovers"] = dict(rpc.route_failovers)
    config["_rpc_estimated_credits"] = int(rpc.estimated_credits)
    config["_rpc_providers"] = rpc.provider_stats()
    scan_health = build_scan_health(summaries, lane_stats, config)
    config["_scan_health"] = scan_health
    if (
        scan_health["status"] == "unhealthy"
        and config.get("scan_health_reject_unhealthy_snapshot", True)
    ):
        raise RuntimeError("unhealthy scan rejected: " + "; ".join(scan_health["reasons"]))

    generated_at = utc_now().isoformat().replace("+00:00", "Z")
    record_market_observations(state, universe, generated_at)
    refreshed_caught_pools = refresh_caught_market_observations(http, state, all_alerts, config, generated_at)
    enrich_market_ath(http, state, [*universe, *refreshed_caught_pools], all_alerts, config, generated_at)
    refresh_alert_tiers_with_market_ath(state, all_alerts, config)
    record_alert_observations(state, all_alerts)
    outcome_alerts = compact_alert_history(
        load_alert_history(),
        all_alerts,
        config,
    )
    update_signal_outcomes(state, outcome_alerts, generated_at, config)
    record_market_activity_baselines(
        state,
        [*universe, *refreshed_caught_pools],
        generated_at,
        config,
    )
    sync_remote_discovery_state(
        state,
        [*universe, *refreshed_caught_pools],
        config,
        generated_at,
    )
    config["_scan_health"] = build_scan_health(summaries, lane_stats, config)
    prune_wallet_cache(state, config)
    compact_state(state, universe, all_alerts, config, generated_at)
    save_runtime_state(state, config, "deep_scan", generated_at)
    write_alerts(all_alerts, config)
    report_config = report_config_for_lanes(config, lane_configs)
    report_payload = build_report_payload(
        universe,
        summaries,
        all_alerts,
        rpc.calls,
        report_config,
        generated_at,
        state,
    )
    report_payload["lane_stats"] = lane_stats
    report_payload["lanes_scanned"] = list(lane_stats)
    write_report_json(report_payload)
    write_dashboard_fallback(report_payload, state, config)
    render_report(report_payload)
    sync_remote_snapshot(report_payload, state, config)

    print(f"Solana RPC: {health}")
    print(f"RPC providers: {', '.join(rpc.providers)}")
    print(f"Universe pools: {len(universe)}")
    print(f"Scanned pools: {len(summaries)}")
    print(f"Alerts: {len(all_alerts)}")
    print(f"Report: {REPORT_PATH}")
    print(f"Report JSON: {REPORT_JSON_PATH}")
    print(f"Dashboard fallback: {DASHBOARD_FALLBACK_PATH}")
    print(f"RPC calls: {dict(rpc.calls)}")


def discovery_pulse_config(config):
    lane_config = (
        apply_lane(config, "reactivation")
        if "reactivation" in (config.get("lanes") or {})
        else dict(config)
    )
    lane_config["registry_refresh_max_tokens"] = int(
        config.get(
            "discovery_pulse_registry_refresh_max_tokens",
            lane_config.get("registry_refresh_max_tokens", 300),
        )
    )
    lane_config["gecko_pages"] = int(
        config.get(
            "discovery_pulse_gecko_pages",
            min(1, int(lane_config.get("gecko_pages", 1))),
        )
    )
    lane_config["gmgn_trenches_queries"] = config.get(
        "discovery_pulse_gmgn_trenches_queries",
        [
            {"sort_by": "swaps_1h", "direction": "desc"},
            {"sort_by": "usd_market_cap", "direction": "asc"},
        ],
    )
    lane_config["gmgn_trending_queries"] = config.get(
        "discovery_pulse_gmgn_trending_queries",
        [
            {"order_by": "volume", "intervals": ["1m", "5m"]},
            {"order_by": "swaps", "intervals": ["1m", "5m"]},
        ],
    )
    return lane_config


def run_discovery_once(config):
    load_env()
    http = Http()
    state = load_discovery_state()
    load_remote_discovery_state(state, config)
    lane_config = discovery_pulse_config(config)
    observed_at = utc_now().isoformat().replace("+00:00", "Z")
    discovered = discover_market_pools(http, lane_config)
    registry, registry_stats = refresh_known_market_pools(
        http,
        state,
        lane_config,
    )
    universe = filter_universe_pools(
        merge_market_pools(registry, discovered),
        lane_config,
    )
    deleted = load_deleted_tokens()
    universe = [pool for pool in universe if not pool_is_deleted(pool, deleted)]
    attach_reactivation_baselines(
        universe,
        state,
        lane_config,
        observed_at,
    )
    queue_stats = update_discovery_queue(
        state,
        universe,
        lane_config,
        observed_at,
    )
    record_market_observations(state, universe, observed_at)
    baseline_stats = record_market_activity_baselines(
        state,
        universe,
        observed_at,
        lane_config,
    )
    remote_stats = sync_remote_discovery_state(
        state,
        universe,
        lane_config,
        observed_at,
    )
    compact_state(state, universe, [], lane_config, observed_at)
    save_discovery_state(state, lane_config, observed_at)
    payload = {
        "generated_at": observed_at,
        "discovered_pools": len(discovered),
        "registry_pools": len(registry),
        "universe_pools": len(universe),
        "registry_refresh": registry_stats,
        "queue": queue_stats,
        "baselines": baseline_stats,
        "remote": remote_stats,
        "gmgn_status": "error" if lane_config.get("_gmgn_error") else "ok",
    }
    status = write_discovery_status("ok", payload)
    sync_remote_discovery_status(status, lane_config)
    print(
        "Discovery pulse: "
        f"{len(discovered)} live, {len(registry)} registry, "
        f"{len(universe)} eligible, {queue_stats['queued_tokens']} queued"
    )
    return payload


def main():
    parser = argparse.ArgumentParser(description="Solana token reactivation radar.")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit.")
    parser.add_argument("--watch", action="store_true", help="Run forever on scan_interval_seconds.")
    parser.add_argument(
        "--discovery-only",
        action="store_true",
        help="Refresh market baselines and the priority queue without RPC scanning.",
    )
    parser.add_argument("--mode", choices=["aggressive", "balanced", "conservative"], help="Scan profile.")
    parser.add_argument(
        "--lane",
        choices=["all", "reactivation"],
        help="Lane profile. Reactivation is the only enabled production lane.",
    )
    args = parser.parse_args()

    config = load_json(CONFIG_PATH if CONFIG_PATH.exists() else DEFAULT_CONFIG_PATH, {})
    if args.mode and not args.lane:
        config = apply_mode(config, args.mode)
        config.pop("lanes", None)
        config.pop("lane_order", None)
        config.pop("lane", None)
    else:
        config["lane"] = args.lane or config.get("lane") or "all"
    if args.discovery_only:
        write_discovery_status("running")
        try:
            run_discovery_once(config)
        except (Exception, SystemExit) as exc:
            status = write_discovery_status("failed", error=exc)
            sync_remote_discovery_status(status, config)
            raise
        return
    if not args.once and not args.watch:
        args.once = True

    while True:
        write_scanner_status("running")
        try:
            run_once(config, args.lane)
        except (Exception, SystemExit) as exc:
            rpc = config.get("_rpc_router")
            if isinstance(rpc, RoutedSolanaRpc):
                config["_rpc_providers"] = rpc.provider_stats()
                scan_health = dict(config.get("_scan_health") or {})
                scan_health.setdefault("status", "unhealthy")
                scan_health.setdefault("reasons", [str(exc)[:300]])
                scan_health["rpc_providers"] = config["_rpc_providers"]
                config["_scan_health"] = scan_health
            status = write_scanner_status(
                "failed",
                error=exc,
                scan_health=config.get("_scan_health"),
            )
            sync_remote_scan_status(status, config)
            raise
        status = write_scanner_status("ok", scan_health=config.get("_scan_health"))
        sync_remote_scan_status(status, config)
        if not args.watch:
            break
        time.sleep(int(config["scan_interval_seconds"]))


if __name__ == "__main__":
    main()
