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


def list_evidence(row):
    if not isinstance(row, dict):
        return row
    result = dict(row)
    for field in ("supply_integrity", "wallet_activity", "observed_position_activity", "outflow_evidence"):
        if isinstance(row.get(field), dict):
            result[field] = fact_map(row[field])
    if isinstance(row.get("coordinated_activity"), dict):
        activity = row["coordinated_activity"]
        result["coordinated_activity"] = {**fact_map(activity),
            "signals": [fact_map(signal) for signal in activity.get("signals", []) if isinstance(signal, dict)]}
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
    details = {}
    for field, target in (("detail_signal_theses", "thesis"), ("detail_current_alerts", "current_alerts"),
                          ("detail_history", "history")):
        for row in body.get(field) or []:
            key = token_key(row)
            if not key:
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
    return summary, documents
