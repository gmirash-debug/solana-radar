import copy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import scanner
from rpc_budget import DurableChunkBudget, MonthlyRpcBudget, configure_monthly_budgets


class DurableRpcBudgetTests(unittest.TestCase):
    def test_real_provider_call_has_a_confirmed_grant_before_network_send(self):
        provider = scanner.HeliusRpc("https://rpc.invalid", max_retries=0)
        saved = []
        provider.monthly_budget = DurableChunkBudget(MonthlyRpcBudget({}, "helius", 100),
            lambda: (saved.append(True) or True), 50)
        response = Mock(status_code=200, text='{"result":"ok"}', headers={}, json=Mock(return_value={"result": "ok"}))
        def sent(*args, **kwargs):
            self.assertEqual(saved, [True])
            return response
        provider.session.post = Mock(side_effect=sent)
        self.assertEqual(provider.call("getHealth"), "ok")
        self.assertEqual(provider.monthly_budget.snapshot()["estimated_units"], 50)
    def test_small_grant_is_saved_before_use_and_lost_process_never_gets_it_back(self):
        ledger, durable = {}, []
        now = datetime(2026, 10, 4, tzinfo=timezone.utc)
        account = MonthlyRpcBudget(ledger, "alchemy", 100, now=now)
        commit = lambda: (durable.append(copy.deepcopy(ledger)) or True)
        budget = DurableChunkBudget(account, commit, 50)
        self.assertTrue(budget.reserve(20))
        self.assertEqual(durable[0]["2026-10"]["alchemy"]["estimated_units"], 50)
        self.assertTrue(budget.reserve(20))
        self.assertEqual(len(durable), 1)
        restarted = DurableChunkBudget(MonthlyRpcBudget(copy.deepcopy(durable[-1]), "alchemy", 100, now=now), lambda: True, 50)
        self.assertTrue(restarted.reserve(40))
        self.assertFalse(restarted.reserve(20))
        self.assertEqual(restarted.account.remaining, 0)

    def test_failed_or_uncertain_ack_disables_usage_without_refund(self):
        for ack in (False, None, "true", {}, {"ok": True}):
            account = MonthlyRpcBudget({}, "helius", 100)
            commit = Mock(return_value=ack)
            budget = DurableChunkBudget(account, commit, 50)
            self.assertFalse(budget.reserve(1))
            self.assertFalse(budget.reserve(1))
            self.assertEqual(account.remaining, 50)
            self.assertEqual(budget.remaining, 0)
            commit.assert_called_once()

    def test_exception_and_month_rollover_preserve_reserved_buckets(self):
        now = [datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc)]
        account = MonthlyRpcBudget({}, "helius", 100, clock=lambda: now[0])
        commit = Mock(return_value=True)
        budget = DurableChunkBudget(account, commit, 50)
        self.assertTrue(budget.reserve(1))
        now[0] = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.assertTrue(budget.reserve(1))
        self.assertEqual(account.ledger["2026-10"]["helius"]["estimated_units"], 50)
        self.assertEqual(account.ledger["2026-11"]["helius"]["estimated_units"], 50)
        self.assertEqual(commit.call_count, 2)
        failed = DurableChunkBudget(account, Mock(side_effect=RuntimeError("private")), 50)
        self.assertFalse(failed.reserve(1))

    def test_restore_merges_monotonic_remote_usage_instead_of_resetting_cache(self):
        state = {"rpc_monthly_usage": {"2026-10": {"alchemy": {"estimated_units": 100, "attempts": 2}}}}
        body = {"ok": True, "document": {"revision": 3, "value": {"version": 1, "ledger": {
            "2026-10": {"alchemy": {"estimated_units": 50, "attempts": 4}}}}}}
        config = {}
        with patch.object(scanner, "remote_api_call", return_value=body):
            self.assertTrue(scanner.load_durable_rpc_ledger(state, config))
        self.assertEqual(state["rpc_monthly_usage"]["2026-10"]["alchemy"]["estimated_units"], 100)
        self.assertEqual(state["rpc_monthly_usage"]["2026-10"]["alchemy"]["attempts"], 4)
        self.assertEqual(config["_rpc_ledger_revision"], 3)

    def test_remote_restore_failure_disables_paid_routes_but_keeps_public_route(self):
        paid, public = SimpleNamespace(monthly_budget=None), SimpleNamespace(monthly_budget=None)
        rpc, state = SimpleNamespace(providers={"alchemy": paid, "publicnode": public}), {}
        configure_monthly_budgets(rpc, state, {})
        with patch.object(scanner, "remote_data_url_from_env", return_value="https://worker.invalid"), \
                patch.object(scanner, "remote_ingest_secret", return_value="private"), \
                patch.object(scanner, "load_durable_rpc_ledger", side_effect=RuntimeError("private")):
            scanner.configure_durable_rpc_budgets(rpc, state, {})
        self.assertFalse(paid.monthly_budget.reserve(1))
        self.assertIsNone(public.monthly_budget)

    def test_commit_requires_explicit_acceptance_not_stale_or_local_only_ack(self):
        state = {"rpc_monthly_usage": {}}
        for ack in ({"ok": True, "accepted": False}, {"status": "local_only"}, {"ok": True, "accepted": True}):
            with patch.object(scanner, "remote_api_call", return_value=ack):
                self.assertEqual(scanner.commit_durable_rpc_ledger(state, {}), ack.get("accepted") is True)
