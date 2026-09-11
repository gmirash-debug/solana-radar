"""Bounded Relay attribution and descriptive buy waves, not ownership clustering."""
import os
import time
from collections import Counter

import requests

ENDPOINT = "https://api.relay.link/requests/v2"
SOLVER = "0xf70da97812cb96acdf810712aa562db8dfa3dbef"


def compact_requests(rows):
    output = []
    for r in rows:
        data = r.get("data", {})
        legs = {key: [{k: t[k] for k in ("hash", "txHash", "chainId", "status") if k in t}
            for t in data.get(key, [])] for key in ("inTxs", "outTxs")}
        if "route" in data:
            legs["route"] = {"actual": {side: {"outputCurrency": value.get("outputCurrency")}
                for side, value in data["route"].get("actual", {}).items() if side in ("origin", "destination") and isinstance(value, dict)}}
        else:
            legs["metadata"] = {"currencyOut": data.get("metadata", {}).get("currencyOut")}
        output.append({**{k: r[k] for k in ("id", "status", "user", "recipient") if k in r}, "data": legs})
    return output


class RelayClient:
    def __init__(self, session, store, deadline, max_calls=96, max_seconds=90):
        self.session, self.store = session, store
        self.deadline = deadline
        self.remaining_seconds = max_seconds
        self.api_key = os.environ.get("RELAY_API_KEY", "")
        self.version = "v3" if self.api_key else "v2"
        self.deprecated = self.version == "v2"
        self.max_calls, self.calls = max_calls, 0
        self.next_at = 0
        self.status = "ready"

    def lookup(self, tx):
        key = "relay:" + self.version + ":" + tx.lower()
        cached = self.store.get(key, 7 * 86400)
        if cached is not None:
            return cached
        if self.status in ("unavailable", "rate_limited"):
            return None
        if self.calls >= self.max_calls or self.remaining_seconds < 9 or time.monotonic() + 9 >= self.deadline:
            self.status = "budget_exhausted"
            return None
        started = time.monotonic()
        time.sleep(max(0, self.next_at - time.monotonic()))
        self.calls += 1
        self.next_at = time.monotonic() + 0.55
        try:
            response = self.session.get(ENDPOINT.replace("v2", self.version),
                params={"fillTxHash" if self.version == "v3" else "hash": tx, "limit": 50},
                headers={"x-api-key": self.api_key} if self.api_key else {}, timeout=8)
            if response.status_code == 429:
                self.status = "rate_limited"
                return None
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("requests")
            if not isinstance(rows, list) or payload.get("continuation") or len(rows) >= 50:
                raise ValueError("Incomplete Relay lookup")
            rows = compact_requests(rows)
            # Unresolved/indexer-lagged results are retried next scan, never cached as no activity.
            if rows and all(isinstance(r, dict) and r.get("status") in ("success", "refund", "failure") for r in rows):
                self.store.put(key, rows)
            return rows
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            self.status = "unavailable"
            return None
        finally:
            self.remaining_seconds -= time.monotonic() - started

    def summary(self):
        return {"status": self.status, "requests": self.calls, "max_requests": self.max_calls,
            "api_version": self.version, "deprecated": self.deprecated,
            "migration_required_by": "2026-11-24" if self.deprecated else None}


def request_output(rows, tx, token, chain_id):
    """Accept one executed destination leg, never quoted output or a refund."""
    matches = []
    for r in rows or []:
        try:
            data = r["data"]
            if "route" in data:
                actual = data["route"]["actual"]
                out = (actual.get("destination") or actual["origin"])["outputCurrency"]
            else:
                out = data["metadata"]["currencyOut"]
            currency = out["currency"]
            if r["status"] != "success" or currency["chainId"] != chain_id or currency["address"].lower() != token:
                continue
            legs = [t for t in data["outTxs"] if t.get("chainId") == chain_id and t.get("txHash", t.get("hash", "")).lower() == tx.lower() and t.get("status") == "success"]
            inputs = [t for t in data["inTxs"] if t.get("status") == "success" and t.get("txHash", t.get("hash"))]
            if len(legs) != 1 or not inputs or int(out["amount"]) <= 0:
                continue
            matches.append({"recipient": r["recipient"].lower(), "bought_raw": str(int(out["amount"])),
                "source_address": r["user"], "source_chain_id": inputs[0]["chainId"], "request_id": r["id"], "transaction": tx})
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    return matches[0] if len(matches) == 1 else None


def buy_wave(events, supply, config):
    """Earliest qualifying observed window; gross volume is not retained supply."""
    unique = {e["transaction"]: e for e in events}
    ordered = sorted(unique.values(), key=lambda e: (e["timestamp"], e["transaction"]))
    best = None
    for last in ordered:
        for seconds, min_buyers, min_supply in config["relay_windows"]:
            window = [e for e in ordered if last["timestamp"] - seconds < e["timestamp"] <= last["timestamp"]]
            totals = Counter()
            for e in window:
                totals[e["recipient"]] += int(e["bought_raw"])
            # Dust recipients must not manufacture a distributed wave.
            material = {w: n for w, n in totals.items() if 100 * n / supply >= config["relay_min_wallet_supply_pct"]}
            gross = sum(material.values())
            if len(material) < min_buyers or 100 * gross / supply < min_supply:
                continue
            if max(material.values()) / gross > config["relay_max_buyer_share"]:
                continue
            selected = [e for e in window if e["recipient"] in material]
            best = {"window_seconds": seconds, "buyers": len(material), "buy_transactions": len(selected),
                "gross_bought_supply_pct": 100 * gross / supply, "gross_bought_raw": str(gross),
                "from_timestamp": min(e["timestamp"] for e in selected), "to_timestamp": last["timestamp"],
                "from_block": min(e["block"] for e in selected), "events": selected,
                "baseline_status": "not_established", "retention_status": "pending"}
            break
        if best:
            break
    return best
