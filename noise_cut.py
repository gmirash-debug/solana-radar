"""Phase-0 noise cut: coordination is risk evidence and risk layers gate candidates.

Every switch defaults to off, so a config without the ``noise_cut_*`` and
``gmgn_prefilter_*`` keys keeps the legacy scanner behaviour.
"""
from gmgn_context import holder_stats

VERSION = 1
COORDINATION_RISK_POINTS = {
    "common_funder": 25,
    "common_recipient": 20,
    "common_executor": 15,
    "hard_wallet_cluster": 25,
}
GATE_SUPPLY_STATUSES = ("concentrated", "unverified")
GATE_COORDINATION_CODES = ("young_low_activity",)
GMGN_PREFILTER_FIELDS = (
    "top_10_holder_rate",
    "dev_team_hold_rate",
    "fresh_wallet_rate",
    "top70_sniper_hold_rate",
    "bundler_wallet_share",
    "sniper_wallets",
)
RISK_PENALTIES = {
    "hard_wallet_cluster": "hard wallet cluster (risk)",
    "linked_wallets": "linked wallets (risk)",
    "synchronous_buys": "synchronous buys (risk)",
    "classified_wallet_evidence": "fresh-wallet cluster (risk)",
}


def coordination_as_risk(config):
    return bool((config or {}).get("noise_cut_coordination_as_risk", False))


def classified_wallets_risk_only(config):
    return bool((config or {}).get("noise_cut_classified_wallets_risk_only", False))


def risk_gates_enabled(config):
    return bool((config or {}).get("noise_cut_risk_gates_enabled", False))


def gmgn_prefilter_enabled(config):
    return bool((config or {}).get("gmgn_prefilter_enabled", False))


def coordination_risk_points(config):
    points = dict(COORDINATION_RISK_POINTS)
    points.update((config or {}).get("noise_cut_coordination_risk_points") or {})
    return {key: max(0.0, float(value or 0)) for key, value in points.items()}


def classic_coordination_risk(hard_cluster, common_funders, common_recipients, config):
    """Risk entries for the evidence ``score_events`` used to reward."""
    points = coordination_risk_points(config)
    risk = []
    if hard_cluster:
        risk.append({"code": "hard_wallet_cluster", "points": points["hard_wallet_cluster"]})
    if common_funders:
        risk.append({"code": "common_funder", "points": points["common_funder"], "groups": len(common_funders)})
    if common_recipients:
        risk.append({"code": "common_recipient", "points": points["common_recipient"], "groups": len(common_recipients)})
    return risk


def wave_coordination_penalty(alert, config):
    if not coordination_as_risk(config):
        return 0.0
    points = coordination_risk_points(config)
    return (points["common_funder"] if alert.get("common_funders") else 0.0) + (
        points["common_executor"] if alert.get("common_executors") else 0.0
    )


def signal_family(record):
    # Classic alerts carry no family; the scanner treats them as classified_wallets everywhere.
    return (record or {}).get("signal_family") or "classified_wallets"


def coordination_signals(record, code):
    signals = ((record or {}).get("coordinated_activity") or {}).get("signals") or []
    return [item for item in signals if isinstance(item, dict) and item.get("code") == code]


def gate_reasons(record, config):
    """Why an alert or thesis must not be shown as a candidate."""
    config = config or {}
    reasons = []
    if classified_wallets_risk_only(config) and signal_family(record) == "classified_wallets":
        reasons.append("classified_wallets is risk evidence only")
    if not risk_gates_enabled(config):
        return reasons
    statuses = config.get("noise_cut_gate_supply_statuses")
    statuses = GATE_SUPPLY_STATUSES if statuses is None else statuses
    supply_status = (record.get("supply_integrity") or {}).get("status")
    if supply_status and supply_status in statuses:
        reasons.append(f"supply integrity {supply_status}")
    codes = config.get("noise_cut_gate_coordination_codes")
    codes = GATE_COORDINATION_CODES if codes is None else codes
    for code in codes:
        if coordination_signals(record, code):
            reasons.append(f"{code} cohort")
    return reasons


