"""Low-cap retirement is not a sale verdict or a permanent discovery blacklist."""
import math
from datetime import datetime, timezone
from statistics import median

POLICY = {"version": 1, "mcap_usd": 20000, "below_seconds": 86400,
          "quote_max_age_seconds": 1200, "max_observation_gap_seconds": 7200,
          "recapture_min_mcap_usd": 30000, "recapture_max_mcap_usd": 500000,
          "recapture_min_growth_pct": 3}


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value > 0 else 0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def token_key(row):
    pool = row.get("pool") or {}
    return str(row.get("token_address") or pool.get("token_address") or "").removeprefix("solana:")


def signal_time(row):
    return timestamp(row.get("window_start") or row.get("signal_at") or row.get("caught_at")
                     or row.get("captured_at") or row.get("created_at"))


def current_record(row, markers):
    marker = markers.get(token_key(row))
    if not marker:
        return True
    return bool(marker.get("reactivated_at") and signal_time(row) > timestamp(marker.get("retired_at")))


def tracked_tokens(state, alerts=()):
    return {token_key(row) for row in [*alerts,
        *(entry.get("signal_thesis") or {} for entry in (state.get("pools") or {}).values()),
        *(state.get("signal_outcomes") or {}).values()] if token_key(row)}


def observe_low_caps(state, tokens, observed_at):
    now = timestamp(observed_at)
    watches = state.setdefault("token_low_cap_watch", {})
    markers = state.get("token_retirements") or {}
    actions = []
    for token in tokens:
        if token in markers and not markers[token].get("reactivated_at"):
            watches.pop(token, None)
            continue
        quote = (state.get("market") or {}).get(token) or {}
        at = timestamp(quote.get("current_market_verified_at"))
        cap = number(quote.get("latest_mcap_usd"))
        # A provider's absent/zero default is not proof that market cap is zero.
        if (quote.get("market_snapshot_stale") or not at or not cap or cap <= 0
                or not 0 <= now - at <= POLICY["quote_max_age_seconds"]):
            previous = watches.get(token) or {}
            if now - timestamp(previous.get("last_quote_at")) > POLICY["max_observation_gap_seconds"]:
                watches.pop(token, None)
            continue
        previous = watches.get(token) or {}
        previous_at = timestamp(previous.get("last_quote_at"))
        if at < previous_at:
            continue
        if cap >= POLICY["mcap_usd"]:
            watches.pop(token, None)
            continue
        if at == previous_at:
            continue
        gap = at - previous_at
        if not previous_at or gap > POLICY["max_observation_gap_seconds"]:
            previous = {"below_since": iso(at), "samples": 0, "max_gap_seconds": 0}
            gap = 0
        previous.update(last_quote_at=iso(at), mcap_usd=cap,
                        samples=previous["samples"] + 1,
                        max_gap_seconds=max(previous["max_gap_seconds"], gap))
        watches[token] = previous
        if at - timestamp(previous["below_since"]) >= POLICY["below_seconds"] and previous["samples"] >= 13:
            actions.append({"operation": "retire", "token_address": token, "retired_at": iso(now),
                            **previous})
    for token in set(watches) - set(tokens):
        watches.pop(token, None)
    return actions


