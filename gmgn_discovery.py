"""GMGN attention lists select tokens; pool providers only resolve their markets."""
import copy
import json
import math
import re
from datetime import datetime, timezone

INTERVALS = ("1m", "5m", "1h", "6h", "24h")
ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def attention_mode(config):
    return config.get("discovery_source_mode") == "gmgn_attention"


def stamp(value):
    try:
        if isinstance(value, (int, float)):
            return int(value / 1000 if value > 10_000_000_000 else value)
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError, OverflowError):
        return 0


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def fetch_attention(config, run):
    intervals = [item for item in config.get("gmgn_attention_intervals", INTERVALS) if item in INTERVALS]
    candidates, errors, succeeded = {}, [], []
    observed_at = iso(int(datetime.now(timezone.utc).timestamp()))
    limit = min(100, max(1, int(config.get("gmgn_trending_limit", 100))))
    order_by = config.get("gmgn_attention_order_by", "default")
    if order_by not in {"default", "volume", "swaps"}:
        raise ValueError("invalid GMGN attention ranking order")

    def add(rows, source, interval, order_by):
        for rank, item in enumerate(rows, 1):
            if not isinstance(item, dict) or item.get("chain", "sol") != "sol":
                continue
            token = str(item.get("address") or "").strip()
            if not ADDRESS.fullmatch(token):
                continue
            record = candidates.setdefault(token, {"token_address": token, "memberships": [], "first_seen_at": observed_at,
                "last_seen_at": observed_at, "metrics": {}})
            membership = {"source": source, "interval": interval, "order_by": order_by,
                "rank": int(number(item.get("rank")) or rank)}
            if membership not in record["memberships"]:
                record["memberships"].append(membership)
            record["metrics"][f"{source}:{interval}:{order_by}"] = {
                key: number(item.get(key)) for key in ("volume", "swaps", "buys", "sells", "visiting_count")
            }

    for interval in intervals:
        try:
            response = run(config, ["market", "trending", "--chain", "sol", "--interval", interval,
                "--order-by", order_by, "--limit", str(limit)], f"gmgn trending {interval}")
            data = response.get("data", response) if isinstance(response, dict) else None
            rows = data.get("rank") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise ValueError("invalid trending response")
            add(rows, "trending", interval, order_by)
            succeeded.append(f"trending:{interval}")
        except Exception as exc:
            errors.append({"source": "trending", "interval": interval, "error": str(exc)[:250]})
            if config.get("_gmgn_circuit_open"):
                break

    if not config.get("_gmgn_circuit_open"):
        try:
            params = [{"label": "hot-search", "chain": "sol", "interval": interval,
                "limit": min(500, max(1, int(config.get("gmgn_hot_searches_limit", 100))))} for interval in intervals]
            response = run(config, ["market", "hot-searches", "--params", json.dumps(params, separators=(",", ":"))],
                "gmgn hot searches all windows")
            blocks = response.get("data") if isinstance(response, dict) else response
            if not isinstance(blocks, list):
                raise ValueError("invalid hot-searches response")
            for interval in intervals:
                matching = [block for block in blocks if isinstance(block, dict) and block.get("chain") == "sol"
                    and block.get("interval") == interval]
                if not matching or not all(isinstance(block.get("tokens"), list) for block in matching):
                    errors.append({"source": "hot_searches", "interval": interval, "error": "missing or invalid result block"})
                    continue
                for block in matching:
                    add(block["tokens"], "hot_searches", interval, "visiting_count")
                succeeded.append(f"hot_searches:{interval}")
        except Exception as exc:
            errors.append({"source": "hot_searches", "error": str(exc)[:250]})

    for record in candidates.values():
        record["sources"] = sorted({item["source"] for item in record["memberships"]})
        record["in_both"] = len(record["sources"]) == 2
    return candidates, {"status": "ok" if len(succeeded) == len(intervals) * 2 else "partial" if succeeded else "unavailable",
        "observed_at": observed_at, "successful_lists": succeeded, "errors": errors,
        "tokens": len(candidates), "coverage": "bounded_gmgn_rankings_not_all_market_tokens"}