def classified_wallet_evidence(alert):
    """Compact summary of a classified-wallet alert kept as risk evidence for its pool."""
    return {
        "hard_wallets": int(alert.get("hard_wallets") or 0),
        "support_wallets": int(alert.get("support_wallets") or 0),
        "hard_sol": float(alert.get("hard_sol") or 0),
        "common_funders": len(alert.get("common_funders") or []),
        "common_recipients": len(alert.get("common_recipients") or []),
        "window_start": alert.get("window_start"),
        "window_end": alert.get("window_end"),
    }


def attach_classified_wallet_evidence(alerts, config):
    """Turn classified-wallet alerts into risk evidence on the same pool's other alerts."""
    if not classified_wallets_risk_only(config):
        return
    classified = [alert for alert in alerts or [] if signal_family(alert) == "classified_wallets"]
    if not classified:
        return
    strongest = max(classified, key=lambda alert: (int(alert.get("hard_wallets") or 0), float(alert.get("hard_sol") or 0)))
    for alert in alerts:
        if signal_family(alert) != "classified_wallets":
            alert["classified_wallet_evidence"] = classified_wallet_evidence(strongest)


def apply_noise_cut(alert, config):
    """Record coordination risk, demote gated alerts to ``noise``. Idempotent."""
    config = config or {}
    if not (coordination_as_risk(config) or classified_wallets_risk_only(config) or risk_gates_enabled(config)):
        return alert
    flags = []
    if coordination_as_risk(config):
        flags.extend(item["code"] for item in alert.get("coordination_risk") or [] if isinstance(item, dict))
        if alert.get("common_funders") or alert.get("common_recipients") or alert.get("common_executors"):
            flags.append("linked_wallets")
        minimum = int(config.get("noise_cut_synchronous_buys_min_wallets", 5))
        synchronous = max((int(item.get("wallet_count") or 0)
                           for item in coordination_signals(alert, "synchronous_buys")), default=0)
        if minimum > 0 and synchronous >= minimum:
            flags.append("synchronous_buys")
    if classified_wallets_risk_only(config) and alert.get("classified_wallet_evidence"):
        flags.append("classified_wallet_evidence")
    flags = list(dict.fromkeys(flags))
    reasons = gate_reasons(alert, config)
    penalties = list(alert.get("quality_penalties") or [])
    for flag in flags:
        label = RISK_PENALTIES.get(flag)
        if label and label not in penalties:
            penalties.append(label)
    if reasons:
        if "risk gate" not in penalties:
            penalties.append("risk gate")
        alert["action_tier"] = "noise"
    alert["quality_penalties"] = penalties
    record = {"version": VERSION, "risk_flags": flags}
    if reasons:
        record["gate"] = {"status": "excluded", "reasons": reasons}
    alert["noise_cut"] = record
    return alert


def excluded_from_candidates(alert):
    return ((alert or {}).get("noise_cut") or {}).get("gate", {}).get("status") == "excluded"


def thesis_gate(thesis, config):
    reasons = gate_reasons(thesis or {}, config)
    return {"version": VERSION, "status": "excluded", "reasons": reasons} if reasons else None


def solana_holder_stats(data):
    """Prefilter inputs from an already fetched Solana GMGN ``token info`` payload."""
    if not isinstance(data, dict) or not isinstance(data.get("stat"), dict):
        return None
    stats = holder_stats(data)
    holders = stats["holder_count"]
    bundlers = stats["wallet_tags"]["bundler_wallets"]
    return {
        **stats["concentration"],
        "bundler_wallets": bundlers,
        "sniper_wallets": stats["wallet_tags"]["sniper_wallets"],
        "holder_count": holders,
        # Lifetime tag counts grow with trading; only the share is comparable across tokens.
        "bundler_wallet_share": bundlers / holders if holders and bundlers is not None else None,
    }


def gmgn_prefilter_reasons(stats, config):
    thresholds = (config or {}).get("gmgn_prefilter_max") or {}
    reasons = []
    for field in GMGN_PREFILTER_FIELDS:
        limit = thresholds.get(field)
        value = (stats or {}).get(field)
        if limit is None or value is None:
            continue
        if float(value) > float(limit):
            reasons.append(f"{field} {float(value):.3g} > {float(limit):.3g}")
    return reasons
