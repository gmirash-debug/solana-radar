"""Bounded, read-only Robinhood mainnet observations; never Solana trade signals."""
import argparse
import copy
import json
import math
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests
from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak

CHAIN_ID = 4663
FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
PUBLIC_RPC = "https://rpc.mainnet.chain.robinhood.com"
GECKO = "https://api.geckoterminal.com/api/v2/networks/robinhood"
SWAP = "0x" + keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
CONFIG = {"discovery_pages": 3, "max_pools": 6, "rpc_budget": 300,
          "max_receipts_per_pool": 24, "window_seconds": 3600,
          "min_pool_age_hours": 24, "max_pool_age_hours": 360,
          "min_liquidity_usd": 3000, "max_fdv_usd": 5_000_000}


def address(value):
    if not isinstance(value, str) or not ADDRESS.fullmatch(value):
        raise ValueError("Invalid EVM address")
    return value.lower()


def token_key(value):
    return f"{CHAIN_ID}:{address(value)}"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class RpcError(RuntimeError):
    pass


class Rpc:
    def __init__(self, url=None, budget=300, session=None):
        self.url = url or os.environ.get("ROBINHOOD_RPC_URL") or PUBLIC_RPC
        self.budget = budget
        self.calls = 0
        self.session = session or requests.Session()
        self.provider = "configured RPC" if self.url != PUBLIC_RPC else "rate-limited public RPC"
        self.next_request_at = 0
        self.deadline = time.monotonic() + 330

    def call(self, method, params):
        for attempt in range(3):
            if self.calls >= self.budget:
                raise RpcError("RPC budget exhausted")
            if time.monotonic() >= self.deadline:
                raise RpcError("RPC time budget exhausted")
            time.sleep(max(0, self.next_request_at - time.monotonic()))
            self.calls += 1
            self.next_request_at = time.monotonic() + (0.8 if self.url == PUBLIC_RPC else 0.2)
            try:
                result = self.session.post(self.url, json={"jsonrpc": "2.0", "id": self.calls,
                    "method": method, "params": params}, timeout=15)
                if result.status_code in (429, 502, 503, 504) and attempt < 2:
                    self.next_request_at = time.monotonic() + 2 ** (attempt + 1)
                    continue
                result.raise_for_status()
                payload = result.json()
                break
            except (requests.RequestException, ValueError):
                # Never persist provider URLs, which may contain credentials.
                raise RpcError(f"RPC unavailable during {method}") from None
        if payload.get("error") or payload.get("result") is None:
            raise RpcError(f"RPC rejected {method}")
        return payload["result"]

    def contract(self, target, signature, returns, args=(), types=(), block="latest"):
        data = "0x" + (keccak(text=signature)[:4] + encode(types, args)).hex()
        raw = self.call("eth_call", [{"to": address(target), "data": data}, block])
        try:
            return decode(returns, bytes.fromhex(raw[2:]))
        except Exception:
            raise RpcError("Contract response could not be decoded") from None


