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
from robinhood_store import Store
from gmgn_context import enrich as enrich_gmgn
from relay_context import RelayClient, SOLVER, request_output, buy_wave

CHAIN_ID = 4663
FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
PUBLIC_RPC = "https://rpc.mainnet.chain.robinhood.com"
PUBLIC_NODE = "https://robinhood-rpc.publicnode.com"
ORDO_RPC = "https://rpc.ordofi.network"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
ZERO = "0x" + "0" * 40
WRAPPED = {ZERO, "0x0bd7d308f8e1639fab988df18a8011f41eacad73", "0x5fc5360d0400a0fd4f2af552add042d716f1d168"}
GECKO = "https://api.geckoterminal.com/api/v2/networks/robinhood"
SWAP = "0x" + keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
SWAP_V4 = "0x" + keccak(text="Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)").hex()
INITIALIZE = "0x" + keccak(text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex()
TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
CONFIG = {"discovery_pages": 3, "new_pool_pages": 2, "max_pools": 16, "rpc_budget": 900,
          "max_receipts_per_pool": 160, "window_seconds": 3600,
          "min_pool_age_hours": 0, "max_pool_age_hours": 360,
          "ordinary_wave_min_age_hours": 24,
          "min_liquidity_usd": 3000, "max_fdv_usd": 5_000_000,
          "max_log_blocks_per_pool": 720_000, "min_cohort_supply_pct": 0.25,
          "min_cohort_wallets": 3, "retention_check_seconds": 1800,
          "relay_max_calls": 96, "relay_windows": [(300, 5, 1.0), (900, 8, 2.0), (3600, 15, 5.0)],
          "relay_min_wallet_supply_pct": 0.01, "relay_max_buyer_share": 0.4,
          "relay_min_retained_supply_pct": 0.5, "relay_min_retained_fraction": 0.6}


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
        configured = url or os.environ.get("ROBINHOOD_RPC_URL")
        archive = os.environ.get("ROBINHOOD_ARCHIVE_RPC_URL")
        self.routes = list(dict.fromkeys(x for x in (configured, archive, PUBLIC_NODE, PUBLIC_RPC, ORDO_RPC) if x))
        self.url = self.routes[0]
        self.budget = budget
        self.calls = 0
        self.session = session or requests.Session()
        self.provider = "Alchemy/configured + PublicNode + Robinhood + Ordo" if configured else "PublicNode + Robinhood + Ordo"
        self.route_names = {u: ("PublicNode" if u == PUBLIC_NODE else "Robinhood public" if u == PUBLIC_RPC else "Ordo" if u == ORDO_RPC else f"configured-{i + 1}") for i, u in enumerate(self.routes)}
        self.verified = set()
        self.disabled = set()
        self.failures = Counter()
        self.last_errors = {}
        self.method_route = {}
        self.stats = Counter()
        self.head = None
        self.next_by_endpoint = {}
        self.next_request_at = 0
        self.deadline = time.monotonic() + 510

    def call(self, method, params):
        if self.calls >= self.budget:
            raise RpcError("RPC budget exhausted")
        block_tag = params[-1] if params and isinstance(params[-1], str) else "latest"
        historical = method in ("eth_getCode", "eth_call", "eth_getStorageAt") and block_tag.startswith("0x") and self.head is not None and int(block_tag, 16) < self.head - 256
        route_key = (method, historical)
        preferred = self.method_route.get(route_key)
        if preferred is None:
            preferred = self.routes[0] if historical else PUBLIC_RPC if method == "eth_getLogs" else PUBLIC_NODE
        routes = sorted(self.routes, key=lambda u: u != preferred)
        if method == "eth_getLogs":
            # PublicNode gates historical logs; Alchemy Free caps ranges at ten blocks.
            span = int(params[0].get("toBlock", "0x0"), 16) - int(params[0].get("fromBlock", "0x0"), 16) + 1
            routes = [u for u in routes if u != PUBLIC_NODE and not (".g.alchemy.com/" in u and span > 10)]
        for endpoint in routes:
            if endpoint in self.disabled:
                continue
            if endpoint not in self.verified and method != "eth_chainId":
                try:
                    if int(self.request(endpoint, "eth_chainId", []), 16) != CHAIN_ID:
                        self.disabled.add(endpoint)
                        continue
                    self.verified.add(endpoint)
                except (RpcError, ValueError):
                    continue
            try:
                value = self.request(endpoint, method, params)
                if method == "eth_chainId" and int(value, 16) != CHAIN_ID:
                    self.disabled.add(endpoint)
                    continue
                self.verified.add(endpoint)
                self.method_route[route_key] = endpoint
                if method == "eth_blockNumber":
                    self.head = int(value, 16)
                return value
            except RpcError as exc:
                self.failures[self.route_names[endpoint]] += 1
                self.last_errors[endpoint] = str(exc)
        reasons = "; ".join(f"{self.route_names[u]}: {self.last_errors[u]}" for u in routes if u in self.last_errors)
        raise RpcError(f"No healthy RPC supports {method} for this block/range" + (f" ({reasons})" if reasons else ""))

    def backoff(self, endpoint, attempt, headers):
        try:
            retry_after = min(60, max(0, float(headers.get("Retry-After", 0))))
        except (TypeError, ValueError):
            retry_after = 0
        delay = max(retry_after, (5, 15)[min(attempt, 1)])
        self.next_by_endpoint[endpoint] = time.monotonic() + delay

    def request(self, endpoint, method, params):
        for attempt in range(3):
            if self.calls >= self.budget:
                raise RpcError("RPC budget exhausted")
            if time.monotonic() >= self.deadline:
                raise RpcError("RPC time budget exhausted")
            delay = max(0, self.next_by_endpoint.get(endpoint, 0) - time.monotonic())
            if time.monotonic() + delay >= self.deadline:
                raise RpcError("RPC time budget exhausted")
            time.sleep(delay)
            self.calls += 1
            self.next_by_endpoint[endpoint] = time.monotonic() + (0.9 if endpoint in (PUBLIC_RPC, ORDO_RPC) else 0.15)
            self.stats[self.route_names[endpoint]] += 1
            try:
                result = self.session.post(endpoint, json={"jsonrpc": "2.0", "id": self.calls,
                    "method": method, "params": params}, timeout=15)
                if result.status_code in (429, 502, 503, 504) and attempt < 2:
                    self.backoff(endpoint, attempt, result.headers)
                    continue
                if result.status_code >= 400:
                    raise RpcError(f"HTTP {result.status_code} during {method}")
                result.raise_for_status()
                payload = result.json()
                error = payload.get("error") if isinstance(payload, dict) else None
                message = str(error.get("message", "")).lower() if isinstance(error, dict) else ""
                busy = isinstance(error, dict) and (error.get("code") == 429 or any(x in message for x in ("too many requests", "network is busy", "rate limit")))
                if busy and attempt < 2:
                    self.backoff(endpoint, attempt, result.headers)
                    continue
                if isinstance(error, dict):
                    code = error.get("code")
                    safe_code = str(code) if isinstance(code, int) else "unknown"
                    raise RpcError(f"RPC {safe_code}{' throttled' if busy else ''} during {method}")
                break
            except (requests.RequestException, ValueError):
                # Never persist provider URLs, which may contain credentials.
                raise RpcError(f"RPC unavailable during {method}") from None
        if not isinstance(payload, dict) or payload.get("error") or payload.get("result") is None:
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
    routes = [("new_pools", page) for page in range(1, config["new_pool_pages"] + 1)]
    routes += [(f"dexes/uniswap-{version}-robinhood/pools", page) for version in ("v3", "v4") for page in range(1, config["discovery_pages"] + 1)]
    for route, page in routes:
        try:
            response = session.get(f"{GECKO}/{route}",
                params={"page": page, "include": "base_token,quote_token"}, timeout=15)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload.get("data"), list):
                raise ValueError("Invalid discovery response")
            metadata = {t["id"]: t.get("attributes", {}) for t in payload.get("included", [])}
            for item in payload["data"]:
                a, rel = item["attributes"], item["relationships"]
                protocol = {"uniswap-v3-robinhood": "v3", "uniswap-v4-robinhood": "v4"}.get(rel["dex"]["data"]["id"])
                if protocol is None:
                    continue
                base_id = rel["base_token"]["data"]["id"]
                quote_id = rel["quote_token"]["data"]["id"]
                pool = a["address"].lower()
                if not re.fullmatch(r"0x[0-9a-f]{64}" if protocol == "v4" else r"0x[0-9a-f]{40}", pool):
                    raise ValueError("Invalid pool identifier")
                base = address(base_id.removeprefix("robinhood_"))
                quote = address(quote_id.removeprefix("robinhood_"))
                age = (now - timestamp(a["pool_created_at"])) / 3600
                fdv = float(a.get("fdv_usd") or 0)
                liquidity = float(a.get("reserve_in_usd") or 0)
                prices = [float(a.get(k) or 0) for k in ("base_token_price_usd", "market_cap_usd")]
                volume = float(a.get("volume_usd", {}).get("h1") or 0)
                if not all(math.isfinite(v) and v >= 0 for v in prices + [volume]):
                    raise ValueError("Non-finite market data")
                eligible = (all(math.isfinite(v) for v in (age, fdv, liquidity))
                    and 0 <= age <= config["max_pool_age_hours"] and base not in WRAPPED
                    and liquidity >= config["min_liquidity_usd"] and 0 < fdv <= config["max_fdv_usd"])
                pools[pool] = {"pool": pool, "protocol": protocol, "token": base, "key": token_key(base), "quote": quote,
                    "symbol": metadata.get(base_id, {}).get("symbol", a["name"].split(" / ")[0]),
                    "name": metadata.get(base_id, {}).get("name", a["name"]),
                    "quote_symbol": metadata.get(quote_id, {}).get("symbol", "quote"),
                    "price_usd": float(a.get("base_token_price_usd") or 0),
                    "liquidity_usd": liquidity, "fdv_usd": fdv,
                    "market_cap_usd": float(a["market_cap_usd"]) if a.get("market_cap_usd") else None,
                    "pool_created_at": a["pool_created_at"], "token_age_verified": False,
                    "market_checked_at": utc_now(),
                    "volume_h1_usd": float(a.get("volume_usd", {}).get("h1") or 0),
                    "eligible": eligible}
        except (requests.RequestException, KeyError, TypeError, ValueError):
            errors.append(f"Discovery {route} page {page} unavailable or invalid")
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
    if pool.get("protocol") == "v4":
        if not isinstance(pool.get("pool_created_at"), str):
            raise RpcError("V4 pool creation time is unavailable")
        created = timestamp(pool.get("pool_created_at"))
        head = int(block, 16)
        head_time = int(rpc.call("eth_getBlockByNumber", [block, False])["timestamp"], 16)
        if not created or created > head_time + 600:
            raise RpcError("V4 pool creation time is unavailable or invalid")
        # Bound the log lookup around discovery metadata, then verify the on-chain identity.
        left, _ = window_start(rpc, head, max(0, head_time - created + 600))
        right, _ = window_start(rpc, head, max(0, head_time - created - 600))
        logs = get_logs(rpc, {"address": POOL_MANAGER, "topics": [INITIALIZE, pool["pool"]]}, left, right)
        if len(logs) != 1:
            raise RpcError("V4 pool initialization could not be verified")
        log = logs[0]
        if log.get("removed") or address(log["address"]) != POOL_MANAGER or len(log["topics"]) != 4 or log["topics"][:2] != [INITIALIZE, pool["pool"]]:
            raise RpcError("Invalid V4 initialization event")
        tokens = [address("0x" + t[-40:]) for t in log["topics"][2:]]
        try:
            fee, spacing, hooks, _, _ = decode(["uint24", "int24", "address", "uint160", "int24"], bytes.fromhex(log["data"][2:]))
        except (ValueError, DecodingError):
            raise RpcError("Invalid V4 initialization data") from None
        identity = "0x" + keccak(encode(["address", "address", "uint24", "int24", "address"], tokens + [fee, spacing, hooks])).hex()
        if identity != pool["pool"] or set(tokens) != {pool["token"], pool["quote"]}:
            raise RpcError("V4 PoolKey identity mismatch")
        pool.update(hooks=address(hooks), fee=fee, pool_creation_block=int(log["blockNumber"], 16))
        return tokens.index(pool["token"])
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
        if log.get("topics", [None])[0] == SWAP_V4:
            amounts = decode(["int128", "int128", "uint160", "uint128", "int24", "uint24"], bytes.fromhex(log["data"][2:]))
            # V4 emits the caller's delta; normalize to V3's pool-side sign.
            return -amounts[0], -amounts[1]
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




