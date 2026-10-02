import unittest
from unittest import mock

import scanner


class PositionIntegrationTests(unittest.TestCase):
    def fixture(self):
        thesis = {"cohort_id": "original", "signal_at": "2026-10-03T00:00:00Z",
                  "last_checked_at": "2026-10-03T00:00:00Z", "status": "intact",
                  "token_address": "mint", "token_retention_pct": 100,
                  "cohort": [{"owner": "owner", "current_retained_tokens": 100}]}
        return scanner.Pool(pool_address="pool", token_address="mint"), {"signal_thesis": thesis}

    def test_partial_activity_is_exported_without_changing_retention_or_confirmation(self):
        pool, state = self.fixture()
        rpc = mock.Mock()
        observed = {"status": "partial", "scope": "supplied_pool_transactions_strictly_after_signal",
                    "wallet_history_complete": False, "affects_original_cohort_retention": False,
                    "confirmation_eligible": False, "observations": [{"source_owner": "owner"}],
                    "owners": {"owner": {"direct_transfer_raw": "50"}}}
        txs, swaps = [{"transaction": {"signatures": ["sig"]}}], []
        with mock.patch.object(scanner, "annotate_pool_activity", return_value=observed) as annotate:
            public = scanner.refresh_signal_thesis(rpc, pool, state, [], {},
                checked_at="2026-10-03T01:00:00Z", observed_transactions=txs, observed_swaps=swaps)
        self.assertEqual(public["observed_position_activity"], observed)
        self.assertEqual(public["token_retention_pct"], 100)
        self.assertEqual(public["status"], "intact")
        self.assertNotIn("signal_confirmation", public)
        self.assertEqual(annotate.call_args.args, ("mint", txs, swaps, ["owner"]))
        self.assertIn("pool", annotate.call_args.kwargs["service_owners"])
        rpc.token_balance.assert_not_called()
        rpc.transactions_for_address.assert_not_called()

    def test_bad_observation_does_not_break_the_scan_or_change_cohort_state(self):
        pool, state = self.fixture()
        with mock.patch.object(scanner, "annotate_pool_activity", side_effect=ValueError("bad shape")):
            public = scanner.refresh_signal_thesis(mock.Mock(), pool, state, [], {},
                checked_at="2026-10-03T01:00:00Z", observed_transactions=[{}])
        self.assertEqual(public["status"], "intact")
        self.assertEqual(public["observed_position_activity"]["status"], "unavailable")
        self.assertFalse(public["observed_position_activity"]["confirmation_eligible"])

    def test_list_snapshot_excludes_activity_addresses_but_details_retain_them(self):
        thesis = {"token_address": "mint", "observed_position_activity": {
            "status": "partial", "owners": {"owner": {}}, "observations": [{"signature": "sig"}],
            "wallet_history_complete": False, "affects_original_cohort_retention": False}}
        compact = scanner.compact_signal_thesis_for_dashboard(thesis)
        activity = compact["observed_position_activity"]
        self.assertNotIn("owners", activity)
        self.assertNotIn("observations", activity)
        self.assertEqual(activity["observation_count"], 1)
        self.assertEqual(scanner.public_signal_thesis(thesis)["observed_position_activity"]["owners"], {"owner": {}})

    def test_only_observed_wave_buy_counts_are_preserved(self):
        for count, expected in ((4, 4), (0, 0), (None, None), (True, None), (-1, None)):
            alert = {"created_at": "2026-10-03T00:00:00Z", "pool": {"token_address": "mint"},
                     "wave": {"top_buyers": [{"owner": "owner", "token_bought": 100,
                         "current_balance": 100, "retained_from_wave": 100, "buy_sol": 1,
                         "buy_count": count}], "balance_coverage_pct": 100}}
            public = scanner.public_signal_thesis(scanner.signal_thesis_from_alert(alert, {}))
            self.assertEqual(public["cohort_wallets"][0].get("buy_count"), expected)


if __name__ == "__main__":
    unittest.main()
