"""Shared estimated usage across hourly and targeted runs, not an account billing API."""
from datetime import datetime, timezone


class MonthlyRpcBudget:
    def __init__(self, ledger, provider, limit, now=None):
        self.ledger = ledger
        self.provider = provider
        self.limit = max(0, int(limit or 0))
        self.period = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
        self.entry = ledger.setdefault(self.period, {}).setdefault(provider, {"estimated_units": 0, "attempts": 0})

    @property
    def remaining(self):
        return max(0, self.limit - self.entry["estimated_units"]) if self.limit else None

    def allows(self, units):
        return not self.limit or units <= self.remaining

    def reserve(self, units):
        units = max(0, int(units))
        if not self.allows(units):
            return False
        self.entry["estimated_units"] += units
        self.entry["attempts"] += 1
        return True

    def snapshot(self):
        return {"period": self.period, "estimated_units": self.entry["estimated_units"],
                "limit": self.limit or None, "remaining": self.remaining,
                "source": "scanner_estimate_not_account_quota"}


def configure_monthly_budgets(rpc, state, config):
    ledger = state.setdefault("rpc_monthly_usage", {})
    defaults = {"alchemy": 25_000_000, "helius": 900_000, "chainstack": 2_700_000}
    limits = config.get("rpc_monthly_estimated_limits", defaults)
    for name, provider in rpc.providers.items():
        provider.monthly_budget = MonthlyRpcBudget(ledger, name, limits.get(name, 0))
    for old in sorted(ledger)[:-3]:
        del ledger[old]


def request_reservation(provider, method, params):
    units = provider.credit_cost(method, None)
    if method == "getTransactionsForAddress" and provider.credit_model == "helius":
        options = params[1] if len(params or []) > 1 and isinstance(params[1], dict) else {}
        units = max(units, 10 * ((int(options.get("limit") or 100) + 99) // 100))
    return units
