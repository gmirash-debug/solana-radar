"""Fixed, resumable history ranges for GMGN candidates (inclusive seconds)."""
import copy


VERSION = 2


def merge_ranges(ranges):
    merged = []
    for start, end in sorted((int(a), int(b)) for a, b in ranges if int(b) >= int(a)):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def missing_ranges(ranges, start, end):
    start, end = int(start), int(end)
    missing = []
    for lower, upper in merge_ranges(ranges):
        if upper < start:
            continue
        if lower > end:
            break
        if lower > start:
            missing.append([start, min(end, lower - 1)])
        start = max(start, upper + 1)
    if start <= end:
        missing.append([start, end])
    return missing


def missing_seconds(ranges, start, end):
    return sum(b - a + 1 for a, b in missing_ranges(ranges, start, end))


def reconcile_tasks(tasks, covered, start, end):
    result = []
    for lower, upper in missing_ranges(covered, start, end):
        # Cursors are valid only with the exact original provider and filters.
        matching = next((task for task in tasks if int(task.get("from") or 0) == lower
                         and int(task.get("remaining_to", task.get("to") or 0)) == upper), None)
        result.append(copy.deepcopy(matching) if matching else
                      {"from": lower, "to": upper, "remaining_to": upper})
    return result


def read_range(fetch_page, next_provider, task, pages, limit, history_task):
    """A last page's second remains uncovered: other transactions can share it."""
    current = copy.deepcopy(task)
    cursor, provider = current.get("cursor"), current.get("provider")
    excluded, transactions, covered = set(), [], []
    stats = {"name": "live_head" if history_task == "live" else "rolling_backlog",
             "history_task": history_task, "pages": 0, "transactions": 0,
             "providers_used": [], "provider_failovers": [], "coverage_complete": False}
    remaining_to = int(current.get("remaining_to", current["to"]))
    if history_task == "archive" and provider and next_provider:
        preferred = next_provider(set(), history_task)
        if preferred and preferred != provider:
            stats["archive_handoff"] = {"from": provider, "to": preferred}
            provider, cursor = preferred, None
            current["to"] = remaining_to
    while stats["pages"] < pages:
        try:
            result = fetch_page(limit=limit, pagination_token=cursor, provider_name=provider,
                                excluded_providers=excluded, history_task=history_task,
                                sort_order="desc", block_time={"gte": current["from"], "lte": current["to"]})
            actual_provider = str(result.get("_provider") or provider or "helius")
            provider = actual_provider
            batch = result.get("data")
            if not isinstance(batch, list):
                raise ValueError("invalid indexed history page")
            times = [int(tx.get("blockTime") or 0) for tx in batch]
            if any(not current["from"] <= at <= current["to"] or at <= 0 for at in times):
                raise ValueError("history page has missing or out-of-range timestamps")
            if any(not ((tx.get("transaction") or {}).get("signatures") or [None])[0] for tx in batch):
                raise ValueError("history page has missing transaction signatures")
            new_cursor = result.get("paginationToken")
        except (AssertionError, KeyError, TypeError, AttributeError):
            raise
        except Exception as exc:
            if provider:
                excluded.add(provider)
            replacement = next_provider(excluded, history_task) if next_provider else None
            if not replacement:
                stats["error"] = str(exc)[:300]
                break
            stats["provider_failovers"].append({"from": provider, "to": replacement,
                                                "reason": str(exc)[:200]})
            # Restart only the unread part; never send another host's opaque cursor.
            provider, cursor = replacement, None
            current["to"] = remaining_to
            continue
        provider = actual_provider
        if provider not in stats["providers_used"]:
            stats["providers_used"].append(provider)
        stats["pages"] += 1
        stats["transactions"] += len(batch)
        stats["page_limit"] = result.get("_page_limit", limit)
        transactions.extend(batch)
        if times:
            stats["oldest_block_time"] = min(min(times), stats.get("oldest_block_time", min(times)))
            stats["newest_block_time"] = max(max(times), stats.get("newest_block_time", max(times)))
        if not new_cursor:
            covered.append([current["from"], current["to"]])
            stats["coverage_complete"] = True
            cursor = None
            break
        if times:
            oldest = min(times)
            if oldest < remaining_to:
                covered.append([oldest + 1, remaining_to])
                remaining_to = oldest
        if not batch or new_cursor == cursor:
            # A stalled cursor is retried as a bounded unread range, not forever.
            stats["cursor_stalled"] = True
            cursor = None
            current["to"] = remaining_to
            break
        cursor = new_cursor
    current.update(cursor=cursor, provider=provider, remaining_to=remaining_to)
    stats.update(pagination_remaining=not stats["coverage_complete"], pagination_token=cursor,
                 pagination_provider=provider, provider=provider, truncated=not stats["coverage_complete"],
                 target_from=current["from"], target_to=current["to"])
    return transactions, covered, None if stats["coverage_complete"] else current, stats


