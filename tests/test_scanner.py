import unittest
from unittest import mock

import scanner


def rpc_response(result=None, *, status_code=200, error=None, text=""):
    response = mock.Mock(
        status_code=status_code,
        ok=200 <= status_code < 300,
        text=text,
        reason="OK" if 200 <= status_code < 300 else "RPC error",
        headers={},
    )
    body = {"jsonrpc": "2.0", "id": 1}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    response.json.return_value = body
    return response


class FakeRpc:
    def __init__(self, signatures):
        self.signatures = signatures

    def signatures_for_address(self, _address, limit=5):
        return self.signatures[:limit]


class FakeTransactionsRpc:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def transactions_for_address(
        self,
        address,
        limit=100,
        sort_order="desc",
        pagination_token=None,
        block_time=None,
    ):
        self.calls.append(
            {
                "address": address,
                "limit": limit,
                "sort_order": sort_order,
                "pagination_token": pagination_token,
                "block_time": block_time,
            }
        )
        response = self.responses.pop(0) if self.responses else {"data": []}
        if isinstance(response, Exception):
            raise response
        return response


class FakeSignatureTransactionRpc:
    def __init__(self, signatures, transaction_error=None):
        self.signatures = signatures
        self.transaction_error = transaction_error

    def signatures_for_address(self, _address, limit=100):
        return self.signatures[:limit]

    def transaction(self, _signature):
        if self.transaction_error:
            raise self.transaction_error
        return None