def discover(session, config, now):
    pools, errors = {}, []
    for page in range(1, config["discovery_pages"] + 1):
        try:
            response = session.get(f"{GECKO}/dexes/uniswap-v3-robinhood/pools",
                params={"page": page, "include": "base_token,quote_token"}, timeout=15)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload.get("data"), list):
                raise ValueError("Invalid discovery response")
            metadata = {t["id"]: t.get("attributes", {}) for t in payload.get("included", [])}
            for item in payload["data"]:
                a, rel = item["attributes"], item["relationships"]
                if rel["dex"]["data"]["id"] != "uniswap-v3-robinhood":
                    continue
                base_id = rel["base_token"]["data"]["id"]
                quote_id = rel["quote_token"]["data"]["id"]
                pool = address(a["address"])
                base = address(base_id.removeprefix("robinhood_"))
                quote = address(quote_id.removeprefix("robinhood_"))
                age = (now - timestamp(a["pool_created_at"])) / 3600
                fdv = float(a.get("fdv_usd") or 0)
                liquidity = float(a.get("reserve_in_usd") or 0)
                eligible = (all(math.isfinite(v) for v in (age, fdv, liquidity))
                    and config["min_pool_age_hours"] <= age <= config["max_pool_age_hours"]
                    and liquidity >= config["min_liquidity_usd"] and 0 < fdv <= config["max_fdv_usd"])
                pools[pool] = {"pool": pool, "token": base, "key": token_key(base), "quote": quote,
                    "symbol": metadata.get(base_id, {}).get("symbol", a["name"].split(" / ")[0]),
                    "name": metadata.get(base_id, {}).get("name", a["name"]),
                    "quote_symbol": metadata.get(quote_id, {}).get("symbol", "quote"),
                    "price_usd": float(a.get("base_token_price_usd") or 0),
                    "liquidity_usd": liquidity, "fdv_usd": fdv,
                    "market_cap_usd": float(a["market_cap_usd"]) if a.get("market_cap_usd") else None,
                    "pool_created_at": a["pool_created_at"], "token_age_verified": False,
                    "volume_h1_usd": float(a.get("volume_usd", {}).get("h1") or 0),
                    "eligible": eligible}
        except (requests.RequestException, KeyError, TypeError, ValueError):
            errors.append(f"Discovery page {page} unavailable or invalid")
        time.sleep(0.25)
    return list(pools.values()), errors


def window_start(rpc, head, seconds):
    end = rpc.call("eth_getBlockByNumber", [hex(head), False])
    target = int(end["timestamp"], 16) - seconds
    lo, hi = 0, head
    # Timestamp search avoids assuming Ethereum block times on an Arbitrum chain.
    while lo < hi:
        mid = (lo + hi) // 2
        block = rpc.call("eth_getBlockByNumber", [hex(mid), False])
        if int(block["timestamp"], 16) < target:
            lo = mid + 1
        else:
            hi = mid
    return lo, end


def verify_pool(rpc, pool, block):
    actual = address(rpc.contract(pool["pool"], "factory()", ["address"], block=block)[0])
    if actual != FACTORY:
        raise RpcError("Pool factory is not the verified Uniswap v3 deployment")
    tokens = [address(rpc.contract(pool["pool"], f"token{i}()", ["address"], block=block)[0]) for i in (0, 1)]
    fee = rpc.contract(pool["pool"], "fee()", ["uint24"], block=block)[0]
    registered = rpc.contract(FACTORY, "getPool(address,address,uint24)", ["address"],
        tokens + [fee], ["address", "address", "uint24"], block)[0]
    if address(registered) != pool["pool"] or set(tokens) != {pool["token"], pool["quote"]}:
        raise RpcError("Pool identity mismatch")
    return tokens.index(pool["token"])


def swap_amounts(log):
    try:
        return decode(["int256", "int256", "uint160", "uint128", "int24"], bytes.fromhex(log["data"][2:]))[:2]
    except (DecodingError, ValueError):
        raise RpcError("Invalid swap data") from None


def transfer_net(receipt, token, wallet):
    net = 0
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if log["address"].lower() != token or len(topics) != 3 or topics[0].lower() != TRANSFER:
            continue
        try:
            amount = decode(["uint256"], bytes.fromhex(log["data"][2:]))[0]
        except (DecodingError, ValueError):
            raise RpcError("Invalid transfer data") from None
        if address("0x" + topics[1][-40:]) == wallet:
            net -= amount
        if address("0x" + topics[2][-40:]) == wallet:
            net += amount
    return net


