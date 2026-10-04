"""Native weighted pacing and leaky buckets, thread-safe ONLY within a process.

Parent: call ProviderLimiters.acquire(provider_name, method, deadline) immediately
before EVERY transport attempt, including retries and chain/capability probes.
Use request_reservation for the separate monthly limit. Replace scanner's fixed
provider/method sleeps via wait_for_rate_slot; do not install a second per-method
Alchemy limiter (all methods/chains consume the same throughput pool).

Helius JSON-RPC uses 10 rps; indexed history additionally uses a conservative
2 rps lane. DAS/enhanced APIs use the separate 2 rps lane. The indexed cap is a
local safety choice, not a claim that Helius bills history as an Enhanced API.
Chainstack uses 25 rps regardless of 1/2 RU billing. Alchemy uses 300 throughput
CU/s conservatively, not a verified account rate. Concurrent processes need
disjoint parent-assigned rates summing to these caps, or full-run serialization.
"""
import math
import threading
import time

from rpc_budget import HELIUS_DAS_METHODS, throughput_cost


class RateLimitDeadline(RuntimeError):
    pass


class NativeUnitLimiter:
    def __init__(self, rate, capacity=None, clock=None, sleep=None):
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("rate must be positive and finite")
        if capacity is not None and (not math.isfinite(capacity) or capacity <= 0):
            raise ValueError("capacity must be positive and finite")
        self.rate = float(rate)
        self.capacity = float(capacity) if capacity is not None else None
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.next_at = 0.0
        self.level = 0.0
        self.updated_at = None
        self.lock = threading.Lock()

    def acquire(self, units=1, deadline=None):
        if isinstance(units, bool) or not math.isfinite(units) or units <= 0:
            raise ValueError("throughput units must be positive and finite")
        if self.capacity is not None and units > self.capacity:
            raise ValueError("request weight exceeds bucket capacity")
        with self.lock:
            now = self.clock()
            if self.capacity is None:
                delay = max(0.0, self.next_at - now)
            else:
                elapsed = max(0.0, now - self.updated_at) if self.updated_at is not None else 0.0
                level = max(0.0, self.level - elapsed * self.rate)
                delay = max(0.0, (level + units - self.capacity) / self.rate)
            if deadline is not None and now + delay >= deadline:
                raise RateLimitDeadline("native limiter time budget exhausted")
            if delay:
                self.sleep(delay)
            started = max(self.clock(), now + delay)
            if deadline is not None and started >= deadline:
                raise RateLimitDeadline("native limiter time budget exhausted")
            if self.capacity is None:
                self.next_at = started + units / self.rate
            else:
                self.level = max(0.0, level - (started - now) * self.rate) + units
                self.updated_at = started
            return delay


class ProviderLimiters:
    def __init__(self, clock=None, sleep=None, alchemy_rate=300, helius_rate=10,
                 indexed_rate=2, chainstack_rate=25):
        args = {"clock": clock, "sleep": sleep}
        self.buckets = {
            "alchemy": NativeUnitLimiter(alchemy_rate, **args),
            "helius": NativeUnitLimiter(helius_rate, **args),
            "helius_indexed": NativeUnitLimiter(indexed_rate, **args),
            "helius_enhanced": NativeUnitLimiter(indexed_rate, **args),
            "chainstack": NativeUnitLimiter(chainstack_rate, **args),
        }

    def acquire(self, provider, method, deadline=None):
        if provider == "helius":
            if method in HELIUS_DAS_METHODS or method in {"enhancedTransactions", "parsedEvents"}:
                return self.buckets["helius_enhanced"].acquire(deadline=deadline)
            waited = 0
            if method == "getTransactionsForAddress":
                waited += self.buckets["helius_indexed"].acquire(deadline=deadline)
            return waited + self.buckets["helius"].acquire(deadline=deadline)
        return self.buckets[provider].acquire(throughput_cost(provider, method), deadline)


DEFAULT_PROVIDER_LIMITERS = ProviderLimiters()


def wait_for_provider_slot(provider, method, params=None, deadline=None, limiters=None):
    """Drop-in scanner rate-slot hook; params is for the send-hook contract."""
    return (limiters or DEFAULT_PROVIDER_LIMITERS).acquire(provider, method, deadline)


# Endpoint-specific docs supersede the older generic CLI '10/10' example:
# https://github.com/GMGNAI/gmgn-skills/blob/main/skills/gmgn-token/SKILL.md
# https://github.com/GMGNAI/gmgn-skills/blob/main/skills/gmgn-market/SKILL.md
GMGN_WEIGHTS = {
    ("token", "info"): 1, ("token", "security"): 1, ("token", "pool"): 1,
    ("token", "holders"): 5, ("token", "traders"): 5,
    ("market", "kline"): 2, ("market", "trending"): 3,
    ("market", "trenches"): 2, ("market", "signal"): 1,
    ("market", "hot-searches"): 3, ("market", "search"): 1,
}
DEFAULT_GMGN_LIMITER = NativeUnitLimiter(5, capacity=5)


def gmgn_request_weight(arguments):
    """CLI arguments start at token/market; never include a key or executable."""
    command = tuple(arguments[:2])
    if command not in GMGN_WEIGHTS:
        raise ValueError("unknown or non-read-only GMGN command weight")
    if command == ("market", "kline"):
        if "--resolution=1s" in arguments or any(
                flag == "--resolution" and i + 1 < len(arguments) and arguments[i + 1] == "1s"
                for i, flag in enumerate(arguments)):
            raise ValueError("1s GMGN kline requires separately verified Pro/global limits")
    return GMGN_WEIGHTS[command]


gmgn_weight = gmgn_request_weight


def wait_for_gmgn_slot(arguments, deadline=None, limiter=None):
    return (limiter or DEFAULT_GMGN_LIMITER).acquire(gmgn_request_weight(arguments), deadline)
