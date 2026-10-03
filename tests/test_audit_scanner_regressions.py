"""Corrected contracts for the consolidated scanner audit findings."""

import copy
import json
import socket
import subprocess
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import scanner as s
from scan_failure import failure_metadata, scanner_failure_class
from scan_scheduling import targeted_profile
from tests.test_coordination_pipeline import NOW, alert


class AuditScannerRegressions(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def receipt(self, accounts, moves, signer="buyer-a", signature="sale", at=NOW + 300):
        keys = [{"pubkey": signer, "signer": True}] + [
            {"pubkey": "account-" + str(i), "signer": False} for i in range(len(accounts))]
        def balances(pos):
            return [{"accountIndex": i + 1, "owner": owner, "mint": mint,
                     "uiTokenAmount": {"amount": str(values[pos]), "decimals": 0}}
                    for i, (owner, mint, *values) in enumerate(accounts)]
        instructions = [{"program": "spl-token", "parsed": {"type": "transfer", "info": {
            "source": "account-" + str(a), "destination": "account-" + str(b), "amount": str(n)}}}
            for a, b, n in moves]
        return {"slot": 12, "blockTime": at, "transaction": {"signatures": [signature],
                "message": {"accountKeys": keys}}, "meta": {"err": None,
                "preTokenBalances": balances(0), "postTokenBalances": balances(1),
                "innerInstructions": [{"instructions": instructions}]}}

    def thesis(self, confirmation=None):
        item = alert()
        item["signal_confirmation"] = confirmation or {"status": "confirmed", "reasons": []}
        s.attach_coordinated_activity(item, {})
        return s.signal_thesis_from_alert(item, {})

    def test_s03_post_capture_sales_and_rebuys_do_not_restore_original_tokens(self):
        thesis = self.thesis()
        pool = s.Pool("pool", token_address="token")
        swaps = [{"kind": "sell", "signature": "exit-" + owner, "block_time": NOW + 300,
                  "coordination_sale_owner": owner, "coordination_sale_amount": 50, "token_amount": 50}
                 for owner in ("buyer-a", "buyer-b", "buyer-c")]
        rpc = Mock()
        rpc.token_balance.return_value = 50
        state = {"signal_thesis": thesis}
        for checked in (NOW + 600, NOW + 1200):
            s.refresh_signal_thesis(rpc, pool, state, [], {}, s.iso(checked), observed_swaps=swaps)
        self.assertEqual(thesis["current_retained_tokens"], 0)
        self.assertEqual(thesis["coordination_inputs"]["proven_sales"], dict.fromkeys(("buyer-a", "buyer-b", "buyer-c"), 50))
        self.assertEqual(thesis["coordinated_activity"]["metrics"]["held_supply_pct"], 0)
        self.assertNotEqual(thesis["status"], "intact")
        self.assertEqual(len(thesis["cohort_sale_events"]), 3)

    def test_s03_unresolved_foreign_and_future_movements_are_not_sales(self):
        thesis = self.thesis()
        pool = s.Pool("pool", token_address="token")
        sale = {"kind": "sell", "signature": "exit", "block_time": NOW + 300,
                "coordination_sale_owner": "buyer-a", "coordination_sale_amount": 50, "token_amount": 50}
        for changes in ({"coordination_sale_owner": None}, {"pool_address": "other"},
                        {"token_address": "other"}, {"block_time": NOW + 900}, {"block_time": NOW},
                        {"coordination_sale_amount": float("nan")}, {"coordination_sale_amount": 51}):
            self.assertFalse(s.record_thesis_sales(thesis, pool, [dict(sale, **changes)], s.iso(NOW + 600)))
        self.assertEqual(thesis["cohort_sale_events"], {})

    def test_s04_paged_seasoning_rechecks_pre_deadline_balances(self):
        thesis = self.thesis({"status": "candidate", "reasons": ["retention not seasoned"],
                              "retention_check_after": s.iso(NOW + 5400)})
        rpc = Mock()
        rpc.token_balance.return_value = 50
        pool = s.Pool("pool", token_address="token")
        state = {"signal_thesis": thesis}
        config = {"_cohort_check_wallet_limit": 2}
        s.recheck_signal_thesis(rpc, pool, state, config, s.iso(NOW + 1800))
        rpc.token_balance.side_effect = lambda owner, mint: 50 if owner == "buyer-c" else 0
        s.recheck_signal_thesis(rpc, pool, state, config, s.iso(NOW + 5400))
        self.assertEqual(thesis["signal_confirmation"]["status"], "candidate")
        s.recheck_signal_thesis(rpc, pool, state, config, s.iso(NOW + 5460))
        self.assertEqual(thesis["signal_confirmation"]["status"], "candidate")
        self.assertAlmostEqual(thesis["token_retention_pct"], 100 / 3)

    def test_o04_programming_failures_cannot_become_provider_backoff(self):
        for error in (KeyError("credit_balance"), TypeError("quota"), NameError("timeout"), ValueError("invariant")):
            metadata = {"error": str(error), **failure_metadata(error),
                        "scan_health": {"scan_error_categories": {"helius_quota": 1}}}
            self.assertEqual(scanner_failure_class(metadata, 1), "hard_failure")
        error = s.HeliusRpcError("method", "quota", "usage exhausted")
        self.assertEqual(scanner_failure_class(failure_metadata(error), 1), "soft_provider_failure")
        self.assertEqual(scanner_failure_class({"error": "credit quota timeout"}, 1), "hard_failure")
        with patch.object(s, "load_json", return_value={}), patch.object(s, "save_json"):
            status = s.write_scanner_status("failed", error=KeyError("credit_balance"))
        self.assertEqual(status["error_type"], "KeyError")
        self.assertEqual(s.scanner_failure_class(status, 1), "hard_failure")
        for error in (TimeoutError("deadline"), subprocess.TimeoutExpired("scanner", 1),
                      s.HeliusRpcError("method", "temporary", "transport unavailable")):
            self.assertEqual(scanner_failure_class(failure_metadata(error), 1), "soft_provider_failure")

    def test_s01_targeted_ath_allowance_is_shared_by_all_stages_and_priority(self):
        pools = [s.Pool("pool-" + str(i), token_address="token-" + str(i), mcap_usd=1000) for i in range(30)]
        config = targeted_profile({"lane": "reactivation", "ath_max_tokens_per_scan": 25,
                                   "ath_request_delay_seconds": 0})
        self.assertEqual(config["ath_max_tokens_per_scan"], 2)
        config["_ath_fetch_pool_addresses"] = [p.pool_address for p in pools[:6]]
        def raw_info(config, args, label):
            token = args[-1]
            return {"address": token, "dev": {"ath_token_info": {"ath_token": token, "ath_mc": 10000}}}
        with patch.dict(s.os.environ, {"GMGN_API_KEY": "test"}), \
                patch.object(s, "run_gmgn_cli", side_effect=raw_info) as remote, \
                patch.object(s, "recent_alert_token_addresses", return_value=["unrelated"]), \
                patch.object(s.time, "sleep"), patch.object(s.time, "time", return_value=NOW):
            state = {"market": {}}
            self.assertEqual(len(s.filter_reactivation_by_ath(None, state, pools, config, s.iso(NOW))), 30)
            self.assertEqual(remote.call_count, 2)
            config["_ath_budget"]["target_tokens"] = [p.token_address for p in pools[:6]]
            priority = [{"pool": {"token_address": p.token_address}} for p in pools[6:10]]
            s.enrich_market_ath(None, {}, pools, priority, config, s.iso(NOW))
            config["ath_max_current_ratio"] = 0.5
            s.filter_reactivation_by_ath(None, {}, pools, config, s.iso(NOW))
            self.assertEqual(remote.call_count, 2)
            self.assertEqual(config["_ath_budget"]["remaining"], 0)
            self.assertNotIn("unrelated", config["_ath_budget"]["tokens"])

    def test_s01_failed_ath_work_is_not_retried_uncached_by_other_stages(self):
        config = {"ath_max_tokens_per_scan": 1}
        with patch.object(s, "fetch_gmgn_raw_token_info", return_value={}) as raw:
            for timestamps in (False, True, False):
                self.assertEqual(s.fetch_gmgn_ath(config, "token", timestamps)["error"], "empty_ath_response")
            self.assertEqual(raw.call_count, 1)
            self.assertEqual(s.fetch_gmgn_ath(config, "other")["error"], "ath_budget_deferred")
            self.assertEqual(raw.call_count, 1)

    def test_s02_discovery_updates_quotes_without_erasing_catch_or_ath(self):
        prior = {"token_address": "token", "pool_address": "old-pool", "latest_seen_at": s.iso(NOW),
                 "latest_mcap_usd": 200, "first_obs_mcap_usd": 200, "caught_obs_mcap_usd": 200,
                 "ath_source": "gmgn", "ath_identity_version": 2, "ath_mcap_usd": 5000, "ath_checked_at": NOW}
        incoming = {"token_address": "token", "pool_address": "new-pool", "latest_seen_at": s.iso(NOW + 300),
                    "latest_mcap_usd": 400, "caught_obs_mcap_usd": 999, "ath_mcap_usd": 999}
        state = {"market": {"token": copy.deepcopy(prior)}}
        s.merge_discovery_state(state, {"market": {"token": incoming}})
        entry = state["market"]["token"]
        self.assertEqual((entry["pool_address"], entry["latest_mcap_usd"]), ("new-pool", 400))
        for key in ("first_obs_mcap_usd", "caught_obs_mcap_usd", "ath_source", "ath_identity_version", "ath_mcap_usd", "ath_checked_at"):
            self.assertEqual(entry[key], prior[key])
        incoming["latest_mcap_usd"] = 999
        self.assertEqual(entry["latest_mcap_usd"], 400)

    def test_s05_creation_age_belongs_to_the_current_pool_not_the_mint(self):
        token = s.SOL_MINT
        old = s.Pool("11111111111111111111111111111111", token_address=token,
                     source="dexscreener", mcap_usd=1000, pair_created_at=NOW - 86400, market_snapshot_at=NOW)
        new = s.Pool("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", token_address=token,
                     source="dexscreener", mcap_usd=1000, pair_created_at=NOW - 600, market_snapshot_at=NOW + 1)
        state = {}
        for pool in (old, new):
            s.record_market_observations(state, [pool], s.iso(pool.market_snapshot_at))
        registry = s.registry_pool_from_market_entry(state["market"][token], now=NOW + 1)
        with patch.object(s, "utc_now", return_value=datetime.fromtimestamp(NOW + 1, timezone.utc)):
            self.assertEqual(registry.pair_created_at, new.pair_created_at)
            self.assertFalse(s.pool_age_matches_config(registry, {"age_min_hours": 0.5, "age_max_hours": 360}))
        newer = s.Pool("new-unknown-pool", token_address=token, source="dexscreener", mcap_usd=1000,
                       market_snapshot_at=NOW + 2)
        s.record_market_observations(state, [newer], s.iso(NOW + 2))
        self.assertNotIn("pair_created_at", state["market"][token])

    def test_s06_gift_and_unrelated_sol_outflow_are_not_a_pool_buy(self):
        tx = self.receipt([("giver", "token", 100, 0), ("buyer-a", "token", 0, 100)], [(0, 1, 100)])
        tx["transaction"]["message"]["accountKeys"].extend([
            {"pubkey": "pool", "signer": False}, {"pubkey": "unrelated", "signer": False}])
        tx["meta"].update(preBalances=[5_000_000_000, 0, 0, 0, 0],
                          postBalances=[2_000_000_000, 0, 0, 0, 3_000_000_000])
        self.assertIsNone(s.parse_pool_swap(tx, s.Pool("pool", token_address="token")))
        valid = self.receipt([("buyer-a", "token", 0, 50), ("pool", "token", 1000, 950),
                              ("pool", s.SOL_MINT, 100, 101)], [(1, 0, 50)])
        swap = s.parse_pool_swap(valid, s.Pool("pool", token_address="token"))
        self.assertEqual((swap["kind"], swap["token_amount"], swap["sol_amount"]), ("buy", 50, 1))

    def test_s07_giver_and_delegate_do_not_become_someone_elses_seller(self):
        tx = self.receipt([("buyer-a", "token", 100, 0), ("gift", "token", 0, 100),
                           ("outside-owner", "token", 50, 0), ("pool", "token", 1000, 1050),
                           ("pool", s.SOL_MINT, 100, 99)], [(0, 1, 100), (2, 3, 50)])
        swap = s.parse_pool_swap(tx, s.Pool("pool", token_address="token"))
        self.assertEqual(swap["coordination_sale_owner"], "outside-owner")
        self.assertEqual(s.wave_sell_owner(swap, ["buyer-a"]), "")
        self.assertEqual(s.owner_activity_since([swap], NOW, ["buyer-a"])["buyer-a"]["token_sold"], 0)
        self.assertEqual(s.owner_activity_since([swap], NOW, ["outside-owner"])["outside-owner"]["token_sold"], 50)
        swap["coordination_sale_owner"] = None
        self.assertEqual(s.wave_sell_owner(swap), "")

    def test_s08_hold_deadline_counts_time_already_elapsed(self):
        item = alert()
        item.update(lane="reactivation", reactivation_baseline={"version": 2, "status": "ready", "reactivation_confirmed": True})
        item["wallet_graph"].update(checked_flow_coverage_pct=100, verified_effective_wallets=6)
        item["wave"].update(min_hold_minutes=90, hold_age_minutes=80)
        s.apply_signal_confirmation(item, {})
        self.assertEqual(item["signal_confirmation"]["reasons"], ["retention not seasoned"])
        self.assertEqual(s.parse_timestamp(item["signal_confirmation"]["retention_check_after"]), NOW + 600)
        item["wave"]["hold_age_minutes"] = 90
        s.apply_signal_confirmation(item, {})
        self.assertEqual(item["signal_confirmation"]["status"], "confirmed")

    def test_s03_s04_legacy_evidence_requires_a_fresh_cycle_and_stays_unknown(self):
        thesis = self.thesis()
        thesis.pop("retention_evidence_version")
        thesis["check_cycle_started_at"] = s.iso(NOW + 1800)
        thesis["check_cycle_verified_owners"] = ["buyer-a", "buyer-b"]
        saved_cohort = copy.deepcopy(thesis["cohort"])
        state = {"pools": {"pool": {"signal_thesis": thesis}}}
        s.migrate_scanner_state(state)
        self.assertEqual(thesis["cohort"], saved_cohort)
        self.assertEqual(thesis["original_sale_history_status"], "unknown")
        self.assertEqual(thesis["signal_confirmation"]["status"], "candidate")
        self.assertEqual(thesis["legacy_signal_confirmation"]["status"], "confirmed")
        rpc = Mock()
        rpc.token_balance.return_value = 50
        for checked in (NOW + 5400, NOW + 5460):
            s.recheck_signal_thesis(rpc, s.Pool("pool", token_address="token"), state["pools"]["pool"],
                                   {"_cohort_check_wallet_limit": 2}, s.iso(checked))
        self.assertEqual(thesis["check_cycle_pending_wallets"], 0)
        self.assertTrue(all(s.parse_timestamp(row["checked_at"]) >= NOW + 5400 for row in thesis["cohort"]))
        self.assertEqual(thesis["status"], "unknown")
        self.assertEqual(s.public_signal_thesis(thesis)["original_sale_history_status"], "unknown")
        self.assertTrue(all(row["behavior_status"] == "unknown" and row["evidence_status"] == "partial"
                            for row in s.history_ledger_wallets(thesis, {})))

    def test_s04_complete_post_deadline_cycle_can_confirm_without_weakening_guards(self):
        thesis = self.thesis({"status": "candidate", "reasons": ["retention not seasoned"],
                              "retention_check_after": s.iso(NOW + 5400)})
        rpc = Mock()
        rpc.token_balance.return_value = 50
        state = {"signal_thesis": thesis}
        for checked in (NOW + 1800, NOW + 5400, NOW + 5460):
            s.recheck_signal_thesis(rpc, s.Pool("pool", token_address="token"), state,
                                   {"_cohort_check_wallet_limit": 2}, s.iso(checked))
            if checked <= NOW + 5400:
                self.assertEqual(thesis["signal_confirmation"]["status"], "candidate")
        self.assertEqual(thesis["signal_confirmation"]["status"], "confirmed")
        self.assertEqual(thesis["token_retention_pct"], 100)

    def test_s03_legacy_downgrade_cannot_replace_the_original_with_new_buyers(self):
        thesis = self.thesis()
        thesis.pop("retention_evidence_version")
        state = {"signal_thesis": thesis}
        incoming = alert()
        incoming["created_at"] = s.iso(NOW + 600)
        incoming["signal_confirmation"] = {"status": "confirmed", "reasons": []}
        for row in incoming["wave"]["top_buyers"]:
            row["owner"] = "new-" + row["owner"]
        current, replaced = s.capture_signal_thesis(state, [incoming], {}, s.iso(NOW + 600))
        self.assertFalse(replaced)
        self.assertIs(current, thesis)
        self.assertEqual([row["owner"] for row in thesis["cohort"]], ["buyer-a", "buyer-b", "buyer-c"])
        thesis["status"] = "invalidated"
        current, replaced = s.capture_signal_thesis(state, [], {}, s.iso(NOW + 700))
        self.assertTrue(replaced)
        self.assertEqual(state["signal_thesis_history"][0]["cohort"], thesis["cohort"])
        self.assertEqual(current["cohort"][0]["owner"], "new-buyer-a")

    def test_s03_bounded_history_gap_is_unknown_not_a_sale_or_restored_inventory(self):
        thesis = self.thesis()
        rpc = Mock()
        rpc.token_balance.return_value = 50
        result = s.refresh_signal_thesis(rpc, s.Pool("pool", token_address="token"),
            {"signal_thesis": thesis}, [], {}, s.iso(NOW + 600), observed_swaps=[],
            observed_coverage={"history_gap_seconds": 600})
        self.assertEqual(result["original_sale_history_status"], "unknown")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(thesis["proven_sales"], dict.fromkeys(("buyer-a", "buyer-b", "buyer-c"), 0))

    def test_o02_retention_event_and_wallet_pnl_cannot_use_a_future_quote(self):
        thesis = self.thesis()
        thesis.update(signal_price_usd=1, signal_mcap_usd=1000, last_checked_at=s.iso(NOW + 3600))
        market = {"latest_seen_at": s.iso(NOW + 6 * 3600), "latest_price_usd": 2, "latest_mcap_usd": 2000}
        state = {"pools": {"pool": {"signal_thesis": thesis}}, "market": {"token": market}}
        with patch.object(s, "load_deleted_tokens", return_value={"tokens": set(), "pools": set()}):
            ledger = s.build_history_ledger({"alerts": []}, state, {}, s.iso(NOW + 6 * 3600))
        check = next(row for row in ledger["events"] if row["event"]["event_type"] == "retention_check")
        self.assertIsNone(check["event"]["price_usd"])
        self.assertIsNone(check["event"]["market_observed_at"])
        self.assertTrue(all(row["estimated_pnl_pct"] is None for row in check["wallets"]))
        market["latest_seen_at"] = thesis["last_checked_at"]
        rows = s.history_ledger_wallets(thesis, market)
        self.assertEqual(rows[0]["estimated_pnl_pct"], 100)
        self.assertEqual(rows[0]["market_observed_at"], thesis["last_checked_at"])

    def test_o03_gap_ratio_uses_the_same_fetch_population(self):
        standard = {"trade_fetch": {"source": "pool_signatures", "history_gap_seconds": 600}}
        enhanced = {"trade_fetch": {"source": "enhanced_transactions", "history_gap_seconds": 600}}
        for summaries in ([standard], [standard, enhanced]):
            health = s.build_scan_health(summaries, {}, {})
            self.assertEqual(health["history_gap_ratio"], 1)
            self.assertEqual(health["status"], "degraded")
            self.assertEqual(health["coverage_by_source"]["pool_signatures"]["history_gaps"], 1)

    def test_o04_health_preserves_programming_error_type(self):
        error = KeyError("helius: credit quota timeout")
        health = s.build_scan_health([{"error": str(error), **failure_metadata(error)}], {}, {})
        self.assertEqual(health["scan_error_categories"], {"other": 1})
        self.assertEqual(scanner_failure_class({"scan_health": health}, 1), "hard_failure")

    def outcome_alert(self):
        return {"pool": {"pool_address": "pool", "token_address": s.SOL_MINT, "price_usd": 2, "mcap_usd": 200000},
                "created_at": s.iso(NOW), "obs_price_usd": 1, "obs_mcap_usd": 100000}

    def test_a03_zero_price_is_total_loss_not_a_mcap_gain(self):
        state = {"market": {s.SOL_MINT: {"latest_seen_at": s.iso(NOW + 86400),
                   "latest_price_usd": 0, "latest_mcap_usd": 200000}}}
        stats = s.update_signal_outcomes(state, [self.outcome_alert()], s.iso(NOW + 86400),
                                        {"signal_outcomes_retention_days": 0})
        checkpoint = state["signal_outcomes"][s.SOL_MINT]["horizons"]["24h"]
        self.assertEqual((checkpoint["return_pct"], checkpoint["return_basis"]), (-100, "price"))
        self.assertEqual(stats["positive_24h_pct"], 0)
        snapshot = {"price_usd": None, "mcap_usd": 0}
        self.assertEqual(s.outcome_return_pct({"caught_mcap_usd": 100}, snapshot), -100)
        self.assertEqual(snapshot["return_basis"], "mcap_proxy")
        self.assertIsNone(s.outcome_return_pct({"caught_price_usd": 1}, {"price_usd": None}))

    def test_a04_missing_historical_entry_is_not_backfilled_from_current_pool(self):
        item = self.outcome_alert()
        item.pop("obs_price_usd")
        item.pop("obs_mcap_usd")
        state = {"market": {s.SOL_MINT: {"latest_seen_at": s.iso(NOW + 86400),
                   "latest_price_usd": 2, "latest_mcap_usd": 200000}}}
        stats = s.update_signal_outcomes(state, [item], s.iso(NOW + 86400), {"signal_outcomes_retention_days": 0})
        row = state["signal_outcomes"][s.SOL_MINT]
        self.assertIsNone(row["caught_price_usd"])
        self.assertIsNone(row["caught_mcap_usd"])
        self.assertEqual(row["entry_quality_status"], "unknown")
        self.assertEqual(stats["with_24h"], 0)
        self.assertIsNone(stats["median_return_24h_pct"])
        item["pool"]["market_snapshot_at"] = NOW
        fresh = {}
        s.update_signal_outcomes(fresh, [item], s.iso(NOW), {})
        self.assertEqual(fresh["signal_outcomes"][s.SOL_MINT]["caught_price_usd"], 2)

    def test_a05_future_endpoint_cannot_freeze_a_horizon(self):
        report_at, source_at = NOW + 86400 - 60, NOW + 86400 + 60
        pool = s.Pool("pool", token_address=s.SOL_MINT, price_usd=2, mcap_usd=200000,
                      source="dexscreener", market_snapshot_at=source_at)
        state = {}
        s.record_market_observations(state, [pool], s.iso(report_at))
        stats = s.update_signal_outcomes(state, [self.outcome_alert()], s.iso(report_at),
                                        {"signal_outcomes_retention_days": 0})
        self.assertEqual(stats["with_24h"], 0)
        self.assertEqual(state["signal_outcomes"][s.SOL_MINT]["horizons"], {})
        stats = s.update_signal_outcomes(state, [], s.iso(source_at), {"signal_outcomes_retention_days": 0})
        self.assertEqual(stats["with_24h"], 1)

    def test_a03_a05_legacy_outcomes_are_retained_but_not_trusted_or_exported(self):
        legacy = {"caught_at": s.iso(NOW), "caught_price_usd": 2, "caught_mcap_usd": 200000,
                  "horizons": {"24h": {"at": s.iso(NOW + 86400), "target_at": s.iso(NOW + 86400),
                              "return_pct": 0, "quality_status": "complete"}}}
        thesis = self.thesis()
        thesis["token_address"] = s.SOL_MINT
        state = {"signal_outcomes": {s.SOL_MINT: copy.deepcopy(legacy)},
                 "pools": {"pool": {"signal_thesis": thesis}},
                 "market": {s.SOL_MINT: {"latest_seen_at": s.iso(NOW + 200 * 86400), "latest_price_usd": 3}}}
        stats = s.update_signal_outcomes(state, [], s.iso(NOW + 200 * 86400), {"signal_outcomes_max_tokens": 1})
        self.assertEqual(stats["methodology_version"], 2)
        self.assertEqual((stats["tracked"], stats["legacy_unverified"], stats["with_24h"]), (1, 1, 0))
        self.assertEqual(state["signal_outcomes"][s.SOL_MINT]["horizons"], legacy["horizons"])
        earlier = self.outcome_alert()
        earlier["created_at"] = s.iso(NOW - 3600)
        s.update_signal_outcomes(state, [earlier], s.iso(NOW + 200 * 86400), {})
        self.assertEqual(state["signal_outcomes"][s.SOL_MINT]["caught_at"], legacy["caught_at"])
        self.assertEqual(state["signal_outcomes"][s.SOL_MINT]["horizons"], legacy["horizons"])
        with patch.object(s, "load_deleted_tokens", return_value={"tokens": set(), "pools": set()}):
            ledger = s.build_history_ledger({"alerts": []}, state, {}, s.iso(NOW + 200 * 86400))
        self.assertFalse(any(row["event"]["event_type"].startswith("outcome_") for row in ledger["events"]))

    def test_a06_optional_project_context_failures_leave_onchain_enrichment_available(self):
        item = self.outcome_alert()
        for failure in (RuntimeError("poll timeout"), None, {"status": "failed", "error": "unavailable"}):
            http = Mock()
            http.post_json.return_value = {"task_id": "task"}
            if isinstance(failure, Exception):
                http.get_json.side_effect = failure
            else:
                http.get_json.return_value = failure
            with patch.object(s, "bright_data_token", return_value="test"), \
                    patch.object(s, "fetch_gmgn_token_info", return_value={}), \
                    patch.object(s, "fetch_dex_token_info", return_value={}), \
                    patch.object(s, "fetch_official_x_profiles", return_value=[]), patch.object(s.time, "sleep"):
                result = s.enrich_alerts_with_social(http, [copy.deepcopy(item)], {"social_enabled": False}, {})[0]
            self.assertEqual(result["obs_price_usd"], 1)
            self.assertTrue(result["token_intel"]["failures"])
            self.assertEqual(result["token_intel"]["context"], [])
        http.post_json.return_value = None
        self.assertTrue(s.request_project_context(http, s.Pool("pool"), {}, "test")[0]["error"])
        http.post_json.return_value = {"task_id": "task"}
        self.assertTrue(s.request_project_context(http, s.Pool("pool"), {"social_timeout_seconds": 0}, "test")[0]["error"])

    def test_a07_foreign_profile_and_cached_identity_are_rejected(self):
        foreign = {"address": s.SOL_MINT.lower(), "name": "Other", "dev": {"creator_address": "wrong"},
                   "link": {"twitter_username": "wrong"}}
        config = {"_gmgn_profile_cache": {s.SOL_MINT: {"source": "gmgn", "name": "cached-other"}},
                  "_gmgn_token_info_cache": {s.SOL_MINT: foreign}}
        with patch.dict(s.os.environ, {"GMGN_API_KEY": "test"}), patch.object(s, "run_gmgn_cli", return_value=foreign):
            self.assertEqual(s.fetch_gmgn_token_info(config, s.SOL_MINT), {})
        self.assertNotIn(s.SOL_MINT, config["_gmgn_token_info_cache"])
        self.assertNotIn(s.SOL_MINT, config["_gmgn_profile_cache"])
        valid = dict(foreign, address=s.SOL_MINT, name="Correct")
        with patch.dict(s.os.environ, {"GMGN_API_KEY": "test"}), patch.object(s, "run_gmgn_cli", return_value=valid):
            profile = s.fetch_gmgn_token_info({}, s.SOL_MINT)
        self.assertEqual((profile["name"], profile["token_address"], profile["identity_version"]), ("Correct", s.SOL_MINT, 2))

    def test_a08_official_profile_fields_are_bound_to_the_requested_user_object(self):
        http = Mock()
        http.session.get.return_value.text = ('<script>{"users":['
            '{"screen_name":"neighbor","description":"WRONG BIO","followers_count":999},'
            '{"legacy":{"screen_name":"target","description":"RIGHT BIO","followers_count":5,"name":"Right"}}'
            ']}</script>')
        profile = {"links": [{"url": "https://x.com/target", "type": "twitter"}]}
        rows = s.fetch_official_x_profiles(http, profile, {}, {})
        self.assertEqual((rows[0]["author"], rows[0]["description"], rows[0]["followers"]), ("target", "RIGHT BIO", 5))
        http.session.get.return_value.text = '{"description":"UNBOUND"},{"screen_name":"target"}'
        self.assertEqual(s.fetch_official_x_profiles(http, profile, {}, {}), [])

    def test_a09_aliases_are_one_post_but_distinct_quotes_and_reposts_remain_distinct(self):
        pool = s.Pool("pool", token_address=s.SOL_MINT, name="Test Token", symbol="TEST")
        urls = ["https://x.com/Alice/status/123", "https://twitter.com/alice/status/123",
                "https://x.com/alice/status/123?ref=test"]
        payload = {"results": [{"url": url, "title": "TEST Solana token", "description": "TEST Solana token",
                                "relevance_score": 1} for url in urls]}
        with patch.object(s, "bright_data_token", return_value="test"), \
                patch.object(s, "request_bright_data_discover", return_value=payload), patch.object(s.time, "sleep"):
            config = {"social_queries_per_token": 1, "social_scrape_x_posts": False}
            result = s.fetch_social_snapshot(Mock(), pool, config, {})
            self.assertEqual((result["x_posts"], result["unique_authors"], result["heat"], result["caller_graph"][0]["posts"]), (1, 1, "quiet", 1))
            self.assertEqual(result["results"][0]["url"], "https://x.com/alice/status/123")
            payload["results"].extend([dict(payload["results"][0], url="https://x.com/alice/status/124"),
                                      dict(payload["results"][0], url="https://x.com/bob/status/125")])
            result = s.fetch_social_snapshot(Mock(), pool, config, {})
            self.assertEqual((result["x_posts"], result["unique_authors"]), (3, 2))
