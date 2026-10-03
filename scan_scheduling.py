"""Small targeted passes use the same detector and state as the hourly scan."""
from datetime import datetime


def scan_freshness_reference(report, targeted=False):
    if targeted:
        return report.get("generated_at")
    if report.get("scan_profile") == "targeted":
        return report.get("last_deep_scan_at")
    return report.get("last_deep_scan_at") or report.get("generated_at")


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else 0
    except (ValueError, TypeError):
        return 0

def targeted_profile(config):
    result = dict(config)
    result["_evaluation_config_version"] = config.get("_evaluation_config_version")
    result["_scan_profile"] = "targeted"
    overrides = {
        "active_pool_limit": int(config.get("targeted_pool_limit", 6)),
        "max_wallet_classifications_per_scan": 35,
        "signal_thesis_extra_balance_budget": 60,
        "scan_priority_share": 0.8,
        "scan_rotation_min_share": 0.2,
        "helius_initial_backfill_enabled": False,
        "helius_rolling_pages": 1,
        "helius_deep_rolling_pages": 1,
        "helius_backlog_pages": 1,
        "helius_deep_backlog_pages": 1,
        "helius_rolling_head_pages": 1,
        "helius_probe_rolling_head_pages": 1,
        "helius_deep_rolling_head_pages": 1,
        "helius_rolling_backlog_pages": 1,
        "helius_probe_rolling_backlog_pages": 1,
        "helius_deep_rolling_backlog_pages": 1,
        "helius_max_pages": 1,
        "helius_probe_max_pages": 1,
        "helius_deep_max_pages": 1,
        "alchemy_rpc_credit_budget_per_scan": 5000,
        "chainstack_rpc_credit_budget_per_scan": 2500,
        "helius_rpc_credit_budget_per_scan": 1000,
        "gmgn_ath_max_tokens_per_scan": 2,
    }
    result.update(overrides)
    result["lanes"] = {key: {**value, **overrides} for key, value in (config.get("lanes") or {}).items()}
    return result


def fast_candidate_pools(universe, state, now):
    queue = {item.get("pool_address") for item in state.get("discovery_queue", [])
             if isinstance(item, dict) and item.get("pool_address") and _timestamp(item.get("expires_at")) > now}
    selected = []
    for pool in universe:
        entry = state.get("pools", {}).get(pool.pool_address, {})
        thesis = entry.get("signal_thesis") or {}
        due = thesis.get("next_check_at") or entry.get("signal_recheck_due_at")
        if pool.pool_address in queue or (_timestamp(due) and _timestamp(due) <= now
                                         and thesis.get("status") != "invalidated"):
            selected.append(pool)
    return selected
