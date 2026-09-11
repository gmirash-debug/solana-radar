"""Read-only LI.FI completed-route attribution with a small shared time budget."""
import time
import requests


def completed_output(data, tx, token, chain):
    try:
        received, sent = data["receiving"], data["sending"]
        if data["status"] != "DONE" or data["substatus"] != "COMPLETED":
            return None
        if received["txHash"].lower() != tx.lower() or received["chainId"] != chain or received["token"]["address"].lower() != token:
            return None
        if received["token"].get("chainId", chain) != chain or not sent.get("txHash") or int(received["amount"]) <= 0:
            return None
        if type(sent["chainId"]) is not int or sent["chainId"] <= 0 or sent["chainId"] == chain:
            return None
        if not isinstance(data["fromAddress"], str) or not data["fromAddress"]:
            return None
        return {"recipient": data["toAddress"].lower(), "source_address": data["fromAddress"],
            "source_chain_id": sent["chainId"], "bought_raw": str(int(received["amount"])),
            "transaction": tx, "request_id": sent["txHash"], "service": "LI.FI"}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


class LifiClient:
    def __init__(self, session, store, deadline, max_calls=16):
        self.session, self.store, self.deadline = session, store, deadline
        self.max_calls, self.calls, self.remaining_seconds = max_calls, 0, 30
        self.status = "ready"
        self.next_at = 0

    def lookup(self, tx, token, chain):
        key = f"lifi:{chain}:{token}:{tx}"
        cached = self.store.get(key, 7 * 86400)
        if cached is not None:
            return completed_output(cached, tx, token, chain)
        if self.status in ("unavailable", "rate_limited"):
            return None
        if self.calls >= self.max_calls or self.remaining_seconds < 8 or time.monotonic() + 60 >= self.deadline:
            self.status = "budget_exhausted"
            return None
        start = time.monotonic()
        time.sleep(max(0, self.next_at - start))
        self.next_at = time.monotonic() + 0.6
        self.calls += 1
        try:
            response = self.session.get("https://li.quest/v1/status", params={"txHash":tx,"toChain":chain},timeout=8)
            if response.status_code == 404:
                # Indexing can lag. Do not persist a negative result.
                return None
            if response.status_code == 429:
                self.status = "rate_limited"
                return None
            response.raise_for_status()
            data = response.json()
            match = completed_output(data, tx, token, chain)
            if match:
                self.store.put(key, {k:data[k] for k in ("status","substatus","sending","receiving","toAddress","fromAddress")})
            return match
        except (requests.RequestException, ValueError, TypeError):
            self.status = "unavailable"
            return None
        finally:
            self.remaining_seconds -= time.monotonic() - start

    def summary(self):
        return {"status":self.status,"requests":self.calls,"max_requests":self.max_calls}
