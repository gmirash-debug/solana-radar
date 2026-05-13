#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
DATA_DIR = ROOT / "data"
STATE_PATH = DATA_DIR / "state.json"
ALERTS_PATH = DATA_DIR / "alerts.jsonl"
REPORT_PATH = DATA_DIR / "latest_report.md"
REPORT_JSON_PATH = DATA_DIR / "latest_report.json"
CONFIG_PATH = ROOT / "config.json"
DEFAULT_CONFIG_PATH = ROOT / "config.example.json"

SOL_MINT = "So11111111111111111111111111111111111111112"


def utc_now():
    return datetime.now(timezone.utc)


def iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value):
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return int(timestamp)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        if text.isdigit():
            return parse_timestamp(int(text))
        try:
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0
    return 0


def load_env():
    env_paths = [ROOT / ".env", REPO_ROOT / ".env"]
    for env_path in env_paths:
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def load_json(path, fallback):
    if not path.exists():
        return fallback
    return json.loads(path.read_text())


def apply_mode(config, mode_name=None):
    config = dict(config)
    selected = mode_name or config.get("mode") or "balanced"
    modes = config.get("modes", {})
    if selected not in modes:
        raise SystemExit(f"Unknown mode '{selected}'. Available: {', '.join(sorted(modes))}")
    merged = dict(config)
    merged.update(modes[selected])
    merged["mode"] = selected
    return merged


def apply_lane(config, lane_name):
    config = dict(config)
    lanes = config.get("lanes", {})
    if lane_name not in lanes:
        raise SystemExit(f"Unknown lane '{lane_name}'. Available: {', '.join(sorted(lanes))}")
    merged = dict(config)
    merged.update(lanes[lane_name])
    merged["lane"] = lane_name
    return merged


def selected_lanes(config, lane_name=None):
    lanes = config.get("lanes", {})
    if not lanes:
        return []
    selected = lane_name or config.get("lane") or "all"
    if selected == "all":
        order = config.get("lane_order") or ["incubation", "young", "breakout", "reactivation"]
        return [lane for lane in order if lane in lanes]
    if selected not in lanes:
        raise SystemExit(f"Unknown lane '{selected}'. Available: all, {', '.join(sorted(lanes))}")
    return [selected]


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def to_float(value, default=0.0):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def chunked(items, size):
    for index in range(0, len(items), size):
        yield items[index : index + size]


@dataclass
class Pool:
    pool_address: str
    token_address: str = ""
    symbol: str = ""
    name: str = ""
    dex: str = ""
    source: str = ""
    url: str = ""
    mcap_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_1h_usd: float = 0.0
    volume_24h_usd: float = 0.0
    price_usd: float = 0.0
    txns_1h: int = 0
    pair_created_at: int = 0

    def key(self):
        return self.pool_address

    def age_hours(self):
        if not self.pair_created_at:
            return None
        return max(0.0, (utc_now().timestamp() - self.pair_created_at) / 3600)

    def as_dict(self):
        age_hours = self.age_hours()
        return {
            "pool_address": self.pool_address,
            "token_address": self.token_address,
            "symbol": self.symbol,
            "name": self.name,
            "dex": self.dex,
            "source": self.source,
            "url": self.url,
            "mcap_usd": self.mcap_usd,
            "liquidity_usd": self.liquidity_usd,
            "volume_1h_usd": self.volume_1h_usd,
            "volume_24h_usd": self.volume_24h_usd,
            "price_usd": self.price_usd,
            "txns_1h": self.txns_1h,
            "pair_created_at": self.pair_created_at,
            "pair_created_at_iso": iso(self.pair_created_at),
            "age_hours": age_hours,
        }


