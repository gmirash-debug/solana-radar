import copy
import unittest
from unittest.mock import Mock, patch

import evidence_lifecycle as e
import scanner as s

START = "2026-08-01T00:00:00Z"


def at(days=0, hours=0):
    return e.iso(e.stamp(START) + days * 86400 + hours * 3600)


def thesis():
    return {"version": 2, "retention_evidence_version": s.RETENTION_EVIDENCE_VERSION,
        "signal_at": START, "captured_at": START, "token_address": "mint", "pool_address": "pool",
        "cohort_id": "cohort", "status": "unknown", "balance_status": "partial",
        "original_sale_history_status": "unknown", "original_sale_history_issues": ["history_gap"],
        "original_retained_tokens": 100, "cohort_token_coverage_pct": 100,
        "cohort_wallet_coverage_pct": 100, "supply": 1000,
        "cohort": [{"owner": "owner", "attributed_tokens": 100, "current_balance": 100,
                    "current_retained_tokens": 100, "checked_at": START}],
        "wallet_activity_checks": {"owner": {"events": {}, "pages": 0}}}


class EvidenceLifecycleTests(unittest.TestCase):
    def test_failed_attempt_times_and_empty_audits_do_not_reset_progress(self):
        t = thesis()
        e.ensure_health(t, START)
        for day in range(1, 31):
            t["updated_at"] = t["last_checked_at"] = at(day)
            t["wallet_activity_checks"]["owner"].update(last_attempt_at=at(day), consecutive_errors=day,
                query={"from": 1, "to": day, "cursor": None}, error_category="history_unavailable")
            self.assertFalse(e.record_attempt(t, {}, at(day), error="history_unavailable"))
        health = t["analysis_health"]
        self.assertEqual(health["last_progress_at"], START)
        self.assertEqual(health["last_attempt_at"], at(30))
        self.assertEqual(health["repeat_error_count"], 30)
        self.assertEqual(health["status"], "stalled")
        self.assertEqual(t["cohort"][0]["current_balance"], 100)
        self.assertEqual(t["status"], "unknown")

    def test_partial_new_balances_and_new_receipts_are_actual_progress(self):
        t = thesis(); e.ensure_health(t, START)
        t["cohort"].append({"owner": "new", "attributed_tokens": 1, "current_balance": None})
        self.assertTrue(e.record_attempt(t, {}, at(8)))
        t["wallet_activity_checks"]["owner"]["events"]["receipt"] = {"signature": "sig", "amount_raw": "10", "kind": "transferred"}
        self.assertTrue(e.record_attempt(t, {}, at(9)))
        self.assertEqual(t["analysis_health"]["last_progress_at"], at(9))
        self.assertEqual(t["analysis_health"]["status"], "active")
        self.assertEqual(t["status"], "unknown")

    def test_same_verified_balances_do_not_repair_stalled_history(self):
        t = thesis(); e.ensure_health(t, START)
        for day in range(1, 9):
            t["cohort"][0]["checked_at"] = at(day)
            self.assertFalse(e.record_attempt(t, {}, at(day), balance_success=True))
        self.assertEqual(t["analysis_health"]["status"], "stalled")
        self.assertEqual(t["analysis_health"]["last_verified_balance_at"], at(8))

    def test_unattempted_thesis_refresh_does_not_fake_verification_time(self):
        t = thesis(); t["last_checked_at"] = START
        e.ensure_health(t, START)["last_attempt_at"] = START
        with patch.object(s, "capture_signal_thesis", return_value=(t, False)), \
             patch.object(s, "update_thesis_position_observation"), \
             patch.object(s, "recheck_signal_thesis") as recheck:
            s.refresh_signal_thesis(Mock(), s.Pool("pool", token_address="mint"),
                {"signal_thesis": t}, [], {}, checked_at=at(8))
            recheck.assert_not_called()
        self.assertEqual(t["analysis_health"]["last_attempt_at"], START)
        self.assertEqual(t["analysis_health"]["last_progress_at"], START)

    def test_recovery_is_12_hourly_but_balances_keep_their_normal_interval(self):
        t = thesis(); e.ensure_health(t, START)
        self.assertTrue(e.history_due(t, {}, at(8)))
        e.mark_history_attempt(t, at(8))
        self.assertFalse(e.history_due(t, {}, at(8, 11)))
        self.assertTrue(e.history_due(t, {}, at(8, 12)))
        self.assertEqual(s.signal_thesis_recheck_interval_minutes(t, {}), 60)
        self.assertEqual(s.signal_thesis_recheck_interval_minutes(t, {"signal_thesis_priority_recheck_minutes": 30}), 30)

    def test_empty_recent_history_does_not_repair_missing_original_legacy_window(self):
        t = thesis(); t["original_sale_history_issues"] = ["legacy_original_sale_history_not_reconstructed"]
        e.ensure_health(t, START)
        for day in range(1, 9):
            t["wallet_activity_checks"]["owner"].update(complete_through=e.stamp(at(day)), checked_at=at(day))
            self.assertFalse(e.record_attempt(t, {}, at(day)))
        self.assertEqual(t["analysis_health"]["status"], "stalled")

    def test_successful_advancing_history_is_not_stalled(self):
        t = thesis(); e.ensure_health(t, START)
        t["wallet_activity_checks"]["owner"]["complete_through"] = e.stamp(at(8))
        self.assertTrue(e.record_attempt(t, {}, at(8)))
        self.assertEqual(t["analysis_health"]["status"], "active")

    def test_repeat_failures_do_not_emit_full_wallet_history_events(self):
        t = thesis(); state = {"pools": {"pool": {"signal_thesis": t}}, "market": {}}
        rpc = Mock(); rpc.token_balance.side_effect = RuntimeError("secret provider URL")
        for hour in range(1, 101):
            s.recheck_signal_thesis(rpc, s.Pool("pool", token_address="mint"), state["pools"]["pool"], {}, at(hours=hour))
            ledger = s.build_history_ledger({}, state, {}, at(hours=hour))
            self.assertEqual([row["event"]["event_type"] for row in ledger["events"]], ["signal"])
        self.assertEqual(t["analysis_health"]["repeat_error_count"], 100)
        self.assertNotIn("secret", str(s.public_signal_thesis(t)))

    def test_successful_unchanged_history_samples_daily_changed_balance_immediately(self):
        t = thesis(); state = {"pools": {"pool": {"signal_thesis": t}}}
        rpc = Mock(); rpc.token_balance.return_value = 100
        observed = []
        for hour in range(1, 26):
            s.recheck_signal_thesis(rpc, s.Pool("pool", token_address="mint"), state["pools"]["pool"], {}, at(hours=hour))
            checks = [row for row in s.build_history_ledger({}, state, {}, at(hours=hour))["events"]
                      if row["event"]["event_type"] == "retention_check"]
            if checks: observed.append(hour)
        self.assertEqual(observed, [1, 25])
        rpc.token_balance.return_value = 50
        s.recheck_signal_thesis(rpc, s.Pool("pool", token_address="mint"), state["pools"]["pool"], {}, at(hours=26))
        self.assertEqual(e.retention_event_at(t), at(hours=26))

    def heavy(self):
        t = thesis()
        t["wallet_activity_checks"]["owner"]["events"] = {
            str(i): {"signature": str(i), "kind": "transferred", "amount_raw": "1", "detail": "x" * 1024}
            for i in range(20)}
        e.ensure_health(t, START)
        return t

    def test_archive_failure_preserves_all_receipts_and_backoff(self):
        t = self.heavy(); before = copy.deepcopy(t["wallet_activity_checks"])
        state = {"pools": {"pool": {"signal_thesis": t}}}
        with patch("cold_evidence.archive_evidence", side_effect=RuntimeError("R2 paused")) as archive:
            s.maintain_analysis_archives(state, {}, at(31))
            s.maintain_analysis_archives(state, {}, at(31, 1))
        self.assertEqual(archive.call_count, 1)
        self.assertEqual(t["wallet_activity_checks"], before)
        self.assertEqual(t["analysis_health"]["status"], "archive_pending")
        self.assertTrue(state["maintenance"]["analysis_storage_health"]["warning"])

    def test_verified_archive_restore_merges_receipts_not_old_cursors_or_balances(self):
        t = self.heavy(); snapshot = e.cold_snapshot(t)
        state = {"pools": {"pool": {"signal_thesis": t}}}
        ref = {"key": "evidence/ref", "sha256": "a" * 64, "bytes": 100}
        initial_fingerprint = e.history_signature(t)
        with patch("cold_evidence.archive_evidence", return_value=ref) as archive:
            s.maintain_analysis_archives(state, {}, at(31))
        self.assertEqual(archive.call_count, 1)
        self.assertNotIn("events", t["wallet_activity_checks"]["owner"])
        self.assertEqual(e.history_signature(t), initial_fingerprint)
        t["wallet_activity_checks"]["owner"]["query"] = {"cursor": "new-hot-cursor"}
        t["cohort"][0]["current_balance"] = 75
        e.restore_archived_events(t, snapshot)
        self.assertEqual(len(t["wallet_activity_checks"]["owner"]["events"]), 20)
        self.assertEqual(t["wallet_activity_checks"]["owner"]["query"]["cursor"], "new-hot-cursor")
        self.assertEqual(t["cohort"][0]["current_balance"], 75)
        with patch("cold_evidence.archive_evidence") as archive:
            s.maintain_analysis_archives(state, {}, at(31, 1))
            archive.assert_not_called()
        self.assertTrue(t["analysis_health"]["_cold"])

    def test_archive_identity_mismatch_never_replaces_hot_receipts(self):
        t = self.heavy(); snapshot = e.cold_snapshot(t)
        e.prune_archived_events(t, {"bytes": 1}, at(31), e.digest(e.event_payload(t)))
        before = copy.deepcopy(t)
        snapshot["cohort_id"] = "other"
        with self.assertRaises(ValueError): e.restore_archived_events(t, snapshot)
        self.assertEqual(t, before)

    def test_cold_summaries_do_not_turn_into_zero_flows_or_trigger_every_scan(self):
        t = self.heavy()
        t["wallet_activity"] = {"status": "backfilling", "checked_at": START, "amounts_tokens": {"sold": 3}}
        e.prune_archived_events(t, {"bytes": 1}, at(31), e.digest(e.event_payload(t)))
        e.mark_history_attempt(t, at(31))
        state = {"pools": {"pool": {"signal_thesis": t}}}
        rpc = Mock()
        with patch.object(s, "hydrate_analysis_receipts") as hydrate:
            s.run_wallet_activity_tasks(rpc, [s.Pool("pool")], state, {}, at(31, 1))
            hydrate.assert_not_called()
        rpc._route_call.assert_not_called()
        self.assertEqual(t["wallet_activity"]["amounts_tokens"]["sold"], 3)
        self.assertEqual(t["wallet_activity"]["checked_at"], START)

    def test_warm_stalled_scheduler_waits_without_relabeling_old_summary_as_fresh(self):
        t = thesis(); e.ensure_health(t, START)
        t["wallet_activity"] = {"checked_at": START, "status": "backfilling"}
        state = {"pools": {"pool": {"signal_thesis": t}}}
        rpc = Mock(archive_order=["alchemy"])
        rpc._route_call.side_effect = RuntimeError("history unavailable")
        pool = s.Pool("pool")
        s.run_wallet_activity_tasks(rpc, [pool], state, {}, at(8))
        self.assertEqual(rpc._route_call.call_count, 1)
        for hour in range(1, 12):
            s.run_wallet_activity_tasks(rpc, [pool], state, {}, at(8, hour))
        self.assertEqual(rpc._route_call.call_count, 1)
        self.assertEqual(t["wallet_activity"]["checked_at"], at(8))
        s.run_wallet_activity_tasks(rpc, [pool], state, {}, at(8, 12))
        self.assertEqual(rpc._route_call.call_count, 2)

    def test_failed_archive_restore_never_requests_rpc_or_replaces_balances(self):
        t = self.heavy()
        e.prune_archived_events(t, {"bytes": 1}, at(31), e.digest(e.event_payload(t)))
        state = {"pools": {"pool": {"signal_thesis": t}}}
        rpc = Mock()
        with patch("cold_evidence.read_evidence", side_effect=RuntimeError("R2 paused")) as read:
            s.run_wallet_activity_tasks(rpc, [s.Pool("pool")], state, {}, at(32))
            s.run_wallet_activity_tasks(rpc, [s.Pool("pool")], state, {}, at(32, 1))
            self.assertEqual(read.call_count, 1)
        rpc._route_call.assert_not_called()
        self.assertEqual(t["cohort"][0]["current_balance"], 100)
        self.assertEqual(t["status"], "unknown")
        self.assertTrue(t["analysis_health"]["_cold"])

    def test_cold_receipts_do_not_hide_new_proven_sales(self):
        t = self.heavy(); before = e.history_signature(t)
        e.prune_archived_events(t, {"bytes": 1}, at(31), e.digest(e.event_payload(t)))
        self.assertEqual(e.history_signature(t), before)
        t["proven_sales"] = {"owner": 10}
        self.assertTrue(e.record_attempt(t, {}, at(32)))
        self.assertEqual(t["analysis_health"]["last_progress_at"], at(32))

    def test_growth_monitor_counts_stalls_and_preserves_original_cohort(self):
        t = self.heavy(); state = {"pools": {"pool": {"signal_thesis": t}}}
        e.storage_health(state, {}, START)
        summary = e.storage_health(state, {}, at(8))
        self.assertEqual(summary["stalled_count"], 1)
        self.assertEqual(summary["oldest_stalled_days"], 8)
        self.assertTrue(summary["warning"])
        s.compact_state(state, [], [], {}, at(100))
        self.assertEqual(state["pools"]["pool"]["signal_thesis"]["cohort"], t["cohort"])
        self.assertFalse(any(key.startswith("_") for key in e.public_health(t)))


if __name__ == "__main__":
    unittest.main()
