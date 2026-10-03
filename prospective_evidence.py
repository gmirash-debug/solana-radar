"""Freeze observations at capture; never rebuild entry features from later balances."""
import hashlib
from datetime import datetime, timezone
from signal_evaluation import HORIZONS


def _ts(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else 0
    except (ValueError, TypeError): return 0


def capture_evaluation_rows(state, universe, summaries, alerts, observed_at, config_version,
                            market_snapshot, *, targeted=False, control_limit=6, max_rows=5000):
    dataset = state.setdefault("signal_evaluation_dataset", {"episodes": {}, "controls": {}})
    captured = _ts(observed_at)
    pools = {pool.token_address: pool for pool in universe if pool.token_address}
    alert_by_token = {}
    for alert in alerts:
        token = (alert.get("pool") or {}).get("token_address")
        if token and (token not in alert_by_token or float(alert.get("score") or 0) > float(alert_by_token[token].get("score") or 0)):
            alert_by_token[token] = alert
    family = next((alert.get("signal_family") for alert in alerts if alert.get("signal_family")), "reactivation_wave")

    def capture(pool, alert=None):
        entry = state.get("market", {}).get(pool.token_address, {})
        if not _ts(entry.get("latest_seen_at") or entry.get("market_snapshot_at")):
            return None
        snapshot = market_snapshot(entry, observed_at)
        if not snapshot or _ts(snapshot.get("at")) > captured or captured - _ts(snapshot.get("at")) > 3600:
            return None
        row = {"token_address": pool.token_address, "pool_address": pool.pool_address,
            "episode_id": hashlib.sha256(f"{pool.token_address}|{observed_at}".encode()).hexdigest()[:24],
            "caught_at": observed_at, "captured_at": observed_at, "caught_price_usd": snapshot.get("price_usd"),
            "caught_mcap_usd": snapshot.get("mcap_usd"), "caught_liquidity_usd": snapshot.get("liquidity_usd"),
            "caught_age_hours": max(0, (captured - pool.pair_created_at) / 3600) if pool.pair_created_at else None, "config_version": config_version,
            "strategy_version": "reactivation-prospective-v1", "signal_family": (alert or {}).get("signal_family") or family,
            "caught_score": (alert or {}).get("score"), "horizons": {}}
        row["feature_snapshot"] = {"at": observed_at, "values": {
            "volume_1h_to_mcap": float(pool.volume_1h_usd or 0) / float(pool.mcap_usd) if pool.mcap_usd else None,
            "retained_supply_pct": ((alert or {}).get("reactivation_wave") or {}).get("retained_supply_pct")}}
        return row

    for token, alert in alert_by_token.items():
        if token in dataset["episodes"] or token not in pools: continue
        row = capture(pools[token], alert)
        if row: dataset["episodes"][token] = row
    if not targeted:
        scanned = {(row.get("pool") or {}).get("token_address") for row in summaries
            if isinstance(row.get("pool"), dict) and not row.get("scan_failed") and not row.get("error")
            and row.get("detector_executed") is True and not row.get("market_snapshot_suppressed")
            and not (row.get("pool") or {}).get("market_snapshot_stale")
            and not (row.get("trade_fetch") or {}).get("market_activity_stale")
            and not row.get("parse_errors") and not row.get("classification_errors")
            and (row.get("wallet_classification_required") is False
                 or int(row.get("classified_buys") or 0) >= int(row.get("candidate_buys") or 0))
            and (row.get("trade_fetch") or {}).get("source") == "enhanced_transactions"
            and (row.get("trade_fetch") or {}).get("passes")
            and all(item.get("coverage_complete") is True for item in (row.get("trade_fetch") or {}).get("passes", []))
            and not (row.get("trade_fetch") or {}).get("live_truncated")
            and not (row.get("trade_fetch") or {}).get("rolling_gap_pending")}
        candidates = [pool for token, pool in pools.items() if token in scanned and token not in alert_by_token
                      and token not in dataset["episodes"] and token not in dataset["controls"]]
        candidates.sort(key=lambda pool: hashlib.sha256(f"{config_version}|{observed_at}|{pool.token_address}".encode()).hexdigest())
        for pool in candidates[:max(0, control_limit)]:
            row = capture(pool)
            if row:
                row["control_source"] = "fully_scanned_no_alert_at_capture"
                row.update(signal_present=False, selection_at=observed_at, selection_method="prospective_universe")
                dataset["controls"][pool.token_address] = row
    for group in ("episodes", "controls"):
        for token, row in list(dataset[group].items()):
            entry = state.get("market", {}).get(token, {})
            if not _ts(entry.get("latest_seen_at") or entry.get("market_snapshot_at")): continue
            snapshot = market_snapshot(entry, observed_at)
            if not snapshot: continue
            at, caught = _ts(snapshot.get("at")), _ts(row["caught_at"])
            if not at or at > captured: continue
            for horizon, seconds in HORIZONS.items():
                due = caught + seconds
                if horizon in row["horizons"] or at < due: continue
                row["horizons"][horizon] = {**snapshot,
                    "target_at": datetime.fromtimestamp(due, timezone.utc).isoformat().replace("+00:00", "Z"),
                    "quality_status": "complete" if at - due <= 3600 else "delayed"}
        if len(dataset[group]) > max_rows:
            rows = sorted(dataset[group].items(), key=lambda item: item[1]["caught_at"], reverse=True)
            dataset[group] = dict(rows[:max_rows])
            dataset["capacity_pruned"] = int(dataset.get("capacity_pruned", 0)) + len(rows) - max_rows
    return dataset
