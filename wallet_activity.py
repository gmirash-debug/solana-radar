"""Resumable finalized wallet + ATA activity, independent of pool history.

Amounts are gross observed flows, never a partition of the original purchase.
Only a successful PumpSwap sell instruction with its exact SPL input transfer
is called a sale. Other protocols, custody and missing receipts remain explicit.
Decoder layout: pump-fun/pump-public-docs/idl/pump_amm.json, sell instruction.
"""
import copy
import math
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

import base58

PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SELL = bytes([51, 230, 133, 164, 1, 127, 131, 173])
TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}
KINDS = ("sold", "transferred", "service", "unclassified", "internal")


def stamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def raw(value):
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
        raise ValueError("invalid raw amount")
    return int(value)


def decode_wallet_activity(tx, mint, owner, *, services=()):
    """Return signature-linked facts; failed transactions have no movements."""
    result = {"events": [], "issues": [], "delta_raw": "0", "decimals": None}
    meta, transaction = tx.get("meta"), tx.get("transaction") or {}
    if not isinstance(meta, dict) or "err" not in meta:
        result["issues"] = ["missing_receipt"]
        return result
    if meta["err"] is not None:
        return result
    signature = (transaction.get("signatures") or [None])[0]
    message = transaction.get("message") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in message.get("accountKeys") or []]
    if not signature or not isinstance(meta.get("preTokenBalances"), list) or not isinstance(meta.get("postTokenBalances"), list):
        result["issues"] = ["missing_token_snapshot"]
        return result
    accounts, snapshots, issues = {}, [{}, {}], set()
    for side, field in enumerate(("preTokenBalances", "postTokenBalances")):
        for row in meta[field]:
            if row.get("mint") != mint:
                continue
            try:
                account = keys[raw(row["accountIndex"])]
                amount = raw(row["uiTokenAmount"]["amount"])
                decimals = raw(row["uiTokenAmount"]["decimals"])
                if not account or not row.get("owner") or decimals > 255 or account in snapshots[side]:
                    raise ValueError("invalid account identity")
                snapshots[side][account] = (row["owner"], amount, decimals)
            except (KeyError, TypeError, IndexError, ValueError):
                issues.add("invalid_token_snapshot")
    instructions = list(message.get("instructions") or [])
    if not isinstance(meta.get("innerInstructions"), list):
        issues.add("missing_inner_instructions")
    for group in meta.get("innerInstructions") or []:
        instructions.extend(group.get("instructions") or [])
    lifecycle = {}
    for ins in instructions:
        if ins.get("programId") not in TOKEN_PROGRAMS:
            continue
        parsed = ins.get("parsed") or {}
        info = parsed.get("info") or {}
        if parsed.get("type") in {"initializeAccount", "initializeAccount2", "initializeAccount3", "closeAccount"}:
            lifecycle[info.get("account")] = parsed.get("type")
    decimals_seen, delta = set(), 0
    for account in set(snapshots[0]) | set(snapshots[1]):
        before, after = snapshots[0].get(account), snapshots[1].get(account)
        identity = before or after
        if before and after and (before[0], before[2]) != (after[0], after[2]):
            issues.add("token_account_identity_changed")
            continue
        if before is None and lifecycle.get(account) not in {"initializeAccount", "initializeAccount2", "initializeAccount3"}:
            issues.add("unverified_account_creation")
        if after is None and lifecycle.get(account) != "closeAccount":
            issues.add("unverified_account_close")
        accounts[account] = {"owner": identity[0], "decimals": identity[2],
                             "before": before[1] if before else 0, "after": after[1] if after else 0}
        decimals_seen.add(identity[2])
        if identity[0] == owner:
            delta += (after[1] if after else 0) - (before[1] if before else 0)
    if len(decimals_seen) > 1:
        issues.add("conflicting_mint_decimals")
    result["decimals"] = next(iter(decimals_seen)) if len(decimals_seen) == 1 else None
    result["delta_raw"] = str(delta)
    transfers = []
    for index, ins in enumerate(instructions):
        if ins.get("programId") not in TOKEN_PROGRAMS:
            continue
        parsed = ins.get("parsed") or {}
        info = parsed.get("info") or {}
        if parsed.get("type") not in {"transfer", "transferChecked"}:
            if info.get("account") in accounts and parsed.get("type") in {"setAuthority", "burn", "burnChecked"}:
                issues.add("unsupported_position_instruction")
            continue
        source, destination = accounts.get(info.get("source")), accounts.get(info.get("destination"))
        if not source:
            continue
        try:
            amount = raw(info.get("amount", (info.get("tokenAmount") or {}).get("amount")))
            if info.get("mint", mint) != mint or source["decimals"] != result["decimals"]:
                raise ValueError("mint mismatch")
            if parsed["type"] == "transferChecked" and raw((info.get("tokenAmount") or {})["decimals"]) != source["decimals"]:
                raise ValueError("decimals mismatch")
        except (KeyError, TypeError, ValueError):
            issues.add("unparsed_transfer")
            continue
        transfers.append({"index": index, "source_account": info["source"], "destination_account": info["destination"],
                          "source_owner": source["owner"], "destination_owner": destination["owner"] if destination else None,
                          "amount_raw": str(amount), "kind": None})
        if destination is None:
            issues.add("unresolved_transfer_destination")
    # Successful, discriminator-decoded swaps are not inferred from SOL deltas.
    used = set()
    for ins in instructions:
        if ins.get("programId") != PUMP_AMM or not isinstance(ins.get("data"), str):
            continue
        try:
            data = base58.b58decode(ins["data"])
            addresses = [keys[a] if isinstance(a, int) else a for a in ins["accounts"]]
            if len(data) != 24 or data[:8] != SELL or len(addresses) < 17 or addresses[3] != mint or addresses[1] != owner:
                continue
            amount = int.from_bytes(data[8:16], "little")
            matches = [leg for leg in transfers if leg["source_account"] == addresses[5]
                       and leg["destination_account"] == addresses[7] and leg["source_owner"] == owner
                       and int(leg["amount_raw"]) == amount and leg["index"] not in used]
            if len(matches) == 1 and amount > 0 and accounts.get(addresses[7], {}).get("owner") == addresses[0]:
                matches[0]["kind"] = "sold"
                matches[0]["protocol"] = "PumpSwap"
                used.add(matches[0]["index"])
        except (KeyError, TypeError, ValueError, IndexError):
            issues.add("unsupported_swap_instruction")
    outgoing = 0
    incoming = 0
    for leg in transfers:
        if leg["source_owner"] != owner:
            if leg["destination_owner"] == owner:
                incoming += int(leg["amount_raw"])
            continue
        if leg["source_account"] == leg["destination_account"]:
            continue
        amount = int(leg["amount_raw"])
        if leg["destination_owner"] == owner:
            leg["kind"] = "internal"
        elif not leg["kind"]:
            leg["kind"] = ("unclassified" if leg["destination_owner"] is None else
                           "service" if leg["destination_owner"] in services or leg["destination_account"] in services else "transferred")
        if leg["kind"] != "internal":
            outgoing += amount
        result["events"].append({**leg, "signature": signature, "timestamp": tx.get("blockTime"), "slot": tx.get("slot")})
    unmatched = max(0, incoming - outgoing - delta)
    if unmatched:
        result["events"].append({"kind": "unclassified", "amount_raw": str(unmatched), "signature": signature,
                                  "source_owner": owner, "index": "unmatched", "timestamp": tx.get("blockTime"), "slot": tx.get("slot")})
        issues.add("unreconciled_owner_debit")
    result["issues"] = sorted(issues)
    return result


