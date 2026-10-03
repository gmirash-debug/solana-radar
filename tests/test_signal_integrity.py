import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import scanner as s


class SignalIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.now = s.parse_timestamp("2026-09-04T12:00:00Z")
        self.config = s.apply_lane(s.load_json(s.DEFAULT_CONFIG_PATH, {}), "reactivation")
        self.pool = s.Pool(pool_address="pool", token_address="token", mcap_usd=50_000,
                           liquidity_usd=10_000, volume_1h_usd=1000)

    def alert(self, owner="a", minutes=0):
        at = s.iso(self.now + minutes * 60)
        return {
            "lane": "reactivation", "signal_family": "reactivation_wave",
            "created_at": at, "window_start": at, "window_end": at,
            "action_tier": "watch", "pool": self.pool.as_dict(),
            "data_quality": {"status": "complete", "reasons": []},
            "reactivation_baseline": {"version": 2, "status": "ready", "reactivation_confirmed": True},
            "wallet_graph": {"checked_flow_coverage_pct": 100, "verified_effective_wallets": 6},
            "wave": {"hold_age_minutes": 60, "min_hold_minutes": 45,
                     "supply": 10_000, "balance_coverage_pct": 100,
                     "top_buyers": [{"owner": owner, "retained_from_wave": 100,
                                     "bought_tokens": 100, "current_balance": 100}]},
        }

    def test_one_dormant_buy_is_candidate_not_confirmed(self):
        events = [{"signer": "single", "wallet_class": "dormant", "sol_amount": 10,
                   "block_time": self.now - 60, "signature": "fake"}]
        with patch.object(s.time, "time", return_value=self.now):
            alerts = s.build_alerts(self.pool, events, self.config)
        s.apply_alert_data_quality(alerts, {}, 1, 1, 0, self.config)
        self.assertTrue(alerts)
        self.assertEqual(alerts[0]["action_tier"], "candidate")
        self.assertIsNone(alerts[0]["data_quality"]["balance_coverage_pct"])

    def test_only_complete_seasoned_verified_wave_confirms(self):
        good = self.alert()
        s.apply_signal_confirmation(good, self.config)
        self.assertEqual(good["signal_confirmation"]["status"], "confirmed")
        for field, replacement in (
            ("data_quality", {"status": "partial"}),
            ("reactivation_baseline", {"status": "warming"}),
            ("wallet_graph", {}),
            ("signal_family", "classified_wallets"),
        ):
            alert = self.alert()
            alert[field] = replacement
            alert["action_tier"] = "hot_reactivation"
            s.apply_signal_confirmation(alert, self.config)
            self.assertEqual(alert["action_tier"], "candidate", field)

    def test_missing_observations_are_not_quiet(self):
        entry = {"hourly": [[self.now - (i + 8) * 3600, 0, 0, 0, 100, 0, 1] for i in range(12)],
                 "last_snapshot_at": self.now - 8 * 3600, "quiet_since": self.now - 12 * 3600,
                 "latest_context": {"version": 2}}
        self.pool.volume_5m_usd = 300
        result = s.activity_context_from_history(entry, self.pool, self.now, self.config)
        self.assertEqual(result["quiet_hours"], 0)
        self.assertFalse(result["reactivation_confirmed"])

    def test_activation_memory_does_not_extend_itself(self):
        entry = {"hourly": [[self.now - (i + 1) * 3600, 0, 0, 0, 100, 0, 1] for i in range(12)],
                 "last_snapshot_at": self.now,
                 "latest_context": {"version": 2, "reactivation_confirmed": True,
                                    "activation_observed_at": s.iso(self.now), "quiet_hours": 8}}
        self.pool.volume_1h_usd = 0
        results = []
        for minutes in (30, 60, 90, 120):
            context = s.activity_context_from_history(entry, self.pool, self.now + minutes * 60, self.config)
            entry.update(latest_context=context, last_snapshot_at=self.now + minutes * 60)
            results.append(context["reactivation_confirmed"])
        self.assertEqual(results, [True, True, False, False])

    def test_retention_confirmation_requires_same_cohort_later_check(self):
        alert = self.alert()
        alert["wave"]["hold_age_minutes"] = 0
        s.apply_signal_confirmation(alert, self.config)
        state = {}
        s.capture_signal_thesis(state, [alert], self.config)
        rpc = Mock()
        rpc.token_balance.return_value = 100
        s.recheck_signal_thesis(rpc, self.pool, state, self.config, s.iso(self.now + 600))
        self.assertEqual(state["signal_thesis"]["signal_confirmation"]["status"], "candidate")
        s.recheck_signal_thesis(rpc, self.pool, state, self.config, s.iso(self.now + 3600))
        self.assertEqual(state["signal_thesis"]["signal_confirmation"]["status"], "confirmed")

    def test_distribution_cannot_confirm_unseasoned_candidate(self):
        alert = self.alert()
        alert["wave"]["hold_age_minutes"] = 0
        s.apply_signal_confirmation(alert, self.config)
        state = {}
        s.capture_signal_thesis(state, [alert], self.config)
        rpc = Mock()
        rpc.token_balance.return_value = 30
        s.recheck_signal_thesis(rpc, self.pool, state, self.config, s.iso(self.now + 3600))
        self.assertEqual(state["signal_thesis"]["signal_confirmation"]["status"], "candidate")

    def test_rotation_cannot_confirm_accumulation(self):
        alert = self.alert()
        alert["coordinated_activity"] = {"metrics": {"market_rotation_observations": 1}}
        s.apply_signal_confirmation(alert, self.config)
        self.assertEqual(alert["signal_confirmation"]["status"], "candidate")
        self.assertIn("rotation", " ".join(alert["signal_confirmation"]["reasons"]))

    def test_explicit_zero_is_not_resurrected_at_capture(self):
        alert = self.alert()
        alert["wave"]["top_buyers"][0]["retained_from_wave"] = 0
        thesis = s.signal_thesis_from_alert(alert, self.config)
        self.assertEqual(thesis["cohort"][0]["current_retained_tokens"], 0)

    def test_unchecked_initial_zero_does_not_freeze_fake_sale(self):
        alert = self.alert()
        alert["wave"]["balance_coverage_pct"] = 0
        alert["wave"]["top_buyers"][0].update(current_balance=0, retained_from_wave=0, balance_verified=False)
        thesis = s.signal_thesis_from_alert(alert, self.config)
        self.assertEqual(thesis["status"], "unknown")
        self.assertEqual(thesis["balance_coverage_pct"], 0)
        rpc = Mock()
        rpc.token_balance.return_value = 100
        s.recheck_signal_thesis(rpc, self.pool, {"signal_thesis": thesis}, self.config, s.iso(self.now + 3600))
        self.assertEqual(thesis["current_retained_tokens"], 100)

    def test_rebuy_does_not_restore_original_inventory(self):
        thesis = s.signal_thesis_from_alert(self.alert(), self.config)
        state = {"signal_thesis": thesis}
        rpc = Mock()
        rpc.token_balance.return_value = 40
        s.recheck_signal_thesis(rpc, self.pool, state, self.config, s.iso(self.now + 600))
        rpc.token_balance.return_value = 200
        s.recheck_signal_thesis(rpc, self.pool, state, self.config, s.iso(self.now + 1200))
        self.assertEqual(thesis["current_retained_tokens"], 40)
        self.assertEqual(thesis["cohort"][0]["current_balance"], 200)
        self.assertEqual(thesis["cohort"][0]["movement_status"], "reduced_unresolved")

    def test_missing_or_invalid_balances_are_not_sold_positions(self):
        for value in (None, True, float("nan"), float("inf"), -1):
            state = {"signal_thesis": s.signal_thesis_from_alert(self.alert(), self.config)}
            rpc = Mock()
            rpc.token_balance.return_value = value
            s.recheck_signal_thesis(rpc, self.pool, state, self.config, s.iso(self.now + 600))
            self.assertEqual(state["signal_thesis"]["status"], "unknown")
            self.assertEqual(state["signal_thesis"]["balance_errors"], 1)

    def test_repeated_same_snapshot_cannot_count_as_two_complete_checks(self):
        state = {"signal_thesis": s.signal_thesis_from_alert(self.alert(), self.config)}
        rpc = Mock()
        rpc.token_balance.return_value = 0
        for _ in range(2):
            s.recheck_signal_thesis(rpc, self.pool, state, self.config, s.iso(self.now + 600))
        self.assertEqual(state["signal_thesis"]["invalidation_streak"], 1)
        self.assertNotEqual(state["signal_thesis"]["status"], "invalidated")

    def test_new_confirmed_wave_replaces_candidate_not_its_metadata(self):
        old, new = self.alert("old"), self.alert("new", 60)
        old["data_quality"]["status"] = "partial"
        for alert in (old, new):
            s.apply_signal_confirmation(alert, self.config)
        state = {}
        s.capture_signal_thesis(state, [old], self.config)
        s.capture_signal_thesis(state, [new], self.config)
        thesis = state["signal_thesis"]
        self.assertEqual(thesis["cohort"][0]["owner"], "new")
        self.assertEqual(thesis["signal_at"], new["created_at"])

    def test_confirmed_original_cohort_is_not_replaced_by_later_wave(self):
        old, new = self.alert("old"), self.alert("new", 60)
        for alert in (old, new):
            s.apply_signal_confirmation(alert, self.config)
        state = {}
        s.capture_signal_thesis(state, [old], self.config)
        s.capture_signal_thesis(state, [new], self.config)
        self.assertEqual(state["signal_thesis"]["cohort"][0]["owner"], "old")
        self.assertEqual(state["signal_thesis"]["signal_confirmation"]["checked_at"], old["created_at"])
        later_candidate = self.alert("third", 120)
        later_candidate["data_quality"]["status"] = "partial"
        s.apply_signal_confirmation(later_candidate, self.config)
        s.capture_signal_thesis(state, [later_candidate], self.config)
        self.assertEqual(state["signal_thesis"]["source_tier"], "watch")
        self.assertEqual(state["signal_thesis"]["cohort"][0]["owner"], "old")

    def test_method_plan_restriction_is_not_global_auth_failure(self):
        provider = object.__new__(s.SolanaRpcProvider)
        self.assertEqual(provider.error_category(status=403, detail="only available on dedicated nodes"), "unsupported")
        self.assertEqual(provider.error_category(status=403, detail="indexed requests require a personal token"), "unsupported")
        self.assertEqual(provider.error_category(status=401, detail="invalid api key"), "auth")

    def test_late_checkpoint_excluded_and_price_preferred_to_supply_change(self):
        checkpoint = {"at": s.iso(self.now), "target_at": s.iso(self.now - 48 * 3600)}
        self.assertFalse(s.outcome_checkpoint_eligible(checkpoint))
        checkpoint["target_at"] = s.iso(self.now - 3599)
        self.assertTrue(s.outcome_checkpoint_eligible(checkpoint))
        self.assertEqual(s.outcome_return_pct({"caught_price_usd": 1, "caught_mcap_usd": 100},
                                             {"price_usd": 2, "mcap_usd": 100}), 100)

    def test_failed_cloud_write_is_replayed_and_removed_only_after_ack(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(s, "REMOTE_OUTBOX_DIR", Path(directory)), \
             patch.object(s, "remote_data_url_from_env", return_value="https://example.invalid"), \
             patch.object(s, "remote_ingest_secret", return_value="test"), \
             patch.object(s, "build_dashboard_snapshot", side_effect=lambda report, *args, **kwargs: {"report": report}), \
             patch.object(s, "remote_api_call") as remote:
            remote.side_effect = RuntimeError("quota exceeded")
            old = {"generated_at": s.iso(self.now)}
            self.assertEqual(s.sync_remote_snapshot(old, {}, {})["pending"], 1)
            remote.side_effect = None
            remote.return_value = {"ok": True}
            new = {"generated_at": s.iso(self.now + 3600)}
            result = s.sync_remote_snapshot(new, {}, {})
            self.assertEqual(result["status"], "synced")
            self.assertEqual(remote.call_args_list[-2].args[3]["report"]["generated_at"], new["generated_at"])
            self.assertEqual(remote.call_args_list[-1].args[3]["report"]["generated_at"], old["generated_at"])
            self.assertFalse(list(Path(directory).glob("*.gz")))
            self.assertTrue((Path(directory) / ".keep").exists())

    def test_balance_monitor_is_bounded_oldest_first_and_skips_fresh(self):
        pools = [s.Pool(pool_address=name, token_address=name) for name in ("fresh", "old", "recent")]
        state = {"pools": {}}
        for pool, age in zip(pools, (60, 7200, 4000)):
            thesis = s.signal_thesis_from_alert(self.alert(), self.config)
            thesis["last_checked_at"] = s.iso(self.now - age)
            state["pools"][pool.pool_address] = {"signal_thesis": thesis}
        rpc = Mock()
        rpc.token_balance.return_value = 100
        result = s.monitor_due_cohorts(rpc, pools, state, {**self.config, "signal_thesis_extra_balance_budget": 1}, s.iso(self.now))
        self.assertEqual(result, {"due": 2, "checked": 1, "balance_requests": 1, "deferred": 1})
        self.assertEqual(rpc.token_balance.call_args.args[1], "old")

    def test_cloud_batches_resume_only_unacknowledged_evidence(self):
        body = {"report": {"generated_at": s.iso(self.now)},
                "detail_signal_theses": [{"token_address": str(i)} for i in range(61)],
                "market": {str(i): {} for i in range(26)},
                "history_ledger": {"events": [{"event_id": str(i),
                    "episode": {"episode_id": "episode", "token_address": "mint", "caught_at": s.iso(self.now)},
                    "event": {"event_type": "signal", "observed_at": s.iso(self.now)}} for i in range(26)]}}
        with patch.object(s, "remote_api_call") as remote:
            remote.side_effect = [{"ok": True}, {"ok": True}, {"ok": True}, {"ok": True}, RuntimeError("temporary quota")]
            with self.assertRaisesRegex(RuntimeError, "quota"):
                s.send_remote_snapshot(body, {})
            self.assertEqual(body["_sync_progress"]["detail_signal_theses"], 25)
            self.assertEqual(body["_sync_progress"]["summary"], 1)
            self.assertEqual(body["_sync_progress"]["durable_history_ledger"], 26)
            remote.reset_mock()
            remote.side_effect = None
            remote.return_value = {"ok": True}
            s.send_remote_snapshot(body, {})
            self.assertEqual(remote.call_args_list[0].args[1], "/api/ingest/details")
            self.assertEqual(remote.call_args_list[0].args[3]["detail_signal_theses"][0]["token_address"], "25")
            self.assertEqual(len(remote.call_args_list), 4)
            for call in remote.call_args_list:
                batch = call.args[3]
                count = sum(len(batch.get(field) or []) for field in ("detail_signal_theses", "market"))
                count += len((batch.get("history_ledger") or {}).get("events") or [])
                self.assertLessEqual(count, 25)

    def test_poison_old_report_does_not_block_current_report(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(s, "REMOTE_OUTBOX_DIR", Path(directory)), \
             patch.object(s, "remote_data_url_from_env", return_value="https://example.invalid"), \
             patch.object(s, "remote_ingest_secret", return_value="test"), \
             patch.object(s, "build_dashboard_snapshot", side_effect=lambda report, *a, **kw: {"report": report}), \
             patch.object(s, "remote_api_call") as remote:
            old = {"generated_at": s.iso(self.now)}
            new = {"generated_at": s.iso(self.now + 3600)}
            remote.side_effect = RuntimeError("old oversized report")
            s.sync_remote_snapshot(old, {}, {})
            def write(_method, _path, _config, body):
                if body["report"]["generated_at"] == old["generated_at"]:
                    raise RuntimeError("old oversized report")
                return {"ok": True}
            remote.side_effect = write
            result = s.sync_remote_snapshot(new, {}, {})
            self.assertTrue(result["current_synced"])
            self.assertEqual(result["pending"], 1)

    def test_cloud_sync_budget_defers_without_acknowledging(self):
        body = {"report": {"generated_at": s.iso(self.now)}}
        with patch.object(s.time, "monotonic", return_value=100), patch.object(s, "remote_api_call") as remote:
            with self.assertRaisesRegex(RuntimeError, "time budget"):
                s.send_remote_snapshot(body, {}, deadline=100)
            remote.assert_not_called()
            self.assertNotIn("summary", body["_sync_progress"])

    def test_gmgn_rate_limit_opens_circuit_and_removes_signing_key(self):
        config = {"gmgn_enabled": True}
        with patch.dict(s.os.environ, {"GMGN_API_KEY": "test-api", "GMGN_PRIVATE_KEY": "never-use", "GMGN_DEBUG": "1"}), \
             patch.object(s, "gmgn_cli_command", return_value=["gmgn-cli"]), \
             patch.object(s.subprocess, "run", return_value=Mock(returncode=1, stderr="429 test-api", stdout="")) as run:
            with self.assertRaisesRegex(RuntimeError, "429 \\*\\*\\*"):
                s.run_gmgn_cli(config, ["market", "trending"], "test")
            self.assertTrue(config["_gmgn_circuit_open"])
            env = run.call_args.kwargs["env"]
            self.assertNotIn("GMGN_PRIVATE_KEY", env)
            self.assertNotIn("GMGN_DEBUG", env)
            self.assertEqual(env["GMGN_RATE_LIMIT_AUTO_RETRY_MAX_WAIT_MS"], "0")
            with self.assertRaisesRegex(RuntimeError, "circuit"):
                s.run_gmgn_cli(config, ["token", "info"], "test")
            self.assertEqual(run.call_count, 1)

    def test_gmgn_requests_are_paced(self):
        config = {"_gmgn_next_request_at": 100.8}
        with patch.dict(s.os.environ, {"GMGN_API_KEY": "test-api"}), \
             patch.object(s, "gmgn_cli_command", return_value=["gmgn-cli"]), \
             patch.object(s.time, "monotonic", return_value=100), \
             patch.object(s.time, "sleep") as sleep, \
             patch.object(s.subprocess, "run", return_value=Mock(returncode=0, stdout='{"ok":true}')):
            self.assertEqual(s.run_gmgn_cli(config, ["market", "trending"], "test"), {"ok": True})
            self.assertAlmostEqual(sleep.call_args.args[0], 0.8)
