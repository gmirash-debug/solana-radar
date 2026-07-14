import unittest

import scanner


class FakeRpc:
    def __init__(self, signatures):
        self.signatures = signatures

    def signatures_for_address(self, _address, limit=5):
        return self.signatures[:limit]


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


if __name__ == "__main__":
    unittest.main()