def pool_log(log, pool):
    topics = log.get("topics", [])
    if pool.get("protocol") == "v4":
        return log["address"].lower() == POOL_MANAGER and len(topics) == 3 and topics[:2] == [SWAP_V4, pool["pool"]]
    return log["address"].lower() == pool["pool"] and len(topics) == 3 and topics[0] == SWAP


def routed_buy(receipt, pool, base_index):
    """Attribute the transaction sender only when pool output reaches them in full."""
    if int(receipt.get("status", "0x0"), 16) != 1:
        return None
    swaps = [x for x in receipt.get("logs", []) if pool_log(x, pool)]
    if len(swaps) != 1:
        return None
    amounts = swap_amounts(swaps[0])
    bought, paid = -amounts[base_index], amounts[1 - base_index]
    wallet = address(receipt["from"])
    custody = POOL_MANAGER if pool.get("protocol") == "v4" else pool["pool"]
    if bought <= 0 or paid <= 0 or wallet in (custody, pool["token"], ZERO):
        return None
    if transfer_net(receipt, pool["token"], wallet) != bought or transfer_net(receipt, pool["token"], custody) != -bought:
        return None
    return wallet, bought


def relay_buy(receipt, pool, base_index, relay):
    """Bind Relay's executed recipient/amount to a canonical pool buy receipt."""
    if relay is None or int(receipt.get("status", "0x0"), 16) != 1:
        return None
    # Infrastructure is only a lookup hint, never evidence of common ownership.
    solver_topic = "0x" + SOLVER[2:].rjust(64, "0")
    if receipt.get("from", "").lower() != SOLVER and not any(
            solver_topic in [t.lower() for t in log.get("topics", [])[1:]] for log in receipt.get("logs", [])):
        return None
    swaps = [x for x in receipt.get("logs", []) if pool_log(x, pool)]
    if len(swaps) != 1:
        return None
    amounts = swap_amounts(swaps[0])
    bought, paid = -amounts[base_index], amounts[1 - base_index]
    custody = POOL_MANAGER if pool.get("protocol") == "v4" else pool["pool"]
    if bought <= 0 or paid <= 0 or transfer_net(receipt, pool["token"], custody) != -bought:
        return None
    match = request_output(relay.lookup(receipt["transactionHash"]), receipt["transactionHash"], pool["token"], CHAIN_ID)
    if not match:
        return None
    wallet, amount = address(match["recipient"]), int(match["bought_raw"])
    if wallet in (custody, pool["token"], ZERO, SOLVER, receipt.get("from", "").lower()) or not 0 < amount <= bought:
        return None
    if transfer_net(receipt, pool["token"], wallet) != amount:
        return None
    # Reject mixed token sources; allow a receipt-reconciled net amount after token fees.
    senders = {address("0x" + x["topics"][1][-40:]) for x in receipt.get("logs", [])
        if x["address"].lower() == pool["token"] and len(x.get("topics", [])) == 3 and x["topics"][0] == TRANSFER}
    if any(w != custody and transfer_net(receipt, pool["token"], w) < 0 for w in senders):
        return None
    return match


