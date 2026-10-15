"""Freeze observations at capture; never rebuild entry features from later balances."""
import hashlib
import math
import time
from datetime import datetime, timezone
from signal_evaluation import HORIZONS

MAX_DELAY_SECONDS = 3600
KLINE_RESOLUTION, KLINE_SECONDS = "5m", 300
KLINE_MAX_ATTEMPTS = 3
SELECTION_METHOD = "prospective_universe_complete_capture"


def _ts(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else 0
    except (ValueError, TypeError): return 0


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def kline_checkpoint(candles, due, resolution_seconds=KLINE_SECONDS, max_delay=MAX_DELAY_SECONDS):
    """First traded price observable at or after `due`; None when no trade is on time.

    A candle containing `due` is observed at its close; otherwise the first later
    candle is observed at its open. Prices are never carried across a no-trade gap."""
    rows = []
    for candle in candles or []:
        start = _positive(candle.get("time")) if isinstance(candle, dict) else None
        if start is None: continue
        start = start / 1000 if start > 10_000_000_000 else start
        if due - resolution_seconds < start <= due + max_delay:
            rows.append((start, candle))
    if not rows:
        return None
    start, candle = min(rows, key=lambda item: item[0])
    if start <= due:
        at, price = start + resolution_seconds, _positive(candle.get("close"))
    else:
        at, price = start, _positive(candle.get("open")) or _positive(candle.get("close"))
    if price is None:
        return None
    return {"at": _iso(at), "target_at": _iso(due), "price_usd": price, "quality_status": "complete",
            "source": "gmgn_kline", "resolution": KLINE_RESOLUTION}


def capture_evaluation_rows(state, universe, summaries, alerts, observed_at, config_version,
                            market_snapshot, *, targeted=False, control_limit=6, max_rows=5000,
                            kline=None, kline_max_calls=0, kline_max_seconds=120, clock=time.monotonic):
    dataset = state.setdefault("signal_evaluation_dataset", {"episodes": {}, "controls": {}})
    for group in ("episodes", "controls"):
        dataset.setdefault(group, {})
    captured = _ts(observed_at)
    stats = {"at": observed_at, "controls_captured": 0, "controls_skipped_incomplete_capture": 0,
             "snapshot_checkpoints": 0, "delayed_snapshot_checkpoints": 0, "kline_available": kline is not None,
             "kline_requests": 0, "kline_checkpoints": 0, "kline_superseded_delayed": 0,
             "kline_no_on_time_trade": 0, "kline_errors": 0, "kline_deferred": 0}
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
        for pool in candidates:
            if stats["controls_captured"] >= max(0, control_limit):
                break
            row = capture(pool)
            # A control without capture price/covariates can never enter a matched
            # price pair; the next hash-ordered candidate is selected instead.
            if not row or None in (_positive(row["caught_price_usd"]), _positive(row["caught_mcap_usd"]),
                                   _positive(row["caught_liquidity_usd"]), row["caught_age_hours"]):
                stats["controls_skipped_incomplete_capture"] += 1
                continue
            row["control_source"] = "fully_scanned_no_alert_at_capture"
            row.update(signal_present=False, selection_at=observed_at, selection_method=SELECTION_METHOD)
            dataset["controls"][pool.token_address] = row
            stats["controls_captured"] += 1

    # Controls leave GMGN attention lists, so their market entry usually stops
    # updating. A row is filled from a fresh snapshot when one is on time, else
    # from the kline once the delay window has closed; never from a later snapshot.
    pending = []
    for group in ("episodes", "controls"):
        for token, row in dataset[group].items():
            caught = _ts(row.get("caught_at"))
            if not caught: continue
            horizons = row.setdefault("horizons", {})
            fills = row.get("horizon_fill") or {}
            entry = state.get("market", {}).get(token)
            snapshot = market_snapshot(entry, observed_at) if isinstance(entry, dict) else None
            at = _ts((snapshot or {}).get("at"))
            if at > captured: at = 0
            for horizon, seconds in HORIZONS.items():
                due = caught + seconds
                if captured < due: break
                frozen = horizons.get(horizon)
                fill = fills.get(horizon) or {}
                final = fill.get("result") == "no_on_time_trade" or int(fill.get("attempts") or 0) >= KLINE_MAX_ATTEMPTS
                if frozen:
                    if (kline is not None and not final and frozen.get("quality_status") == "delayed"
                            and frozen.get("source") != "gmgn_kline"):
                        pending.append((due, group, token, horizon, None))
                    continue
                if at and due <= at <= due + MAX_DELAY_SECONDS:
                    horizons[horizon] = {**snapshot, "target_at": _iso(due), "quality_status": "complete",
                                         "source": "market_snapshot"}
                    stats["snapshot_checkpoints"] += 1
                elif captured <= due + MAX_DELAY_SECONDS:
                    continue
                elif kline is not None and not final:
                    pending.append((due, group, token, horizon, snapshot if at > due else None))
                elif at > due:
                    horizons[horizon] = {**snapshot, "target_at": _iso(due), "quality_status": "delayed",
                                         "source": "market_snapshot"}
                    stats["delayed_snapshot_checkpoints"] += 1
    pending.sort(key=lambda item: item[:4])
    deadline = clock() + max(0, kline_max_seconds)
    for index, (due, group, token, horizon, late_snapshot) in enumerate(pending):
        if stats["kline_requests"] >= max(0, kline_max_calls) or stats["kline_errors"] or clock() >= deadline:
            stats["kline_deferred"] = len(pending) - index
            break
        row = dataset[group][token]
        fill = row.setdefault("horizon_fill", {}).setdefault(horizon, {})
        fill.update(attempts=int(fill.get("attempts") or 0) + 1, at=observed_at)
        stats["kline_requests"] += 1
        try:
            candles = kline(token, KLINE_RESOLUTION, due - KLINE_SECONDS, due + MAX_DELAY_SECONDS)
            if candles is None:
                raise RuntimeError("kline unavailable")
        except Exception as exc:
            # One failure stops this run's fallback so a GMGN limit cannot cascade.
            fill.update(result="error", error=str(exc)[:160])
            stats["kline_errors"] += 1
            continue
        checkpoint = kline_checkpoint(candles, due)
        if checkpoint is None:
            fill.update(result="no_on_time_trade")
            fill.pop("error", None)
            stats["kline_no_on_time_trade"] += 1
            if late_snapshot and horizon not in row["horizons"]:
                row["horizons"][horizon] = {**late_snapshot, "target_at": _iso(due), "quality_status": "delayed",
                                            "source": "market_snapshot"}
                stats["delayed_snapshot_checkpoints"] += 1
            continue
        previous = row["horizons"].get(horizon)
        if previous:
            checkpoint["superseded"] = {"at": previous.get("at"), "quality_status": previous.get("quality_status"),
                                        "source": previous.get("source") or "market_snapshot"}
            stats["kline_superseded_delayed"] += 1
        row["horizons"][horizon] = checkpoint
        row["horizon_fill"].pop(horizon, None)
        if not row["horizon_fill"]:
            row.pop("horizon_fill")
        stats["kline_checkpoints"] += 1
    dataset.setdefault("collection", {})["targeted" if targeted else "deep"] = stats
    for group in ("episodes", "controls"):
        if len(dataset[group]) > max_rows:
            rows = sorted(dataset[group].items(), key=lambda item: item[1]["caught_at"], reverse=True)
            dataset[group] = dict(rows[:max_rows])
            dataset["capacity_pruned"] = int(dataset.get("capacity_pruned", 0)) + len(rows) - max_rows
    return dataset
