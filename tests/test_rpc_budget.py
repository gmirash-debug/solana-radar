import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from rpc_budget import (MonthlyRpcBudget, PreallocatedRpcBudget, allocate_run_budget,
                        configure_monthly_budgets, monthly_budget_for, native_cost,
                        request_reservation, throughput_cost)
from runtime_checkpoint import build_checkpoint, decode_checkpoint


class RpcBudgetTests(unittest.TestCase):
    def test_documented_alchemy_costs_and_zero_billing_nonzero_throughput(self):
        costs = {"getAccountInfo": 10, "getTokenAccountsByOwner": 10,
                 "getMultipleAccounts": 20, "getTokenLargestAccounts": 20,
                 "getSignaturesForAddress": 40, "getTransaction": 40,
                 "getTransactionsForAddress": 100, "eth_getTransactionReceipt": 20,
                 "eth_getLogs": 60, "eth_call": 26, "eth_blockNumber": 10}
        for method, cost in costs.items():
            self.assertEqual(native_cost("alchemy", method), cost)
        self.assertEqual(native_cost("alchemy", "eth_chainId"), 0)
        self.assertEqual(throughput_cost("alchemy", "eth_chainId"), 5)
        with self.assertRaisesRegex(ValueError, "unknown Alchemy"):
            native_cost("alchemy", "eth_unverifiedMethod")

    def test_helius_history_full_and_signatures_contracts(self):
        for limit, units in ((1, 10), (100, 10), (101, 20), (300, 30), (1000, 100)):
            params = ["wallet", {"limit": limit, "transactionDetails": "full"}]
            self.assertEqual(native_cost("helius", "getTransactionsForAddress", params, None), units)
            self.assertEqual(native_cost("helius", "getTransactionsForAddress", params,
                                         {"data": []}), 10)
        params = ["wallet", {"limit": 1000, "transactionDetails": "signatures"}]
        self.assertEqual(native_cost("helius", "getTransactionsForAddress", params), 10)
        self.assertEqual(native_cost("helius", "getTransactionsForAddress", result={"data": [None] * 250}), 30)
        for invalid in (0, -1, 1001, True, "100"):
            with self.assertRaises(ValueError):
                native_cost("helius", "getTransactionsForAddress", ["w", {"limit": invalid}])
        with self.assertRaises(ValueError):
            native_cost("helius", "getTransactionsForAddress", ["w", {"transactionDetails": "none"}])
        for method, units in (("getTransaction", 1), ("getSignaturesForAddress", 1),
                              ("getProgramAccounts", 10), ("getAsset", 10)):
            self.assertEqual(native_cost("helius", method), units)

    def test_chainstack_archive_upper_bound_is_not_rps_weight(self):
        for method in ("getTransaction", "getSignaturesForAddress", "getFirstAvailableBlock",
                       "getSignatureStatuses"):
            self.assertEqual(native_cost("chainstack", method), 2)
            self.assertEqual(throughput_cost("chainstack", method), 1)
        self.assertEqual(native_cost("chainstack", "getMultipleAccounts", [["a"] * 100]), 1)

    def test_requested_cost_ignores_legacy_provider_estimate(self):
        provider = SimpleNamespace(credit_model="chainstack", credit_cost=lambda *args: 1)
        self.assertEqual(request_reservation(provider, "getTransaction", []), 2)

    def test_shared_solana_evm_allowance_survives_checkpoint(self):
        state = {}
        config = {"rpc_monthly_estimated_limits": {"alchemy": 100}}
        router = SimpleNamespace(providers={"alchemy": SimpleNamespace(), "helius": SimpleNamespace()})
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        configure_monthly_budgets(router, state, config, now=now)
        self.assertEqual(router.providers["helius"].monthly_budget.limit, 900_000)
        self.assertTrue(router.providers["alchemy"].monthly_budget.reserve(40))
        evm = monthly_budget_for(state, "alchemy", config, now=now)
        self.assertTrue(evm.reserve(60))
        self.assertFalse(evm.reserve(1))
        restored = decode_checkpoint(build_checkpoint(state))
        resumed = monthly_budget_for(restored, "alchemy", config, now=now)
        self.assertEqual(resumed.remaining, 0)
        self.assertEqual(resumed.entry["attempts"], 2)
        self.assertEqual(list(restored["rpc_monthly_usage"]["2026-10"]), ["alchemy"])

    def test_utc_rollover_in_existing_budget_and_clock_rollback(self):
        now = [datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc)]
        budget = MonthlyRpcBudget({}, "alchemy", 40, clock=lambda: now[0])
        self.assertTrue(budget.reserve(40))
        now[0] += timedelta(seconds=1)
        self.assertEqual(budget.period, "2026-11")
        self.assertTrue(budget.reserve(40))
        now[0] -= timedelta(seconds=1)
        self.assertFalse(budget.reserve(1))
        local = datetime(2026, 11, 1, 0, 30, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(MonthlyRpcBudget({}, "alchemy", 40, now=local).period, "2026-10")

    def test_zero_limit_and_invalid_units_fail_closed(self):
        budget = MonthlyRpcBudget({}, "alchemy", 0)
        self.assertFalse(budget.reserve(1))
        self.assertTrue(budget.reserve(0))  # Free probes still count as attempts.
        self.assertEqual(budget.snapshot()["attempts"], 1)
        for units in (-1, 1.5, True, "40"):
            with self.assertRaises(ValueError):
                budget.reserve(units)
        ledger = {budget.period: {"alchemy": {"estimated_units": -1, "attempts": 0}}}
        with self.assertRaises(ValueError):
            MonthlyRpcBudget(ledger, "alchemy", 40).reserve(1)

    def test_durable_preallocated_roles_cannot_exceed_shared_ceiling(self):
        now = [datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc)]
        account = MonthlyRpcBudget({}, "alchemy", 100, clock=lambda: now[0])
        sol = allocate_run_budget(account, 60, "solana")
        evm = allocate_run_budget(account, 40, "robinhood")
        self.assertIsNone(allocate_run_budget(account, 1, "robinhood"))
        grant = PreallocatedRpcBudget(json.loads(json.dumps(evm)), clock=lambda: now[0])
        self.assertTrue(grant.reserve(20))
        self.assertTrue(grant.reserve(20))
        self.assertFalse(grant.reserve(1))
        self.assertEqual(account.entry["estimated_units"], 100)
        self.assertEqual(account.entry["allocated_units"], 100)
        self.assertEqual(account.entry["attempts"], 0)
        # Even a crashed worker which did not consume its grant leaves it charged.
        self.assertEqual(sol["limit"], 60)
        now[0] += timedelta(seconds=1)
        self.assertFalse(grant.reserve(0))
        self.assertEqual(grant.remaining, 0)

    def test_grant_and_snapshot_read_period_once_at_boundary(self):
        before = datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc)
        after = before + timedelta(seconds=1)
        ticks = iter([before, after])
        budget = MonthlyRpcBudget({}, "alchemy", 100, clock=lambda: next(ticks))
        grant = allocate_run_budget(budget, 60, "robinhood")
        self.assertEqual(grant["period"], "2026-10")
        self.assertEqual(budget.ledger["2026-10"]["alchemy"]["estimated_units"], 60)
        self.assertEqual(budget.snapshot()["period"], "2026-11")


if __name__ == "__main__":
    unittest.main()
