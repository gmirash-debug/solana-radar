"""Small ready list with immutable, generation-bound per-token evidence documents."""
import hashlib
import json


def fact_map(value):
    """Keep decision scalars and scalar maps; detailed trails live in token docs."""
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if item is None or isinstance(item, (str, int, float, bool)):
            result[key] = item
        elif isinstance(item, dict):
            result[key] = {name: fact for name, fact in item.items()
                           if fact is None or isinstance(fact, (str, int, float, bool))}
        elif key in {"flags", "limitations", "reasons", "methods", "active_signals", "signal_keys"} and isinstance(item, list):
            result[key] = [fact for fact in item if fact is None or isinstance(fact, (str, int, float, bool))]
    return result


def decision_facts(value, fields=None):
    if not isinstance(value, dict):
        return value
    selected = {key: item for key, item in value.items() if fields is None or key in fields}
    return fact_map({key: item for key, item in selected.items()
                     if key not in {"scope", "ownership", "reason", "limitations", "amount_basis", "detail"}})


def list_evidence(row):
    if not isinstance(row, dict):
        return row
    result = dict(row)
    evidence_fields = {
        "supply_integrity": {"status", "data_quality_status", "checked_at", "evidence_version",
            "token_supply", "observed_top_accounts_supply_pct", "cohort_top_holder_supply_pct"},
        "wallet_activity": {"status", "interpretation_complete", "checked_at", "wallet_coverage_pct",
            "token_coverage_pct", "wallets_checked", "wallets_total", "amounts_tokens"},
        "outflow_evidence": {"balance_check_complete", "observed_sale_transactions", "direct_transfer_transactions"},
        "observed_position_activity": {"status", "checked_at"},
    }
    for field, fields in evidence_fields.items():
        if isinstance(row.get(field), dict):
            result[field] = decision_facts(row[field], fields)
            if field == "wallet_activity" and isinstance(row[field].get("amounts_tokens"), dict):
                result[field]["amounts_tokens"] = decision_facts(row[field]["amounts_tokens"], {"sold", "transferred", "service"})
    if isinstance(row.get("signal_confirmation"), dict):
        confirmation = row["signal_confirmation"]
        result["signal_confirmation"] = decision_facts(confirmation, {"status", "version", "checked_at"})
        result["signal_confirmation"]["reasons"] = [item[:240] for item in confirmation.get("reasons", [])[:1]
                                                      if isinstance(item, str)]
    if isinstance(row.get("coordinated_activity"), dict):
        activity = row["coordinated_activity"]
        metrics = activity.get("metrics") or {}
        result["coordinated_activity"] = {
            **decision_facts(activity, {"status", "version", "checked_at"}),
            "metrics": decision_facts(metrics, {"material_pattern", "material_union_held_supply_pct",
                "max_material_group_held_supply_pct", "market_rotation_observations", "buyer_count"}),
            "signals": [decision_facts(signal, {"kind", "code", "family", "label", "supporting_only",
                         "wallet_count", "held_supply_pct"}) for signal in activity.get("signals", [])[:3]
                        if isinstance(signal, dict)]}
    return result


def list_report(report):
    result = dict(report)
    for field in ("signal_theses", "alerts"):
        result[field] = [list_evidence(row) for row in report.get(field, [])]
    evaluation = report.get("signal_evaluation")
    if isinstance(evaluation, dict):
        result["signal_evaluation"] = {key: evaluation[key] for key in
            ("mode", "counts", "horizons", "generated_at", "version") if key in evaluation}
        if isinstance(evaluation.get("horizons"), dict):
            result["signal_evaluation"]["horizons"] = {key: fact_map(value) for key, value in evaluation["horizons"].items()}
    return result


def token_key(row):
    pool = row.get("pool") or {}
    return row.get("token_address") or pool.get("token_address") or row.get("pool_address") or pool.get("pool_address")


def dashboard_documents(body):
    summary = {key: value for key, value in body.items()
               if key not in {"detail_signal_theses", "detail_current_alerts", "detail_history", "history_ledger", "_sync_progress"}}
    summary["report"] = list_report(summary.get("report") or {})
    summary["history"] = [list_evidence(row) for row in summary.get("history", [])]
    visible = set()
    for field in ("signal_theses", "alerts", "summaries"):
        visible.update(token_key(row) for row in summary["report"].get(field, []) if isinstance(row, dict))
    visible.update(token_key(row) for row in summary.get("history", []) if isinstance(row, dict))
    # Old callers that provide only a generation and details have no explicit
    # list to project. Current snapshots carry explicit signal/alert arrays.
    project_visible = any(field in (body.get("report") or {}) for field in ("signal_theses", "alerts"))
    details = {}
    for field, target in (("detail_signal_theses", "thesis"), ("detail_current_alerts", "current_alerts"),
                          ("detail_history", "history")):
        for row in body.get(field) or []:
            key = token_key(row)
            if not key or project_visible and key not in visible:
                continue
            detail = details.setdefault(key, {"token_key": key, "thesis": None, "current_alerts": [], "history": [],
                                               "market": (body.get("market") or {}).get(key), "wallet_edge": None})
            if target == "thesis":
                detail[target] = row
            else:
                detail[target].append(row)
    refs, documents = {}, []
    for key, detail in sorted(details.items()):
        data = json.dumps(detail, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
        digest = hashlib.sha256(data.encode("ascii")).hexdigest()
        refs[key] = {"id": digest, "bytes": len(data)}
        documents.append({"encoding": "json-ascii", "sha256": digest, "encoded_bytes": len(data), "data": data})
    summary["token_detail_refs"] = refs
    visible.update(details)
    summary["market"] = {key: value for key, value in (body.get("market") or {}).items() if key in visible}
    summary["runtime_list_version"] = 3
    return summary, documents
