"""Read-only GMGN context. Provider labels never replace attributed RPC evidence."""
import json
import math
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone

VERSION = "1.6.1"
EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"


def number(value, positive=False):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and (result > 0 if positive else result >= 0) else None
    except (ValueError, TypeError, OverflowError):
        return None


def same_token(a, b, chain):
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return False
    return a == b if chain == "sol" else bool(EVM.fullmatch(a) and EVM.fullmatch(b) and a.lower() == b.lower())


def fraction(value):
    value = number(value)
    return value if value is not None and value <= 1 else None


def token_ath(data, token, chain):
    if not isinstance(data, dict) or not same_token(data.get("address"), token, chain):
        raise ValueError("GMGN token identity mismatch")
    dev = data.get("dev") if isinstance(data.get("dev"), dict) else {}
    best = dev.get("ath_token_info") if isinstance(dev.get("ath_token_info"), dict) else {}
    price = number(data.get("ath_price"), True)
    # The creator's best token is NOT necessarily the token being queried.
    cap = number(best.get("ath_mc"), True) if same_token(best.get("ath_token"), token, chain) else None
    return {"highest_price": price, "highest_market_cap": cap,
            "token_address": token, "identity_version": 2,
            "mcap_basis": "matching_token_reported" if cap else None,
            "status": "reported" if cap else "price_only" if price else "unavailable"}


class Unavailable(RuntimeError):
    pass


class Client:
    def __init__(self, max_calls=24, seconds=45):
        self.calls = 0
        self.max_calls = max_calls
        self.deadline = time.monotonic() + seconds
        self.stopped = None
        self.enabled = bool(os.environ.get("GMGN_API_KEY"))
        self.prefix = ["gmgn-cli"] if shutil.which("gmgn-cli") else ["npx", "-y", f"gmgn-cli@{VERSION}"]

    def query(self, kind, token):
        if kind not in ("info", "security", "holders") or not EVM.fullmatch(token):
            raise ValueError("Invalid read-only GMGN request")
        remaining = self.deadline - time.monotonic()
        if not self.enabled or self.stopped or self.calls >= self.max_calls or remaining < 1:
            raise Unavailable(self.stopped or ("missing_api_key" if not self.enabled else "budget_exhausted"))
        args = [*self.prefix, "token", kind, "--chain", "robinhood", "--address", token, "--raw"]
        if kind == "holders":
            args += ["--limit", "30"]
        env = dict(os.environ, GMGN_RATE_LIMIT_AUTO_RETRY_MAX_WAIT_MS="0")
        env.pop("GMGN_DEBUG", None)
        env.pop("GMGN_PRIVATE_KEY", None)
        self.calls += 1
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=min(8, remaining), env=env, check=False)
        except (subprocess.TimeoutExpired, OSError):
            raise Unavailable("request_timeout_or_unavailable") from None
        if result.returncode:
            message = (result.stderr or "") + (result.stdout or "")
            if any(x in message for x in ("429", "RATE_LIMIT")):
                self.stopped = "rate_limited"
            elif any(x in message for x in ("401", "403", "Unauthorized", "Forbidden")):
                self.stopped = "auth_unavailable"
            # Never export CLI diagnostics: they can contain credentials or provider URLs.
            raise Unavailable(self.stopped or "request_failed")
        try:
            data = json.loads(result.stdout)
            if not isinstance(data, dict):
                raise ValueError()
            if "code" in data:
                if data["code"] != 0 or not isinstance(data.get("data"), dict):
                    raise ValueError()
                data = data["data"]
            return data
        except (ValueError, TypeError):
            raise Unavailable("invalid_response") from None


def normalize_info(data, token):
    ath = token_ath(data, token, "robinhood")
    price = data.get("price") if isinstance(data.get("price"), dict) else {}
    stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}
    tags = data.get("wallet_tags_stat") if isinstance(data.get("wallet_tags_stat"), dict) else {}
    pool = data.get("pool") if isinstance(data.get("pool"), dict) else {}
    return {"token": token, "chain_id": 4663, "ath": ath,
            "price_usd": number(price.get("price"), True), "liquidity_usd": number(pool.get("liquidity")),
            "pool_address": pool.get("pool_address"), "holder_count": number(data.get("holder_count")),
            "activity": {window: {field: number(price.get(f"{field}_{window}"))
                         for field in ("buys", "sells", "buy_volume", "sell_volume", "volume")}
                         for window in ("1m", "5m", "1h", "6h", "24h")},
            "concentration": {key: fraction(stat.get(key)) for key in
                              ("top_10_holder_rate", "dev_team_hold_rate", "fresh_wallet_rate", "top70_sniper_hold_rate")},
            "wallet_tags": {key: number(tags.get(key)) for key in
                            ("smart_wallets", "renowned_wallets", "fresh_wallets", "sniper_wallets", "bundler_wallets")}}