class Http:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "solana-radar/0.1"})

    def get_json(self, url, params=None, headers=None, timeout=25):
        response = self.session.get(url, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()

    def post_json(self, url, body, headers=None, timeout=45):
        response = self.session.post(url, json=body, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()


class HeliusRpc:
    def __init__(self, api_key, timeout_seconds=30, transactions_timeout_seconds=25):
        self.url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.calls = Counter()
        self.timeout_seconds = int(timeout_seconds)
        self.transactions_timeout_seconds = int(transactions_timeout_seconds)

    def request_timeout(self, timeout):
        seconds = float(timeout if timeout is not None else self.timeout_seconds)
        connect_timeout = min(10.0, max(3.0, seconds / 3))
        return (connect_timeout, seconds)

    def call(self, method, params=None, timeout=None):
        self.calls[method] += 1
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        response = self.session.post(self.url, json=payload, timeout=self.request_timeout(timeout))
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise RuntimeError(f"{method}: {body['error']}")
        return body.get("result")

    def health(self):
        return self.call("getHealth")

    def signatures_for_address(self, address, limit=40, before=None):
        opts = {"limit": int(limit)}
        if before:
            opts["before"] = before
        return self.call("getSignaturesForAddress", [address, opts]) or []

    def transaction(self, signature):
        return self.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )

    def transactions_for_address(
        self,
        address,
        limit=100,
        sort_order="desc",
        pagination_token=None,
        block_time=None,
        status="succeeded",
    ):
        filters = {"status": status}
        if block_time:
            filters["blockTime"] = block_time
        opts = {
            "transactionDetails": "full",
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
            "sortOrder": sort_order,
            "limit": min(int(limit), 100),
            "filters": filters,
        }
        if pagination_token:
            opts["paginationToken"] = pagination_token
        return self.call("getTransactionsForAddress", [address, opts], timeout=self.transactions_timeout_seconds) or {}


def gecko_pool_from_item(item, source):
    attrs = item.get("attributes", {})
    rel = item.get("relationships", {})
    base = rel.get("base_token", {}).get("data", {}).get("id", "")
    token_address = base.split("solana_", 1)[-1] if base.startswith("solana_") else base
    dex = rel.get("dex", {}).get("data", {}).get("id", "")
    tx_h1 = attrs.get("transactions", {}).get("h1", {}) or {}
    volume = attrs.get("volume_usd", {}) or {}
    return Pool(
        pool_address=attrs.get("address", ""),
        token_address=token_address,
        name=attrs.get("name", ""),
        symbol=(attrs.get("name", "").split("/", 1)[0].strip() if attrs.get("name") else ""),
        dex=dex,
        source=source,
        url=f"https://www.geckoterminal.com/solana/pools/{attrs.get('address', '')}",
        mcap_usd=to_float(attrs.get("market_cap_usd") or attrs.get("fdv_usd")),
        liquidity_usd=to_float(attrs.get("reserve_in_usd")),
        volume_1h_usd=to_float(volume.get("h1")),
        volume_24h_usd=to_float(volume.get("h24")),
        price_usd=to_float(attrs.get("base_token_price_usd")),
        txns_1h=int(to_float(tx_h1.get("buys")) + to_float(tx_h1.get("sells"))),
        pair_created_at=parse_timestamp(attrs.get("pool_created_at") or attrs.get("created_at")),
    )


def dexscreener_pool_from_pair(pair, source):
    tx_h1 = pair.get("txns", {}).get("h1", {}) or {}
    volume = pair.get("volume", {}) or {}
    base = pair.get("baseToken", {}) or {}
    liquidity = pair.get("liquidity", {}) or {}
    return Pool(
        pool_address=pair.get("pairAddress", ""),
        token_address=base.get("address", ""),
        name=base.get("name", ""),
        symbol=base.get("symbol", ""),
        dex=pair.get("dexId", ""),
        source=source,
        url=pair.get("url", ""),
        mcap_usd=to_float(pair.get("marketCap") or pair.get("fdv")),
        liquidity_usd=to_float(liquidity.get("usd")),
        volume_1h_usd=to_float(volume.get("h1")),
        volume_24h_usd=to_float(volume.get("h24")),
        price_usd=to_float(pair.get("priceUsd")),
        txns_1h=int(to_float(tx_h1.get("buys")) + to_float(tx_h1.get("sells"))),
        pair_created_at=parse_timestamp(pair.get("pairCreatedAt")),
    )


def solana_tracker_pool_from_item(item):
    mint = item.get("mint", "")
    pool_address = item.get("poolAddress", "")
    symbol = item.get("symbol", "")
    name = item.get("name", "")
    return Pool(
        pool_address=pool_address,
        token_address=mint,
        name=name,
        symbol=symbol,
        dex=item.get("market", ""),
        source="solana_tracker",
        url=f"https://www.solanatracker.io/tokens/{mint}" if mint else "",
        mcap_usd=to_float(item.get("marketCapUsd")),
        liquidity_usd=to_float(item.get("liquidityUsd")),
        volume_1h_usd=to_float(item.get("volume_1h")),
        volume_24h_usd=to_float(item.get("volume_24h") or item.get("volume")),
        price_usd=to_float(item.get("priceUsd")),
        txns_1h=int(to_float(item.get("buys")) + to_float(item.get("sells"))),
        pair_created_at=parse_timestamp(
            item.get("pairCreatedAt")
            or item.get("poolCreatedAt")
            or item.get("createdAt")
            or item.get("created_at")
            or item.get("created")
        ),
    )


def fetch_solana_tracker_universe(http, config):
    if not config.get("solana_tracker_enabled", True):
        return {}
    api_key = os.environ.get("SOLANA_TRACKER_API_KEY")
    if not api_key:
        return {}

    pools = {}
    headers = {"x-api-key": api_key}
    cursor = None
    pages = int(config.get("solana_tracker_pages", 1))
    delay = float(config.get("market_request_delay_seconds", 1.0))

    for page in range(1, pages + 1):
        params = {
            "limit": int(config.get("solana_tracker_limit", 500)),
            "sortBy": "volume_1h",
            "sortOrder": "desc",
            "minMarketCap": config["mcap_min_usd"],
            "maxMarketCap": config["mcap_max_usd"],
            "minLiquidity": config["liquidity_min_usd"],
            "minVolume_1h": config["volume_1h_min_usd"],
        }
        if cursor:
            params["cursor"] = cursor
        else:
            params["page"] = page
        try:
            data = http.get_json("https://data.solanatracker.io/search", params=params, headers=headers)
        except Exception as exc:
            print(f"warn: solana_tracker search failed: {exc}", file=sys.stderr)
            break
        for item in data.get("data", []) or []:
            pool = solana_tracker_pool_from_item(item)
            if pool.pool_address:
                pools[pool.key()] = pool
        cursor = data.get("nextCursor")
        if not data.get("hasMore") or not cursor:
            break
        time.sleep(delay)
    return pools


def fetch_gecko_universe(http, config):
    pools = {}
    delay = float(config.get("market_request_delay_seconds", 1.0))
    endpoints = [
        ("gecko_trending", "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"),
        ("gecko_new", "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"),
        ("gecko_pools", "https://api.geckoterminal.com/api/v2/networks/solana/pools"),
    ]
    page_count = int(config.get("gecko_pages", 2))
    for source, url in endpoints:
        pages = 1 if source == "gecko_trending" else page_count
        for page in range(1, pages + 1):
            params = {"page": page} if page > 1 or source == "gecko_pools" else None
            try:
                data = http.get_json(url, params=params)
            except Exception as exc:
                print(f"warn: {source} failed: {exc}", file=sys.stderr)
                if "429" in str(exc):
                    break
                continue
            for item in data.get("data", []):
                pool = gecko_pool_from_item(item, source)
                if pool.pool_address:
                    pools[pool.key()] = pool
            time.sleep(delay)
    return pools


def fetch_dex_token_addresses(http):
    addresses = set()
    urls = [
        "https://api.dexscreener.com/token-profiles/latest/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-boosts/top/v1",
    ]
    for url in urls:
        try:
            data = http.get_json(url)
        except Exception as exc:
            print(f"warn: {url} failed: {exc}", file=sys.stderr)
            continue
        if isinstance(data, dict):
            data = data.get("data", [])
        for item in data or []:
            if item.get("chainId") == "solana" and item.get("tokenAddress"):
                addresses.add(item["tokenAddress"])
        time.sleep(0.25)
    return addresses


def fetch_dex_pairs_for_tokens(http, token_addresses, source):
    pools = {}
    for group in chunked(sorted(set(token_addresses)), 30):
        if not group:
            continue
        try:
            data = http.get_json("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(group))
        except Exception as exc:
            print(f"warn: dexscreener token batch failed: {exc}", file=sys.stderr)
            continue
        for pair in data.get("pairs") or []:
            if pair.get("chainId") != "solana":
                continue
            pool = dexscreener_pool_from_pair(pair, source)
            if pool.pool_address:
                pools[pool.key()] = pool
        time.sleep(0.25)
    return pools


def fetch_dex_pair_for_pool(http, pool_address):
    try:
        data = http.get_json(f"https://api.dexscreener.com/latest/dex/pairs/solana/{pool_address}")
    except Exception:
        return None
    pairs = data.get("pairs") or []
    if not pairs:
        return None
    return dexscreener_pool_from_pair(pairs[0], "manual_pool")


def normalize_dex_name(value):
    return clean_social_text(value).lower().replace("_", "-")


def pool_dex_allowed(pool, config):
    allowlist = [normalize_dex_name(item) for item in config.get("dex_allowlist", []) if item]
    if not allowlist:
        return True
    return normalize_dex_name(pool.dex) in allowlist


def build_universe(http, config):
    pools = fetch_solana_tracker_universe(http, config)

    fallback_pools = fetch_gecko_universe(http, config)
    fallback_pools.update(pools)
    pools = fallback_pools

    token_addresses = fetch_dex_token_addresses(http)
    pools.update(fetch_dex_pairs_for_tokens(http, token_addresses, "dexscreener_tokens"))

    manual_tokens = config.get("manual_tokens", [])
    pools.update(fetch_dex_pairs_for_tokens(http, manual_tokens, "manual_token"))

    for pool_address in config.get("manual_pools", []):
        pool = fetch_dex_pair_for_pool(http, pool_address)
        if pool:
            pools[pool.key()] = pool

    filtered = []
    for pool in pools.values():
        if not pool.pool_address:
            continue
        if not pool_dex_allowed(pool, config):
            continue
        is_manual = pool.source in ("manual_pool", "manual_token")
        if not is_manual:
            if pool.mcap_usd <= 0:
                continue
            if not (config["mcap_min_usd"] <= pool.mcap_usd <= config["mcap_max_usd"]):
                continue
            if pool.liquidity_usd < config["liquidity_min_usd"]:
                continue
            age_hours = pool.age_hours()
            age_min = config.get("age_min_hours")
            age_max = config.get("age_max_hours")
            if age_min is not None or age_max is not None:
                if age_hours is None:
                    continue
                if age_min is not None and age_hours < float(age_min):
                    continue
                if age_max is not None and age_hours > float(age_max):
                    continue
            if pool.volume_1h_usd < config["volume_1h_min_usd"] and pool.source != "dexscreener_tokens":
                continue
            if config.get("volume_1h_max_usd") is not None and pool.volume_1h_usd > float(config["volume_1h_max_usd"]):
                continue
            if config.get("volume_1h_to_mcap_min") is not None:
                if pool.mcap_usd <= 0 or (pool.volume_1h_usd / pool.mcap_usd) < float(config["volume_1h_to_mcap_min"]):
                    continue
            if config.get("volume_1h_to_liquidity_min") is not None:
                if pool.liquidity_usd <= 0 or (pool.volume_1h_usd / pool.liquidity_usd) < float(config["volume_1h_to_liquidity_min"]):
                    continue
        filtered.append(pool)

    by_token = {}
    for pool in filtered:
        key = pool.token_address or pool.pool_address
        current = by_token.get(key)
        if not current:
            by_token[key] = pool
            continue
        if (pool.volume_1h_usd, pool.liquidity_usd) > (current.volume_1h_usd, current.liquidity_usd):
            by_token[key] = pool

    filtered = list(by_token.values())
    filtered.sort(key=lambda pool: (pool.volume_1h_usd, pool.txns_1h, pool.liquidity_usd), reverse=True)
    return filtered[: int(config["light_pool_limit"])]


def bright_data_token():
    for name in ("BRIGHTDATA_API_KEY", "BRIGHT_DATA_API_KEY", "BRIGHT_DATA_API_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    return ""


def clean_social_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def x_author_from_url(url):
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    host = parsed.hostname or ""
    if host.startswith("www."):
        host = host[4:]
    if host not in ("x.com", "twitter.com", "nitter.net"):
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return ""
    author = parts[0]
    if author.lower() in {"i", "intent", "search", "home", "share", "hashtag"}:
        return ""
    return author


def x_status_id_from_url(url):
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in ("x.com", "twitter.com", "nitter.net"):
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[1].lower() == "status" and parts[2].isdigit():
        return parts[2]
    return ""


def to_int(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    multiplier = 1
    if text.lower().endswith("k"):
        multiplier = 1_000
        text = text[:-1]
    elif text.lower().endswith("m"):
        multiplier = 1_000_000
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return None


def token_terms(pool):
    values = [pool.symbol, pool.name, pool.token_address]
    terms = []
    for value in values:
        value = clean_social_text(value)
        if not value:
            continue
        terms.append(value.lower())
        if value.startswith("$"):
            terms.append(value[1:].lower())
        elif value.isascii() and len(value) <= 12:
            terms.append(f"${value}".lower())
    return {term for term in terms if len(term) >= 2}


def social_item_matches_pool(item, pool):
    haystack = clean_social_text(
        " ".join(
            clean_social_text(value)
            for value in [
                item.get("title"),
                item.get("description"),
                item.get("content"),
                item.get("url"),
                item.get("link"),
            ]
        )
    ).lower()
    terms = token_terms(pool)
    if not terms:
        return False
    if (pool.token_address or "").lower() in haystack:
        return True
    symbol = (pool.symbol or "").lower()
    if symbol and len(symbol) > 3 and (f"${symbol}" in haystack or f" {symbol} " in f" {haystack} "):
        return True
    return any(term in haystack for term in terms if len(term) >= 4)


def build_social_queries(pool, config):
    symbol = clean_social_text(pool.symbol).lstrip("$")
    name = clean_social_text(pool.name)
    mint = clean_social_text(pool.token_address)
    queries = []
    if symbol:
        queries.extend(
            [
                f'${symbol} Solana meme token x.com',
                f'{symbol} Solana token x.com',
                f'{symbol} Solana pump x.com',
            ]
        )
    if name and name.lower() != symbol.lower():
        queries.append(f'"{name}" Solana x.com')
    if mint:
        queries.append(f'"{mint}" x.com')
    extra = config.get("social_extra_queries", [])
    for query in extra:
        queries.append(query.format(symbol=symbol, name=name, mint=mint))
    deduped = []
    for query in queries:
        if query and query not in deduped:
            deduped.append(query)
    return deduped[: int(config.get("social_queries_per_token", 3))]


def request_bright_data_discover(http, query, pool, config, token):
    primary_keyword = clean_social_text(pool.symbol or pool.name)
    filter_keywords = [primary_keyword] if len(primary_keyword) >= 3 else []
    body = {
        "query": query,
        "intent": (
            f"Find recent public X posts about Solana token {pool.symbol or pool.name} "
            f"({pool.token_address}). Return direct x.com status URLs when available."
        ),
        "filter_keywords": filter_keywords,
        "num_results": int(config.get("social_num_results", 8)),
        "country": config.get("social_country", "US"),
        "language": config.get("social_language", "en"),
        "format": "json",
        "remove_duplicates": True,
        "start_date": config.get("social_start_date"),
    }
    body = {key: value for key, value in body.items() if value not in (None, "", [])}
    url = config.get("bright_data_discover_url", "https://api.brightdata.com/discover")
    headers = {"authorization": f"Bearer {token}"}
    initial = http.post_json(url, body, headers=headers, timeout=int(config.get("social_timeout_seconds", 45)))
    if isinstance(initial.get("results"), list):
        return initial
    task_id = initial.get("task_id")
    if not task_id:
        return {"results": [], "error": "Bright Data Discover did not return results or task_id"}

    deadline = time.time() + int(config.get("social_timeout_seconds", 45))
    poll_interval = float(config.get("social_poll_interval_seconds", 3))
    while time.time() < deadline:
        time.sleep(poll_interval)
        payload = http.get_json(f"{url}?task_id={task_id}", headers=headers, timeout=20)
        if payload.get("status") == "done":
            return payload
        if payload.get("status") == "failed":
            return {"results": [], "error": payload.get("error") or "Bright Data Discover task failed"}
    return {"results": [], "error": "Bright Data Discover task timed out"}


def scrape_x_post_metrics(http, urls, config, token):
    if not config.get("social_scrape_x_posts", True):
        return {}
    status_urls = []
    for url in urls:
        if x_status_id_from_url(url) and url not in status_urls:
            status_urls.append(url)
    status_urls = status_urls[: int(config.get("social_x_scrape_max_urls", 8))]
    if not status_urls:
        return {}

    dataset_id = config.get("social_x_scrape_dataset_id", "gd_lwxkxvnf1cynvib9co")
    endpoint = config.get("social_x_scrape_url", "https://api.brightdata.com/datasets/v3/scrape")
    params = f"dataset_id={dataset_id}&format=json&include_errors=true"
    headers = {"authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"input": [{"url": url} for url in status_urls]}
    try:
        payload = http.post_json(
            f"{endpoint}?{params}",
            body,
            headers=headers,
            timeout=int(config.get("social_x_scrape_timeout_seconds", 75)),
        )
    except Exception as exc:
        return {"_error": str(exc)}
    if not isinstance(payload, list):
        return {"_error": f"unexpected_x_scrape_payload:{type(payload).__name__}"}

    by_url = {}
    for item in payload:
        url = item.get("url") or (item.get("input") or {}).get("url")
        if not url:
            continue
        by_url[url] = {
            "post_id": str(item.get("id") or x_status_id_from_url(url) or ""),
            "url": url,
            "author": clean_social_text(item.get("user_posted") or x_author_from_url(url)),
            "name": clean_social_text(item.get("name")),
            "text": clean_social_text(item.get("description")),
            "date_posted": item.get("date_posted"),
            "followers": to_int(item.get("followers")),
            "following": to_int(item.get("following")),
            "profile_posts_count": to_int(item.get("posts_count")),
            "is_verified": bool(item.get("is_verified")),
            "verification_type": item.get("verification_type"),
            "likes": to_int(item.get("likes")) or 0,
            "reposts": to_int(item.get("reposts")) or 0,
            "replies": to_int(item.get("replies")) or 0,
            "quotes": to_int(item.get("quotes")) or 0,
            "bookmarks": to_int(item.get("bookmarks")) or 0,
            "views": to_int(item.get("views")) or 0,
            "profile_image_link": item.get("profile_image_link"),
            "biography": clean_social_text(item.get("biography")),
            "scraped_at": item.get("timestamp"),
        }
    return by_url


def build_caller_graph(results, watched_accounts):
    callers = {}
    watched = {name.lower().lstrip("@") for name in watched_accounts}
    for item in results:
        author = clean_social_text(item.get("author")).lstrip("@")
        if not author:
            continue
        key = author.lower()
        caller = callers.setdefault(
            key,
            {
                "author": author,
                "name": "",
                "followers": None,
                "following": None,
                "profile_posts_count": None,
                "is_verified": False,
                "verification_type": None,
                "posts": 0,
                "views": 0,
                "likes": 0,
                "reposts": 0,
                "replies": 0,
                "quotes": 0,
                "bookmarks": 0,
                "engagements": 0,
                "engagement_rate_views_pct": None,
                "engagement_rate_followers_pct": None,
                "influence_score": 0,
                "watched": key in watched,
                "top_post": None,
                "post_urls": [],
            },
        )
        caller["posts"] += 1
        metrics = item.get("metrics") or {}
        caller["name"] = caller["name"] or metrics.get("name") or item.get("title") or ""
        for field in ("followers", "following", "profile_posts_count"):
            if metrics.get(field) is not None:
                caller[field] = max(caller[field] or 0, metrics[field])
        if metrics.get("is_verified"):
            caller["is_verified"] = True
        if metrics.get("verification_type"):
            caller["verification_type"] = metrics.get("verification_type")
        for field in ("views", "likes", "reposts", "replies", "quotes", "bookmarks"):
            caller[field] += int(metrics.get(field) or 0)
        engagements = int(metrics.get("likes") or 0) + int(metrics.get("reposts") or 0) + int(metrics.get("replies") or 0) + int(metrics.get("quotes") or 0) + int(metrics.get("bookmarks") or 0)
        caller["engagements"] += engagements
        if item.get("url") and item["url"] not in caller["post_urls"]:
            caller["post_urls"].append(item["url"])
        top = caller.get("top_post")
        post_score = int(metrics.get("views") or 0) + engagements * 25 + float(item.get("relevance_score") or 0) * 100
        if not top or post_score > top.get("score", 0):
            caller["top_post"] = {
                "url": item.get("url"),
                "text": metrics.get("text") or item.get("description") or item.get("title") or "",
                "date_posted": metrics.get("date_posted"),
                "views": metrics.get("views") or 0,
                "likes": metrics.get("likes") or 0,
                "reposts": metrics.get("reposts") or 0,
                "replies": metrics.get("replies") or 0,
                "quotes": metrics.get("quotes") or 0,
                "bookmarks": metrics.get("bookmarks") or 0,
                "score": post_score,
            }
    output = []
    for caller in callers.values():
        if caller["views"]:
            caller["engagement_rate_views_pct"] = caller["engagements"] / caller["views"] * 100
        if caller["followers"]:
            caller["engagement_rate_followers_pct"] = caller["engagements"] / caller["followers"] * 100
        caller["influence_score"] = (
            (caller["followers"] or 0) / 1_000
            + caller["views"] / 100
            + caller["engagements"] * 2
            + (25 if caller["watched"] else 0)
            + (10 if caller["is_verified"] else 0)
        )
        output.append(caller)
    output.sort(key=lambda item: item["influence_score"], reverse=True)
    return output


def normalize_link(url, label="", kind="link"):
    url = clean_social_text(url)
    if not url:
        return None
    return {"url": url, "label": clean_social_text(label) or kind, "type": kind}


def decode_json_string(value):
    if value is None:
        return ""
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return clean_social_text(value)


def dedupe_links(links):
    output = []
    seen = set()
    for link in links:
        if not link or not link.get("url"):
            continue
        key = link["url"].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        output.append(link)
    return output


def fetch_solana_tracker_token_info(http, token_address):
    api_key = os.environ.get("SOLANA_TRACKER_API_KEY")
    if not api_key or not token_address:
        return {}
    headers = {"x-api-key": api_key}
    data = http.get_json(f"https://data.solanatracker.io/tokens/{token_address}", headers=headers, timeout=30)
    token = data.get("token") or {}
    strict = token.get("strictSocials") or {}
    links = [
        normalize_link(token.get("website") or strict.get("website"), "Website", "website"),
        normalize_link(token.get("twitter") or strict.get("twitter"), "X", "twitter"),
        normalize_link(token.get("telegram") or strict.get("telegram"), "Telegram", "telegram"),
    ]
    return {
        "source": "solana_tracker",
        "name": clean_social_text(token.get("name")),
        "symbol": clean_social_text(token.get("symbol")),
        "description": clean_social_text(token.get("description")),
        "image": token.get("image"),
        "created_on": token.get("createdOn"),
        "creator": (token.get("creation") or {}).get("creator"),
        "created_tx": (token.get("creation") or {}).get("created_tx"),
        "created_time": (token.get("creation") or {}).get("created_time"),
        "links": dedupe_links(links),
    }


def fetch_dex_token_info(http, token_address):
    if not token_address:
        return {}
    try:
        data = http.get_json(f"https://api.dexscreener.com/latest/dex/tokens/{token_address}", timeout=25)
    except Exception:
        return {}
    pairs = [pair for pair in data.get("pairs") or [] if pair.get("chainId") == "solana"]
    if not pairs:
        return {}
    pairs.sort(key=lambda pair: to_float((pair.get("liquidity") or {}).get("usd")), reverse=True)
    best = pairs[0]
    info = best.get("info") or {}
    links = []
    for item in info.get("websites") or []:
        links.append(normalize_link(item.get("url"), item.get("label") or "Website", "website"))
    for item in info.get("socials") or []:
        links.append(normalize_link(item.get("url"), item.get("type") or "social", item.get("type") or "social"))
    return {
        "source": "dexscreener",
        "pair_url": best.get("url"),
        "image": info.get("imageUrl"),
        "header": info.get("header"),
        "links": dedupe_links(links),
        "pairs": [
            {
                "pair_address": pair.get("pairAddress"),
                "dex": pair.get("dexId"),
                "url": pair.get("url"),
                "liquidity_usd": to_float((pair.get("liquidity") or {}).get("usd")),
                "market_cap_usd": to_float(pair.get("marketCap") or pair.get("fdv")),
                "volume_24h_usd": to_float((pair.get("volume") or {}).get("h24")),
            }
            for pair in pairs[:5]
        ],
    }


def request_project_context(http, pool, config, token):
    if not config.get("token_intel_project_context_enabled", True) or not token:
        return []
    query = f'"{pool.name or pool.symbol}" {pool.symbol} Solana token project x.com website'
    body = {
        "query": query,
        "intent": (
            f"Find public sources that explain Solana token {pool.symbol} / {pool.name}, "
            f"including official website, X account, CoinGecko, Dexscreener, project writeups, or launchpad news."
        ),
        "filter_keywords": [value for value in [pool.symbol, pool.name] if value],
        "num_results": int(config.get("token_intel_context_results", 5)),
        "country": config.get("social_country", "US"),
        "language": config.get("social_language", "en"),
        "format": "json",
        "remove_duplicates": True,
    }
    body = {key: value for key, value in body.items() if value not in (None, "", [])}
    url = config.get("bright_data_discover_url", "https://api.brightdata.com/discover")
    headers = {"authorization": f"Bearer {token}"}
    try:
        payload = http.post_json(url, body, headers=headers, timeout=int(config.get("social_timeout_seconds", 45)))
    except Exception as exc:
        return [{"error": str(exc), "source": "bright_data"}]
    if not isinstance(payload.get("results"), list):
        task_id = payload.get("task_id")
        if task_id:
            deadline = time.time() + int(config.get("social_timeout_seconds", 45))
            poll_interval = float(config.get("social_poll_interval_seconds", 3))
            while time.time() < deadline:
                time.sleep(poll_interval)
                payload = http.get_json(f"{url}?task_id={task_id}", headers=headers, timeout=20)
                if payload.get("status") == "done":
                    break
                if payload.get("status") == "failed":
                    return [{"error": payload.get("error") or "Bright Data context task failed", "source": "bright_data"}]
    results = []
    for item in payload.get("results", []) or []:
        link = item.get("url") or item.get("link")
        if not link:
            continue
        normalized = {
            "url": link,
            "title": clean_social_text(item.get("title") or link),
            "description": clean_social_text(item.get("description") or item.get("content")),
            "relevance_score": to_float(item.get("relevance_score")),
            "source": "bright_data",
        }
        if not social_item_matches_pool(normalized, pool):
            continue
        results.append(
            normalized
        )
    return results


def official_x_links(profile, dex_info):
    links = []
    for link in [*profile.get("links", []), *dex_info.get("links", [])]:
        url = link.get("url") or ""
        link_type = (link.get("type") or link.get("label") or "").lower()
        if link_type in {"twitter", "x"} or x_author_from_url(url):
            author = x_author_from_url(url)
            if author and not x_status_id_from_url(url):
                links.append({"url": url, "author": author})
    by_author = {}
    for item in links:
        by_author.setdefault(item["author"].lower(), item)
    return list(by_author.values())


def fetch_official_x_profiles(http, profile, dex_info, config):
    if not config.get("token_intel_official_x_profile_enabled", True):
        return []
    profiles = []
    links = official_x_links(profile, dex_info)[: int(config.get("token_intel_official_x_profile_limit", 2))]
    for link in links:
        author = link["author"].lstrip("@")
        url = f"https://x.com/{author}"
        try:
            response = http.session.get(
                url,
                timeout=int(config.get("token_intel_official_x_profile_timeout_seconds", 12)),
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
        except Exception:
            continue
        marker = f'"screen_name":"{re.escape(author)}"'
        match = re.search(marker, response.text, re.I)
        if not match:
            continue
        window = response.text[max(0, match.start() - 3500) : match.end() + 1800]
        desc_match = re.search(r'"description":"((?:\\.|[^"\\])*)"', window)
        if not desc_match:
            continue
        description = clean_social_text(decode_json_string(desc_match.group(1)))
        if not description:
            continue
        followers_match = re.search(r'"followers_count":(\d+)', window)
        name_match = re.search(r'"name":"((?:\\.|[^"\\])*)"', window)
        profiles.append(
            {
                "url": url,
                "title": f"Official X @{author}",
                "description": description,
                "relevance_score": 0.95,
                "source": "official_x_profile",
                "author": author,
                "name": clean_social_text(decode_json_string(name_match.group(1))) if name_match else author,
                "followers": to_int(followers_match.group(1)) if followers_match else None,
            }
        )
    return profiles


def short_excerpt(value, max_len=260):
    text = clean_social_text(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "..."


def source_metrics_label(metrics):
    if not metrics:
        return None
    parts = []
    if metrics.get("views") is not None:
        parts.append(f"{to_int(metrics.get('views')) or 0} views")
    if metrics.get("likes") is not None:
        parts.append(f"{to_int(metrics.get('likes')) or 0} likes")
    if metrics.get("reposts") is not None:
        parts.append(f"{to_int(metrics.get('reposts')) or 0} reposts")
    if metrics.get("replies") is not None:
        parts.append(f"{to_int(metrics.get('replies')) or 0} replies")
    if metrics.get("quotes") is not None:
        parts.append(f"{to_int(metrics.get('quotes')) or 0} quotes")
    return ", ".join(parts[:5]) if parts else None


def lore_official_claims(profile, context):
    claims = []
    if profile.get("description"):
        claims.append(profile.get("description"))
    for item in context:
        if item.get("source") == "official_x_profile" and item.get("description"):
            claims.append(f"Official X: {item.get('description')}")
    output = []
    seen = set()
    for claim in claims:
        text = short_excerpt(claim, 220)
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            output.append(text)
    return output[:3]


def build_lore_analysis(pool, primary, secondary, ranked, profile, dex_info, context, social, evidence, sources):
    social_results = (social or {}).get("results", [])
    ranked_names = [name for name, _ in ranked]
    text_parts = [
        pool.symbol,
        pool.name,
        profile.get("description"),
        " ".join(f"{item.get('title')} {item.get('description')}" for item in context),
        " ".join(f"{item.get('title')} {item.get('description')}" for item in social_results),
        " ".join(f"{(item.get('metrics') or {}).get('text')}" for item in social_results),
    ]
    full_text = clean_social_text(" ".join(str(part or "") for part in text_parts))
    lower_text = full_text.lower()

    evidence_rows = []

    def add_row(kind, claim, source, url=None, metrics=None, confidence="supporting"):
        row = {
            "kind": kind,
            "claim": short_excerpt(claim),
            "source": short_excerpt(source, 90),
            "confidence": confidence,
        }
        if url:
            row["url"] = url
        metric_text = source_metrics_label(metrics)
        if metric_text:
            row["metrics"] = metric_text
        evidence_rows.append(row)

    if profile.get("description"):
        add_row(
            "official_profile",
            profile.get("description"),
            "Solana Tracker token profile",
            f"https://www.solanatracker.io/tokens/{pool.token_address}",
            confidence="strong" if primary != "Unclear" else "supporting",
        )
    for item in context[:5]:
        if item.get("description") or item.get("title"):
            kind = "official_x_profile" if item.get("source") == "official_x_profile" else "public_context"
            source = (
                f"Official X @{item.get('author')}"
                if kind == "official_x_profile" and item.get("author")
                else item.get("title") or item.get("url") or "Public context"
            )
            add_row(
                kind,
                f"{item.get('title')}: {item.get('description')}",
                source,
                item.get("url"),
                confidence="strong" if kind == "official_x_profile" or (item.get("relevance_score", 0) and item.get("relevance_score", 0) >= 0.64) else "supporting",
            )
    for item in social_results[:5]:
        metrics = item.get("metrics") or {}
        social_text = metrics.get("text") or item.get("description") or item.get("title")
        if social_text:
            add_row(
                "social_post",
                social_text,
                f"@{item.get('author')}" if item.get("author") else item.get("title") or "X post",
                item.get("url"),
                metrics,
                confidence="strong" if metrics.get("views") and metrics.get("views") >= 10_000 else "supporting",
            )

    symbol = clean_social_text(pool.symbol).lstrip("$")
    hygiene = []
    if len(symbol) <= 3:
        hygiene.append(
            f"Short ticker guard: ${symbol} alone is not enough; matches must include mint, full token name, or official project link."
        )
    if social_results:
        hygiene.append(f"Social graph filtered to {len(social_results)} token-matched X results.")

    conflicts = []
    if "Animals" in ranked_names and primary != "Animals":
        conflicts.append("Animal/mascot branding is treated as secondary packaging, not the core market lore.")
    if "Classic Meme" in ranked_names and primary != "Classic Meme":
        conflicts.append("Meme phrasing exists, but source evidence did not beat the primary narrative.")

    lore_patterns = {
        "roaring_kitty_psyop": re.search(
            r"roaring kitty|kevin14|tsuki|0\.14|buyback-burn|buyback burn|burning the supply|control the memes|psyop|mystery dev",
            lower_text,
            re.I,
        )
        is not None,
        "hantavirus": re.search(r"hanta|hantavirus|outbreak", lower_text, re.I) is not None,
        "gaming_creator": re.search(r"bloxapi|roblox|game creator|game launchpad|creator studio|studio plugin", lower_text, re.I) is not None,
        "privacy_compute": re.search(
            r"confidential computing|confidential execution|zero[- ]knowledge|\bmpc\b|privacy-first|secure enclave|secure enclaves|intel sgx|amd sev|trustzone|end-to-end encryption|encrypt, compute",
            lower_text,
            re.I,
        )
        is not None,
    }
    official_claims = lore_official_claims(profile, context)

    if primary == "Classic Meme" and lore_patterns["roaring_kitty_psyop"]:
        headline = "Roaring Kitty / Kevin14 psyop meme lore"
        driver = "social-lore catalyst"
        summary = (
            f"{pool.symbol} is not just an animal mascot meme. Official branding is puppy/dog, "
            "but stronger public and social context centers on Roaring Kitty, Kevin14, TSUKI, "
            "0.14 buyback-burn claims, and psyop speculation."
        )
        confidence = "high" if any(row["kind"] == "social_post" and row.get("metrics") for row in evidence_rows) else "medium"
    elif primary == "Health/Bio" and lore_patterns["hantavirus"]:
        headline = "Health/news-cycle meme lore"
        driver = "external news cycle"
        summary = (
            f"{pool.symbol} is a Health/Bio news-cycle meme: project and public sources frame it as "
            "Hanta-Kun, an anime mascot created around the hantavirus theme and reused for the current "
            "hantavirus narrative. Anime/mascot wording is packaging; Health/Bio is the lead thesis."
        )
        confidence = "high" if profile.get("description") and context else "medium"
    elif primary == "Gaming/Creator Infra" and lore_patterns["gaming_creator"]:
        headline = "Gaming creator-infrastructure project lore"
        driver = "project/product catalyst"
        summary = (
            f"{pool.symbol} is a Gaming/Creator Infra project thesis: project/profile sources point to "
            "game creator tools, Roblox-style creator infrastructure, analytics, or launchpad language."
        )
        confidence = "high" if context or profile.get("description") else "medium"
    elif primary == "Privacy/Compute Infra" and lore_patterns["privacy_compute"]:
        headline = "Confidential compute / privacy infra"
        driver = "project/product catalyst"
        claim_text = "; ".join(official_claims)
        summary = (
            f"{pool.symbol} is a Privacy/Compute Infra thesis: official/project sources frame {pool.name or pool.symbol} as "
            "privacy-first compute infrastructure for encrypted workloads, zero-knowledge/MPC flows, and "
            f"confidential execution. {claim_text}"
        ).strip()
        confidence = "high" if any(row["kind"] == "official_x_profile" for row in evidence_rows) else "medium"
    elif primary == "DevTool/Infra":
        headline = "Project infrastructure thesis"
        driver = "project/product catalyst"
        claim_text = "; ".join(official_claims) or "project/profile sources describe developer or infrastructure tooling."
        summary = f"{pool.symbol} is a DevTool/Infra thesis: {claim_text}"
        confidence = "high" if official_claims else "medium"
    elif primary == "Animals":
        headline = "Animal mascot meme lore"
        driver = "mascot/community meme"
        summary = (
            f"{pool.symbol} is an animal mascot/community meme: the strongest available evidence is "
            "the token name/profile mascot, with no stronger project, news, or social-lore catalyst detected."
        )
        confidence = "medium" if profile.get("description") or context else "low"
    elif primary == "Unclear":
        headline = "Lore not established"
        driver = "not classified"
        summary = "No dominant lore was established from official metadata, public context, and social evidence."
        confidence = "low"
    else:
        headline = f"{primary} lore"
        driver = "source-backed narrative"
        summary = f"{pool.symbol} is a {primary} thesis backed by scanner token-intel evidence."
        confidence = "medium" if evidence_rows else "low"

    return {
        "headline": headline,
        "summary": summary,
        "driver": driver,
        "confidence": confidence,
        "primary_evidence": evidence.get(primary, []),
        "evidence": evidence_rows[:8],
        "conflicts": conflicts[:4],
        "source_hygiene": hygiene[:4],
    }


def classify_token_narrative(pool, profile, dex_info, context, social):
    text_parts = [
        pool.symbol,
        pool.name,
        pool.token_address,
        profile.get("name"),
        profile.get("symbol"),
        profile.get("description"),
        " ".join(f"{link.get('label')} {link.get('url')}" for link in profile.get("links", [])),
        " ".join(f"{link.get('label')} {link.get('url')}" for link in dex_info.get("links", [])),
        " ".join(f"{item.get('title')} {item.get('description')} {item.get('url')}" for item in context),
        " ".join(f"{item.get('title')} {item.get('description')} {item.get('url')}" for item in (social or {}).get("results", [])),
        " ".join(f"{(item.get('metrics') or {}).get('text')}" for item in (social or {}).get("results", [])),
    ]
    text = clean_social_text(" ".join(str(part or "") for part in text_parts)).lower()
    scores = Counter()
    evidence = {}

    def add(name, points, reason):
        scores[name] += points
        evidence.setdefault(name, [])
        if reason not in evidence[name]:
            evidence[name].append(reason)

    def has(pattern):
        return re.search(pattern, text, re.I) is not None

    if has(r"hanta|hantavirus|covid|plague|vaccine|cancer|outbreak|infection|healthcare|biotech"):
        add("Health/Bio", 6, "token/profile/social text matches health or virus narrative")
    if re.search(r"hanta|hantavirus", f"{pool.symbol} {pool.name} {profile.get('description') or ''}", re.I):
        add("Health/Bio", 2, "health narrative appears in token name or official profile")
    if has(r"bloxapi|bloxxbuilder|streamerconnect|trustconnect|roblox|game creator|game launchpad|creator studio|studio plugin|gamefi|gaming|player engagement|retention|monetization"):
        add("Gaming/Creator Infra", 7, "project/profile sources point to gaming creator infrastructure")
    if has(r"confidential computing|confidential execution|zero[- ]knowledge|\bmpc\b|privacy-first|secure enclave|secure enclaves|intel sgx|amd sev|trustzone|end-to-end encryption|encrypt, compute"):
        add("Privacy/Compute Infra", 7, "official/project sources describe confidential compute or privacy infrastructure")
    if has(r"\b(api|sdk|dashboard|analytics|plugin|toolset|infrastructure|builder|build in public|hackathon)\b"):
        add("DevTool/Infra", 4, "project sources include developer or infrastructure terms")
    if has(r"\b(ai agent|ai agents|artificial intelligence|machine learning|neural|gpt|robot|bot|grok|openai|google ai|agi)\b"):
        add("AI", 4, "explicit AI or automation terms found in token/project context")
    if has(r"\b(anime|waifu|hentai|manga|neko|samurai|senpai|kawaii|japan|japanese|kantaro)\b|san chan"):
        add("Anime/Asia", 6, "profile or social context uses anime/Japan/kawaii branding")
    elif has(r"\b(kun|chan)\b"):
        add("Anime/Asia", 3, "token/profile includes anime-style honorific branding")
    if has(r"\b(dog|puppy|barking|shiba|cat|kitty|frog|pepe|bear|ape|monkey|giraffe|meow|pock|bird|fish|bull|whale|whally|underdog)\b"):
        add("Animals", 6, "profile/name uses animal mascot narrative")
    if re.search(r"whally|whale|puppy|barking|shiba", f"{pool.symbol} {pool.name} {profile.get('description') or ''}", re.I):
        add("Animals", 2, "animal mascot appears in token name or official profile")
    if has(r"trump|biden|maga|president|politic|party|tax|government|kalshi"):
        add("Politics/Prediction", 4, "political or prediction-market terms found")
    if has(r"worldcup|football|fifa|nba|ufc|suarez|cup|goal|sport"):
        add("Sports", 4, "sports terms found")
    if has(r"\b(defi|rwa|yield|liquidity protocol|staking|lending|borrow|perps|perpetual|amm|dex aggregator|tokenomics|treasury|revenue share|stablecoin)\b|ace round"):
        add("Finance/DeFi", 4, "explicit DeFi, protocol, or token-economics terms found")
    if has(r"roaring kitty|kevin14|tsuki|0\.14|buyback-burn|buyback burn|burning the supply|control the memes|psyop|mystery dev"):
        add("Classic Meme", 7, "Roaring Kitty, buyback-burn, or psyop lore found in public context")
    if has(r"troll|rage|wojak|meme|chud|incel|retard|lol|psyop"):
        add("Classic Meme", 3, "classic meme or psyop terms found")

    if not scores:
        primary = "Unclear"
        score = 1
        ranked = [("Unclear", 1)]
        evidence["Unclear"] = ["no dominant narrative after source enrichment"]
    else:
        priority = {
            "Gaming/Creator Infra": 90,
            "Privacy/Compute Infra": 85,
            "DevTool/Infra": 80,
            "Health/Bio": 75,
            "Animals": 66,
            "Anime/Asia": 65,
            "AI": 55,
            "Finance/DeFi": 50,
            "Politics/Prediction": 45,
            "Sports": 40,
            "Classic Meme": 10,
        }
        ranked = sorted(scores.items(), key=lambda item: (item[1], priority.get(item[0], 0)), reverse=True)
        primary, score = ranked[0]

    secondary = []
    for name, value in ranked[1:]:
        if value < 3:
            continue
        if primary in ("Gaming/Creator Infra", "Privacy/Compute Infra", "DevTool/Infra") and name in ("Animals", "Classic Meme") and value < 7:
            continue
        if primary != "Finance/DeFi" and name == "Finance/DeFi" and value < 5:
            continue
        if primary == "Health/Bio" and name == "Finance/DeFi" and value < 5:
            continue
        secondary.append(name)
    gap = score - (ranked[1][1] if len(ranked) > 1 else 0)
    tilt = "strong tilt" if score >= 6 and gap >= 2 else "medium tilt" if score >= 4 else "weak tilt"
    sources = []
    sources.append({"label": "Solana Tracker profile", "url": f"https://www.solanatracker.io/tokens/{pool.token_address}"})
    if pool.url:
        sources.append({"label": "Primary pair", "url": pool.url})
    for link in [*profile.get("links", []), *dex_info.get("links", [])]:
        sources.append({"label": link.get("label") or link.get("type") or "Source", "url": link.get("url")})
    for item in context[:3]:
        if item.get("url"):
            sources.append({"label": item.get("title") or "Context", "url": item["url"]})
    sources = dedupe_links(sources)

    overlay_type = "project" if primary in ("Gaming/Creator Infra", "Privacy/Compute Infra", "DevTool/Infra", "AI", "Finance/DeFi") else "news" if primary == "Health/Bio" else "narrative"
    overlay = {
        "headline": f"{overlay_type.title()} overlay: {primary}",
        "summary": (
            f"Scanner enriched this token with Solana Tracker/Dexscreener metadata, official/social links, "
            f"and public context. Primary narrative is {primary} because: {'; '.join(evidence.get(primary, []))}."
        ),
        "sources": sources[:8],
        "type": overlay_type,
    }
    lore = build_lore_analysis(pool, primary, secondary, ranked, profile, dex_info, context, social, evidence, sources)
    overlay["headline"] = lore.get("headline") or overlay["headline"]
    overlay["summary"] = lore.get("summary") or overlay["summary"]
    return {
        "primary": primary,
        "secondary": secondary,
        "tilt": tilt,
        "score": score,
        "evidence": evidence.get(primary, []),
        "ranked": [{"name": name, "score": value, "evidence": evidence.get(name, [])} for name, value in ranked],
        "overlay": overlay,
        "lore": lore,
    }


def build_token_intel(http, pool, config, state, social=None):
    if not config.get("token_intel_enabled", True):
        return None
    token_key = pool.token_address or pool.pool_address
    if not token_key:
        return None
    cache = state.setdefault("token_intel_cache", {})
    now = int(time.time())
    ttl = int(config.get("token_intel_cache_ttl_minutes", 360)) * 60
    cached = cache.get(token_key)
    if cached and now - int(cached.get("cached_at", 0)) < ttl:
        intel = dict(cached.get("intel") or {})
        intel["cache"] = "hit"
        return intel

    profile = {}
    dex_info = {}
    context = []
    failures = []
    try:
        profile = fetch_solana_tracker_token_info(http, token_key)
    except Exception as exc:
        failures.append(f"solana_tracker_profile: {exc}")
    try:
        dex_info = fetch_dex_token_info(http, token_key)
    except Exception as exc:
        failures.append(f"dexscreener_profile: {exc}")
    official_profiles = []
    try:
        official_profiles = fetch_official_x_profiles(http, profile, dex_info, config)
    except Exception as exc:
        failures.append(f"official_x_profile: {exc}")
    bd_token = bright_data_token()
    if bd_token:
        context = request_project_context(http, pool, config, bd_token)
    context = dedupe_links([*official_profiles, *context])
    narrative = classify_token_narrative(pool, profile, dex_info, context, social)
    intel = {
        "enabled": True,
        "cache": "miss",
        "created_at": utc_now().isoformat().replace("+00:00", "Z"),
        "profile": profile,
        "dex": dex_info,
        "official_profiles": official_profiles,
        "context": context,
        "narrative": narrative,
        "failures": failures[:5],
    }
    cache[token_key] = {"cached_at": now, "intel": intel}
    return intel


def fetch_social_snapshot(http, pool, config, state):
    if not config.get("social_enabled", True):
        return None
    token = bright_data_token()
    if not token:
        return {"enabled": False, "reason": "missing_bright_data_api_key"}

    cache = state.setdefault("social_cache", {})
    cache_key = pool.token_address or pool.pool_address or pool.symbol
    now = int(time.time())
    ttl = int(config.get("social_cache_ttl_minutes", 120)) * 60
    cached = cache.get(cache_key)
    if cached and now - int(cached.get("cached_at", 0)) < ttl:
        snapshot = dict(cached.get("snapshot") or {})
        snapshot["cache"] = "hit"
        return snapshot

    results = []
    failures = []
    for query in build_social_queries(pool, config):
        try:
            payload = request_bright_data_discover(http, query, pool, config, token)
        except Exception as exc:
            failures.append(f"{query}: {exc}")
            continue
        if payload.get("error"):
            failures.append(f"{query}: {payload['error']}")
        for item in payload.get("results", []) or []:
            url = item.get("url") or item.get("link") or ""
            author = x_author_from_url(url)
            if not author:
                continue
            normalized = {
                "url": url,
                "author": author,
                "title": clean_social_text(item.get("title") or url),
                "description": clean_social_text(item.get("description") or item.get("content")),
                "relevance_score": to_float(item.get("relevance_score")),
            }
            if social_item_matches_pool(normalized, pool):
                results.append(normalized)
        time.sleep(float(config.get("social_request_delay_seconds", 0.5)))

    by_url = {}
    for item in results:
        by_url.setdefault(item["url"], item)
    results = sorted(
        by_url.values(),
        key=lambda item: (item.get("relevance_score") or 0.0, item.get("url") or ""),
        reverse=True,
    )[: int(config.get("social_max_results_per_token", 12))]
    metrics_by_url = scrape_x_post_metrics(http, [item["url"] for item in results], config, token)
    if metrics_by_url.get("_error"):
        failures.append(f"x_scrape: {metrics_by_url['_error']}")
        metrics_by_url = {}
    for item in results:
        metrics = metrics_by_url.get(item["url"])
        if metrics:
            item["metrics"] = metrics

    authors = Counter(item["author"] for item in results)
    watched = {name.lower().lstrip("@") for name in config.get("social_watched_accounts", [])}
    watched_hits = sorted({item["author"] for item in results if item["author"].lower().lstrip("@") in watched})
    post_count = len(results)
    unique_authors = len(authors)
    heat = "none"
    if post_count >= int(config.get("social_hot_results", 8)) and unique_authors >= 4:
        heat = "hot"
    elif post_count >= int(config.get("social_warming_results", 3)):
        heat = "warming"
    elif post_count:
        heat = "quiet"

    score = 0
    if post_count:
        score += 10
    if unique_authors >= 2:
        score += 10
    if watched_hits:
        score += 15
    if any((pool.token_address or "").lower() in (item.get("description", "") + item.get("title", "") + item.get("url", "")).lower() for item in results):
        score += 10
    if heat == "hot":
        score -= 10
    caller_graph = build_caller_graph(results, config.get("social_watched_accounts", []))
    if any((caller.get("followers") or 0) >= int(config.get("social_influencer_followers", 10_000)) for caller in caller_graph):
        score += 10

    snapshot = {
        "enabled": True,
        "cache": "miss",
        "created_at": utc_now().isoformat().replace("+00:00", "Z"),
        "heat": heat,
        "score": max(0, score),
        "x_posts": post_count,
        "unique_authors": unique_authors,
        "top_authors": [{"author": author, "posts": count} for author, count in authors.most_common(8)],
        "caller_graph": caller_graph[: int(config.get("social_caller_report_limit", 12))],
        "caller_metrics_status": "enriched" if any(item.get("metrics") for item in results) else "discover_only",
        "watched_account_hits": watched_hits,
        "results": results[: int(config.get("social_report_results", 5))],
        "failures": failures[:3],
    }
    cache[cache_key] = {"cached_at": now, "snapshot": snapshot}
    return snapshot


def enrich_alerts_with_social(http, alerts, config, state):
    if not alerts:
        return alerts
    social_max_tokens = int(config.get("social_max_tokens_per_scan", 5))
    token_intel_max_tokens = int(config.get("token_intel_max_tokens_per_scan", 50))
    enriched = []
    seen_tokens = {}
    social_tokens = 0
    token_intel_tokens = 0

    def within_limit(count, limit):
        return limit <= 0 or count < limit

    for alert in sorted(alerts, key=lambda item: item.get("score", 0), reverse=True):
        pool = Pool(**{key: value for key, value in alert["pool"].items() if key in Pool.__dataclass_fields__})
        token_key = pool.token_address or pool.pool_address
        if token_key in seen_tokens:
            alert["social"] = seen_tokens[token_key].get("social")
            alert["token_intel"] = seen_tokens[token_key].get("token_intel")
        else:
            snapshot = None
            if within_limit(social_tokens, social_max_tokens):
                snapshot = fetch_social_snapshot(http, pool, config, state)
                social_tokens += 1
            else:
                snapshot = {"enabled": True, "heat": "unchecked", "reason": "social_scan_limit"}
            token_intel = None
            if within_limit(token_intel_tokens, token_intel_max_tokens):
                token_intel = build_token_intel(http, pool, config, state, snapshot)
                token_intel_tokens += 1
            else:
                token_intel = {
                    "enabled": False,
                    "reason": "token_intel_scan_limit",
                    "created_at": utc_now().isoformat().replace("+00:00", "Z"),
                }
            seen_tokens[token_key] = {"social": snapshot, "token_intel": token_intel}
            alert["social"] = snapshot
            alert["token_intel"] = token_intel
        enriched.append(alert)
    return enriched


def token_amount(balance):
    amount = balance.get("uiTokenAmount", {}).get("amount")
    decimals = int(balance.get("uiTokenAmount", {}).get("decimals", 0))
    if amount is None:
        return to_float(balance.get("uiTokenAmount", {}).get("uiAmount"))
    return int(amount) / (10**decimals)


def parse_pool_swap(tx, pool):
    if not tx or tx.get("meta", {}).get("err"):
        return None
    meta = tx.get("meta", {})
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    signer = next((key.get("pubkey") for key in keys if key.get("signer")), keys[0].get("pubkey") if keys else "")

    pre = {}
    post = {}
    for balance in meta.get("preTokenBalances", []) or []:
        pre[(balance.get("accountIndex"), balance.get("mint"), balance.get("owner"))] = token_amount(balance)
    for balance in meta.get("postTokenBalances", []) or []:
        post[(balance.get("accountIndex"), balance.get("mint"), balance.get("owner"))] = token_amount(balance)

    token_mint = pool.token_address
    if not token_mint:
        token_mint = next((mint for _, mint, owner in set(pre) | set(post) if owner == pool.pool_address and mint != SOL_MINT), "")

    pool_token_delta = 0.0
    pool_sol_delta = 0.0
    signer_token_delta = 0.0
    token_owner_deltas = defaultdict(float)
    for key in set(pre) | set(post):
        _, mint, owner = key
        delta = post.get(key, 0.0) - pre.get(key, 0.0)
        if owner == pool.pool_address and mint == token_mint:
            pool_token_delta += delta
        if owner == pool.pool_address and mint == SOL_MINT:
            pool_sol_delta += delta
        if owner == signer and mint == token_mint:
            signer_token_delta += delta
        if owner and owner != pool.pool_address and mint == token_mint:
            token_owner_deltas[owner] += delta

    signer_lamport_delta = 0.0
    for index, key in enumerate(keys):
        if key.get("pubkey") != signer:
            continue
        if index < len(meta.get("preBalances", [])) and index < len(meta.get("postBalances", [])):
            signer_lamport_delta += (meta["postBalances"][index] - meta["preBalances"][index]) / 1_000_000_000

    kind = None
    sol_amount = 0.0
    token_amount_value = 0.0
    if pool_token_delta < -1e-8 and pool_sol_delta > 1e-10:
        kind = "buy"
        sol_amount = pool_sol_delta
        token_amount_value = -pool_token_delta
    elif pool_token_delta > 1e-8 and pool_sol_delta < -1e-10:
        kind = "sell"
        sol_amount = -pool_sol_delta
        token_amount_value = pool_token_delta
    elif signer_token_delta > 1e-8:
        kind = "buy"
        token_amount_value = signer_token_delta
        sol_amount = max(0.0, -signer_lamport_delta)
    elif signer_token_delta < -1e-8:
        kind = "sell"
        token_amount_value = -signer_token_delta
        sol_amount = max(0.0, signer_lamport_delta)

    if not kind:
        return None

    positive_token_owners = sorted(
        ((owner, delta) for owner, delta in token_owner_deltas.items() if delta > 1e-8),
        key=lambda item: item[1],
        reverse=True,
    )
    negative_token_owners = sorted(
        ((owner, -delta) for owner, delta in token_owner_deltas.items() if delta < -1e-8),
        key=lambda item: item[1],
        reverse=True,
    )
    token_recipient = positive_token_owners[0][0] if positive_token_owners else signer
    token_recipient_amount = positive_token_owners[0][1] if positive_token_owners else max(0.0, signer_token_delta)
    token_sender = negative_token_owners[0][0] if negative_token_owners else signer
    token_sender_amount = negative_token_owners[0][1] if negative_token_owners else max(0.0, -signer_token_delta)
    routed = kind == "buy" and token_recipient != signer
    recipient_share = token_recipient_amount / token_amount_value if token_amount_value else 0.0

    return {
        "signature": tx.get("transaction", {}).get("signatures", [""])[0],
        "block_time": tx.get("blockTime"),
        "time": iso(tx.get("blockTime")),
        "pool_address": pool.pool_address,
        "token_address": token_mint,
        "symbol": pool.symbol,
        "kind": kind,
        "signer": signer,
        "token_recipient": token_recipient,
        "token_recipient_amount": token_recipient_amount,
        "token_sender": token_sender,
        "token_sender_amount": token_sender_amount,
        "recipient_share": recipient_share,
        "routed": routed,
        "sol_amount": sol_amount,
        "token_amount": token_amount_value,
        "price_native": sol_amount / token_amount_value if token_amount_value else 0.0,
    }


def wallet_cache_key(wallet, before_signature):
    return f"{wallet}:{before_signature}"


def classify_wallet(rpc, wallet, before_signature, buy_time, config, state):
    cache_key = wallet_cache_key(wallet, before_signature)
    wallet_cache = state.setdefault("wallet_cache", {})
    if cache_key in wallet_cache:
        return wallet_cache[cache_key]

    previous = rpc.signatures_for_address(wallet, limit=50, before=before_signature)
    count = len(previous)
    prev = previous[0] if previous else None
    gap = None
    if prev and prev.get("blockTime") and buy_time:
        gap = buy_time - prev["blockTime"]

    if count == 0:
        wallet_class = "fresh"
    elif count <= int(config["freshish_max_previous_txs"]):
        wallet_class = "freshish"
    elif gap is not None and gap >= int(config["dormant_gap_days"]) * 86400:
        wallet_class = "dormant"
    elif count <= int(config["low_tx_max_previous_txs"]):
        wallet_class = "low_tx"
    else:
        wallet_class = "normal"

    funding_source = None
    funding_sol = 0.0
    if wallet_class in ("fresh", "freshish") and prev:
        funding_source, funding_sol = extract_funding_source(rpc, prev.get("signature"), wallet)

    result = {
        "wallet_class": wallet_class,
        "previous_tx_count_50": count,
        "previous_gap_seconds": gap,
        "previous_signature": prev.get("signature") if prev else None,
        "previous_time": iso(prev.get("blockTime")) if prev else None,
        "funding_source": funding_source,
        "funding_sol": funding_sol,
    }
    wallet_cache[cache_key] = result
    return result


def extract_funding_source(rpc, signature, target_wallet):
    if not signature:
        return None, 0.0
    try:
        tx = rpc.transaction(signature)
    except Exception:
        return None, 0.0
    if not tx or tx.get("meta", {}).get("err"):
        return None, 0.0

    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    meta = tx.get("meta", {})
    deltas = []
    target_delta = 0.0
    for index, key in enumerate(keys):
        if index >= len(meta.get("preBalances", [])) or index >= len(meta.get("postBalances", [])):
            continue
        delta = (meta["postBalances"][index] - meta["preBalances"][index]) / 1_000_000_000
        pubkey = key.get("pubkey")
        if pubkey == target_wallet:
            target_delta += delta
        if abs(delta) >= 0.01:
            deltas.append((pubkey, delta))
    if target_delta <= 0.1:
        return None, 0.0
    senders = sorted((item for item in deltas if item[1] < -0.01), key=lambda item: item[1])
    if not senders:
        return None, target_delta
    return senders[0][0], target_delta


def score_events(events, config):
    suspicious_classes = {"fresh", "freshish", "low_tx", "dormant"}
    suspicious = [event for event in events if event.get("wallet_class") in suspicious_classes]
    suspicious_wallets = {event["signer"] for event in suspicious}
    suspicious_sol = sum(event.get("sol_amount", 0.0) for event in suspicious)
    class_counts = Counter(event.get("wallet_class") for event in events)
    funding_sources = Counter(event.get("funding_source") for event in suspicious if event.get("funding_source"))
    token_recipients = Counter(
        event.get("token_recipient") for event in suspicious if event.get("token_recipient")
    )
    common_funders = [
        {"source": source, "wallets": count}
        for source, count in funding_sources.items()
        if count >= 2
    ]
    common_recipients = [
        {"recipient": recipient, "txs": count}
        for recipient, count in token_recipients.items()
        if count >= 2
    ]

    score = 0
    if class_counts.get("dormant", 0):
        score += 35
    if len(suspicious_wallets) >= int(config["alert_min_suspicious_wallets"]):
        score += 25
    if suspicious_sol >= float(config["alert_min_suspicious_sol"]):
        score += 25
    if common_funders:
        score += 20
    if common_recipients:
        score += 15
    if any(event.get("sol_amount", 0.0) >= float(config["big_buy_sol"]) for event in suspicious):
        score += 10
    return min(score, 100), suspicious, common_funders, common_recipients


def build_alerts(pool, events, config):
    if not events:
        return []
    window_seconds = int(config["alert_window_minutes"]) * 60
    events = sorted(events, key=lambda event: event.get("block_time") or 0)
    alerts = []
    for index, event in enumerate(events):
        start = event.get("block_time") or 0
        end = start + window_seconds
        window = [item for item in events[index:] if (item.get("block_time") or 0) <= end]
        score, suspicious, common_funders, common_recipients = score_events(window, config)
        suspicious_wallet_count = len({item["signer"] for item in suspicious})
        suspicious_sol = sum(item.get("sol_amount", 0.0) for item in suspicious)
        if score < 40:
            continue
        if (
            suspicious_wallet_count < int(config["alert_min_suspicious_wallets"])
            and suspicious_sol < float(config["alert_min_suspicious_sol"])
            and not any(item.get("wallet_class") == "dormant" for item in suspicious)
        ):
            continue
        created_at = utc_now().isoformat().replace("+00:00", "Z")
        alerts.append(
            {
                "created_at": created_at,
                "score": score,
                "lane": config.get("lane") or config.get("mode"),
                "pool": pool.as_dict(),
                "obs_mcap_usd": pool.mcap_usd,
                "obs_price_usd": pool.price_usd,
                "obs_liquidity_usd": pool.liquidity_usd,
                "obs_mcap_at": created_at,
                "window_start": iso(start),
                "window_end": iso(end),
                "suspicious_wallets": suspicious_wallet_count,
                "suspicious_sol": suspicious_sol,
                "classes": dict(Counter(item.get("wallet_class") for item in suspicious)),
                "common_funders": common_funders,
                "common_recipients": common_recipients,
                "routed_buys": sum(1 for item in suspicious if item.get("routed")),
                "events": suspicious[:20],
            }
        )
    deduped = {}
    for alert in alerts:
        key = alert["pool"]["pool_address"]
        existing = deduped.get(key)
        if not existing:
            deduped[key] = alert
            continue
        if (alert["score"], alert["suspicious_sol"], alert["suspicious_wallets"]) > (
            existing["score"],
            existing["suspicious_sol"],
            existing["suspicious_wallets"],
        ):
            deduped[key] = alert
    return sorted(deduped.values(), key=lambda alert: alert["score"], reverse=True)[:5]


def helius_page_budget(pool, config, kind, phase=None):
    phase_prefix = f"helius_{phase}_" if phase else "helius_"
    pages = int(
        config.get(
            f"{phase_prefix}{kind}_pages",
            config.get(f"helius_{kind}_pages", config.get("helius_transactions_pages", 4)),
        )
    )
    txns_1h = int(pool.txns_1h or 0)
    high_threshold = int(config.get("helius_high_txn_threshold", 10_000))
    medium_threshold = int(config.get("helius_medium_txn_threshold", 1_000))
    if txns_1h >= high_threshold:
        pages = max(
            pages,
            int(
                config.get(
                    f"{phase_prefix}{kind}_high_tx_pages",
                    config.get(f"helius_{kind}_high_tx_pages", config.get("helius_high_tx_pages", pages)),
                )
            ),
        )
    elif txns_1h >= medium_threshold:
        pages = max(
            pages,
            int(
                config.get(
                    f"{phase_prefix}{kind}_medium_tx_pages",
                    config.get(f"helius_{kind}_medium_tx_pages", config.get("helius_medium_tx_pages", pages)),
                )
            ),
        )
    return max(1, pages)


def fetch_helius_pool_transactions(rpc, pool, config, pool_state, phase=None):
    now = int(time.time())
    limit = int(config.get("helius_transactions_limit", 100))
    lookback_minutes = int(config.get("helius_recent_lookback_minutes", max(60, int(config["alert_window_minutes"]))))
    recent_from = max(0, now - lookback_minutes * 60)
    previous_time = int(pool_state.get("helius_latest_block_time") or 0)
    transactions = []
    seen = set()
    stats = {
        "source": "helius_transactions",
        "phase": phase or "full",
        "pages": 0,
        "transactions": 0,
        "passes": [],
        "truncated": False,
    }

    def add_batch(batch):
        added = 0
        for tx in batch:
            signature = (tx.get("transaction") or {}).get("signatures", [""])[0]
            if not signature or signature in seen:
                continue
            seen.add(signature)
            transactions.append(tx)
            added += 1
        return added

    def run_pass(name, sort_order, max_pages, block_time=None, pagination_token=None, save_cursor_key=None):
        cursor = pagination_token
        pages = 0
        pass_stats = {
            "name": name,
            "sort_order": sort_order,
            "pages": 0,
            "transactions": 0,
            "added": 0,
            "truncated": False,
        }
        if max_pages <= 0:
            stats["passes"].append(pass_stats)
            return
        while pages < max_pages:
            result = rpc.transactions_for_address(
                pool.pool_address,
                limit=limit,
                sort_order=sort_order,
                pagination_token=cursor,
                block_time=block_time,
            )
            batch = result.get("data") or []
            pages += 1
            stats["pages"] += 1
            pass_stats["pages"] += 1
            pass_stats["transactions"] += len(batch)
            pass_stats["added"] += add_batch(batch)
            cursor = result.get("paginationToken")
            if not result.get("paginationToken") or not batch:
                if save_cursor_key:
                    pool_state.pop(save_cursor_key, None)
                    pool_state[f"{save_cursor_key}_complete"] = True
                break
            if save_cursor_key:
                pool_state[save_cursor_key] = cursor
                pool_state.pop(f"{save_cursor_key}_complete", None)
        else:
            pass_stats["truncated"] = True
            stats["truncated"] = True
            if save_cursor_key and cursor:
                pool_state[save_cursor_key] = cursor
        stats["passes"].append(pass_stats)

    if previous_time:
        incremental_from = max(0, previous_time - int(config.get("helius_incremental_overlap_seconds", 30)))
        run_pass(
            "incremental",
            "asc",
            helius_page_budget(pool, config, "incremental", phase=phase),
            block_time={"gt": incremental_from},
        )
    else:
        run_pass(
            "recent",
            "desc",
            helius_page_budget(pool, config, "recent", phase=phase),
            block_time={"gte": recent_from},
        )

    age_hours = pool.age_hours()
    initial_max_age = float(config.get("helius_initial_backfill_max_age_hours", 96))
    should_backfill = (
        pool.pair_created_at
        and age_hours is not None
        and age_hours <= initial_max_age
        and not pool_state.get("helius_initial_backfill_cursor_complete")
    )
    if should_backfill:
        launch_from = max(0, int(pool.pair_created_at) - int(config.get("helius_launch_time_cushion_seconds", 120)))
        run_pass(
            "launch_backfill",
            "asc",
            helius_page_budget(pool, config, "initial_backfill", phase=phase),
            block_time={"gte": launch_from},
            pagination_token=pool_state.get("helius_initial_backfill_cursor"),
            save_cursor_key="helius_initial_backfill_cursor",
        )

    if int(pool.txns_1h or 0) >= int(config.get("helius_high_txn_threshold", 10_000)):
        tail_pages_key = f"helius_{phase}_high_tx_tail_pages" if phase else "helius_high_tx_tail_pages"
        run_pass(
            "high_tx_tail",
            "desc",
            int(config.get(tail_pages_key, config.get("helius_high_tx_tail_pages", 4))),
            block_time={"gte": recent_from},
        )

    stats["transactions"] = len(transactions)
    return sorted(transactions, key=lambda tx: (tx.get("blockTime") or 0, tx.get("transactionIndex") or 0)), stats


def merge_transactions(*transaction_groups):
    by_signature = {}
    for group in transaction_groups:
        for tx in group or []:
            signature = (tx.get("transaction") or {}).get("signatures", [""])[0]
            if not signature:
                continue
            by_signature[signature] = tx
    return sorted(
        by_signature.values(),
        key=lambda tx: (tx.get("blockTime") or 0, tx.get("transactionIndex") or 0),
    )


def update_pool_transaction_state(pool_state, pool, txs):
    if not txs:
        return
    latest = max(txs, key=lambda tx: ((tx.get("blockTime") or 0), tx.get("transactionIndex") or 0))
    signature = (latest.get("transaction") or {}).get("signatures", [""])[0]
    block_time = int(latest.get("blockTime") or 0)
    if signature:
        pool_state["latest_signature"] = signature
        pool_state["helius_latest_signature"] = signature
    if block_time:
        pool_state["latest_time"] = iso(block_time)
        pool_state["helius_latest_time"] = iso(block_time)
        pool_state["helius_latest_block_time"] = block_time
    pool_state["symbol"] = pool.symbol


def select_buy_swaps_for_classification(candidates, config, budget_limit):
    if budget_limit <= 0:
        return []
    by_signature = {}
    by_wallet = set()
    ordered = []
    dedupe_wallets = bool(config.get("helius_dedupe_classification_wallets", True))

    def add(swaps):
        for swap in swaps:
            signature = swap.get("signature")
            if not signature or signature in by_signature:
                continue
            signer = swap.get("signer")
            if dedupe_wallets and signer:
                if signer in by_wallet:
                    continue
                by_wallet.add(signer)
            by_signature[signature] = swap
            ordered.append(swap)
            if len(ordered) >= budget_limit:
                return

    global_limit = min(
        budget_limit,
        int(config.get("helius_classify_global_buy_limit", max(20, budget_limit // 3))),
    )
    top_global = sorted(
        candidates,
        key=lambda swap: (swap.get("sol_amount", 0.0), swap.get("block_time") or 0),
        reverse=True,
    )[:global_limit]
    add(top_global)

    window_seconds = int(config.get("helius_classify_window_minutes", config["alert_window_minutes"])) * 60
    per_window = int(config.get("helius_classify_top_buys_per_window", 12))
    buckets = defaultdict(list)
    for swap in candidates:
        block_time = int(swap.get("block_time") or 0)
        bucket = block_time // window_seconds if window_seconds else 0
        buckets[bucket].append(swap)
    for bucket in sorted(buckets):
        if len(ordered) >= budget_limit:
            break
        add(
            sorted(
                buckets[bucket],
                key=lambda swap: (swap.get("sol_amount", 0.0), swap.get("block_time") or 0),
                reverse=True,
            )[:per_window]
        )

    return ordered[:budget_limit]


def classify_buy_swaps(rpc, swaps, config, state, classification_budget):
    min_sol = float(config["classify_buy_min_sol"])
    candidates = [swap for swap in swaps if swap.get("kind") == "buy" and swap.get("sol_amount", 0.0) >= min_sol]
    per_pool_limit = int(config.get("max_wallet_classifications_per_pool", classification_budget["remaining"]))
    budget_limit = min(classification_budget["remaining"], per_pool_limit)
    selected = select_buy_swaps_for_classification(candidates, config, budget_limit)
    events = []
    for swap in selected:
        cache_key = wallet_cache_key(swap["signer"], swap["signature"])
        cache_hit = cache_key in state.setdefault("wallet_cache", {})
        if not cache_hit and classification_budget["remaining"] <= 0:
            break
        if not cache_hit:
            classification_budget["remaining"] -= 1
        wallet_info = classify_wallet(rpc, swap["signer"], swap["signature"], swap["block_time"], config, state)
        swap.update(wallet_info)
        events.append(swap)
    return events, len(candidates)


def parse_helius_swaps(txs, pool):
    swaps = []
    parse_errors = 0
    for tx in txs:
        try:
            swap = parse_pool_swap(tx, pool)
        except Exception:
            parse_errors += 1
            continue
        if swap:
            swaps.append(swap)
    return swaps, parse_errors


def merge_events(*event_groups):
    by_signature = {}
    for group in event_groups:
        for event in group or []:
            signature = event.get("signature")
            if not signature:
                continue
            by_signature[signature] = event
    return sorted(by_signature.values(), key=lambda event: event.get("block_time") or 0)


def probe_classification_config(config):
    probe_config = dict(config)
    probe_limit = int(config.get("helius_probe_wallet_limit", config.get("max_wallet_classifications_per_pool", 60)))
    probe_config["max_wallet_classifications_per_pool"] = min(
        probe_limit,
        int(config.get("max_wallet_classifications_per_pool", probe_limit)),
    )
    if config.get("helius_probe_classify_global_buy_limit") is not None:
        probe_config["helius_classify_global_buy_limit"] = int(config["helius_probe_classify_global_buy_limit"])
    else:
        probe_config["helius_classify_global_buy_limit"] = min(
            int(config.get("helius_classify_global_buy_limit", probe_limit)),
            probe_limit,
        )
    if config.get("helius_probe_classify_top_buys_per_window") is not None:
        probe_config["helius_classify_top_buys_per_window"] = int(config["helius_probe_classify_top_buys_per_window"])
    return probe_config


def should_deep_scan(pool, config, pool_state, events, candidate_buys, alerts, classification_budget):
    if not config.get("helius_deep_scan_enabled", True):
        return False, "deep_disabled"
    if not config.get("helius_probe_enabled", True):
        return True, "probe_disabled"
    if alerts:
        return True, "probe_alert"

    score, suspicious, common_funders, common_recipients = score_events(events, config)
    high_conviction_wallets = {
        event["signer"]
        for event in suspicious
        if event.get("signer") and event.get("wallet_class") in ("fresh", "freshish", "dormant")
    }
    suspicious_sol = sum(event.get("sol_amount", 0.0) for event in suspicious)
    if common_funders or common_recipients:
        return True, "linked_wallets"
    if any(event.get("wallet_class") == "dormant" for event in suspicious):
        return True, "dormant_wallet"
    if len(high_conviction_wallets) >= int(config.get("helius_deep_min_suspicious_wallets", 2)):
        return True, "suspicious_wallet_probe"
    if suspicious_sol >= float(config.get("helius_deep_min_suspicious_sol", 8)):
        return True, "suspicious_flow_probe"

    min_candidates = int(config.get("helius_deep_min_candidate_buys", 20))
    probe_buy_sol = sum(event.get("sol_amount", 0.0) for event in events)
    if (
        candidate_buys >= min_candidates
        and probe_buy_sol >= float(config.get("helius_deep_min_probe_buy_sol", 15))
        and score > 0
    ):
        return True, "flow_probe"

    audit_interval = float(config.get("helius_deep_audit_interval_hours", 0))
    audits_remaining = int(classification_budget.get("deep_audits_remaining", 0))
    if audit_interval > 0 and audits_remaining > 0 and candidate_buys >= min_candidates:
        last_deep = parse_timestamp(pool_state.get("helius_deep_scanned_at"))
        if not last_deep or time.time() - last_deep >= audit_interval * 3600:
            classification_budget["deep_audits_remaining"] = audits_remaining - 1
            return True, "scheduled_deep_audit"

    return False, "probe_clean"


def combine_fetch_stats(probe_stats, deep_stats):
    combined = dict(deep_stats or probe_stats or {})
    if not probe_stats or not deep_stats:
        return combined
    combined["phase"] = "probe_plus_deep"
    combined["pages"] = int(probe_stats.get("pages", 0)) + int(deep_stats.get("pages", 0))
    combined["transactions"] = int(probe_stats.get("transactions", 0)) + int(deep_stats.get("transactions", 0))
    combined["passes"] = [*(probe_stats.get("passes") or []), *(deep_stats.get("passes") or [])]
    combined["truncated"] = bool(probe_stats.get("truncated") or deep_stats.get("truncated"))
    combined["probe"] = probe_stats
    combined["deep"] = deep_stats
    return combined


def scan_pool_helius_transactions(rpc, pool, config, state, classification_budget):
    pool_state = state.setdefault("pools", {}).setdefault(pool.pool_address, {})
    preclassified = False
    swaps = []
    parse_errors = 0
    events = []
    seed_events = []
    candidate_buys = 0
    try:
        if config.get("helius_probe_enabled", True):
            probe_txs, probe_fetch_stats = fetch_helius_pool_transactions(rpc, pool, config, pool_state, phase="probe")
            update_pool_transaction_state(pool_state, pool, probe_txs)
            probe_swaps, probe_parse_errors = parse_helius_swaps(probe_txs, pool)
            probe_config = probe_classification_config(config)
            probe_events, probe_candidate_buys = classify_buy_swaps(
                rpc,
                probe_swaps,
                probe_config,
                state,
                classification_budget,
            )
            probe_alerts = build_alerts(pool, probe_events, config)
            deepen, deep_reason = should_deep_scan(
                pool,
                config,
                pool_state,
                probe_events,
                probe_candidate_buys,
                probe_alerts,
                classification_budget,
            )
            if deepen:
                deep_txs, deep_fetch_stats = fetch_helius_pool_transactions(rpc, pool, config, pool_state, phase="deep")
                txs = merge_transactions(probe_txs, deep_txs)
                fetch_stats = combine_fetch_stats(probe_fetch_stats, deep_fetch_stats)
                fetch_stats["deep_reason"] = deep_reason
                pool_state["helius_deep_scanned_at"] = utc_now().isoformat().replace("+00:00", "Z")
                seed_events = probe_events
            else:
                txs = probe_txs
                fetch_stats = dict(probe_fetch_stats)
                fetch_stats["deep_skipped"] = True
                fetch_stats["deep_reason"] = deep_reason
                swaps = probe_swaps
                parse_errors = probe_parse_errors
                events = probe_events
                candidate_buys = probe_candidate_buys
                preclassified = True
        else:
            txs, fetch_stats = fetch_helius_pool_transactions(rpc, pool, config, pool_state, phase="deep")
    except Exception as exc:
        if not config.get("helius_transactions_fallback_signatures", True):
            return [], {"pool": pool.as_dict(), "error": str(exc), "trade_source": "helius_transactions"}
        return scan_pool_signatures(rpc, pool, config, state, classification_budget, fallback_error=str(exc))

    update_pool_transaction_state(pool_state, pool, txs)
    if not preclassified:
        swaps, parse_errors = parse_helius_swaps(txs, pool)
        classified_events, candidate_buys = classify_buy_swaps(rpc, swaps, config, state, classification_budget)
        events = merge_events(seed_events, classified_events)
    alerts = build_alerts(pool, events, config)
    return alerts, {
        "pool": pool.as_dict(),
        "lane": config.get("lane") or config.get("mode"),
        "trade_source": "helius_transactions",
        "new_signatures": len(txs),
        "transactions_scanned": len(txs),
        "parsed_swaps": len(swaps),
        "candidate_buys": candidate_buys,
        "classified_buys": len(events),
        "classes": dict(Counter(event.get("wallet_class") for event in events)),
        "buy_sol": sum(event.get("sol_amount", 0.0) for event in events),
        "parse_errors": parse_errors,
        "trade_fetch": fetch_stats,
    }


def scan_pool_signatures(rpc, pool, config, state, classification_budget, fallback_error=None):
    pool_state = state.setdefault("pools", {}).setdefault(pool.pool_address, {})
    previous_latest = pool_state.get("latest_signature")
    limit = int(config["max_new_signatures_per_pool"]) if previous_latest else int(config["initial_backfill_signatures"])

    try:
        signatures = rpc.signatures_for_address(pool.pool_address, limit=limit)
    except Exception as exc:
        return [], {"pool": pool.as_dict(), "error": str(exc), "trade_source": "pool_signatures"}

    if signatures:
        pool_state["latest_signature"] = signatures[0]["signature"]
        pool_state["latest_time"] = iso(signatures[0].get("blockTime"))
        pool_state["symbol"] = pool.symbol

    new_signatures = []
    for item in signatures:
        if previous_latest and item["signature"] == previous_latest:
            break
        if item.get("err"):
            continue
        new_signatures.append(item["signature"])

    events = []
    for signature in reversed(new_signatures):
        try:
            tx = rpc.transaction(signature)
        except Exception:
            continue
        swap = parse_pool_swap(tx, pool)
        if not swap or swap["kind"] != "buy":
            continue
        if swap["sol_amount"] < float(config["classify_buy_min_sol"]):
            continue
        if classification_budget["remaining"] <= 0:
            continue
        classification_budget["remaining"] -= 1
        wallet_info = classify_wallet(rpc, swap["signer"], swap["signature"], swap["block_time"], config, state)
        swap.update(wallet_info)
        events.append(swap)

    alerts = build_alerts(pool, events, config)
    summary = {
        "pool": pool.as_dict(),
        "lane": config.get("lane") or config.get("mode"),
        "trade_source": "pool_signatures",
        "new_signatures": len(new_signatures),
        "classified_buys": len(events),
        "classes": dict(Counter(event.get("wallet_class") for event in events)),
        "buy_sol": sum(event.get("sol_amount", 0.0) for event in events),
    }
    if fallback_error:
        summary["fallback_error"] = fallback_error
    return alerts, summary


def scan_pool(rpc, pool, config, state, classification_budget):
    if config.get("helius_transactions_enabled", True):
        return scan_pool_helius_transactions(rpc, pool, config, state, classification_budget)
    return scan_pool_signatures(rpc, pool, config, state, classification_budget)


def write_alerts(alerts):
    if not alerts:
        return
    ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ALERTS_PATH.open("a") as handle:
        for alert in alerts:
            handle.write(json.dumps(alert, separators=(",", ":")) + "\n")


def recent_alert_token_addresses(limit=250):
    if not ALERTS_PATH.exists():
        return []
    tokens = []
    for line in ALERTS_PATH.read_text().splitlines()[-limit:]:
        if not line.strip():
            continue
        try:
            alert = json.loads(line)
        except json.JSONDecodeError:
            continue
        token = (alert.get("pool") or {}).get("token_address")
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def fetch_solana_tracker_ath(http, token_address):
    api_key = os.environ.get("SOLANA_TRACKER_API_KEY")
    if not api_key:
        return None
    headers = {"x-api-key": api_key}
    return http.get_json(f"https://data.solanatracker.io/tokens/{token_address}/ath", headers=headers)


def apply_solana_tracker_ath(entry, ath, observed_at):
    if ath.get("highest_market_cap") is not None:
        entry["ath_mcap_usd"] = to_float(ath.get("highest_market_cap"))
    if ath.get("highest_price") is not None:
        entry["ath_price_usd"] = to_float(ath.get("highest_price"))
    if ath.get("timestamp"):
        timestamp = int(ath["timestamp"])
        if timestamp > 10_000_000_000:
            timestamp = timestamp // 1000
        entry["ath_mcap_at"] = iso(timestamp)
    if ath.get("pool_id"):
        entry["ath_pool_address"] = ath.get("pool_id")
    entry["ath_source"] = "solana_tracker"
    entry["ath_status"] = "ready"
    entry["ath_latest_checked_at"] = observed_at
    entry.pop("ath_error", None)
    entry.pop("ath_error_checked_at", None)


def trusted_ath_mcap(entry):
    if not entry:
        return 0.0
    if entry.get("ath_source") not in ("solana_tracker", "ohlcv_high"):
        return 0.0
    return to_float(entry.get("ath_mcap_usd"))


def filter_reactivation_by_ath(http, state, pools, config, observed_at):
    max_ratio = config.get("ath_max_current_ratio")
    if config.get("lane") != "reactivation" or max_ratio is None:
        return pools

    max_ratio = float(max_ratio)
    market = state.setdefault("market", {})
    now = int(time.time())
    error_ttl = int(config.get("ath_error_cache_ttl_minutes", 20)) * 60
    delay = float(config.get("ath_request_delay_seconds", 0.25))
    fetch_limit = int(config.get("ath_filter_max_tokens_per_scan", config.get("ath_max_tokens_per_scan", 25)))
    require_trusted = bool(config.get("ath_require_trusted", True))
    api_key = os.environ.get("SOLANA_TRACKER_API_KEY")
    fetched = 0
    rate_limited = False
    kept = []
    stats = Counter()

    for pool in pools:
        token = pool.token_address or pool.pool_address
        if not token or pool.mcap_usd <= 0:
            stats["missing_token_or_mcap"] += 1
            continue
        entry = market.setdefault(token, {"token_address": token})
        ath_mcap = trusted_ath_mcap(entry)

        if not ath_mcap and api_key and not rate_limited:
            recent_error = entry.get("ath_error_checked_at") and now - int(entry.get("ath_error_checked_at", 0)) < error_ttl
            if not recent_error and fetched < fetch_limit:
                try:
                    ath = fetch_solana_tracker_ath(http, token)
                    fetched += 1
                    if ath and not ath.get("error"):
                        entry["ath_checked_at"] = now
                        apply_solana_tracker_ath(entry, ath, observed_at)
                        ath_mcap = trusted_ath_mcap(entry)
                    elif ath and ath.get("error"):
                        entry["ath_error"] = ath.get("error")
                        entry["ath_error_checked_at"] = now
                        entry["ath_status"] = "error"
                    else:
                        entry["ath_error"] = "empty_ath_response"
                        entry["ath_error_checked_at"] = now
                        entry["ath_status"] = "error"
                    time.sleep(delay)
                except Exception as exc:
                    message = str(exc)
                    print(f"warn: reactivation ath filter failed for {token}: {message}", file=sys.stderr)
                    entry["ath_error"] = message
                    entry["ath_error_checked_at"] = now
                    entry["ath_status"] = "error"
                    fetched += 1
                    if "429" in message or "Too Many" in message:
                        rate_limited = True

        if not ath_mcap:
            if require_trusted:
                stats["missing_ath"] += 1
                continue
            kept.append(pool)
            stats["kept_without_ath"] += 1
            continue

        ratio = pool.mcap_usd / ath_mcap
        entry["ath_current_ratio"] = ratio
        entry["ath_drawdown_pct"] = max(0.0, (1 - ratio) * 100)
        entry["ath_filter_checked_at"] = observed_at
        if ratio <= max_ratio:
            kept.append(pool)
            stats["kept_corrected"] += 1
        else:
            stats["too_close_to_ath"] += 1

    stats["input_pools"] = len(pools)
    stats["kept_pools"] = len(kept)
    stats["ath_fetches"] = fetched
    if rate_limited:
        stats["rate_limited"] = 1
    config["_ath_filter_stats"] = dict(stats)
    return kept


def enrich_market_ath(http, state, pools, alerts, config, observed_at):
    if not config.get("ath_enabled", True):
        return
    market = state.setdefault("market", {})
    pool_tokens = [pool.token_address for pool in pools if pool.token_address]
    alert_tokens = [(alert.get("pool") or {}).get("token_address") for alert in alerts]
    recent_tokens = recent_alert_token_addresses(int(config.get("ath_recent_alert_limit", 100)))
    required_tokens = []
    for token in [*alert_tokens, *recent_tokens]:
        if token and token not in required_tokens:
            required_tokens.append(token)
    candidates = []
    for token in [*required_tokens, *pool_tokens]:
        if token and token not in candidates:
            candidates.append(token)
    max_tokens = int(config.get("ath_max_tokens_per_scan", 25))
    ttl = int(config.get("ath_cache_ttl_minutes", 360)) * 60
    error_ttl = int(config.get("ath_error_cache_ttl_minutes", 20)) * 60
    delay = float(config.get("ath_request_delay_seconds", 0.25))
    now = int(time.time())
    fetched = 0
    if not os.environ.get("SOLANA_TRACKER_API_KEY"):
        for token in required_tokens:
            entry = market.setdefault(token, {"token_address": token})
            entry["ath_status"] = "missing_api_key"
            entry["ath_error"] = "missing_solana_tracker_api_key"
            entry["ath_error_checked_at"] = now
        return
    for token in candidates:
        entry = market.setdefault(token, {"token_address": token})
        is_required = token in required_tokens
        has_trusted_ath = entry.get("ath_source") in ("solana_tracker", "ohlcv_high") and entry.get("ath_mcap_usd")
        if has_trusted_ath and not entry.get("ath_status"):
            entry["ath_status"] = "ready"
        if has_trusted_ath and entry.get("ath_checked_at") and now - int(entry.get("ath_checked_at", 0)) < ttl:
            continue
        if entry.get("ath_error_checked_at") and now - int(entry.get("ath_error_checked_at", 0)) < error_ttl:
            continue
        if not is_required and fetched >= max_tokens:
            break
        try:
            ath = fetch_solana_tracker_ath(http, token)
        except Exception as exc:
            print(f"warn: solana_tracker ath failed for {token}: {exc}", file=sys.stderr)
            entry["ath_error"] = str(exc)
            entry["ath_error_checked_at"] = now
            entry["ath_status"] = "error"
            fetched += 1
            continue
        fetched += 1
        if ath and not ath.get("error"):
            entry["ath_checked_at"] = now
            apply_solana_tracker_ath(entry, ath, observed_at)
        elif ath and ath.get("error"):
            entry["ath_error"] = ath.get("error")
            entry["ath_error_checked_at"] = now
            entry["ath_status"] = "error"
        else:
            entry["ath_error"] = "empty_ath_response"
            entry["ath_error_checked_at"] = now
            entry["ath_status"] = "error"
        time.sleep(delay)


def record_market_observations(state, pools, observed_at):
    market = state.setdefault("market", {})
    for pool in pools:
        key = pool.token_address or pool.pool_address
        if not key:
            continue
        entry = market.setdefault(key, {})
        entry.update(
            {
                "token_address": pool.token_address,
                "pool_address": pool.pool_address,
                "symbol": pool.symbol,
                "name": pool.name,
                "latest_mcap_usd": pool.mcap_usd,
                "latest_price_usd": pool.price_usd,
                "latest_liquidity_usd": pool.liquidity_usd,
                "latest_seen_at": observed_at,
                "scan_mcap_usd": pool.mcap_usd,
                "scan_price_usd": pool.price_usd,
                "scan_liquidity_usd": pool.liquidity_usd,
                "scan_mcap_at": observed_at,
                "scan_source": "scanner_snapshot",
            }
        )
        if pool.pair_created_at and (
            not entry.get("pair_created_at") or pool.pair_created_at < int(entry.get("pair_created_at", 0))
        ):
            entry["pair_created_at"] = pool.pair_created_at
            entry["pair_created_at_iso"] = iso(pool.pair_created_at)


def record_alert_observations(state, alerts):
    market = state.setdefault("market", {})
    for alert in alerts:
        pool = alert.get("pool") or {}
        key = pool.get("token_address") or pool.get("pool_address")
        if not key:
            continue
        signal_at = alert.get("window_start") or alert.get("created_at")
        obs_at = alert.get("obs_mcap_at") or alert.get("created_at") or signal_at
        entry = market.setdefault(key, {"token_address": pool.get("token_address")})
        existing_at = entry.get("first_signal_at")
        if existing_at and signal_at and parse_timestamp(signal_at) >= parse_timestamp(existing_at):
            continue
        entry.update(
            {
                "first_signal_at": signal_at,
                "first_obs_mcap_usd": alert.get("obs_mcap_usd") or pool.get("mcap_usd"),
                "first_obs_price_usd": alert.get("obs_price_usd") or pool.get("price_usd"),
                "first_obs_liquidity_usd": alert.get("obs_liquidity_usd") or pool.get("liquidity_usd"),
                "first_obs_mcap_at": obs_at,
                "first_obs_source": "first_alert_snapshot",
                "first_obs_lane": alert.get("lane"),
                "first_obs_score": alert.get("score"),
            }
        )


def apply_market_meta(pool_dict, state):
    if not pool_dict:
        return pool_dict
    market = state.get("market", {}) if state else {}
    meta = market.get(pool_dict.get("token_address") or pool_dict.get("pool_address"))
    if not meta:
        return pool_dict
    enriched = dict(pool_dict)
    for key in (
        "ath_mcap_usd",
        "ath_mcap_at",
        "ath_price_usd",
        "ath_pool_address",
        "ath_source",
        "ath_status",
        "ath_error",
        "ath_error_checked_at",
        "ath_current_ratio",
        "ath_drawdown_pct",
        "ath_filter_checked_at",
        "latest_mcap_usd",
        "latest_price_usd",
        "latest_liquidity_usd",
        "latest_seen_at",
        "scan_mcap_usd",
        "scan_price_usd",
        "scan_liquidity_usd",
        "scan_mcap_at",
        "scan_source",
        "first_signal_at",
        "first_obs_mcap_usd",
        "first_obs_price_usd",
        "first_obs_liquidity_usd",
        "first_obs_mcap_at",
        "first_obs_source",
        "first_obs_lane",
        "first_obs_score",
    ):
        if meta.get(key) is not None:
            enriched[key] = meta.get(key)
    if not enriched.get("pair_created_at") and meta.get("pair_created_at"):
        enriched["pair_created_at"] = meta.get("pair_created_at")
        enriched["pair_created_at_iso"] = meta.get("pair_created_at_iso")
    return enriched


def apply_market_meta_to_summary(summary, state):
    enriched = dict(summary)
    if "pool" in enriched:
        enriched["pool"] = apply_market_meta(enriched["pool"], state)
    return enriched


def apply_market_meta_to_alert(alert, state):
    enriched = dict(alert)
    if "pool" in enriched:
        enriched["pool"] = apply_market_meta(enriched["pool"], state)
    return enriched


def build_report_payload(universe, summaries, alerts, rpc_calls, config, generated_at, state):
    enriched_summaries = [apply_market_meta_to_summary(summary, state) for summary in summaries]
    enriched_alerts = [apply_market_meta_to_alert(alert, state) for alert in alerts]
    active = [summary for summary in enriched_summaries if summary.get("classified_buys")]
    active.sort(key=lambda item: item.get("buy_sol", 0.0), reverse=True)
    return {
        "generated_at": generated_at,
        "mode": config.get("mode"),
        "lane": config.get("lane"),
        "profile": config.get("lane") or config.get("mode"),
        "config": {
            "mcap_min_usd": config["mcap_min_usd"],
            "mcap_max_usd": config["mcap_max_usd"],
            "liquidity_min_usd": config["liquidity_min_usd"],
            "classify_buy_min_sol": config["classify_buy_min_sol"],
            "alert_window_minutes": config["alert_window_minutes"],
        },
        "stats": {
            "universe_pools": len(universe),
            "scanned_pools": len(summaries),
            "alerts": len(alerts),
            "rpc_calls": dict(rpc_calls),
        },
        "alerts": enriched_alerts,
        "active_pools": active[:100],
        "universe": [apply_market_meta(pool.as_dict(), state) for pool in universe[:250]],
        "summaries": enriched_summaries[:250],
    }


def write_report_json(payload):
    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def render_report(payload):
    config = payload["config"]
    stats = payload["stats"]
    alerts = payload["alerts"]
    lines = []
    lines.append(f"# Solana Radar Report")
    lines.append("")
    lines.append(f"- generated_at: {payload['generated_at']}")
    lines.append(f"- profile: {payload.get('profile') or payload.get('mode')}")
    if payload.get("lane_stats"):
        lines.append(f"- lanes_scanned: {', '.join(payload.get('lanes_scanned') or [])}")
    else:
        lines.append(f"- mcap_filter: ${config['mcap_min_usd']:,}-${config['mcap_max_usd']:,}")
        lines.append(f"- liquidity_min_usd: ${config['liquidity_min_usd']:,}")
        lines.append(f"- classify_buy_min_sol: {config['classify_buy_min_sol']}")
    lines.append(f"- universe_pools: {stats['universe_pools']}")
    lines.append(f"- scanned_pools: {stats['scanned_pools']}")
    lines.append(f"- alerts: {stats['alerts']}")
    lines.append(f"- rpc_calls: {stats['rpc_calls']}")
    lines.append("")

    if alerts:
        lines.append("## Alerts")
        for alert in alerts[:20]:
            pool = alert["pool"]
            lines.append("")
            lines.append(
                f"### {pool.get('symbol') or pool.get('name') or pool['pool_address']} "
                f"score {alert['score']}"
            )
            lines.append(f"- pool: {pool['pool_address']}")
            lines.append(f"- url: {pool.get('url')}")
            lines.append(f"- mcap_usd: {pool.get('mcap_usd'):.0f}")
            lines.append(f"- liquidity_usd: {pool.get('liquidity_usd'):.0f}")
            lines.append(f"- window: {alert['window_start']} - {alert['window_end']}")
            lines.append(f"- suspicious_wallets: {alert['suspicious_wallets']}")
            lines.append(f"- suspicious_sol: {alert['suspicious_sol']:.2f}")
            lines.append(f"- classes: {alert['classes']}")
            if alert["common_funders"]:
                lines.append(f"- common_funders: {alert['common_funders']}")
            if alert.get("common_recipients"):
                lines.append(f"- common_recipients: {alert['common_recipients']}")
            if alert.get("routed_buys"):
                lines.append(f"- routed_buys: {alert['routed_buys']}")
            token_intel = alert.get("token_intel") or {}
            narrative = token_intel.get("narrative") or {}
            if narrative:
                lines.append(
                    f"- narrative: {narrative.get('primary')} ({narrative.get('tilt')}) "
                    f"score={narrative.get('score')}"
                )
                if narrative.get("secondary"):
                    lines.append(f"- secondary_flavor: {narrative.get('secondary')}")
            social = alert.get("social")
            if social:
                if not social.get("enabled", True):
                    lines.append(f"- social: disabled ({social.get('reason')})")
                else:
                    lines.append(
                        f"- social: heat={social.get('heat')} score={social.get('score')} "
                        f"x_posts={social.get('x_posts')} authors={social.get('unique_authors')} "
                        f"cache={social.get('cache')}"
                    )
                    if social.get("watched_account_hits"):
                        lines.append(f"- social_watched_hits: {social['watched_account_hits']}")
                    if social.get("top_authors"):
                        lines.append(f"- social_top_authors: {social['top_authors'][:5]}")
                    if social.get("results"):
                        lines.append("- social_results:")
                        for item in social["results"][:5]:
                            lines.append(
                                f"  - @{item.get('author')}: {item.get('title')} {item.get('url')}"
                            )
            lines.append("- top_events:")
            for event in sorted(alert["events"], key=lambda item: item.get("sol_amount", 0.0), reverse=True)[:8]:
                lines.append(
                    f"  - {event['time']} {event['wallet_class']} "
                    f"{event['sol_amount']:.2f} SOL signer={event['signer']} "
                    f"recipient={event.get('token_recipient')}"
                )
    else:
        lines.append("## Alerts")
        lines.append("")
        lines.append("No alerts in this scan.")

    lines.append("")
    lines.append("## Active Pools")
    active = payload["active_pools"]
    if not active:
        lines.append("")
        lines.append("No classified buys above threshold.")
    for summary in active[:30]:
        pool = summary["pool"]
        lines.append(
            f"- {pool.get('symbol') or pool.get('name') or pool['pool_address']}: "
            f"{summary['classified_buys']} buys, {summary['buy_sol']:.2f} SOL, "
            f"classes={summary['classes']}, mcap=${pool.get('mcap_usd'):.0f}, "
            f"pool={pool['pool_address']}"
        )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n")


def scan_with_config(http, rpc, state, config):
    label = config.get("lane") or config.get("mode") or "scan"
    print(f"Building market universe for {label}...", flush=True)
    universe = build_universe(http, config)
    observed_at = utc_now().isoformat().replace("+00:00", "Z")
    before_ath_filter = len(universe)
    universe = filter_reactivation_by_ath(http, state, universe, config, observed_at)
    ath_filter_stats = config.get("_ath_filter_stats") or {}
    if ath_filter_stats:
        print(
            f"{label}: ATH correction filter kept {len(universe)}/{before_ath_filter} pools "
            f"(max current/ATH {float(config['ath_max_current_ratio']) * 100:.0f}%)",
            flush=True,
        )
    scan_targets = universe[: int(config["active_pool_limit"])]
    print(f"{label}: universe {len(universe)} pools, scanning {len(scan_targets)}", flush=True)
    classification_budget = {
        "remaining": int(config["max_wallet_classifications_per_scan"]),
        "deep_audits_remaining": int(config.get("helius_deep_audit_max_pools_per_scan", 0)),
    }

    all_alerts = []
    summaries = []
    for index, pool in enumerate(scan_targets, start=1):
        print(f"{label}: scanning {index}/{len(scan_targets)} {pool.symbol}", flush=True)
        alerts, summary = scan_pool(rpc, pool, config, state, classification_budget)
        summaries.append(summary)
        all_alerts.extend(alerts)
        if index % 25 == 0:
            print(f"{label}: scanned {index}/{len(scan_targets)} pools", flush=True)
        time.sleep(0.05)

    all_alerts = enrich_alerts_with_social(http, all_alerts, config, state)
    return universe, summaries, all_alerts


def run_once(config, lane_name=None):
    load_env()
    api_key = os.environ.get("HELIUS_API_KEY")
    if not api_key:
        raise SystemExit("HELIUS_API_KEY is missing. Put it in .env or environment.")

    http = Http()
    rpc = HeliusRpc(
        api_key,
        timeout_seconds=int(config.get("helius_rpc_timeout_seconds", 30)),
        transactions_timeout_seconds=int(config.get("helius_transactions_timeout_seconds", 25)),
    )
    health = rpc.health()
    if health != "ok":
        raise SystemExit(f"Helius health is not ok: {health}")

    state = load_json(STATE_PATH, {"pools": {}, "wallet_cache": {}})
    lane_list = selected_lanes(config, lane_name) if config.get("lanes") else []
    if not lane_list:
        lane_list = [None]
    all_alerts = []
    summaries = []
    universe = []
    lane_stats = {}
    for lane in lane_list:
        lane_config = apply_lane(config, lane) if lane else config
        lane_universe, lane_summaries, lane_alerts = scan_with_config(http, rpc, state, lane_config)
        all_alerts.extend(lane_alerts)
        summaries.extend(lane_summaries)
        universe.extend(lane_universe)
        lane_stats[lane_config.get("lane") or lane_config.get("mode") or "scan"] = {
            "universe_pools": len(lane_universe),
            "scanned_pools": min(len(lane_universe), int(lane_config["active_pool_limit"])),
            "alerts": len(lane_alerts),
            "mcap_min_usd": lane_config["mcap_min_usd"],
            "mcap_max_usd": lane_config["mcap_max_usd"],
            "liquidity_min_usd": lane_config["liquidity_min_usd"],
            "age_min_hours": lane_config.get("age_min_hours"),
            "age_max_hours": lane_config.get("age_max_hours"),
            "volume_1h_min_usd": lane_config.get("volume_1h_min_usd"),
            "volume_1h_max_usd": lane_config.get("volume_1h_max_usd"),
            "volume_1h_to_mcap_min": lane_config.get("volume_1h_to_mcap_min"),
            "volume_1h_to_liquidity_min": lane_config.get("volume_1h_to_liquidity_min"),
            "ath_max_current_ratio": lane_config.get("ath_max_current_ratio"),
            "ath_require_trusted": lane_config.get("ath_require_trusted"),
            "ath_filter_stats": lane_config.get("_ath_filter_stats"),
        }

    generated_at = utc_now().isoformat().replace("+00:00", "Z")
    record_market_observations(state, universe, generated_at)
    record_alert_observations(state, all_alerts)
    enrich_market_ath(http, state, universe, all_alerts, config, generated_at)
    save_json(STATE_PATH, state)
    write_alerts(all_alerts)
    report_payload = build_report_payload(universe, summaries, all_alerts, rpc.calls, config, generated_at, state)
    report_payload["lane_stats"] = lane_stats
    report_payload["lanes_scanned"] = list(lane_stats)
    write_report_json(report_payload)
    render_report(report_payload)

    print(f"Helius: {health}")
    print(f"Universe pools: {len(universe)}")
    print(f"Scanned pools: {len(summaries)}")
    print(f"Alerts: {len(all_alerts)}")
    print(f"Report: {REPORT_PATH}")
    print(f"Report JSON: {REPORT_JSON_PATH}")
    print(f"RPC calls: {dict(rpc.calls)}")


def main():
    parser = argparse.ArgumentParser(description="Solana fresh/dormant wallet radar.")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit.")
    parser.add_argument("--watch", action="store_true", help="Run forever on scan_interval_seconds.")
    parser.add_argument("--mode", choices=["aggressive", "balanced", "conservative"], help="Scan profile.")
    parser.add_argument(
        "--lane",
        choices=["all", "incubation", "young", "breakout", "reactivation"],
        help="Lane profile. Defaults to all lane-based filters.",
    )
    args = parser.parse_args()

    config = load_json(CONFIG_PATH if CONFIG_PATH.exists() else DEFAULT_CONFIG_PATH, {})
    if args.mode and not args.lane:
        config = apply_mode(config, args.mode)
        config.pop("lanes", None)
        config.pop("lane_order", None)
        config.pop("lane", None)
    else:
        config["lane"] = args.lane or config.get("lane") or "all"
    if not args.once and not args.watch:
        args.once = True

    while True:
        run_once(config, args.lane)
        if not args.watch:
            break
        time.sleep(int(config["scan_interval_seconds"]))


if __name__ == "__main__":
    main()
