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
        return self.responses.pop(0) if self.responses else {"data": []}


class ScannerCoreTests(unittest.TestCase):
    def test_retention_only_attributes_tokens_bought_in_wave(self):
        result = scanner.attributed_wave_retention(
            balance=1_000,
            bought_tokens=100,
            sold_tokens=20,
            min_retention_pct=20,
        )
        self.assertEqual(result["retained_tokens"], 80)
        self.assertEqual(result["retention_pct"], 100)
        self.assertTrue(result["qualified"])

        mostly_sold = scanner.attributed_wave_retention(
            balance=5,
            bought_tokens=100,
            sold_tokens=20,
            min_retention_pct=20,
        )
        self.assertEqual(mostly_sold["retained_tokens"], 5)
        self.assertAlmostEqual(mostly_sold["retention_pct"], 6.25)
        self.assertFalse(mostly_sold["qualified"])

    def test_buyer_concentration(self):
        top, top3 = scanner.buyer_concentration(
            [{"buy_sol": 60}, {"buy_sol": 20}, {"buy_sol": 10}, {"buy_sol": 10}]
        )
        self.assertAlmostEqual(top, 0.6)
        self.assertAlmostEqual(top3, 0.9)

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
            "helius_transactions_limit": 250,
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
            "helius_transactions_limit": 250,
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
        self.assertEqual(rpc.calls[0]["block_time"], {"gte": now - 5_400})
        self.assertEqual(stats["live_head_lag_seconds"], 10)
        self.assertEqual(stats["history_gap_seconds"], 5_400)
        self.assertTrue(stats["live_truncated"])

    def test_launch_backfill_does_not_fetch_outside_retained_buffer(self):
        now = 2_000_000
        rpc = FakeTransactionsRpc([{"data": []}])
        config = {
            "alert_window_minutes": 240,
            "helius_transactions_limit": 250,
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


if __name__ == "__main__":
    unittest.main()