def recapture_proof(alert, marker, now, price_events=None):
    cutoff = timestamp(marker.get("retired_at"))
    pool = alert.get("pool") or {}
    at = timestamp(pool.get("market_snapshot_at"))
    cap = number(pool.get("mcap_usd"))
    attention = pool.get("gmgn_attention") or {}
    attention_at = timestamp(attention.get("last_seen_at"))
    now = timestamp(now)
    if (alert.get("signal_family") != "reactivation_wave" or signal_time(alert) <= cutoff
            or not signal_time(alert) <= timestamp(alert.get("window_end")) <= now
            or pool.get("market_snapshot_stale") or not at or not 0 <= now - at <= 1200
            or cap is None or not POLICY["recapture_min_mcap_usd"] <= cap <= POLICY["recapture_max_mcap_usd"]
            or not attention.get("memberships") or not 0 <= now - attention_at <= 1800
            or (number((alert.get("wave") or {}).get("net_buy_sol")) or 0) <= 0):
        return None
    events = price_events if price_events is not None else (alert.get("coordination_events") or alert.get("events") or [])
    quotes = []
    for event in events:
        time = timestamp(event.get("block_time") or event.get("time"))
        price = number(event.get("price_native"))
        if not price:
            sol, tokens = number(event.get("sol_amount")), number(event.get("token_amount"))
            price = sol / tokens if sol and tokens and tokens > 0 else None
        if price and price > 0 and cutoff < time <= now:
            quotes.append((time, price))
    quotes.sort()
    if (len(quotes) < 4 or quotes[-2][0] <= quotes[1][0]
            or now - quotes[-2][0] > POLICY["quote_max_age_seconds"]):
        return None
    before, after = median(row[1] for row in quotes[:2]), median(row[1] for row in quotes[-2:])
    growth = (after / before - 1) * 100
    if growth < POLICY["recapture_min_growth_pct"]:
        return None
    return {"operation": "recapture", "token_address": token_key(alert),
            "retired_at": marker["retired_at"], "signal_at": iso(signal_time(alert)),
            "reactivated_at": iso(now), "mcap_usd": cap, "growth_pct": growth,
            "net_buy_sol": alert["wave"]["net_buy_sol"], "quote_at": iso(at),
            "attention_at": iso(attention_at), "price_samples": len(quotes),
            "price_start_at": iso(quotes[0][0]), "price_observed_at": iso(quotes[-2][0])}


def purge_retired_state(state):
    """Keep only tiny retirement fences; never carry old cohorts into a recatch."""
    markers = state.get("token_retirements") or {}
    inactive = {token for token, marker in markers.items() if not marker.get("reactivated_at")}
    pool_tokens = {key: token_key(entry.get("signal_thesis") or entry.get("pool") or entry)
                   for key, entry in (state.get("pools") or {}).items()}
    pool_tokens.update({entry.get("pool_address"): token for token, entry in (state.get("market") or {}).items()})
    pool_tokens.update({(entry.get("market_pool") or {}).get("pool_address"): token
                       for token, entry in (state.get("gmgn_candidates") or {}).items()})
    removed = 0
    for key, entry in list((state.get("pools") or {}).items()):
        thesis = entry.get("signal_thesis") or entry.get("pending_signal_thesis") or {}
        token = token_key(thesis) or pool_tokens.get(key)
        if token in inactive or (token in markers and thesis and not current_record(thesis, markers)):
            state["pools"].pop(key, None)
            removed += 1
    for field in ("market", "activity_baselines", "gmgn_candidates", "signal_outcomes", "token_low_cap_watch",
                  "token_account_enumerations", "social_cache", "token_intel_cache"):
        records = state.get(field)
        if not isinstance(records, dict):
            continue
        for key, row in list(records.items()):
            token = key.removeprefix("solana:") if isinstance(key, str) else token_key(row)
            old_catch = field == "market" and token in markers and row.get("first_signal_at") \
                and timestamp(row["first_signal_at"]) <= timestamp(markers[token].get("retired_at"))
            if token in inactive or old_catch or (field == "signal_outcomes" and token in markers and not current_record(row, markers)):
                records.pop(key, None)
    for group in (state.get("signal_evaluation_dataset") or {}).values():
        if not isinstance(group, dict):
            continue
        for key, row in list(group.items()):
            if isinstance(row, dict) and not current_record(row, markers):
                group.pop(key, None)
    queue = state.get("discovery_queue")
    if isinstance(queue, dict):
        for key in list(queue):
            if key in inactive or pool_tokens.get(key) in inactive:
                queue.pop(key, None)
    elif isinstance(queue, list):
        state["discovery_queue"] = [row for row in queue if token_key(row) not in inactive]
    return removed
