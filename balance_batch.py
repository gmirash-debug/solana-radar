"""Conservative known-token-account batching; never infer absence from null.

Parent integration, scanner.py only:
* Attach the active shared state to RpcRouter (not the public report). In its
  token_balance, after the successful full getTokenAccountsByOwner response and
  total calculation, call remember_enumeration(state, owner, mint, result, total,
  time.time()). This records the actual pubkeys, not an assumed ATA derivation.
* Before a group of row balance reads, call prime_known_balances(rpc, owners,
  mint, state, config, time.time()). Keep the existing row token_balance calls:
  owners not safely primed have their cache evicted and require FULL enumeration
  immediately. Never substitute a zero for a failed batch or fallback.
* Keep the original attributed-inventory cap and acquisition/new-buy semantics
  outside this module. A batched increase is an observed balance, not a new buy.
* Persist token_account_enumerations in the serialized scanner state/checkpoint.
  The record is rebuildable; it is NOT account-ownership or sale evidence.

Enumeration is due at most every six hours; any individual known account drop,
closure, owner/mint/program mismatch, stale slot, or invalid response bypasses
the optimization until a new complete enumeration is remembered. Empty known
inventories cannot be validated by a batch and always use full enumeration.
Config: balance_batch_enabled (True), balance_batch_enumeration_seconds (21600,
clamped to <=21600), balance_batch_max_owners (256), balance_batch_max_accounts
(1000). Each getMultipleAccounts request has <=100 accounts and minContextSlot
at least the latest complete enumeration/validated read slot.
"""
import math


STATE_KEY = "token_account_enumerations"
MAX_ENUMERATION_SECONDS = 6 * 3600


def _raw_int(raw):
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
        raise ValueError("missing or invalid raw amount")
    return int(raw)


def _slot(result):
    context = result.get("context") if isinstance(result, dict) else None
    slot = context.get("slot") if isinstance(context, dict) else None
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
        raise ValueError("missing or invalid context slot")
    return slot


def _account(account, owner, mint):
    if not isinstance(account, dict) or account.get("executable") is True:
        raise ValueError("missing or invalid token account")
    program = account.get("owner")
    data = account.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not isinstance(program, str) or not program or not isinstance(parsed, dict):
        raise ValueError("missing token account program or parsed data")
    if parsed.get("type") != "account":
        raise ValueError("not a parsed token account")
    info = parsed.get("info")
    if not isinstance(info, dict) or info.get("mint") != mint or info.get("owner") != owner:
        raise ValueError("token account owner/mint mismatch")
    amount = info.get("tokenAmount")
    raw = amount.get("amount") if isinstance(amount, dict) else None
    decimals = amount.get("decimals") if isinstance(amount, dict) else None
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 255:
        raise ValueError("invalid token decimals")
    return _raw_int(raw), decimals, program


def _total(raw, decimals):
    total = raw / (10 ** (decimals or 0))
    if not math.isfinite(total):
        raise ValueError("non-finite token balance")
    return total


def _entries(store, mint):
    return store.setdefault(STATE_KEY, {}).setdefault(mint, {})


