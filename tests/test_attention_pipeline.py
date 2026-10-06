import copy
import unittest
from unittest.mock import Mock, patch

import gmgn_discovery as g
import scanner as s
from rpc_budget import DurableChunkBudget, MonthlyRpcBudget

NOW = 1791244800
TOKEN = "3Ydb2n8vAFdBJYpdiZEoDxRXLmiGDY1fuizVmMPMpump"


class AttentionPipelineTests(unittest.TestCase):
    def config(self):
        return s.apply_lane(s.load_json(s.DEFAULT_CONFIG_PATH, {}), "reactivation")

    def pool(self, name="one"):
        return s.Pool(name, token_address=TOKEN, symbol=name, source="gmgn_attention",
            gmgn_attention={"sources": ["trending"], "memberships": [{"source": "trending", "interval": "5m"}]})

    def test_successful_snapshot_removes_old_membership_immediately(self):
        record = {"last_seen_at": g.iso(NOW), "memberships": [{"source": "trending", "interval": "5m"}]}
        state = {"gmgn_candidates": {TOKEN: record}}
        g.merge_candidates(state, {}, {"observed_at": g.iso(NOW + 60),
            "successful_lists": ["trending:5m"]}, {})
        self.assertEqual(g.current_candidates(state, {}, NOW + 61), {})
        self.assertIn(TOKEN, state["gmgn_candidates"])

    def test_failed_source_keeps_its_cached_membership_only_within_ttl(self):
        state = {"gmgn_candidates": {TOKEN: {"last_seen_at": g.iso(NOW), "memberships": [
            {"source": "trending", "interval": "5m"}, {"source": "hot_searches", "interval": "1h"}]}}}
        g.merge_candidates(state, {}, {"observed_at": g.iso(NOW + 60),
            "successful_lists": ["trending:5m"]}, {})
        self.assertEqual(g.current_candidates(state, {}, NOW + 61)[TOKEN]["sources"], ["hot_searches"])
        self.assertEqual(g.current_candidates(state, {}, NOW + 3601), {})

    def test_cli_hot_searches_unwrapped_array_is_accepted_for_all_windows(self):
        def run(config, args, label):
            if args[1] == "trending":
                return {"code": 0, "data": {"rank": []}}
            return [{"chain": "sol", "interval": window, "tokens": [{"address": TOKEN}]}
                for window in g.INTERVALS]
        records, health = g.fetch_attention({}, run)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(records[TOKEN]["sources"], ["hot_searches"])
        self.assertEqual(len(records[TOKEN]["memberships"]), 5)

    def test_attention_candidates_are_not_cut_off_by_legacy_light_pool_limit(self):
        pools = [self.pool("one"), self.pool("two")]
        pools[1].token_address = "So11111111111111111111111111111111111111112"
        config = {"discovery_source_mode": "gmgn_attention", "light_pool_limit": 1}
        self.assertEqual(len(s.filter_universe_pools(pools, config)), 2)

    def test_gmgn_pool_fallback_checks_identity_and_uses_own_pool_not_creator_ath(self):
        info = {"address": TOKEN, "symbol": "TEST", "total_supply": "1000",
            "pool": {"pool_address": "4kfz4pK6D7cTTS458QMqCic78QDhXQ9JN3ZEvC5bc72S",
                "base_address": TOKEN, "exchange": "pump_amm", "liquidity": "25"},
            "price": {"address": TOKEN, "price": "2", "buys_1h": 3, "sells_1h": 2},
            "dev": {"ath_token_info": {"ath_mc": 9999999}}}
        pool = s.gmgn_attention_pool(info, TOKEN, NOW)
        self.assertEqual(pool.mcap_usd, 2000)
        self.assertEqual(pool.txns_1h, 5)
        self.assertEqual(pool.dex, "pumpfun-amm")
        self.assertIsNone(s.gmgn_attention_pool({**info, "address": "wrong"}, TOKEN, NOW))
        info["pool"]["base_address"] = "wrong"
        self.assertIsNone(s.gmgn_attention_pool(info, TOKEN, NOW))

    def test_unresolved_pool_lookup_is_bounded_cached_and_not_repeated_after_refresh(self):
        pool = self.pool("4kfz4pK6D7cTTS458QMqCic78QDhXQ9JN3ZEvC5bc72S")
        records = {TOKEN: {"last_seen_at": g.iso(NOW), "first_seen_at": g.iso(NOW), "memberships": []}}
        config = {"gmgn_attention_resolve_limit": 12, "_scan_profile": "targeted"}
        with patch.object(s.time, "time", return_value=NOW), \
                patch.object(s, "fetch_gmgn_raw_token_info", return_value={}) as read, \
                patch.object(s, "gmgn_attention_pool", return_value=pool):
            resolved = {}
            stats = s.resolve_missing_attention_pools(records, resolved, config)
            self.assertEqual(stats["resolved"], 1)
            read.assert_called_once_with(config, TOKEN)
        state = {"gmgn_candidates": records}
        incoming = {TOKEN: {"first_seen_at": g.iso(NOW + 60), "last_seen_at": g.iso(NOW + 60),
            "memberships": [{"source": "trending", "interval": "5m"}]}}
        g.merge_candidates(state, incoming, {"observed_at": g.iso(NOW + 60), "successful_lists": ["trending:5m"]}, {})
        with patch.object(s.time, "time", return_value=NOW + 60), patch.object(s, "fetch_gmgn_raw_token_info") as read:
            resolved = {}
            stats = s.resolve_missing_attention_pools(state["gmgn_candidates"], resolved, config)
        read.assert_not_called()
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(resolved[pool.pool_address].token_address, TOKEN)

    def test_targeted_pool_resolution_cannot_consume_deep_lookup_allowance(self):
        records = {str(i): {} for i in range(10)}
        with patch.object(s, "fetch_gmgn_raw_token_info", return_value={}) as read, \
                patch.object(s.time, "time", return_value=NOW):
            stats = s.resolve_missing_attention_pools(records, {},
                {"gmgn_attention_resolve_limit": 12, "_scan_profile": "targeted"})
        self.assertEqual(read.call_count, 2)
        self.assertEqual(stats["pending"], 10)

    def test_fair_work_share_restores_actual_provider_limits_after_exception(self):
        provider = s.HeliusRpc("https://rpc.invalid", credit_budget=100)
        provider.attempted_credits = 20
        rpc = s.RoutedSolanaRpc([provider])
        with self.assertRaisesRegex(RuntimeError, "stop"):
            with s.fair_pool_rpc_budget(rpc, 4):
                self.assertEqual(provider.credit_budget, 40)
                raise RuntimeError("stop")
        self.assertEqual(provider.credit_budget, 100)

    def test_small_share_never_becomes_zero_unlimited_budget(self):
        provider = s.HeliusRpc("https://rpc.invalid", credit_budget=1)
        rpc = s.RoutedSolanaRpc([provider])
        with s.fair_pool_rpc_budget(rpc, 40):
            self.assertEqual(provider.credit_budget, 1)
            self.assertFalse(provider.can_call("getTransactionsForAddress"))

    def test_route_diagnostics_distinguish_uncertain_ledger_from_spent_quota(self):
        provider = s.HeliusRpc("https://rpc.invalid", credit_budget=100)
        provider.monthly_budget = DurableChunkBudget(MonthlyRpcBudget({}, "helius", 100), lambda: False, 50)
        self.assertFalse(provider.monthly_budget.reserve(10))
        rpc = s.RoutedSolanaRpc([provider])
        self.assertEqual(rpc.history_route_status()["getTransactionsForAddress"]["helius"], "budget_ledger_unavailable")
        self.assertEqual(rpc.provider_stats()["helius"]["status"], "budget_ledger_unavailable")
        provider.monthly_budget = None
        provider.attempted_credits = 100
        self.assertEqual(rpc.history_route_status()["getTransactionsForAddress"]["helius"], "per_scan_budget")

    def test_temporary_cooldown_and_unsupported_are_not_budget_exhaustion(self):
        provider = s.HeliusRpc("https://rpc.invalid", credit_budget=100)
        provider.method_cooldowns["getTransactionsForAddress"] = s.time.monotonic() + 100
        rpc = s.RoutedSolanaRpc([provider])
        self.assertEqual(rpc.history_route_status()["getTransactionsForAddress"]["helius"], "temporary_cooldown")
        provider.unsupported_methods.add("getSignaturesForAddress")
        self.assertEqual(rpc.history_route_status()["getSignaturesForAddress"]["helius"], "unsupported")

    def test_prefetch_never_advances_durable_cursor_before_successful_parse(self):
        pool = self.pool()
        state = {"pools": {pool.pool_address: {"helius_rolling_backlogs": [{"cursor": "old"}]}}}
        before = copy.deepcopy(state["pools"][pool.pool_address])
        rpc = Mock()
        rpc.available_history_mode.return_value = "enhanced"

        def fetch(rpc, pool, config, working, phase):
            self.assertTrue(config["_candidate_head_only"])
            self.assertEqual(config["helius_probe_max_pages"], 1)
            working["helius_rolling_backlogs"] = [{"cursor": "new"}]
            return [], {"live_from": NOW - 100, "observed_to": NOW}

        with patch.object(s, "pool_has_new_activity", return_value=(True, {})), \
                patch.object(s, "fetch_helius_pool_transactions", side_effect=fetch), patch.object(s.time, "time", return_value=NOW):
            prepared, stats = s.prepare_attention_history(rpc, [pool], state, self.config())
        self.assertEqual(state["pools"][pool.pool_address]["helius_rolling_backlogs"], before["helius_rolling_backlogs"])
        self.assertEqual(prepared[pool.pool_address]["history_state"]["helius_rolling_backlogs"], [{"cursor": "new"}])
        self.assertEqual(stats["prepared"], 1)

    def test_cached_probe_is_used_without_a_second_network_read(self):
        pool, rpc = self.pool(), Mock()
        config = self.config()
        config.update(classic_alerts_enabled=False, _prepared_probe={"transactions": [], "stats": {
            "source": "enhanced_transactions", "live_from": NOW - 100, "observed_to": NOW}, "history_state": {}})
        with patch.object(s, "pool_has_new_activity") as probe, \
                patch.object(s, "fetch_helius_pool_transactions") as fetch, \
                patch.object(s, "parse_helius_swaps", return_value=([], 0)), \
                patch.object(s, "should_deep_scan", return_value=(False, "no_evidence")), \
                patch.object(s, "build_reactivation_wave_alerts", return_value=[]), \
                patch.object(s, "build_sticky_accumulation_alerts", return_value=[]), \
                patch.object(s, "refresh_signal_thesis", return_value={}):
            _, summary = s.scan_pool(rpc, pool, config, {}, {"remaining": 0})
        fetch.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(summary["transactions_scanned"], 0)

    def test_cached_cursor_is_rolled_back_if_parser_fails(self):
        pool = self.pool()
        original = {"latest_signature": "old", "helius_rolling_backlogs": [{"cursor": "old"}]}
        state = {"pools": {pool.pool_address: copy.deepcopy(original)}}
        config = self.config()
        config["_prepared_probe"] = {"history_state": {"latest_signature": "new"}}

        def fail(*args):
            s.restore_history_state(state["pools"][pool.pool_address], config["_prepared_probe"]["history_state"])
            return [], {"parse_errors": 1}

        with patch.object(s, "scan_pool_history", side_effect=fail):
            s.scan_pool(Mock(), pool, config, state, {})
        self.assertEqual(state["pools"][pool.pool_address]["latest_signature"], "old")
        self.assertEqual(state["pools"][pool.pool_address]["helius_rolling_backlogs"], original["helius_rolling_backlogs"])

    def test_missing_wallet_evidence_keeps_successful_history_without_a_signal(self):
        pool, rpc, state = self.pool(), Mock(), {}
        tx = {"blockTime": NOW, "transaction": {"signatures": ["cached"]}}
        config = self.config()
        config.update(classic_alerts_enabled=False, _prepared_probe={"transactions": [tx], "stats": {
            "source": "enhanced_transactions", "live_from": NOW - 100, "observed_to": NOW}, "history_state": {}})
        with patch.object(s, "fetch_helius_pool_transactions") as fetch, \
                patch.object(s, "parse_helius_swaps", return_value=([], 0)), \
                patch.object(s, "should_deep_scan", return_value=(False, "no_evidence")), \
                patch.object(s, "build_reactivation_wave_alerts", side_effect=s.WaveDataUnavailable("balance read deferred")), \
                patch.object(s, "build_sticky_accumulation_alerts", return_value=[]), \
                patch.object(s, "refresh_signal_thesis", return_value={}), patch.object(s.time, "time", return_value=NOW):
            alerts, summary = s.scan_pool(rpc, pool, config, state, {"remaining": 0})
        fetch.assert_not_called()
        self.assertEqual(alerts, [])
        history = state["pools"][pool.pool_address]
        self.assertEqual(history["latest_signature"], "cached")
        self.assertIn("cached", history["candidate_processed_signatures"])
        self.assertEqual(summary["trade_fetch"]["evidence_pending"], ["balance read deferred"])
        g.record_history_check(history, summary, config, NOW)
        self.assertTrue(history["candidate_evidence_pending"])
        self.assertTrue(history["candidate_history_pending"])
        self.assertEqual(history["candidate_history_complete_to"], NOW)

    def test_pending_wallet_evidence_is_not_a_confirmed_alert_or_a_failed_history_read(self):
        alert = {"lane": "reactivation", "action_tier": "hot_reactivation", "signal_family": "reactivation_wave",
            "wave": {"balance_coverage_pct":100,"owner_resolution_coverage_pct":100,"min_hold_minutes":30,"hold_age_minutes":60},
            "reactivation_baseline":{"version":2,"status":"ready","reactivation_confirmed":True},
            "wallet_graph":{"checked_flow_coverage_pct":100,"verified_effective_wallets":10}}
        stats = {"source":"enhanced_transactions","evidence_pending":["balances unavailable"]}
        s.apply_alert_data_quality([alert],stats,0,0,0,self.config())
        self.assertEqual(alert["signal_confirmation"]["status"],"candidate")
        self.assertNotEqual(alert["action_tier"],"hot_reactivation")
        health=s.build_scan_health([{"trade_fetch":stats}]*5,
            {"reactivation":{"universe_pools":40,"selection":{"selected":5}}},self.config())
        self.assertEqual(health["failed_pools"],0)
        self.assertEqual(health["evidence_pending_pools"],5)
        self.assertEqual(health["status"],"degraded")

    def test_programming_errors_still_fail_and_legacy_mode_keeps_its_failure_contract(self):
        with patch.object(s,"build_reactivation_wave_alerts",side_effect=ValueError("bug")):
            with self.assertRaisesRegex(ValueError,"bug"):
                s.accumulation_alerts_with_available_evidence(self.pool(),[],[],self.config(),Mock(),{})
        with patch.object(s,"build_reactivation_wave_alerts",side_effect=s.WaveDataUnavailable("missing")):
            with self.assertRaises(s.WaveDataUnavailable):
                s.accumulation_alerts_with_available_evidence(self.pool(),[],[],{"discovery_source_mode":"composite"},Mock(),{})

    def test_prepared_standard_head_is_not_downloaded_twice_or_dated_later(self):
        rpc = Mock()
        config = {"_candidate_signature_head": [], "_candidate_head_observed_at": NOW - 60}
        _, result = g.collect_signature_ranges(rpc, "pool", {}, config, NOW)
        rpc.signatures_for_address.assert_not_called()
        self.assertEqual(result["observed_to"], NOW - 60)

    def test_all_heads_precede_cohort_and_wallet_checks_and_offlist_monitor_is_not_scanned(self):
        pools = [self.pool(str(i)) for i in range(3)]
        monitor = self.pool("monitor")
        monitor.source = "signal_thesis_monitor"
        calls = []
        rpc = Mock(circuit_open_reason=None)
        rpc.available_history_mode.return_value = "enhanced"
        config = self.config()
        config["active_pool_limit"] = 3

        def fetch(rpc, pool, config, working, phase):
            calls.append(("head", pool.pool_address))
            return [], {"source": "enhanced_transactions", "live_from": NOW - 100, "observed_to": NOW}

        def analyze(rpc, pool, config, state, budget):
            calls.append(("analysis", pool.pool_address))
            self.assertIn("_prepared_probe", config)
            return [], {"pool": pool.as_dict(), "trade_fetch": config["_prepared_probe"]["stats"]}

        def cohort(*args):
            calls.append(("cohort", "monitor"))
            return {"checked": 1}

        with patch.object(s, "filter_universe_pools", side_effect=lambda pools, _: pools), \
                patch.object(s, "filter_reactivation_by_ath", side_effect=lambda http, state, pools, *args: pools), \
                patch.object(s, "load_deleted_tokens", return_value={}), \
                patch.object(s, "attach_reactivation_baselines"), \
                patch.object(s, "due_signal_thesis_monitor_pools", return_value=[monitor]), \
                patch.object(s, "monitor_due_cohorts", side_effect=cohort), \
                patch.object(s, "pool_has_new_activity", return_value=(True, {})), \
                patch.object(s, "fetch_helius_pool_transactions", side_effect=fetch), \
                patch.object(s, "scan_pool", side_effect=analyze), \
                patch.object(s.time, "sleep"), patch.object(s.time, "time", return_value=NOW):
            _, summaries, _ = s.scan_with_config(Mock(), rpc, {}, config, base_universe=pools)
        self.assertEqual(calls[:3], [("head", str(i)) for i in range(3)])
        self.assertEqual(calls[3], ("cohort", "monitor"))
        self.assertEqual(len(summaries), 3)
        self.assertNotIn(("analysis", "monitor"), calls)

    def test_coverage_uses_selected_count_not_entire_universe(self):
        health = s.build_scan_health([{}] * 4, {"reactivation": {"universe_pools": 1047,
            "selection": {"selected": 40}}}, {"scan_health_min_scanned_pools": 5})
        self.assertEqual(health["status"], "unhealthy")
        self.assertIn("4/40 selected", health["reasons"][0])
        self.assertEqual(health["candidate_pools"], 1047)
        self.assertEqual(health["selected_pools"], 40)

    def test_recently_checked_candidates_do_not_invalidate_a_small_due_batch(self):
        health = s.build_scan_health([{}] * 3, {"reactivation": {"universe_pools": 1047,
            "selection": {"selected": 3}}}, {"scan_health_min_scanned_pools": 5})
        self.assertEqual(health["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
