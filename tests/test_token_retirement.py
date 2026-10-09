import copy
import unittest

from token_retirement import (POLICY, observe_low_caps, purge_retired_state, recapture_proof,
                              current_record, iso, timestamp)


class TokenRetirementTests(unittest.TestCase):
    def setUp(self):
        self.start = timestamp("2026-10-08T12:00:00Z")
        self.state = {"market": {}, "pools": {}}

    def observe(self, hour, cap=19000, **changes):
        at = iso(self.start + hour * 3600)
        self.state["market"]["token"] = {"latest_mcap_usd": cap, "current_market_verified_at": at,
            "market_snapshot_stale": False, **changes}
        return observe_low_caps(self.state, {"token"}, at)

    def test_retire_only_after_24_hours_of_distinct_fresh_observations(self):
        for hour in range(24):
            self.assertEqual(self.observe(hour), [])
        actions = self.observe(24)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["operation"], "retire")
        self.assertEqual(actions[0]["samples"], 25)
        self.assertEqual(actions[0]["max_gap_seconds"], 3600)

    def test_20k_boundary_and_recovery_restart_full_clock(self):
        for hour in range(23):
            self.observe(hour)
        self.assertEqual(self.observe(23, 20000), [])
        self.assertEqual(self.observe(24), [])
        self.assertEqual(self.state["token_low_cap_watch"]["token"]["below_since"], iso(self.start + 24 * 3600))

    def test_long_missing_history_is_not_a_24_hour_low(self):
        self.observe(0)
        self.assertEqual(self.observe(24), [])
        self.assertEqual(self.state["token_low_cap_watch"]["token"]["samples"], 1)

    def test_invalid_stale_future_and_missing_quotes_cannot_retire(self):
        for cap in (0, None, False, "19000", float("nan"), float("inf"), -1):
            self.assertEqual(self.observe(0, cap), [])
            self.assertNotIn("token", self.state.get("token_low_cap_watch", {}))
        self.assertEqual(self.observe(0, market_snapshot_stale=True), [])
        self.assertEqual(self.observe(24, current_market_verified_at=iso(self.start)), [])
        self.assertEqual(self.observe(0, current_market_verified_at=iso(self.start + 60)), [])

    def test_same_quote_is_never_counted_twice_and_existing_fences_stop_watch(self):
        self.observe(0)
        self.observe(0)
        self.assertEqual(self.state["token_low_cap_watch"]["token"]["samples"], 1)
        self.state["token_retirements"] = {"token": {"retired_at": iso(self.start)}}
        self.observe(1)
        self.assertNotIn("token", self.state["token_low_cap_watch"])

    def alert(self):
        now = self.start + 3600
        return {"signal_family": "reactivation_wave", "pool": {"token_address": "token", "mcap_usd": 35000,
            "market_snapshot_at": now, "gmgn_attention": {"last_seen_at": iso(now),
                "memberships": [{"source": "trending"}]}}, "wave": {"net_buy_sol": 30},
            "window_start": iso(self.start + 1800), "window_end": iso(now - 60),
            "coordination_events": [{"block_time": self.start + 1900 + i * 60, "price_native": p}
                for i, p in enumerate([1, 1, 1.1, 1.1])]}

    def test_new_growing_buy_wave_can_recapture(self):
        proof = recapture_proof(self.alert(), {"retired_at": iso(self.start)}, iso(self.start + 3600))
        self.assertEqual(proof["operation"], "recapture")
        self.assertAlmostEqual(proof["growth_pct"], 10)

    def test_ranking_or_price_bounce_alone_or_old_wave_cannot_recapture(self):
        marker, now = {"retired_at": iso(self.start)}, iso(self.start + 3600)
        for change in ({"coordination_events": []}, {"window_start": iso(self.start - 60)},
                       {"wave": {"net_buy_sol": 0}}, {"signal_family": "legacy"}):
            alert = self.alert()
            alert.update(change)
            self.assertIsNone(recapture_proof(alert, marker, now))
        for field, value in (("mcap_usd", 29999), ("mcap_usd", 500001),
                             ("market_snapshot_stale", True), ("gmgn_attention", {})):
            alert = self.alert()
            alert["pool"][field] = value
            self.assertIsNone(recapture_proof(alert, marker, now))
        alert = self.alert()
        for row in alert["coordination_events"]:
            row["price_native"] = 1
        self.assertIsNone(recapture_proof(alert, marker, now))

    def test_purge_keeps_other_tokens_and_budget_ledgers_but_not_original_cohort(self):
        self.state.update(token_retirements={"token": {"retired_at": iso(self.start)}},
            pools={"pool": {"signal_thesis": {"token_address": "token", "cohort": [{"owner": "wallet"}]}},
                   "other": {"signal_thesis": {"token_address": "other"}}},
            signal_outcomes={"token": {"token_address": "token"}},
            signal_evaluation_dataset={"episodes": {"token": {"token_address": "token"}}},
            rpc_budget_ledger={"allocated": 10}, gmgn_candidates={"token": {"rank": 1}},
            activity_baselines={"token": {"bins": [1]}})
        self.state["market"]["token"] = {"pool_address": "pool"}
        ledger = copy.deepcopy(self.state["rpc_budget_ledger"])
        self.assertEqual(purge_retired_state(self.state), 1)
        self.assertEqual(set(self.state["pools"]), {"other"})
        self.assertEqual(self.state["market"], {})
        self.assertEqual(self.state["rpc_budget_ledger"], ledger)
        self.assertEqual(self.state["signal_evaluation_dataset"]["episodes"], {})
        self.assertEqual(self.state["gmgn_candidates"], {})
        self.assertIn("token", self.state["token_retirements"])

    def test_recapture_does_not_resurrect_old_signal_episode(self):
        marker = {"retired_at": iso(self.start), "reactivated_at": iso(self.start + 3600)}
        self.assertFalse(current_record({"token_address": "token", "signal_at": iso(self.start - 1)}, {"token": marker}))
        self.assertTrue(current_record({"token_address": "token", "signal_at": iso(self.start + 1800)}, {"token": marker}))


if __name__ == "__main__":
    unittest.main()
