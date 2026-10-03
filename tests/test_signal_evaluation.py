import copy
import json
import itertools
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from signal_evaluation import CostScenario, EvaluationOptions, HORIZONS, evaluate_signals


ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def at(hours=0):
    return (START + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def observation(token="signal", hours=0, return_pct=20, horizon="24h", **changes):
    caught = changes.pop("caught_at", at(hours))
    caught_dt = datetime.fromisoformat(caught.replace("Z", "+00:00"))
    due = (caught_dt + timedelta(seconds=HORIZONS[horizon])).isoformat().replace("+00:00", "Z")
    row = {
        "token_address": token, "caught_at": caught,
        "strategy_version": "strategy-v1", "config_version": "config-v1", "signal_family": "reactivation_wave",
        "caught_age_hours": 24, "caught_mcap_usd": 100000, "caught_liquidity_usd": 20000,
        "caught_price_usd": 1, "caught_score": 50,
        "horizons": {horizon: {"at": due, "target_at": due, "price_usd": 1 + return_pct / 100,
                                "return_pct": return_pct, "quality_status": "complete"}},
    }
    row.update(changes)
    return row


def control(token="control", hours=0, return_pct=0, **changes):
    return observation(token, hours, return_pct, **{
        "signal_present": False, "selection_at": at(hours),
        "selection_method": "prospective_universe", **changes,
    })


def options(**changes):
    return EvaluationOptions(**{"horizons": ("24h",), "holdout_start": at(240),
                                "bootstrap_samples": 100, "min_sample": 2, **changes})


def evaluate(signals=None, controls=None, opts=None, as_of=at(720)):
    return evaluate_signals({"generated_at": as_of, "episodes": signals or [], "controls": controls or []},
                            options=opts or options())


def metrics(result, split="train", stratum=0, horizon="24h"):
    return result["strata"][stratum]["horizons"][horizon]["splits"][split]


def status(row, **kwargs):
    return evaluate([row], **kwargs)["episode_diagnostics"][0]["outcomes"]["24h"]["status"]


class IntakeAndDedupeTests(unittest.TestCase):
    def test_empty_is_not_evidence_and_summary_is_json_safe(self):
        result = evaluate()
        self.assertEqual(result["summary"]["counts"]["primary_signals"], 0)
        self.assertEqual(result["summary"]["horizons"]["24h"]["complete_price_pairs"], 0)
        self.assertFalse(result["summary"]["edge_claim"])
        json.dumps(result, allow_nan=False)

    def test_as_of_is_required_not_wall_clock_and_naive_timestamps_rejected(self):
        for payload in ({}, {"generated_at": "2026-01-01T00:00:00"}, []):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                evaluate_signals(payload)
        result = evaluate_signals({"episodes": [{"token_address": "epoch", "caught_at": 0}]}, as_of=0)
        self.assertEqual(result["summary"]["counts"]["primary_signals"], 1)
        result = evaluate_signals({"as_of": 0, "generated_at": at(720),
                                   "episodes": [{"token_address": "epoch", "caught_at": 0}]})
        self.assertEqual(result["summary"]["as_of"], "1970-01-01T00:00:00Z")

    def test_invalid_rows_and_future_catches_are_accounted_for(self):
        result = evaluate([None, {}, {**observation(), "caught_at": "bad"}, observation(caught_at=at(800))])
        self.assertEqual(result["summary"]["counts"]["invalid_rows"], 3)
        self.assertEqual(result["summary"]["counts"]["future_captures"], 1)

    def test_duplicate_captures_with_different_ids_are_one_sample(self):
        first = observation(episode_id="first")
        second = observation(episode_id="alias")
        result = evaluate([second, first])
        self.assertEqual(metrics(result)["gross_signal"]["n"], 1)
        self.assertEqual(result["summary"]["counts"]["duplicate_captures"], 1)

    def test_repeated_token_earliest_missing_is_not_replaced_with_later_winner(self):
        result = evaluate([observation(hours=72, return_pct=1000), observation(horizons={})])
        self.assertEqual(result["summary"]["counts"]["repeated_token"], 1)
        self.assertEqual(metrics(result)["signal_outcome_status_counts"], {"missing": 1})
        self.assertEqual(metrics(result)["gross_signal"]["n"], 0)

    def test_version_and_cohort_conflicts_fail_closed(self):
        for duplicate in (observation(config_version="changed"), control("signal")):
            with self.subTest(duplicate=duplicate):
                result = evaluate([observation(), duplicate] if duplicate.get("signal_present") is None else [observation()],
                                  [duplicate] if duplicate.get("signal_present") is False else [])
                self.assertEqual(result["summary"]["counts"]["conflicting_capture"], 1)
                self.assertEqual(result["summary"]["counts"]["primary_signals"], 0)

    def test_episode_id_cannot_refer_to_two_tokens_or_catch_times(self):
        result = evaluate([observation("a", episode_id="shared"), observation("b", episode_id="shared")])
        self.assertEqual(result["summary"]["counts"]["reused_episode_id"], 2)
        self.assertEqual(result["summary"]["counts"]["primary_signals"], 0)

    def test_first_checkpoint_is_frozen_even_if_later_snapshot_is_better(self):
        first = observation(return_pct=-20)
        later = observation(return_pct=1000)
        later["horizons"]["24h"]["at"] = at(25)
        for rows in ([first, later], [later, first]):
            self.assertAlmostEqual(metrics(evaluate(rows))["gross_signal"]["mean_pct"], -20)

    def test_later_duplicate_conflicts_cannot_erase_earlier_checkpoint_by_order(self):
        first = observation(return_pct=-20)
        later = observation(return_pct=1000)
        conflicting_later = observation(return_pct=2000)
        later["horizons"]["24h"]["at"] = at(25)
        conflicting_later["horizons"]["24h"]["at"] = at(25)
        for rows in itertools.permutations([first, later, conflicting_later]):
            self.assertAlmostEqual(metrics(evaluate(list(rows)))["gross_signal"]["mean_pct"], -20)

    def test_equivalent_checkpoint_time_formats_and_unused_peak_fields_dedupe(self):
        second = observation()
        second["horizons"]["24h"].update(at="2026-01-02T01:00:00+01:00", max_return_pct=1000)
        result = evaluate([observation(), second])
        self.assertEqual(metrics(result)["gross_signal"]["n"], 1)

    def test_empty_checkpoints_merge_but_unplaceable_exports_fail_closed(self):
        empty = observation(horizons={"24h": {}})
        for rows in ([empty, observation()], [observation(), empty]):
            self.assertEqual(metrics(evaluate(rows))["gross_signal"]["n"], 1)
        invalid = observation()
        invalid["horizons"]["24h"]["at"] = "bad"
        for rows in ([invalid, observation()], [observation(), invalid]):
            self.assertEqual(metrics(evaluate(rows))["signal_outcome_status_counts"], {"conflicting_checkpoint": 1})

    def test_conflicting_same_timestamp_checkpoint_is_not_selected_by_return(self):
        result = evaluate([observation(return_pct=-20), observation(return_pct=1000)])
        self.assertEqual(metrics(result)["signal_outcome_status_counts"], {"conflicting_checkpoint": 1})

    def test_saved_report_summary_cannot_become_token_returns_or_controls(self):
        payload = {
            "generated_at": at(720), "config": {"config_version": "current", "strategy_version": "current"},
            "stats": {"signal_outcomes": {"tracked": 500, "with_24h": 400, "median_return_24h_pct": 500}},
            "alerts": [{"created_at": at(), "score": 99, "pool": {
                "token_address": "a", "mcap_usd": 500, "liquidity_usd": 600, "age_hours": 900, "price_usd": 3}}],
            "universe": [{"token_address": "b"}], "summaries": [{"token_address": "c"}],
        }
        result = evaluate_signals(payload, options=options())
        self.assertEqual(metrics(result)["gross_signal"]["n"], 0)
        self.assertEqual(result["summary"]["counts"]["primary_controls"], 0)
        self.assertEqual(result["episode_diagnostics"][0]["config_version"], "unknown")
        self.assertEqual(metrics(result)["feature_diagnostics"]["caught_score"]["missing_at_capture"], 1)
        self.assertTrue(any("aggregate" in warning for warning in result["summary"]["warnings"]))

    def test_tracker_mapping_and_wrapped_snapshot_fit_saved_data(self):
        row = observation()
        del row["token_address"]
        row.pop("strategy_version")
        payload = {"report": {"generated_at": at(720), "config": {"strategy_version": "now"},
                              "signal_outcomes": {"mint": row}}}
        result = evaluate_signals(payload, options=options())
        self.assertEqual(result["episode_diagnostics"][0]["token_address"], "mint")
        self.assertEqual(metrics(result)["gross_signal"]["n"], 1)
        self.assertEqual(result["strata"][0]["strategy_version"], "unknown")

    def test_saved_state_and_nested_prospective_contract(self):
        state = {"report": {"generated_at": at(720), "stats": {"signal_outcomes": {"tracked": 1}}},
                 "state": {"signal_outcomes": {"mint": observation("mint")}}}
        prospective = {"generated_at": at(720), "signal_evaluation_dataset": {
            "episodes": [observation("mint")], "controls": [control()]}}
        self.assertEqual(metrics(evaluate_signals(state, options=options()))["gross_signal"]["n"], 1)
        self.assertFalse(any("aggregate outcomes only" in warning for warning in evaluate_signals(state, options=options())["summary"]["warnings"]))
        self.assertEqual(evaluate_signals(prospective, options=options())["summary"]["counts"]["selected_pairs"], 1)

    def test_input_is_not_mutated_and_no_network_is_used(self):
        payload = {"generated_at": at(720), "episodes": [observation()], "controls": [control()]}
        original = copy.deepcopy(payload)
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            result = evaluate_signals(payload, options=options())
        self.assertEqual(payload, original)
        self.assertEqual(result, evaluate_signals(payload, options=options()))

    def test_invalid_collection_shape_is_not_silently_empty(self):
        for key in ("episodes", "controls", "signal_outcomes", "alerts"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                evaluate_signals({"generated_at": at(720), key: "invalid"}, options=options())


class CheckpointTests(unittest.TestCase):
    def test_pending_missing_and_future_outcome_are_distinct(self):
        self.assertEqual(status(observation(horizons={}), as_of=at(23)), "pending")
        self.assertEqual(status(observation(horizons={}), as_of=at(24)), "missing")
        self.assertEqual(status(observation(), as_of=at(23)), "future_outcome")

    def test_delay_boundary_is_inclusive_and_late_is_excluded(self):
        for seconds, expected in ((0, "eligible"), (3600, "eligible"), (3601, "late"), (-1, "early")):
            row = observation()
            row["horizons"]["24h"]["at"] = at(24 + seconds / 3600)
            with self.subTest(seconds=seconds):
                self.assertEqual(status(row), expected)

    def test_invalid_targets_quality_and_stale_data_are_excluded(self):
        for field, value, expected in (
            ("target_at", at(25), "invalid_timestamp"), ("at", "not-a-date", "invalid_timestamp"),
            ("target_at", None, "invalid_timestamp"), ("quality_status", "delayed", "late"),
            ("quality_status", "partial", "incomplete"), ("quality_status", "unverified", "incomplete"),
            ("market_snapshot_stale", True, "incomplete"),
        ):
            row = observation()
            row["horizons"]["24h"][field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(status(row), expected)

    def test_return_price_is_preferred_and_mcap_proxy_not_execution(self):
        row = observation(return_pct=20)
        row["horizons"]["24h"].update(return_pct=999, mcap_usd=500000)
        result = evaluate([row])
        self.assertAlmostEqual(metrics(result)["gross_signal"]["mean_pct"], 20)
        del row["horizons"]["24h"]["price_usd"]
        del row["horizons"]["24h"]["return_pct"]
        result = evaluate([row], [control()])
        self.assertEqual(metrics(result)["gross_signal"]["mean_pct"], 400)
        self.assertEqual(metrics(result)["complete_price_pairs"], 0)
        self.assertEqual(metrics(result)["gross_signal"]["return_basis_counts"], {"mcap_proxy": 1})

    def test_reported_returns_have_unverified_basis_not_matched_evidence(self):
        row = observation(caught_price_usd=None)
        result = evaluate([row], [control()])
        self.assertEqual(metrics(result)["gross_signal"]["n"], 1)
        self.assertEqual(metrics(result)["complete_price_pairs"], 0)

    def test_nonfinite_boolean_and_below_minus_100_returns_rejected(self):
        for value in (None, float("nan"), float("inf"), "NaN", True, -100.01):
            row = observation(caught_price_usd=None, caught_mcap_usd=None)
            row["horizons"]["24h"].update(price_usd=None, return_pct=value)
            with self.subTest(value=value):
                result = evaluate([row])
                self.assertEqual(metrics(result)["signal_outcome_status_counts"], {"invalid_return": 1})
                json.dumps(result, allow_nan=False)

    def test_zero_exit_is_total_loss_and_costs_are_not_clipped(self):
        result = evaluate([observation(return_pct=-100)], [control()], opts=options(cost_scenarios=(CostScenario("test", 100, 200),)))
        scenario = metrics(result)["net_scenarios"]["test"]
        self.assertEqual(scenario["all_signal_net"]["mean_pct"], -103)
        self.assertEqual(scenario["cost_pct"], 3)
        self.assertEqual(scenario["execution"], "estimate_only")

    def test_horizons_are_evaluated_independently(self):
        row = observation()
        row["horizons"]["1h"] = {"at": at(100), "target_at": at(1), "price_usd": 10}
        result = evaluate([row], opts=options(horizons=("1h", "24h", "72h")))
        statuses = result["episode_diagnostics"][0]["outcomes"]
        self.assertEqual({h: x["status"] for h, x in statuses.items()}, {"1h": "late", "24h": "eligible", "72h": "missing"})


class MatchingAndHoldoutTests(unittest.TestCase):
    def test_all_calipers_and_time_are_required(self):
        for field, value in (("caught_age_hours", 100), ("caught_mcap_usd", 200000),
                             ("caught_liquidity_usd", 50000), ("caught_liquidity_usd", 0)):
            result = evaluate([observation()], [control(**{field: value})])
            with self.subTest(field=field):
                self.assertEqual(result["summary"]["counts"]["selected_pairs"], 0)
        self.assertEqual(evaluate([observation()], [control(hours=1.0001)])["summary"]["counts"]["selected_pairs"], 0)
        boundary = control(hours=1, caught_age_hours=36, caught_mcap_usd=150000, caught_liquidity_usd=30000)
        self.assertEqual(evaluate([observation()], [boundary])["summary"]["counts"]["selected_pairs"], 1)

    def test_young_tokens_use_one_hour_floor_and_missing_age_is_not_guessed(self):
        self.assertEqual(evaluate([observation(caught_age_hours=0)], [control(caught_age_hours=0.5)])["summary"]["counts"]["selected_pairs"], 1)
        self.assertEqual(evaluate([observation(caught_age_hours=None)], [control()])["summary"]["counts"]["selected_pairs"], 0)
        signal = observation(caught_age_hours=None, pair_created_at=at(-24))
        self.assertEqual(evaluate([signal], [control()])["summary"]["counts"]["selected_pairs"], 1)

    def test_strategy_config_family_strata_must_match_and_unknown_cannot_compare(self):
        for field in ("strategy_version", "config_version", "signal_family"):
            for value in ("another", None):
                result = evaluate([observation()], [control(**{field: value})])
                with self.subTest(field=field, value=value):
                    self.assertEqual(result["summary"]["counts"]["selected_pairs"], 0)
        signals = [observation("a"), observation("b", config_version="config-v2")]
        self.assertEqual(len(evaluate(signals)["strata"]), 2)

    def test_controls_must_be_explicitly_selected_at_capture_without_signal(self):
        for field, value in (("signal_present", True), ("signal_present", None), ("signal_present", 0),
                             ("selection_at", None), ("selection_at", at(1)), ("selection_method", ""),
                             ("captured_at", at(1))):
            result = evaluate([observation()], [control(**{field: value})])
            with self.subTest(field=field, value=value):
                self.assertEqual(result["summary"]["counts"]["selected_pairs"], 0)

    def test_control_token_reuse_and_later_signals_cannot_cross_primary_samples(self):
        result = evaluate([observation("s", hours=1), observation("c", hours=72)],
                          [control("c"), control("c", hours=1)])
        self.assertEqual(result["summary"]["counts"]["primary_controls"], 1)
        self.assertEqual(result["summary"]["counts"]["primary_signals"], 1)
        self.assertEqual(len(result["control_matches"]), 1)

    def test_no_control_replacement_or_outcome_conditioned_matching(self):
        nearest_missing = control("nearest", horizons={})
        farther_winner = control("farther", hours=0.5, return_pct=1000)
        result = evaluate([observation()], [farther_winner, nearest_missing])
        self.assertEqual(result["control_matches"][0]["control_token"], "nearest")
        self.assertEqual(metrics(result)["selected_pairs"], 1)
        self.assertEqual(metrics(result)["complete_price_pairs"], 0)
        self.assertEqual(metrics(result)["pair_completion_pct"], 0)

    def test_matching_is_one_to_one_not_repeated_control_pseudoreplication(self):
        result = evaluate([observation("a"), observation("b")], [control()])
        self.assertEqual(metrics(result)["selected_pairs"], 1)
        self.assertEqual(metrics(result)["unmatched_signals"], 1)

    def test_chronological_time_blocks_and_same_time_rows_stay_together(self):
        rows = [observation("a", hours=0), observation("b", hours=100), observation("c", hours=100), observation("d", hours=200)]
        result = evaluate(rows, opts=options(holdout_start=None, holdout_fraction=0.5))
        self.assertEqual(result["summary"]["holdout"]["start_at"], at(100))
        self.assertEqual(metrics(result, "holdout")["signals"], 3)
        self.assertEqual(metrics(result, "train")["signals"], 1)
        single = evaluate([observation("a"), observation("b")], opts=options(holdout_start=None))
        self.assertIsNone(single["summary"]["holdout"]["start_at"])
        self.assertIn("chronological_holdout_unavailable", single["strata"][0]["horizons"]["24h"]["review_blockers"])

    def test_training_outcomes_and_grace_window_are_purged_at_boundary(self):
        # Target 24h plus 1h grace reaches the boundary: equality must be purged.
        result = evaluate([observation("purged", hours=215), observation("train", hours=214)],
                          [control("cp", hours=215), control("ct", hours=214)])
        self.assertEqual(metrics(result, "purged")["signals"], 1)
        self.assertEqual(metrics(result, "train")["signals"], 1)
        self.assertEqual(metrics(result, "train")["complete_price_pairs"], 1)

    def test_match_cannot_cross_holdout_or_use_purged_control_as_training(self):
        result = evaluate([observation(hours=239.5)], [control(hours=240)])
        self.assertEqual(result["summary"]["counts"]["selected_pairs"], 0)
        result = evaluate([observation(hours=214.5)], [control(hours=215)])
        self.assertEqual(metrics(result)["selected_pairs"], 1)
        self.assertEqual(metrics(result)["complete_price_pairs"], 0)

    def test_sample_ready_requires_train_holdout_and_controls_but_never_claims_edge(self):
        signals = [observation("s" + str(i), hours=h) for i, h in enumerate((0, 100, 240, 340))]
        controls = [control("c" + str(i), hours=h) for i, h in enumerate((0, 100, 240, 340))]
        result = evaluate(signals, controls)
        horizon = result["strata"][0]["horizons"]["24h"]
        self.assertTrue(horizon["sample_ready"])
        self.assertFalse(horizon["edge_claim"])
        self.assertFalse(result["summary"]["edge_claim"])
        self.assertFalse(evaluate(signals)["strata"][0]["horizons"]["24h"]["sample_ready"])
        self.assertFalse(evaluate(signals, controls, opts=options(min_sample=30))["strata"][0]["horizons"]["24h"]["sample_ready"])

    def test_400_captures_match_once_and_summary_scales_without_fetching(self):
        signals = [observation("s" + str(i), hours=0 if i < 100 else 240) for i in range(200)]
        controls = [control("c" + str(i), hours=0 if i < 100 else 240) for i in range(200)]
        result = evaluate(signals, controls, opts=options(min_sample=30))
        self.assertEqual(len(result["control_matches"]), 200)
        self.assertEqual(result["summary"]["counts"]["unique_tokens"], 400)
        self.assertTrue(result["strata"][0]["horizons"]["24h"]["sample_ready"])
        self.assertEqual(metrics(result, "holdout")["complete_price_pairs"], 100)
        self.assertFalse(result["summary"]["edge_claim"])

    def test_insufficient_pair_completion_is_a_separate_gate(self):
        hours = (0, 72, 144, 240, 312, 384)
        signals = [observation("s" + str(i), hours=h) for i, h in enumerate(hours)]
        controls = [control("c" + str(i), hours=h) for i, h in enumerate(hours)]
        controls[2]["horizons"] = {}
        controls[5]["horizons"] = {}
        horizon = evaluate(signals, controls)["strata"][0]["horizons"]["24h"]
        self.assertFalse(horizon["sample_ready"])
        self.assertIn("train_insufficient_pair_coverage", horizon["review_blockers"])
        self.assertNotIn("train_insufficient_complete_pairs", horizon["review_blockers"])


class ConfidenceAndFeatureTests(unittest.TestCase):
    def test_net_scenarios_confidence_and_sizes_are_exposed_in_summary(self):
        signals = [observation("a", return_pct=20), observation("b", hours=72, return_pct=-10)]
        controls = [control("c"), control("d", hours=72, return_pct=5)]
        result = evaluate(signals, controls)
        base = metrics(result)["net_scenarios"]["base"]
        self.assertAlmostEqual(base["all_signal_net"]["mean_pct"], 2)
        self.assertEqual(base["paired_excess"]["n"], 2)
        self.assertIsNotNone(base["paired_excess"]["mean_ci95_pct"])
        self.assertEqual(metrics(result)["gross_signal"]["positive_pct"], 50)
        self.assertLess(metrics(result)["gross_signal"]["positive_ci95_pct"][0], 50)
        self.assertGreater(metrics(result)["gross_signal"]["positive_ci95_pct"][1], 50)
        compact = result["summary"]["strata"][0]["horizons"]["24h"]["splits"]["train"]
        self.assertEqual(compact["net_scenarios"]["base"]["paired_excess"]["n"], 2)
        self.assertEqual(base["paired_excess"], metrics(result)["net_scenarios"]["stress"]["paired_excess"])

    def test_singleton_mean_interval_is_unavailable_not_false_precision(self):
        metric = metrics(evaluate([observation()]))["gross_signal"]
        self.assertIsNone(metric["mean_ci95_pct"])
        self.assertIsNotNone(metric["positive_ci95_pct"])

    def test_only_capture_features_are_used_never_wallet_scores(self):
        rows = []
        for i in range(3):
            rows.append(observation(str(i), hours=72 * i, return_pct=10 * i, caught_score=10 * i,
                                    feature_snapshot={"at": at(72 * i), "values": {
                                        "unique_buyers": i, "wallet_score": 1000, "future_wallet_rank": 1}}))
        result = evaluate(rows)
        diagnostics = metrics(result)["feature_diagnostics"]
        self.assertAlmostEqual(diagnostics["caught_score"]["pearson_r"], 1)
        self.assertAlmostEqual(diagnostics["unique_buyers"]["pearson_r"], 1)
        self.assertNotIn("wallet_score", diagnostics)
        self.assertEqual(result["summary"]["counts"]["rejected_feature_values"], 6)
        self.assertTrue(diagnostics["unique_buyers"]["shadow_only"])
        self.assertFalse(diagnostics["unique_buyers"]["production_applied"])

    def test_missing_or_late_feature_timestamps_are_rejected(self):
        for timestamp in (None, at(1), "bad"):
            row = observation(feature_snapshot={"at": timestamp, "values": {"unique_buyers": 10}})
            result = evaluate([row])
            self.assertEqual(metrics(result)["feature_diagnostics"]["unique_buyers"]["n"], 0)
            self.assertEqual(result["episode_diagnostics"][0]["rejected_features"], 1)

    def test_late_or_invalid_capture_cannot_supply_features_or_matches(self):
        for timestamp in ("bad", at(1)):
            row = observation(captured_at=timestamp, feature_snapshot={"at": at(), "values": {"unique_buyers": 10}})
            result = evaluate([row], [control()])
            self.assertEqual(result["summary"]["counts"]["selected_pairs"], 0)
            self.assertEqual(metrics(result)["feature_diagnostics"]["caught_score"]["n"], 0)
            signal = next(row for row in result["episode_diagnostics"] if row["cohort"] == "signal")
            self.assertEqual(signal["rejected_features"], 2)

    def test_large_finite_feature_values_do_not_overflow_correlation(self):
        rows = [observation(str(i), hours=72 * i, return_pct=10 * i, caught_score=1e300 * (i + 1)) for i in range(3)]
        result = evaluate(rows)
        self.assertAlmostEqual(metrics(result)["feature_diagnostics"]["caught_score"]["pearson_r"], 1)
        json.dumps(result, allow_nan=False)

    def test_options_are_normalized_and_frozen_against_mutable_sequences(self):
        horizons = ["24h"]
        costs = [CostScenario("custom", "100", "200")]
        opts = options(horizons=horizons, age_ratio="1.5", cost_scenarios=costs)
        horizons.append("72h")
        costs.clear()
        self.assertEqual(opts.horizons, ("24h",))
        self.assertEqual(metrics(evaluate([observation()], [control()], opts=opts))["net_scenarios"]["custom"]["cost_pct"], 3)

    def test_constant_feature_correlation_not_manufactured(self):
        rows = [observation(str(i), hours=72 * i, return_pct=i * 10) for i in range(3)]
        self.assertIsNone(metrics(evaluate(rows))["feature_diagnostics"]["caught_score"]["pearson_r"])

    def test_invalid_options_fail_with_clear_errors(self):
        invalid = (
            {"min_sample": 1}, {"min_sample": True}, {"horizons": ("2h",)}, {"horizons": ("24h", "24h")},
            {"max_delay_seconds": -1}, {"max_capture_gap_seconds": float("inf")}, {"age_ratio": 0.9},
            {"holdout_fraction": 1}, {"holdout_fraction": float("nan")}, {"min_pair_coverage": 1.1},
            {"bootstrap_samples": 1}, {"holdout_start": "2026-01-01"}, {"cost_scenarios": ()},
            {"cost_scenarios": (CostScenario("same"), CostScenario("same"))},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                options(**changes)
        for fees in (-1, True, float("nan"), 10001):
            with self.subTest(fees=fees), self.assertRaises(ValueError):
                CostScenario("bad", fees, 0)


class CliTests(unittest.TestCase):
    def test_documented_prospective_json_example_matches_contract(self):
        document = (ROOT / "SIGNAL_EVALUATION.md").read_text(encoding="utf-8")
        sample = document.split("```json\n", 1)[1].split("```", 1)[0]
        result = evaluate_signals(json.loads(sample), options=options(holdout_start=None))
        self.assertEqual(result["summary"]["counts"]["primary_signals"], 1)
        self.assertEqual(result["summary"]["counts"]["selected_pairs"], 1)
        self.assertEqual(result["summary"]["horizons"]["24h"]["control_outcome_status_counts"], {"missing": 1})
        self.assertEqual(metrics(result)["gross_signal"]["n"], 1)
        self.assertFalse(result["strata"][0]["horizons"]["24h"]["sample_ready"])

    def run_cli(self, payload, *args):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload, encoding="utf-8")
            return subprocess.run([sys.executable, str(ROOT / "tools" / "evaluate_signals.py"), str(path), *args],
                                  capture_output=True, text=True, check=False, cwd=directory)

    def test_cli_runs_from_other_directory_with_configured_estimated_costs(self):
        process = self.run_cli({"generated_at": at(720), "episodes": [observation()]},
                               "--horizons", "24h", "--summary-only", "--bootstrap-samples", "100",
                               "--cost-scenario", "custom:50:150")
        self.assertEqual(process.returncode, 0, process.stderr)
        summary = json.loads(process.stdout)
        self.assertTrue(summary["offline"])
        self.assertEqual(summary["policy"]["cost_scenarios"][0]["roundtrip_fees_bps"], 50)
        self.assertEqual(summary["counts"]["primary_signals"], 1)

    def test_invalid_json_policy_and_cost_arguments_have_no_traceback(self):
        for payload, args in (("{", ()), ({"generated_at": at(720)}, ("--min-sample", "1")),
                              ({}, ("--cost-scenario", "bad")), ({}, ())):
            process = self.run_cli(payload, *args)
            with self.subTest(payload=payload, args=args):
                self.assertEqual(process.returncode, 2)
                self.assertIn("error:", process.stderr)
                self.assertNotIn("Traceback", process.stderr)


if __name__ == "__main__":
    unittest.main()
