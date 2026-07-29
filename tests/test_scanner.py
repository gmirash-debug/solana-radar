import unittest
from unittest import mock

import scanner


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
            "signer": "buyer",
            "token_recipient": "router",
            "routed": True,
        }
        routed_sell = {
            "kind": "sell",
            "signer": "seller",
            "token_sender": "pool-vault",
        }
        self.assertEqual(scanner.wave_buy_owner(routed_buy), "")
        self.assertEqual(scanner.wave_sell_owner(routed_sell), "seller")

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
            {"state_swap_buffer_retention_hours": 1, "state_swap_buffer_max_swaps": 10},
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

        self.assertEqual([item["name"] for item in stats["passes"]], ["live"])
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
        self.assertEqual(pool_state["helius_live_cursor"], "older-page")
        self.assertEqual(pool_state["helius_live_pending_signature"], "newest")

        second_rpc = FakeTransactionsRpc(
            [
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
        self.assertFalse(second_stats["live_truncated"])
        self.assertEqual(second_rpc.calls[0]["pagination_token"], "older-page")
        self.assertEqual(second_stats["live_checkpoint"]["signature"], "newest")

        scanner.update_pool_transaction_state(
            pool_state,
            scanner.Pool(pool_address="pool", token_address="token"),
            [],
            checkpoint=second_stats["live_checkpoint"],
        )
        self.assertEqual(pool_state["helius_latest_signature"], "newest")
        self.assertNotIn("helius_live_cursor", pool_state)

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


if __name__ == "__main__":
    unittest.main()