def attributed_buy(receipt, pool, base_index):
    """Conservative direct-beneficiary buys only; transfers alone are not trades."""
    if int(receipt.get("status", "0x0"), 16) != 1:
        return None
    wallet = address(receipt["from"])
    swaps = [log for log in receipt.get("logs", []) if log["address"].lower() == pool["pool"]
             and log.get("topics", [None])[0] == SWAP]
    if len(swaps) != 1:
        return None
    swap = swaps[0]
    if len(swap["topics"]) != 3 or address("0x" + swap["topics"][2][-40:]) != wallet:
        return None
    amounts = swap_amounts(swap)
    bought, paid = -amounts[base_index], amounts[1 - base_index]
    net = transfer_net(receipt, pool["token"], wallet)
    if bought <= 0 or paid <= 0 or net != bought:
        return None
    return wallet, bought


def inspect_pool(rpc, pool, start, head, config):
    block = hex(head)
    result = dict(pool, status="observed", checked_at=utc_now(), wallets=[],
        history_complete=False, attribution_complete=False, security_checked=False,
        retained_supply_upper_bound_pct=None)
    base_index = verify_pool(rpc, pool, block)
    decimals = rpc.contract(pool["token"], "decimals()", ["uint8"], block=block)[0]
    supply = rpc.contract(pool["token"], "totalSupply()", ["uint256"], block=block)[0]
    if not 0 <= decimals <= 36 or supply <= 0:
        raise RpcError("Invalid token supply or decimals")
    logs = {}
    for left in range(start, head + 1, 10000):
        chunk = rpc.call("eth_getLogs", [{"address": pool["pool"], "fromBlock": hex(left),
            "toBlock": hex(min(head, left + 9999)), "topics": [SWAP]}])
        if not isinstance(chunk, list) or len(chunk) >= 1000:
            raise RpcError("Log response may be capped; history is incomplete")
        for log in chunk:
            if log.get("removed"):
                continue
            if (log["address"].lower() != pool["pool"] or log.get("topics", [None])[0] != SWAP
                    or not left <= int(log["blockNumber"], 16) <= min(head, left + 9999)):
                raise RpcError("Log does not match requested pool/window")
            logs[(log["transactionHash"], log["logIndex"])] = log
    result["history_complete"] = True
    txs = list(dict.fromkeys(log["transactionHash"] for log in logs.values()))
    result["swap_transactions"] = len(txs)
    result["buy_swaps"] = sum(swap_amounts(log)[base_index] < 0 for log in logs.values())
    result["sell_swaps"] = sum(swap_amounts(log)[base_index] > 0 for log in logs.values())
    buy_txs = list(dict.fromkeys(log["transactionHash"] for log in logs.values()
        if swap_amounts(log)[base_index] < 0))
    result["buy_transactions"] = len(buy_txs)
    cohort = Counter()
    checked, attributed = 0, 0
    for tx in buy_txs[-config["max_receipts_per_pool"]:]:
        receipt = rpc.call("eth_getTransactionReceipt", [tx])
        if not start <= int(receipt["blockNumber"], 16) <= head:
            raise RpcError("Receipt outside requested block window")
        checked += 1
        buy = attributed_buy(receipt, pool, base_index)
        if buy:
            wallet, amount = buy
            cohort[wallet] += amount
            attributed += 1
    result.update(receipts_checked=checked, attributed_buy_transactions=attributed,
        attribution_complete=(checked == len(buy_txs) and attributed == result["buy_swaps"]))
    held = 0
    for wallet, bought in cohort.items():
        balance = rpc.contract(pool["token"], "balanceOf(address)", ["uint256"],
            [wallet], ["address"], block)[0]
        retained = min(balance, bought)
        held += retained
        result["wallets"].append({"address": wallet, "bought_raw": str(bought),
            "balance_raw": str(balance), "retained_upper_bound_raw": str(retained),
            "retention_upper_bound_pct": 100 * retained / bought,
            "supply_upper_bound_pct": 100 * retained / supply})
    result.update(decimals=decimals, total_supply_raw=str(supply),
        retained_supply_upper_bound_pct=100 * held / supply if cohort else None,
        balance_block=head, window_from_block=start, window_to_block=head)
    # This is a measurable buy wave, not proof of collusion, insider identity or safety.
    if len(cohort) >= 3 and result["attribution_complete"] and result["buy_swaps"] >= 2 * max(1, result["sell_swaps"]):
        result["status"] = "buy_wave"
    return result


