"""Conservative native-unit reservations, not provider invoices or account quota.

Parent integration (no per-RPC remote calls):
* Restore state['rpc_monthly_usage'] from the durable runtime checkpoint BEFORE
  constructing either client. Pass that same state to configure_monthly_budgets
  and robinhood.scan(shared_state=state). Alchemy has ONE provider entry, not an
  EVM entry. Preserve it on success, partial scans, errors, and evidence rollback.
* A shared dict is NOT cross-process safe. The parent must serialize load/run/
  checkpoint for ALL spenders, including Robinhood, not just Solana state writers.
* For concurrent workers, the serialized parent can allocate_run_budget, persist
  the updated ledger BEFORE dispatch, and pass the resulting grant as a worker's
  monthly_budget. Never replay a grant or refund unused/failed/crashed attempts.
  Expired grants fail closed. This bounds aggregate reservations without a DO
  request per RPC. Grant consumption is local; issuance needs durable serialization.
* If even grant issuance cannot be serialized, provision disjoint, durable role
  allowances whose SUM is below the verified account allowance (including other
  apps). Never give each process a fresh full allowance or use last-writer-wins
  ledger merges. Missing state in Robinhood disables Alchemy, not public RPC.

Calendar UTC months are a LOCAL safety window, not an assertion about a provider's
invoice reset date. Parent must configure headroom for existing account spend,
other apps, billing-period differences, and older underestimated ledger entries.
Costs checked 2026-10-04:
https://www.alchemy.com/docs/reference/compute-unit-costs
https://www.helius.dev/docs/billing/credits
https://www.helius.dev/docs/rpc/gettransactionsforaddress
https://docs.chainstack.com/docs/request-units
"""
from datetime import datetime, timezone
from threading import RLock


DEFAULT_MONTHLY_LIMITS = {"alchemy": 25_000_000, "helius": 900_000, "chainstack": 2_700_000}
NATIVE_UNITS = {"alchemy": "CU", "helius": "credits", "chainstack": "RU"}
ALCHEMY_COSTS = {
    "eth_chainId": 0, "net_version": 0, "web3_clientVersion": 0,
    "eth_blockNumber": 10, "eth_getBalance": 20, "eth_getBlockByNumber": 20,
    "eth_getBlockByHash": 20, "eth_getCode": 20, "eth_getStorageAt": 20,
    "eth_getTransactionByHash": 20, "eth_getTransactionReceipt": 20,
    "eth_getTransactionCount": 20, "eth_call": 26, "eth_getLogs": 60,
    "getAccountInfo": 10, "getBalance": 10, "getTokenAccountsByOwner": 10,
    "getTokenAccountsByOwnerV2": 10, "getMultipleAccounts": 20,
    "getHealth": 20, "getSlot": 20, "getBlockHeight": 20, "getBlockTime": 20,
    "getLatestBlockhash": 20, "getSignatureStatuses": 20,
    "getTokenLargestAccounts": 20, "getTokenAccountBalance": 20,
    "getTokenSupply": 20, "getFirstAvailableBlock": 40, "getBlock": 40,
    "getSignaturesForAddress": 40, "getTransaction": 40,
    "getTransactionsForAddress": 100,
}
ALCHEMY_THROUGHPUT_COSTS = {"eth_chainId": 5, "net_version": 5, "web3_clientVersion": 5}
HELIUS_DAS_METHODS = frozenset({
    "getAsset", "getAssetProof", "getAssetProofBatch", "getAssetBatch",
    "getAssetsByOwner", "getAssetsByAuthority", "getAssetsByCreator",
    "getAssetsByGroup", "getSignaturesForAsset", "getNftEditions",
    "getTokenAccounts", "searchAssets",
})
# Without the node's retention boundary/transaction slot, reserve the archive
# upper bound. Do not advertise archive availability just because it is priced.
CHAINSTACK_ARCHIVE_METHODS = frozenset({
    "getTransaction", "getBlock", "getBlockTime", "getBlocks", "getBlocksWithLimit",
    "getSignaturesForAddress", "getFirstAvailableBlock", "getSignatureStatuses",
})
_UNSET = object()
_LEDGER_LOCK = RLock()  # Only coordinates threads in this interpreter.


