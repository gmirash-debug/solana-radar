import unittest
from unittest import mock

import scanner


class HistoryRpc:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def transactions_for_address(self, _address, **kwargs):
        self.calls.append(kwargs)
        return next(self.pages)


def page(signature, timestamp, cursor=None):
    return {"data": [{"blockTime": timestamp, "transaction": {"signatures": [signature]}}],
            "paginationToken": cursor}


class HistoryCoverageTests(unittest.TestCase):
    config = {"alert_window_minutes": 240, "helius_transactions_limit": 100,
              "helius_probe_incremental_pages": 1, "helius_probe_rolling_backlog_pages": 1,
              "helius_dynamic_page_budget_enabled": False, "helius_initial_backfill_enabled": False,
              "market_activity_consistency_enabled": False}

    def fetch(self, rpc, state, **config):
        with mock.patch.object(scanner.time, "time", return_value=20000):
            return scanner.fetch_helius_pool_transactions(
                rpc, scanner.Pool(pool_address="pool"), {**self.config, **config}, state, phase="probe")

    def test_short_page_with_cursor_is_not_complete(self):
        rpc = HistoryRpc([page("new", 19990, "more")])
        state = {"helius_latest_block_time": 19000}
        _, stats = self.fetch(rpc, state)
        self.assertTrue(stats["live_truncated"])
        self.assertNotIn("live_checkpoint", stats)
        self.assertEqual(state["helius_rolling_backlogs"][0]["cursor"], "more")

    def test_empty_page_with_cursor_preserves_pending_gap(self):
        rpc = HistoryRpc([{"data": [], "paginationToken": "more"}])
        _, stats = self.fetch(rpc, {"helius_latest_block_time": 19000})
        self.assertTrue(stats["live_truncated"])
        self.assertEqual(len(rpc.calls), 1)

    def test_backlog_compaction_keeps_oldest_repair_and_full_lower_bound(self):
        state = {"helius_latest_block_time": 10000, "helius_rolling_backlogs": [
            {"provider": "helius", "cursor": "old", "from": 10000, "head_block_time": 11000},
            {"provider": "helius", "cursor": "mid", "from": 11000, "head_block_time": 12000},
            {"provider": "helius", "cursor": "newer", "from": 12000, "head_block_time": 13000},
        ]}
        rpc = HistoryRpc([page("head", 19990, "head-tail"), page("repair", 10500, "old-next")])
        _, stats = self.fetch(rpc, state, helius_rolling_backlog_max_segments=2)
        pending = state["helius_rolling_backlogs"]
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0]["cursor"], "old-next")
        self.assertEqual(pending[1]["cursor"], "head-tail")
        self.assertEqual(pending[1]["from"], 9970)
        self.assertTrue(stats["rolling_gap_pending"])
        self.assertNotIn("rolling_backlog_segments_dropped", stats)

    def test_idle_pool_still_repairs_pending_history_without_activity_probe(self):
        rpc = mock.Mock()
        active, reason = scanner.pool_has_new_activity(
            rpc, scanner.Pool(pool_address="pool", txns_1h=0),
            {"latest_signature": "same", "helius_rolling_backlogs": [{"cursor": "old"}]}, {})
        self.assertTrue(active)
        self.assertEqual(reason["reason"], "history_gap_repair")
        rpc.signatures_for_address.assert_not_called()

    def test_final_deep_state_overrides_closed_probe_gap_but_keeps_other_errors(self):
        probe = {"live_truncated": True, "rolling_gap_pending": True,
                 "rolling_backlog_segments_after": 2, "history_gap_seconds": 600,
                 "transaction_errors": 1}
        deep = {"live_truncated": False, "rolling_gap_pending": False,
                "rolling_backlog_segments_after": 0, "history_gap_seconds": 0}
        final = scanner.combine_fetch_stats(probe, deep)
        self.assertFalse(final["live_truncated"])
        self.assertFalse(final["rolling_gap_pending"])
        self.assertEqual(final["rolling_backlog_segments_after"], 0)
        self.assertEqual(final["history_gap_seconds"], 0)
        self.assertEqual(final["transaction_errors"], 1)

    def test_recovery_limit_gap_is_not_erased_by_closed_deep_queue(self):
        final = scanner.combine_fetch_stats(
            {"history_gap_seconds": 600, "live_truncated": True},
            {"rolling_backlog_segments_after": 0, "history_gap_seconds": 600, "live_truncated": False})
        self.assertEqual(final["history_gap_seconds"], 600)


if __name__ == "__main__":
    unittest.main()
