"""Bounded pool-launch investigation, independent of the rolling live swap buffer."""
import copy


def advance_launch_history(rpc, pool, task, parser, *, now, window_seconds=21600,
                           max_transactions=2000, max_owners=300):
    start = int(pool.pair_created_at or 0)
    if not start:
        return {"status": "unavailable", "reason": "pool_creation_time_unknown", "checked_at": now}
    end = start + max(60, int(window_seconds))
    task = copy.deepcopy(task or {})
    if task.get("from_timestamp") != start or task.get("to_timestamp") != end:
        task = {"from_timestamp": start, "to_timestamp": end, "cursor": None, "owners": {},
                "transactions": 0, "parse_errors": 0, "unresolved_sales": 0, "seen_signatures": []}
    task.update({"checked_at": now, "scope": "pool_creation_window_not_full_token_launch",
                 "owner_accounting_scope": "largest_resolved_recipient_per_transaction",
                 "status": "pending", "ownership_inferred": False})
    if task["transactions"] >= max_transactions or len(task["owners"]) >= max_owners:
        task.update(status="bounded_partial", reason="investigation_capacity_reached")
        return task
    result = rpc.transactions_for_address(pool.pool_address, limit=min(100, max_transactions - task["transactions"]),
        sort_order="asc", block_time={"gte": start, "lte": end}, pagination_token=task.get("cursor"),
        provider_name=task.get("provider"), history_task="archive")
    provider = result.get("_provider")
    if task.get("cursor") and provider != task.get("provider"):
        task.update(status="partial", reason="provider_cursor_mismatch")
        return task
    transactions = result.get("data")
    if not isinstance(transactions, list):
        raise ValueError("launch history returned no transaction array")
    seen = set(task["seen_signatures"])
    for tx in transactions:
        signatures = (tx.get("transaction") or {}).get("signatures") or []
        signature = signatures[0] if signatures else None
        timestamp = int(tx.get("blockTime") or 0)
        if not signature or not start <= timestamp <= end:
            task["parse_errors"] += 1
            continue
        if signature in seen:
            continue
        seen.add(signature)
        task["transactions"] += 1
        try:
            swap = parser(tx, pool)
        except Exception:
            task["parse_errors"] += 1
            continue
        if not swap:
            continue
        kind = swap.get("kind")
        owner = swap.get("token_recipient") if kind == "buy" else swap.get("coordination_sale_owner")
        if not owner:
            if kind == "sell": task["unresolved_sales"] += 1
            continue
        if owner not in task["owners"] and len(task["owners"]) >= max_owners:
            task.update(status="bounded_partial", reason="owner_capacity_reached")
            return task
        row = task["owners"].setdefault(owner, {"bought_tokens": 0, "observed_sold_tokens": 0, "buys": 0})
        if kind == "buy":
            row["bought_tokens"] += max(0, float(swap.get("token_recipient_amount") or swap.get("token_amount") or 0))
            row["buys"] += 1
        elif kind == "sell":
            row["observed_sold_tokens"] += max(0, float(swap.get("coordination_sale_amount") or 0))
    task["seen_signatures"] = sorted(seen)
    task["cursor"] = result.get("paginationToken")
    task["provider"] = provider
    task["provider_query_exhausted"] = not task["cursor"]
    task["coverage_complete"] = False
    task["status"] = "pending" if task["cursor"] else "query_exhausted" if task["transactions"] and not task["parse_errors"] else "partial"
    task["coverage_reason"] = "provider_retention_and_pool_launch_coverage_not_independently_verified"
    task["current_balances_verified"] = False
    return task