def _units(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("native units must be a nonnegative integer")
    return value


def native_cost(provider, method, params=None, result=_UNSET):
    """Reserve before send; optional response estimate is never auto-refunded."""
    if provider == "alchemy":
        if method not in ALCHEMY_COSTS:
            raise ValueError("unknown Alchemy method cost")
        return ALCHEMY_COSTS[method]
    if provider == "helius":
        if method == "getTransactionsForAddress":
            options = params[1] if len(params or []) > 1 and isinstance(params[1], dict) else {}
            mode = options.get("transactionDetails", "full")
            if mode not in {"full", "signatures"}:
                raise ValueError("invalid indexed history response mode")
            limit = options.get("limit", 100)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
                raise ValueError("invalid indexed history page size")
            if mode == "signatures":
                return 10
            count = limit
            if result is not _UNSET and result is not None:
                if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                    raise ValueError("invalid indexed history response")
                count = len(result["data"])
            return max(10, 10 * ((count + 99) // 100))
        if method in HELIUS_DAS_METHODS or method in {"getProgramAccounts", "getTransfersByAddress"}:
            return 10
        return 1
    if provider == "chainstack":
        return 2 if method in CHAINSTACK_ARCHIVE_METHODS else 1
    raise ValueError("unknown native-unit provider")


def throughput_cost(provider, method):
    if provider == "alchemy":
        return ALCHEMY_THROUGHPUT_COSTS.get(method, native_cost(provider, method))
    return 1  # Helius and Chainstack throughput is requests, not billing credits/RU.


class MonthlyRpcBudget:
    def __init__(self, ledger, provider, limit, now=None, clock=None):
        self.ledger = ledger
        self.provider = provider
        self.limit = _units(limit)
        self.clock = clock or (lambda: now if now is not None else datetime.now(timezone.utc))

    @property
    def period(self):
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc).strftime("%Y-%m")

    @property
    def entry(self):
        return self._entry_for_period(self.period)

    def _entry_for_period(self, period):
        entry = self.ledger.setdefault(period, {}).setdefault(
            self.provider, {"estimated_units": 0, "attempts": 0})
        _units(entry["estimated_units"])
        _units(entry["attempts"])
        return entry

    @property
    def remaining(self):
        with _LEDGER_LOCK:
            return max(0, self.limit - self.entry["estimated_units"])

    def allows(self, units):
        return _units(units) <= self.remaining

    def reserve(self, units):
        units = _units(units)
        with _LEDGER_LOCK:
            entry = self.entry
            if entry["estimated_units"] + units > self.limit:
                return False
            entry["estimated_units"] += units
            entry["attempts"] += 1
            return True

    def snapshot(self):
        with _LEDGER_LOCK:
            period = self.period
            entry = self._entry_for_period(period)
            return {"period": period, "provider": self.provider,
                    "unit": NATIVE_UNITS.get(self.provider, "estimated_units"),
                    "estimated_units": entry["estimated_units"], "attempts": entry["attempts"],
                    "limit": self.limit, "remaining": max(0, self.limit - entry["estimated_units"]),
                    "source": "scanner_estimate_not_account_quota",
                    "allocated_units": entry.get("allocated_units", 0),
                    "allocations": entry.get("allocations", 0),
                    "coordination": "parent_serialized_state_required"}


def monthly_budget_for(state, provider, config=None, **kwargs):
    limits = {**DEFAULT_MONTHLY_LIMITS, **((config or {}).get("rpc_monthly_estimated_limits") or {})}
    # No default unlimited allowance for a newly introduced provider.
    return MonthlyRpcBudget(state.setdefault("rpc_monthly_usage", {}), provider,
                            limits.get(provider, 0), **kwargs)


def configure_monthly_budgets(rpc, state, config, **kwargs):
    for name, provider in rpc.providers.items():
        if name in DEFAULT_MONTHLY_LIMITS:
            provider.monthly_budget = monthly_budget_for(state, name, config, **kwargs)
    # Never prune a current/future bucket based on lexicographic position: a
    # clock rollback or a stale checkpoint must not recreate its allowance.


def request_reservation(provider, method, params):
    model = provider.credit_model
    if model in NATIVE_UNITS:
        return native_cost(model, method, params)
    return provider.credit_cost(method, None)


class PreallocatedRpcBudget:
    """Single-worker grant; payload replay in another worker is NOT safe."""
    def __init__(self, payload, clock=None):
        if payload.get("provider") not in NATIVE_UNITS:
            raise ValueError("unknown grant provider")
        if payload.get("role") not in {"solana", "robinhood"}:
            raise ValueError("unknown grant role")
        self.provider = payload["provider"]
        self.period = payload["period"]
        self.limit = _units(payload["limit"])
        self.used = 0
        self.attempts = 0
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.role = payload["role"]

    @property
    def remaining(self):
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if now.astimezone(timezone.utc).strftime("%Y-%m") != self.period:
            return 0
        return max(0, self.limit - self.used)

    def allows(self, units):
        units = _units(units)
        # Even zero-CU probes may not use a grant from a previous month.
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc).strftime("%Y-%m") == self.period and units <= self.remaining

    def reserve(self, units):
        with _LEDGER_LOCK:
            if not self.allows(units):
                return False
            self.used += units
            self.attempts += 1
            return True

    def snapshot(self):
        return {"provider": self.provider, "period": self.period, "role": self.role,
                "unit": NATIVE_UNITS[self.provider], "limit": self.limit,
                "estimated_units": self.used, "attempts": self.attempts,
                "remaining": self.remaining, "source": "preallocated_parent_reservation",
                "coordination": "single_worker_no_replay"}


def allocate_run_budget(budget, units, role):
    """Parent must checkpoint this reservation before handing the grant to a worker."""
    units = _units(units)
    if role not in {"solana", "robinhood"}:
        raise ValueError("unknown grant role")
    with _LEDGER_LOCK:
        period = budget.period
        entry = budget._entry_for_period(period)
        if entry["estimated_units"] + units > budget.limit:
            return None
        entry["estimated_units"] += units
        entry["allocated_units"] = entry.get("allocated_units", 0) + units
        entry["allocations"] = entry.get("allocations", 0) + 1
        return {"provider": budget.provider, "period": period, "role": role, "limit": units}


class DurableChunkBudget:
    """Serialized parent persists small grants before RPC and never refunds them."""
    def __init__(self, account, commit, chunk, role="solana"):
        self.account, self.commit = account, commit
        self.chunk, self.role = max(1, _units(chunk)), role
        self.grant = None
        self.used = self.attempts = 0
        self.persistence_failed = False

    @property
    def remaining(self):
        return 0 if self.persistence_failed else self.account.remaining + (self.grant.remaining if self.grant else 0)

    def allows(self, units):
        return _units(units) <= self.remaining and not self.persistence_failed

    def reserve(self, units):
        units = _units(units)
        with _LEDGER_LOCK:
            if self.persistence_failed:
                return False
            if not self.grant or not self.grant.allows(units):
                if units > self.account.remaining:
                    return False
                payload = allocate_run_budget(self.account, min(self.account.remaining, max(self.chunk, units)), self.role)
                if payload is None:
                    return False
                try:
                    accepted = self.commit()
                except Exception:
                    accepted = False
                if accepted is not True:
                    # The write may have committed despite a lost reply. Keep
                    # its charge and disable this run rather than reuse it.
                    self.persistence_failed = True
                    return False
                self.grant = PreallocatedRpcBudget(payload, clock=self.account.clock)
            if not self.grant.reserve(units):
                return False
            self.used += units
            self.attempts += 1
            return True

    def snapshot(self):
        return {**self.account.snapshot(), "run_used_units": self.used,
                "run_attempts": self.attempts, "grant_chunk": self.chunk,
                "persistence_failed": self.persistence_failed,
                "coordination": "durable_preallocation_serialized_parent"}