def scan(previous=None, config=None, rpc=None, session=None):
    config = config or CONFIG
    rpc = rpc or Rpc(budget=config["rpc_budget"])
    session = session or requests.Session()
    previous = previous if (previous or {}).get("chain_id") == CHAIN_ID else {}
    output = copy.deepcopy(previous)
    output.setdefault("tokens", [])
    output.update(chain_id=CHAIN_ID, chain="robinhood", attempted_at=utc_now(), config=config,
        mode="research", scope="Uniswap v3 only; bounded GeckoTerminal discovery; not network-wide",
        provider=rpc.provider)
    try:
        if int(rpc.call("eth_chainId", []), 16) != CHAIN_ID:
            raise RpcError("Wrong chain: refusing to scan")
        head = int(rpc.call("eth_blockNumber", []), 16) - 64
        start, block = window_start(rpc, head, config["window_seconds"])
        if not -60 <= time.time() - int(block["timestamp"], 16) <= 900:
            raise RpcError("RPC head is stale or ahead of wall clock")
        pools, errors = discover(session, config, time.time())
        observed_at = utc_now()
        if not pools and errors:
            raise RpcError("All discovery pages failed")
        old = {p["key"]: p for p in previous.get("tokens", [])}
        eligible = [p for p in pools if p["eligible"]]
        # Oldest checked first; a popular pool cannot monopolize the request budget.
        eligible.sort(key=lambda p: (old.get(p["key"], {}).get("checked_at", ""), -p["volume_h1_usd"]))
        selected, seen = [], set()
        for pool in eligible:
            if pool["key"] not in seen:
                selected.append(pool)
                seen.add(pool["key"])
        results = []
        # Reserve a request to validate the snapshot block after all balance reads.
        rpc.budget -= 1
        for pool in selected[:config["max_pools"]]:
            try:
                row = inspect_pool(rpc, pool, start, head, config)
            except (RpcError, ValueError, KeyError) as exc:
                row = dict(pool, status="check_failed", error=str(exc), checked_at=utc_now(), wallets=[])
                errors.append(f"{pool['pool']}: {exc}")
            row["first_observed_at"] = old.get(pool["key"], {}).get("first_observed_at") or observed_at
            results.append(row)
        # Keep pending eligible tokens visible without presenting previous balances as fresh.
        for pool in selected[config["max_pools"]:]:
            results.append(dict(pool, status="queued", wallets=[],
                first_observed_at=old.get(pool["key"], {}).get("first_observed_at") or observed_at))
        rpc.budget += 1
        rpc.deadline += 20
        if rpc.call("eth_getBlockByNumber", [hex(head), False])["hash"] != block["hash"]:
            raise RpcError("Block changed during scan; observations discarded")
        output.update(generated_at=utc_now(), status="partial" if errors else "ok", tokens=results,
            discovered_pools=len(pools), eligible_tokens=len(selected), checked_pools=min(len(selected), config["max_pools"]),
            errors=errors, window_from_block=start, window_to_block=head,
            block_hash=block["hash"], block_timestamp=int(block["timestamp"], 16))
    except (RpcError, ValueError, KeyError) as exc:
        output.update(status="unavailable", errors=[str(exc)])
    output["rpc_calls"] = rpc.calls
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/robinhood.json")
    args = parser.parse_args()
    path = Path(args.output)
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = {}
    result = scan(previous)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, separators=(",", ":")))
    temporary.replace(path)
    print(json.dumps({key: result.get(key) for key in ("status", "rpc_calls", "discovered_pools", "eligible_tokens", "checked_pools")}))


if __name__ == "__main__":
    main()