def remember_enumeration(store, owner, mint, result, total, now):
    """Return False on incomplete/invalid data; never replace it with empty state."""
    entries = _entries(store, mint)
    previous = entries.get(owner)
    try:
        slot = _slot(result)
        if (not isinstance(owner, str) or not owner or not isinstance(mint, str) or not mint
                or isinstance(total, bool) or not math.isfinite(total) or total < 0
                or isinstance(now, bool) or not math.isfinite(now)):
            raise ValueError("invalid enumeration identity, time or total")
        if previous and slot < previous.get("checked_slot", previous.get("slot", 0)):
            raise ValueError("enumeration older than validated inventory")
        rows = result.get("value")
        if not isinstance(rows, list):
            raise ValueError("missing complete enumeration")
        accounts, seen, raw_total, decimals = [], set(), 0, None
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid enumeration row")
            pubkey = row.get("pubkey")
            if not isinstance(pubkey, str) or not pubkey or pubkey in seen:
                raise ValueError("missing or duplicate token account pubkey")
            seen.add(pubkey)
            raw, scale, program = _account(row.get("account"), owner, mint)
            if decimals is not None and decimals != scale:
                raise ValueError("inconsistent token decimals")
            decimals = scale
            raw_total += raw
            accounts.append({"pubkey": pubkey, "program": program, "amount_raw": str(raw)})
        actual_total = _total(raw_total, decimals)
        if not math.isclose(actual_total, total, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("enumeration total mismatch")
        entries[owner] = {"schema_version": 1, "owner": owner, "mint": mint,
                          "slot": slot, "checked_slot": slot, "enumerated_at": now,
                          "checked_at": now, "decimals": decimals,
                          "total_raw": str(raw_total), "total": actual_total,
                          "accounts": accounts}
        return True
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        if isinstance(previous, dict):
            previous["needs_enumeration"] = True
        return False


def _limit(config, key, default, maximum):
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid balance batch limit")
    return min(value, maximum)


def prime_known_balances(rpc, owners, mint, store, config, now):
    """Prime only fully validated nondecreasing inventories; caller enumerates others."""
    stats = {"status": "disabled", "primed": 0, "batch_requests": 0,
             "full_enumeration_required": [], "failures": 0}
    if not config.get("balance_batch_enabled", True):
        return stats
    max_owners = _limit(config, "balance_batch_max_owners", 256, 4096)
    max_accounts = _limit(config, "balance_batch_max_accounts", 1000, 10_000)
    interval = _limit(config, "balance_batch_enumeration_seconds",
                      MAX_ENUMERATION_SECONDS, MAX_ENUMERATION_SECONDS)
    entries = _entries(store, mint)
    selected, seen = [], set()
    for owner in owners:
        if owner in seen:
            continue
        seen.add(owner)
        rpc.token_balance_cache.pop((owner, mint), None)
        if len(selected) < max_owners:
            selected.append(owner)
        else:
            stats["full_enumeration_required"].append(owner)
    eligible, flat, results, invalid = {}, [], {}, set()
    for owner in selected:
        entry = entries.get(owner)
        try:
            if (not isinstance(entry, dict) or entry.get("schema_version") != 1
                    or entry.get("owner") != owner or entry.get("mint") != mint
                    or entry.get("needs_enumeration")):
                raise ValueError("untracked inventory")
            age = now - entry["enumerated_at"]
            if not math.isfinite(age) or age < 0 or age >= interval:
                raise ValueError("enumeration due")
            accounts = entry["accounts"]
            if not isinstance(accounts, list) or not accounts or len(flat) + len(accounts) > max_accounts:
                raise ValueError("no bounded known-account inventory")
            keys = [row["pubkey"] for row in accounts]
            if len(set(keys)) != len(keys) or any(not isinstance(key, str) or not key for key in keys):
                raise ValueError("invalid persisted account list")
            decimals = entry["decimals"]
            if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 255:
                raise ValueError("invalid persisted token decimals")
            raw_total = sum(_raw_int(row["amount_raw"]) for row in accounts)
            if (raw_total != _raw_int(entry["total_raw"])
                    or any(not isinstance(row["program"], str) or not row["program"] for row in accounts)):
                raise ValueError("invalid persisted inventory totals")
            minimum = max(entry["slot"], entry["checked_slot"])
            if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
                raise ValueError("invalid enumeration slot")
            eligible[owner] = entry
            flat.extend((owner, row, minimum) for row in accounts)
            results[owner] = []
        except (ValueError, TypeError, KeyError, OverflowError):
            invalid.add(owner)
    for start in range(0, len(flat), 100):
        group = flat[start:start + 100]
        addresses = [row["pubkey"] for owner, row, minimum in group]
        minimum = max(slot for owner, row, slot in group)
        stats["batch_requests"] += 1
        try:
            # Do not use multiple_accounts(): its cache drops context/minContextSlot.
            response = rpc.call("getMultipleAccounts", [addresses, {
                "encoding": "jsonParsed", "minContextSlot": minimum}])
            slot = _slot(response)
            rows = response.get("value")
            if slot < minimum or not isinstance(rows, list) or len(rows) != len(group):
                raise ValueError("stale or incomplete account batch")
            for (owner, known, old_slot), account in zip(group, rows):
                try:
                    raw, decimals, program = _account(account, owner, mint)
                    entry = eligible[owner]
                    if (decimals != entry["decimals"] or program != known["program"]
                            or raw < _raw_int(known["amount_raw"])):
                        raise ValueError("known account dropped or changed")
                    results[owner].append((known, raw, slot))
                except (ValueError, TypeError, KeyError, OverflowError):
                    invalid.add(owner)
        except Exception:
            # A failed batch is unknown, not a zero balance. Diagnostics may
            # include endpoint credentials; never expose the exception text.
            stats["failures"] += 1
            invalid.update(owner for owner, row, slot in group)
    for owner, entry in eligible.items():
        if owner in invalid or len(results[owner]) != len(entry["accounts"]):
            invalid.add(owner)
            continue
        try:
            raw_total = sum(raw for known, raw, slot in results[owner])
            if raw_total < _raw_int(entry["total_raw"]):
                raise ValueError("inventory decreased")
            total = _total(raw_total, entry["decimals"])
            rpc.token_balance_cache[(owner, mint)] = total
            for known, raw, slot in results[owner]:
                known["amount_raw"] = str(raw)
            entry.update(total_raw=str(raw_total), total=total,
                         checked_slot=max(slot for known, raw, slot in results[owner]), checked_at=now)
            stats["primed"] += 1
        except (ValueError, TypeError, KeyError, OverflowError):
            rpc.token_balance_cache.pop((owner, mint), None)
            invalid.add(owner)
    for owner in selected:
        if owner in invalid:
            entry = entries.get(owner)
            if isinstance(entry, dict):
                entry["needs_enumeration"] = True
            stats["full_enumeration_required"].append(owner)
    stats["status"] = "partial" if stats["full_enumeration_required"] else "ok"
    return stats
