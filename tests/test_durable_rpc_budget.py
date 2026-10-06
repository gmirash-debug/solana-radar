import copy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import scanner
from rpc_budget import DurableChunkBudget, MonthlyRpcBudget, configure_monthly_budgets


class DurableRpcBudgetTests(unittest.TestCase):
    def test_lost_commit_reply_only_recovers_exact_unique_persisted_grant(self):
        saved = {}
        config = {}
        state = {"rpc_monthly_usage": {"2026-10": {"helius": {"estimated_units": 250}}}}
        def remote(method, path, cfg, payload=None):
            if method == "POST":
                saved.update(copy.deepcopy(payload))
                raise scanner.requests.Timeout("lost response")
            return {"document": copy.deepcopy(saved)}
        with patch.object(scanner,"remote_api_call",side_effect=remote) as call:
            self.assertTrue(scanner.commit_durable_rpc_ledger(state,config))
        self.assertEqual([item.args[0] for item in call.call_args_list],["POST","GET"])
        self.assertTrue(saved["value"]["allocation_id"])
        self.assertEqual(config["_rpc_ledger_revision"],1)
        self.assertEqual(state["rpc_monthly_usage"]["2026-10"]["helius"]["estimated_units"],250)

    def test_another_grant_with_equal_counters_cannot_acknowledge_our_reservation(self):
        def remote(method,path,cfg,payload=None):
            if method == "POST":
                return {"ok":True,"accepted":False}
            return {"document":{"value":{"version":1,"ledger":{},"allocation_id":"another"},"revision":1}}
        config={}
        with patch.object(scanner,"remote_api_call",side_effect=remote) as call:
            self.assertFalse(scanner.commit_durable_rpc_ledger({"rpc_monthly_usage":{}},config))
        self.assertEqual(call.call_count,2)
        self.assertNotIn("_rpc_ledger_revision",config)

    def test_transient_retry_uses_same_allocation_without_recharging_or_changing_payload(self):
        writes=[]
        def remote(method,path,cfg,payload=None):
            if method == "GET":
                return {"document":{}}
            writes.append(copy.deepcopy(payload))
            if len(writes)==1:
                raise RuntimeError("Remote HTTP 503: storage_sql_timeout")
            return {"ok":True,"accepted":True,"revision":payload["revision"]}
        account=MonthlyRpcBudget({},"alchemy",10000)
        state={"rpc_monthly_usage":account.ledger}
        budget=DurableChunkBudget(account,lambda:scanner.commit_durable_rpc_ledger(state,{}),2000)
        with patch.object(scanner,"remote_api_call",side_effect=remote), patch.object(scanner.time,"sleep"):
            self.assertTrue(budget.reserve(100))
        self.assertEqual(writes[0],writes[1])
        self.assertEqual(account.snapshot()["estimated_units"],2000)
        self.assertEqual(budget.used,100)

    def test_counter_regression_and_unknown_errors_never_retry_new_writes(self):
        for message in ("Remote HTTP 500: rpc_ledger_counter_regression","invariant failure"):
            def remote(method,path,cfg,payload=None):
                if method=="POST":
                    raise RuntimeError(message)
                return {"document":{}}
            with patch.object(scanner,"remote_api_call",side_effect=remote) as call:
                self.assertFalse(scanner.commit_durable_rpc_ledger({"rpc_monthly_usage":{}},{}))
            self.assertEqual(sum(item.args[0]=="POST" for item in call.call_args_list),1)

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
