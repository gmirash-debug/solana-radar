"""Pure, descriptive coordination evidence; never ownership or trade confirmation."""
import copy
import math
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations


VERSION = 1
SERVICE_KINDS = {"service", "cex", "bridge", "router", "exchange", "relay", "lifi"}
LABELS = {
    "synchronous_buys": "Synchronous purchases",
    "young_low_activity": "Young, low-activity cohort",
    "synchronized_preparation": "Synchronized buyer preparation",
    "common_direct_funding": "Common parsed direct funding",
    "inventory_churn": "High observed inventory churn",
}
LIMITATIONS = [
    "Only the supplied original cohort is analyzed; the caller must keep it frozen.",
    "Coincidences do not establish common ownership, a confirmed bundle, or readiness.",
    "Shared services, executors, fees, slots, and equal amounts are not ownership links.",
    "An unclassified funding source may be a service, even when its transfer is verified.",
    "Funding needs a material native amount; unknown buy cost supports coincidence only, not funding-based watch.",
    "Balances do not prove sales; only supplied observed sells reduce the attribution cap.",
    "Amounts and supply must use the same token units; profiles must describe pre-buy history.",
    "First activity means first activity in complete available history, not wallet creation.",
    "Inventory churn can be ordinary trading or arbitrage; it is not wash trading or provider attribution.",
    "Net-delta parsers may omit same-transaction roundtrips; missing legs are not inferred.",
    "No pattern means no match in the checked subset, not absence outside that subset.",
]


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _integer(value):
    result = _number(value)
    return int(result) if result is not None and result.is_integer() else None


def _time(value):
    result = _number(value)
    if result is None and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            result = parsed.timestamp() if parsed.tzinfo is not None else None
        except (ValueError, OverflowError):
            return None
    if result is None or result < 0 or result >= 253402300800:
        return None
    return result


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value is not None else None


def _address(value):
    return value.strip() if isinstance(value, str) else ""


def _percent(amount, supply):
    if amount is None or supply is None:
        return None
    result = amount / supply * 100
    return result if math.isfinite(result) else None


def _total(values):
    return math.fsum(values) if values and all(value is not None for value in values) else None


def _setting(config, key, default, minimum=0, maximum=None):
    value = _number(config.get(key))
    value = default if value is None else max(minimum, value)
    return min(value, maximum) if maximum is not None else value


def _normalize_trades(buys, observed, excluded, reasons):
    duplicates = defaultdict(list)
    for raw in buys or []:
        if not isinstance(raw, dict):
            reasons.add("invalid_buy_rows")
            continue
        owner, tx, at = _address(raw.get("owner")), _address(raw.get("transaction")), _time(raw.get("timestamp"))
        if not owner or not tx or at is None or owner in excluded or (observed is not None and at > observed):
            reasons.add("invalid_or_unresolved_buy_rows")
            continue
        kind = "sell" if str(raw.get("kind", "buy")).lower() == "sell" else "buy"
        duplicates[(owner, tx, kind)].append({"owner": owner, "transaction": tx, "timestamp": at, "kind": kind,
            "bought_tokens": _number(raw.get("bought_tokens")), "sold_tokens": _number(raw.get("sold_tokens")),
            "amount_native": _number(raw.get("amount_native"))})
    normalized = []
    for key in sorted(duplicates):
        rows = duplicates[key]
        if len({row["timestamp"] for row in rows}) != 1:
            reasons.add("conflicting_duplicate_buy_times")
            continue
        row = dict(rows[0])
        # The adapter has no log key: repeated owner/tx/kind rows cannot add volume.
        bought = [item["bought_tokens"] for item in rows]
        sold = [item["sold_tokens"] for item in rows if item["sold_tokens"] is not None]
        native = [item["amount_native"] for item in rows]
        row["bought_tokens"] = min(bought) if all(value is not None for value in bought) else None
        row["sold_tokens"] = max(sold) if sold else None
        row["amount_native"] = max(native) if all(value is not None for value in native) else None
        normalized.append(row)
    return sorted(normalized, key=lambda row: (row["timestamp"], row["owner"], row["transaction"], row["kind"]))


