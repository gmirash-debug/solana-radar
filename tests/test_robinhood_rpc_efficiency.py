import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import robinhood as rh
from robinhood_store import Store
from rpc_budget import MonthlyRpcBudget, configure_monthly_budgets
from rpc_limiter import NativeUnitLimiter, ProviderLimiters
from rpc_routing import evm_endpoint_provider, evm_route_supports
from runtime_checkpoint import build_checkpoint, decode_checkpoint


ALCHEMY = "https://robinhood-mainnet.g.alchemy.com/v2/do-not-export-secret"


def response(result="0x1237", status=200, body=None):
    result_body = body if body is not None else {"result": result}
    return Mock(status_code=status, headers={}, json=Mock(return_value=result_body))


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class RobinhoodRpcEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.limiters = ProviderLimiters(clock=self.clock, sleep=self.clock.sleep)
        time_patch = patch.object(rh.time, "monotonic", self.clock)
        time_patch.start()
        self.addCleanup(time_patch.stop)
        sleep_patch = patch.object(rh.time, "sleep", self.clock.sleep)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def rpc(self, units=100, **kwargs):
        budget = MonthlyRpcBudget({}, "alchemy", units)
        return rh.Rpc(url=ALCHEMY, session=Mock(), monthly_budget=budget,
                      limiters=self.limiters, **kwargs)

    def test_chain_probes_have_zero_billing_but_count_attempts(self):
        rpc = self.rpc()
        rpc.session.post.return_value = response()
        rpc.request(ALCHEMY, "eth_chainId", [])
        rpc.request(ALCHEMY, "eth_chainId", [])
        self.assertEqual(rpc.monthly_budget.remaining, 100)
        self.assertEqual(rpc.monthly_budget.entry["attempts"], 2)
        self.assertAlmostEqual(self.clock.now, 100 + 5 / 300)

    def test_retries_share_solana_monthly_allowance_and_stop_before_send(self):
        state = {}
        config = {"rpc_monthly_estimated_limits": {"alchemy": 80}}
        provider = SimpleNamespace()
        configure_monthly_budgets(SimpleNamespace(providers={"alchemy": provider}), state, config)
        self.assertTrue(provider.monthly_budget.reserve(40))
        rpc = rh.Rpc(url=ALCHEMY, session=Mock(), shared_state=state,
                     budget_config=config, limiters=self.limiters)
        rpc.session.post.return_value = response(status=503)
        with self.assertRaisesRegex(rh.RpcError, "monthly budget exhausted"):
            rpc.request(ALCHEMY, "eth_getTransactionReceipt", ["tx"])
        self.assertEqual(rpc.calls, 2)
        self.assertEqual(rpc.session.post.call_count, 2)
        restored = decode_checkpoint(build_checkpoint(state))
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        self.assertEqual(restored["rpc_monthly_usage"][period]["alchemy"]["estimated_units"], 80)
        self.assertEqual(restored["rpc_monthly_usage"][period]["alchemy"]["attempts"], 3)

    def test_network_retries_and_bad_responses_are_charged_without_secret(self):
        rpc = self.rpc(units=60)
        rpc.session.post.side_effect = [rh.requests.Timeout(ALCHEMY),
                                        response(status=500), response("0x1")]
        self.assertEqual(rpc.request(ALCHEMY, "eth_getTransactionReceipt", ["tx"]), "0x1")
        self.assertEqual(rpc.monthly_budget.remaining, 0)
        self.assertEqual(rpc.monthly_budget.entry["attempts"], 3)
        rpc = self.rpc(units=20)
        rpc.session.post.return_value = response(body={"status": "missing result"})
        with self.assertRaises(rh.RpcError) as exc:
            rpc.request(ALCHEMY, "eth_getCode", ["wallet", "latest"])
        self.assertNotIn("do-not-export", str(exc.exception))
        self.assertEqual(rpc.monthly_budget.remaining, 0)

    def test_unknown_eth_cost_and_missing_ledger_never_send_to_alchemy(self):
        rpc = self.rpc()
        with self.assertRaisesRegex(rh.RpcError, "Unknown Alchemy"):
            rpc.request(ALCHEMY, "eth_unpriced", [])
        rpc.session.post.assert_not_called()
        unconfigured = rh.Rpc(url=ALCHEMY, session=Mock(), limiters=self.limiters)
        with self.assertRaisesRegex(rh.RpcError, "budget unavailable"):
            unconfigured.request(ALCHEMY, "eth_chainId", [])
        unconfigured.session.post.assert_not_called()

    def test_public_fallback_needs_no_ledger_and_learns_method_capability(self):
        rpc = rh.Rpc(url=ALCHEMY, session=Mock(), limiters=self.limiters)
        rpc.head = 1000
        rpc.session.post.side_effect = [response(), response(body={
            "error": {"code": -32601, "message": "unsupported secret URL"}}),
            response(), response("0x10"), response("0x11")]
        self.assertEqual(rpc.call("eth_getCode", ["wallet", "0x1"]), "0x10")
        self.assertEqual(rpc.call("eth_getCode", ["wallet", "0x1"]), "0x11")
        endpoints = [call.args[0] for call in rpc.session.post.call_args_list]
        self.assertNotIn(ALCHEMY, endpoints)
        self.assertEqual(endpoints.count(rh.PUBLIC_NODE), 2)
        self.assertNotIn("secret", json.dumps(rpc.last_errors))

    def test_deadline_blocked_attempt_does_not_consume_monthly_units(self):
        rpc = self.rpc()
        rpc.session.post.return_value = response("0x1")
        rpc.request(ALCHEMY, "eth_call", [{}, "latest"])
        rpc.deadline = self.clock.now + 0.01
        with self.assertRaisesRegex(rh.RpcError, "time budget"):
            rpc.request(ALCHEMY, "eth_getTransactionReceipt", ["tx"])
        self.assertEqual(rpc.calls, 1)
        self.assertEqual(rpc.monthly_budget.remaining, 74)

    def test_alchemy_free_log_range_and_host_classification(self):
        self.assertEqual(evm_endpoint_provider(ALCHEMY), "alchemy")
        self.assertEqual(evm_endpoint_provider("https://alchemy.com.evil.test/key"), "configured")
        for end, expected in ((10, True), (11, False), (0, False)):
            self.assertEqual(evm_route_supports("alchemy", "eth_getLogs", [{
                "fromBlock": "0x1", "toBlock": hex(end)}]), expected)
        self.assertFalse(evm_route_supports("publicnode", "eth_getLogs", [{}]))


class RobinhoodCliBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.state = {"pools": {"keep": {"cursor": 99}}, "rpc_monthly_usage": {}}
        self.path.write_text(json.dumps(self.state))
        self.persisted = []

        def save(state, config, writer):
            self.persisted.append((writer, copy.deepcopy(state)))
            self.path.write_text(json.dumps(state))
            config["_runtime_deep_saved"] = {"ok": True, "accepted": True}

        self.runtime = SimpleNamespace(STATE_PATH=self.path, load_env=Mock(),
            load_runtime_checkpoint=Mock(side_effect=lambda state, config, kind: state),
            save_runtime_state=Mock(side_effect=save))
        self.config = {"robinhood_rpc_native_units_per_scan": 60,
                       "rpc_monthly_estimated_limits": {"alchemy": 100}}
        self.store = Store()
        self.addCleanup(self.store.close)
        env = patch.dict(rh.os.environ, {"ROBINHOOD_RPC_URL": ALCHEMY}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, effect=None):
        with patch.object(rh, "scan", side_effect=effect or (lambda *args, **kw: {
                "status": "unavailable", "tokens": []})):
            return rh.scan_with_shared_ledger({}, self.store, self.config, self.runtime)

    def test_preallocation_is_durable_before_scan_and_remains_on_failure(self):
        def failed(previous, rpc, store):
            durable = json.loads(self.path.read_text())
            entry = next(iter(durable["rpc_monthly_usage"].values()))["alchemy"]
            self.assertEqual(entry["estimated_units"], 60)
            self.assertTrue(rpc.monthly_budget.reserve(20))
            self.assertFalse(rpc.monthly_budget.reserve(41))
            return {"status": "unavailable", "tokens": []}

        output = self.run_cli(failed)
        self.assertEqual(output["alchemy_account_monthly_usage"]["estimated_units"], 60)
        self.assertEqual([name for name, state in self.persisted],
                         ["robinhood_rpc_preallocation", "robinhood_rpc_finished"])
        self.assertEqual(json.loads(self.path.read_text())["pools"], self.state["pools"])
        output = self.run_cli()
        self.assertEqual(output["alchemy_account_monthly_usage"]["estimated_units"], 100)

    def test_unhandled_error_finally_checkpoints_without_refund(self):
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            self.run_cli(lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated crash")))
        entry = next(iter(json.loads(self.path.read_text())["rpc_monthly_usage"].values()))["alchemy"]
        self.assertEqual(entry["estimated_units"], 60)
        self.assertEqual(len(self.persisted), 2)

    def test_failed_preallocation_checkpoint_disables_paid_provider(self):
        def save(state, config, writer):
            self.path.write_text(json.dumps(state))
            config["_runtime_deep_saved"] = {"ok": False, "error": "not exported secret"}

        self.runtime.save_runtime_state.side_effect = save

        def check(previous, rpc, store):
            self.assertIsNone(rpc.monthly_budget)
            return {"status": "unavailable", "tokens": []}

        output = self.run_cli(check)
        self.assertIn("checkpoint_failed", output["rpc_ledger_status"])
        self.assertNotIn("secret", json.dumps(output))

    def test_missing_or_corrupt_state_cannot_reset_allowance(self):
        def check(previous, rpc, store):
            self.assertIsNone(rpc.monthly_budget)
            return {"status": "unavailable", "tokens": []}

        self.path.unlink()
        self.run_cli(check)
        self.path.write_text("{broken")
        self.run_cli(check)
        self.runtime.save_runtime_state.assert_not_called()

    def test_remote_ack_must_explicitly_accept_preallocation(self):
        for ack in ({}, {"status": "local_only"}, {"ok": True},
                    {"ok": True, "accepted": False}, {"ok": False, "accepted": True},
                    {"ok": True, "accepted": "true"}):
            with self.subTest(ack=ack):
                self.path.write_text(json.dumps(self.state))
                config = copy.deepcopy(self.config)
                config.pop("_runtime_deep_saved", None)

                def save(state, config, writer):
                    self.path.write_text(json.dumps(state))
                    config["_runtime_deep_saved"] = ack

                self.runtime.save_runtime_state.side_effect = save

                def check(previous, rpc, store):
                    self.assertIsNone(rpc.monthly_budget)
                    return {"status": "unavailable", "tokens": []}

                with patch.object(rh, "scan", side_effect=check):
                    result = rh.scan_with_shared_ledger({}, self.store, config, self.runtime)
                self.assertIn("checkpoint_failed", result["rpc_ledger_status"])

    def test_cli_main_calls_durable_bridge_not_unconfigured_scan(self):
        self.store.close()
        self.store = Store()
        self.addCleanup(self.store.close)
        output = Path(self.temp.name) / "robinhood.json"
        with patch.object(rh, "scan_with_shared_ledger", return_value={"status": "unavailable"}) as bridge, \
                patch.object(rh, "Store", return_value=self.store), \
                patch.object(rh, "scan") as scan, \
                patch("sys.argv", ["robinhood.py", "--output", str(output), "--db", str(self.path)]):
            rh.main()
        bridge.assert_called_once()
        scan.assert_not_called()
        self.assertEqual(json.loads(output.read_text())["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