def merge_candidates(state, incoming, health, config):
    records = state.setdefault("gmgn_candidates", {})
    now = stamp(health.get("observed_at"))
    successful = set(health.get("successful_lists") or [])
    for token, previous in list(records.items()):
        if not isinstance(previous, dict):
            continue
        remaining = [item for item in previous.get("memberships", [])
            if f"{item.get('source')}:{item.get('interval')}" not in successful]
        if token not in incoming and successful:
            previous["memberships"] = remaining
            previous["sources"] = sorted({item["source"] for item in remaining})
            previous["in_both"] = len(previous["sources"]) == 2
            previous["active"] = bool(remaining)
    for token, record in incoming.items():
        previous = records.get(token) or {}
        memberships = copy.deepcopy(record.get("memberships") or [])
        memberships.extend(copy.deepcopy(item) for item in previous.get("memberships", [])
            if f"{item.get('source')}:{item.get('interval')}" not in successful
            and item not in memberships)
        records[token] = {**copy.deepcopy(record), "active": True, "memberships": memberships,
            "first_seen_at": previous.get("first_seen_at") or record["first_seen_at"]}
        for key in ("market_resolution_attempt_at", "market_pool"):
            if key in previous:
                records[token][key] = copy.deepcopy(previous[key])
        records[token]["sources"] = sorted({item["source"] for item in memberships})
        records[token]["in_both"] = len(records[token]["sources"]) == 2
    state["gmgn_discovery_health"] = copy.deepcopy(health)
    retention = max(1, int(config.get("gmgn_candidate_retention_days", 7))) * 86400
    retained = {token: record for token, record in records.items() if stamp(record.get("last_seen_at")) >= now - retention}
    cap = max(100, int(config.get("gmgn_candidate_registry_limit", 2000)))
    # Retention pruning may drop old memberships, never this successful snapshot.
    active = {token: retained[token] for token in incoming if token in retained}
    previous = sorted(((token, record) for token, record in retained.items() if token not in active),
        key=lambda item: stamp(item[1].get("last_seen_at")), reverse=True)
    state["gmgn_candidates"] = {**active, **dict(previous[:max(0, cap - len(active))])}


def current_candidates(state, config, now):
    ttl = max(1, int(config.get("gmgn_candidate_ttl_minutes", 60))) * 60
    return {token: record for token, record in (state.get("gmgn_candidates") or {}).items()
        if isinstance(record, dict) and record.get("active") is not False
        and 0 <= now - stamp(record.get("last_seen_at")) <= ttl}


def merge_discovery_candidates(state, discovery_state):
    """Membership belongs to the list snapshot, not a token's last positive sighting."""
    health = discovery_state.get("gmgn_discovery_health") or {}
    observed = stamp(health.get("observed_at"))
    previous = stamp((state.get("gmgn_discovery_health") or {}).get("observed_at"))
    if not observed or observed < previous:
        return
    candidates = state.setdefault("gmgn_candidates", {})
    incoming = discovery_state.get("gmgn_candidates") or {}
    incoming = {token: record for token, record in incoming.items() if isinstance(record, dict)}
    successful = set(health.get("successful_lists") or [])
    for token, record in candidates.items():
        if token in incoming or not isinstance(record, dict) or not successful:
            continue
        record["memberships"] = [item for item in record.get("memberships", [])
            if f"{item.get('source')}:{item.get('interval')}" not in successful]
        record["sources"] = sorted({item["source"] for item in record["memberships"]})
        record["in_both"] = len(record["sources"]) == 2
        record["active"] = bool(record["memberships"])
    for token, record in incoming.items():
        existing = candidates.get(token) or {}
        merged = copy.deepcopy(record)
        first_seen = [stamp(value) for value in (existing.get("first_seen_at"), record.get("first_seen_at"))]
        if any(first_seen):
            merged["first_seen_at"] = iso(min(value for value in first_seen if value))
        if stamp(existing.get("last_seen_at")) > stamp(record.get("last_seen_at")):
            merged["last_seen_at"] = existing["last_seen_at"]
        if stamp(existing.get("market_resolution_attempt_at")) > stamp(record.get("market_resolution_attempt_at")):
            for key in ("market_resolution_attempt_at", "market_pool"):
                if key in existing:
                    merged[key] = copy.deepcopy(existing[key])
        candidates[token] = merged
    state["gmgn_discovery_health"] = copy.deepcopy(health)