def _holdings(positions, owners, sells, observed, max_age, reasons):
    if isinstance(positions, dict):
        rows = [dict(row, owner=owner) for owner, row in positions.items() if isinstance(row, dict)]
    else:
        rows = positions or []
    grouped = defaultdict(list)
    for row in rows:
        if isinstance(row, dict) and row.get("owner") in owners:
            grouped[row["owner"]].append(row)
    held, attribution, churn_retention = {}, {}, {}
    for owner in owners:
        candidates = grouped[owner]
        times = [_time(row.get("checked_at")) for row in candidates]
        latest = max((at for at in times if at is not None), default=None)
        selected = [row for row, at in zip(candidates, times) if at == latest]
        amounts = [_number(row.get("attributed_tokens")) for row in selected]
        attribution[owner] = amounts[0] if amounts and all(amount == amounts[0] for amount in amounts) else None
        values, upper_values = [], []
        for row in selected:
            balance, attributed = _number(row.get("current_balance")), _number(row.get("attributed_tokens"))
            if row.get("balance_verified") is not True or balance is None or attributed is None or latest is None:
                values.append(None)
                upper_values.append(None)
            elif observed is not None and not 0 <= observed - latest <= max_age:
                values.append(None)
                upper_values.append(None)
            else:
                retained = _number(row.get("retained_tokens"))
                if row.get("retained_tokens") is not None and retained is None:
                    values.append(None)
                    upper_values.append(None)
                else:
                    # An authoritative retained value already incorporates observed sells.
                    cap = attributed if retained is not None else max(0, attributed - sells.get(owner, 0))
                    values.append(min(balance, cap, retained) if retained is not None else min(balance, cap))
                    upper_values.append(min(balance, attributed))
        held[owner] = values[0] if values and all(value == values[0] for value in values) else None
        churn_retention[owner] = upper_values[0] if upper_values and all(value == upper_values[0] for value in upper_values) else None
        if held[owner] is None:
            reasons.add("missing_stale_or_conflicting_positions")
    return held, attribution, churn_retention


def _windows(rows, field, seconds):
    ordered = sorted(rows, key=lambda row: (row[field], row["owner"], row.get("transaction", "")))
    left = 0
    for right, row in enumerate(ordered):
        while row[field] - ordered[left][field] > seconds:
            left += 1
        window = ordered[left:right + 1]
        if len({item["owner"] for item in window}) >= 3:
            yield window


def _distinct_transactions(rows):
    choices = defaultdict(set)
    for row in rows:
        choices[row["owner"]].add(row["transaction"])
    assigned = {}

    def assign(owner, seen):
        for tx in sorted(choices[owner]):
            if tx in seen:
                continue
            seen.add(tx)
            if tx not in assigned or assign(assigned[tx], seen):
                assigned[tx] = owner
                return True
        return False

    for owner in sorted(choices):
        assign(owner, set())
        if len(assigned) >= 3:
            break
    return len(assigned)


