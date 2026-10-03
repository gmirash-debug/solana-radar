import copy
import unittest
from unittest.mock import Mock, patch

import scanner as s
from rpc_budget import MonthlyRpcBudget
from rpc_routing import validate_result
from tools.probe_rpc_routes import probe


def response(result=None, status=200, text="", body=None):
    value = Mock(status_code=status, ok=status < 400, text=text, reason="", headers={})
    value.json.return_value = {"result": result} if body is None else body
    return value


class RpcRoutingTests(unittest.TestCase):
    def router(self, config=None):
        with patch.dict(s.os.environ, {"HELIUS_API_KEY": "secret-key", "ALCHEMY_SOLANA_RPC_URL": "https://alchemy.invalid/v2/test-key",
                                      "CHAINSTACK_SOLANA_RPC_URL": "https://chainstack.invalid/test-key"}, clear=True):
            rpc = s.build_rpc_router(config or {})
        for provider in rpc.providers.values():
            provider.max_retries = 0
            provider.min_interval_seconds = 0
            provider.method_min_interval_seconds = {}
            provider.session.post = Mock(return_value=response())
        return rpc

    def test_actual_tasks_have_distinct_primaries(self):
        rpc = self.router()
        rpc.providers["chainstack"].session.post.side_effect = [response({"slot": 1}), response({"value": [{"amount": "1"}]}), response({"value": {"amount": "100", "decimals": 2}})]
        rpc.providers["helius"].session.post.side_effect = [response({"value": []}), response([{"signature": "sig"}])]
        rpc.providers["alchemy"].session.post.side_effect = [response({"data": []}), response({"value": []})]
        self.assertEqual(rpc.transaction("sig"), {"slot": 1})
        self.assertEqual(len(rpc.multiple_accounts(["account"])), 1)
        self.assertEqual(rpc.token_supply("mint"), 1)
        self.assertEqual(rpc.token_balance("owner", "mint"), 0)
        self.assertEqual(rpc.signatures_for_address("pool")[0]["signature"], "sig")
        self.assertEqual(rpc.transactions_for_address("pool")["_provider"], "alchemy")
        self.assertEqual(rpc.largest_token_accounts("mint"), [])
        self.assertEqual(rpc.last_provider_by_method["getTokenAccountsByOwner"], "helius")
        self.assertEqual(rpc.last_provider_by_method["getTokenLargestAccounts"], "alchemy")

    def test_method_override_is_used_by_normal_helpers(self):
        rpc = self.router({"rpc_method_provider_orders": {"getTokenAccountsByOwner": ["alchemy", "helius"]}})
        rpc.providers["alchemy"].session.post.return_value = response({"value": []})
        rpc.token_balance("owner", "mint")
        self.assertEqual(rpc.last_provider_by_method["getTokenAccountsByOwner"], "alchemy")
        rpc.providers["helius"].session.post.assert_not_called()

    def test_live_and_archive_pages_use_distinct_routes(self):
        rpc = self.router()
        for provider in rpc.providers.values():
            provider.session.post.return_value = response({"data": []})
        self.assertEqual(rpc.transactions_for_address("pool")["_provider"], "alchemy")
        self.assertEqual(rpc.transactions_for_address("pool", history_task="archive")["_provider"], "helius")
        self.assertEqual(rpc.transactions_for_address("pool", history_task="archive", provider_name="alchemy", pagination_token="pinned")["_provider"], "alchemy")

    def test_chainstack_restricted_indexed_reads_are_not_attempted(self):
        rpc = self.router()
        rpc.providers["alchemy"].session.post.return_value = response({"value": []})
        rpc.largest_token_accounts("mint")
        rpc.providers["chainstack"].session.post.assert_not_called()

    def test_null_transaction_tries_independent_archive(self):
        rpc = self.router()
        rpc.providers["chainstack"].session.post.return_value = response(None)
        rpc.providers["helius"].session.post.return_value = response({"slot": 123})
        self.assertEqual(rpc.transaction("old-signature"), {"slot": 123})
        self.assertEqual(rpc.null_transactions["chainstack"], 1)
        self.assertEqual(rpc.last_provider_by_method["getTransaction"], "helius")

    def test_all_null_is_unknown_and_does_not_disable_provider(self):
        rpc = self.router()
        self.assertIsNone(rpc.transaction("absent-signature"))
        self.assertEqual(rpc.blocked_providers, {})
        self.assertEqual(sum(rpc.null_transactions.values()), 3)

    def test_bad_balance_falls_back_without_caching_false_sale(self):
        rpc = self.router()
        rpc.providers["helius"].session.post.return_value = response({})
        rpc.providers["alchemy"].session.post.return_value = response({"value": [{"account": {"data": {"parsed": {"info": {
            "mint": "mint", "tokenAmount": {"amount": "12300", "decimals": 2}}}}}}]})
        self.assertEqual(rpc.token_balance("owner", "mint"), 123)
        self.assertEqual(rpc.token_balance_cache[("owner", "mint")], 123)
        self.assertEqual(rpc.last_provider_by_method["getTokenAccountsByOwner"], "alchemy")

    def test_all_bad_balances_remain_unavailable(self):
        rpc = self.router()
        for provider in rpc.providers.values():
            provider.session.post.return_value = response(None)
        with self.assertRaises(s.RpcProvidersUnavailable):
            rpc.token_balance("owner", "mint")
        self.assertNotIn(("owner", "mint"), rpc.token_balance_cache)

    def test_zero_balance_is_valid_only_with_real_empty_account_list(self):
        validate_result("getTokenAccountsByOwner", ["owner", {"mint": "mint"}], {"value": []})
        for data in (None, {}, {"value": None}, {"value": [{}]}):
            with self.assertRaises(ValueError):
                validate_result("getTokenAccountsByOwner", ["owner", {"mint": "mint"}], data)

    def test_bad_mint_amount_or_incomplete_batch_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_result("getMultipleAccounts", [["a", "b"]], {"value": [None]})
        with self.assertRaises(ValueError):
            validate_result("getTokenSupply", ["mint"], {"value": {"uiAmount": float("nan")}})
        with self.assertRaises(ValueError):
            validate_result("getTokenAccountsByOwner", ["owner", {"mint": "mint"}], {"value": [{"account": {"data": {"parsed": {"info": {
                "mint": "other", "tokenAmount": {"amount": "1", "decimals": 2}}}}}}]})

    def test_ui_string_balance_is_not_silently_converted_to_zero(self):
        rpc = self.router()
        rpc.providers["helius"].session.post.return_value = response({"value": [{"account": {"data": {"parsed": {"info": {
            "tokenAmount": {"uiAmountString": "12.5"}}}}}}]})
        self.assertEqual(rpc.token_balance("owner", "mint"), 12.5)
        with self.assertRaises(ValueError):
            validate_result("getTokenSupply", ["mint"], {"value": {"uiAmount": False}})

    def test_large_account_batch_is_split_without_partial_complete_cache(self):
        rpc = self.router()
        accounts = [str(i) for i in range(101)]
        rpc.providers["chainstack"].session.post.side_effect = [response({"value": [None] * 100}), response({"value": [None]})]
        self.assertEqual(len(rpc.multiple_accounts(accounts)), 101)
        self.assertEqual([len(call.kwargs["json"]["params"][0]) for call in rpc.providers["chainstack"].session.post.call_args_list], [100, 1])

    def test_missing_envelope_is_not_a_successful_zero(self):
        provider = s.HeliusRpc("secret", max_retries=0)
        provider.session.post = Mock(return_value=response(body={"status": "ok"}))
        with self.assertRaisesRegex(s.HeliusRpcError, "missing RPC result"):
            provider.call("getTokenSupply", ["mint"])

    def test_rate_limit_cools_down_method_but_other_tasks_continue(self):
        rpc = self.router()
        helius = rpc.providers["helius"]
        helius.session.post.return_value = response(status=429, text="rate limit")
        rpc.providers["alchemy"].session.post.return_value = response({"value": []})
        with patch.object(s.time, "monotonic", return_value=100):
            rpc.token_balance("a", "mint")
            self.assertFalse(helius.can_call("getTokenAccountsByOwner"))
            self.assertTrue(helius.can_call("getSignaturesForAddress"))
            rpc.token_balance("b", "mint")
            self.assertEqual(helius.session.post.call_count, 1)
        with patch.object(s.time, "monotonic", return_value=1000):
            self.assertTrue(helius.can_call("getTokenAccountsByOwner"))

    def test_retry_attempts_cannot_overrun_per_scan_budget(self):
        provider = s.HeliusRpc("secret", max_retries=2, credit_budget=1)
        provider.session.post = Mock(return_value=response(status=500, text="server error"))
        with patch.object(s.time, "sleep"), self.assertRaises(s.HeliusRpcError):
            provider.call("getSlot")
        self.assertEqual(provider.session.post.call_count, 1)
        self.assertEqual(provider.attempted_credits, 1)

    def test_expensive_history_does_not_disable_remaining_cheap_reads(self):
        rpc = self.router({"helius_rpc_credit_budget_per_scan": 5})
        helius = rpc.providers["helius"]
        self.assertFalse(helius.can_call("getTransactionsForAddress", ["pool", {"limit": 100}]))
        with self.assertRaises(s.RpcProvidersUnavailable):
            rpc.transactions_for_address("pool", provider_name="helius")
        self.assertNotIn("helius", rpc.blocked_providers)
        helius.session.post.return_value = response({"value": []})
        self.assertEqual(rpc.token_balance("owner", "mint"), 0)

    def test_monthly_reservation_uses_requested_history_page_size(self):
        provider = s.HeliusRpc("secret", credit_budget=100)
        provider.monthly_budget = MonthlyRpcBudget({}, "helius", 20)
        self.assertFalse(provider.can_call("getTransactionsForAddress", ["pool", {"limit": 300}]))
        self.assertTrue(provider.can_call("getTokenAccountsByOwner"))

    def test_cursor_is_pinned_and_not_sent_to_fallback(self):
        rpc = self.router()
        rpc.providers["alchemy"].session.post.return_value = response(status=402, text="quota exhausted")
        with self.assertRaises(s.RpcProvidersUnavailable):
            rpc.transactions_for_address("pool", pagination_token="alchemy-only", provider_name="alchemy")
        rpc.providers["helius"].session.post.assert_not_called()
        with self.assertRaisesRegex(ValueError, "originating provider"):
            rpc.transactions_for_address("pool", pagination_token="unowned")

    def test_unsupported_method_persists_expires_and_resets_with_endpoint(self):
        state = {}
        rpc = self.router()
        rpc.configure_capabilities(state, {})
        rpc._record_provider_error("helius", "getTransactionsForAddress", s.HeliusRpcError("getTransactionsForAddress", "unsupported", "method not found"))
        saved = copy.deepcopy(state)
        next_rpc = self.router()
        next_rpc.configure_capabilities(saved, {})
        self.assertNotIn("helius", list(next_rpc._eligible_names(next_rpc.enhanced_order, "getTransactionsForAddress")))
        rotated = self.router()
        rotated.providers["helius"].url += "rotated"
        rotated.configure_capabilities(copy.deepcopy(state), {})
        self.assertIn("helius", list(rotated._eligible_names(rotated.enhanced_order, "getTransactionsForAddress")))
        expired = self.router()
        with patch.object(s.time, "time", return_value=s.time.time() + 90000):
            expired.configure_capabilities(copy.deepcopy(state), {})
        self.assertIn("helius", list(expired._eligible_names(expired.enhanced_order, "getTransactionsForAddress")))

    def test_secret_not_in_provider_diagnostics(self):
        provider = s.HeliusRpc("sensitive-secret-key", max_retries=0)
        provider.session.post = Mock(return_value=response(status=401, text="Invalid API key sensitive-secret-key"))
        with self.assertRaises(s.HeliusRpcError) as raised:
            provider.call("getSlot")
        self.assertNotIn("sensitive-secret-key", str(raised.exception))

    def test_gmgn_repeated_local_sorts_make_one_server_request(self):
        config = {"mcap_min_usd": 0, "mcap_max_usd": 5000000, "liquidity_min_usd": 3000,
                  "gmgn_trenches_queries": [{"sort_by": key} for key in ("volume_1h", "holder_count", "created_timestamp")]}
        with patch.dict(s.os.environ, {"GMGN_API_KEY": "test"}), patch.object(s, "run_gmgn_cli", return_value={"completed": []}) as call, patch.object(s.time, "sleep"):
            self.assertEqual(s.fetch_gmgn_trenches_universe(config), {})
        self.assertEqual(call.call_count, 1)
        self.assertEqual(config["_gmgn_trenches_requests"]["coverage"], "bounded_ranked_list_not_all_pools")

    def test_probe_is_read_only_and_labels_empty_history_unknown(self):
        rpc = self.router()
        for provider in rpc.providers.values():
            def result(method, params=None, **kwargs):
                return {"data": []} if method == "getTransactionsForAddress" else [] if method == "getSignaturesForAddress" else {"value": {"amount": "1", "decimals": 0}} if method == "getTokenSupply" else {"value": []} if method in ("getTokenAccountsByOwner", "getTokenLargestAccounts") else 1
            provider.call = Mock(side_effect=result)
        with patch.object(s, "build_rpc_router", return_value=rpc), patch.object(s, "save_runtime_state") as save:
            result = probe({})
        save.assert_not_called()
        self.assertTrue(result["read_only"])
        self.assertTrue(all(row["archive_coverage"] == "no_rows_not_proof_of_absence" for row in result["checks"] if row.get("archive_coverage")))


if __name__ == "__main__":
    unittest.main()