def history_plan(pool_state, config, now):
    """An unchanged ranking is not new trading evidence; probes still run periodically."""
    previous = stamp(pool_state.get("candidate_checked_at"))
    retry = stamp(pool_state.get("candidate_retry_at"))
    if retry and now < retry:
        return {"scan": False, "reason": "retry_backoff"}
    pending = bool(pool_state.get("helius_rolling_backlogs") or pool_state.get("helius_live_cursor") or pool_state.get("candidate_signature_gaps")
        or pool_state.get("force_enhanced_next_scan") or pool_state.get("candidate_history_pending"))
    interval = max(1, int(config.get("candidate_history_check_minutes", 15))) * 60
    if pending:
        interval = max(1, int(config.get("candidate_history_retry_minutes", 5))) * 60
    if previous and now - previous < interval:
        return {"scan": False, "reason": "recently_checked"}
    return {"scan": True, "reason": "history_gap_repair" if pending else "incremental" if previous else "initial"}


def initial_history_hours(attention, config):
    intervals = {item.get("interval") for item in (attention or {}).get("memberships", [])}
    hours = float(config.get("candidate_initial_history_hours", 6))
    if intervals and not intervals.intersection({"1m", "5m", "1h"}):
        hours = float(config.get("candidate_extended_history_hours", 24))
    return min(24, max(1, hours))


def record_history_check(pool_state, summary, config, now):
    trade = summary.get("trade_fetch") or {}
    failed = bool(summary.get("scan_failed") or summary.get("error") or summary.get("parse_errors")
        or trade.get("transaction_errors") or trade.get("market_activity_stale")
        or trade.get("market_activity_unverified") or trade.get("timestamp_missing"))
    history_pending = failed or bool(trade.get("live_truncated", trade.get("truncated"))
        or trade.get("rolling_gap_pending") or trade.get("history_gap_seconds") or pool_state.get("helius_rolling_backlogs")
        or pool_state.get("candidate_signature_gaps"))
    evidence_pending = bool(trade.get("evidence_pending") or summary.get("classification_errors"))
    pending = history_pending or evidence_pending
    pool_state["candidate_checked_at"] = iso(now)
    pool_state["candidate_history_pending"] = pending
    pool_state["candidate_history_gap_pending"] = history_pending
    pool_state["candidate_evidence_pending"] = evidence_pending
    if trade.get("coverage_version") == 2:
        pool_state["candidate_history_live_window_complete"] = not failed and bool(trade.get("live_window_complete"))
        pool_state["candidate_history_initial_pending"] = bool(trade.get("initial_history_pending"))
        pool_state["candidate_history_extended_pending"] = bool(trade.get("extended_history_pending"))
    elif pool_state.get("candidate_pool_history_version") == 2:
        pool_state["candidate_history_live_window_complete"] = False
    if failed:
        pool_state["candidate_retry_at"] = iso(now + max(1, int(config.get("candidate_history_retry_minutes", 5))) * 60)
    else:
        pool_state.pop("candidate_retry_at", None)
    start = int(trade.get("live_from") or 0)
    end = min(now, int(trade.get("observed_to") or now))
    if trade.get("coverage_version") != 2 and pool_state.get("candidate_pool_history_version") != 2 and not history_pending and start and end >= start:
        pool_state["candidate_history_complete_to"] = end
        ranges = pool_state.setdefault("candidate_covered_ranges", [])
        ranges.append([start, end])
        merged = []
        for a, b in sorted(ranges):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        pool_state["candidate_covered_ranges"] = merged[-24:]
    pool_state["candidate_check_scope"] = trade.get("phase") or "unknown"