def collect_history(pool_state, now, start, fetch_page, next_provider, *, live_limit=100,
                    archive_limit=1000, head_pages=1, repair_pages=1, overlap=30,
                    head_only=False, reuse_head=False, priority_window=None):
    covered = merge_ranges(pool_state.get("candidate_covered_ranges") or [])
    tasks = copy.deepcopy(pool_state.get("helius_rolling_backlogs") or [])
    migrated = pool_state.get("candidate_pool_history_version") != VERSION
    if migrated:
        # Legacy cursors lack a frozen upper bound. Replay once rather than guess it.
        tasks = []
    observed = int(pool_state.get("candidate_history_head_observed_to") or
                   (max((b for a, b in covered), default=0) if migrated else 0))
    if reuse_head and observed:
        now = min(now, observed)
    live_from = (int(pool_state.get("candidate_history_live_from") or start) if reuse_head else
                 max(start, observed - max(0, overlap)) if observed else start)
    initial_to = int(pool_state.get("candidate_history_initial_to") or now)
    stats = {"source": "enhanced_transactions", "coverage_version": VERSION, "observed_to": now,
             "live_from": live_from, "had_previous_state": bool(observed), "passes": [],
             "pages": 0, "transactions": 0, "providers_used": [], "provider_failovers": [],
             "rolling_backlog_segments_before": len(tasks), "live_resumed": bool(tasks),
             "history_cursor_migrated": migrated, "repair_errors": 0}
    transactions = []
    if not reuse_head:
        head, prefix, pending, result = read_range(fetch_page, next_provider,
            {"from": live_from, "to": now, "remaining_to": now}, head_pages, live_limit, "live")
        if result.get("error") and not result["pages"]:
            raise RuntimeError(result["error"])
        transactions.extend(head)
        covered = merge_ranges([*covered, *prefix])
        if pending:
            tasks.append(pending)
        stats["passes"].append(result)
        observed = now
    tasks = reconcile_tasks(tasks, covered, start, now)
    if tasks and not head_only:
        selected = 0
        if priority_window and priority_window[0] and priority_window[1] >= priority_window[0]:
            selected = next((i for i, task in enumerate(tasks)
                             if task["from"] <= priority_window[1]
                             and task["remaining_to"] >= priority_window[0]), 0)
        task = tasks[selected]
        stats["repair_priority"] = "signal_window" if selected or priority_window and (
            task["from"] <= priority_window[1] and task["remaining_to"] >= priority_window[0]) else "oldest_gap"
        tail, prefix, pending, result = read_range(fetch_page, next_provider,
            task, repair_pages, archive_limit, "archive")
        transactions.extend(tail)
        covered = merge_ranges([*covered, *prefix])
        tasks[selected:selected + 1] = [pending] if pending else []
        stats["passes"].append(result)
        stats["repair_errors"] = int(bool(result.get("error")))
        tasks = reconcile_tasks(tasks, covered, start, now)
    # Keep storage bounded without claiming forgotten covered spans are complete.
    if len(covered) > 128:
        covered = [covered[0], *covered[-127:]]
        tasks = reconcile_tasks(tasks, covered, start, now)
        stats["coverage_ranges_compacted"] = True
    if len(tasks) > 128:
        oldest = tasks[0]
        last_merged = tasks[-128]
        tasks = [{"from": oldest["from"], "to": last_merged["remaining_to"],
                  "remaining_to": last_merged["remaining_to"]}, *tasks[-127:]]
        stats["rolling_backlog_segments_compacted"] = True
    gap = missing_seconds(covered, live_from, now)
    stats.update(coverage_ranges=covered, live_truncated=bool(gap), live_window_complete=not bool(gap),
                 history_gap_seconds=gap, rolling_gap_pending=bool(tasks),
                 initial_history_pending=bool(missing_ranges(covered, start, initial_to)),
                 extended_history_pending=bool(missing_ranges(covered, start, live_from - 1)),
                 backfill_pending=bool(tasks), rolling_backlog_segments_after=len(tasks),
                 truncated=bool(tasks), full_history_complete=not bool(tasks))
    for result in stats["passes"]:
        stats["pages"] += result["pages"]
        stats["transactions"] += result["transactions"]
        stats["provider_failovers"].extend(result["provider_failovers"])
        for provider in result["providers_used"]:
            if provider not in stats["providers_used"]:
                stats["providers_used"].append(provider)
    times = [int(tx["blockTime"]) for tx in transactions]
    previous_head = int(pool_state.get("candidate_history_head_block_time") or 0)
    stats["live_newest_block_time"] = max([*times, previous_head])
    stats["live_oldest_block_time"] = min(times) if times else None
    if stats["live_newest_block_time"]:
        stats["live_head_lag_seconds"] = max(0, now - stats["live_newest_block_time"])
    pool_state.update(candidate_pool_history_version=VERSION, candidate_history_initial_to=initial_to,
                      candidate_history_head_observed_to=observed,
                      candidate_history_live_from=live_from,
                      candidate_history_head_block_time=stats["live_newest_block_time"],
                      candidate_covered_ranges=covered)
    contiguous = next((b for a, b in covered if a <= start <= b), 0)
    if contiguous:
        pool_state["candidate_history_complete_to"] = contiguous
    else:
        pool_state.pop("candidate_history_complete_to", None)
    if tasks:
        pool_state["helius_rolling_backlogs"] = tasks
    else:
        pool_state.pop("helius_rolling_backlogs", None)
    for key in ("helius_live_cursor", "helius_live_pending_signature", "helius_live_pending_block_time",
                "helius_live_from", "helius_live_cursor_created_at"):
        pool_state.pop(key, None)
    signatures = set()
    unique = []
    for tx in transactions:
        signature = (tx.get("transaction") or {}).get("signatures", [""])[0]
        if signature and signature not in signatures:
            unique.append(tx)
            signatures.add(signature)
    return sorted(unique, key=lambda tx: (tx["blockTime"], tx.get("transactionIndex") or 0)), stats
