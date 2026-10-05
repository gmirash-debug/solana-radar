import copy
import json
import unittest
from unittest.mock import Mock, patch

import gmgn_discovery as g
import scanner as s

TOKEN = "So11111111111111111111111111111111111111112"
OTHER = "3Ydb2n8vAFdBJYpdiZEoDxRXLmiGDY1fuizVmMPMpump"
NOW = 1791194400


class AttentionTests(unittest.TestCase):
    def response(self, config, args, label):
        if args[1] == "trending":
            return {"data": {"rank": [{"address": TOKEN, "chain": "sol", "volume": "100"},
                {"address": "<script>"}, {"address": OTHER, "chain": "eth"}]}}
        params = json.loads(args[args.index("--params") + 1])
        return {"data": [{"chain": "sol", "interval": item["interval"], "tokens": [
            {"address": TOKEN, "rank": 3}, {"address": OTHER, "rank": 4}]} for item in params]}

    def test_union_all_windows_one_hot_batch_no_age_or_platform_filters(self):
        run = Mock(side_effect=self.response)
        records, health = g.fetch_attention({}, run)
        self.assertEqual(set(records), {TOKEN, OTHER})
        self.assertEqual(len(records[TOKEN]["memberships"]), 10)
        self.assertTrue(records[TOKEN]["in_both"])
        self.assertEqual(records[OTHER]["sources"], ["hot_searches"])
        self.assertEqual(health["status"], "ok")
        self.assertEqual(run.call_count, 6)
        for call in run.call_args_list:
            self.assertNotIn("--platform", call.args[1])
            self.assertNotIn("--min-created", call.args[1])

    def test_valid_empty_is_not_an_error_and_missing_blocks_are_not_success(self):
        def empty(config, args, label):
            if args[1] == "trending":
                return {"data": {"rank": []}}
            return {"data": []}
        records, health = g.fetch_attention({}, empty)
        self.assertEqual(records, {})
        self.assertEqual(health["status"], "partial")
        self.assertEqual(len(health["errors"]), 5)

    def test_failed_source_does_not_exclude_other_source(self):
        def partially(config, args, label):
            if args[1] == "trending":
                raise RuntimeError("transient failure")
            return self.response(config, args, label)
        records, health = g.fetch_attention({}, partially)
        self.assertEqual(health["status"], "partial")
        self.assertEqual(records[TOKEN]["sources"], ["hot_searches"])

    def test_first_seen_stable_and_stale_membership_expires(self):
        records, health = g.fetch_attention({}, self.response)
        health["observed_at"] = g.iso(NOW)
        for record in records.values():
            record.update(first_seen_at=g.iso(NOW), last_seen_at=g.iso(NOW))
        state = {}
        g.merge_candidates(state, records, health, {})
        later = copy.deepcopy(records)
        later[TOKEN].update(first_seen_at=g.iso(NOW + 300), last_seen_at=g.iso(NOW + 300))
        g.merge_candidates(state, later, {**health, "observed_at": g.iso(NOW + 300)}, {})
        self.assertEqual(state["gmgn_candidates"][TOKEN]["first_seen_at"], g.iso(NOW))
        self.assertEqual(g.current_candidates(state, {}, NOW + 4000), {})

    def test_only_gmgn_tokens_resolved_and_unrelated_pools_not_admitted(self):
        config = {"discovery_source_mode": "gmgn_attention", "_active_runtime_state": {}}
        with patch.dict(s.os.environ, {"GMGN_API_KEY": "test"}), patch.object(s, "run_gmgn_cli", side_effect=self.response), \
                patch.object(s, "fetch_dex_pairs_for_tokens", return_value={"pool": s.Pool("pool", token_address=OTHER)}) as resolve, \
                patch.object(s, "fetch_gecko_universe") as gecko, patch.object(s, "fetch_gmgn_trenches_universe") as trenches:
            pools = s.discover_market_pools(Mock(), config)
        self.assertEqual(set(resolve.call_args.args[1]), {TOKEN, OTHER})
        self.assertEqual(pools[0].gmgn_attention["sources"], ["hot_searches"])
        gecko.assert_not_called()
        trenches.assert_not_called()
        self.assertFalse(s.pool_matches_config(s.Pool("other", token_address=TOKEN), config))

    def test_no_age_cap_migration_or_liquidity_candidate_exclusion(self):
        config = {"discovery_source_mode": "gmgn_attention", "age_max_hours": 360, "dex_allowlist": ["pumpswap"]}
        pool = s.Pool("pool", token_address=TOKEN, dex="raydium", mcap_usd=100_000_000,
            liquidity_usd=0, pair_created_at=1, gmgn_attention={"sources": ["trending"]})
        self.assertTrue(s.pool_matches_config(pool, config))

    def test_discovery_merge_does_not_overwrite_deep_cursor(self):
        state = {"pools": {"pool": {"candidate_covered_ranges": [[1, 9]]}}}
        incoming = {"gmgn_candidates": {TOKEN: {"last_seen_at": g.iso(NOW), "sources": ["hot_searches"]}},
            "gmgn_discovery_health": {"observed_at": g.iso(NOW), "status": "ok"}}
        s.merge_discovery_state(state, incoming)
        self.assertEqual(state["pools"]["pool"]["candidate_covered_ranges"], [[1, 9]])
        self.assertIn(TOKEN, s.discovery_state_from(state)["gmgn_candidates"])

    def test_repeated_rank_does_not_trigger_a_second_history_check(self):
        state = {"candidate_checked_at": g.iso(NOW)}
        self.assertFalse(g.history_plan(state, {}, NOW + 300)["scan"])
        self.assertEqual(g.history_plan(state, {}, NOW + 901)["reason"], "incremental")
        state["candidate_history_pending"] = True
        self.assertTrue(g.history_plan(state, {}, NOW + 301)["scan"])

    def test_partial_and_parse_failure_never_acknowledge_coverage(self):
        for summary in ({"parse_errors": 1}, {"trade_fetch": {"live_truncated": True}},
                        {"trade_fetch": {"market_activity_unverified": True}}):
            state = {"candidate_covered_ranges": [[1, 2]]}
            summary.setdefault("trade_fetch", {})["live_from"] = 2
            g.record_history_check(state, summary, {}, NOW)
            self.assertEqual(state["candidate_covered_ranges"], [[1, 2]])
            self.assertTrue(state["candidate_history_pending"])
            self.assertNotIn("candidate_history_complete_to", state)

    def test_complete_ranges_merge_and_incremental_fetch_starts_after_coverage(self):
        state = {"candidate_covered_ranges": [[NOW - 200, NOW - 100]]}
        g.record_history_check(state, {"trade_fetch": {"live_from": NOW - 130}}, {}, NOW)
        self.assertEqual(state["candidate_covered_ranges"], [[NOW - 200, NOW]])
        config = {"discovery_source_mode": "gmgn_attention", "alert_window_minutes": 360,
            "helius_probe_recent_pages": 1, "helius_transactions_limit": 100}
        rpc = Mock()
        rpc.transactions_for_address.return_value = {"data": []}
        with patch.object(s.time, "time", return_value=NOW + 300):
            s.fetch_helius_pool_transactions(rpc, s.Pool("pool"), config, state, phase="probe")
        self.assertEqual(rpc.transactions_for_address.call_args.kwargs["block_time"]["gte"], NOW - 30)

    def test_standard_history_pages_resume_and_already_parsed_transactions_are_reused(self):
        head = [{"signature": f"s{i}", "blockTime": NOW - i} for i in range(10)]
        tail = [{"signature": "older", "blockTime": NOW - 1000}]
        rpc = Mock()
        rpc.signatures_for_address.side_effect = [head, head, head, tail]
        state = {}
        rows, info = g.collect_signature_ranges(rpc, "pool", state, {"candidate_signature_page_size": 10}, NOW)
        self.assertFalse(info["complete"])
        self.assertEqual(rpc.signatures_for_address.call_args.kwargs["before"], "s9")
        state["candidate_signature_gaps"] = info["gaps"]
        rows, info = g.collect_signature_ranges(rpc, "pool", state, {"candidate_signature_page_size": 10}, NOW)
        self.assertTrue(info["complete"])
        tx = {"transaction": {"signatures": ["older"]}, "blockTime": NOW - 1000}
        g.remember_transactions([tx], state, NOW)
        self.assertEqual(g.unprocessed_transactions([tx], state), [])

    def test_long_rankings_get_an_anchored_longer_first_window(self):
        self.assertEqual(g.initial_history_hours({"memberships": [{"interval": "24h"}]}, {}), 24)
        self.assertEqual(g.initial_history_hours({"memberships": [{"interval": "24h"}, {"interval": "5m"}]}, {}), 6)
        state = {}
        rpc = Mock()
        rpc.signatures_for_address.side_effect = RuntimeError("unavailable")
        with self.assertRaises(RuntimeError):
            g.collect_signature_ranges(rpc, "pool", state, {"helius_recent_lookback_minutes": 1440}, NOW)
        start = state["candidate_history_start"]
        rpc.signatures_for_address.side_effect = None
        rpc.signatures_for_address.return_value = []
        _, info = g.collect_signature_ranges(rpc, "pool", state, {}, NOW + 600)
        self.assertEqual(info["start"], start)

    def test_coverage_never_claims_time_spent_processing_after_read(self):
        state = {}
        g.record_history_check(state, {"trade_fetch": {"live_from": NOW - 100, "observed_to": NOW}}, {}, NOW + 200)
        self.assertEqual(state["candidate_history_complete_to"], NOW)

    def test_both_sources_raise_priority_without_excluding_single_source(self):
        config = {"discovery_source_mode": "gmgn_attention", "light_pool_limit": 100}
        single = s.Pool("single", token_address=TOKEN, volume_1h_usd=1_000_000, gmgn_attention={"in_both": False})
        both = s.Pool("both", token_address=OTHER, gmgn_attention={"in_both": True})
        self.assertEqual([p.pool_address for p in s.filter_universe_pools([single, both], config)], ["both", "single"])

    def test_parser_failure_rolls_back_all_history_cursors_but_keeps_retry(self):
        config = {"discovery_source_mode": "gmgn_attention"}
        original = {"helius_rolling_backlogs": [{"cursor": "old", "from": 10}], "latest_signature": "old-head"}
        state = {"pools": {"pool": copy.deepcopy(original)}}

        def failed(*args):
            pool_state = state["pools"]["pool"]
            pool_state.update(helius_rolling_backlogs=[], latest_signature="new-head", candidate_processed_signatures={"bad": NOW})
            return [], {"parse_errors": 1, "trade_fetch": {"live_from": 10}}

        with patch.object(s, "scan_pool_history", side_effect=failed):
            s.scan_pool(Mock(), s.Pool("pool"), config, state, {})
        self.assertEqual(state["pools"]["pool"]["helius_rolling_backlogs"], original["helius_rolling_backlogs"])
        self.assertEqual(state["pools"]["pool"]["latest_signature"], "old-head")
        self.assertNotIn("candidate_processed_signatures", state["pools"]["pool"])
        self.assertTrue(state["pools"]["pool"]["force_enhanced_next_scan"])

    def test_same_standard_head_does_not_fetch_transaction_details_twice(self):
        pool = s.Pool("pool", token_address=TOKEN)
        state = {"pools": {"pool": {"latest_signature": "same", "candidate_history_complete_to": NOW - 60}}}
        config = {"discovery_source_mode": "gmgn_attention", "helius_standard_incremental_signature_limit": 100}
        rpc = Mock()
        rpc.signatures_for_address.return_value = [{"signature": "same", "blockTime": NOW - 100}]
        with patch.object(s, "refresh_signal_thesis", return_value={}), patch.object(s.time, "time", return_value=NOW):
            _, summary = s.scan_pool_signatures(rpc, pool, config, state, {})
        self.assertTrue(summary["trade_fetch"]["activity_unchanged"])
        rpc.transaction.assert_not_called()

    def test_no_key_never_substitutes_all_market_discovery(self):
        config = {"discovery_source_mode": "gmgn_attention", "_active_runtime_state": {}}
        with patch.dict(s.os.environ, {}, clear=True), patch.object(s, "fetch_dex_pairs_for_tokens", return_value={}), \
                patch.object(s, "fetch_gecko_universe") as gecko:
            self.assertEqual(s.discover_market_pools(Mock(), config), [])
        self.assertEqual(config["_gmgn_attention_health"]["status"], "unavailable")
        gecko.assert_not_called()

    def test_completed_deep_repair_closes_probe_gap_without_claiming_later_time(self):
        stats = s.combine_fetch_stats({"truncated":True,"live_truncated":True,"observed_to":NOW,"live_from":NOW-100},
            {"truncated":False,"live_truncated":False,"rolling_backlog_segments_after":0,"observed_to":NOW+10,"live_from":NOW-100})
        state = {}
        g.record_history_check(state,{"trade_fetch":stats},{},NOW+30)
        self.assertFalse(state["candidate_history_pending"])
        self.assertEqual(state["candidate_history_complete_to"],NOW)

    def test_retention_cap_does_not_drop_any_current_memberships(self):
        records = {str(i): {"first_seen_at":g.iso(NOW),"last_seen_at":g.iso(NOW)} for i in range(101)}
        state = {}
        g.merge_candidates(state,records,{"observed_at":g.iso(NOW)},{"gmgn_candidate_registry_limit":100})
        self.assertEqual(len(state["gmgn_candidates"]),101)

    def test_due_cohorts_keep_monitoring_after_leaving_gmgn(self):
        config = {"discovery_source_mode":"gmgn_attention","dex_allowlist":["pumpswap"],"age_max_hours":360}
        thesis = {"status":"intact","pool_address":"pool","token_address":TOKEN,"dex":"raydium","pair_created_at":1}
        state = {"pools":{"pool":{"signal_thesis":thesis,"signal_recheck_due_at":g.iso(NOW-60)}}}
        with patch.object(s,"load_alert_history",return_value=[]):
            pools = s.due_signal_thesis_monitor_pools(state,config,now=NOW)
        self.assertEqual(len(pools),1)
        self.assertEqual(pools[0].source,"signal_thesis_monitor")


if __name__ == "__main__":
    unittest.main()