class AuditBudget:
    def __init__(self, pages=24, seconds=90):
        self.remaining = max(0, int(pages))
        self.used = 0
        self.deadline = time.monotonic() + max(0, seconds)

    def available(self):
        return self.remaining > 0 and time.monotonic() < self.deadline

    def take(self):
        if not self.available():
            raise RuntimeError("wallet_history_budget_deferred")
        self.remaining -= 1
        self.used += 1


def advance_wallet_history(rpc, thesis, row, budget, checked_at, *, services=(), page_size=100, max_events=2048):
    """One page per owner per pass; failed pages never advance the checkpoint."""
    owner, mint = row["owner"], thesis["token_address"]
    audits = thesis.setdefault("wallet_activity_checks", {})
    audit = audits.setdefault(owner, {"version": 1, "events": {}, "issues": [], "pages": 0})
    audit["last_attempt_at"] = checked_at
    end = int(stamp(checked_at)) - 120
    first = math.ceil(stamp(thesis["signal_at"]))
    if end < first or not budget.available():
        return False
    query = audit.get("query")
    if not query:
        start = max(first, int(audit.get("complete_through", first - 1)) + 1)
        if start > end:
            return False
        query = audit["query"] = {"from": start, "to": end, "cursor": None, "provider": None, "issues": []}
    options = {"transactionDetails": "full", "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
               "commitment": "finalized", "sortOrder": "asc", "limit": min(100, max(1, page_size)),
               "filters": {"status": "any", "tokenAccounts": "all", "blockTime": {"gte": query["from"], "lte": query["to"]}}}
    if query["cursor"]:
        options["paginationToken"] = query["cursor"]
    provider = query.get("provider")
    budget.take()
    try:
        response, used = rpc._route_call("getTransactionsForAddress", [owner, options], preferred=provider,
            order=[provider] if provider else rpc.archive_order, timeout=8)
        if provider and used != provider:
            raise ValueError("cursor provider changed")
        txs = response.get("data") if isinstance(response, dict) else None
        if not isinstance(txs, list) or len(txs) > options["limit"]:
            raise ValueError("invalid wallet history page")
        next_cursor = response.get("paginationToken")
        if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor or next_cursor == query["cursor"]):
            raise ValueError("nonadvancing history cursor")
        updated = copy.deepcopy(audit)
        for tx in txs:
            at = tx.get("blockTime") if isinstance(tx, dict) else None
            if not isinstance(at, (int, float)) or isinstance(at, bool) or not query["from"] <= at <= query["to"]:
                updated["query"]["issues"] = sorted(set(updated["query"]["issues"]) | {"transaction_outside_wallet_window"})
                continue
            facts = decode_wallet_activity(tx, mint, owner, services=services)
            if facts["decimals"] is not None:
                if updated.get("decimals", facts["decimals"]) != facts["decimals"]:
                    facts["issues"].append("conflicting_mint_decimals")
                updated["decimals"] = facts["decimals"]
            updated["query"]["issues"] = sorted(set(updated["query"]["issues"]) | set(facts["issues"]))
            for event in facts["events"]:
                key = f"{event['signature']}|{event['index']}"
                previous = updated["events"].get(key)
                if previous and previous != event:
                    raise ValueError("conflicting movement receipt")
                if not previous and len(updated["events"]) >= max_events:
                    raise ValueError("wallet movement event limit")
                updated["events"][key] = event
        updated["pages"] += 1
        updated["query"].update(cursor=next_cursor, provider=used)
        updated["status"] = "backfilling" if next_cursor else "checked"
        if not next_cursor and updated["query"]["issues"]:
            # Incomplete receipts cannot establish a checkpoint. Replay this
            # same window later; a successful repair replaces its old issues.
            updated["issues"] = list(updated["query"]["issues"])
            updated["query"].update(cursor=None, issues=[])
            updated["status"] = "partial"
            updated["retry_after"] = iso(stamp(checked_at) + 3600)
        elif not next_cursor:
            updated["issues"] = []
            updated["complete_through"] = updated["query"]["to"]
            updated.pop("query", None)
            updated["checked_at"] = checked_at
        audits[owner] = updated
        if updated["status"] != "partial":
            updated.pop("retry_after", None)
        updated.pop("error_category", None)
        updated["consecutive_errors"] = 0
        return True
    except Exception as exc:
        # Preserve cursor and prior evidence; never publish provider URLs/errors.
        audit["status"] = "retry"
        audit["retry_after"] = iso(stamp(checked_at) + 900)
        audit["error_category"] = getattr(exc, "category", "history_unavailable")
        audit["consecutive_errors"] = int(audit.get("consecutive_errors") or 0) + 1
        if audit["consecutive_errors"] >= 3 and audit.get("query"):
            # Opaque cursors are never sent to another provider. Replay the same
            # bounded window from its start; signature/leg IDs deduplicate it.
            audit["query"].update(cursor=None, provider=None)
        return False