def analyze_coordinated_activity(buys, profiles=None, positions=None, supply=None,
        observed_at=None, coverage=None, config=None, infrastructure_addresses=None):
    """Analyze normalized, frozen-cohort evidence without a clock, RPC, or mutation.

    ``pattern`` describes coincidences, including supporting-only ones. Materiality
    additionally requires verified holdings and intersecting independent families;
    only material funding + synchronous buys can yield ``coordination_watch``.
    Coverage accepts history_status, owner_resolution_pct, balance_coverage_pct,
    and expected_buyers; status/total_buyers are legacy aliases.
    Position checked_at and observed_at accept UNIX seconds or timezone-aware ISO.
    Sell legs are deduplicated per (owner, transaction, kind). Explicit position
    retained_tokens are already net; full position attribution overrides amounts
    on representative buys. Missing sell legs are never invented.
    Parsed profile funding_amount_native must meet the native minimum and, when
    first-buy amount_native is positive and known, the buy fraction minimum.
    Without a known positive buy cost, funding remains supporting-only evidence.
    Config: min_held_supply_pct (1), min_wallets (3), min_families (2),
    max_balance_age_seconds (3600), max_signals (12), max_members (20),
    buy_window_seconds (120), preparation_window_seconds (900),
    max_funding_lag_seconds (3600), young_max_age_days (14),
    young_max_previous_tx_count (3), min_funding_native (0.05),
    min_funding_buy_fraction (0.1). All keys are unprefixed.
    """
    config = config if isinstance(config, dict) else {}
    profiles = profiles if isinstance(profiles, dict) else {}
    supplied_coverage = coverage if isinstance(coverage, dict) else {}
    observed = _time(observed_at)
    supply = _number(supply)
    supply = supply if supply is not None and supply > 0 else None
    if isinstance(infrastructure_addresses, str):
        infrastructure_addresses = [infrastructure_addresses]
    excluded = {_address(value) for value in infrastructure_addresses or [] if _address(value)}
    reasons = set()
    if observed is None:
        reasons.add("observation_time_unavailable")
    if supply is None:
        reasons.add("total_supply_unavailable")
    trades = _normalize_trades(buys, observed, excluded, reasons)
    rows = [row for row in trades if row["kind"] == "buy"]
    by_owner = defaultdict(list)
    for row in rows:
        by_owner[row["owner"]].append(row)
    owners = sorted(by_owner)
    first = {owner: by_owner[owner][0] for owner in owners}
    bought = {owner: _total([row["bought_tokens"] for row in by_owner[owner]]) for owner in owners}
    sell_rows = defaultdict(list)
    for row in trades:
        if row["kind"] == "sell" and row["owner"] in first:
            sell_rows[row["owner"]].append(row)
    sells = {owner: math.fsum(row["sold_tokens"] for row in (sell_rows[owner] or by_owner[owner])
        if row["sold_tokens"] is not None) for owner in owners}
    held, attribution, churn_retention = _holdings(positions, owners, sells, observed,
        _setting(config, "max_balance_age_seconds", 3600), reasons)
    if supply is not None and math.fsum(value for value in held.values() if value is not None) > supply * (1 + 1e-9):
        reasons.add("held_amount_exceeds_total_supply")
        held = dict.fromkeys(owners)
        churn_retention = dict.fromkeys(owners)
    cohort_bought = {owner: attribution[owner] if attribution[owner] is not None else bought[owner] for owner in owners}
    max_members = int(_setting(config, "max_members", 20, 3, 20))
    max_signals = int(_setting(config, "max_signals", 12, 1, 12))
    buy_window = _setting(config, "buy_window_seconds", 120)
    prep_window = _setting(config, "preparation_window_seconds", 900)
    funding_lag = _setting(config, "max_funding_lag_seconds", 3600)
    young_age = _setting(config, "young_max_age_days", 14)
    young_prior = int(_setting(config, "young_max_previous_tx_count", 3))
    min_funding_native = _setting(config, "min_funding_native", 0.05)
    min_funding_fraction = _setting(config, "min_funding_buy_fraction", 0.1)
    candidates = {}

    def add(code, family, members, detail):
        members = sorted(set(members))
        if len(members) < (1 if code == "inventory_churn" else 3):
            return
        full_count = len(members)
        members = members[:max_members]
        detail = dict(detail)
        if full_count > len(members):
            detail.update(members_truncated=True, observed_wallet_count=full_count)
            reasons.add("group_members_truncated")
        key = (code, detail.get("source", ""), tuple(members))
        signal = {"code": code, "label": LABELS[code], "family": family, "detail": detail,
            "members": members, "wallet_count": len(members),
            "held_supply_pct": _percent(_total([held[owner] for owner in members]), supply),
            "supporting_only": True}
        previous = candidates.get(key)
        if previous is None or tuple(sorted(detail.items())) < tuple(sorted(previous["detail"].items())):
            candidates[key] = signal

    for window in _windows(rows, "timestamp", buy_window):
        if _distinct_transactions(window) >= 3:
            add("synchronous_buys", "temporal", [row["owner"] for row in window],
                {"buy_span_seconds": max(row["timestamp"] for row in window) - min(row["timestamp"] for row in window),
                 "distinct_transactions": len({row["transaction"] for row in window})})

    young = defaultdict(list)
    complete_profiles = 0
    service_sources = set(excluded)
    for profile in profiles.values():
        if isinstance(profile, dict) and str(profile.get("source_kind", "")).strip().lower().replace(".", "") in SERVICE_KINDS:
            service_sources.add(_address(profile.get("funding_source")))
    funding = defaultdict(list)
    material_funding_owners = set()
    for owner in owners:
        profile = profiles.get(owner)
        if not isinstance(profile, dict):
            reasons.add("pre_buy_history_incomplete")
            continue
        complete = profile.get("history_complete") is True
        complete_profiles += int(complete)
        if not complete:
            reasons.add("pre_buy_history_incomplete")
        activity, prior = _time(profile.get("first_activity_at")), _integer(profile.get("previous_tx_count"))
        buy_time = first[owner]["timestamp"]
        if complete and activity is not None and prior is not None and prior <= young_prior and 0 <= buy_time - activity <= young_age * 86400:
            day = datetime.fromtimestamp(activity, timezone.utc).date().isoformat()
            young[day].append(owner)
        source, funded = _address(profile.get("funding_source")), _time(profile.get("funding_at"))
        if profile.get("funding_verified") is True and source and source != owner and source not in service_sources \
                and funded is not None and funded < buy_time:
            amount = _number(profile.get("funding_amount_native"))
            if amount is None:
                reasons.add("funding_amount_native_unavailable")
                continue
            if amount <= 0 or amount < min_funding_native:
                continue
            buy_amount = first[owner]["amount_native"]
            fraction_checked = buy_amount is not None and buy_amount > 0
            if fraction_checked and amount < min_funding_fraction * buy_amount:
                continue
            if fraction_checked:
                material_funding_owners.add(owner)
            else:
                reasons.add("funding_buy_amount_native_unavailable")
            funding[source].append({"owner": owner, "funding_time": funded, "buy_time": buy_time,
                "transaction": first[owner]["transaction"]})
    for day, members in sorted(young.items()):
        add("young_low_activity", "age_activity", members,
            {"first_activity_utc_day": day, "max_age_days": young_age, "max_previous_tx_count": young_prior})
    for source, funded_rows in sorted(funding.items()):
        add("common_direct_funding", "funding", [row["owner"] for row in funded_rows],
            {"source": source, "source_identity": "unknown", "transfer_verified": True})
        prepared = [row for row in funded_rows if row["buy_time"] - row["funding_time"] <= funding_lag]
        for funding_window in _windows(prepared, "funding_time", prep_window):
            for window in _windows(funding_window, "buy_time", prep_window):
                add("synchronized_preparation", "funding", [row["owner"] for row in window],
                    {"source": source, "source_identity": "unknown", "transfer_verified": True,
                     "funding_span_seconds": max(row["funding_time"] for row in window) - min(row["funding_time"] for row in window),
                     "buy_span_seconds": max(row["buy_time"] for row in window) - min(row["buy_time"] for row in window),
                     "max_funding_lag_seconds": max(row["buy_time"] - row["funding_time"] for row in window)})

    churn_checked = 0
    churn_wallets = 0
    for owner in owners:
        purchases, sales = by_owner[owner], sell_rows[owner]
        retained = churn_retention[owner]
        if len(purchases) < 2 or len(sales) < 2 or retained is None:
            continue
        gross_bought = _total([row["bought_tokens"] for row in purchases])
        gross_sold = _total([row["sold_tokens"] for row in sales])
        if gross_bought is None or gross_bought <= 0 or gross_sold is None \
                or any(row["timestamp"] <= first[owner]["timestamp"] for row in sales):
            continue
        churn_checked += 1
        # Low retained inventory must not be inferred from the same gross sales test.
        if gross_sold < 0.8 * gross_bought or retained > 0.2 * gross_bought:
            continue
        churn_wallets += 1
        add("inventory_churn", "turnover", [owner], {"buy_transactions": len(purchases),
            "sell_transactions": len(sales), "gross_bought_tokens": gross_bought,
            "observed_gross_sold_tokens": gross_sold, "sold_tokens_capped_to_buys": min(gross_sold, gross_bought),
            "observed_net_tokens": max(0, gross_bought - gross_sold),
            "turnover_pct": min(gross_sold / gross_bought, 1) * 100,
            "retained_bought_pct": retained / gross_bought * 100,
            "interpretation": "Supporting warning only; trading or arbitrage may explain this pattern"})

    # Retain maximal groups per code/source, so overlapping windows do not crowd out families.
    signals = list(candidates.values())
    signals = [signal for signal in signals if not any(
        signal["code"] == other["code"] and signal["detail"].get("source") == other["detail"].get("source")
        and set(signal["members"]) < set(other["members"]) for other in signals)]
    signals.sort(key=lambda signal: (-(signal["held_supply_pct"] or 0), -signal["wallet_count"],
        signal["code"], signal["detail"].get("source", ""), tuple(signal["members"])))
    if len(signals) > max_signals:
        reasons.add("signals_truncated")
    signals = signals[:max_signals]
    for signal in signals:
        if signal["family"] == "funding":
            eligible = len(set(signal["members"]) & material_funding_owners)
            signal["detail"].update(min_funding_native=min_funding_native,
                min_funding_buy_fraction=min_funding_fraction,
                buy_fraction_checked_wallets=eligible,
                supporting_only_wallets=signal["wallet_count"] - eligible)
    min_wallets = int(_setting(config, "min_wallets", 3, 3))
    min_families = int(_setting(config, "min_families", 2, 2))
    min_held = _setting(config, "min_held_supply_pct", 1, 0)
    material = {}
    watch = False
    for group in combinations(signals, min_families):
        families = {signal["family"] for signal in group}
        if len(families) < min_families:
            continue
        members = set.intersection(*(set(signal["members"]) for signal in group))
        if "funding" in families:
            # Native-only funding cannot qualify via timing, age, or an overlapping group.
            members.intersection_update(material_funding_owners)
        members = sorted(owner for owner in members if held[owner] is not None and held[owner] > 0)
        percent = _percent(math.fsum(held[owner] for owner in members), supply)
        if len(members) < min_wallets or percent is None or percent < min_held:
            continue
        material[tuple(members)] = percent
        watch = watch or {"funding", "temporal"}.issubset(families)
        for signal in group:
            signal["supporting_only"] = False
            previous = signal["detail"].get("material_held_supply_pct", -1)
            if percent > previous:
                signal["detail"].update(material_wallet_count=len(members), material_held_supply_pct=percent)
    history_status = supplied_coverage.get("history_status", supplied_coverage.get("status", "unknown"))
    total_buyers = _integer(supplied_coverage.get("expected_buyers", supplied_coverage.get("total_buyers")))
    if total_buyers is None and history_status == "complete" and "expected_buyers" not in supplied_coverage:
        total_buyers = len(owners)
    if total_buyers is None or total_buyers < len(owners):
        reasons.add("original_buyer_count_unavailable_or_inconsistent")
        total_buyers = None
    elif total_buyers > len(owners):
        reasons.add("buyer_sample_incomplete")
    if history_status != "complete":
        reasons.add("input_coverage_not_explicitly_complete")
    owner_coverage = _number(supplied_coverage.get("owner_resolution_pct"))
    reported_balance_coverage = _number(supplied_coverage.get("balance_coverage_pct"))
    if owner_coverage is None or owner_coverage > 100:
        owner_coverage = None
        reasons.add("owner_resolution_coverage_unavailable")
    elif owner_coverage < 100:
        reasons.add("owner_resolution_incomplete")
    if reported_balance_coverage is not None and reported_balance_coverage > 100:
        reported_balance_coverage = None
        reasons.add("reported_balance_coverage_invalid")
    elif reported_balance_coverage is not None and reported_balance_coverage < 100:
        reasons.add("reported_balance_coverage_incomplete")
    known_held = [value for value in held.values() if value is not None]
    known_bought = [value for value in cohort_bought.values() if value is not None]
    if len(known_bought) != len(owners):
        reasons.add("observed_buy_amounts_incomplete")
    observed_tokens, held_tokens = _total(list(cohort_bought.values())), _total(list(held.values()))
    balance_coverage = 100 * len(known_held) / len(owners) if owners else None
    if balance_coverage is not None and reported_balance_coverage is not None:
        balance_coverage = min(balance_coverage, reported_balance_coverage)
    material_members = set().union(*(set(members) for members in material))
    material_union = math.fsum(held[owner] for owner in sorted(material_members)) if material_members else None
    metrics = {"buyer_count": len(owners), "buy_transactions": len({row["transaction"] for row in rows}),
        "sell_transactions": len({row["transaction"] for sales in sell_rows.values() for row in sales}),
        "observed_bought_tokens": observed_tokens, "observed_supply_pct": _percent(observed_tokens, supply),
        "representative_bought_tokens": _total(list(bought.values())),
        "held_tokens": held_tokens, "held_supply_pct": _percent(held_tokens, supply),
        "verified_subset_held_tokens": math.fsum(known_held) if known_held else None,
        "verified_subset_held_supply_pct": _percent(math.fsum(known_held) if known_held else None, supply),
        "material_pattern": bool(material), "material_group_count": len(material),
        "material_union_held_supply_pct": _percent(material_union, supply),
        "inventory_churn_checked_wallets": churn_checked, "inventory_churn_wallets": churn_wallets,
        "max_material_group_wallets": max((len(members) for members in material), default=0),
        "max_material_group_held_supply_pct": max(material.values(), default=None)}
    status = "not_checked" if not rows else "no_pattern_in_checked_subset" if not signals else "coordination_watch" if watch else "pattern"
    return {"version": VERSION, "status": status, "checked_at": _iso(observed),
        "ownership": "not_established", "bundle": "not_established",
        "scope": "Supplied original buyer cohort; bounded descriptive evidence only", "metrics": metrics,
        "coverage": {"status": "partial" if reasons or not owners else "complete",
            "history_status": history_status, "owner_resolution_pct": owner_coverage, "sample_buyers": len(owners),
            "total_buyers": total_buyers, "buyer_coverage_pct": 100 * len(owners) / total_buyers if total_buyers else None,
            "profiled_buyers": complete_profiles, "profile_coverage_pct": 100 * complete_profiles / len(owners) if owners else None,
            "balance_checked_buyers": len(known_held), "balance_coverage_pct": balance_coverage,
            "reasons": sorted(reasons)}, "signals": signals, "limitations": list(LIMITATIONS)}


def compact_coordinated_activity(result):
    """Copy summary facts without wallet members or funding-source addresses."""
    if not isinstance(result, dict):
        return {}
    compact = copy.deepcopy(result)
    for signal in compact.get("signals", []):
        signal.pop("members", None)
        if isinstance(signal.get("detail"), dict):
            signal["detail"].pop("source", None)
    return compact