def normalize_security(data, token):
    if not same_token(data.get("address"), token, "robinhood"):
        raise ValueError("GMGN security identity mismatch")
    flags = {}
    for key in ("is_honeypot", "is_open_source", "is_renounced", "is_blacklist"):
        value = data.get(key)
        flags[key] = value if isinstance(value, bool) else None
    return {"flags": flags, "buy_tax": number(data.get("buy_tax")), "sell_tax": number(data.get("sell_tax"))}


def normalize_holders(data, token, excluded):
    rows = data.get("list")
    if not isinstance(rows, list):
        raise ValueError("Invalid holders response")
    output, seen, removed = [], set(), 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid holder")
        wallet = row.get("address", "")
        if not isinstance(wallet, str) or not EVM.fullmatch(wallet):
            raise ValueError("Invalid holder address")
        wallet = wallet.lower()
        # Missing address type is not evidence that an address is an ordinary wallet.
        if wallet in excluded or row.get("exchange") or row.get("addr_type") != 0:
            removed += 1
            continue
        if wallet in seen:
            continue
        seen.add(wallet)
        bought = number(row.get("buy_amount_cur"))
        output.append({"address": wallet, "balance": number(row.get("balance")),
                       "supply_fraction": fraction(row.get("amount_percentage")),
                       "bought": bought, "sold": number(row.get("sell_amount_cur")),
                       "transferred_in": number(row.get("current_transfer_in_amount")),
                       "transferred_out": number(row.get("current_transfer_out_amount")),
                       "buy_volume_usd": number(row.get("buy_volume_cur")),
                       "sell_volume_usd": number(row.get("sell_volume_cur")),
                       "entry_usd": number(row.get("avg_cost"), True),
                       "has_reported_buys": bought is not None and bought > 0,
                       "tags": [x for x in row.get("tags", []) if isinstance(x, str)][:12]})
    return {"token": token, "wallets": output, "sample_size": len(rows), "excluded": removed,
            "coverage": "top_30_sample_not_signal_cohort"}


def enrich(rows, store, client=None):
    client = client or Client()
    stats = {"status": "ok" if client.enabled else "missing_api_key", "requests": 0, "enriched": 0, "errors": 0}
    excluded = {POOL_MANAGER, "0x" + "0" * 40, "0x000000000000000000000000000000000000dead"}
    excluded.update(str(r.get("pool", "")).lower() for r in rows)
    excluded.update(str(r.get("token", "")).lower() for r in rows)

    def obtain(kind, row, ttl):
        key = f"gmgn:v2:{kind}:{row['token']}"
        cached = store.get(key, ttl)
        if cached:
            return cached
        old = store.get(key)
        try:
            raw = client.query(kind, row["token"])
            data = normalize_info(raw, row["token"]) if kind == "info" else normalize_security(raw, row["token"]) if kind == "security" else normalize_holders(raw, row["token"], excluded)
            result = {"status": "ok", "checked_at": datetime.now(timezone.utc).isoformat(), **data}
            store.put(key, result)
            return result
        except (Unavailable, ValueError, TypeError, KeyError):
            stats["errors"] += 1
            return dict(old or {}, status="stale" if old else "unavailable")

    # Cheap market context first; deeper provider samples only for four strong candidates.
    for row in rows[:16]:
        context = {"source": "GMGN", "info": obtain("info", row, 3300)}
        row["gmgn"] = context
        if context["info"]["status"] == "ok":
            stats["enriched"] += 1
    candidates = [r for r in rows[:16] if r.get("cohort_created_at") or
                  (r.get("attributed_buy_transactions", 0) >= 3 and len(r.get("wallets", [])) >= 3)]
    candidates.sort(key=lambda r: (not bool(r.get("cohort_created_at")), -r.get("attributed_buy_transactions", 0)))
    for row in candidates[:4]:
        row["gmgn"]["security"] = obtain("security", row, 21600)
        row["gmgn"]["holders"] = obtain("holders", row, 3300)
    stats["requests"] = client.calls
    if stats["errors"] and stats["status"] == "ok":
        stats["status"] = client.stopped or "partial"
    return stats
