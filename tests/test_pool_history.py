import copy
import unittest
from unittest.mock import patch

import scanner as s
from gmgn_discovery import record_history_check
from pool_history import collect_history, merge_ranges, missing_ranges, missing_seconds, read_range


def tx(at, signature=None):
    return {"blockTime": at, "transaction": {"signatures": [signature or f"tx-{at}"]}}


class HistoryReader:
    def __init__(self, rows=(), replies=None):
        self.rows, self.calls = list(rows), []
        self.replies = iter(replies) if replies is not None else None

    def __call__(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if self.replies is not None:
            result = next(self.replies)
            if isinstance(result, Exception):
                raise result
            return result
        provider = kwargs.get("provider_name") or ("helius" if kwargs["history_task"] == "archive" else "alchemy")
        cursor = kwargs.get("pagination_token")
        if cursor:
            assert cursor.startswith(provider + ":")
        offset = int(cursor.split(":")[1]) if cursor else 0
        lower, upper = (kwargs["block_time"][key] for key in ("gte", "lte"))
        rows = sorted((row for row in self.rows if lower <= row["blockTime"] <= upper),
                      key=lambda row: row["blockTime"], reverse=True)
        limit = min(kwargs["limit"], 1000 if provider == "helius" else 100)
        batch = rows[offset:offset + limit]
        return {"data": batch, "paginationToken": f"{provider}:{offset + limit}" if offset + limit < len(rows) else None,
                "_provider": provider, "_page_limit": limit}


class PoolHistoryTests(unittest.TestCase):
    def collect(self, reader, state, now=20000, start=10000, **kwargs):
        return collect_history(state, now, start, reader, kwargs.pop("next_provider", None), **kwargs)

    def test_inclusive_interval_union_difference_and_empty_window(self):
        self.assertEqual(merge_ranges([[3, 5], [1, 2], [4, 7], [9, 9]]), [[1, 7], [9, 9]])
        self.assertEqual(missing_ranges([[1, 7], [9, 9]], 1, 10), [[8, 8], [10, 10]])
        self.assertEqual(missing_seconds([[1, 7], [9, 9]], 1, 10), 2)
        self.assertEqual(missing_ranges([], 3, 2), [])

    def test_without_alternative_provider_original_cursor_is_resumed_safely(self):
        reader = HistoryReader([tx(19900 - i) for i in range(350)])
        state = {}
        rows, stats = self.collect(reader, state)
        self.assertEqual(len(rows), 200)
        self.assertEqual(len(reader.calls), 2)
        self.assertEqual(reader.calls[1]["pagination_token"], "alchemy:100")
        # Existing provider-bound cursor is finished safely at that provider's cap.
        self.assertEqual(reader.calls[1]["provider_name"], "alchemy")
        self.assertTrue(stats["rolling_gap_pending"])
        self.assertFalse(stats["full_history_complete"])

    def test_archive_handoff_uses_large_page_only_for_unread_seconds(self):
        reader = HistoryReader([tx(19900 - i) for i in range(350)])
        state = {}
        rows, stats = self.collect(reader, state, next_provider=lambda excluded, task: "helius")
        self.assertEqual(len(rows), 350)
        self.assertEqual(reader.calls[1]["block_time"], {"gte": 10000, "lte": 19801})
        self.assertIsNone(reader.calls[1]["pagination_token"])
        self.assertEqual(reader.calls[1]["provider_name"], "helius")
        self.assertEqual(stats["providers_used"], ["alchemy", "helius"])
        self.assertTrue(stats["full_history_complete"])

    def test_initial_archive_can_start_at_large_page_without_cross_provider_cursor(self):
        reader = HistoryReader([tx(19900 - i) for i in range(350)])
        state = {"candidate_pool_history_version": 2,
                 "candidate_history_head_observed_to": 20000, "candidate_history_live_from": 10000}
        rows, stats = self.collect(reader, state, reuse_head=True)
        self.assertEqual(reader.calls[0]["limit"], 1000)
        self.assertIsNone(reader.calls[0]["pagination_token"])
        self.assertEqual(stats["providers_used"], ["helius"])
        self.assertEqual(len(rows), 350)
        self.assertTrue(stats["full_history_complete"])

    def test_new_head_does_not_restart_old_initial_window(self):
        reader = HistoryReader([tx(19900 - i) for i in range(1500)])
        state = {}
        self.collect(reader, state, head_only=True)
        original = copy.deepcopy(state["helius_rolling_backlogs"][0])
        reader.rows.extend(tx(20800 - i) for i in range(5))
        rows, stats = self.collect(reader, state, now=20900, head_only=True)
        self.assertEqual(reader.calls[1]["block_time"], {"gte": 19970, "lte": 20900})
        self.assertEqual(len(rows), 5)
        self.assertEqual(state["helius_rolling_backlogs"][0], original)
        self.assertTrue(stats["live_window_complete"])
        self.assertTrue(stats["extended_history_pending"])
        self.assertEqual(stats["history_gap_seconds"], 0)
        self.assertNotIn("candidate_history_complete_to", state)

    def test_cursor_and_upper_bound_survive_serialized_restart(self):
        reader = HistoryReader([tx(19900 - i) for i in range(350)])
        state = {}
        self.collect(reader, state, head_only=True)
        state = copy.deepcopy(state)
        self.collect(reader, state, now=20900, head_only=True)
        self.collect(reader, state, now=21000, reuse_head=True)
        archive = reader.calls[-1]
        self.assertEqual(archive["block_time"], {"gte": 10000, "lte": 20000})
        self.assertEqual(archive["pagination_token"], "alchemy:100")
        self.assertEqual(archive["history_task"], "archive")

    def test_same_second_is_not_covered_until_its_cursor_finishes(self):
        reader = HistoryReader([tx(19000, str(i)) for i in range(250)])
        state = {}
        self.collect(reader, state, head_only=True)
        self.assertEqual(missing_seconds(state["candidate_covered_ranges"], 19000, 19000), 1)
        self.collect(reader, state, reuse_head=True)
        self.assertEqual(missing_seconds(state["candidate_covered_ranges"], 19000, 19000), 1)
        _, stats = self.collect(reader, state, reuse_head=True)
        self.assertTrue(stats["full_history_complete"])
        self.assertEqual(state["candidate_history_complete_to"], 20000)

    def test_empty_page_with_cursor_does_not_acknowledge_gap_and_resets_stalled_cursor(self):
        reader = HistoryReader(replies=[{"data": [], "paginationToken": "same"}])
        _, covered, pending, stats = read_range(reader, None,
            {"from": 100, "to": 200, "cursor": "same", "provider": "helius"}, 1, 1000, "archive")
        self.assertEqual(covered, [])
        self.assertIsNone(pending["cursor"])
        self.assertTrue(stats["cursor_stalled"])
        self.assertFalse(stats["coverage_complete"])

    def test_short_page_with_cursor_only_covers_strictly_newer_seconds(self):
        reader = HistoryReader(replies=[{"data": [tx(150)], "paginationToken": "tail"}])
        _, covered, pending, stats = read_range(reader, None,
            {"from": 100, "to": 200}, 1, 100, "live")
        self.assertEqual(covered, [[151, 200]])
        self.assertEqual(pending["remaining_to"], 150)
        self.assertTrue(stats["pagination_remaining"])

    def test_true_empty_finished_range_is_complete(self):
        reader = HistoryReader()
        _, stats = self.collect(reader, {})
        self.assertTrue(stats["full_history_complete"])
        self.assertEqual(len(reader.calls), 1)

    def test_provider_failover_restarts_only_unread_part_and_preserves_provider_filters(self):
        reader = HistoryReader(replies=[{"data": [tx(180)], "paginationToken": "helius:next", "_provider": "helius"},
                                        RuntimeError("quota"), {"data": [tx(160)], "_provider": "alchemy"}])
        _, covered, pending, stats = read_range(reader, lambda excluded, task: "alchemy" if "helius" in excluded else None,
            {"from": 100, "to": 200}, 2, 1000, "archive")
        self.assertEqual(reader.calls[1]["pagination_token"], "helius:next")
        self.assertEqual(reader.calls[2]["block_time"], {"gte": 100, "lte": 180})
        self.assertIsNone(reader.calls[2]["pagination_token"])
        self.assertEqual(merge_ranges(covered), [[100, 200]])
        self.assertIsNone(pending)
        self.assertEqual(stats["pages"], 2)

    def test_unavailable_repair_preserves_head_progress_and_pending_cursor(self):
        state = {"candidate_pool_history_version": 2,
            "candidate_history_head_observed_to": 19000, "candidate_covered_ranges": [[18001, 19000]],
            "helius_rolling_backlogs": [{"from": 10000, "to": 19000, "remaining_to": 18000,
                                         "cursor": "helius:100", "provider": "helius"}]}
        reader = HistoryReader(replies=[{"data": [tx(19900)], "_provider": "alchemy"}, RuntimeError("quota")])
        _, stats = self.collect(reader, state)
        self.assertEqual(stats["repair_errors"], 1)
        self.assertTrue(stats["live_window_complete"])
        self.assertEqual(state["candidate_history_head_observed_to"], 20000)
        self.assertEqual(state["helius_rolling_backlogs"][0]["cursor"], "helius:100")

    def test_legacy_overlapping_tasks_replay_once_as_disjoint_unread_ranges(self):
        state = {"helius_rolling_backlogs": [{"from": 10000, "cursor": "a"}, {"from": 10000, "cursor": "b"}],
                 "candidate_covered_ranges": [[18000, 19000]]}
        reader = HistoryReader(replies=[{"data": [], "paginationToken": "new"}])
        self.collect(reader, state, head_only=True)
        self.assertEqual([(task["from"], task["remaining_to"]) for task in state["helius_rolling_backlogs"]],
                         [(10000, 17999), (19001, 20000)])
        self.assertTrue(all(not task.get("cursor") for task in state["helius_rolling_backlogs"]))

    def test_original_signal_range_is_repaired_before_unrelated_older_gap(self):
        state = {"candidate_pool_history_version": 2, "candidate_history_head_observed_to": 20000,
                 "candidate_history_live_from": 19970, "candidate_covered_ranges": [[15000, 16000], [19000, 20000]]}
        reader = HistoryReader()
        _, stats = self.collect(reader, state, reuse_head=True, priority_window=[17000, 18000])
        self.assertEqual(reader.calls[0]["block_time"], {"gte": 16001, "lte": 18999})
        self.assertEqual(stats["repair_priority"], "signal_window")
        self.assertTrue(stats["extended_history_pending"])

    def test_record_check_does_not_mark_old_gap_complete_when_live_head_is_complete(self):
        state = {}
        reader = HistoryReader([tx(19900 - i) for i in range(200)])
        self.collect(reader, state, head_only=True)
        _, stats = self.collect(reader, state, now=20900, head_only=True)
        record_history_check(state, {"trade_fetch": stats}, {}, 20900)
        self.assertTrue(state["candidate_history_gap_pending"])
        self.assertNotIn("candidate_history_complete_to", state)

    def test_history_snapshot_restores_every_new_cursor_and_coverage_field(self):
        state = {"candidate_history_start": 10000, "candidate_pool_history_version": 2,
                 "candidate_history_head_observed_to": 19000, "candidate_covered_ranges": [[15000, 19000]]}
        original = s.history_state_snapshot(state)
        self.collect(HistoryReader(), state)
        s.restore_history_state(state, original)
        self.assertEqual(s.history_state_snapshot(state), original)

    def test_programming_failure_is_not_masked_as_rpc_limit(self):
        with self.assertRaises(TypeError):
            self.collect(HistoryReader(replies=[TypeError("bad callback")]), {})

    def test_missing_and_out_of_range_timestamps_cannot_advance_coverage(self):
        for timestamp in (None, 9999, 20001):
            reader = HistoryReader(replies=[{"data": [tx(timestamp)], "_provider": "helius"}])
            state = {}
            with self.assertRaises(RuntimeError):
                self.collect(reader, state)
            self.assertNotIn("candidate_covered_ranges", state)

    def test_missing_signature_cannot_acknowledge_history(self):
        state = {}
        with self.assertRaises(RuntimeError):
            self.collect(HistoryReader(replies=[{"data": [{"blockTime": 19000}]}]), state)
        self.assertEqual(state, {})

    def test_parse_failure_restores_observed_watermark_and_repair_cursor(self):
        state = {"pools": {"pool": {"candidate_history_start":10000, "candidate_history_context_version":1}}}
        rpc = unittest.mock.Mock()
        rpc.transactions_for_address.return_value = {"data": [tx(19900)], "paginationToken":"tail"}
        config = {"discovery_source_mode":"gmgn_attention", "alert_window_minutes":360,
                  "_candidate_head_only":True, "helius_probe_max_pages":1,
                  "market_activity_consistency_enabled":False}
        original = s.history_state_snapshot(state["pools"]["pool"])

        def fail_parse(*args):
            rows, stats = s.fetch_helius_pool_transactions(rpc, s.Pool("pool"), config, state["pools"]["pool"], phase="probe")
            return [], {"parse_errors":1, "trade_fetch":stats}

        with patch.object(s.time,"time",return_value=20000), patch.object(s,"scan_pool_history",side_effect=fail_parse):
            s.scan_pool(rpc,s.Pool("pool"),config,state,{})
        self.assertEqual(s.history_state_snapshot(state["pools"]["pool"]),original)
        self.assertTrue(state["pools"]["pool"]["force_enhanced_next_scan"])

    def test_three_bounded_passes_repair_large_old_window_while_new_buys_continue(self):
        reader = HistoryReader([tx(19900 - i) for i in range(3000)])
        state, signatures = {}, set()
        for index in range(3):
            now = 20000 + index * 900
            if index:
                reader.rows.extend(tx(now - 10 - i) for i in range(5))
            rows, stats = self.collect(reader,state,now=now,next_provider=lambda excluded,task:"helius")
            signatures.update(row["transaction"]["signatures"][0] for row in rows)
            self.assertLessEqual(stats["pages"],2)
        self.assertEqual(len(signatures),3010)
        self.assertEqual(len(reader.calls),6)
        self.assertTrue(stats["full_history_complete"])
        self.assertEqual(reader.calls[2]["block_time"],{"gte":19970,"lte":20900})
        self.assertEqual(state["candidate_history_complete_to"],21800)

    def test_signature_fallback_cannot_acknowledge_an_enhanced_history_gap(self):
        state={"candidate_pool_history_version":2,"candidate_covered_ranges":[[19000,20000]],
               "helius_rolling_backlogs":[{"from":10000,"to":18999,"remaining_to":18999}]}
        record_history_check(state,{"trade_fetch":{"live_from":10000,"observed_to":21000}}, {},21000)
        self.assertEqual(state["candidate_covered_ranges"],[[19000,20000]])
        self.assertTrue(state["candidate_history_gap_pending"])
        self.assertFalse(state["candidate_history_live_window_complete"])
        self.assertNotIn("candidate_history_complete_to",state)

    def test_public_market_analysis_retains_separate_window_coverage_and_dates(self):
        state={"pools":{"pool":{"candidate_pool_history_version":2,"candidate_history_hours":6,
            "candidate_checked_at":s.iso(20000),"candidate_history_gap_pending":True,
            "candidate_history_live_window_complete":True,"candidate_history_live_from":19000,
            "candidate_history_head_observed_to":20000,"candidate_history_initial_pending":True,
            "candidate_history_extended_pending":True,"candidate_covered_ranges":[[19000,20000]],
            "signal_thesis":{"signal_window_start":s.iso(19500),"signal_window_end":s.iso(19600)},
            "helius_rolling_backlogs":[{"from":10000,"to":18999,"remaining_to":18999}]}}}
        s.record_market_observations(state,[s.Pool("pool",token_address="mint",market_snapshot_at=20000)],s.iso(20000))
        analysis=state["market"]["mint"]["candidate_analysis"]
        self.assertTrue(analysis["signal_window_complete"])
        self.assertTrue(analysis["live_window_complete"])
        self.assertTrue(analysis["initial_history_pending"])
        self.assertEqual(analysis["pending_ranges"],1)
        self.assertEqual(analysis["live_window_end"],s.iso(20000))

    def test_deep_final_state_does_not_report_a_closed_probe_as_pending(self):
        stats = s.combine_fetch_stats({"truncated": True, "backfill_pending": True},
            {"coverage_version": 2, "truncated": False, "backfill_pending": False, "rolling_backlog_segments_after": 0})
        self.assertFalse(stats["truncated"])
        self.assertFalse(stats["backfill_pending"])

    def test_quality_uses_exact_purchase_window_not_unrelated_initial_history(self):
        def alert(start, end):
            return {"lane": "reactivation", "action_tier": "watch", "signal_family": "reactivation_wave",
                    "window_start": s.iso(start), "window_end": s.iso(end),
                    "wave": {"balance_coverage_pct": 100, "owner_resolution_coverage_pct": 100}}
        alerts = [alert(19500, 19600), alert(17900, 18100)]
        s.apply_alert_data_quality(alerts, {"coverage_version": 2, "coverage_ranges": [[19000, 20000]],
            "observed_to": 20000, "live_truncated": True, "initial_history_pending": True}, 0, 0, 0, {})
        self.assertEqual(alerts[0]["data_quality"]["status"], "complete")
        self.assertTrue(alerts[0]["data_quality"]["initial_history_pending"])
        self.assertEqual(alerts[1]["data_quality"]["status"], "partial")
        self.assertEqual(alerts[1]["data_quality"]["signal_window_missing_seconds"], 201)
        self.assertEqual(alerts[0]["signal_confirmation"]["status"], "candidate")

    def test_invalid_signal_window_cannot_be_marked_complete(self):
        alert = {"window_start": s.iso(19500), "window_end": s.iso(20001), "wave": {
                 "balance_coverage_pct": 100, "owner_resolution_coverage_pct": 100}}
        s.apply_alert_data_quality([alert], {"coverage_version": 2, "observed_to": 20000,
            "coverage_ranges": [[10000, 20000]]}, 0, 0, 0, {})
        self.assertFalse(alert["data_quality"]["signal_window_complete"])


if __name__ == "__main__":
    unittest.main()
