import copy
import socket
import unittest
from unittest.mock import Mock, patch

import scanner as s


class CohortOutflowStatesTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.at = "2026-10-04T12:00:00Z"
        self.later = "2026-10-04T13:00:00Z"
        self.pool = s.Pool("pool", token_address="mint")

    def alert(self, size=60):
        return {"created_at": self.at, "window_start": "2026-10-04T11:00:00Z",
            "window_end": self.at, "signal_family": "reactivation_wave",
            "signal_confirmation": {"status": "candidate", "reasons": ["history incomplete"]},
            "pool": {"pool_address": "pool", "token_address": "mint"},
            "wave": {"balance_coverage_pct": 100, "checked_wallets": size,
                "checked_bought_tokens": size * 100, "supply": 100_000,
                "top_buyers": [{"owner": f"buyer-{i}", "token_bought": 100,
                    "retained_from_wave": 100, "current_balance": 100, "balance_verified": True,
                    "first_buy_time": "2026-10-04T11:30:00Z"} for i in range(size)]}}

    def test_full_verified_capture_is_saved_even_with_a_smaller_recheck_batch(self):
        thesis = s.signal_thesis_from_alert(self.alert(), {"signal_thesis_wallet_limit": 40})
        self.assertEqual(len(thesis["cohort"]), 60)
        self.assertEqual(thesis["cohort_wallet_coverage_pct"], 100)
        self.assertEqual(thesis["cohort_token_coverage_pct"], 100)
        rpc = Mock()
        rpc.token_balance.return_value = 100
        state = {"signal_thesis": thesis}
        s.recheck_signal_thesis(rpc, self.pool, state, {}, self.later)
        self.assertEqual(rpc.token_balance.call_count, 40)
        self.assertEqual(thesis["check_cycle_pending_wallets"], 20)
        self.assertEqual(thesis["balance_status"], "partial")
        s.recheck_signal_thesis(rpc, self.pool, state, {}, "2026-10-04T13:05:00Z")
        self.assertEqual(rpc.token_balance.call_count, 60)
        self.assertEqual(thesis["balance_status"], "present")
        self.assertEqual(thesis["signal_confirmation"]["status"], "candidate")

    def test_classified_capture_does_not_drop_buyers_at_the_balance_batch_limit(self):
        alert = self.alert()
        alert.pop("wave")
        alert["suspicious_wallets"] = 60
        alert["events"] = [{"kind": "buy", "token_recipient": f"buyer-{i}",
            "wallet_class": "dormant", "token_amount": 100} for i in range(60)]
        self.assertEqual(len(s.signal_thesis_from_alert(alert, {"signal_thesis_wallet_limit": 40})["cohort"]), 60)

    def test_checked_balances_do_not_clear_unknown_sale_history_or_confirm(self):
        thesis = s.signal_thesis_from_alert(self.alert(3), {})
        s.mark_thesis_sale_history_unknown(thesis, "history_gap")
        rpc = Mock()
        rpc.token_balance.return_value = 100
        s.recheck_signal_thesis(rpc, self.pool, {"signal_thesis": thesis}, {}, self.later)
        self.assertEqual(thesis["status"], "unknown")
        self.assertEqual(thesis["balance_status"], "present")
        self.assertEqual(thesis["original_sale_history_status"], "unknown")
        self.assertEqual(thesis["signal_confirmation"]["status"], "candidate")

    def test_only_balance_based_legacy_closures_reopen_without_restoring_caps(self):
        thesis = s.signal_thesis_from_alert(self.alert(3), {})
        thesis.pop("position_evidence_version")
        thesis.update(status="invalidated", invalidation_candidate=True, invalidation_streak=2,
            token_retention_pct=0, current_retained_tokens=0, holder_retention_pct=0)
        thesis["cohort"][0]["retention_cap_tokens"] = 0
        state = {"pools": {"pool": {"signal_thesis": thesis}}}
        s.migrate_scanner_state(state)
        self.assertEqual(thesis["status"], "weakening")
        self.assertEqual(thesis["balance_status"], "outflow")
        self.assertEqual(thesis["legacy_balance_invalidation"]["status"], "invalidated")
        self.assertEqual(thesis["cohort"][0]["retention_cap_tokens"], 0)
        self.assertIn("signal_recheck_due_at", state["pools"]["pool"])
        thesis.pop("position_evidence_version")
        thesis.update(status="invalidated", invalidation_candidate=False, reason="explicit manual close")
        self.assertFalse(s.prepare_thesis_position_evidence(thesis))
        self.assertEqual(thesis["status"], "invalidated")

    def truncated(self):
        thesis = s.signal_thesis_from_alert(self.alert(), {})
        thesis["cohort"] = thesis["cohort"][:40]
        thesis.update(original_wallets=40, original_retained_tokens=4000,
            original_attributed_tokens=4000, cohort_wallet_coverage_pct=100 * 40 / 60,
            cohort_token_coverage_pct=100 * 40 / 60)
        thesis["cohort"][0]["retention_cap_tokens"] = 0
        return thesis

    def test_recovery_requires_the_exact_original_window_and_preserves_existing_caps(self):
        thesis = self.truncated()
        cohort_id = thesis["cohort_id"]
        self.assertTrue(s.recover_original_cohort(thesis, [self.alert()], {}, self.later))
        self.assertEqual(thesis["cohort_id"], cohort_id)
        self.assertEqual(len(thesis["cohort"]), 60)
        self.assertEqual(thesis["cohort"][0]["retention_cap_tokens"], 0)
        self.assertEqual(thesis["balance_coverage_pct"], 0)
        self.assertIsNone(thesis["token_retention_pct"])
        self.assertIsNone(thesis["last_checked_at"])
        self.assertEqual(thesis["original_sale_history_status"], "unknown")
        self.assertEqual(thesis["original_retained_tokens"], 6000)
        self.assertFalse(s.recover_original_cohort(thesis, [self.alert()], {}, self.later))

    def test_recovery_rejects_later_buyers_mismatched_amounts_and_incomplete_copies(self):
        original = self.alert()
        for mutate in (
            lambda a: a.update(created_at=self.later),
            lambda a: a.update(window_end=self.later),
            lambda a: a["pool"].update(token_address="other"),
            lambda a: a["wave"]["top_buyers"][0].update(token_bought=200),
            lambda a: a["wave"]["top_buyers"][59].update(first_buy_time=self.later),
            lambda a: a["wave"].update(top_buyers=a["wave"]["top_buyers"][:40]),
        ):
            alert = copy.deepcopy(original)
            mutate(alert)
            thesis = self.truncated()
            self.assertFalse(s.recover_original_cohort(thesis, [alert], {}, self.later))
            self.assertEqual(len(thesis["cohort"]), 40)

    def test_transfer_receipts_are_deduplicated_bounded_and_never_sales(self):
        thesis = s.signal_thesis_from_alert(self.alert(3), {})
        at = s.parse_timestamp(self.later)
        thesis["observed_position_activity"] = {"mint": "mint",
            "scope": "supplied_pool_transactions_strictly_after_signal", "checked_timestamp": at,
            "observations": [{"signature": f"transfer-{i}", "source_owner": "buyer-0",
                "timestamp": at - 10, "resolution": "direct_transfer_not_ownership"} for i in range(3)]}
        for _ in range(2):
            s.update_thesis_position_observation(thesis, {"signal_thesis_movement_receipt_limit": 2})
        facts = s.public_signal_thesis(thesis)["outflow_evidence"]
        self.assertEqual(facts["direct_transfer_transactions"], 2)
        self.assertEqual(facts["observed_sale_transactions"], 0)
        self.assertTrue(facts["receipt_limit_reached"])
        self.assertFalse(facts["original_sales_proven"])
        self.assertEqual(thesis["token_retention_pct"], 100)
        self.assertNotIn("cohort_movement_events", s.public_signal_thesis(thesis))

    def test_sales_and_transfer_observations_do_not_close_or_restore_original_inventory(self):
        thesis = s.signal_thesis_from_alert(self.alert(3), {})
        swaps = [{"kind": "sell", "signature": f"sale-{i}", "block_time": s.parse_timestamp(self.at) + 10,
            "coordination_sale_owner": f"buyer-{i}", "coordination_sale_amount": 100, "token_amount": 100} for i in range(3)]
        rpc = Mock()
        rpc.token_balance.return_value = 100  # Later rebuy is not the original lot.
        state = {"signal_thesis": thesis}
        for at in (self.later, "2026-10-04T14:00:00Z"):
            s.record_thesis_sales(thesis, self.pool, swaps, at)
            s.recheck_signal_thesis(rpc, self.pool, state, {}, at)
        self.assertEqual(thesis["status"], "weakening")
        self.assertEqual(thesis["balance_status"], "outflow")
        self.assertEqual(thesis["current_retained_tokens"], 0)
        self.assertEqual(thesis["outflow_evidence"]["observed_sale_transactions"], 3)
        self.assertFalse(thesis["invalidation_candidate"])


if __name__ == "__main__":
    unittest.main()