def collect_signature_ranges(rpc, address, pool_state, config, now):
    """Bounded, resumable pages; a newest head never acknowledges an unfinished tail."""
    now = min(now, int(config.get("_candidate_head_observed_at") or now))
    limit = min(1000, max(10, int(config.get("candidate_signature_page_size", 100))))
    start = int(pool_state.setdefault("candidate_history_start", now - int(config.get("helius_recent_lookback_minutes",
        float(config.get("candidate_initial_history_hours", 6)) * 60)) * 60))
    previous = pool_state.get("latest_signature") or pool_state.get("rpc_latest_signature")
    gaps = copy.deepcopy(pool_state.get("candidate_signature_gaps") or [])
    head = config.get("_candidate_signature_head")
    if head is None:
        head = rpc.signatures_for_address(address, limit=limit)
    rows = []
    cutoff = max(start, int(pool_state.get("candidate_history_complete_to") or start) - 30)
    for item in head:
        if previous and item.get("signature") == previous:
            break
        rows.append(item)

    def complete(batch, stop_signature, stop_time):
        return len(batch) < limit or any(item.get("signature") == stop_signature and stop_signature
            or item.get("blockTime") is not None and int(item["blockTime"]) <= stop_time for item in batch)

    head_signature = head[0].get("signature") if head else None
    head_complete = complete(head, previous, cutoff)
    if head and not head_complete and not any(item.get("head") == head_signature for item in gaps):
        gaps.append({"head": head_signature, "before": head[-1]["signature"], "stop_signature": previous,
            "stop_time": cutoff, "from": cutoff})
    cap = max(2, int(config.get("candidate_signature_gap_limit", 12)))
    if len(gaps) > cap:
        overlapping = gaps[cap - 1:]
        replay = dict(overlapping[-1])
        replay.update(stop_time=min(int(item["stop_time"]) for item in overlapping), stop_signature=None)
        gaps = [*gaps[:cap - 1], replay]
    pages = 1
    completed_heads = set()
    if gaps:
        gap = gaps[0]
        batch = rpc.signatures_for_address(address, limit=limit, before=gap["before"])
        pages += 1
        for item in batch:
            if gap.get("stop_signature") and item.get("signature") == gap["stop_signature"]:
                break
            if item.get("blockTime") is not None and int(item["blockTime"]) < int(gap["stop_time"]):
                break
            rows.append(item)
        if complete(batch, gap.get("stop_signature"), int(gap["stop_time"])):
            completed_heads.add(gap.get("head"))
            gaps.pop(0)
        elif batch and batch[-1].get("signature") != gap["before"]:
            gap["before"] = batch[-1]["signature"]
    unique = {}
    for item in rows:
        if item.get("signature") and not item.get("err"):
            at = item.get("blockTime")
            if at is None or int(at) >= start:
                unique[item["signature"]] = item
    return list(unique.values()), {"gaps": gaps, "complete": not gaps and (head_complete or head_signature in completed_heads),
        "pages": pages, "live_from": cutoff, "head": head, "start": start, "observed_to": now,
        "timestamp_missing": any(item.get("blockTime") is None for item in unique.values())}


def unprocessed_transactions(transactions, pool_state):
    known = pool_state.get("candidate_processed_signatures") or {}
    return [tx for tx in transactions if next(iter((tx.get("transaction") or {}).get("signatures") or []), "") not in known]


def remember_transactions(transactions, pool_state, now):
    known = pool_state.setdefault("candidate_processed_signatures", {})
    for tx in transactions:
        signature = next(iter((tx.get("transaction") or {}).get("signatures") or []), "")
        if signature:
            known[signature] = int(tx.get("blockTime") or now)
    prune_processed_signatures(pool_state, now)


def prune_processed_signatures(pool_state, now):
    known = pool_state.get("candidate_processed_signatures") or {}
    pool_state["candidate_processed_signatures"] = dict(sorted(
        ((signature, at) for signature, at in known.items() if at >= now - 86400),
        key=lambda item: item[1], reverse=True)[:600])