class ScannerCoreTests(unittest.TestCase):
    def test_helius_circuit_opens_after_repeated_provider_failures(self):
        rpc = scanner.HeliusRpc(
            "secret-key",
            max_retries=0,
            circuit_failure_threshold=2,
        )
        response = mock.Mock(
            status_code=429,
            ok=False,
            text="credits exhausted",
            reason="Too Many Requests",
            headers={},
        )
        rpc.session.post = mock.Mock(return_value=response)

        with self.assertRaises(scanner.HeliusRpcError):
            rpc.call("getSignaturesForAddress", ["pool", {"limit": 1}])
        with self.assertRaises(scanner.HeliusRpcError):
            rpc.call("getTransactionsForAddress", ["pool", {}])
        with self.assertRaises(scanner.HeliusCircuitOpen):
            rpc.call("getTokenSupply", ["mint"])

        self.assertEqual(rpc.failures["quota"], 2)
        self.assertEqual(rpc.session.post.call_count, 2)
        self.assertNotIn("secret-key", rpc.circuit_open_reason)

    def test_helius_usage_exhaustion_is_not_retried(self):
        rpc = scanner.HeliusRpc(
            "secret-key",
            max_retries=2,
            circuit_failure_threshold=2,
        )
        response = mock.Mock(
            status_code=429,
            ok=False,
            text='{"error":{"code":-32429,"message":"max usage reached"}}',
            reason="Too Many Requests",
            headers={},
        )
        rpc.session.post = mock.Mock(return_value=response)
        with self.assertRaises(scanner.HeliusRpcError) as raised:
            rpc.call("getTransactionsForAddress", ["pool", {}])
        self.assertEqual(raised.exception.category, "quota")
        self.assertEqual(rpc.session.post.call_count, 1)
        self.assertEqual(rpc.estimated_credits, 0)

    def test_helius_honors_retry_after_within_configured_cap(self):
        rpc = scanner.HeliusRpc(
            "secret-key",
            max_retries=1,
            retry_max_seconds=120,
        )
        retry_response = mock.Mock(
            status_code=429,
            ok=False,
            text="temporarily rate limited",
            reason="Too Many Requests",
            headers={"Retry-After": "120"},
        )
        success_response = mock.Mock(
            status_code=200,
            ok=True,
            text="",
            reason="OK",
            headers={},
        )
        success_response.json.return_value = {"result": "ok"}
        rpc.session.post = mock.Mock(side_effect=[retry_response, success_response])

        with mock.patch.object(scanner.time, "sleep") as sleep:
            self.assertEqual(rpc.call("getHealth"), "ok")

        sleep.assert_called_once_with(120.0)
        self.assertEqual(rpc.session.post.call_count, 2)
        self.assertEqual(rpc.estimated_credits, 1)

    def test_helius_transaction_history_credits_follow_returned_rows(self):
        rpc = scanner.HeliusRpc("secret-key", max_retries=0)
        self.assertEqual(
            rpc.credit_cost(
                "getTransactionsForAddress",
                {"data": [{} for _ in range(250)]},
            ),
            30,
        )
        self.assertEqual(
            rpc.credit_cost("getTransactionsForAddress", {"data": []}),
            10,
        )

    def test_drpc_uses_flat_solana_compute_unit_cost(self):
        rpc = scanner.DrpcRpc(
            "https://drpc.invalid",
            max_retries=0,
            credit_budget=40,
        )

        self.assertEqual(rpc.credit_cost("getHealth", None), 0)
        self.assertEqual(rpc.credit_cost("getSignaturesForAddress", None), 20)
        self.assertEqual(rpc.credit_cost("getTransaction", None), 20)
        self.assertTrue(rpc.can_call("getTransaction"))
        rpc.estimated_credits = 40
        self.assertFalse(rpc.can_call("getTransaction"))

    def test_rpc_uses_standard_history_after_enhanced_budget_is_spent(self):
        alchemy = scanner.AlchemyRpc(
            "https://alchemy.invalid",
            max_retries=0,
            credit_budget=100,
        )
        alchemy.estimated_credits = 100
        drpc = scanner.DrpcRpc(
            "https://drpc.invalid",
            max_retries=0,
            credit_budget=100,
        )
        rpc = scanner.RoutedSolanaRpc(
            [alchemy, drpc],
            standard_order=["drpc", "alchemy"],
            enhanced_order=["alchemy"],
        )

        self.assertEqual(rpc.available_history_mode(), "standard")
        drpc.estimated_credits = 100
        self.assertIsNone(rpc.available_history_mode())

    def test_build_rpc_router_accepts_drpc_api_key(self):
        with mock.patch.dict(
            scanner.os.environ,
            {"DRPC_API_KEY": "test-key"},
            clear=True,
        ):
            rpc = scanner.build_rpc_router({})

        self.assertEqual(list(rpc.providers), ["drpc"])
        self.assertEqual(
            rpc.providers["drpc"].url,
            "https://lb.drpc.live/solana/test-key",
        )

    def test_build_rpc_router_accepts_publicnode_fallback(self):
        with mock.patch.dict(
            scanner.os.environ,
            {"PUBLICNODE_SOLANA_RPC_URL": "https://solana-rpc.publicnode.com"},
            clear=True,
        ):
            rpc = scanner.build_rpc_router({})

        self.assertEqual(list(rpc.providers), ["publicnode"])
        self.assertEqual(rpc.available_history_mode(), "standard")
        self.assertIn(
            "getTokenAccountsByOwner",
            rpc.providers["publicnode"].unsupported_methods,
        )

    def test_temporary_method_failures_do_not_block_provider_history(self):
        provider = scanner.SolanaRpcProvider(
            "fallback",
            "https://fallback.invalid",
            max_retries=0,
            circuit_failure_threshold=2,
        )
        balance_error = scanner.HeliusRpcError(
            "getTokenAccountsByOwner",
            "temporary",
            "ReadTimeout",
            provider="fallback",
        )

        provider.record_failure(balance_error)
        provider.record_failure(balance_error)

        self.assertFalse(provider.can_call("getTokenAccountsByOwner"))
        self.assertTrue(provider.can_call("getSignaturesForAddress"))
        self.assertIsNone(provider.circuit_open_reason)
        self.assertIn(
            "getTokenAccountsByOwner",
            provider.method_circuit_open_reasons,
        )

    def test_rpc_min_interval_paces_consecutive_calls(self):
        rpc = scanner.AlchemyRpc(
            "https://alchemy.invalid",
            max_retries=0,
            min_interval_seconds=0.25,
        )
        rpc.session.post = mock.Mock(
            side_effect=[rpc_response("ok"), rpc_response("ok")]
        )

        with (
            mock.patch.object(scanner.time, "monotonic", side_effect=[10.0, 10.1]),
            mock.patch.object(scanner.time, "sleep") as sleep,
        ):
            self.assertEqual(rpc.call("getHealth"), "ok")
            self.assertEqual(rpc.call("getHealth"), "ok")

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.15)

    def test_rpc_method_interval_can_be_stricter_than_global_interval(self):
        rpc = scanner.AlchemyRpc(
            "https://alchemy.invalid",
            max_retries=0,
            min_interval_seconds=0.25,
            method_min_interval_seconds={"getSignaturesForAddress": 1.0},
        )
        rpc.session.post = mock.Mock(
            side_effect=[rpc_response("ok"), rpc_response([])]
        )

        with (
            mock.patch.object(scanner.time, "monotonic", side_effect=[10.0, 10.2]),
            mock.patch.object(scanner.time, "sleep") as sleep,
        ):
            self.assertEqual(rpc.call("getHealth"), "ok")
            self.assertEqual(
                rpc.call("getSignaturesForAddress", ["pool", {"limit": 1}]),
                [],
            )

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.8)

    def test_standard_transaction_prefers_chainstack_and_falls_back_to_alchemy(self):
        chainstack = scanner.ChainstackRpc(
            "https://chainstack.invalid",
            max_retries=0,
        )
        alchemy = scanner.AlchemyRpc("https://alchemy.invalid", max_retries=0)
        chainstack.session.post = mock.Mock(
            return_value=rpc_response(
                status_code=402,
                text="quota reached",
            )
        )
        alchemy.session.post = mock.Mock(
            return_value=rpc_response({"slot": 123})
        )
        rpc = scanner.RoutedSolanaRpc(
            [chainstack, alchemy],
            standard_order=["chainstack", "alchemy"],
            enhanced_order=["alchemy"],
        )

        result = rpc.transaction("sig")

        self.assertEqual(result["slot"], 123)
        self.assertEqual(rpc.last_provider_by_method["getTransaction"], "alchemy")
        self.assertIn("chainstack", rpc.blocked_providers)
        self.assertEqual(rpc.route_failovers["getTransaction"], 1)

    def test_signatures_skip_unsupported_chainstack_method(self):
        chainstack = scanner.ChainstackRpc(
            "https://chainstack.invalid",
            max_retries=0,
        )
        alchemy = scanner.AlchemyRpc("https://alchemy.invalid", max_retries=0)
        alchemy.session.post = mock.Mock(
            return_value=rpc_response([{"signature": "sig"}])
        )
        rpc = scanner.RoutedSolanaRpc(
            [chainstack, alchemy],
            standard_order=["chainstack", "alchemy"],
            enhanced_order=["alchemy"],
        )

        result = rpc.signatures_for_address("pool", limit=1)

        self.assertEqual(result[0]["signature"], "sig")
        self.assertEqual(sum(chainstack.calls.values()), 0)
        self.assertEqual(alchemy.calls["getSignaturesForAddress"], 1)
        self.assertEqual(rpc.route_failovers["getSignaturesForAddress"], 0)

    def test_single_chainstack_rate_limit_does_not_block_provider_for_run(self):
        chainstack = scanner.ChainstackRpc(
            "https://chainstack.invalid",
            max_retries=0,
            circuit_failure_threshold=3,
        )
        alchemy = scanner.AlchemyRpc("https://alchemy.invalid", max_retries=0)
        chainstack.session.post = mock.Mock(
            return_value=rpc_response(status_code=429, text="rate limit reached")
        )
        alchemy.session.post = mock.Mock(
            return_value=rpc_response({"slot": 123})
        )
        rpc = scanner.RoutedSolanaRpc(
            [chainstack, alchemy],
            standard_order=["chainstack", "alchemy"],
            enhanced_order=["alchemy"],
        )

        self.assertEqual(rpc.transaction("sig")["slot"], 123)
        self.assertNotIn("chainstack", rpc.blocked_providers)
        self.assertIsNone(chainstack.circuit_open_reason)

    def test_enhanced_history_prefers_alchemy_without_touching_chainstack(self):
        chainstack = scanner.ChainstackRpc(
            "https://chainstack.invalid",
            max_retries=0,
        )
        alchemy = scanner.AlchemyRpc("https://alchemy.invalid", max_retries=0)
        helius = scanner.HeliusRpc("secret-key", max_retries=0)
        alchemy.session.post = mock.Mock(
            return_value=rpc_response({"data": [], "paginationToken": None})
        )
        rpc = scanner.RoutedSolanaRpc(
            [chainstack, alchemy, helius],
            standard_order=["chainstack", "alchemy", "helius"],
            enhanced_order=["alchemy", "helius"],
        )

        result = rpc.transactions_for_address("pool", limit=1)

        self.assertEqual(result["_provider"], "alchemy")
        self.assertEqual(alchemy.session.post.call_count, 1)
        self.assertEqual(sum(chainstack.calls.values()), 0)
        self.assertEqual(sum(helius.calls.values()), 0)

    def test_token_balance_skips_unsupported_chainstack_method(self):
        chainstack = scanner.ChainstackRpc(
            "https://chainstack.invalid",
            max_retries=0,
        )
        alchemy = scanner.AlchemyRpc("https://alchemy.invalid", max_retries=0)
        alchemy.session.post = mock.Mock(
            return_value=rpc_response({"value": []})
        )
        rpc = scanner.RoutedSolanaRpc(
            [chainstack, alchemy],
            standard_order=["chainstack", "alchemy"],
            enhanced_order=["alchemy"],
            balance_order=["alchemy", "chainstack"],
        )

        self.assertEqual(rpc.token_balance("owner", "mint"), 0.0)
        self.assertEqual(sum(chainstack.calls.values()), 0)
        self.assertEqual(alchemy.calls["getTokenAccountsByOwner"], 1)

    def test_gmgn_trenches_pool_normalization(self):
        pool = scanner.gmgn_pool_from_trenches_item(
            {
                "address": "7bK4jRMa3aY85wJMQEQqWsmY9mmrRT32AFWZ23cBpump",
                "pool_address": "HXFDxwervt35vNJq91y57P8vzfpocK3Qm8dC8kmBLwiH",
                "name": "Tiny World Builder",
                "symbol": "TinyWorld",
                "exchange": "pump_amm",
                "usd_market_cap": 41_000,
                "liquidity": 20_000,
                "volume_1h": 2_500,
                "volume_24h": 30_000,
                "swaps_1h": 45,
                "total_supply": 1_000_000_000,
                "created_timestamp": 1_779_267_918,
            }
        )

        self.assertEqual(pool.dex, "pumpfun-amm")
        self.assertEqual(pool.source, "gmgn_trenches")
        self.assertEqual(pool.mcap_usd, 41_000)
        self.assertAlmostEqual(pool.price_usd, 0.000041)
        self.assertEqual(pool.txns_1h, 45)

    def test_apply_gmgn_ath_marks_timestamp_pending_without_losing_mcap(self):
        entry = {
            "ath_source": "solana_tracker",
            "ath_mcap_at": "2026-05-10T10:43:00Z",
        }
        scanner.apply_gmgn_ath(
            entry,
            {
                "highest_market_cap": 1_270_134,
                "highest_price": 0.00127,
                "pool_id": "pool",
            },
            "2026-07-29T12:00:00Z",
        )

        self.assertEqual(entry["ath_source"], "gmgn")
        self.assertEqual(entry["ath_status"], "partial")
        self.assertEqual(entry["ath_mcap_usd"], 1_270_134)
        self.assertNotIn("ath_mcap_at", entry)

    def test_implausible_gmgn_ath_is_quarantined_from_scoring(self):
        entry = {
            "latest_mcap_usd": 500_000,
            "scan_mcap_usd": 500_000,
        }
        accepted = scanner.apply_gmgn_ath(
            entry,
            {
                "highest_market_cap": 1_073_754_389_621_439,
                "highest_price": 1_000_000,
                "supply": 1_000_000_000,
            },
            "2026-07-29T12:00:00Z",
            current_mcap=500_000,
        )

        self.assertFalse(accepted)
        self.assertEqual(entry["ath_status"], "suspect")
        self.assertEqual(entry["ath_candidate_status"], "suspect")
        self.assertEqual(scanner.trusted_ath_mcap(entry), 0)

    def test_stale_registry_snapshot_does_not_reuse_old_activity(self):
        pool = scanner.registry_pool_from_market_entry(
            {
                "pool_address": "HXFDxwervt35vNJq91y57P8vzfpocK3Qm8dC8kmBLwiH",
                "token_address": "7bK4jRMa3aY85wJMQEQqWsmY9mmrRT32AFWZ23cBpump",
                "latest_mcap_usd": 250_000,
                "latest_liquidity_usd": 20_000,
                "latest_volume_1h_usd": 100_000,
                "latest_txns_1h": 2_000,
                "latest_seen_at": "1970-01-01T00:16:40Z",
            },
            now=10_000,
            activity_max_age_seconds=3_600,
        )

        self.assertTrue(pool.market_snapshot_stale)
        self.assertEqual(pool.mcap_usd, 250_000)
        self.assertEqual(pool.volume_1h_usd, 0)
        self.assertEqual(pool.txns_1h, 0)

    def test_reactivation_baseline_confirms_break_after_quiet_period(self):
        now = 2_000_000
        hourly = [
            [now - (index + 1) * 3600, 100_000, 10_000, 20, 500, 1, 5]
            for index in range(12)
        ]
        context = scanner.activity_context_from_history(
            {
                "hourly": hourly,
                "quiet_since": now - 8 * 3600,
                "last_snapshot_at": now - 300,
            },
            scanner.Pool(
                pool_address="pool",
                token_address="token",
                volume_5m_usd=2_000,
                volume_1h_usd=10_000,
                txns_5m=20,
                txns_1h=100,
            ),
            now,
            {
                "reactivation_baseline_min_samples": 12,
                "reactivation_baseline_min_quiet_hours": 6,
                "reactivation_baseline_min_volume_ratio": 3,
                "reactivation_baseline_min_txn_ratio": 3,
            },
        )

        self.assertEqual(context["status"], "ready")
        self.assertTrue(context["reactivation_confirmed"])
        self.assertEqual(context["quiet_hours"], 8)
        self.assertGreater(context["volume_1h_ratio"], 10)


    def test_retention_only_attributes_tokens_bought_in_wave(self):
        result = scanner.attributed_wave_retention(
            balance=1_000,
            bought_tokens=100,
            sold_tokens=20,
            min_retention_pct=20,
        )
        self.assertEqual(result["retained_tokens"], 80)
        self.assertEqual(result["retention_pct"], 80)
        self.assertEqual(result["net_coverage_pct"], 100)
        self.assertTrue(result["qualified"])

        mostly_sold = scanner.attributed_wave_retention(
            balance=5,
            bought_tokens=100,
            sold_tokens=20,
            min_retention_pct=20,
        )
        self.assertEqual(mostly_sold["retained_tokens"], 5)
        self.assertAlmostEqual(mostly_sold["retention_pct"], 5)
        self.assertAlmostEqual(mostly_sold["net_coverage_pct"], 6.25)
        self.assertFalse(mostly_sold["qualified"])

    def test_buyer_concentration(self):
        top, top3 = scanner.buyer_concentration(
            [{"buy_sol": 60}, {"buy_sol": 20}, {"buy_sol": 10}, {"buy_sol": 10}]
        )
        self.assertAlmostEqual(top, 0.6)
        self.assertAlmostEqual(top3, 0.9)

    def test_post_window_sales_reduce_attributed_retention(self):
        activity = scanner.owner_activity_since(
            [
                {
                    "kind": "buy",
                    "token_recipient": "buyer",
                    "token_amount": 100,
                    "sol_amount": 10,
                    "block_time": 100,
                },
                {
                    "kind": "sell",
                    "token_sender": "buyer",
                    "token_amount": 80,
                    "sol_amount": 8,
                    "block_time": 500,
                },
            ],
            100,
            {"buyer"},
        )
        retention = scanner.attributed_wave_retention(
            balance=20,
            bought_tokens=100,
            sold_tokens=activity["buyer"]["token_sold"],
            min_retention_pct=50,
        )
        self.assertEqual(activity["buyer"]["token_sold"], 80)
        self.assertEqual(retention["retained_tokens"], 20)
        self.assertEqual(retention["retention_pct"], 20)
        self.assertEqual(retention["net_coverage_pct"], 100)
        self.assertFalse(retention["qualified"])

    def test_router_accounts_are_not_used_as_wave_wallet_identity(self):
        routed_buy = {
            "kind": "buy",
            "signer": "router",
            "token_recipient": "buyer",
            "routed": True,
            "recipient_share": 0.98,
            "owner_resolution": "token_recipient",
        }
        unresolved_routed_buy = {
            "kind": "buy",
            "signer": "router",
            "token_recipient": "temporary-vault",
            "routed": True,
            "recipient_share": 0.50,
            "owner_resolution": "unresolved",
        }
        routed_sell = {
            "kind": "sell",
            "signer": "seller",
            "token_sender": "pool-vault",
        }
        self.assertEqual(scanner.wave_buy_owner(routed_buy), "buyer")
        self.assertEqual(scanner.wave_buy_owner(unresolved_routed_buy), "")
        self.assertEqual(scanner.wave_sell_owner(routed_sell), "seller")

    def test_resolved_routed_buy_contributes_to_reactivation_wave(self):
        metrics = scanner.reactivation_wave_window_metrics(
            [
                {
                    "kind": "buy",
                    "signer": "router",
                    "token_recipient": "buyer",
                    "routed": True,
                    "recipient_share": 0.99,
                    "owner_resolution": "token_recipient",
                    "sol_amount": 5,
                    "token_amount": 100,
                    "block_time": 100,
                }
            ],
            {
                "reactivation_wave_min_trade_sol": 0.1,
                "reactivation_wave_min_buy_sol": 1,
                "reactivation_wave_min_net_buy_sol": 1,
                "reactivation_wave_min_net_buy_ratio": 0.1,
                "reactivation_wave_min_unique_buyers": 1,
                "reactivation_wave_min_large_buyers": 1,
                "reactivation_wave_large_buy_min_sol": 1,
            },
        )

        self.assertEqual(metrics["unique_buyers"], 1)
        self.assertEqual(metrics["buyers"][0]["owner"], "buyer")
        self.assertEqual(metrics["owner_resolution_coverage_pct"], 100)

    def test_delegated_sell_is_attributed_to_token_sender(self):
        delegated_sell = {
            "kind": "sell",
            "signer": "delegate",
            "token_sender": "buyer",
        }
        self.assertEqual(scanner.wave_sell_owner(delegated_sell, {"buyer"}), "buyer")

    def test_routed_recipient_is_not_reported_as_wallet_link(self):
        score = scanner.score_events(
            [
                {
                    "wallet_class": "fresh",
                    "signer": "buyer-1",
                    "token_recipient": "router",
                    "sol_amount": 5,
                    "routed": True,
                },
                {
                    "wallet_class": "fresh",
                    "signer": "buyer-2",
                    "token_recipient": "router",
                    "sol_amount": 5,
                    "routed": True,
                },
            ],
            {
                "alert_min_suspicious_wallets": 2,
                "alert_min_suspicious_sol": 5,
                "big_buy_sol": 10,
            },
        )
        self.assertEqual(score[3], [])

    def test_scan_selection_combines_priority_and_oldest_rotation(self):
        pools = [
            scanner.Pool(pool_address=f"pool-{index}", token_address=f"token-{index}")
            for index in range(6)
        ]
        state = {
            "pools": {
                "pool-2": {"last_scanned_at": "2026-07-15T12:00:00Z"},
                "pool-4": {"last_scanned_at": "2026-07-15T10:00:00Z"},
                "pool-5": {"last_scanned_at": "2026-07-15T11:00:00Z"},
            }
        }
        selected, stats = scanner.select_scan_targets(
            pools,
            state,
            {"active_pool_limit": 4, "scan_priority_share": 0.5},
        )
        self.assertEqual([pool.pool_address for pool in selected[:2]], ["pool-0", "pool-1"])
        self.assertEqual([pool.pool_address for pool in selected[2:]], ["pool-3", "pool-4"])
        self.assertEqual(stats["priority"], 2)
        self.assertEqual(stats["rotation"], 2)

    def test_default_lane_selection_only_enables_reactivation(self):
        config = scanner.load_json(scanner.DEFAULT_CONFIG_PATH, {})
        self.assertEqual(scanner.selected_lanes(config, "all"), ["reactivation"])
        with self.assertRaises(SystemExit):
            scanner.selected_lanes(config, "micro_sticky")

    def test_scan_selection_reserves_capacity_for_recent_signals(self):
        pools = [
            scanner.Pool(pool_address=f"pool-{index}", token_address=f"token-{index}")
            for index in range(6)
        ]
        recent_alert = {
            "created_at": "2026-07-29T12:00:00Z",
            "pool": {"pool_address": "pool-5", "token_address": "token-5"},
            "score": 70,
        }
        with (
            mock.patch.object(scanner, "load_alert_history", return_value=[recent_alert]),
            mock.patch.object(
                scanner.time,
                "time",
                return_value=scanner.parse_timestamp(recent_alert["created_at"]) + 3_600,
            ),
        ):
            selected, stats = scanner.select_scan_targets(
                pools,
                {"pools": {}},
                {
                    "active_pool_limit": 4,
                    "scan_priority_share": 0.5,
                    "signal_monitor_share": 0.25,
                    "signal_monitor_max_age_hours": 48,
                },
            )
        self.assertEqual(selected[0].pool_address, "pool-5")
        self.assertEqual(stats["signal_monitor"], 1)
        self.assertEqual(len(selected), 4)

    def test_reactivation_monitor_ignores_alerts_from_disabled_lanes(self):
        pools = [
            scanner.Pool(pool_address=f"pool-{index}", token_address=f"token-{index}")
            for index in range(3)
        ]
        old_lane_alert = {
            "created_at": "2026-07-29T12:00:00Z",
            "lane": "micro_sticky",
            "pool": {"pool_address": "pool-2", "token_address": "token-2"},
            "score": 90,
        }
        with (
            mock.patch.object(scanner, "load_alert_history", return_value=[old_lane_alert]),
            mock.patch.object(
                scanner.time,
                "time",
                return_value=scanner.parse_timestamp(old_lane_alert["created_at"]) + 3_600,
            ),
        ):
            selected, stats = scanner.select_scan_targets(
                pools,
                {"pools": {}},
                {
                    "lane": "reactivation",
                    "active_pool_limit": 2,
                    "scan_priority_share": 1,
                    "signal_monitor_share": 0.5,
                    "signal_monitor_max_age_hours": 48,
                },
            )
        self.assertEqual([pool.pool_address for pool in selected], ["pool-0", "pool-1"])
        self.assertEqual(stats["signal_monitor"], 0)

    def test_market_refresh_ignores_catches_from_disabled_lanes(self):
        old_token = "A" * 32
        react_token = "B" * 32
        history = [
            {
                "created_at": "2026-07-29T12:00:00Z",
                "lane": "micro_sticky",
                "pool": {"pool_address": "old-pool", "token_address": old_token},
            },
            {
                "created_at": "2026-07-29T13:00:00Z",
                "lane": "reactivation",
                "pool": {"pool_address": "react-pool", "token_address": react_token},
            },
        ]
        with mock.patch.object(scanner, "load_alert_history", return_value=history):
            tokens = scanner.caught_market_token_addresses(
                {"market": {}},
                limit=10,
                lanes={"reactivation"},
            )
        self.assertEqual(tokens, [react_token])

    def test_alert_history_compaction_drops_disabled_lanes(self):
        config = scanner.load_json(scanner.DEFAULT_CONFIG_PATH, {})
        alerts = [
            {
                "created_at": "2026-07-29T12:00:00Z",
                "lane": "micro_sticky",
                "action_tier": "actionable",
                "score": 90,
                "pool": {"pool_address": "old-pool", "token_address": "old-token"},
            },
            {
                "created_at": "2026-07-29T13:00:00Z",
                "lane": "reactivation",
                "action_tier": "watch",
                "score": 70,
                "pool": {"pool_address": "react-pool", "token_address": "react-token"},
            },
        ]
        with mock.patch.object(scanner.time, "time", return_value=scanner.parse_timestamp("2026-07-29T14:00:00Z")):
            compacted = scanner.compact_alert_history(alerts, [], config)
        self.assertEqual(
            [(alert["lane"], alert["pool"]["token_address"]) for alert in compacted],
            [("reactivation", "react-token")],
        )

    def test_due_signal_recheck_preempts_recent_signal_monitor(self):
        now = scanner.parse_timestamp("2026-07-29T13:00:00Z")
        pools = [
            scanner.Pool(pool_address=f"pool-{index}", token_address=f"token-{index}")
            for index in range(6)
        ]
        recent_alert = {
            "created_at": "2026-07-29T12:30:00Z",
            "pool": {"pool_address": "pool-5", "token_address": "token-5"},
            "score": 70,
        }
        state = {
            "pools": {
                "pool-4": {"signal_recheck_due_at": "2026-07-29T12:00:00Z"},
            }
        }
        with (
            mock.patch.object(scanner, "load_alert_history", return_value=[recent_alert]),
            mock.patch.object(scanner.time, "time", return_value=now),
        ):
            selected, stats = scanner.select_scan_targets(
                pools,
                state,
                {
                    "active_pool_limit": 4,
                    "scan_priority_share": 0.5,
                    "signal_monitor_share": 0.25,
                    "signal_monitor_max_age_hours": 48,
                },
            )
        self.assertEqual(selected[0].pool_address, "pool-4")
        self.assertEqual(stats["due_rechecks"], 1)

    def test_swap_buffer_compaction_drops_old_and_redundant_data(self):
        now = 1_000_000
        state = {
            "pool": {
                "sticky_accumulation_swaps": [
                    {"signature": "old", "block_time": now - 7_200, "kind": "buy"},
                    {
                        "signature": "new",
                        "block_time": now - 30,
                        "kind": "buy",
                        "sol_amount": 2,
                        "symbol": "DROP_ME",
                    },
                ]
            }
        }
        stats = scanner.compact_pool_swap_buffers(
            state,
            {
                "sticky_accumulation_enabled": True,
                "state_swap_buffer_retention_hours": 1,
                "state_swap_buffer_max_swaps": 10,
            },
            now,
        )
        swaps = state["pool"]["sticky_accumulation_swaps"]
        self.assertEqual(stats["removed_swaps"], 1)
        self.assertEqual(len(swaps), 1)
        self.assertEqual(swaps[0]["signature"], "new")
        self.assertNotIn("symbol", swaps[0])

    def test_scan_health_rejects_partial_scan(self):
        health = scanner.build_scan_health(
            [{"transactions_scanned": 1, "parsed_swaps": 1}],
            {"reactivation": {"universe_pools": 10}},
            {"scan_health_min_scanned_pools": 5, "scan_health_max_failed_ratio": 0.25},
        )
        self.assertEqual(health["status"], "unhealthy")

    def test_scan_health_marks_rpc_budget_deferral_as_degraded(self):
        health = scanner.build_scan_health(
            [
                {"transactions_scanned": 1, "parsed_swaps": 1}
                for _ in range(5)
            ],
            {
                "reactivation": {
                    "universe_pools": 10,
                    "selection": {"rpc_budget_deferred": 2},
                }
            },
            {"scan_health_min_scanned_pools": 5},
        )

        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["rpc_budget_deferred_pools"], 2)

    def test_activity_probe_skips_unchanged_pool(self):
        changed, details = scanner.pool_has_new_activity(
            FakeRpc([{"signature": "same", "err": None}]),
            scanner.Pool(pool_address="pool", token_address="token"),
            {"helius_latest_signature": "same"},
            {"helius_activity_probe_enabled": True},
        )
        self.assertFalse(changed)
        self.assertEqual(details["reason"], "unchanged")

    def test_market_activity_avoids_redundant_signature_probe(self):
        rpc = mock.Mock()
        changed, details = scanner.pool_has_new_activity(
            rpc,
            scanner.Pool(
                pool_address="pool",
                token_address="token",
                txns_1h=10,
            ),
            {"helius_latest_signature": "previous"},
            {
                "helius_activity_probe_enabled": True,
                "helius_activity_probe_market_active_txns_1h": 5,
            },
        )
        self.assertTrue(changed)
        self.assertEqual(details["reason"], "market_activity")
        rpc.signatures_for_address.assert_not_called()

    def test_mature_wave_signal_gets_periodic_retention_recheck(self):
        pool_state = {}
        with mock.patch.object(scanner.time, "time", return_value=1_000_000):
            due_at = scanner.schedule_signal_recheck(
                pool_state,
                [
                    {
                        "wave": {
                            "hold_age_minutes": 180,
                            "min_hold_minutes": 120,
                        }
                    }
                ],
                {"signal_retention_recheck_minutes": 60},
            )
        self.assertEqual(due_at, 1_003_600)
        self.assertEqual(scanner.parse_timestamp(pool_state["signal_recheck_due_at"]), due_at)

    def test_concentrated_wave_is_ranked_as_noise_not_dropped(self):
        pool = scanner.Pool(
            pool_address="pool",
            token_address="token",
            mcap_usd=20_000,
            volume_1h_usd=1_000,
        )
        tier, _reasons, penalties, quality = scanner.classify_alert_tier(
            pool,
            {
                "signal_family": "sticky_accumulation",
                "wave": {
                    "sticky_supply_pct": 10,
                    "sticky_bought_pct": 80,
                    "net_token_retention_pct": 90,
                    "sticky_net_sol": 20,
                    "top_buyer_share": 0.7,
                    "top3_buyer_share": 0.9,
                },
                "suspicious_sol": 20,
            },
            {},
            {
                "lane": "micro_sticky",
                "alert_min_suspicious_wallets": 3,
                "alert_min_suspicious_sol": 8,
                "actionable_mcap_max_usd": 30_000,
                "watch_mcap_max_usd": 50_000,
                "sticky_accumulation_min_sticky_net_sol": 15,
                "sticky_accumulation_actionable_sticky_supply_pct": 10,
                "sticky_accumulation_actionable_sticky_bought_pct": 65,
                "sticky_accumulation_actionable_net_token_retention_pct": 85,
                "sticky_accumulation_max_top_buyer_share": 0.35,
                "sticky_accumulation_max_top3_buyer_share": 0.7,
            },
        )
        self.assertEqual(tier, "noise")
        self.assertIn("concentrated buyer flow", penalties)
        self.assertEqual(quality["top_buyer_share"], 0.7)

    def test_unseasoned_or_late_sticky_signal_cannot_be_actionable(self):
        pool = scanner.Pool(
            pool_address="pool",
            token_address="token",
            mcap_usd=40_000,
            volume_1h_usd=1_000,
        )
        tier, _reasons, penalties, _quality = scanner.classify_alert_tier(
            pool,
            {
                "signal_family": "sticky_accumulation",
                "wave": {
                    "sticky_supply_pct": 12,
                    "sticky_bought_pct": 80,
                    "net_token_retention_pct": 90,
                    "sticky_net_sol": 25,
                    "hold_age_minutes": 30,
                    "min_hold_minutes": 90,
                    "balance_coverage_pct": 100,
                },
                "suspicious_sol": 30,
            },
            {},
            {
                "lane": "micro_sticky",
                "alert_min_suspicious_wallets": 3,
                "alert_min_suspicious_sol": 8,
                "actionable_mcap_max_usd": 30_000,
                "watch_mcap_max_usd": 50_000,
                "sticky_accumulation_min_sticky_net_sol": 15,
                "sticky_accumulation_actionable_sticky_supply_pct": 10,
                "sticky_accumulation_actionable_sticky_bought_pct": 65,
                "sticky_accumulation_actionable_net_token_retention_pct": 85,
            },
        )
        self.assertEqual(tier, "watch")
        self.assertIn("retention not seasoned", penalties)
        self.assertIn("above actionable mcap", penalties)

    def test_partial_onchain_window_caps_actionable_alert_to_watch(self):
        alerts = scanner.apply_alert_data_quality(
            [{"action_tier": "actionable", "signal_family": "sticky_accumulation", "wave": {}}],
            {"live_truncated": True, "history_gap_seconds": 120},
            candidate_buys=0,
            classified_buys=0,
            classification_errors=0,
            config={"actionable_max_history_gap_seconds": 60},
        )
        self.assertEqual(alerts[0]["action_tier"], "watch")
        self.assertEqual(alerts[0]["data_quality"]["status"], "partial")
        self.assertIn("partial onchain coverage", alerts[0]["quality_penalties"])

    def test_sticky_lane_can_count_buys_without_wallet_classification(self):
        swaps = [
            {"kind": "buy", "sol_amount": 0.09},
            {"kind": "buy", "sol_amount": 0.1},
            {"kind": "buy", "sol_amount": 2},
            {"kind": "sell", "sol_amount": 3},
        ]
        candidates = scanner.buy_swap_candidates(swaps, {"classify_buy_min_sol": 0.1})
        self.assertEqual(len(candidates), 2)

    def test_dynamic_page_budget_scales_with_hourly_activity(self):
        config = {
            "helius_transactions_pages": 1,
            "helius_probe_incremental_pages": 1,
            "helius_medium_txn_threshold": 10_000,
            "helius_high_txn_threshold": 20_000,
            "helius_dynamic_page_budget_enabled": True,
            "helius_transactions_limit": 100,
            "helius_dynamic_incremental_target_hours": 1,
            "helius_dynamic_page_safety_factor": 1,
            "helius_probe_dynamic_max_pages": 4,
            "helius_dynamic_max_pages": 4,
        }
        active = scanner.Pool(pool_address="active", txns_1h=1_000)
        quiet = scanner.Pool(pool_address="quiet", txns_1h=50)
        self.assertEqual(scanner.helius_page_budget(active, config, "incremental", phase="probe"), 4)
        self.assertEqual(scanner.helius_page_budget(quiet, config, "incremental", phase="probe"), 1)

    def test_live_fetch_reads_newest_edge_when_state_is_stale(self):
        now = 2_000_000
        rpc = FakeTransactionsRpc(
            [
                {
                    "data": [
                        {
                            "blockTime": now - 10,
                            "transaction": {"signatures": ["newest"]},
                        }
                    ],
                    "paginationToken": "more",
                }
            ]
        )
        config = {
            "alert_window_minutes": 240,
            "helius_transactions_limit": 100,
            "helius_recent_lookback_minutes": 360,
            "helius_live_lookback_minutes": 90,
            "helius_probe_incremental_pages": 1,
            "helius_dynamic_page_budget_enabled": False,
            "helius_initial_backfill_enabled": False,
        }
        with mock.patch.object(scanner.time, "time", return_value=now):
            transactions, stats = scanner.fetch_helius_pool_transactions(
                rpc,
                scanner.Pool(pool_address="pool", token_address="token", txns_1h=100),
                config,
                {"helius_latest_block_time": now - 10_800},
                phase="probe",
            )

        self.assertEqual(len(transactions), 1)
        self.assertEqual(rpc.calls[0]["sort_order"], "desc")
        self.assertEqual(rpc.calls[0]["block_time"], {"gte": now - 10_830})
        self.assertEqual(stats["live_head_lag_seconds"], 10)
        self.assertEqual(stats["history_gap_seconds"], 0)
        self.assertFalse(stats["live_truncated"])
        self.assertEqual(len(rpc.calls), 1)

    def test_launch_backfill_does_not_fetch_outside_retained_buffer(self):
        now = 2_000_000
        rpc = FakeTransactionsRpc([{"data": []}])
        config = {
            "alert_window_minutes": 240,
            "helius_transactions_limit": 100,
            "helius_recent_lookback_minutes": 360,
            "helius_deep_recent_pages": 1,
            "helius_dynamic_page_budget_enabled": False,
            "helius_initial_backfill_enabled": True,
            "helius_initial_backfill_max_age_hours": 96,
            "state_swap_buffer_retention_hours": 24,
        }
        with mock.patch.object(scanner.time, "time", return_value=now):
            _transactions, stats = scanner.fetch_helius_pool_transactions(
                rpc,
                scanner.Pool(
                    pool_address="pool",
                    token_address="token",
                    pair_created_at=now - 48 * 3_600,
                ),
                config,
                {},
                phase="deep",
            )

        self.assertEqual([item["name"] for item in stats["passes"]], ["live_head"])
        self.assertFalse(stats["backfill_pending"])

    def test_scan_health_ignores_incomplete_background_backfill(self):
        health = scanner.build_scan_health(
            [
                {
                    "transactions_scanned": 1,
                    "parsed_swaps": 1,
                    "trade_fetch": {
                        "source": "helius_transactions",
                        "truncated": True,
                        "live_truncated": False,
                        "backfill_pending": True,
                        "live_head_lag_seconds": 10,
                    },
                }
            ],
            {"reactivation": {"universe_pools": 1}},
            {
                "scan_health_min_scanned_pools": 1,
                "scan_health_max_failed_ratio": 0.25,
                "scan_health_max_zero_parse_ratio": 0.25,
            },
        )
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["backfill_pending_pools"], 1)

    def test_scan_health_uses_successes_plus_errors_as_transaction_denominator(self):
        health = scanner.build_scan_health(
            [
                {
                    "transactions_scanned": 9,
                    "parsed_swaps": 9,
                    "trade_fetch": {
                        "source": "pool_signatures",
                        "transactions": 9,
                        "transaction_errors": 1,
                    },
                }
            ],
            {"micro_sticky": {"universe_pools": 1}},
            {
                "scan_health_min_scanned_pools": 1,
                "scan_health_max_failed_ratio": 0.25,
                "scan_health_max_zero_parse_ratio": 0.25,
                "scan_health_max_transaction_error_ratio": 0.09,
            },
        )
        self.assertAlmostEqual(health["transaction_error_ratio"], 0.1)
        self.assertEqual(health["status"], "degraded")

    def test_live_cursor_resumes_partial_transaction_window(self):
        now = 2_000_000
        first_rpc = FakeTransactionsRpc(
            [
                {
                    "data": [
                        {"blockTime": now - 5, "transaction": {"signatures": ["newest"]}},
                        {"blockTime": now - 10, "transaction": {"signatures": ["middle"]}},
                    ],
                    "paginationToken": "older-page",
                }
            ]
        )
        config = {
            "alert_window_minutes": 240,
            "helius_transactions_limit": 2,
            "helius_recent_lookback_minutes": 360,
            "helius_live_lookback_minutes": 90,
            "helius_probe_incremental_pages": 1,
            "helius_dynamic_page_budget_enabled": False,
            "helius_initial_backfill_enabled": False,
        }
        pool_state = {"helius_latest_block_time": now - 600}
        with mock.patch.object(scanner.time, "time", return_value=now):
            _transactions, first_stats = scanner.fetch_helius_pool_transactions(
                first_rpc,
                scanner.Pool(pool_address="pool", token_address="token", txns_1h=10),
                config,
                pool_state,
                phase="probe",
            )
        self.assertTrue(first_stats["live_truncated"])
        self.assertEqual(
            pool_state["helius_rolling_backlogs"][0]["cursor"],
            "older-page",
        )
        self.assertEqual(
            pool_state["helius_rolling_backlogs"][0]["head_signature"],
            "newest",
        )

        second_rpc = FakeTransactionsRpc(
            [
                {
                    "data": [
                        {"blockTime": now + 55, "transaction": {"signatures": ["fresh"]}},
                        {"blockTime": now + 50, "transaction": {"signatures": ["fresh-middle"]}},
                    ],
                    "paginationToken": "fresh-gap",
                },
                {
                    "data": [
                        {"blockTime": now - 20, "transaction": {"signatures": ["oldest"]}},
                    ]
                }
            ]
        )
        with mock.patch.object(scanner.time, "time", return_value=now + 60):
            _transactions, second_stats = scanner.fetch_helius_pool_transactions(
                second_rpc,
                scanner.Pool(pool_address="pool", token_address="token", txns_1h=10),
                config,
                pool_state,
                phase="probe",
        )
        self.assertTrue(second_stats["live_resumed"])
        self.assertTrue(second_stats["live_truncated"])
        self.assertEqual(second_rpc.calls[0]["pagination_token"], None)
        self.assertEqual(second_rpc.calls[1]["pagination_token"], "older-page")
        self.assertEqual(second_stats["live_checkpoint"]["signature"], "newest")
        self.assertEqual(
            pool_state["helius_rolling_backlogs"][0]["cursor"],
            "fresh-gap",
        )

        scanner.update_pool_transaction_state(
            pool_state,
            scanner.Pool(pool_address="pool", token_address="token"),
            [],
            checkpoint=second_stats["live_checkpoint"],
        )
        self.assertEqual(pool_state["helius_latest_signature"], "newest")
        self.assertNotIn("helius_live_cursor", pool_state)
        self.assertIn("helius_rolling_backlogs", pool_state)

    def test_stale_live_cursor_refreshes_head_without_discarding_backlog(self):
        now = 2_000_000
        rpc = FakeTransactionsRpc(
            [
                {
                    "data": [
                        {
                            "blockTime": now - 5,
                            "transaction": {"signatures": ["fresh-head"]},
                        }
                    ]
                }
            ]
        )
        config = {
            "alert_window_minutes": 240,
            "helius_transactions_limit": 100,
            "helius_recent_lookback_minutes": 360,
            "helius_live_lookback_minutes": 90,
            "helius_incremental_overlap_seconds": 30,
            "helius_live_cursor_head_refresh_seconds": 600,
            "helius_probe_incremental_pages": 1,
            "helius_dynamic_page_budget_enabled": False,
            "helius_initial_backfill_enabled": False,
        }
        pool_state = {
            "helius_latest_block_time": now - 7_200,
            "helius_live_cursor": {"provider": "alchemy", "token": "old-tail"},
            "helius_live_from": now - 7_200,
            "helius_live_pending_signature": "old-head",
            "helius_live_pending_block_time": now - 3_600,
        }

        with mock.patch.object(scanner.time, "time", return_value=now):
            transactions, stats = scanner.fetch_helius_pool_transactions(
                rpc,
                scanner.Pool(pool_address="pool", token_address="token", txns_1h=100),
                config,
                pool_state,
                phase="probe",
            )

        self.assertFalse(stats["live_cursor_reset"])
        self.assertTrue(stats["live_resumed"])
        self.assertEqual(rpc.calls[0]["pagination_token"], None)
        self.assertEqual(rpc.calls[0]["block_time"], {"gte": now - 7_230})
        self.assertEqual(stats["live_head_lag_seconds"], 5)
        self.assertEqual(
            transactions[0]["transaction"]["signatures"][0],
            "fresh-head",
        )
        self.assertNotIn("helius_live_cursor", pool_state)

    def test_market_activity_head_mismatch_uses_signature_fallback(self):
        now = 2_000_000
        rpc = FakeTransactionsRpc(
            [
                {
                    "data": [
                        {
                            "blockTime": now - 1_800,
                            "transaction": {"signatures": ["stale-enhanced-head"]},
                        }
                    ]
                }
            ]
        )
        rpc.signatures_for_address = mock.Mock(
            return_value=[
                {
                    "signature": "fresh-standard-head",
                    "blockTime": now - 10,
                    "err": None,
                }
            ]
        )
        config = {
            "alert_window_minutes": 240,
            "helius_transactions_limit": 100,
            "helius_recent_lookback_minutes": 360,
            "helius_live_lookback_minutes": 90,
            "helius_probe_recent_pages": 1,
            "helius_dynamic_page_budget_enabled": False,
            "helius_initial_backfill_enabled": False,
            "market_activity_consistency_enabled": True,
            "market_activity_consistency_min_txns_1h": 5,
            "market_activity_consistency_high_txns_1h": 100,
            "market_activity_consistency_high_max_lag_seconds": 600,
            "market_activity_consistency_head_mismatch_seconds": 60,
        }
        with (
            mock.patch.object(scanner.time, "time", return_value=now),
            self.assertRaises(scanner.EnhancedHistoryHeadMismatch),
        ):
            scanner.fetch_helius_pool_transactions(
                rpc,
                scanner.Pool(
                    pool_address="pool",
                    token_address="token",
                    txns_1h=500,
                ),
                config,
                {},
                phase="probe",
            )
        rpc.signatures_for_address.assert_called_once()

    def test_stale_market_snapshot_is_suppressed_when_both_heads_are_old(self):
        now = 2_000_000
        rpc = FakeTransactionsRpc(
            [
                {
                    "data": [
                        {
                            "blockTime": now - 1_800,
                            "transaction": {"signatures": ["stale-enhanced-head"]},
                        }
                    ]
                }
            ]
        )
        rpc.signatures_for_address = mock.Mock(
            return_value=[
                {
                    "signature": "stale-standard-head",
                    "blockTime": now - 1_700,
                    "err": None,
                }
            ]
        )
        config = {
            "alert_window_minutes": 240,
            "helius_transactions_limit": 100,
            "helius_recent_lookback_minutes": 360,
            "helius_live_lookback_minutes": 90,
            "helius_probe_recent_pages": 1,
            "helius_dynamic_page_budget_enabled": False,
            "helius_initial_backfill_enabled": False,
            "market_activity_consistency_enabled": True,
            "market_activity_consistency_min_txns_1h": 5,
            "market_activity_consistency_high_txns_1h": 100,
            "market_activity_consistency_high_max_lag_seconds": 600,
            "market_activity_stale_cooldown_minutes": 360,
        }
        pool_state = {}
        with mock.patch.object(scanner.time, "time", return_value=now):
            _transactions, stats = scanner.fetch_helius_pool_transactions(
                rpc,
                scanner.Pool(
                    pool_address="pool",
                    token_address="token",
                    mcap_usd=20_000,
                    volume_1h_usd=20_000,
                    txns_1h=500,
                ),
                config,
                pool_state,
                phase="probe",
            )
        self.assertTrue(stats["market_activity_stale"])
        self.assertEqual(
            stats["market_activity_probe"]["status"],
            "stale",
        )
        self.assertEqual(
            scanner.parse_timestamp(pool_state["market_activity_stale_until"]),
            now + 360 * 60,
        )

    def test_legacy_helius_cursor_replays_on_alchemy_without_token_leakage(self):
        now = 2_000_000
        helius = scanner.HeliusRpc("secret-key", max_retries=0)
        alchemy = scanner.AlchemyRpc("https://alchemy.invalid", max_retries=0)
        helius.session.post = mock.Mock(
            return_value=rpc_response(
                status_code=429,
                text="max usage reached",
            )
        )
        alchemy.session.post = mock.Mock(
            side_effect=[
                rpc_response(
                    {
                        "data": [
                            {
                                "blockTime": now - 5,
                                "transaction": {"signatures": ["alchemy-head"]},
                            },
                            {
                                "blockTime": now - 10,
                                "transaction": {"signatures": ["alchemy-middle"]},
                            },
                        ],
                        "paginationToken": "alchemy-head-tail",
                    }
                ),
                rpc_response(
                        {
                            "data": [
                                {
                                    "blockTime": now - 20,
                                    "transaction": {"signatures": ["alchemy-old"]},
                                }
                            ]
                        }
                ),
            ]
        )
        rpc = scanner.RoutedSolanaRpc(
            [alchemy, helius],
            standard_order=["alchemy", "helius"],
            enhanced_order=["alchemy", "helius"],
        )
        config = {
            "alert_window_minutes": 240,
            "helius_transactions_limit": 2,
            "helius_recent_lookback_minutes": 360,
            "helius_live_lookback_minutes": 90,
            "helius_probe_incremental_pages": 1,
            "helius_dynamic_page_budget_enabled": False,
            "helius_initial_backfill_enabled": False,
        }
        pool_state = {
            "helius_latest_block_time": now - 600,
            "helius_live_cursor": "helius-only-token",
            "helius_live_from": now - 600,
        }

        with mock.patch.object(scanner.time, "time", return_value=now):
            transactions, stats = scanner.fetch_helius_pool_transactions(
                rpc,
                scanner.Pool(pool_address="pool", token_address="token", txns_1h=10),
                config,
                pool_state,
                phase="probe",
            )

        helius_payload = helius.session.post.call_args.kwargs["json"]
        alchemy_payloads = [
            call.kwargs["json"] for call in alchemy.session.post.call_args_list
        ]
        self.assertEqual(
            helius_payload["params"][1]["paginationToken"],
            "helius-only-token",
        )
        self.assertNotIn("paginationToken", alchemy_payloads[0]["params"][1])
        self.assertNotIn("paginationToken", alchemy_payloads[1]["params"][1])
        self.assertEqual(
            transactions[-1]["transaction"]["signatures"][0],
            "alchemy-head",
        )
        self.assertEqual(stats["providers_used"], ["alchemy"])
        self.assertEqual(stats["provider_failovers"][0]["to"], "alchemy")
        self.assertNotIn("helius_live_cursor", pool_state)
        self.assertIn("helius_rolling_backlogs", pool_state)

    def test_mid_page_failover_restarts_window_and_deduplicates_signatures(self):
        now = 2_000_000
        alchemy = scanner.AlchemyRpc("https://alchemy.invalid", max_retries=0)
        helius = scanner.HeliusRpc("secret-key", max_retries=0)
        alchemy.session.post = mock.Mock(
            side_effect=[
                rpc_response(
                    {
                        "data": [
                            {
                                "blockTime": now - 5,
                                "transaction": {"signatures": ["same-head"]},
                            }
                        ],
                        "paginationToken": "alchemy-next",
                    }
                ),
                rpc_response(status_code=429, text="rate limit reached"),
            ]
        )
        helius.session.post = mock.Mock(
            return_value=rpc_response(
                {
                    "data": [
                        {
                            "blockTime": now - 5,
                            "transaction": {"signatures": ["same-head"]},
                        },
                        {
                            "blockTime": now - 10,
                            "transaction": {"signatures": ["older"]},
                        },
                    ]
                }
            )
        )
        rpc = scanner.RoutedSolanaRpc(
            [alchemy, helius],
            standard_order=["alchemy", "helius"],
            enhanced_order=["alchemy", "helius"],
        )
        config = {
            "alert_window_minutes": 240,
            "helius_transactions_limit": 1,
            "helius_recent_lookback_minutes": 360,
            "helius_live_lookback_minutes": 90,
            "helius_probe_incremental_pages": 2,
            "helius_dynamic_page_budget_enabled": False,
            "helius_initial_backfill_enabled": False,
        }
        pool_state = {"helius_latest_block_time": now - 600}

        with mock.patch.object(scanner.time, "time", return_value=now):
            transactions, stats = scanner.fetch_helius_pool_transactions(
                rpc,
                scanner.Pool(pool_address="pool", token_address="token", txns_1h=10),
                config,
                pool_state,
                phase="probe",
            )

        helius_payload = helius.session.post.call_args.kwargs["json"]
        signatures = [
            item["transaction"]["signatures"][0]
            for item in transactions
        ]
        self.assertNotIn("paginationToken", helius_payload["params"][1])
        self.assertEqual(signatures, ["older", "same-head"])
        self.assertEqual(stats["provider_failovers"][0]["from"], "alchemy")
        self.assertEqual(stats["provider_failovers"][0]["to"], "helius")

    def test_live_cursor_is_not_advanced_when_a_later_page_fails(self):
        now = 2_000_000
        rpc = FakeTransactionsRpc(
            [
                {
                    "data": [
                        {"blockTime": now - 5, "transaction": {"signatures": ["new"]}},
                    ],
                    "paginationToken": "next-page",
                },
                RuntimeError("provider page failed"),
            ]
        )
        config = {
            "alert_window_minutes": 240,
            "helius_transactions_limit": 1,
            "helius_recent_lookback_minutes": 360,
            "helius_live_lookback_minutes": 90,
            "helius_probe_incremental_pages": 2,
            "helius_dynamic_page_budget_enabled": False,
            "helius_initial_backfill_enabled": False,
        }
        pool_state = {
            "helius_latest_block_time": now - 600,
            "helius_live_cursor": "starting-page",
            "helius_live_from": now - 600,
        }
        with (
            mock.patch.object(scanner.time, "time", return_value=now),
            self.assertRaisesRegex(RuntimeError, "provider page failed"),
        ):
            scanner.fetch_helius_pool_transactions(
                rpc,
                scanner.Pool(pool_address="pool", token_address="token", txns_1h=10),
                config,
                pool_state,
                phase="probe",
            )
        self.assertEqual(pool_state["helius_live_cursor"], "starting-page")
        self.assertNotIn("helius_live_pending_signature", pool_state)

    def test_probe_truncation_is_cleared_when_deep_resume_completes(self):
        combined = scanner.combine_fetch_stats(
            {"live_truncated": True, "pages": 1, "transactions": 100},
            {
                "live_truncated": False,
                "live_resumed": True,
                "pages": 1,
                "transactions": 20,
            },
        )
        self.assertFalse(combined["live_truncated"])

    def test_standard_incremental_does_not_advance_past_failed_transaction(self):
        state = {
            "pools": {
                "pool": {
                    "latest_signature": "previous",
                    "helius_latest_signature": "previous",
                }
            }
        }
        rpc = FakeSignatureTransactionRpc(
            [
                {"signature": "new", "blockTime": 200, "err": None},
                {"signature": "previous", "blockTime": 100, "err": None},
            ],
            transaction_error=RuntimeError("detail unavailable"),
        )
        alerts, summary = scanner.scan_pool_signatures(
            rpc,
            scanner.Pool(pool_address="pool", token_address="token"),
            {
                "initial_backfill_signatures": 100,
                "helius_standard_incremental_signature_limit": 100,
                "classic_alerts_enabled": False,
                "sticky_accumulation_enabled": False,
                "reactivation_wave_enabled": False,
                "classify_buy_min_sol": 0.1,
                "alert_window_minutes": 240,
            },
            state,
            {"remaining": 0},
        )
        self.assertEqual(alerts, [])
        self.assertEqual(summary["trade_fetch"]["transaction_errors"], 1)
        self.assertEqual(state["pools"]["pool"]["latest_signature"], "previous")
        self.assertTrue(state["pools"]["pool"]["force_enhanced_next_scan"])

    def test_complete_head_mismatch_fallback_clears_stale_enhanced_cursor(self):
        state = {
            "pools": {
                "pool": {
                    "latest_signature": "previous",
                    "helius_latest_signature": "previous",
                    "helius_live_cursor": {"provider": "alchemy", "token": "stale-tail"},
                    "helius_live_pending_signature": "stale-head",
                    "helius_live_pending_block_time": 150,
                    "helius_live_from": 100,
                }
            }
        }
        rpc = FakeSignatureTransactionRpc(
            [
                {"signature": "new", "blockTime": 200, "err": None},
                {"signature": "previous", "blockTime": 100, "err": None},
            ]
        )
        rpc.transaction = mock.Mock(return_value={"transaction": {}})
        with mock.patch.object(scanner, "parse_pool_swap", return_value=None):
            _alerts, summary = scanner.scan_pool_signatures(
                rpc,
                scanner.Pool(pool_address="pool", token_address="token"),
                {
                    "initial_backfill_signatures": 100,
                    "helius_standard_incremental_signature_limit": 100,
                    "classic_alerts_enabled": False,
                    "sticky_accumulation_enabled": False,
                    "reactivation_wave_enabled": False,
                    "classify_buy_min_sol": 0.1,
                    "alert_window_minutes": 240,
                },
                state,
                {"remaining": 0},
                fallback_error=(
                    "enhanced transaction head lagged the standard RPC head by 1200s"
                ),
            )
        self.assertEqual(
            summary["fallback_error"],
            "enhanced transaction head lagged the standard RPC head by 1200s",
        )
        self.assertEqual(state["pools"]["pool"]["latest_signature"], "new")
        self.assertNotIn("helius_live_cursor", state["pools"]["pool"])
        self.assertNotIn("helius_live_pending_signature", state["pools"]["pool"])

    def test_standard_incremental_does_not_advance_past_parse_error(self):
        state = {
            "pools": {
                "pool": {
                    "latest_signature": "previous",
                    "helius_latest_signature": "previous",
                }
            }
        }
        rpc = FakeSignatureTransactionRpc(
            [
                {"signature": "new", "blockTime": 200, "err": None},
                {"signature": "previous", "blockTime": 100, "err": None},
            ]
        )
        rpc.transaction = mock.Mock(return_value={"transaction": {}})
        with mock.patch.object(scanner, "parse_pool_swap", side_effect=ValueError("bad tx")):
            _alerts, summary = scanner.scan_pool_signatures(
                rpc,
                scanner.Pool(pool_address="pool", token_address="token"),
                {
                    "initial_backfill_signatures": 100,
                    "helius_standard_incremental_signature_limit": 100,
                    "classic_alerts_enabled": False,
                    "sticky_accumulation_enabled": False,
                    "reactivation_wave_enabled": False,
                    "classify_buy_min_sol": 0.1,
                    "alert_window_minutes": 240,
                },
                state,
                {"remaining": 0},
            )
        self.assertEqual(summary["parse_errors"], 1)
        self.assertEqual(state["pools"]["pool"]["latest_signature"], "previous")
        self.assertTrue(state["pools"]["pool"]["force_enhanced_next_scan"])

    def test_classification_coverage_counts_unique_wallets(self):
        swaps = [
            {
                "kind": "buy",
                "signer": "same-wallet",
                "signature": "sig-1",
                "block_time": 100,
                "sol_amount": 1,
            },
            {
                "kind": "buy",
                "signer": "same-wallet",
                "signature": "sig-2",
                "block_time": 200,
                "sol_amount": 2,
            },
        ]
        with mock.patch.object(
            scanner,
            "classify_wallet",
            return_value={"wallet_class": "fresh"},
        ):
            events, candidate_count, errors = scanner.classify_buy_swaps(
                mock.Mock(),
                swaps,
                {
                    "classify_buy_min_sol": 0.1,
                    "max_wallet_classifications_per_pool": 10,
                    "helius_classify_global_buy_limit": 10,
                    "helius_classify_top_buys_per_window": 10,
                    "helius_dedupe_classification_wallets": True,
                    "alert_window_minutes": 60,
                },
                {"wallet_cache": {}},
                {"remaining": 10},
            )
        self.assertEqual(len(events), 1)
        self.assertEqual(candidate_count, 1)
        self.assertEqual(errors, 0)

    def test_wave_score_distinguishes_weak_and_strong_retention(self):
        config = {
            "sticky_accumulation_min_net_buy_sol": 10,
            "sticky_accumulation_min_unique_buyers": 5,
            "sticky_accumulation_min_sticky_wallets": 3,
            "sticky_accumulation_actionable_sticky_supply_pct": 5,
            "sticky_accumulation_actionable_sticky_bought_pct": 65,
            "sticky_accumulation_actionable_net_token_retention_pct": 85,
            "sticky_accumulation_max_top_buyer_share": 0.35,
            "sticky_accumulation_max_top3_buyer_share": 0.7,
            "actionable_min_balance_coverage_pct": 80,
        }
        weak = {
            "signal_family": "sticky_accumulation",
            "wave": {
                "net_buy_sol": 10,
                "unique_buyers": 5,
                "sticky_wallets": 3,
                "sticky_supply_pct": 3,
                "sticky_bought_pct": 40,
                "net_token_retention_pct": 60,
                "balance_coverage_pct": 100,
                "hold_age_minutes": 20,
                "min_hold_minutes": 120,
            },
        }
        strong = {
            "signal_family": "sticky_accumulation",
            "wave": {
                "net_buy_sol": 30,
                "unique_buyers": 10,
                "sticky_wallets": 6,
                "sticky_supply_pct": 8,
                "sticky_bought_pct": 90,
                "net_token_retention_pct": 95,
                "balance_coverage_pct": 100,
                "hold_age_minutes": 180,
                "min_hold_minutes": 120,
            },
        }
        self.assertLess(scanner.wave_quality_score(weak, config), scanner.wave_quality_score(strong, config))

    def test_wave_supply_failure_is_retriable_not_a_clean_result(self):
        candidate = {
            "start": 100,
            "end": 200,
            "window": [],
            "metrics": {
                "buyers": [],
                "last_buy_time": 100,
            },
        }
        rpc = mock.Mock()
        rpc.token_supply.side_effect = RuntimeError("supply unavailable")
        with (
            mock.patch.object(
                scanner,
                "reactivation_wave_window_candidates",
                return_value=[candidate],
            ),
            self.assertRaises(scanner.WaveDataUnavailable),
        ):
            scanner.build_reactivation_wave_alerts(
                scanner.Pool(pool_address="pool", token_address="token"),
                [],
                {"lane": "reactivation", "reactivation_wave_enabled": True},
                rpc,
            )

    def test_reactivation_stages_remove_mcap_floor_and_scale_thresholds(self):
        config = scanner.load_json(scanner.DEFAULT_CONFIG_PATH, {})
        lane = scanner.apply_lane(config, "reactivation")
        self.assertEqual(lane["mcap_min_usd"], 0)
        self.assertEqual(lane["liquidity_min_usd"], 3_000)
        self.assertIsNone(lane["ath_max_current_ratio"])

        ignition = scanner.reactivation_stage_config(
            scanner.Pool(pool_address="pool-1", token_address="token-1", mcap_usd=50_000),
            lane,
        )
        early = scanner.reactivation_stage_config(
            scanner.Pool(pool_address="pool-2", token_address="token-2", mcap_usd=133_042),
            lane,
        )
        self.assertEqual(ignition["reactivation_stage"], "ignition")
        self.assertEqual(ignition["reactivation_wave_min_buy_sol"], 20)
        self.assertEqual(early["reactivation_stage"], "early")
        self.assertEqual(early["reactivation_wave_min_buy_sol"], 35)
        self.assertEqual(early["volume_1h_to_mcap_max_watch"], 1.2)

    def test_reactivation_universe_accepts_old_token_below_100k(self):
        config = scanner.apply_lane(
            scanner.load_json(scanner.DEFAULT_CONFIG_PATH, {}),
            "reactivation",
        )
        pool = scanner.Pool(
            pool_address="pool",
            token_address="token",
            dex="pumpswap",
            mcap_usd=20_000,
            liquidity_usd=4_000,
            volume_1h_usd=500,
            pair_created_at=int(scanner.time.time() - 45 * 24 * 3600),
        )
        self.assertTrue(scanner.pool_matches_config(pool, config))
        self.assertEqual(
            scanner.reactivation_stage_config(pool, config)["reactivation_stage"],
            "ignition",
        )

    def test_reactivation_priority_favors_low_cap_activation_velocity(self):
        low_cap_wave = scanner.Pool(
            pool_address="low",
            token_address="low-token",
            mcap_usd=70_000,
            liquidity_usd=20_000,
            volume_1h_usd=20_000,
            txns_1h=300,
        )
        larger_slow_pool = scanner.Pool(
            pool_address="large",
            token_address="large-token",
            mcap_usd=2_000_000,
            liquidity_usd=200_000,
            volume_1h_usd=80_000,
            txns_1h=40,
        )
        self.assertGreater(
            scanner.reactivation_activity_score(low_cap_wave),
            scanner.reactivation_activity_score(larger_slow_pool),
        )

    def test_reactivation_priority_rewards_five_minute_burst(self):
        quiet = scanner.Pool(
            pool_address="quiet",
            token_address="quiet-token",
            mcap_usd=100_000,
            liquidity_usd=20_000,
            volume_5m_usd=100,
            volume_1h_usd=12_000,
            txns_5m=2,
            txns_1h=100,
        )
        bursting = scanner.Pool(
            pool_address="burst",
            token_address="burst-token",
            mcap_usd=100_000,
            liquidity_usd=20_000,
            volume_5m_usd=5_000,
            volume_1h_usd=12_000,
            txns_5m=50,
            txns_1h=100,
        )
        self.assertGreater(
            scanner.reactivation_activity_score(bursting),
            scanner.reactivation_activity_score(quiet),
        )

    def test_reactivation_priority_reserves_capacity_by_market_stage(self):
        config = scanner.apply_lane(
            scanner.load_json(scanner.DEFAULT_CONFIG_PATH, {}),
            "reactivation",
        )
        stage_mcaps = {
            "ignition": 50_000,
            "early": 150_000,
            "established": 500_000,
            "mature": 2_000_000,
        }
        candidates = []
        for stage, mcap in reversed(list(stage_mcaps.items())):
            for index in range(10):
                candidates.append(
                    scanner.Pool(
                        pool_address=f"{stage}-{index}",
                        token_address=f"{stage}-token-{index}",
                        mcap_usd=mcap,
                    )
                )
        selected = scanner.select_reactivation_priority(candidates, 20, config)
        counts = scanner.reactivation_stage_counts(selected, config)
        self.assertEqual(
            counts,
            {"ignition": 6, "early": 6, "established": 5, "mature": 3},
        )

    def test_reactivation_priority_defers_unchanged_stale_market_snapshot(self):
        now = 2_000_000
        stale = scanner.Pool(
            pool_address="stale",
            token_address="stale-token",
            mcap_usd=20_000,
            liquidity_usd=5_000,
            volume_1h_usd=20_000,
            txns_1h=500,
        )
        fresh_one = scanner.Pool(
            pool_address="fresh-1",
            token_address="fresh-token-1",
            mcap_usd=30_000,
            liquidity_usd=7_000,
            volume_1h_usd=5_000,
            txns_1h=100,
        )
        fresh_two = scanner.Pool(
            pool_address="fresh-2",
            token_address="fresh-token-2",
            mcap_usd=40_000,
            liquidity_usd=8_000,
            volume_1h_usd=4_000,
            txns_1h=80,
        )
        state = {
            "pools": {
                "stale": {
                    "market_activity_stale_until": scanner.iso(now + 3_600),
                    "market_activity_stale_fingerprint": scanner.market_activity_fingerprint(stale),
                }
            }
        }
        with mock.patch.object(scanner.time, "time", return_value=now):
            selected, stats = scanner.select_scan_targets(
                [stale, fresh_one, fresh_two],
                state,
                {
                    "lane": "reactivation",
                    "active_pool_limit": 2,
                    "scan_priority_share": 1,
                    "signal_monitor_share": 0,
                },
            )
        self.assertEqual(
            {pool.pool_address for pool in selected},
            {"fresh-1", "fresh-2"},
        )
        self.assertEqual(stats["market_stale_suppressed"], 1)
        self.assertEqual(stats["market_stale_selected"], 0)

    def test_changed_market_snapshot_rearms_suppressed_reactivation(self):
        now = 2_000_000
        pool = scanner.Pool(
            pool_address="pool",
            token_address="token",
            mcap_usd=25_000,
            volume_1h_usd=8_000,
            txns_1h=120,
        )
        state = {
            "pools": {
                "pool": {
                    "market_activity_stale_until": scanner.iso(now + 3_600),
                    "market_activity_stale_fingerprint": {
                        "mcap_usd": 20_000,
                        "volume_1h_usd": 1_000,
                        "txns_1h": 20,
                    },
                }
            }
        }
        self.assertFalse(
            scanner.market_activity_priority_suppressed(
                pool,
                state,
                {
                    "lane": "reactivation",
                    "market_activity_stale_rearm_change_ratio": 0.25,
                },
                now=now,
            )
        )

    def test_reactivation_without_ath_gate_still_uses_cached_ath_context(self):
        pool = scanner.Pool(
            pool_address="pool",
            token_address="token",
            mcap_usd=100_000,
        )
        kept = scanner.filter_reactivation_by_ath(
            mock.Mock(),
            {
                "market": {
                    "token": {
                        "ath_source": "gmgn",
                        "ath_mcap_usd": 1_000_000,
                    }
                }
            },
            [pool],
            {
                "lane": "reactivation",
                "ath_max_current_ratio": None,
            },
            "2026-07-30T00:00:00Z",
        )
        self.assertEqual(kept, [pool])
        self.assertEqual(pool.ath_mcap_usd, 1_000_000)
        self.assertEqual(pool.ath_current_ratio, 0.1)

    def test_ath_filter_log_handles_disabled_correction_gate(self):
        message = scanner.ath_filter_log_message(
            "reactivation",
            kept_pools=432,
            input_pools=432,
            max_ratio=None,
        )
        self.assertIn("correction gate disabled", message)
        self.assertNotIn("None", message)

    def test_brotchen_style_early_wave_is_hot_reactivation_not_late_chase(self):
        base = scanner.load_json(scanner.DEFAULT_CONFIG_PATH, {})
        pool = scanner.Pool(
            pool_address="pool",
            token_address="token",
            mcap_usd=133_042,
            liquidity_usd=43_548,
            volume_1h_usd=34_586,
        )
        config = scanner.reactivation_stage_config(
            pool,
            scanner.apply_lane(base, "reactivation"),
        )
        alert = {
            "score": 90,
            "signal_family": "reactivation_wave",
            "suspicious_sol": 82.6,
            "wave": {
                "net_buy_sol": 82.6,
                "unique_buyers": 142,
                "sticky_wallets": 36,
                "sticky_supply_pct": 14.13,
                "sticky_bought_pct": 43.68,
                "net_token_retention_pct": 55.42,
                "top_buyer_share": 0.227,
                "top3_buyer_share": 0.305,
                "balance_coverage_pct": 100,
                "hold_age_minutes": 0,
                "min_hold_minutes": 45,
            },
        }
        tier, reasons, penalties, quality = scanner.classify_alert_tier(
            pool,
            alert,
            {
                "hard_wallets": 0,
                "support_wallets": 0,
                "hard_sol": 0,
                "support_sol": 0,
                "hard_classes": {},
                "support_only": False,
            },
            config,
        )
        self.assertEqual(tier, "hot_reactivation")
        self.assertIn("early reactivation ignition", reasons)
        self.assertNotIn("blowoff volume", penalties)
        self.assertTrue(quality["hot_reactivation"])

    def test_high_cap_blowoff_without_early_quality_stays_late_chase(self):
        base = scanner.load_json(scanner.DEFAULT_CONFIG_PATH, {})
        pool = scanner.Pool(
            pool_address="pool",
            token_address="token",
            mcap_usd=900_000,
            liquidity_usd=100_000,
            volume_1h_usd=800_000,
        )
        config = scanner.reactivation_stage_config(
            pool,
            scanner.apply_lane(base, "reactivation"),
        )
        alert = {
            "score": 65,
            "signal_family": "reactivation_wave",
            "suspicious_sol": 30,
            "wave": {
                "net_buy_sol": 30,
                "unique_buyers": 20,
                "sticky_wallets": 10,
                "sticky_supply_pct": 4,
                "sticky_bought_pct": 50,
                "net_token_retention_pct": 60,
                "top_buyer_share": 0.2,
                "top3_buyer_share": 0.4,
                "balance_coverage_pct": 100,
            },
        }
        tier, _reasons, penalties, _quality = scanner.classify_alert_tier(
            pool,
            alert,
            {
                "hard_wallets": 0,
                "support_wallets": 0,
                "hard_sol": 0,
                "support_sol": 0,
                "hard_classes": {},
                "support_only": False,
            },
            config,
        )
        self.assertEqual(tier, "late_chase")
        self.assertIn("blowoff volume", penalties)

    def test_history_preserves_strong_early_reactivation_even_if_legacy_tier_is_late(self):
        alert = {
            "created_at": scanner.iso(scanner.time.time()),
            "action_tier": "late_chase",
            "lane": "reactivation",
            "signal_family": "reactivation_wave",
            "score": 90,
            "obs_mcap_usd": 133_042,
            "pool": {
                "pool_address": "pool",
                "token_address": "token",
                "mcap_usd": 133_042,
            },
        }
        history = scanner.compact_alert_history(
            [],
            [alert],
            {
                "alert_history_keep_tiers": ["actionable", "hot_reactivation", "watch"],
                "alert_history_keep_late_reactivation_min_score": 75,
                "alert_history_keep_late_reactivation_max_mcap_usd": 250_000,
                "alert_history_retention_hours": 0,
                "alert_history_max_tokens": 10,
                "alert_history_max_alerts": 10,
                "alert_history_max_alerts_per_token": 2,
            },
        )
        self.assertEqual(history, [alert])

    def test_gmgn_discovery_queries_volume_swaps_and_change(self):
        config = scanner.load_json(scanner.DEFAULT_CONFIG_PATH, {})
        calls = []

        def fake_run(_config, arguments, _label):
            calls.append(arguments)
            return {"data": {"rank": []}}

        with (
            mock.patch.dict(scanner.os.environ, {"GMGN_API_KEY": "test"}),
            mock.patch.object(scanner, "run_gmgn_cli", side_effect=fake_run),
            mock.patch.object(scanner.time, "sleep"),
        ):
            scanner.fetch_gmgn_trending_token_addresses(config)

        query_pairs = {
            (
                arguments[arguments.index("--interval") + 1],
                arguments[arguments.index("--order-by") + 1],
            )
            for arguments in calls
        }
        self.assertIn(("1m", "volume"), query_pairs)
        self.assertIn(("1m", "swaps"), query_pairs)
        self.assertIn(("1h", "change1h"), query_pairs)

    def test_wave_wallet_graph_merges_overlapping_funder_and_executor_links(self):
        buyers = [
            {
                "owner": "wallet-a",
                "buy_sol": 4,
                "top_buy": {
                    "signature": "sig-a",
                    "block_time": 100,
                    "routed": False,
                    "signer": "wallet-a",
                },
            },
            {
                "owner": "wallet-b",
                "buy_sol": 3,
                "top_buy": {
                    "signature": "sig-b",
                    "block_time": 101,
                    "routed": True,
                    "signer": "executor",
                },
            },
            {
                "owner": "wallet-c",
                "buy_sol": 3,
                "top_buy": {
                    "signature": "sig-c",
                    "block_time": 102,
                    "routed": True,
                    "signer": "executor",
                },
            },
        ]
        classifications = {
            "wallet-a": {"wallet_class": "fresh", "funding_source": "funder"},
            "wallet-b": {"wallet_class": "freshish", "funding_source": "funder"},
            "wallet-c": {"wallet_class": "normal", "funding_source": None},
        }

        def classify(_rpc, wallet, *_args):
            return classifications[wallet]

        with mock.patch.object(scanner, "classify_wallet", side_effect=classify):
            graph = scanner.analyze_wave_wallet_graph(
                mock.Mock(),
                buyers,
                {
                    "reactivation_wallet_graph_enabled": True,
                    "reactivation_wallet_graph_limit": 12,
                },
                {},
            )

        self.assertEqual(graph["effective_wallets"], 1)
        self.assertEqual(graph["max_cluster_share"], 1)
        self.assertEqual(graph["clusters"][0]["wallets"], 3)
        self.assertEqual(graph["common_funders"][0]["wallets"], 2)
        self.assertEqual(graph["common_executors"][0]["wallets"], 2)

    def test_linked_wallet_concentration_cannot_be_actionable(self):
        config = scanner.apply_lane(
            scanner.load_json(scanner.DEFAULT_CONFIG_PATH, {}),
            "reactivation",
        )
        pool = scanner.Pool(
            pool_address="pool",
            token_address="token",
            mcap_usd=100_000,
            volume_1h_usd=4_000,
        )
        pool.ath_mcap_usd = 1_000_000
        pool.ath_current_ratio = 0.1
        alert = {
            "score": 90,
            "signal_family": "reactivation_wave",
            "reactivation_baseline": {
                "status": "ready",
                "reactivation_confirmed": True,
            },
            "wave": {
                "net_buy_sol": 50,
                "unique_buyers": 20,
                "effective_unique_buyers": 5,
                "sticky_wallets": 12,
                "sticky_supply_pct": 10,
                "sticky_bought_pct": 80,
                "net_token_retention_pct": 90,
                "top_buyer_share": 0.2,
                "top3_buyer_share": 0.5,
                "max_linked_cluster_share": 0.8,
                "balance_coverage_pct": 100,
                "hold_age_minutes": 120,
                "min_hold_minutes": 30,
            },
        }
        tier, _reasons, penalties, quality = scanner.classify_alert_tier(
            pool,
            alert,
            {
                "hard_wallets": 0,
                "support_wallets": 0,
                "hard_sol": 0,
                "hard_classes": {},
                "support_only": False,
            },
            config,
        )
        self.assertEqual(tier, "noise")
        self.assertIn("linked wallet concentration", penalties)
        self.assertEqual(quality["effective_unique_buyers"], 5)

    def test_signal_outcomes_capture_horizons_and_preserve_peak(self):
        token = "11111111111111111111111111111111"
        caught = 1_800_000_000
        state = {
            "market": {
                token: {
                    "latest_seen_at": scanner.iso(caught + 25 * 3600),
                    "latest_mcap_usd": 200_000,
                    "latest_price_usd": 0.002,
                    "latest_liquidity_usd": 20_000,
                }
            }
        }
        alert = {
            "created_at": scanner.iso(caught),
            "obs_mcap_at": scanner.iso(caught),
            "obs_mcap_usd": 100_000,
            "obs_price_usd": 0.001,
            "score": 80,
            "action_tier": "actionable",
            "signal_family": "reactivation_wave",
            "pool": {
                "pool_address": "pool",
                "token_address": token,
                "symbol": "TEST",
            },
        }
        config = {
            "signal_outcomes_enabled": True,
            "signal_outcomes_retention_days": 0,
            "signal_outcomes_max_tokens": 10,
        }
        scanner.update_signal_outcomes(
            state,
            [alert],
            scanner.iso(caught + 25 * 3600),
            config,
        )
        outcome = state["signal_outcomes"][token]
        self.assertEqual(outcome["horizons"]["24h"]["return_pct"], 100)
        self.assertEqual(outcome["max_favorable"]["return_pct"], 100)

        state["market"][token].update(
            {
                "latest_seen_at": scanner.iso(caught + 73 * 3600),
                "latest_mcap_usd": 50_000,
                "latest_price_usd": 0.0005,
            }
        )
        scanner.update_signal_outcomes(
            state,
            [],
            scanner.iso(caught + 73 * 3600),
            config,
        )
        outcome = state["signal_outcomes"][token]
        self.assertEqual(outcome["horizons"]["72h"]["return_pct"], -50)
        self.assertEqual(outcome["max_favorable"]["return_pct"], 100)
        self.assertEqual(outcome["max_adverse"]["return_pct"], -50)

    def test_wallet_classification_cache_reuses_same_time_bucket(self):
        rpc = mock.Mock()
        rpc.signatures_for_address.return_value = []
        state = {}
        config = {
            "wallet_cache_bucket_hours": 6,
            "freshish_max_previous_txs": 3,
            "dormant_gap_days": 14,
            "low_tx_max_previous_txs": 10,
        }
        first = scanner.classify_wallet(
            rpc,
            "wallet",
            "sig-1",
            10_000,
            config,
            state,
        )
        second = scanner.classify_wallet(
            rpc,
            "wallet",
            "sig-2",
            10_100,
            config,
            state,
        )
        self.assertEqual(first, second)
        self.assertEqual(rpc.signatures_for_address.call_count, 1)
        self.assertEqual(len(state["wallet_cache"]), 1)

    def test_rpc_credit_budget_fails_over_before_overspend(self):
        alchemy = scanner.AlchemyRpc(
            "https://alchemy.invalid",
            max_retries=0,
            credit_budget=5,
        )
        chainstack = scanner.ChainstackRpc(
            "https://chainstack.invalid",
            max_retries=0,
            credit_budget=10,
        )
        alchemy.session.post = mock.Mock()
        chainstack.session.post = mock.Mock(return_value=rpc_response(123))
        rpc = scanner.RoutedSolanaRpc(
            [alchemy, chainstack],
            standard_order=["alchemy", "chainstack"],
        )
        result = rpc.call("getBalance", ["wallet"])
        self.assertEqual(result, 123)
        self.assertEqual(alchemy.session.post.call_count, 0)
        self.assertEqual(chainstack.session.post.call_count, 1)
        self.assertIn("alchemy", rpc.blocked_providers)
        self.assertEqual(rpc.route_failovers["getBalance"], 1)

    def test_discovery_pulse_uses_small_fast_market_query_set(self):
        config = scanner.load_json(scanner.DEFAULT_CONFIG_PATH, {})
        pulse = scanner.discovery_pulse_config(config)
        self.assertEqual(pulse["registry_refresh_max_tokens"], 120)
        self.assertEqual(pulse["gecko_pages"], 1)
        self.assertEqual(
            pulse["gmgn_trenches_queries"],
            [
                {"sort_by": "swaps_1h", "direction": "desc"},
                {"sort_by": "usd_market_cap", "direction": "asc"},
            ],
        )
        self.assertEqual(
            pulse["gmgn_trending_queries"],
            [
                {"order_by": "volume", "intervals": ["1m", "5m"]},
                {"order_by": "swaps", "intervals": ["1m", "5m"]},
            ],
        )

    def test_scan_selection_reserves_gap_repair_capacity(self):
        pools = [
            scanner.Pool(
                pool_address=f"pool-{index}",
                token_address=f"token-{index}",
                mcap_usd=100_000 + index,
                liquidity_usd=10_000,
                volume_1h_usd=1_000 - index,
            )
            for index in range(6)
        ]
        state = {
            "pools": {
                pool.pool_address: {
                    "last_scanned_at": scanner.iso(1_700_000_000 + index),
                }
                for index, pool in enumerate(pools)
            }
        }
        state["pools"]["pool-5"]["helius_rolling_backlogs"] = [
            {"from_timestamp": 1_600_000_000}
        ]
        with mock.patch.object(scanner.time, "time", return_value=1_800_000_000):
            selected, stats = scanner.select_scan_targets(
                pools,
                state,
                {
                    "active_pool_limit": 4,
                    "signal_monitor_share": 0,
                    "scan_priority_share": 0.75,
                    "scan_gap_repair_share": 0.25,
                    "scan_rotation_min_share": 0.25,
                },
            )
        self.assertIn("pool-5", [pool.pool_address for pool in selected])
        self.assertEqual(stats["gap_repairs"], 1)
        self.assertEqual(stats["rotation_reserve"], 1)


if __name__ == "__main__":
    unittest.main()
