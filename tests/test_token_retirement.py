import copy
import unittest
from unittest.mock import patch

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
            "coordination_events": [{"block_time": self.start + 2400 + i * 60, "price_native": p}
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

        alert = self.alert()
        for row in alert["coordination_events"]:
            row["block_time"] -= 3600
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

    def test_lifecycle_contract_is_outside_the_checksummed_checkpoint_manifest(self):
        import scanner
        from runtime_checkpoint import hydrate_checkpoint, restore_checkpoint
        state = {"pools": {}, "wallet_cache": {}, "token_retirements": {}, "token_low_cap_watch": {"token": {"samples": 1}},
                 "_runtime": {"updated_at": iso(self.start), "revision": 1}}
        uploads = []
        def upload(path, config, payload, deadline, params=None):
            uploads.append((payload, params))
            return {"ok": True, "accepted": True}
        with patch.object(scanner, "remote_data_url_from_env", return_value="https://test"), \
             patch.object(scanner, "remote_ingest_secret", return_value="test-only"), \
             patch.object(scanner, "remote_api_call", return_value={"parts": []}), \
             patch.object(scanner, "safe_runtime_upload", side_effect=upload):
            result = scanner.sync_runtime_checkpoint(state, {"token_retirement_enabled": True}, "deep")
        self.assertTrue(result["ok"])
        manifest, params = uploads[-1]
        self.assertEqual(manifest["token_lifecycle_version"], 1)
        self.assertNotIn("token_lifecycle_version", manifest["checkpoint"])
        parts = {params["part"]: payload["checkpoint"] for payload, params in uploads[:-1]}
        hydrated = hydrate_checkpoint(manifest["checkpoint"], lambda key: parts[key])
        restored, ok = restore_checkpoint({}, hydrated)
        self.assertTrue(ok)
        self.assertEqual(restored, state)

    def test_small_clock_write_recovers_an_ambiguous_commit_by_exact_readback(self):
        import scanner
        config = {}
        state = {"token_low_cap_watch": {"token": {"samples": 1}}}
        with patch.object(scanner, "remote_data_url_from_env", return_value="https://test"), \
             patch.object(scanner, "remote_ingest_secret", return_value="test-only"), \
             patch.object(scanner, "remote_api_call", side_effect=[RuntimeError("timeout"),
                 {"document": {"value": {"version": 1, "watches": state["token_low_cap_watch"]}, "revision": 7}}]):
            self.assertTrue(scanner.persist_low_cap_watch(state, config, iso(self.start)))
        self.assertEqual(config["_low_cap_watch_revision"], 7)

    def test_unverified_small_clock_cannot_retire_and_does_not_upload_a_huge_checkpoint(self):
        import scanner
        token = "CrJPSvj625TnPdWS42aG5ybMcHeFvnNqq5AExVespump"
        state = {"pools": {"pool": {"signal_thesis": {"token_address": token}}},
                 "market": {token: {"latest_mcap_usd": 19000,
                    "current_market_verified_at": iso(self.start + 24 * 3600)}}}
        state["token_low_cap_watch"] = {token: {"below_since": iso(self.start),
            "last_quote_at": iso(self.start + 23 * 3600), "samples": 24, "max_gap_seconds": 3600, "mcap_usd": 19000}}
        with patch.object(scanner, "load_alert_history", return_value=[]), \
             patch.object(scanner, "persist_low_cap_watch", return_value=False), \
             patch.object(scanner, "remote_api_call") as remote, \
             patch.object(scanner, "save_runtime_state") as save:
            scanner.update_token_retirements(state, [], {"token_retirement_enabled": True}, iso(self.start + 24 * 3600))
        remote.assert_not_called()
        self.assertFalse(state.get("token_retirements"))
        self.assertEqual(state["maintenance"]["token_retirement"]["clock_storage"], "pending")
        self.assertEqual(save.call_args.kwargs["sync"], False)

    def test_a_durable_due_clock_retries_on_the_same_still_fresh_quote(self):
        for hour in range(25):
            self.observe(hour)
        first = self.state["token_low_cap_watch"]["token"]["samples"]
        self.assertEqual(len(self.observe(24)), 1)
        self.assertEqual(self.state["token_low_cap_watch"]["token"]["samples"], first)

    def test_obsolete_checkpoint_does_not_download_its_hundreds_of_parts(self):
        import scanner
        from runtime_checkpoint import build_checkpoint, checkpoint_documents
        state = {"_runtime": {"updated_at": iso(self.start + 3600), "revision": 2}, "pools": {}}
        old, _ = checkpoint_documents(build_checkpoint({"_runtime": {"updated_at": iso(self.start), "revision": 1}, "pools": {}}))
        config = {}
        with patch.object(scanner, "remote_data_url_from_env", return_value="https://test"), \
             patch.object(scanner, "remote_ingest_secret", return_value="test-only"), \
             patch.object(scanner, "remote_api_call", return_value={"document": {"value": old}}) as remote, \
             patch.object(scanner, "hydrate_checkpoint", side_effect=AssertionError("stale parts must not be downloaded")):
            self.assertIs(scanner.load_runtime_checkpoint(state, config, "deep"), state)
        remote.assert_called_once()
        self.assertTrue(config["_runtime_deep_stale_download_skipped"])


if __name__ == "__main__":
    unittest.main()