def get_logs(rpc, query, start, end, chunk_size=10000):
    output = {}
    for left in range(start, end + 1, chunk_size):
        right = min(end, left + chunk_size - 1)
        chunk = rpc.call("eth_getLogs", [dict(query, fromBlock=hex(left), toBlock=hex(right))])
        if not isinstance(chunk, list):
            raise RpcError("Invalid log response")
        if len(chunk) >= 1000:
            if left == right:
                raise RpcError("Single-block log response may be truncated")
            chunk = get_logs(rpc, query, left, right, max(1, chunk_size // 2))
        for log in chunk:
            if log.get("removed") or address(log["address"]) != query["address"] or not left <= int(log["blockNumber"], 16) <= right:
                raise RpcError("Log outside requested canonical range")
            for i, expected in enumerate(query.get("topics", [])):
                if expected is not None and (i >= len(log["topics"]) or log["topics"][i] not in (expected if isinstance(expected, list) else [expected])):
                    raise RpcError("Log topic does not match filter")
            output[(log["transactionHash"], log["logIndex"])] = log
    return sorted(output.values(), key=lambda x: (int(x["blockNumber"], 16), int(x.get("transactionIndex", "0x0"), 16), int(x["logIndex"], 16)))


def stock_registry(session, store):
    cached = store.get("stocks", 86400)
    if cached:
        return set(cached)
    try:
        r = session.get("https://api.robinhood.com/rhj/assets", timeout=15)
        r.raise_for_status()
        assets = r.json()["assets"]
        stocks = {address(d["contractAddress"]) for a in assets for d in a["deployments"] if d["chainId"] == CHAIN_ID}
        if not stocks:
            raise ValueError("Empty stock registry")
        store.put("stocks", sorted(stocks))
        return stocks
    except (requests.RequestException, ValueError, KeyError, TypeError):
        raise RpcError("Official stock registry unavailable; new-token discovery paused") from None


def token_age(rpc, token, head, oldest_block, youngest_block, store):
    cached = store.get("birth:" + token)
    if cached:
        return cached
    def exists(block):
        code = rpc.call("eth_getCode", [token, hex(block)])
        if not isinstance(code, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", code):
            raise RpcError("Invalid historical bytecode")
        return code != "0x"
    if exists(oldest_block):
        return {"age_status": "too_old", "token_age_verified": True}
    if not exists(youngest_block):
        return {"age_status": "too_young", "token_age_verified": True}
    lo, hi = oldest_block + 1, youngest_block
    while lo < hi:
        mid = (lo + hi) // 2
        if exists(mid):
            hi = mid
        else:
            lo = mid + 1
    birth = rpc.call("eth_getBlockByNumber", [hex(lo), False])
    result = {"token_age_verified": True, "age_status": "eligible", "token_creation_block": lo,
        "token_created_at": datetime.fromtimestamp(int(birth["timestamp"], 16), timezone.utc).isoformat()}
    store.put("birth:" + token, result)
    return result


def security_check(session, token, store):
    cached = store.get("risk:" + token, 3600)
    if cached:
        return cached
    result = {"status": "unknown", "source": "GoPlus", "checked_at": utc_now(), "flags": [], "unknown": []}
    flags = ("is_honeypot", "cannot_buy", "cannot_sell_all", "is_blacklisted", "is_mintable", "hidden_owner", "owner_change_balance", "transfer_pausable", "slippage_modifiable", "is_proxy")
    try:
        r = session.get(f"https://api.gopluslabs.io/api/v1/token_security/{CHAIN_ID}", params={"contract_addresses": token}, timeout=15)
        r.raise_for_status()
        payload = r.json()
        data = payload.get("result", {}).get(token) if isinstance(payload, dict) and payload.get("code") == 1 and isinstance(payload.get("result"), dict) else None
        if not isinstance(data, dict) or not data:
            raise ValueError("No token risk evidence")
        for flag in flags:
            if data.get(flag) == "1":
                result["flags"].append(flag)
            elif data.get(flag) != "0":
                result["unknown"].append(flag)
        for key in ("buy_tax", "sell_tax"):
            raw = data.get(key)
            tax = float(raw) if raw not in (None, "") else None
            result[key] = tax if tax is not None and math.isfinite(tax) and tax >= 0 else None
            if result[key] is None:
                result["unknown"].append(key)
            elif tax > 0.05:
                result["flags"].append(key + "_over_5pct")
        if data.get("is_open_source") != "1":
            result["unknown"].append("source_unverified")
        result["status"] = "risk" if result["flags"] else "unknown" if result["unknown"] else "no_flags"
        store.put("risk:" + token, result)
    except (requests.RequestException, ValueError, TypeError):
        result["unknown"].append("provider_unavailable")
    return result


def recheck_cohort(rpc, pool, cohort, head, supply, now, config):
    result = copy.deepcopy(cohort)
    if head <= cohort["balance_block"]:
        return result
    wallets = {w["address"]: w for w in result["wallets"]}
    outgoing = get_logs(rpc, {"address": pool["token"], "topics": [TRANSFER, ["0x" + w[2:].rjust(64, "0") for w in wallets]]}, cohort["balance_block"] + 1, head)
    for log in outgoing:
        sender = address("0x" + log["topics"][1][-40:])
        recipient = address("0x" + log["topics"][2][-40:])
        if sender != recipient:
            w = wallets[sender]
            amount = int(log["data"], 16)
            w["retained_lower_bound_raw"] = str(max(0, int(w["retained_lower_bound_raw"]) - amount))
    for wallet, w in wallets.items():
        balance = rpc.contract(pool["token"], "balanceOf(address)", ["uint256"], [wallet], ["address"], hex(head))[0]
        upper = min(balance, int(w["bought_raw"]))
        lower = min(upper, int(w["retained_lower_bound_raw"]))
        w.update(balance_raw=str(balance), retained_lower_bound_raw=str(lower), retained_upper_bound_raw=str(upper),
            retention_upper_bound_pct=100 * upper / int(w["bought_raw"]), supply_upper_bound_pct=100 * upper / supply)
    if now - cohort["last_check_timestamp"] >= config["retention_check_seconds"]:
        result["checks"] += 1
        result["last_check_timestamp"] = now
    result.update(balance_block=head, checked_at=utc_now())
    return result


def inspect_incremental(rpc, pool, start, head, config, store, session, now, relay=None):
    identity = store.get("pool:" + pool["pool"])
    if identity:
        if identity["token"] != pool["token"] or identity["quote"] != pool["quote"]:
            raise RpcError("Cached pool token identity changed")
        index = identity["base_index"]
        pool.update({k: identity[k] for k in ("hooks", "fee", "pool_creation_block") if k in identity})
    else:
        index = verify_pool(rpc, pool, hex(head))
        store.put("pool:" + pool["pool"], dict(pool, base_index=index))
    cursor = store.get("cursor:" + pool["pool"])
    if cursor:
        if head < cursor["block"]:
            raise RpcError("RPC head behind persisted history checkpoint")
        canonical = rpc.call("eth_getBlockByNumber", [hex(cursor["block"]), False])
        if canonical["hash"] != cursor["hash"]:
            # Never continue a thesis derived from a replaced block.
            raise RpcError("History checkpoint reorganized; manual historical rebuild required")
    left = cursor["block"] + 1 if cursor else start
    end = min(head, left + config["max_log_blocks_per_pool"] - 1)
    query = {"address": POOL_MANAGER if pool.get("protocol") == "v4" else pool["pool"],
        "topics": [SWAP_V4, pool["pool"]] if pool.get("protocol") == "v4" else [SWAP]}
    logs = get_logs(rpc, query, left, end)
    store.add_logs(pool["pool"], logs)
    checkpoint = rpc.call("eth_getBlockByNumber", [hex(end), False])
    store.put("cursor:" + pool["pool"], {"block": end, "hash": checkpoint["hash"], "from_block": cursor["from_block"] if cursor else start})
    decimals = rpc.contract(pool["token"], "decimals()", ["uint8"], block=hex(head))[0]
    supply = rpc.contract(pool["token"], "totalSupply()", ["uint256"], block=hex(head))[0]
    if not 0 <= decimals <= 36 or supply <= 0:
        raise RpcError("Invalid token supply")
    window = store.logs(pool["pool"], start, head)
    buys = [x for x in window if swap_amounts(x)[index] < 0]
    txs = list(dict.fromkeys(x["transactionHash"] for x in buys))
    cohort = Counter()
    receipts, attributed, relay_events = [], 0, []
    for tx in txs[:config["max_receipts_per_pool"]]:
        if isinstance(rpc, Rpc) and (rpc.budget - rpc.calls < 70 or rpc.deadline - time.monotonic() < 45):
            break
        receipt = store.get("receipt:" + tx)
        if receipt is None:
            receipt = rpc.call("eth_getTransactionReceipt", [tx])
            store.put("receipt:" + tx, receipt)
        matching = [x for x in buys if x["transactionHash"] == tx]
        if receipt.get("transactionHash") != tx or any(receipt.get("blockHash") != x["blockHash"] or receipt["blockNumber"] != x["blockNumber"] for x in matching):
            raise RpcError("Receipt does not match canonical swap")
        receipts.append(receipt)
        buy = routed_buy(receipt, pool, index)
        routed = relay_buy(receipt, pool, index, relay) if buy is None else None
        if routed:
            block_key = "block-time:" + receipt["blockHash"]
            block_time = store.get(block_key)
            if block_time is None:
                block_data = rpc.call("eth_getBlockByNumber", [receipt["blockNumber"], False])
                if block_data["hash"] != receipt["blockHash"]:
                    raise RpcError("Relay timestamp block does not match receipt")
                block_time = int(block_data["timestamp"], 16)
                store.put(block_key, block_time)
            relay_events.append(dict(routed, timestamp=block_time, block=int(receipt["blockNumber"], 16)))
            buy = routed["recipient"], int(routed["bought_raw"])
        if buy:
            cohort[buy[0]] += buy[1]
            attributed += 1
    risk = security_check(session, pool["token"], store)
    if pool.get("hooks", ZERO) != ZERO:
        risk = dict(risk, status="risk", flags=risk["flags"] + ["v4_hook_contract"])
    row = dict(pool, status="observed", wallets=[], checked_at=utc_now(), history_complete=end == head,
        attribution_complete=len(receipts) == len(txs) == attributed, security=risk, security_checked=risk["status"] != "unknown",
        decimals=decimals, total_supply_raw=str(supply), buy_transactions=len(txs), receipts_checked=len(receipts),
        attributed_buy_transactions=attributed, buy_swaps=len(buys), sell_swaps=len(window) - len(buys),
        swap_transactions=len({x["transactionHash"] for x in window}), window_from_block=start, window_to_block=head,
        indexed_through_block=end, history_from_block=cursor["from_block"] if cursor else start,
        backlog_blocks=max(0, head - end), balance_block=head, retained_supply_upper_bound_pct=None)
    frozen = store.get("cohort:" + pool["key"])
    wave = buy_wave(relay_events, supply, config)
    if wave:
        birth_time = timestamp(pool["token_created_at"]) if pool.get("token_age_verified") and pool.get("token_created_at") else None
        wave["launch_phase"] = "unknown" if birth_time is None else "first_hour" if wave["from_timestamp"] - birth_time < 3600 else "post_launch"
    row["relay"] = {"matched_buys": len(relay_events), "buyers": len({e["recipient"] for e in relay_events}),
        "gross_bought_supply_pct": 100 * sum(int(e["bought_raw"]) for e in relay_events) / supply,
        "coverage": "complete" if row["attribution_complete"] and row["history_complete"] else "partial",
        "baseline_status": "not_established", "wave": wave}
    cohort_start = start
    if not frozen and wave:
        # Freeze only the triggering Relay buyers, not unrelated later buyers.
        cohort = Counter()
        for event in wave["events"]:
            cohort[event["recipient"]] += int(event["bought_raw"])
        cohort_start = wave["from_block"]
    if not frozen and cohort:
        outflows = get_logs(rpc, {"address": pool["token"], "topics": [TRANSFER, ["0x" + w[2:].rjust(64, "0") for w in cohort]]}, cohort_start, head)
        spent = Counter()
        for log in outflows:
            sender, recipient = [address("0x" + t[-40:]) for t in log["topics"][1:3]]
            if sender != recipient:
                spent[sender] += int(log["data"], 16)
        for wallet, bought in cohort.items():
            balance = rpc.contract(pool["token"], "balanceOf(address)", ["uint256"], [wallet], ["address"], hex(head))[0]
            upper, lower = min(balance, bought), min(balance, max(0, bought - spent[wallet]))
            row["wallets"].append({"address": wallet, "bought_raw": str(bought), "balance_raw": str(balance),
                "retained_lower_bound_raw": str(lower), "retained_upper_bound_raw": str(upper),
                "retention_upper_bound_pct": 100 * upper / bought, "supply_upper_bound_pct": 100 * upper / supply})
        lower_supply = 100 * sum(int(w["retained_lower_bound_raw"]) for w in row["wallets"]) / supply
        enough = len(cohort) >= config["min_cohort_wallets"] and lower_supply >= config["min_cohort_supply_pct"]
        retained = sum(int(w["retained_lower_bound_raw"]) for w in row["wallets"])
        relay_enough = bool(wave and lower_supply >= config["relay_min_retained_supply_pct"]
            and retained / int(wave["gross_bought_raw"]) >= config["relay_min_retained_fraction"])
        if wave:
            wave.update(retained_supply_lower_bound_pct=lower_supply,
                retained_fraction=retained / int(wave["gross_bought_raw"]), retention_status="first_check" if relay_enough else "insufficient")
        min_ordinary_age = config["ordinary_wave_min_age_hours"] * 3600
        ordinary_age_ok = min_ordinary_age == 0 or bool(pool.get("token_created_at")
            and now - timestamp(pool["token_created_at"]) >= min_ordinary_age)
        ordinary_enough = not wave and ordinary_age_ok and enough and row["attribution_complete"] and len(buys) >= 2 * max(1, row["sell_swaps"])
        if row["history_complete"] and (relay_enough or ordinary_enough):
            frozen = {"wallets": row["wallets"], "checks": 1, "created_at": utc_now(), "checked_at": utc_now(),
                "balance_block": head, "last_check_timestamp": now, "reason": "Relay buy wave" if relay_enough else "Distributed net buy wave",
                "initial_supply_raw": str(supply), "initial_retained_raw": str(sum(int(w["retained_lower_bound_raw"]) for w in row["wallets"]))}
            if relay_enough:
                frozen["relay_wave"] = copy.deepcopy(wave)
    elif frozen:
        frozen = recheck_cohort(rpc, pool, frozen, head, supply, now, config)
    if frozen:
        store.put("cohort:" + pool["key"], frozen)
        row.update(wallets=frozen["wallets"], cohort_created_at=frozen["created_at"], cohort_checks=frozen["checks"],
            cohort_reason=frozen["reason"], status="buy_wave" if frozen["checks"] < 2 else "retained")
        if frozen.get("relay_wave"):
            row["relay"]["signal_wave"] = frozen["relay_wave"]
        lower = sum(int(w["retained_lower_bound_raw"]) for w in frozen["wallets"])
        if lower < 0.6 * int(frozen["initial_retained_raw"]):
            row["status"] = "reduced"
        if str(supply) != frozen["initial_supply_raw"]:
            row["status"] = "risk"
            row["security"] = dict(risk, flags=risk["flags"] + ["supply_changed"], status="risk")
    if row["wallets"]:
        row["retained_supply_lower_bound_pct"] = 100 * sum(int(w["retained_lower_bound_raw"]) for w in row["wallets"]) / supply
        row["retained_supply_upper_bound_pct"] = 100 * sum(int(w["retained_upper_bound_raw"]) for w in row["wallets"]) / supply
    if risk["status"] == "risk":
        row["status"] = "risk"
    store.summarize(pool["pool"], row)
    # Retention evidence is independent of an investment recommendation or contract safety.
    return row




def scan(previous=None, config=None, rpc=None, session=None, store=None):
    config = config or CONFIG
    rpc = rpc or Rpc(budget=config["rpc_budget"])
    session = session or requests.Session()
    store = store or Store()
    previous = previous if (previous or {}).get("chain_id") == CHAIN_ID else {}
    output = copy.deepcopy(previous)
    output.setdefault("tokens", [])
    output.update(chain_id=CHAIN_ID, chain="robinhood", attempted_at=utc_now(), config=config,
        schema_version=2, mode="evidence", scope="Uniswap v3 + v4; bounded discovery; not network-wide",
        provider=rpc.provider)
    try:
        if int(rpc.call("eth_chainId", []), 16) != CHAIN_ID:
            raise RpcError("Wrong chain: refusing to scan")
        head = int(rpc.call("eth_blockNumber", []), 16) - 64
        start, block = window_start(rpc, head, config["window_seconds"])
        if not -60 <= time.time() - int(block["timestamp"], 16) <= 900:
            raise RpcError("RPC head is stale or ahead of wall clock")
        pools, errors = discover(session, config, time.time())
        stocks = stock_registry(session, store)
        oldest, _ = window_start(rpc, head, config["max_pool_age_hours"] * 3600)
        youngest, _ = window_start(rpc, head, config["min_pool_age_hours"] * 3600)
        observed_at = utc_now()
        if not pools and errors:
            raise RpcError("All discovery pages failed")
        old = {p["key"]: p for p in previous.get("tokens", [])}
        eligible = [p for p in pools if p["eligible"] and p["token"] not in stocks
                    and (store.get("excluded:" + p["key"]) or {}).get("age_status") != "too_old"]
        discovered_ids = {p["key"] for p in eligible}
        for key, previous_row in old.items():
            if key not in discovered_ids and store.get("cohort:" + key) and previous_row["token"] not in stocks:
                eligible.append(dict(previous_row, market_stale=True, eligible=True))
        # Oldest checked first; a popular pool cannot monopolize the request budget.
        eligible.sort(key=lambda p: (old.get(p["key"], store.get("excluded:" + p["key"]) or {}).get("last_attempt_at", ""), -p["volume_h1_usd"]))
        selected, seen = [], set()
        for pool in eligible:
            if pool["key"] not in seen:
                selected.append(pool)
                seen.add(pool["key"])
        results = []
        excluded = Counter()
        checked = 0
        # Reserve a request to validate the snapshot block after all balance reads.
        rpc.budget -= 1
        attempted = 0
        deep_attempts = 0
        relay = RelayClient(session, store, rpc.deadline, max_calls=config["relay_max_calls"])
        for pool in selected[:config["max_pools"] * 2]:
            if deep_attempts >= config["max_pools"] or rpc.budget - rpc.calls < 100 or rpc.deadline - time.monotonic() < 70:
                break
            attempted += 1
            try:
                frozen = store.get("cohort:" + pool["key"])
                age = token_age(rpc, pool["token"], head, oldest, youngest, store)
                pool.update(age)
                if age.get("token_created_at"):
                    hours = (time.time() - timestamp(age["token_created_at"])) / 3600
                    pool["age_status"] = "eligible" if config["min_pool_age_hours"] <= hours <= config["max_pool_age_hours"] else "out_of_range"
                if pool["age_status"] != "eligible" and not frozen:
                    excluded[pool["age_status"]] += 1
                    # Remember attempts so excluded assets cannot starve later candidates.
                    store.put("excluded:" + pool["key"], {"last_attempt_at": utc_now(), **age})
                    continue
                deep_attempts += 1
                row = inspect_incremental(rpc, pool, start, head, config, store, session, time.time(), relay=relay)
                checked += 1
            except (RpcError, ValueError, KeyError) as exc:
                row = dict(old.get(pool["key"], {}), **pool)
                row.update(status="check_failed", error=str(exc))
                row.setdefault("wallets", [])
                errors.append(f"{pool['pool']}: {exc}")
            row["last_attempt_at"] = utc_now()
            row["first_observed_at"] = old.get(pool["key"], {}).get("first_observed_at") or observed_at
            results.append(row)
        # Keep pending eligible tokens visible without presenting previous balances as fresh.
        for pool in selected[attempted:]:
            previous_row = old.get(pool["key"], {})
            exclusion = store.get("excluded:" + pool["key"])
            if exclusion and exclusion.get("age_status") in ("too_young", "too_old"):
                continue
            row = dict(previous_row, **pool)
            row.update(status="queued", wallets=previous_row.get("wallets", []),
                first_observed_at=previous_row.get("first_observed_at") or observed_at)
            results.append(row)
        rpc.budget += 1
        rpc.deadline += 20
        if rpc.call("eth_getBlockByNumber", [hex(head), False])["hash"] != block["hash"]:
            raise RpcError("Block changed during scan; observations discarded")
        output.update(generated_at=utc_now(), status="partial" if errors else "ok", tokens=results, relay_status=relay.summary(),
            discovered_pools=len(pools), eligible_tokens=len(results), checked_pools=checked,
            excluded_stocks=sum(p["token"] in stocks for p in pools), excluded_age=dict(excluded),
            errors=errors, window_from_block=start, window_to_block=head,
            block_hash=block["hash"], block_timestamp=int(block["timestamp"], 16))
    except (RpcError, ValueError, KeyError) as exc:
        output.update(status="unavailable", errors=[str(exc)])
        store.finish(False)
    output["rpc_calls"] = rpc.calls
    if isinstance(rpc, Rpc):
        output["provider_requests"] = dict(rpc.stats)
        output["provider_failures"] = dict(rpc.failures)
    if output["status"] != "unavailable":
        output["gmgn_status"] = enrich_gmgn(output["tokens"], store)
        store.put("snapshot", output)
        store.finish()
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/robinhood.json")
    parser.add_argument("--db", default="data/robinhood.sqlite")
    args = parser.parse_args()
    path = Path(args.output)
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = {}
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(args.db)
    cached = store.get("snapshot")
    if cached and cached.get("generated_at", "") > previous.get("generated_at", ""):
        previous = cached
    result = scan(previous, store=store)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"persisted={'true' if store.get('snapshot') else 'false'}\n")
    store.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, separators=(",", ":"), allow_nan=False))
    temporary.replace(path)
    print(json.dumps({key: result.get(key) for key in ("status", "rpc_calls", "discovered_pools", "eligible_tokens", "checked_pools")}))


if __name__ == "__main__":
    main()