def summarize_wallet_activity(thesis, checked_at):
    cohort = {row["owner"]: row for row in thesis.get("cohort") or [] if row.get("owner")}
    audits = thesis.get("wallet_activity_checks") or {}
    total = defaultdict(Decimal)
    counts = defaultdict(int)
    owners, events = [], []
    complete = 0
    statuses = defaultdict(int)
    pages = 0
    token_complete = 0.0
    horizon = int(stamp(checked_at)) - 900
    for owner, row in cohort.items():
        audit = audits.get(owner) or {}
        statuses[audit.get("status", "pending")] += 1
        pages += int(audit.get("pages") or 0)
        amounts = defaultdict(Decimal)
        decimals = audit.get("decimals")
        for event in (audit.get("events") or {}).values():
            if decimals is None:
                continue
            amount = Decimal(event["amount_raw"]) / (Decimal(10) ** decimals)
            amounts[event["kind"]] += amount
            total[event["kind"]] += amount
            counts[event["kind"]] += 1
            events.append({**event, "tokens": float(amount)})
        history_complete = bool("complete_through" in audit and audit["complete_through"] >= horizon
                                and not audit.get("issues") and not audit.get("query"))
        if history_complete:
            complete += 1
            token_complete += float(row.get("attributed_tokens") or 0)
        owners.append({"owner": owner, "status": audit.get("status", "pending"), "history_complete": history_complete,
            "checked_at": audit.get("checked_at"), "complete_through": iso(audit["complete_through"]) if audit.get("complete_through") else None,
            "pages": audit.get("pages", 0), "issues": audit.get("issues", []) + (audit.get("query") or {}).get("issues", []),
            "amounts_tokens": {kind: float(amounts[kind]) for kind in KINDS}})
    attributed = sum(float(row.get("attributed_tokens") or 0) for row in cohort.values())
    supply = float(thesis.get("supply") or 0)
    amounts = {kind: float(total[kind]) for kind in KINDS}
    summary = {"version": 1, "scope": "finalized_wallet_and_token_account_activity_since_catch",
        "status": "checked" if cohort and complete == len(cohort) else "backfilling",
        "checked_at": checked_at, "wallets_total": len(cohort), "wallets_checked": complete,
        "wallet_coverage_pct": 100 * complete / len(cohort) if cohort else 0,
        "token_coverage_pct": 100 * token_complete / attributed if attributed else 0,
        "amounts_tokens": amounts, "amounts_supply_pct": {kind: value / supply * 100 if supply else None for kind, value in amounts.items()},
        "event_counts": dict(counts), "pages_checked": pages, "wallet_status_counts": dict(statuses),
        "amount_basis": "gross_flows_may_include_later_buys_not_original_inventory",
        "ownership": "not_established", "owners": owners,
        "events": sorted(events, key=lambda event: (event.get("timestamp") or 0, event["signature"]), reverse=True)[:50],
        "event_count": len(events), "events_display_limit": 50,
        "interpretation_complete": complete == len(cohort) and not amounts["unclassified"] and not amounts["service"] if cohort else False}
    thesis["wallet_activity"] = summary
    return summary


def recover_sale_history_status(thesis, summary):
    """Recover history coverage, not confirmation or original-lot provenance."""
    if (summary.get("status") != "checked" or summary.get("wallet_coverage_pct") != 100
            or summary.get("token_coverage_pct", 0) < 99.99 or not summary.get("interpretation_complete")
            or float(thesis.get("cohort_wallet_coverage_pct") or 0) < 99.99
            or float(thesis.get("cohort_token_coverage_pct") or 0) < 99.99):
        return False
    # A post-capture audit cannot prove the original purchase window or repair
    # legacy inventory attribution. Keep that uncertainty separate from flows.
    issues = thesis.get("original_sale_history_issues") or []
    if "legacy_original_sale_history_not_reconstructed" in issues:
        return False
    thesis.setdefault("sale_history_recovery", {})["previous_issues"] = list(thesis.get("original_sale_history_issues") or [])
    thesis["sale_history_recovery"].update(checked_at=summary["checked_at"], basis=summary["scope"])
    thesis["original_sale_history_status"] = "reconstructed_from_capture"
    thesis["original_sale_history_issues"] = []
    return True
