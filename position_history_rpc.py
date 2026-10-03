"""Bounded finalized token-account history for shadow position evidence."""
from solana_position_lineage import resolve_position_history, parse_position_transaction


class PositionHistoryRpc:
    def __init__(self, rpc, *, max_pages=4):
        self.rpc = rpc
        self.remaining = max(0, int(max_pages))
        self.providers = {}
        self.boundaries = {}
        self.anchors = {}

    def history_page(self, account, *, mint, from_slot, to_slot, cursor, limit):
        if not self.remaining:
            raise RuntimeError("position_history_budget_exhausted")
        self.remaining -= 1
        provider = self.providers.get(account)
        options = {"transactionDetails": "full", "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0, "commitment": "finalized", "sortOrder": "asc",
            "limit": min(64, limit), "filters": {"status": "any", "tokenAccounts": "none",
            "slot": {"gte": from_slot, "lte": to_slot}}}
        if cursor:
            options["paginationToken"] = cursor
        archive_order = getattr(self.rpc, "archive_order", None)
        if not isinstance(archive_order, list):
            archive_order = self.rpc.enhanced_order
        result, used = self.rpc._route_call("getTransactionsForAddress", [account, options],
            preferred=provider, order=[provider] if provider else archive_order)
        if provider and used != provider:
            raise RuntimeError("position_history_provider_changed")
        self.providers[account] = used
        rows = result.get("data") if isinstance(result, dict) else None
        if not isinstance(rows, list) or len(rows) > options["limit"]:
            raise ValueError("invalid_position_history_page")
        anchor = self.anchors.setdefault(account, {"seen": False, "seed_index": None, "other_indices": [], "unsafe": False, "proof": None})
        boundary = self.boundaries.get(account)
        for tx in rows:
            if not isinstance(tx, dict) or not isinstance(tx.get("slot"), int) or not isinstance(tx.get("meta"), dict) or "err" not in tx["meta"]:
                anchor["unsafe"] = True
                continue
            if tx.get("slot") != from_slot or tx["meta"]["err"] is not None:
                continue
            signature = ((tx.get("transaction") or {}).get("signatures") or [None])[0]
            index = tx.get("transactionIndex")
            if boundary and signature == boundary["signature"]:
                anchor["seen"] = True
                anchor["seed_index"] = index
                parsed = parse_position_transaction(tx, mint)
                raw = (parsed.get("accounts") or {}).get(account) or {}
                try:
                    if parsed.get("status") == "parsed" and raw.get("owner") == boundary.get("owner") and raw.get("mint") == mint \
                        and int(raw["decimals"]) == int(boundary["decimals"]) \
                        and int(raw["after_raw"]) == int(boundary["balance_raw"]) \
                        and int(raw["after_raw"]) - int(raw["before_raw"]) == int(boundary["bought_raw"]):
                        anchor["proof"] = {"owner": raw["owner"], "mint": mint, "decimals": raw["decimals"],
                            "balance_raw": raw["after_raw"], "bought_raw": str(int(raw["after_raw"]) - int(raw["before_raw"]))}
                except (KeyError, TypeError, ValueError):
                    anchor["unsafe"] = True
            else:
                anchor["other_indices"].append(index)
        next_cursor = result.get("paginationToken")
        ordered_tail = not anchor["other_indices"] or (
            isinstance(anchor["seed_index"], int) and all(isinstance(index, int)
            and index < anchor["seed_index"] for index in anchor["other_indices"]))
        boundary_verified = bool(boundary and anchor["proof"] and ordered_tail and not anchor["unsafe"] and not next_cursor)
        coverage = {"provider": used, "account": account, "mint": mint,
            "from_slot": from_slot, "to_slot": to_slot, "scope": "all_token_account_activity",
            "commitment": "finalized", "complete": bool(not next_cursor and not anchor["unsafe"]
                and (anchor["seen"] if boundary else rows)),
            "completeness_basis": "finalized_provider_query_with_receipt_anchor_not_independent_archive_audit"}
        if boundary_verified:
            coverage["seed_boundary"] = {**anchor["proof"], "account": account, "slot": from_slot,
                "signature": boundary["signature"], "no_later_successful_activity": True}
        return {"transactions": [tx for tx in rows if isinstance(tx, dict) and isinstance(tx.get("slot"), int)
                                  and from_slot < tx["slot"] <= to_slot],
                "next_cursor": next_cursor, "coverage": coverage}


def check_receipt_positions(rpc, thesis, mint, *, services=(), checked_at=None,
                            max_pages=4, max_owners=2):
    frozen = thesis.get("receipt_position_seeds") or {}
    reduced = {row.get("owner") for row in thesis.get("cohort", []) if row.get("movement_status") == "reduced_unresolved"}
    owners = [(owner, row) for owner, row in (frozen.get("owners") or {}).items() if row.get("seeds") and owner in reduced]
    if not owners:
        return {"status": "unavailable", "reason": "no_exact_frozen_receipt", "checked_at": checked_at,
                "scope": "receipt_component_not_entire_cohort", "confirmation_eligible": False}
    prior = thesis.setdefault("receipt_position_checks", {})
    owners.sort(key=lambda item: (prior.get(item[0], {}).get("checked_at") or "", item[0]))
    slot, _ = rpc._route_call("getSlot", [{"commitment": "finalized"}])
    adapter = PositionHistoryRpc(rpc, max_pages=max_pages)
    checked = 0
    for owner, row in owners[:max_owners]:
        if not adapter.remaining: break
        for seed in row["seeds"]:
            adapter.boundaries[seed["account"]] = {**seed, "signature": row["signature"]}
        result = resolve_position_history(mint, row["seeds"], adapter,
            from_slot=row["seed_slot"], to_slot=slot, max_depth=2, max_addresses=12,
            max_pages=adapter.remaining, max_transactions=64, page_size=32,
            service_owners=services, decode_transaction=lambda tx: {"transaction_index": tx.get("transactionIndex")})
        # Never add overlapping replay snapshots or substitute this component for the cohort.
        prior[owner] = {"checked_at": checked_at, "capture_key": row["capture_key"], "result": result}
        checked += 1
    return {"status": "shadow", "checked_at": checked_at, "selected_receipt_owners": len(owners),
        "owners_checked": checked, "page_calls": max_pages - adapter.remaining,
        "scope": "receipt_component_not_entire_cohort", "ownership": "not_established",
        "confirmation_eligible": False, "affects_original_cohort_retention": False,
        "results": {owner: prior[owner] for owner, _ in owners if owner in prior}}
