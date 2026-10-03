"""Bounded SPL-token position evidence. No implicit RPC or ownership inference.

Contract
--------
parse_position_transaction consumes one successful jsonParsed getTransaction
response (including raw pre/post token amounts and resolved account keys). It
returns a JSON-serializable batch for analyze_position. It never accepts UI
amounts, signer heuristics, or scanner kind='sell' as sale proof. Unknown account
lifecycle, fees, identity changes and unsupported token instructions fail closed.

A sale additionally needs a caller-decoded protocol swap witness containing:
signature, program_id, outer_index, mint, owner, input_account, input_vault,
input_amount_raw, output_mint, output_account, output_vault, output_amount_raw.
The program must be in swap_program_ids. This is a TRUSTED DECODER boundary, not
a built-in protocol decoder: the caller must establish that the instruction is
a swap, not liquidity provision or two gifts. We independently match its exact
input/output transfers and account deltas. Native SOL / multi-hop / fee-bearing
swaps without these exact legs remain unresolved.

analyze_position seeds a frozen, disjoint purchase denominator per token account:
{mint, account, owner, bought_raw, balance_raw, retained_lower_raw (default 0),
 retained_upper_raw (default min(balance_raw, bought_raw))}. The seed is AFTER
all transactions in from_slot; seed balances and attribution must share that
boundary. Never seed a lower bound from min(current_balance, historical_buys).
Receipt-derived seeds additionally require receipt_slot_boundary_verified=True
as a trusted caller assertion; resolve_position_history sets this only after
validating its adapter's seed_boundary coverage. History after the seed slot
alone does not establish that a receipt post-balance was its slot-end balance.
Each batch must come from parse_position_transaction. transaction_index is a
caller-supplied canonical block order, NOT signature order. Ambiguous same-slot
ordering degrades evidence instead of inventing an order.

history_complete defaults False. True asserts that ALL incoming/outgoing token
activity for every tracked account is supplied from the seed to to_slot, not
just swaps in one pool. Fresh closing_balances may be supplied as rows
{mint, account, owner, slot: to_slot, balance_raw}. Missing rows are NOT zero.
Partial history cannot establish current retained provenance; upper bounds stay
broad. Bounds are marginal and non-additive. amounts_raw is a partition into
guaranteed original/transferred/sold and unresolved unknown, NOT a disposition
claim for the unknown part. A direct transfer is traceability, not common control.

First integration: annotate existing partial pool batches, WITHOUT changing
original-cohort retention or invalidation::

    from position_evidence import annotate_solana_pool_position_activity
    activity = annotate_solana_pool_position_activity(
        pool.token_address, txs, parsed_pool_swaps, frozen_cohort_owners,
        signal_timestamp=thesis['signal_at'], checked_timestamp=checked_at,
        service_owners=known_public_services, service_accounts=known_vaults)
    thesis['observed_position_activity'] = activity

This returns status='partial', affects_original_cohort_retention=False,
original_position_sold_raw=None, retention_bounds_raw=None, per-owner gross
activity and signature-linked observations. Do not sum this annotation with
previous snapshots; transactions may overlap. Zero observations mean no match
in the supplied subset. Keep the existing retention/invalidation path unchanged.

Optional bounded ledger example (use only transactions already fetched)::

    from position_evidence import parse_solana_position_transaction, solana_position_summary
    batches = [parse_solana_position_transaction(tx, mint,
        transaction_index=block_order.get(tx['transaction']['signatures'][0]),
        swap_witnesses=decoded_swaps, swap_program_ids=verified_swap_programs)
        for tx in existing_transactions]
    evidence = solana_position_summary(mint, frozen_account_seeds, batches,
        from_slot=seed_slot, to_slot=checked_slot,
        history_complete=False,  # pool history alone is not account history
        service_accounts=known_vaults, service_owners=known_public_services)

Keep this descriptive evidence separate from actionable/confirmation logic.
Current scanner float swap rows cannot supply exact raw seeds or swap witnesses.
freeze_receipt_seeds can instead freeze one raw, reconciled pool receipt component
per owner. This is NOT the entire original cohort or a verified protocol buy.
Capture its result once per thesis, including unavailable entries; do not rerun
selection against later buys. Receipt seeds require a verified seed-slot tail
boundary before the slot-based resolver can establish retained provenance.
resolve_position_history optionally follows exact token-account destinations via
a caller-owned history callback. It never discovers accounts from a wallet's
current balance or follows public-service destinations. See its callback contract
before integration; it does not implement provider pagination or swap decoding.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import math


VERSION = 1
TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}


def _raw(value):
    if isinstance(value, bool):
        raise ValueError("Raw amounts and order fields must be non-negative integers")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise ValueError("Raw amounts and order fields must be non-negative integers")


def _address(value):
    # Solana addresses are case-sensitive; do not normalize or resolve authorities.
    return value if isinstance(value, str) and value and value == value.strip() else None


def _token_instruction(instruction):
    program, program_id = instruction.get("program"), instruction.get("programId")
    return ((isinstance(program, str) and program in {"spl-token", "spl-token-2022"})
            or (isinstance(program_id, str) and program_id in TOKEN_PROGRAMS))


def parse_position_transaction(tx, mint, *, transaction_index=None,
                               swap_witnesses=(), swap_program_ids=(),
                               max_instructions=512, max_accounts=256):
    """Normalize exact transfers; malformed/incomplete data produces partial evidence.

    Limits reject the whole transaction, never a silently truncated prefix.
    Input objects are not mutated. Failed transactions produce no token events.
    """
    if not _address(mint):
        raise ValueError("An exact mint is required")
    result = {"version": VERSION, "mint": mint, "signature": None, "slot": None,
              "transaction_index": transaction_index, "status": "unavailable",
              "accounts": {}, "transfers": [], "issues": []}
    if not isinstance(tx, Mapping):
        result["issues"] = ["missing_transaction"]
        return result
    max_instructions, max_accounts = _raw(max_instructions), _raw(max_accounts)
    transaction = tx.get("transaction") or {}
    if not isinstance(transaction, Mapping) or not isinstance(transaction.get("message") or {}, Mapping):
        result["issues"] = ["invalid_transaction_shape"]
        return result
    message = transaction.get("message") or {}
    signatures = transaction.get("signatures") or []
    if not isinstance(signatures, list):
        result["issues"] = ["invalid_transaction_shape"]
        return result
    result["signature"] = _address(signatures[0]) if signatures else None
    try:
        result["slot"] = _raw(tx.get("slot"))
        if transaction_index is not None:
            result["transaction_index"] = _raw(transaction_index)
    except ValueError:
        result["issues"] = ["missing_canonical_order"]
        return result
    meta = tx.get("meta")
    if not result["signature"] or not isinstance(meta, Mapping) or "err" not in meta:
        result["issues"] = ["missing_success_metadata_or_signature"]
        return result
    if meta["err"] is not None:
        result["status"] = "failed"
        return result
    keys = message.get("accountKeys") or []
    outer = message.get("instructions") or []
    inner = meta.get("innerInstructions") or []
    if (not all(isinstance(rows, list) for rows in (keys, outer, inner))
            or not all(isinstance(g, Mapping) and isinstance(g.get("instructions") or [], list) for g in inner)
            or not all(isinstance(entry, Mapping) for entry in outer)):
        result["issues"] = ["invalid_instruction_shape"]
        return result
    instruction_count = len(outer) + sum(len(g.get("instructions") or []) for g in inner)
    if (len(keys) > max_accounts or instruction_count > max_instructions or len(swap_witnesses) > max_instructions
            or any(isinstance(meta.get(field), list) and len(meta[field]) > max_accounts
                   for field in ("preTokenBalances", "postTokenBalances"))):
        result["issues"] = ["transaction_limit"]
        return result
    keys = [k.get("pubkey") if isinstance(k, Mapping) else k for k in keys]
    issues = set()
    snapshots = [{}, {}]
    for side, field in enumerate(("preTokenBalances", "postTokenBalances")):
        if not isinstance(meta.get(field), list):
            issues.add("missing_token_balances")
        for row in meta.get(field) if isinstance(meta.get(field), list) else []:
            try:
                index = _raw(row.get("accountIndex"))
                account = keys[index]
                owner, asset = _address(row.get("owner")), _address(row.get("mint"))
                amount = _raw((row.get("uiTokenAmount") or {}).get("amount"))
                identity = (owner, asset, _raw((row.get("uiTokenAmount") or {}).get("decimals")))
                if not _address(account) or not owner or not asset or identity[2] > 255:
                    raise ValueError("Unresolved token account identity")
            except (ValueError, TypeError, IndexError, AttributeError):
                issues.add("unresolved_token_account")
                continue
            if account in snapshots[side]:
                issues.add("duplicate_token_account")
                snapshots[side][account] = None
            else:
                snapshots[side][account] = (identity, amount)
    accounts = result["accounts"]
    for account in sorted(set(snapshots[0]) | set(snapshots[1])):
        before, after = snapshots[0].get(account), snapshots[1].get(account)
        identity = before[0] if before else after[0] if after else (None, None, None)
        complete = bool(before and after and before[0] == after[0])
        accounts[account] = {"owner": identity[0], "mint": identity[1], "decimals": identity[2],
                             "before_raw": str(before[1]) if before else None,
                             "after_raw": str(after[1]) if after else None,
                             "identity_resolved": complete, "complete": complete}
        if not complete:
            issues.add("incomplete_account_lifecycle_or_identity")
    groups = defaultdict(list)
    for group in inner:
        try:
            index = _raw(group.get("index"))
            if index >= len(outer) or index in groups:
                raise ValueError("Invalid inner instruction group")
            groups[index] = group.get("instructions") or []
        except (ValueError, AttributeError):
            issues.add("invalid_instruction_order")
    legs = []
    incomplete_instructions = not isinstance(meta.get("innerInstructions"), list)
    if incomplete_instructions:
        issues.add("missing_inner_instruction_coverage")
    for outer_index, instruction in enumerate(outer):
        for inner_index, entry in enumerate([instruction] + groups[outer_index]):
            if not isinstance(entry, Mapping):
                incomplete_instructions = True
                continue
            if not _token_instruction(entry):
                continue
            if entry.get("programId") is not None and (not isinstance(entry["programId"], str) or entry["programId"] not in TOKEN_PROGRAMS):
                incomplete_instructions = True
                continue
            parsed = entry.get("parsed") or {}
            if not isinstance(parsed, Mapping):
                incomplete_instructions = True
                continue
            if not isinstance(parsed.get("type"), str) or parsed["type"] not in {"transfer", "transferChecked"}:
                # Net balance reconciliation alone cannot exclude hidden roundtrips.
                incomplete_instructions = True
                continue
            info = parsed.get("info") or {}
            try:
                if not isinstance(info, Mapping) or not _address(info.get("source")) or not _address(info.get("destination")):
                    raise ValueError("Malformed transfer accounts")
                source, destination = accounts.get(info["source"]), accounts.get(info["destination"])
                amount = _raw(info.get("amount", (info.get("tokenAmount") or {}).get("amount")))
                if not source or not destination or source["mint"] != destination["mint"] or not source["mint"]:
                    raise ValueError("Unresolved transfer mint")
                if info.get("mint", source["mint"]) != source["mint"]:
                    raise ValueError("Transfer mint mismatch")
                if parsed["type"] == "transferChecked" and _raw((info.get("tokenAmount") or {}).get("decimals")) != source["decimals"]:
                    raise ValueError("Transfer decimals mismatch")
                if source["decimals"] != destination["decimals"]:
                    raise ValueError("Account decimals mismatch")
            except (ValueError, TypeError, AttributeError):
                incomplete_instructions = True
                continue
            if amount:
                legs.append({"outer_index": outer_index, "inner_index": inner_index,
                             "source_account": info["source"], "destination_account": info["destination"],
                             "source_owner": source["owner"], "destination_owner": destination["owner"],
                             "mint": source["mint"], "amount_raw": str(amount), "kind": "transfer"})
    deltas = defaultdict(int)
    for leg in legs:
        deltas[leg["source_account"]] -= int(leg["amount_raw"])
        deltas[leg["destination_account"]] += int(leg["amount_raw"])
    for account, row in accounts.items():
        if row["complete"] and int(row["before_raw"]) + deltas[account] != int(row["after_raw"]):
            row["complete"] = False
            issues.add("unreconciled_token_account")
        if incomplete_instructions or "invalid_instruction_order" in issues or "unresolved_token_account" in issues:
            row["complete"] = False
    running = {account: int(row["before_raw"]) for account, row in accounts.items()
               if row["before_raw"] is not None}
    for leg in legs:
        source, destination, amount = leg["source_account"], leg["destination_account"], int(leg["amount_raw"])
        if source == destination:
            continue
        if source in running and running[source] < amount:
            accounts[source]["complete"] = False
            issues.add("unreconciled_intermediate_balance")
        if source in running:
            running[source] -= amount
        if destination in running:
            running[destination] += amount
    if incomplete_instructions:
        issues.add("unsupported_or_unresolved_token_instruction")
    claimed = set()
    for witness in swap_witnesses:
        selected = _sale_legs(witness, result, outer, legs, set(swap_program_ids))
        if selected is None:
            issues.add("unverified_swap_witness")
            continue
        for index in selected:
            if index in claimed:
                continue
            claimed.add(index)
            legs[index]["kind"] = "verified_sale"
            legs[index]["sale_basis"] = "caller_decoded_swap_and_reconciled_exact_legs"
    result["transfers"] = [leg for leg in legs if leg["mint"] == mint]
    result["issues"] = sorted(issues)
    result["status"] = "partial" if issues else "parsed"
    return result


def _sale_legs(witness, batch, outer, legs, programs):
    if not isinstance(witness, Mapping):
        return None
    try:
        index = _raw(witness.get("outer_index"))
        amount, output = _raw(witness.get("input_amount_raw")), _raw(witness.get("output_amount_raw"))
        program = witness.get("program_id")
        owner = _address(witness.get("owner"))
        if (witness.get("signature") != batch["signature"] or witness.get("mint") != batch["mint"]
                or not owner or program not in programs or outer[index].get("programId") != program
                or not amount or not output or not _address(witness.get("output_mint"))
                or witness["output_mint"] == batch["mint"]):
            return None
        names = [witness[k] for k in ("input_account", "input_vault", "output_account", "output_vault")]
        if len(set(names)) != 4:
            return None
        source, vault, recipient, quote_vault = [batch["accounts"][name] for name in names]
        if (not all(row["complete"] for row in (source, vault, recipient, quote_vault))
                or source["owner"] != owner or recipient["owner"] != owner
                or vault["owner"] == owner or quote_vault["owner"] == owner
                or source["mint"] != batch["mint"] or vault["mint"] != batch["mint"]
                or recipient["mint"] != witness["output_mint"] or quote_vault["mint"] != witness["output_mint"]):
            return None
        if (int(source["before_raw"]) - int(source["after_raw"]) != amount
                or int(vault["after_raw"]) - int(vault["before_raw"]) != amount
                or int(recipient["after_raw"]) - int(recipient["before_raw"]) != output
                or int(quote_vault["before_raw"]) - int(quote_vault["after_raw"]) != output):
            return None
        inputs = [i for i, leg in enumerate(legs) if leg["outer_index"] == index
                  and leg["source_account"] == names[0] and leg["destination_account"] == names[1]]
        outputs = [leg for leg in legs if leg["outer_index"] == index
                   and leg["source_account"] == names[3] and leg["destination_account"] == names[2]]
        if sum(int(legs[i]["amount_raw"]) for i in inputs) != amount or sum(int(leg["amount_raw"]) for leg in outputs) != output:
            return None
        return inputs
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None


def freeze_receipt_seeds(mint, transactions, parsed_swaps, cohort_owners, *,
                         service_owners=(), service_accounts=(), max_owners=40,
                         max_transactions=256, max_swaps=512,
                         max_instructions=512, max_accounts=256):
    """Freeze one latest explicit successful pool-buy receipt component per owner.

    Returns ``owners[owner]`` with {status, seed_slot, signature, bought_raw,
    seeds, capture_key, issues}; each seed is an analyze_position-compatible raw
    account row. All returned rows have scope='receipt_component_not_entire_cohort'.
    bought_raw is the exact positive account delta, NOT any parsed/UI buy amount.
    Both retained bounds equal that delta immediately after the receipt, even
    when its post-balance also contains older inventory. Multiple positive token
    accounts in that ONE receipt are disjoint; no amounts across buys are summed.

    Parsed swaps only nominate signature/pool/token_recipient with kind='buy'
    and exact token_address. No signer fallback, float conversion or largest-owner
    heuristic is used. The existing raw parser must reconcile every mint leg as
    a direct pool-owner -> recipient-owner transfer, with matching net deltas.
    This establishes a token receipt component, NOT protocol swap/payment proof.
    New/closed accounts without both raw snapshots and decimals fail closed.

    Failed buys are skipped; a missing/ambiguous candidate cannot be ordered.
    Duplicate signatures/swap rows, a latest-slot tie, or unsafe latest receipt
    refuse that owner's capture, never silently fall back to an older buy. Known
    pool owners and caller-classified public services cannot become seed owners.
    Owner selection is a bounded prefix in caller order, default 40; oversized
    receipt/swap inputs reject the whole observation set, never a partial prefix.

    This function is stateless. The parent MUST store its capture once per thesis
    before later rechecks, even if unavailable; do not replace it with rebuys.
    A receipt post-state is not a slot-end state: adapter coverage must attest
    seed_boundary={account, slot, signature, owner, mint, decimals, balance_raw,
    bought_raw, no_later_successful_activity: True} on a finalized page for each
    receipt seed. Amounts/identity must come from its finalized receipt, not be
    echoed from an unverified capture or reconstructed from a current balance.
    Without that proof the resolver
    reports partial evidence, not guaranteed holdings or original sales. Different
    owners' seed slots may differ; replay their components separately, not with
    one invented shared slot or a reconstructed entire-cohort denominator.
    """
    if not _address(mint) or not all(isinstance(rows, Sequence) and not isinstance(rows, (str, bytes))
                                   for rows in (transactions, parsed_swaps, cohort_owners)):
        raise ValueError("Expected exact mint and bounded receipt/swap/owner sequences")
    owner_limit, tx_limit, swap_limit, instruction_limit, account_limit = map(
        _raw, (max_owners, max_transactions, max_swaps, max_instructions, max_accounts))
    owners = list(cohort_owners[:owner_limit])
    if not all(_address(owner) for owner in owners) or len(set(owners)) != len(owners):
        raise ValueError("Selected cohort owners must be unique exact addresses")
    scope = "receipt_component_not_entire_cohort"
    issues = {"owner_limit"} if len(cohort_owners) > owner_limit else set()
    entries = {owner: {"status": "unavailable", "scope": scope, "seeds": [],
                       "issues": [], "seed_slot": None, "signature": None,
                       "bought_raw": None, "capture_key": None} for owner in owners}
    receipts, swaps, nomination_issues = defaultdict(list), defaultdict(list), defaultdict(set)
    excluded_owners, excluded_accounts = set(service_owners), set(service_accounts)
    if len(transactions) > tx_limit or len(parsed_swaps) > swap_limit:
        issues.add("receipt_observation_limit")
        transactions, parsed_swaps = [], []
        for entry in entries.values():
            entry["issues"] = ["receipt_observation_limit"]
    for tx in transactions:
        transaction = tx.get("transaction") if isinstance(tx, Mapping) else None
        signatures = transaction.get("signatures") if isinstance(transaction, Mapping) else None
        signature = _address(signatures[0]) if isinstance(signatures, list) and signatures else None
        if signature:
            receipts[signature].append(tx)
    for swap in parsed_swaps:
        if not isinstance(swap, Mapping):
            continue
        pool = _address(swap.get("pool_address"))
        if pool:
            excluded_owners.add(pool)
        if swap.get("token_address") == mint:
            signature = _address(swap.get("signature"))
            if signature:
                swaps[signature].append(swap)
            elif swap.get("kind") == "buy" and _address(swap.get("token_recipient")) in entries:
                nomination_issues[swap["token_recipient"]].add("missing_buy_receipt_signature")
    for owner, entry in entries.items():
        if owner in excluded_owners:
            entry["issues"] = ["public_service_seed_owner"]
            continue
        candidates, problems = [], set(entry["issues"]) | nomination_issues[owner]
        for signature, rows in swaps.items():
            buys = [row for row in rows if row.get("kind") == "buy" and row.get("token_recipient") == owner]
            if not buys:
                continue
            bodies = receipts.get(signature, [])
            if len(bodies) != 1:
                problems.add("duplicate_receipt_signature" if bodies else "missing_buy_receipt")
                continue
            tx, swap = bodies[0], buys[0]
            meta = tx.get("meta")
            if not isinstance(meta, Mapping) or "err" not in meta:
                problems.add("missing_receipt_success_metadata")
                continue
            if meta["err"] is not None:
                continue
            try:
                slot = _raw(tx.get("slot"))
            except ValueError:
                problems.add("missing_receipt_slot")
                continue
            candidates.append((slot, signature, swap, tx, len(rows)))
        if problems or not candidates:
            entry["issues"] = sorted(problems or {"no_successful_pool_buy_receipt"})
            continue
        latest_slot = max(row[0] for row in candidates)
        latest = [row for row in candidates if row[0] == latest_slot]
        if len(latest) != 1:
            entry["issues"] = ["ambiguous_latest_buy_slot"]
            continue
        slot, signature, swap, tx, swap_count = latest[0]
        entry.update(seed_slot=slot, signature=signature)
        pool = _address(swap.get("pool_address"))
        if swap_count != 1:
            entry["issues"] = ["duplicate_or_conflicting_pool_swap_rows"]
            continue
        if not pool or swap.get("owner_resolution") == "unresolved":
            entry["issues"] = ["unresolved_pool_buy_identity"]
            continue
        if "slot" in swap:
            try:
                if _raw(swap["slot"]) != slot:
                    raise ValueError("Buy receipt slot mismatch")
            except ValueError:
                entry["issues"] = ["pool_buy_slot_mismatch"]
                continue
        batch = parse_position_transaction(tx, mint, max_instructions=instruction_limit,
                                           max_accounts=account_limit)
        if batch["status"] != "parsed":
            entry["issues"] = sorted({"incomplete_buy_receipt"} | set(batch["issues"]))
            continue
        accounts = {account: row for account, row in batch["accounts"].items() if row["mint"] == mint}
        if len({row["decimals"] for row in accounts.values()}) != 1:
            entry["issues"] = ["inconsistent_mint_decimals"]
            continue
        deltas = {account: int(row["after_raw"]) - int(row["before_raw"])
                  for account, row in accounts.items()}
        positive = {account: amount for account, amount in deltas.items()
                    if amount > 0 and accounts[account]["owner"] == owner}
        total = sum(positive.values())
        if not total or any(amount < 0 and accounts[account]["owner"] == owner for account, amount in deltas.items()):
            entry["issues"] = ["non_positive_or_mixed_recipient_delta"]
            continue
        if any(accounts[account]["owner"] not in {owner, pool} and amount for account, amount in deltas.items()):
            entry["issues"] = ["ambiguous_receipt_participants"]
            continue
        if any(account in excluded_accounts for account in positive):
            entry["issues"] = ["public_service_seed_account"]
            continue
        legs = batch["transfers"]
        credits = defaultdict(int)
        if not legs or any(leg["source_owner"] != pool or leg["destination_owner"] != owner for leg in legs):
            entry["issues"] = ["ambiguous_or_unverified_pool_receipt_flow"]
            continue
        for leg in legs:
            credits[leg["destination_account"]] += int(leg["amount_raw"])
        pool_delta = sum(amount for account, amount in deltas.items() if accounts[account]["owner"] == pool)
        if dict(credits) != positive or pool_delta != -total:
            entry["issues"] = ["pool_receipt_delta_mismatch"]
            continue
        ambiguous_slot = False
        for other in transactions:
            other_transaction = other.get("transaction") if isinstance(other, Mapping) else None
            if not isinstance(other_transaction, Mapping):
                continue
            other_signatures = other_transaction.get("signatures")
            if isinstance(other_signatures, list) and other_signatures and other_signatures[0] == signature:
                continue
            try:
                other_slot = _raw(other.get("slot"))
            except ValueError:
                continue
            if other_slot != slot:
                continue
            other_meta = other.get("meta")
            if isinstance(other_meta, Mapping) and "err" in other_meta and other_meta["err"] is not None:
                continue
            message = other_transaction.get("message")
            keys = message.get("accountKeys") if isinstance(message, Mapping) else None
            if isinstance(keys, list) and (len(keys) > account_limit or any(
                    (k.get("pubkey") if isinstance(k, Mapping) else k) in positive
                    for k in keys if _address(k.get("pubkey") if isinstance(k, Mapping) else k))):
                ambiguous_slot = True
                break
        if ambiguous_slot:
            entry["issues"] = ["ambiguous_seed_slot_activity"]
            continue
        entry.update(status="frozen_receipt_component", pool_address=pool,
            bought_raw=str(total), capture_key=f"{mint}:{owner}:{signature}",
            seed_boundary="immediately_after_receipt", requires_seed_slot_tail_check=True,
            buy_classification="caller_parsed_not_protocol_verified",
            seeds=[{"mint": mint, "account": account, "owner": owner,
                    "decimals": accounts[account]["decimals"], "bought_raw": str(amount),
                    "balance_raw": accounts[account]["after_raw"],
                    "retained_lower_raw": str(amount), "retained_upper_raw": str(amount),
                    "scope": scope, "seed_slot": slot, "seed_signature": signature,
                    "seed_boundary": "immediately_after_receipt"}
                   for account, amount in sorted(positive.items())])
    issues.update(issue for entry in entries.values() for issue in entry["issues"])
    return {"version": VERSION, "mint": mint, "scope": scope,
        "status": "partial" if any(entry["seeds"] for entry in entries.values()) else "unavailable",
        "denominator": "selected_receipt_net_positive_raw_delta",
        "original_cohort_denominator_complete": False, "wallet_history_complete": False,
        "ownership": "not_established", "affects_original_cohort_retention": False,
        "confirmation_eligible": False, "capture_policy": "once_per_thesis_never_replace_with_rebuys",
        "supplied_owner_count": len(cohort_owners), "selected_owner_count": len(entries),
        "owners": entries, "issues": sorted(issues),
        "limits": {"owners": owner_limit, "transactions": tx_limit, "swaps": swap_limit,
                   "instructions": instruction_limit, "accounts_per_transaction": account_limit}}


def analyze_position(mint, seeds, batches, *, from_slot, to_slot,
                     history_complete=False, closing_balances=(),
                     service_accounts=(), service_owners=(),
                     max_transactions=256, max_events=1024, max_accounts=32, max_depth=2):
    """Replay a single frozen mint position without fetching or attributing ownership.

    Invalid seed/scope is a ValueError. Missing/ambiguous observation data yields
    partial evidence, not a successful negative result. Limits are hard bounds.
    """
    if not _address(mint) or not all(isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)) for rows in (seeds, batches)):
        raise ValueError("Expected exact mint and bounded seed/batch sequences")
    start, end = _raw(from_slot), _raw(to_slot)
    if end < start:
        raise ValueError("Position horizon precedes seed")
    max_transactions, max_events, max_accounts, max_depth = map(_raw, (max_transactions, max_events, max_accounts, max_depth))
    excluded_accounts, excluded_owners = set(service_accounts), set(service_owners)
    originals, nodes, total = set(), {}, 0
    receipt_boundary_unverified = False
    for seed in seeds:
        if not isinstance(seed, Mapping):
            raise ValueError("Seed rows must contain exact token account evidence")
        account, owner = _address(seed.get("account")), _address(seed.get("owner"))
        if seed.get("mint") != mint or not account or not owner or account in nodes:
            raise ValueError("Seeds require unique exact mint/account/owner scope")
        if account in excluded_accounts or owner in excluded_owners:
            raise ValueError("A public service cannot seed an original wallet position")
        if seed.get("scope") == "receipt_component_not_entire_cohort":
            if _raw(seed.get("seed_slot")) != start or not _address(seed.get("seed_signature")):
                raise ValueError("Receipt components require their own exact seed slot and signature")
            if seed.get("receipt_slot_boundary_verified") is not True:
                receipt_boundary_unverified = True
        bought, balance = _raw(seed.get("bought_raw")), _raw(seed.get("balance_raw"))
        lower = _raw(seed.get("retained_lower_raw", 0))
        upper = _raw(seed.get("retained_upper_raw", min(balance, bought)))
        if not 0 <= lower <= upper <= min(balance, bought):
            raise ValueError("Seed bounds exceed frozen purchases or token balance")
        nodes[account] = {"owner": owner, "balance": balance, "lower": lower,
                          "upper": upper, "depth": 0, "observed_slot": start}
        originals.add(owner)
        total += bought
    issues = set()
    complete = history_complete is True
    if not complete:
        issues.add("incomplete_history")
    if len(nodes) > max_accounts:
        issues.add("account_limit")
        nodes = dict(list(nodes.items())[:max_accounts])
        complete = False
    sold_lower = sold_upper = observed_sale = 0
    edges = []

    def gap(reason):
        nonlocal complete
        issues.add(reason)
        complete = False
        for node in nodes.values():
            node["lower"] = 0

    if not complete:
        gap("incomplete_history")
    if receipt_boundary_unverified:
        gap("receipt_seed_slot_boundary_unverified")
    # Conflicting duplicates poison the interval; identical repeated batches do not add volume.
    unique, conflicts = {}, set()
    if len(batches) > max_transactions:
        gap("transaction_limit")
        batches = []
    for batch in batches:
        if not isinstance(batch, Mapping) or batch.get("version") != VERSION or batch.get("mint") != mint:
            gap("invalid_batch_scope")
            continue
        signature = _address(batch.get("signature"))
        if not signature:
            gap("missing_transaction_signature")
            continue
        if signature in unique and unique[signature] != batch:
            conflicts.add(signature)
            gap("conflicting_duplicate_transaction")
        else:
            unique[signature] = deepcopy(batch)
    by_slot = defaultdict(list)
    for signature, batch in unique.items():
        if signature in conflicts:
            continue
        try:
            slot = _raw(batch.get("slot"))
            if not start < slot <= end:
                gap("transaction_outside_horizon")
                continue
            if batch.get("transaction_index") is not None:
                _raw(batch["transaction_index"])
        except ValueError:
            gap("missing_canonical_order")
            continue
        by_slot[slot].append(batch)
    ordered = []
    for slot in sorted(by_slot):
        group = by_slot[slot]
        indices = [b.get("transaction_index") for b in group]
        if len(group) > 1 and (None in indices or len(set(indices)) != len(indices)):
            gap("ambiguous_same_slot_order")
            # Do not replay an arbitrary signature order, even as a false exact ledger.
            continue
        ordered.extend(sorted(group, key=lambda b: _raw(b.get("transaction_index") or 0)))
    if sum(len(b.get("transfers") or []) for b in ordered) > max_events:
        gap("event_limit")
        ordered = []
    for batch in ordered:
        if batch.get("status") == "failed":
            continue
        if batch.get("status") not in {"parsed", "partial"}:
            gap("unavailable_transaction")
            continue
        if batch.get("status") == "partial":
            gap("partial_transaction")
        accounts = batch["accounts"]
        if any(not row["complete"] for row in accounts.values() if row.get("mint") == mint):
            gap("incomplete_transaction_accounts")
        for account, row in accounts.items():
            if account not in nodes or row.get("mint") != mint:
                continue
            node = nodes[account]
            if row.get("owner") != node["owner"]:
                gap("token_account_owner_changed")
                node["upper"] = 0
                continue
            if row.get("before_raw") is None:
                gap("missing_pre_balance")
                continue
            before = _raw(row["before_raw"])
            if before != node["balance"]:
                gap("ledger_gap")
                node["balance"] = before
                node["upper"] = min(node["upper"], before)
        for leg in batch["transfers"]:
            source, destination = leg["source_account"], leg["destination_account"]
            amount = _raw(leg["amount_raw"])
            if source == destination or not amount:
                continue
            sender = nodes.get(source)
            lower = upper = 0
            source_row, destination_row = accounts[source], accounts[destination]
            valid = source_row["complete"] and destination_row["complete"]
            if sender and leg["source_owner"] == sender["owner"]:
                balance = sender["balance"]
                if amount > balance:
                    gap("unreconciled_intermediate_balance")
                    sender["upper"] = 0
                else:
                    lower = max(0, amount - (balance - sender["lower"])) if valid else 0
                    upper = min(amount, sender["upper"])
                    sender.update(balance=balance - amount, lower=max(0, sender["lower"] - amount),
                                  upper=min(sender["upper"], balance - amount), observed_slot=batch["slot"])
            sale = leg["kind"] == "verified_sale" and valid
            service = destination in excluded_accounts or leg["destination_owner"] in excluded_owners
            if sale:
                observed_sale += amount
                sold_lower += lower
                sold_upper += upper
                resolution = "verified_sale"
            elif service:
                resolution = "service_destination_unresolved"
            elif not valid:
                resolution = "unknown_outflow"
            else:
                resolution = "direct_transfer"
            recipient = nodes.get(destination)
            if not sale and not service and valid and not recipient and sender and upper:
                depth = sender["depth"] + 1
                if len(nodes) >= max_accounts:
                    issues.add("account_limit")
                elif depth > max_depth:
                    issues.add("depth_limit")
                else:
                    recipient = nodes[destination] = {"owner": leg["destination_owner"],
                        "balance": _raw(destination_row["before_raw"]), "lower": 0, "upper": 0,
                        "depth": depth, "observed_slot": batch["slot"]}
            if recipient and not sale and not service and recipient["owner"] == leg["destination_owner"]:
                recipient["balance"] += amount
                recipient["lower"] += lower if valid else 0
                recipient["upper"] += upper if valid else 0
                recipient["observed_slot"] = batch["slot"]
            if sender:
                edges.append({"signature": batch["signature"], "slot": batch["slot"],
                    "outer_index": leg["outer_index"], "inner_index": leg["inner_index"],
                    "source_account": source, "source_owner": leg["source_owner"],
                    "destination_account": destination, "destination_owner": leg["destination_owner"],
                    "amount_raw": str(amount), "attributed_lower_raw": str(lower),
                    "attributed_upper_raw": str(upper), "resolution": resolution,
                    "disposition": "sale" if sale else "custody" if service else "transfer" if valid else "unknown",
                    "common_control": "not_established"})
        for account, row in accounts.items():
            if account not in nodes or row.get("mint") != mint or row.get("after_raw") is None:
                continue
            node = nodes[account]
            after = _raw(row["after_raw"])
            if node["balance"] != after:
                gap("ledger_gap")
                node["balance"] = after
                node["upper"] = min(node["upper"], after)
            node["observed_slot"] = batch["slot"]
    closing = {}
    for row in closing_balances:
        account = row.get("account")
        if (row.get("mint") != mint or row.get("slot") != end or account not in nodes
                or row.get("owner") != nodes[account]["owner"]):
            gap("invalid_closing_balance_scope")
            continue
        try:
            amount = _raw(row.get("balance_raw"))
        except ValueError:
            gap("invalid_closing_balance_amount")
            continue
        if account in closing and closing[account] != amount:
            gap("conflicting_closing_balance")
            closing[account] = None
        elif account not in closing:
            closing[account] = amount
    for account, amount in closing.items():
        if amount is None:
            continue
        node = nodes[account]
        if amount != node["balance"]:
            gap("closing_balance_mismatch")
        node["balance"] = amount
        node["upper"] = min(node["upper"], amount)
        node["observed_slot"] = end
    original = sum(n["lower"] for n in nodes.values() if n["owner"] in originals)
    transferred = sum(n["lower"] for n in nodes.values() if n["owner"] not in originals)
    unknown = total - original - transferred - sold_lower
    if unknown < 0:
        raise ValueError("Attribution exceeds the frozen original position")
    remaining = total - sold_lower
    upper_original = min(remaining - transferred,
        sum(n["balance"] for n in nodes.values() if n["owner"] in originals),
        unknown + sum(n["upper"] for n in nodes.values() if n["owner"] in originals)) if complete else remaining
    upper_transferred = min(remaining - original,
        unknown + sum(n["upper"] for n in nodes.values() if n["owner"] not in originals)) if complete else remaining
    locations = []
    for account, node in sorted(nodes.items()):
        current = complete or closing.get(account) is not None
        locations.append({"account": account, "owner": node["owner"], "mint": mint,
            "role": "original" if node["owner"] in originals else "transfer_recipient",
            "depth": node["depth"], "current_balance_raw": str(node["balance"]) if current else None,
            "last_observed_balance_raw": str(node["balance"]), "observed_slot": node["observed_slot"],
            "held_lower_bound_raw": str(node["lower"]),
            "held_upper_bound_raw": str(min(node["upper"] + unknown if complete else remaining, node["balance"])) if current else str(remaining),
            "ownership": "not_established"})
    return {"version": VERSION, "mint": mint, "from_slot": start, "checked_slot": end,
        "status": "checked" if complete and not issues else "partial",
        "history_complete": complete, "history_basis": "caller_asserted_account_history" if history_complete is True else "supplied_subset",
        "ownership": "not_established", "bought_raw": str(total),
        "amounts_raw": {"original": str(original), "transferred": str(transferred),
                        "sold": str(sold_lower), "unknown": str(unknown)},
        "upper_bounds_raw": {"original": str(upper_original), "transferred": str(upper_transferred),
                             "sold": str(min(total - original - transferred, sold_upper)), "unknown": str(unknown)},
        "upper_bounds_are_non_additive": True, "observed_verified_sale_raw": str(observed_sale),
        "locations": locations, "edges": edges, "issues": sorted(issues),
        "limits": {"transactions": max_transactions, "events": max_events,
                   "accounts": max_accounts, "depth": max_depth},
        "confirmation_eligible": False}


def resolve_position_history(mint, seeds, history_fetcher, *, from_slot, to_slot,
                             decode_transaction=None, swap_program_ids=(),
                             service_accounts=(), service_owners=(),
                             max_depth=2, max_addresses=32, max_transactions=256,
                             max_events=1024, max_pages=128, page_size=64,
                             max_instructions=512, max_transaction_accounts=256):
    """Fetch and replay one frozen position; all network access belongs to the caller.

    ``history_fetcher(account, *, mint, from_slot, to_slot, cursor, limit)``
    (or an adapter's ``history_page`` method) returns a mapping::

        {"transactions": [jsonParsed_getTransaction_result, ...],
         "next_cursor": "opaque cursor" or None,
         "coverage": {"provider": "provider identifier", "account": account,
                      "mint": mint, "from_slot": from_slot, "to_slot": to_slot,
                      "scope": "all_token_account_activity",
                      "commitment": "finalized", "complete": True},
         "closing_balance": {"account": account, "owner": owner, "mint": mint,
                             "slot": to_slot, "balance_raw": "0"}}

    The interval is (from_slot, to_slot], with seeds AFTER from_slot. Each page
    repeats its scope/provider; only a terminal page may assert complete=True.
    This attests to ALL account activity across the accumulated pages, including
    non-pool transfers, failed transactions, and available transaction bodies.
    An empty page, exhausted cursor, missing/closed account, provider's retention
    floor, or an RPC signature list alone is NOT evidence of complete history.
    Adapters must paginate/filter correctly and pin the finalized horizon. An
    optional closing balance is exact evidence at to_slot, not a live RPC balance.
    Optional coverage.available_from_slot and coverage.gaps report pruning and
    missing history; either contradicting the interval prevents completeness.
    For freeze_receipt_seeds rows, coverage.seed_boundary must match the seed's
    {account, slot, signature, owner, mint, decimals, balance_raw, bought_raw,
    no_later_successful_activity: True} on a finalized page. Raw amounts and
    identity must match the finalized seed receipt, not merely its signature.
    This attests that its receipt post-state is also its slot-end state;
    absent proof degrades all original attribution. Merely exhausting a cursor
    for slots strictly AFTER the seed cannot verify the seed-slot tail.

    ``decode_transaction(tx)`` optionally returns {transaction_index,
    swap_witnesses}. It supplies canonical block order and trusted protocol swap
    witnesses, NOT a buy/sell label; the existing parser validates exact legs.

    Limits bound token-account addresses (not owners), accepted transaction rows
    INCLUDING duplicates, total page calls, replay events and parser work. A page
    exceeding the requested row limit is rejected, not truncated. Each account
    is fetched once with bounded pagination; cycles/overlapping histories replay
    each signature once. Discovery uses only possible original-position flow,
    not new buys. Public services are terminal custody observations, not owned
    children or proven sales. Custody remains in the unknown amount partition.

    The existing summary schema is preserved, with coverage and marginal
    original_position_bounds_raw added. Missing coverage anywhere deliberately
    degrades the whole position; per-account independent retention is not proved.
    With full provider coverage, an uninterpretable later batch clears ongoing
    provenance but does not reverse an original-position sale already proved.
    For targeted rechecks, seed the disjoint frozen accounts of selected owners
    jointly and label the report as that subset, never the entire cohort. Replace
    overlapping shadow snapshots rather than summing them. Current UI balances
    or historical float buy amounts cannot manufacture exact attributed seeds.
    max_pages bounds callback calls, not their internal RPCs: the adapter must
    enforce the parent's shared request budget, retry limits and timeouts.
    Nothing here changes scanner retention/invalidation or confirms a signal.
    Invalid caller inputs raise ValueError before IO; provider/decoder failures
    become partial evidence. Inputs and callback transaction objects are not mutated.
    """
    fetch = history_fetcher if callable(history_fetcher) else getattr(history_fetcher, "history_page", None)
    if not callable(fetch) or (decode_transaction is not None and not callable(decode_transaction)):
        raise ValueError("Expected a history callback/adapter and optional decoder callback")
    if not isinstance(seeds, Sequence) or isinstance(seeds, (str, bytes)) or not seeds:
        raise ValueError("At least one frozen token-account seed is required")
    start, end = _raw(from_slot), _raw(to_slot)
    depth, addresses, transactions, events, pages, size, instructions, tx_accounts = map(
        _raw, (max_depth, max_addresses, max_transactions, max_events, max_pages,
               page_size, max_instructions, max_transaction_accounts))
    if not size:
        raise ValueError("page_size must be positive")
    seeds = deepcopy(list(seeds))
    services, owners, programs = tuple(service_accounts), tuple(service_owners), tuple(swap_program_ids)
    replay_options = dict(from_slot=start, to_slot=end, service_accounts=services,
                          service_owners=owners, max_transactions=transactions,
                          max_events=events, max_accounts=addresses, max_depth=depth)
    # Validate the whole seed denominator before any provider calls, even if capped.
    initial = analyze_position(mint, seeds, [], **replay_options)
    receipt_seeds = {}
    for seed in seeds:
        if seed.get("scope") == "receipt_component_not_entire_cohort":
            if _raw(seed.get("seed_slot")) != start or not _address(seed.get("seed_signature")):
                raise ValueError("Receipt components require their own exact seed slot and signature")
            receipt_seeds[seed["account"]] = seed
    collected, raw_transactions, conflicts = {}, {}, set()
    records, calls = 0, 0
    issues = set()
    interpretation_issues = {"incomplete_transaction_data", "transaction_decode_error", "invalid_swap_witnesses"}
    histories = {}
    queue = []
    closing = []

    def batches():
        return [dict(batch, status="unavailable", transfers=[]) if signature in conflicts else batch
                for signature, batch in collected.items()]

    def discover(summary):
        issues.update(set(summary["issues"]) & {"account_limit", "depth_limit", "event_limit"})
        for location in sorted(summary["locations"], key=lambda row: (row["depth"], row["account"])):
            account = location["account"]
            if account not in histories:
                histories[account] = {"account": account, "owner": location["owner"],
                    "depth": location["depth"], "provider": None, "commitment": None,
                    "providers": [], "available_from_slot": None, "reported_gap_pages": 0,
                    "from_slot": start, "to_slot": end, "history_complete": False,
                    "provider_history_complete": False,
                    "receipt_boundary_verified": False if account in receipt_seeds else None,
                    "cursor_exhausted": False, "pages": 0, "transactions_received": 0,
                    "status": "unavailable", "issues": []}
                queue.append(account)

    discover(initial)
    while queue:
        account = queue.pop(0)
        history = histories[account]
        account_issues = set()
        cursor, seen_cursors = None, {None}
        while True:
            if calls >= pages or records >= transactions:
                account_issues.add("page_limit" if calls >= pages else "transaction_limit")
                break
            limit = min(size, transactions - records)
            calls += 1
            history["pages"] += 1
            try:
                page = fetch(account, mint=mint, from_slot=start, to_slot=end,
                             cursor=cursor, limit=limit)
            except Exception:
                # Provider exceptions can contain credentials; do not export their text.
                account_issues.add("history_fetch_error")
                break
            if not isinstance(page, Mapping) or not isinstance(page.get("transactions"), list):
                account_issues.add("invalid_history_page")
                break
            rows = page["transactions"]
            if len(rows) > limit:
                account_issues.add("history_page_transaction_limit")
                break
            coverage = page.get("coverage")
            if not isinstance(coverage, Mapping):
                coverage = {}
            provider = _address(coverage.get("provider"))
            if not provider:
                account_issues.add("missing_provider_identity")
            else:
                if provider not in history["providers"]:
                    history["providers"].append(provider)
                if history["provider"] is None:
                    history["provider"] = provider
                elif history["provider"] != provider:
                    account_issues.add("history_provider_changed")
            try:
                scope_start, scope_end = _raw(coverage.get("from_slot")), _raw(coverage.get("to_slot"))
            except ValueError:
                scope_start = scope_end = None
            if (coverage.get("account") != account or coverage.get("mint") != mint
                    or scope_start != start or scope_end != end
                    or coverage.get("scope") != "all_token_account_activity"):
                account_issues.add("invalid_history_coverage_scope")
            if "available_from_slot" in coverage:
                try:
                    floor = _raw(coverage["available_from_slot"])
                    history["available_from_slot"] = max(floor, history["available_from_slot"] or 0)
                    if start < end and floor > start + 1:
                        account_issues.add("provider_history_pruned")
                except ValueError:
                    account_issues.add("invalid_provider_history_floor")
            if coverage.get("gaps"):
                history["reported_gap_pages"] += 1
                account_issues.add("provider_reported_history_gaps")
            commitment = coverage.get("commitment")
            history["commitment"] = commitment if isinstance(commitment, str) else None
            if commitment != "finalized":
                account_issues.add("unfinalized_history")
            if account in receipt_seeds and "seed_boundary" in coverage:
                proof = coverage["seed_boundary"]
                try:
                    valid_boundary = (isinstance(proof, Mapping) and proof.get("account") == account
                        and _raw(proof.get("slot")) == start
                        and proof.get("signature") == receipt_seeds[account]["seed_signature"]
                        and proof.get("owner") == receipt_seeds[account]["owner"]
                        and proof.get("mint") == mint
                        and _raw(proof.get("decimals")) == _raw(receipt_seeds[account].get("decimals"))
                        and _raw(proof.get("balance_raw")) == _raw(receipt_seeds[account]["balance_raw"])
                        and _raw(proof.get("bought_raw")) == _raw(receipt_seeds[account]["bought_raw"])
                        and proof.get("no_later_successful_activity") is True and commitment == "finalized")
                except ValueError:
                    valid_boundary = False
                if valid_boundary:
                    history["receipt_boundary_verified"] = "receipt_seed_slot_boundary_unverified" not in account_issues
                else:
                    history["receipt_boundary_verified"] = False
                    account_issues.add("receipt_seed_slot_boundary_unverified")
            next_cursor = page.get("next_cursor")
            if next_cursor is not None and not _address(next_cursor):
                account_issues.add("invalid_history_cursor")
                break
            if next_cursor is not None and coverage.get("complete") is True:
                account_issues.add("premature_history_complete")
            balance = page.get("closing_balance")
            if balance is not None:
                if isinstance(balance, Mapping) and balance.get("account") == account:
                    closing.append(deepcopy(balance))
                else:
                    account_issues.add("invalid_closing_balance_scope")
            records += len(rows)
            history["transactions_received"] += len(rows)
            for tx in rows:
                signature = None
                if isinstance(tx, Mapping) and isinstance(tx.get("transaction"), Mapping):
                    signatures = tx["transaction"].get("signatures")
                    if isinstance(signatures, list) and signatures:
                        signature = _address(signatures[0])
                if not signature:
                    account_issues.add("unavailable_transaction")
                    continue
                message = tx["transaction"].get("message")
                keys = message.get("accountKeys") if isinstance(message, Mapping) else None
                if (not isinstance(keys, list) or len(keys) > tx_accounts
                        or not any((key.get("pubkey") if isinstance(key, Mapping) else key) == account for key in keys)):
                    account_issues.add("transaction_missing_queried_account")
                if signature in raw_transactions:
                    if tx != raw_transactions[signature]:
                        conflicts.add(signature)
                        account_issues.add("conflicting_duplicate_transaction")
                    if collected[signature]["status"] not in {"parsed", "failed"}:
                        account_issues.add("incomplete_transaction_data")
                    continue
                metadata = {}
                decoder_issue = None
                if decode_transaction is not None:
                    try:
                        metadata = decode_transaction(deepcopy(tx))
                        if not isinstance(metadata, Mapping):
                            raise ValueError("Invalid transaction decoder result")
                    except Exception:
                        account_issues.add("transaction_decode_error")
                        decoder_issue = "transaction_decode_error"
                        metadata = {}
                witnesses = metadata.get("swap_witnesses", ())
                if (not isinstance(witnesses, Sequence) or isinstance(witnesses, (str, bytes))
                        or len(witnesses) > instructions):
                    account_issues.add("invalid_swap_witnesses")
                    decoder_issue = "invalid_swap_witnesses"
                    witnesses = ()
                batch = parse_position_transaction(tx, mint,
                    transaction_index=metadata.get("transaction_index"),
                    swap_witnesses=witnesses, swap_program_ids=programs,
                    max_instructions=instructions, max_accounts=tx_accounts)
                if decoder_issue and batch["status"] == "parsed":
                    batch["status"] = "partial"
                    batch["issues"] = sorted(set(batch["issues"]) | {decoder_issue})
                raw_transactions[signature] = deepcopy(tx)
                collected[signature] = batch
                if batch["status"] not in {"parsed", "failed"}:
                    account_issues.add("incomplete_transaction_data")
                if batch["slot"] is None or not start < batch["slot"] <= end:
                    account_issues.add("transaction_outside_horizon")
            if next_cursor is None:
                history["cursor_exhausted"] = True
                if coverage.get("complete") is not True:
                    account_issues.add("incomplete_account_history")
                if account in receipt_seeds and not history["receipt_boundary_verified"]:
                    account_issues.add("receipt_seed_slot_boundary_unverified")
                history["history_complete"] = not account_issues
                history["provider_history_complete"] = not (account_issues - interpretation_issues)
                break
            if next_cursor in seen_cursors:
                account_issues.add("history_cursor_cycle")
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        history["issues"] = sorted(account_issues)
        history["status"] = "complete" if history["history_complete"] else "partial" if history["pages"] else "unavailable"
        issues.update(account_issues)
        # Discovery needs possible flow only, never a fabricated complete-history claim.
        discover(analyze_position(mint, seeds, batches(), **replay_options))

    coverage_complete = bool(histories) and not (issues - interpretation_issues) and all(
        history["provider_history_complete"] for history in histories.values())
    replay_seeds = [dict(seed, receipt_slot_boundary_verified=coverage_complete
                        and histories.get(seed["account"], {}).get("receipt_boundary_verified") is True)
                    if seed["account"] in receipt_seeds else seed for seed in seeds]
    result = analyze_position(mint, replay_seeds, batches(), history_complete=coverage_complete,
                              closing_balances=closing, **replay_options)
    result["issues"] = sorted(set(result["issues"]) | issues)
    if result["issues"]:
        result["status"] = "partial"
    result["history_basis"] = "provider_asserted_token_account_history" if coverage_complete else "bounded_partial_account_history"
    result["resolver_version"] = 1
    if receipt_seeds:
        result["scope"] = "receipt_component_not_entire_cohort"
        result["denominator"] = "selected_receipt_net_positive_raw_delta"
        result["original_cohort_denominator_complete"] = False
    result["coverage"] = {"scope": "bounded_token_account_history", "from_slot": start,
        "to_slot": end, "providers": sorted({p for h in histories.values() for p in h["providers"]}),
        "provider_history_complete": coverage_complete,
        "complete": result["history_complete"] and not result["issues"],
        "accounts": list(histories.values()), "addresses_discovered": len(histories),
        "addresses_requested": sum(h["pages"] > 0 for h in histories.values()),
        "pages_requested": calls, "transactions_received": records,
        "unique_transactions": len(collected), "conflicting_signatures": sorted(conflicts),
        "transaction_issues": [{"signature": b["signature"], "slot": b["slot"],
                                "status": b["status"], "issues": b["issues"]}
                               for b in collected.values() if b["issues"]],
        "limits_reached": sorted(set(result["issues"]) & {"account_limit", "depth_limit", "event_limit",
                                                           "transaction_limit", "page_limit", "history_page_transaction_limit"}),
        "cycle_policy": "fetch_account_once_replay_signature_once"}
    result["original_position_bounds_raw"] = {
        kind: {"lower_raw": value, "upper_raw": result["upper_bounds_raw"][kind]}
        for kind, value in result["amounts_raw"].items()}
    result["affects_original_cohort_retention"] = False
    result["limits"].update(addresses=addresses, pages=pages, page_size=size,
                            instructions=instructions, transaction_accounts=tx_accounts)
    for edge in result["edges"]:
        destination = edge["destination_account"]
        if edge["disposition"] == "sale":
            follow = "sale_terminal"
        elif edge["disposition"] == "custody":
            follow = "public_service_terminal_beneficiary_unresolved"
        elif edge["disposition"] == "unknown":
            follow = "unsupported_flow"
        elif destination in histories:
            follow = "account_history_" + histories[destination]["status"]
        else:
            follow = "outside_bounded_lineage"
        edge["follow_status"] = follow
    return result


def _timestamp(value):
    if isinstance(value, str) and not value.isdigit():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Timestamp must be timezone-aware")
            value = parsed.timestamp()
        except (ValueError, OverflowError):
            raise ValueError("Expected Unix seconds or timezone-aware ISO timestamp") from None
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        return int(value) if value.is_integer() else value
    return _raw(value)


def _ui_raw(value, decimals):
    """Legacy pool rows are candidates only; never round float amounts into evidence."""
    try:
        amount = Decimal(str(value)) * (Decimal(10) ** decimals)
        if amount.is_finite() and amount > 0 and amount == amount.to_integral_value():
            return int(amount)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        pass
    return None


def _pool_sale_candidate(batch, swap):
    """Cross-check a legacy pool classification, without promoting it to swap proof."""
    if (swap.get("kind") != "sell" or swap.get("token_address") != batch["mint"]
            or swap.get("signature") != batch["signature"]
            or not _address(swap.get("coordination_sale_owner")) or not _address(swap.get("pool_address"))):
        return set()
    owner, pool = swap["coordination_sale_owner"], swap["pool_address"]
    legs, accounts = batch["transfers"], batch["accounts"]
    inputs = [i for i, leg in enumerate(legs) if leg["source_owner"] == owner
              and leg["destination_owner"] == pool and accounts[leg["source_account"]]["complete"]
              and accounts[leg["destination_account"]]["complete"]]
    if not inputs:
        return set()
    decimals = {accounts[legs[i]["source_account"]]["decimals"] for i in inputs}
    if len(decimals) != 1:
        return set()
    amount = _ui_raw(swap.get("coordination_sale_amount"), next(iter(decimals)))
    if amount != sum(int(legs[i]["amount_raw"]) for i in inputs):
        return set()
    # Unresolved or additional sellers into the vault prevent a largest-sender shortcut.
    if any(leg["destination_owner"] == pool and i not in inputs for i, leg in enumerate(legs)):
        return set()
    owner_delta = sum(int(row["after_raw"]) - int(row["before_raw"])
                      for row in accounts.values() if row["mint"] == batch["mint"]
                      and row["owner"] == owner and row["complete"])
    pool_delta = sum(int(row["after_raw"]) - int(row["before_raw"])
                     for row in accounts.values() if row["mint"] == batch["mint"]
                     and row["owner"] == pool and row["complete"])
    if owner_delta != -amount or pool_delta != amount:
        return set()
    # Both sides of another exact token leg must reflect proceeds; native deltas
    # alone also contain rent/fees and are deliberately insufficient here.
    quote_deltas = defaultdict(lambda: [0, 0])
    for row in accounts.values():
        if row["mint"] != batch["mint"] and row["complete"]:
            delta = int(row["after_raw"]) - int(row["before_raw"])
            if row["owner"] == owner:
                quote_deltas[row["mint"]][0] += delta
            if row["owner"] == pool:
                quote_deltas[row["mint"]][1] += delta
    return set(inputs) if any(credit > 0 and debit == -credit for credit, debit in quote_deltas.values()) else set()


def annotate_pool_activity(mint, transactions, parsed_pool_swaps, cohort_owners, *,
                           signal_timestamp, checked_timestamp=None,
                           service_accounts=(), service_owners=(),
                           swap_witnesses=(), swap_program_ids=(),
                           max_transactions=256, max_events=1024, max_owners=64):
    """Useful observations from partial pool history, without a position assertion.

    Accepts existing raw jsonParsed transactions, existing parse_pool_swap rows,
    frozen owner addresses, and the cohort signal timestamp (Unix seconds or
    timezone-aware ISO). Only strictly later transactions are counted; equal
    seconds are ambiguous. No seeds, ATA-history fetches or canonical tx order
    are required because this annotates activity, not a sequential inventory.

    verified_sale_raw requires the trusted decoder witness described above.
    reconciled_pool_sale_candidate_raw cross-checks the legacy pool swap parser
    but is NOT a verified sale. Gross amounts belong to the observed owner, not
    necessarily its original signal inventory; buys/rebuys do not restore it.
    unknown_outflow_raw is a reconciled unmatched debit, not a sale or a full
    estimate of missing outflows. Zero means none found in the supplied subset.
    Use affects_original_cohort_retention=False as an integration guardrail.
    """
    if not _address(mint) or not all(isinstance(rows, Sequence) and not isinstance(rows, (str, bytes))
                                   for rows in (transactions, parsed_pool_swaps, cohort_owners)):
        raise ValueError("Expected exact mint and bounded observation/owner sequences")
    start = _timestamp(signal_timestamp)
    checked = _timestamp(checked_timestamp) if checked_timestamp is not None else None
    if checked is not None and checked < start:
        raise ValueError("Observation horizon precedes signal")
    max_transactions, max_events, max_owners = map(_raw, (max_transactions, max_events, max_owners))
    if not all(_address(owner) for owner in cohort_owners):
        raise ValueError("Cohort must contain exact owner addresses")
    owners = sorted(set(cohort_owners))
    issues = {"pool_history_not_wallet_history"}
    if len(owners) > max_owners:
        owners = owners[:max_owners]
        issues.add("owner_limit")
    fields = ("verified_sale_raw", "direct_transfer_raw", "service_outflow_raw", "unknown_outflow_raw",
              "reconciled_pool_sale_candidate_raw", "internal_transfer_raw")
    totals = {owner: dict.fromkeys(fields, 0) for owner in owners}
    observations = []
    unique, conflicts, swaps = {}, set(), defaultdict(list)
    excluded_owners = set(service_owners)
    excluded_accounts = set(service_accounts)
    if len(transactions) > max_transactions or len(parsed_pool_swaps) > max_events or len(swap_witnesses) > max_events:
        issues.add("observation_limit")
        transactions, parsed_pool_swaps = [], []
    for swap in parsed_pool_swaps:
        if isinstance(swap, Mapping) and swap.get("token_address") == mint and _address(swap.get("signature")):
            if swap not in swaps[swap["signature"]]:
                swaps[swap["signature"]].append(swap)
            if _address(swap.get("pool_address")):
                excluded_owners.add(swap["pool_address"])
    witnesses = defaultdict(list)
    for witness in swap_witnesses:
        if isinstance(witness, Mapping) and _address(witness.get("signature")):
            witnesses[witness["signature"]].append(witness)
    for tx in transactions:
        transaction = tx.get("transaction") if isinstance(tx, Mapping) else None
        signatures = transaction.get("signatures") if isinstance(transaction, Mapping) else None
        signature = signatures[0] if isinstance(signatures, list) and signatures else None
        batch = parse_position_transaction(tx, mint, swap_witnesses=witnesses.get(signature, ()),
                                           swap_program_ids=swap_program_ids)
        signature = batch["signature"]
        if batch["status"] == "failed":
            continue
        if not signature or batch["status"] == "unavailable":
            issues.update(batch["issues"])
            continue
        try:
            at = _timestamp(tx.get("blockTime"))
        except ValueError:
            issues.add("missing_transaction_timestamp")
            continue
        if math.floor(at) == math.floor(start):
            issues.add("same_timestamp_as_signal")
            continue
        if at < start:
            continue
        if checked is not None and at > checked:
            issues.add("transaction_after_checked_timestamp")
            continue
        record = (at, batch)
        if signature in unique and unique[signature] != record:
            conflicts.add(signature)
            issues.add("conflicting_duplicate_transaction")
        else:
            unique[signature] = record
    records = [record for signature, record in unique.items() if signature not in conflicts]
    # Budget unmatched debits too, so missing parsed instructions cannot produce
    # an unbounded observations array through balance rows alone.
    possible_events = sum(len(batch["transfers"]) + sum(
        row.get("mint") == mint and row.get("owner") in totals for row in batch["accounts"].values())
        for _, batch in records)
    if possible_events > max_events:
        records = []
        issues.add("event_limit")
    for at, batch in sorted(records, key=lambda row: (row[0], row[1]["slot"], row[1]["signature"])):
        issues.update(batch["issues"])
        accounts = batch["accounts"]
        candidates = swaps[batch["signature"]]
        candidate_indices = _pool_sale_candidate(batch, candidates[0]) if len(candidates) == 1 else set()
        if len(candidates) > 1:
            issues.add("conflicting_pool_swap_rows")
        visible_delta = defaultdict(int)
        for index, leg in enumerate(batch["transfers"]):
            amount = int(leg["amount_raw"])
            visible_delta[leg["source_account"]] -= amount
            visible_delta[leg["destination_account"]] += amount
            owner = leg["source_owner"]
            if owner not in totals:
                continue
            source, destination = accounts[leg["source_account"]], accounts[leg["destination_account"]]
            if not source["identity_resolved"] or not destination["identity_resolved"]:
                issues.add("unresolved_transfer_owner")
                continue
            if leg["source_account"] == leg["destination_account"]:
                continue
            if leg["kind"] == "verified_sale":
                field, resolution = "verified_sale_raw", "verified_sale"
            elif index in candidate_indices:
                field, resolution = "reconciled_pool_sale_candidate_raw", "reconciled_pool_sale_candidate"
            elif leg["destination_account"] in excluded_accounts or leg["destination_owner"] in excluded_owners:
                field, resolution = "service_outflow_raw", "service_destination_unresolved"
            elif owner == leg["destination_owner"]:
                field, resolution = "internal_transfer_raw", "same_spl_owner_account_transfer"
            else:
                field, resolution = "direct_transfer_raw", "direct_transfer_not_ownership"
            totals[owner][field] += amount
            observations.append(dict(leg, signature=batch["signature"], slot=batch["slot"],
                timestamp=at, resolution=resolution, original_position_attribution="unknown",
                common_control="not_established"))
        for account, row in accounts.items():
            owner = row["owner"]
            if (owner not in totals or row["mint"] != mint or not row["identity_resolved"]
                    or row["before_raw"] is None or row["after_raw"] is None):
                continue
            unexplained = max(0, int(row["before_raw"]) + visible_delta[account] - int(row["after_raw"]))
            if unexplained:
                totals[owner]["unknown_outflow_raw"] += unexplained
                observations.append({"signature": batch["signature"], "slot": batch["slot"],
                    "timestamp": at, "mint": mint, "source_account": account, "source_owner": owner,
                    "amount_raw": str(unexplained), "resolution": "unknown_outflow_not_sale",
                    "original_position_attribution": "unknown", "common_control": "not_established"})
    return {"version": VERSION, "mint": mint, "signal_timestamp": start, "checked_timestamp": checked,
        "status": "partial", "scope": "supplied_pool_transactions_strictly_after_signal",
        "wallet_history_complete": False, "unresolved_outflows_possible": True,
        "ownership": "not_established", "affects_original_cohort_retention": False,
        "confirmation_eligible": False, "original_position_sold_raw": None, "retention_bounds_raw": None,
        "owners": {owner: {**{key: str(value) for key, value in amounts.items()},
            "resolution": "observed_activity_only_not_original_inventory"} for owner, amounts in totals.items()},
        "observations": observations, "issues": sorted(issues),
        "limits": {"transactions": max_transactions, "events": max_events, "owners": max_owners}}
