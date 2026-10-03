import copy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import scanner as s
from runtime_checkpoint import build_checkpoint, decode_checkpoint, restore_checkpoint
from rpc_budget import MonthlyRpcBudget, configure_monthly_budgets
from scan_scheduling import targeted_profile, fast_candidate_pools, scan_freshness_reference
from launch_history import advance_launch_history
from prospective_evidence import capture_evaluation_rows
from signal_evaluation import evaluate_signals, EvaluationOptions


class RuntimeArchitectureTests(unittest.TestCase):
    def test_targeted_report_without_deep_time_cannot_postpone_hourly_scan(self):
        report = {"generated_at": "2026-10-03T01:30:00Z", "scan_profile": "targeted"}
        self.assertIsNone(scan_freshness_reference(report))
        self.assertEqual(scan_freshness_reference(report, targeted=True), report["generated_at"])
        report["last_deep_scan_at"] = "2026-10-03T00:30:00Z"
        self.assertEqual(scan_freshness_reference(report), report["last_deep_scan_at"])
        self.assertEqual(scan_freshness_reference({"generated_at": report["generated_at"]}), report["generated_at"])

    def test_termination_preserves_active_state_and_monthly_usage(self):
        active = {"rpc_monthly_usage": {"2026-10": {"alchemy": {"estimated_units": 100}}},
                  "pools": {"pool": {"cursor": "preserved"}}}
        def interrupted(config, lane):
            config["_active_runtime_state"] = active
            s.interrupt_scan(15, None)
        with patch("sys.argv", ["scanner.py", "--once", "--lane", "reactivation"]), \
                patch.object(s, "load_json", return_value={}), \
                patch.object(s, "run_once", side_effect=interrupted), \
                patch.object(s, "save_runtime_state") as save, \
                patch.object(s, "write_scanner_status", return_value={}), \
                patch.object(s, "sync_remote_scan_status"):
            with self.assertRaisesRegex(SystemExit, "workflow timeout or cancellation"):
                s.main()
        self.assertIs(save.call_args.args[0], active)
        self.assertEqual(save.call_args.args[2], "failed_attempt")

    def test_checkpoint_preserves_cursors_cohorts_buffers_and_monthly_usage(self):
        state = {"_runtime": {"revision": 3, "updated_at": "2026-10-03T00:00:00Z"},
            "pools": {"p": {"helius_rolling_backlogs": [{"cursor": "opaque"}],
                            "wave_swaps": [{"signature": "receipt"}], "signal_thesis": {"cohort": [{"owner": "owner"}]}}},
            "rpc_monthly_usage": {"2026-10": {"alchemy": {"estimated_units": 100}}},
            "wallet_cache": {"rebuildable": 1}}
        before = copy.deepcopy(state)
        payload = build_checkpoint(state)
        restored, changed = restore_checkpoint({}, payload)
        self.assertTrue(changed)
        self.assertEqual(restored["pools"], state["pools"])
        self.assertEqual(restored["rpc_monthly_usage"], state["rpc_monthly_usage"])
        self.assertEqual(restored["wallet_cache"], {})
        self.assertEqual(state, before)
        self.assertEqual(restore_checkpoint(state, payload), (state, False))

    def test_corrupt_or_oversize_checkpoint_is_not_partially_restored(self):
        payload = build_checkpoint({"value": "preserved"})
        payload["sha256"] = "wrong"
        with self.assertRaises(ValueError): decode_checkpoint(payload)
        with patch("runtime_checkpoint.MAX_ENCODED_BYTES", 8):
            with self.assertRaises(ValueError): build_checkpoint({"value": "no truncation"})

    def test_shared_monthly_budget_survives_fast_pass_and_rolls_by_provider(self):
        ledger = {}
        now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        first = MonthlyRpcBudget(ledger, "alchemy", 120, now)
        self.assertTrue(first.reserve(100))
        second = MonthlyRpcBudget(ledger, "alchemy", 120, now)
        self.assertFalse(second.reserve(40))
        self.assertEqual(second.remaining, 20)
        self.assertTrue(MonthlyRpcBudget(ledger, "chainstack", 100, now).reserve(1))
        self.assertEqual(MonthlyRpcBudget(ledger, "alchemy", 120, datetime(2026, 11, 1, tzinfo=timezone.utc)).remaining, 120)

    def test_targeted_profile_changes_budgets_not_token_filter(self):
        config = {"age_min_hours": 24, "age_max_hours": 360, "mcap_min_usd": 0,
                  "lanes": {"reactivation": {"age_min_hours": 24, "age_max_hours": 360, "enabled": True}}}
        fast = targeted_profile(config)
        self.assertEqual(fast["active_pool_limit"], 6)
        self.assertEqual(fast["age_max_hours"], 360)
        self.assertEqual(fast["lanes"]["reactivation"]["age_min_hours"], 24)
        self.assertNotIn("active_pool_limit", config)
        self.assertFalse(fast["helius_initial_backfill_enabled"])

    def test_fast_queue_skips_expired_candidates_and_not_yet_due_theses(self):
        pools = [SimpleNamespace(pool_address=key) for key in ("expired", "candidate", "due", "later")]
        state = {"discovery_queue": [{"pool_address": "expired", "expires_at": "2026-10-02T00:00:00Z"},
            {"pool_address": "candidate", "expires_at": "2026-10-04T00:00:00Z"}], "pools": {
            "due": {"signal_thesis": {"status": "unknown", "next_check_at": "2026-10-02T00:00:00Z"}},
            "later": {"signal_thesis": {"status": "intact", "next_check_at": "2026-10-04T00:00:00Z"}}}}
        self.assertEqual([pool.pool_address for pool in fast_candidate_pools(pools, state, s.parse_timestamp("2026-10-03T00:00:00Z"))], ["candidate", "due"])

    def test_missing_or_future_market_timestamp_cannot_enter_prospective_evaluation(self):
        pool = s.Pool(pool_address="signal", token_address="signal", pair_created_at=1790812800)
        for at in (None, "2026-10-03T02:00:00Z", "2026-10-03T00:00:00"):
            state = {"market": {"signal": {"latest_price_usd": 1, "latest_seen_at": at}}}
            data = capture_evaluation_rows(state, [pool], [], [{"pool": {"token_address": "signal"}}],
                "2026-10-03T00:00:00Z", "v1", s.outcome_market_snapshot)
            self.assertEqual(data["episodes"], {})

    def test_launch_task_is_independent_of_six_hour_rolling_retention(self):
        pool = SimpleNamespace(pair_created_at=1000, pool_address="pool", age_hours=lambda: 240)
        rpc = Mock()
        tx = {"blockTime": 1002, "transaction": {"signatures": ["buy"]}}
        rpc.transactions_for_address.side_effect = [
            {"data": [tx], "_provider": "alchemy", "paginationToken": "next"},
            {"data": [tx], "_provider": "alchemy"}]
        parser = lambda tx, pool: {"kind": "buy", "token_recipient": "owner", "token_amount": 12}
        task = advance_launch_history(rpc, pool, {}, parser, now="2026-10-03T00:00:00Z")
        self.assertEqual(task["status"], "pending")
        task = advance_launch_history(rpc, pool, task, parser, now="2026-10-03T01:00:00Z")
        self.assertEqual(task["status"], "query_exhausted")
        self.assertFalse(task["coverage_complete"])
        self.assertEqual(task["owners"]["owner"]["bought_tokens"], 12)
        self.assertFalse(task["current_balances_verified"])
        self.assertEqual(rpc.transactions_for_address.call_args.kwargs["block_time"]["gte"], 1000)

    def test_launch_provider_change_cannot_reuse_opaque_cursor(self):
        pool = SimpleNamespace(pair_created_at=1000, pool_address="pool")
        rpc = Mock()
        rpc.transactions_for_address.return_value = {"data": [], "_provider": "helius"}
        task = {"from_timestamp": 1000, "to_timestamp": 22600, "cursor": "alchemy-cursor", "provider": "alchemy",
                "transactions": 3, "owners": {}, "seen_signatures": []}
        result = advance_launch_history(rpc, pool, task, Mock(), now="2026-10-03T00:00:00Z")
        self.assertEqual(result["reason"], "provider_cursor_mismatch")
        self.assertEqual(result["transactions"], 3)

    def test_partial_cohort_continues_without_false_complete_or_rebuy_restoration(self):
        cohort = [{"owner": str(i), "attributed_tokens": 100, "current_retained_tokens": 100} for i in range(3)]
        thesis = {"cohort": cohort, "original_retained_tokens": 300, "cohort_token_coverage_pct": 100,
                  "cohort_wallet_coverage_pct": 100, "supply": 1000, "status": "intact"}
        rpc = Mock(); rpc.token_balance.return_value = 0
        pool = s.Pool(pool_address="pool", token_address="token")
        config = {"_cohort_check_wallet_limit": 2}
        s.recheck_signal_thesis(rpc, pool, {"signal_thesis": thesis}, config, "2026-10-03T00:00:00Z")
        self.assertEqual(thesis["status"], "unknown")
        self.assertNotIn("last_complete_check_at", thesis)
        rpc.token_balance.return_value = 100
        s.recheck_signal_thesis(rpc, pool, {"signal_thesis": thesis}, config, "2026-10-03T00:15:00Z")
        self.assertEqual(rpc.token_balance.call_count, 3)
        self.assertEqual(thesis["last_complete_check_at"], "2026-10-03T00:15:00Z")
        self.assertEqual(cohort[0]["current_retained_tokens"], 0)

    def test_prospective_collection_freezes_features_and_explicit_controls(self):
        pools = [s.Pool(pool_address=x, token_address=x, mcap_usd=100000, volume_1h_usd=1000, pair_created_at=1790812800) for x in ("signal", "control")]
        state = {"market": {x: {"latest_price_usd": 1, "latest_mcap_usd": 100000, "latest_liquidity_usd": 10000,
                                "latest_seen_at": "2026-10-03T00:00:00Z"} for x in ("signal", "control")}}
        summaries = [{"pool": {"token_address": "control"}, "detector_executed": True, "trade_fetch": {"source": "enhanced_transactions", "passes": [{"coverage_complete": True}]}}]
        alerts = [{"pool": {"token_address": "signal"}, "score": 50, "signal_family": "reactivation_wave"}]
        dataset = capture_evaluation_rows(state, pools, summaries, alerts, "2026-10-03T00:00:00Z", "version1", s.outcome_market_snapshot)
        self.assertFalse(dataset["controls"]["control"]["signal_present"])
        alerts[0]["score"] = 100
        capture_evaluation_rows(state, pools, summaries, alerts, "2026-10-03T00:15:00Z", "version1", s.outcome_market_snapshot)
        self.assertEqual(dataset["episodes"]["signal"]["caught_score"], 50)
        summary = evaluate_signals({"generated_at": "2026-10-03T00:15:00Z", "signal_evaluation_dataset": dataset}, options=EvaluationOptions(bootstrap_samples=100))["summary"]
        self.assertEqual(summary["counts"]["primary_controls"], 1)
        self.assertFalse(summary["edge_claim"])

    def test_prospective_horizon_uses_source_time_not_run_time(self):
        start, end = "2026-10-03T00:00:00Z", "2026-10-03T01:00:00Z"
        source_at = "2026-10-03T00:45:00Z"
        pool = s.Pool(pool_address="pool", token_address="signal", source="dexscreener",
            price_usd=1, mcap_usd=100000, liquidity_usd=10000, pair_created_at=1790812800,
            market_snapshot_at=s.parse_timestamp(start))
        state = {}
        s.record_market_observations(state, [pool], start)
        dataset = capture_evaluation_rows(state, [pool], [], [{"pool": {"token_address": "signal"}}],
            start, "v1", s.outcome_market_snapshot)
        pool.price_usd = 2
        pool.market_snapshot_at = s.parse_timestamp(source_at)
        s.record_market_observations(state, [pool], end)
        self.assertEqual(s.parse_timestamp(state["market"]["signal"]["latest_seen_at"]),
            s.parse_timestamp(source_at))
        capture_evaluation_rows(state, [pool], [], [], end, "v1", s.outcome_market_snapshot)
        self.assertNotIn("1h", dataset["episodes"]["signal"]["horizons"])
        result = evaluate_signals({"generated_at": end, "signal_evaluation_dataset": dataset},
            options=EvaluationOptions(horizons=("1h",), bootstrap_samples=100))
        checkpoint = result["episode_diagnostics"][0]["outcomes"]["1h"]
        self.assertEqual(checkpoint["status"], "missing")
        self.assertIsNone(checkpoint["return_pct"])
        for at, stale in ((0, False), (s.parse_timestamp(source_at), True)):
            with self.subTest(source_at=at, stale=stale):
                pool.market_snapshot_at, pool.market_snapshot_stale = at, stale
                unknown_state = {}
                s.record_market_observations(unknown_state, [pool], end)
                entry = unknown_state["market"]["signal"]
                self.assertTrue(entry["market_snapshot_stale"])
                self.assertIsNone(s.outcome_market_snapshot(entry, end))
                if not at:
                    self.assertIsNone(entry["latest_seen_at"])

    def test_prospective_missing_price_is_not_a_measured_zero_loss(self):
        self.assertIsNone(s.Pool(pool_address="unknown").price_usd)
        self.assertIsNone(s.gecko_pool_from_item({"attributes": {"address": "pool"}}, "gecko").price_usd)
        self.assertIsNone(s.dexscreener_pool_from_pair({"pairAddress": "pool"}, "dexscreener").price_usd)
        start, end = "2026-10-03T00:00:00Z", "2026-10-03T01:00:00Z"
        for exit_price, basis, expected in ((None, "mcap_proxy", 20), (0, "price", -100)):
            with self.subTest(exit_price=exit_price):
                pool = s.Pool(pool_address="pool", token_address="signal", source="dexscreener",
                    price_usd=1, mcap_usd=100000, liquidity_usd=10000, pair_created_at=1790812800,
                    market_snapshot_at=s.parse_timestamp(start))
                state = {}
                s.record_market_observations(state, [pool], start)
                dataset = capture_evaluation_rows(state, [pool], [], [{"pool": {"token_address": "signal"}}],
                    start, "v1", s.outcome_market_snapshot)
                pool.price_usd, pool.mcap_usd = exit_price, 120000
                pool.market_snapshot_at = s.parse_timestamp(end)
                s.record_market_observations(state, [pool], end)
                capture_evaluation_rows(state, [pool], [], [], end, "v1", s.outcome_market_snapshot)
                frozen = dataset["episodes"]["signal"]["horizons"]["1h"]
                self.assertEqual(frozen["price_usd"], exit_price)
                result = evaluate_signals({"generated_at": end, "signal_evaluation_dataset": dataset},
                    options=EvaluationOptions(horizons=("1h",), bootstrap_samples=100))
                checkpoint = result["episode_diagnostics"][0]["outcomes"]["1h"]
                self.assertEqual(checkpoint["status"], "eligible")
                self.assertEqual(checkpoint["return_basis"], basis)
                self.assertAlmostEqual(checkpoint["return_pct"], expected)
        self.assertIsNone(s.outcome_market_snapshot({"latest_price_usd": 1, "latest_mcap_usd": 100000}, end))
        self.assertIsNone(s.outcome_market_snapshot({"latest_seen_at": end, "latest_price_usd": None}, end))

    def test_prospective_controls_require_executed_unsuppressed_complete_scan(self):
        at = "2026-10-03T00:00:00Z"
        pool = s.Pool(pool_address="pool", token_address="control", pair_created_at=1790812800)
        base = {"pool": {"token_address": "control"}, "detector_executed": True,
            "wallet_classification_required": True, "candidate_buys": 3, "classified_buys": 3,
            "trade_fetch": {"source": "enhanced_transactions", "passes": [{"coverage_complete": True}]}}
        cases = [
            ("fully_classified", {}, True),
            ("detector_missing", {"detector_executed": None}, False),
            ("detector_not_run", {"detector_executed": False}, False),
            ("suppressed", {"market_snapshot_suppressed": True}, False),
            ("stale_pool", {"pool": {"token_address": "control", "market_snapshot_stale": True}}, False),
            ("stale_activity", {"trade_fetch": {**base["trade_fetch"], "market_activity_stale": True}}, False),
            ("partial_pass", {"trade_fetch": {**base["trade_fetch"],
                "passes": [{"coverage_complete": True}, {"coverage_complete": False}]}}, False),
            ("classic_incomplete", {"classified_buys": 0}, False),
            ("classification_policy_unknown", {"wallet_classification_required": None, "classified_buys": 0}, False),
            ("wave_only", {"wallet_classification_required": False, "classified_buys": 0}, True),
        ]
        for name, changes, eligible in cases:
            with self.subTest(case=name):
                state = {"market": {"control": {"latest_seen_at": at, "latest_price_usd": 1,
                    "latest_mcap_usd": 100000, "latest_liquidity_usd": 10000}}}
                summary = {**copy.deepcopy(base), **copy.deepcopy(changes)}
                if name == "detector_missing":
                    summary.pop("detector_executed")
                dataset = capture_evaluation_rows(state, [pool], [summary], [], at, "v1", s.outcome_market_snapshot)
                self.assertEqual("control" in dataset["controls"], eligible)
