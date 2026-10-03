"""Small ready list with immutable, generation-bound per-token evidence documents."""
import hashlib
import json


def token_key(row):
    pool = row.get("pool") or {}
    return row.get("token_address") or pool.get("token_address") or row.get("pool_address") or pool.get("pool_address")


def dashboard_documents(body):
    summary = {key: value for key, value in body.items()
               if key not in {"detail_signal_theses", "detail_current_alerts", "detail_history", "history_ledger", "_sync_progress"}}
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
