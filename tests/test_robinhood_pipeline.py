import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from eth_abi import encode
from eth_utils import keccak
import robinhood as rh
from robinhood_store import Store
from tests.test_robinhood import TOKEN, QUOTE, POOL, WALLET, ROUTER, fixture, topic


class PipelineTests(unittest.TestCase):
    def test_cohort_freezes_then_holds_then_weakens_without_new_buyers_replacing_it(self):
        store = Store()
        p = {"pool": POOL, "token": TOKEN, "quote": QUOTE, "key": rh.token_key(TOKEN), "token_created_at": "1969-12-30T00:00:00Z"}
        swaps, receipts = [], {}
        buyers = [WALLET, "0x" + "6" * 40, "0x" + "7" * 40]
        for n, wallet in enumerate(buyers):
            r = fixture()
            r.update({"from": wallet, "transactionHash": f"tx{n}", "blockHash": f"hash{100+n}", "blockNumber": hex(100+n)})
            r["logs"][0]["topics"][2] = topic(wallet)
            r["logs"][1]["topics"][2] = topic(wallet)
            log = dict(r["logs"][0], transactionHash=f"tx{n}", blockHash=f"hash{100+n}", blockNumber=hex(100+n), logIndex="0x1")
            swaps.append(log)
            receipts[f"tx{n}"] = r
        rpc = Mock()
        rpc.call.side_effect = lambda m, args: receipts[args[0]] if m == "eth_getTransactionReceipt" else {"hash": "hash" + str(int(args[0], 16))}
        balance = [100]
        rpc.contract.side_effect = lambda target, sig, *args, **kwargs: ((6,) if sig == "decimals()" else (10000,) if sig == "totalSupply()" else (balance[0],))
        with patch.object(rh, "verify_pool", return_value=0), patch.object(rh, "security_check", return_value={"status": "no_flags", "flags": []}), patch.object(rh, "position_check", return_value={"status":"pending"}):
            with patch.object(rh, "get_logs", side_effect=[swaps, []]):
                first = rh.inspect_incremental(rpc, p, 100, 200, rh.CONFIG, store, Mock(), 1000)
            self.assertEqual(first["status"], "buy_wave")
            self.assertEqual(first["cohort_checks"], 1)
            with patch.object(rh, "get_logs", side_effect=[[], []]) as logs:
                second = rh.inspect_incremental(rpc, p, 150, 300, rh.CONFIG, store, Mock(), 3000)
                self.assertEqual(logs.call_args_list[0].args[2], 201)
            self.assertEqual(second["status"], "retained")
            self.assertEqual(second["cohort_checks"], 2)
            outgoing = [{"topics": [rh.TRANSFER, topic(w), topic(ROUTER)], "data": hex(80)} for w in buyers]
            balance[0] = 20
            with patch.object(rh, "get_logs", side_effect=[[], outgoing]):
                third = rh.inspect_incremental(rpc, p, 250, 400, rh.CONFIG, store, Mock(), 5000)
            self.assertEqual(third["status"], "reduced")
            self.assertAlmostEqual(third["retained_supply_lower_bound_pct"], 0.6)
            self.assertEqual([w["address"] for w in third["wallets"]], buyers)

    def test_reorg_does_not_change_original_cohort(self):
        store = Store()
        p = {"pool": POOL, "token": TOKEN, "quote": QUOTE, "key": rh.token_key(TOKEN)}
        store.put("pool:" + POOL, dict(p, base_index=0))
        store.put("cursor:" + POOL, {"block": 100, "hash": "old", "from_block": 1})
        store.put("cohort:" + p["key"], {"checks": 1})
        rpc = Mock()
        rpc.call.return_value = {"hash": "replaced"}
        with self.assertRaisesRegex(rh.RpcError, "reorganized"):
            rh.inspect_incremental(rpc, p, 90, 200, rh.CONFIG, store, Mock(), 0)
        self.assertEqual(store.get("cohort:" + p["key"]), {"checks": 1})

    def test_json_rpc_throttle_retries_even_with_http_200(self):
        session = Mock()
        response = Mock(status_code=200)
        response.json.side_effect = [{"error": {"code": 429, "message": "Too Many Requests"}}, {"result": "0x1237"}]
        session.post.return_value = response
        rpc = rh.Rpc(session=session, budget=3)
        with patch.object(rh.time, "sleep"):
            self.assertEqual(rpc.request(rh.PUBLIC_RPC, "eth_chainId", []), "0x1237")
        self.assertEqual(rpc.calls, 2)

    def test_busy_log_provider_retries_without_leaking_error_details(self):
        response = Mock(status_code=200, headers={"Retry-After": "12"})
        response.json.side_effect = [{"error": {"code": -32005, "message": "the network is busy, please try again"}}, {"result": []}]
        session = Mock()
        session.post.return_value = response
        rpc = rh.Rpc(session=session, budget=3)
        with patch.object(rh.time, "sleep") as sleep:
            self.assertEqual(rpc.request(rh.ORDO_RPC, "eth_getLogs", []), [])
        self.assertGreater(sleep.call_args_list[1].args[0], 11)
        response.json.side_effect = None
        response.json.return_value = {"error": {"code": -32602, "message": "invalid key secret-do-not-publish"}}
        with patch.object(rh.time, "sleep"), self.assertRaises(rh.RpcError) as error:
            rpc.request(rh.ORDO_RPC, "eth_getLogs", [])
        self.assertNotIn("secret", str(error.exception))
        self.assertIn("-32602", str(error.exception))

    def test_backoff_does_not_exceed_deadline(self):
        rpc = rh.Rpc(session=Mock())
        rpc.deadline = rh.time.monotonic() + 1
        rpc.backoff(rh.PUBLIC_RPC, 0, {"Retry-After": "30"})
        with self.assertRaisesRegex(rh.RpcError, "time budget"):
            rpc.request(rh.PUBLIC_RPC, "eth_getLogs", [])
        self.assertEqual(rpc.calls, 0)

    def test_routed_buy_requires_full_custody_and_beneficiary_net(self):
        self.assertEqual(rh.routed_buy(fixture(ROUTER), {"pool": POOL, "token": TOKEN}, 0), (WALLET, 100))
        r = fixture(received=99)
        self.assertIsNone(rh.routed_buy(r, {"pool": POOL, "token": TOKEN}, 0))
        r = fixture()
        r["logs"] = r["logs"][1:]
        self.assertIsNone(rh.routed_buy(r, {"pool": POOL, "token": TOKEN}, 0))

    def test_v4_signs_and_beneficiary(self):
        pool_id = "0x" + "a" * 64
        r = fixture()
        r["logs"][0] = {"address": rh.POOL_MANAGER, "topics": [rh.SWAP_V4, pool_id, topic(ROUTER)],
            "data": "0x" + encode(["int128", "int128", "uint160", "uint128", "int24", "uint24"], [100, -5, 1, 1, 0, 3000]).hex()}
        r["logs"][1]["topics"][1] = topic(rh.POOL_MANAGER)
        p = {"protocol": "v4", "pool": pool_id, "token": TOKEN}
        self.assertEqual(rh.swap_amounts(r["logs"][0]), (-100, 5))
        self.assertEqual(rh.routed_buy(r, p, 0), (WALLET, 100))
        r["logs"][0]["topics"][1] = "0x" + "b" * 64
        self.assertIsNone(rh.routed_buy(r, p, 0))

    def test_v4_poolkey_verified_not_just_provider_label(self):
        key = [TOKEN, QUOTE, 3000, 60, rh.ZERO]
        pool_id = "0x" + keccak(encode(["address", "address", "uint24", "int24", "address"], key)).hex()
        event = {"address": rh.POOL_MANAGER, "blockNumber": "0xf", "transactionHash": "tx1", "logIndex": "0x0", "topics": [rh.INITIALIZE, pool_id, topic(TOKEN), topic(QUOTE)],
            "data": "0x" + encode(["uint24", "int24", "address", "uint160", "int24"], key[2:] + [1, 0]).hex()}
        rpc = Mock()
        rpc.call.side_effect = lambda method, args: {"timestamp": "0x7d0"} if method == "eth_getBlockByNumber" else [event]
        p = {"pool": pool_id, "token": TOKEN, "quote": QUOTE, "protocol": "v4", "pool_created_at": "1970-01-01T00:16:40Z"}
        with patch.object(rh, "window_start", side_effect=[(10, {}), (20, {})]):
            self.assertEqual(rh.verify_pool(rpc, p, "0x20"), 0)
        query = [call.args[1][0] for call in rpc.call.call_args_list if call.args[0] == "eth_getLogs"][0]
        self.assertEqual((query["fromBlock"], query["toBlock"]), ("0xa", "0x14"))
        self.assertEqual(p["hooks"], rh.ZERO)
        event["data"] = "0x" + encode(["uint24", "int24", "address", "uint160", "int24"], [4000, 60, rh.ZERO, 1, 0]).hex()
        with patch.object(rh, "window_start", side_effect=[(10, {}), (20, {})]), self.assertRaisesRegex(rh.RpcError, "identity"):
            rh.verify_pool(rpc, p, "0x10")

    def test_archive_failure_is_not_young_token(self):
        rpc = Mock()
        rpc.call.side_effect = rh.RpcError("Archive unavailable")
        with self.assertRaises(rh.RpcError):
            rh.token_age(rpc, TOKEN, 100, 20, 80, Store())

    def test_real_age_and_cached_creation(self):
        rpc = Mock()
        rpc.call.side_effect = lambda m, p: ("0x6000" if int(p[1], 16) >= 50 else "0x") if m == "eth_getCode" else {"timestamp": "0x64"}
        store = Store()
        result = rh.token_age(rpc, TOKEN, 100, 20, 80, store)
        self.assertEqual(result["token_creation_block"], 50)
        rpc.call.reset_mock()
        self.assertEqual(rh.token_age(rpc, TOKEN, 101, 21, 81, store), result)
        rpc.call.assert_not_called()

    def test_new_pool_does_not_make_old_token_young(self):
        rpc = Mock()
        rpc.call.return_value = "0x6000"
        self.assertEqual(rh.token_age(rpc, TOKEN, 100, 20, 80, Store())["age_status"], "too_old")

    def test_stock_registry_is_address_and_chain_scoped(self):
        session = Mock()
        session.get.return_value.json.return_value = {"assets": [{"tokenSymbol": "MEME", "deployments": [
            {"chainId": 4663, "contractAddress": TOKEN}, {"chainId": 1, "contractAddress": WALLET}]}]}
        self.assertEqual(rh.stock_registry(session, Store()), {TOKEN})

    def test_missing_tax_not_zero(self):
        session = Mock()
        session.get.return_value.json.return_value = {"code": 1, "result": {TOKEN: {"is_honeypot": "0", "buy_tax": "", "sell_tax": ""}}}
        result = rh.security_check(session, TOKEN, Store())
        self.assertEqual(result["status"], "unknown")
        self.assertIn("sell_tax", result["unknown"])
        self.assertIsNone(result["buy_tax"])

    def test_missing_risk_response_not_safe(self):
        session = Mock()
        session.get.return_value.json.return_value = {"code": 1, "result": {}}
        self.assertEqual(rh.security_check(session, TOKEN, Store())["status"], "unknown")

    def test_capped_single_block_fails(self):
        rpc = Mock()
        rpc.call.return_value = [{}] * 1000
        with self.assertRaisesRegex(rh.RpcError, "truncated"):
            rh.get_logs(rpc, {"address": POOL}, 10, 10)

    def test_wrong_log_cannot_advance_history(self):
        rpc = Mock()
        rpc.call.return_value = [{"address": TOKEN, "blockNumber": "0x1"}]
        with self.assertRaisesRegex(rh.RpcError, "range"):
            rh.get_logs(rpc, {"address": POOL}, 1, 100)

    def test_database_rollback_and_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = d + "/state.sqlite"
            store = Store(path)
            store.put("cohort:x", {"checks": 1})
            store.finish()
            store.put("cohort:x", {"checks": 2})
            store.finish(False)
            store.close()
            reopened = Store(path)
            self.assertEqual(reopened.get("cohort:x"), {"checks": 1})
            reopened.close()

    def test_outflows_reduce_lower_bound_inflows_do_not_restore_it(self):
        rpc = Mock()
        rpc.contract.return_value = (1000,)
        cohort = {"checks": 1, "balance_block": 100, "last_check_timestamp": 1000, "wallets": [{"address": WALLET,
            "bought_raw": "100", "balance_raw": "100", "retained_lower_bound_raw": "100"}]}
        transfer = {"topics": [rh.TRANSFER, topic(WALLET), topic(ROUTER)], "data": hex(80)}
        with patch.object(rh, "get_logs", return_value=[transfer]):
            checked = rh.recheck_cohort(rpc, {"token": TOKEN}, cohort, 200, 10000, 3000, rh.CONFIG)
        self.assertEqual(checked["wallets"][0]["retained_lower_bound_raw"], "20")
        self.assertEqual(checked["wallets"][0]["retained_upper_bound_raw"], "100")
        self.assertEqual(checked["checks"], 2)
        self.assertEqual(cohort["wallets"][0]["retained_lower_bound_raw"], "100")

    def test_repeated_same_block_is_not_another_confirmation(self):
        rpc = Mock()
        cohort = {"balance_block": 100, "checks": 1}
        self.assertEqual(rh.recheck_cohort(rpc, {}, cohort, 100, 10000, 9000, rh.CONFIG), cohort)
        rpc.contract.assert_not_called()

    def test_rpc_wrong_chain_disabled_before_data_queries(self):
        rpc = rh.Rpc(url="https://configured.test", budget=20)
        rpc.routes = ["https://configured.test", rh.PUBLIC_RPC]
        def request(endpoint, method, params):
            if method == "eth_chainId":
                return "0x1" if endpoint == "https://configured.test" else "0x1237"
            self.assertEqual(endpoint, rh.PUBLIC_RPC)
            return "0x6000"
        with patch.object(rpc, "request", side_effect=request):
            self.assertEqual(rpc.call("eth_getCode", [TOKEN, "0x1"]), "0x6000")
        self.assertIn("https://configured.test", rpc.disabled)

    def test_rpc_fallback_and_budget_include_failed_attempts(self):
        session = Mock()
        failed = Mock(status_code=403)
        failed.raise_for_status.side_effect = rh.requests.HTTPError("secret URL must not escape")
        good = Mock(status_code=200)
        good.json.return_value = {"result": "0x1237"}
        session.post.side_effect = [failed, good]
        rpc = rh.Rpc(budget=2, session=session)
        with patch.object(rh.time, "sleep"):
            self.assertEqual(rpc.call("eth_chainId", []), "0x1237")
        self.assertEqual(rpc.calls, 2)
        with self.assertRaisesRegex(rh.RpcError, "budget"):
            rpc.call("eth_chainId", [])


if __name__ == "__main__":
    unittest.main()
