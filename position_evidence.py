"""Conservative token provenance. Location is not beneficial ownership."""
import copy
from collections import defaultdict


def seed_position(wallets, block, block_hash, supply):
    return {"version": 1, "from_block": block, "block": block, "block_hash": block_hash,
        "supply_raw": str(supply), "bought_raw": str(sum(int(w["bought_raw"]) for w in wallets)),
        "sold_raw": "0", "originals": [w["address"] for w in wallets],
        "nodes": {w["address"]: {"balance": w["balance_raw"], "known": w["retained_lower_bound_raw"], "depth": 0}
                  for w in wallets}}


def replay_position(previous, transfers, nodes, balances, sold_events, block, block_hash):
    """Only transfer the guaranteed attributable part of a mixed balance."""
    result = copy.deepcopy(previous)
    result["nodes"] = copy.deepcopy(nodes)
    sold = int(result["sold_raw"])
    seen = set()
    for event in sorted(transfers, key=lambda e: (e["block"], e["index"])):
        key = (event["tx"], event["index"])
        if key in seen:
            continue
        seen.add(key)
        sender, recipient, amount = event["sender"], event["recipient"], event["amount"]
        if sender == recipient:
            continue
        guaranteed = 0
        if sender in result["nodes"]:
            node = result["nodes"][sender]
            balance, known = int(node["balance"]), int(node["known"])
            if amount > balance:
                raise ValueError("Transfer ledger does not reconcile")
            guaranteed = max(0, amount - (balance - known))
            node.update(balance=str(balance - amount), known=str(max(0, known - amount)))
        if recipient in result["nodes"]:
            node = result["nodes"][recipient]
            node.update(balance=str(int(node["balance"]) + amount), known=str(int(node["known"]) + guaranteed))
        elif key in sold_events:
            sold += guaranteed
        # Untracked destinations and fungible mixing remain unresolved, never called sales.
    for wallet, node in result["nodes"].items():
        if int(node["balance"]) != balances[wallet]:
            raise ValueError("End balance does not reconcile")
    result.update(sold_raw=str(sold), block=block, block_hash=block_hash)
    position_summary(result)  # Conservation assertion before durable checkpoint.
    return result


def position_summary(state):
    originals = set(state["originals"])
    original = sum(int(n["known"]) for w, n in state["nodes"].items() if w in originals)
    moved = sum(int(n["known"]) for w, n in state["nodes"].items() if w not in originals)
    sold, bought = int(state["sold_raw"]), int(state["bought_raw"])
    unknown = bought - original - moved - sold
    if min(original, moved, sold, unknown) < 0:
        raise ValueError("Position accounting exceeds attributed purchases")
    amounts = dict(original=original, transferred=moved, sold=sold, unknown=unknown)
    return {"status": "checked", "from_block": state["from_block"], "checked_block": state["block"],
        "denominator": "total_supply", "ownership": "not_established",
        "amounts_raw": {k: str(v) for k, v in amounts.items()},
        "supply_pct": {k: 100 * v / int(state["supply_raw"]) for k, v in amounts.items()},
        "recipients": [{"address": w, "held_raw": n["known"], "supply_pct": 100 * int(n["known"]) / int(state["supply_raw"]), "depth": n["depth"]}
                       for w, n in state["nodes"].items() if w not in originals and int(n["known"]) > 0]}


def cross_chain_summary(events, chain_id, supply, coverage):
    unique = {e["transaction"]: e for e in events}
    cross = [e for e in unique.values() if isinstance(e.get("source_chain_id"), int) and e["source_chain_id"] != chain_id]
    sources = {(e["source_chain_id"], e["source_address"]) for e in cross}
    return {"verified_buys": len(cross), "recipients": len({e["recipient"] for e in cross}),
        "source_wallets": len(sources), "source_chains": sorted({e["source_chain_id"] for e in cross}),
        "gross_bought_supply_pct": 100 * sum(int(e["bought_raw"]) for e in cross) / supply,
        "verified_services": sorted({e.get("service", "Relay") for e in cross}),
        "coverage": coverage, "other_services": "not_verified", "baseline_status": "not_established"}


def preparation_summary(buys, funding, excluded):
    """Coinciding direct quote funding and buys; unclassified payers are not owners."""
    first = {}
    for buy in buys:
        wallet = buy["recipient"]
        if wallet not in first or buy["block"] < first[wallet]["block"]:
            first[wallet] = buy
    groups = defaultdict(dict)
    for event in funding:
        buy = first.get(event["recipient"])
        if not buy or event["sender"] in excluded or event["sender"] in first or event["amount"] <= 0:
            continue
        # Same-block ordering is deliberately excluded without a complete funding receipt.
        if event["block"] >= buy["block"] or not 0 < buy["timestamp"] - event["timestamp"] <= 3600:
            continue
        groups[event["sender"]][event["recipient"]] = event
    matches = []
    for sender, wallets in groups.items():
        times = sorted({e["timestamp"] for e in wallets.values()})
        for end in times:
            selected = {w: e for w, e in wallets.items() if end - 900 < e["timestamp"] <= end}
            if len(selected) < 3:
                continue
            buy_times = [first[w]["timestamp"] for w in selected]
            if max(buy_times) - min(buy_times) > 900:
                continue
            matches.append({"source": sender, "wallets": sorted(selected), "wallet_count": len(selected),
                "funding_transactions": sorted({e["tx"] for e in selected.values()}),
                "funding_span_seconds": end - min(e["timestamp"] for e in selected.values()),
                "buy_span_seconds": max(buy_times) - min(buy_times),
                "strength": "coincidence", "source_identity": "unclassified"})
            break
    return {"status": "coincidences" if matches else "no_match_in_checked_subset", "groups": matches,
        "ownership": "not_established", "scope": "Robinhood quote-token transfers before selected buys",
        "source_chain_funding": "not_checked"}
