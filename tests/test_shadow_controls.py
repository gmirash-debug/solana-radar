"""Shadow control group: reproduce why control outcomes went missing/late, and the repair."""
import copy
import os
import unittest
from unittest.mock import patch

import scanner as s
from prospective_evidence import KLINE_MAX_ATTEMPTS, capture_evaluation_rows, kline_checkpoint
from runtime_dashboard import dashboard_documents, list_report
from signal_evaluation import EvaluationOptions, evaluate_signals

T0 = s.parse_timestamp("2026-10-03T00:00:00Z")
HOUR = 3600


def iso(offset):
    return s.iso(T0 + offset)


def pool(token, *, price=1.0, mcap=100000.0, liquidity=20000.0, at=0, created=T0 - 48 * HOUR):
    return s.Pool(pool_address="pool-" + token, token_address=token, source="dexscreener", price_usd=price,
                  mcap_usd=mcap, liquidity_usd=liquidity, pair_created_at=created, market_snapshot_at=T0 + at)


def scanned(token):
    return {"pool": {"token_address": token}, "detector_executed": True, "wallet_classification_required": False,
            "trade_fetch": {"source": "enhanced_transactions", "passes": [{"coverage_complete": True}]}}


class FakeKline:
    """GMGN-shaped 5m candles (ms start times) for the requested window."""

    def __init__(self, prices=None, fail=False):
        self.prices = prices or {}
        self.fail = fail
        self.calls = []

    def __call__(self, token, resolution, start, end):
        self.calls.append((token, resolution, start, end))
        if self.fail:
            raise RuntimeError("gmgn kline: 429 RATE_LIMIT")
        price = self.prices.get(token)
        if price is None:
            return []
        first = int(start // 300 + 1) * 300
        return [{"time": t * 1000, "open": price(t), "close": price(t + 300), "high": 0, "low": 0}
                for t in range(first, int(end) + 1, 300)]


class Scanner:
    """The deep-scan order: record universe snapshots, then freeze evaluation rows."""

    def __init__(self):
        self.state = {}

    def run(self, offset, universe=(), alerts=(), summaries=None, **kwargs):
        at = iso(offset)
        universe = list(universe)
        s.record_market_observations(self.state, universe, at)
        return capture_evaluation_rows(
            self.state, universe, [scanned(p.token_address) for p in universe] if summaries is None else summaries,
            list(alerts), at, "v1", s.outcome_market_snapshot, **kwargs)

    def outcome(self, offset, token, horizon):
        result = evaluate_signals({"generated_at": iso(offset), "signal_evaluation_dataset": self.state["signal_evaluation_dataset"]},
                                  options=EvaluationOptions(horizons=(horizon,), bootstrap_samples=100))
        return next(row["outcomes"][horizon] for row in result["episode_diagnostics"] if row["token_address"] == token)


def alert(token):
    return {"pool": {"token_address": token}, "score": 60, "signal_family": "reactivation_wave"}


class ShadowControlCauseTests(unittest.TestCase):
    def test_control_leaving_attention_lists_had_no_refresh_and_went_missing(self):
        radar = Scanner()
        radar.run(0, [pool("control")])
        # Attention-mode registry refresh keeps only current GMGN candidates, and the
        # caught-market refresh covers alerts only: the control's market entry freezes.
        radar.run(65 * 60)
        radar.run(130 * 60)
        self.assertEqual(radar.state["market"]["control"]["latest_seen_at"], iso(0))
        self.assertEqual(radar.outcome(130 * 60, "control", "1h")["status"], "missing")

        repaired = Scanner()
        repaired.run(0, [pool("control")])
        kline = FakeKline({"control": lambda t: 1.5})
        repaired.run(65 * 60, kline=kline, kline_max_calls=10)
        self.assertEqual(kline.calls, [], "an on-time snapshot may still arrive before the window closes")
        repaired.run(130 * 60, kline=kline, kline_max_calls=10)
        self.assertEqual(kline.calls, [("control", "5m", T0 + HOUR - 300, T0 + 2 * HOUR)])
        checkpoint = repaired.state["signal_evaluation_dataset"]["controls"]["control"]["horizons"]["1h"]
        self.assertEqual((checkpoint["source"], checkpoint["at"]), ("gmgn_kline", iso(HOUR + 300)))
        outcome = repaired.outcome(130 * 60, "control", "1h")
        self.assertEqual((outcome["status"], outcome["return_basis"]), ("eligible", "price"))
        self.assertAlmostEqual(outcome["return_pct"], 50)

    def test_reappearing_control_froze_one_late_snapshot_for_every_due_horizon(self):
        radar = Scanner()
        radar.run(0, [pool("control")])
        radar.run(8 * HOUR, [pool("control", price=3, at=8 * HOUR)])
        horizons = radar.state["signal_evaluation_dataset"]["controls"]["control"]["horizons"]
        self.assertEqual({h: c["at"] for h, c in horizons.items()}, {"1h": iso(8 * HOUR), "6h": iso(8 * HOUR)})
        self.assertEqual({h: c["quality_status"] for h, c in horizons.items()}, {"1h": "delayed", "6h": "delayed"})
        self.assertEqual(radar.outcome(8 * HOUR, "control", "1h")["status"], "late")
        self.assertEqual(radar.outcome(8 * HOUR, "control", "6h")["status"], "late")

        repaired = Scanner()
        repaired.run(0, [pool("control")])
        kline = FakeKline({"control": lambda t: 1 + (t - T0) / HOUR})
        repaired.run(8 * HOUR, [pool("control", price=3, at=8 * HOUR)], kline=kline, kline_max_calls=10)
        horizons = repaired.state["signal_evaluation_dataset"]["controls"]["control"]["horizons"]
        self.assertEqual({h: c["source"] for h, c in horizons.items()}, {"1h": "gmgn_kline", "6h": "gmgn_kline"})
        for horizon, expected in (("1h", 2 + 300 / HOUR), ("6h", 7 + 300 / HOUR)):
            outcome = repaired.outcome(8 * HOUR, "control", horizon)
            self.assertEqual(outcome["status"], "eligible")
            self.assertAlmostEqual(outcome["return_pct"], (expected - 1) * 100)

    def test_hourly_cadence_gap_made_refreshed_tokens_late_in_both_cohorts(self):
        for cohort, alerts in (("episodes", [alert("token")]), ("controls", [])):
            with self.subTest(cohort=cohort):
                radar = Scanner()
                radar.run(0, [pool("token")], alerts=alerts)
                radar.run(55 * 60, [pool("token", price=1.1, at=55 * 60)])
                radar.run(130 * 60, [pool("token", price=1.2, at=130 * 60)])
                self.assertEqual(radar.outcome(130 * 60, "token", "1h")["status"], "late")

                repaired = Scanner()
                repaired.run(0, [pool("token")], alerts=alerts)
                repaired.run(55 * 60, [pool("token", price=1.1, at=55 * 60)])
                kline = FakeKline({"token": lambda t: 1.15})
                repaired.run(130 * 60, [pool("token", price=1.2, at=130 * 60)], kline=kline, kline_max_calls=10)
                outcome = repaired.outcome(130 * 60, "token", "1h")
                self.assertEqual(outcome["status"], "eligible")
                self.assertAlmostEqual(outcome["return_pct"], 15)
                self.assertIn("token", repaired.state["signal_evaluation_dataset"][cohort])

    def test_incomplete_capture_covariates_kept_controls_out_of_matching(self):
        radar = Scanner()
        universe = [pool("no-liquidity", liquidity=0), pool("no-age", created=0), pool("no-price", price=None),
                    pool("ok-1"), pool("ok-2")]
        dataset = radar.run(0, universe, control_limit=len(universe))
        self.assertEqual(set(dataset["controls"]), {"ok-1", "ok-2"})
        self.assertEqual(dataset["collection"]["deep"]["controls_skipped_incomplete_capture"], 3)
        self.assertEqual(dataset["controls"]["ok-1"]["selection_method"], "prospective_universe_complete_capture")
        legacy = copy.deepcopy(dataset["controls"]["ok-1"])
        legacy.update(token_address="legacy", caught_liquidity_usd=0)
        legacy.pop("episode_id")
        result = evaluate_signals({"generated_at": iso(0), "signal_evaluation_dataset": {
            "episodes": {}, "controls": {**dataset["controls"], "legacy": legacy}}},
            options=EvaluationOptions(bootstrap_samples=100))
        self.assertEqual(result["summary"]["counts"]["baseline_exclusion_counts_by_cohort"],
                         {"signal": {}, "control": {"missing_matching_covariates": 1}})


class KlineFallbackTests(unittest.TestCase):
    def test_checkpoint_uses_first_on_time_trade_and_never_carries_prices(self):
        due = T0 + HOUR
        candle = lambda start, o, c: {"time": start * 1000, "open": o, "close": c}
        containing = kline_checkpoint([candle(due - 120, 1, 2), candle(due + 180, 5, 6)], due)
        self.assertEqual((containing["at"], containing["price_usd"]), (s.iso(due + 180), 2.0))
        gap = kline_checkpoint([candle(due - 900, 9, 9), candle(due + 1800, 3, 4)], due)
        self.assertEqual((gap["at"], gap["price_usd"]), (s.iso(due + 1800), 3.0))
        self.assertIsNone(kline_checkpoint([candle(due - 900, 9, 9), candle(due + HOUR + 300, 3, 4)], due))
        self.assertIsNone(kline_checkpoint([candle(due - 120, 1, 0)], due))
        self.assertIsNone(kline_checkpoint([candle(due - 120, 1, "nan"), {"time": None}], due))
        self.assertIsNone(kline_checkpoint([], due))

    def test_delayed_legacy_checkpoint_is_superseded_once_by_on_time_kline(self):
        for prices, source, status in (({"control": lambda t: 2.0}, "gmgn_kline", "eligible"),
                                       ({}, "market_snapshot", "late")):
            with self.subTest(source=source):
                radar = Scanner()
                radar.run(0, [pool("control")])
                radar.run(3 * HOUR, [pool("control", price=3, at=3 * HOUR)])
                kline = FakeKline(prices)
                radar.run(4 * HOUR, kline=kline, kline_max_calls=10)
                radar.run(5 * HOUR, kline=kline, kline_max_calls=10)
                self.assertEqual(len(kline.calls), 1)
                checkpoint = radar.state["signal_evaluation_dataset"]["controls"]["control"]["horizons"]["1h"]
                self.assertEqual(checkpoint["source"], source)
                self.assertEqual(radar.outcome(5 * HOUR, "control", "1h")["status"], status)
                if source == "gmgn_kline":
                    self.assertEqual(checkpoint["superseded"], {"at": iso(3 * HOUR), "quality_status": "delayed",
                                                                "source": "market_snapshot"})

    def test_no_on_time_trade_stays_missing_without_repeated_requests(self):
        radar = Scanner()
        radar.run(0, [pool("control")])
        kline = FakeKline()
        for offset in (3 * HOUR, 4 * HOUR):
            dataset = radar.run(offset, kline=kline, kline_max_calls=10)
        self.assertEqual(len(kline.calls), 1)
        row = dataset["controls"]["control"]
        self.assertNotIn("1h", row["horizons"])
        self.assertEqual(row["horizon_fill"]["1h"]["result"], "no_on_time_trade")
        self.assertEqual(radar.outcome(4 * HOUR, "control", "1h")["status"], "missing")

    def test_errors_stop_the_run_then_fall_back_after_bounded_attempts(self):
        radar = Scanner()
        radar.run(0, [pool("control"), pool("other")], control_limit=2)
        kline = FakeKline(fail=True)
        for attempt in range(KLINE_MAX_ATTEMPTS):
            dataset = radar.run(3 * HOUR + attempt * HOUR, kline=kline, kline_max_calls=10)
            stats = dataset["collection"]["deep"]
            self.assertEqual((stats["kline_requests"], stats["kline_errors"]), (1, 1))
            self.assertGreaterEqual(stats["kline_deferred"], 1)
        self.assertEqual(len(kline.calls), KLINE_MAX_ATTEMPTS)
        exhausted = min(dataset["controls"], key=lambda token: token)
        self.assertEqual(dataset["controls"][exhausted]["horizon_fill"]["1h"]["attempts"], KLINE_MAX_ATTEMPTS)
        reappeared = pool(exhausted, price=2, at=7 * HOUR)
        radar.run(7 * HOUR, [reappeared], kline=kline, kline_max_calls=10)
        self.assertEqual(dataset["controls"][exhausted]["horizons"]["1h"]["quality_status"], "delayed")

    def test_budget_serves_oldest_due_horizons_first(self):
        radar = Scanner()
        radar.run(0, [pool("a")])
        radar.run(20 * 60, [pool("b", at=20 * 60)])
        radar.run(40 * 60, [pool("c", at=40 * 60)])
        kline = FakeKline({token: (lambda t: 1.0) for token in "abc"})
        stats = radar.run(3 * HOUR, kline=kline, kline_max_calls=2)["collection"]["deep"]
        self.assertEqual([call[0] for call in kline.calls], ["a", "b"])
        self.assertEqual((stats["kline_checkpoints"], stats["kline_deferred"]), (2, 1))
        clock = iter([0, 10]).__next__
        stats = radar.run(3 * HOUR + 60, kline=kline, kline_max_calls=10, kline_max_seconds=5, clock=clock)
        self.assertEqual((len(kline.calls), stats["collection"]["deep"]["kline_deferred"]), (2, 1))
        radar.run(3 * HOUR + 120, kline=kline, kline_max_calls=10)
        self.assertEqual([call[0] for call in kline.calls], ["a", "b", "c"])

    def test_matched_signal_and_control_complete_a_price_pair_after_leaving_lists(self):
        radar = Scanner()
        radar.run(0, [pool("signal"), pool("control", mcap=110000, liquidity=21000)], alerts=[alert("signal")])
        kline = FakeKline({"signal": lambda t: 1.3, "control": lambda t: 1.1})
        radar.run(130 * 60, kline=kline, kline_max_calls=10)
        result = evaluate_signals({"generated_at": iso(130 * 60), "signal_evaluation_dataset": radar.state["signal_evaluation_dataset"]},
                                  options=EvaluationOptions(horizons=("1h",), bootstrap_samples=100))
        self.assertEqual(result["summary"]["counts"]["selected_pairs"], 1)
        self.assertEqual(result["summary"]["horizons"]["1h"]["complete_price_pairs"], 1)
        excess = result["strata"][0]["horizons"]["1h"]["splits"]["train"]["net_scenarios"]["base"]["paired_excess"]
        self.assertAlmostEqual(excess["mean_pct"], 20)


class PublicationTests(unittest.TestCase):
    def dataset(self):
        radar = Scanner()
        radar.run(0, [pool("signal"), pool("control-a"), pool("control-b", mcap=900000)],
                  alerts=[alert("signal")], control_limit=2)
        radar.run(130 * 60, kline=FakeKline({"signal": lambda t: 1.2, "control-a": lambda t: 0.5}), kline_max_calls=10)
        return radar.state

    def test_capture_rows_carry_frozen_capture_values_stratum_and_outcomes(self):
        state = self.dataset()
        result = evaluate_signals({"generated_at": iso(130 * 60), "signal_evaluation_dataset": state["signal_evaluation_dataset"]},
                                  options=EvaluationOptions(horizons=("1h", "6h"), bootstrap_samples=100))
        table = result["capture_rows"]
        self.assertEqual(table["columns"], ["cohort", "token_address", "captured_at", "price_usd", "mcap_usd",
                                            "liquidity_usd", "age_hours", "stratum", "status", "1h", "6h"])
        rows = {row[1]: dict(zip(table["columns"], row)) for row in table["rows"]}
        self.assertEqual(table["totals"], {"signal": 1, "control": 2})
        self.assertEqual(table["strata"], [["reactivation-prospective-v1", "v1", "reactivation_wave"]])
        control = rows["control-a"]
        self.assertEqual((control["cohort"], control["captured_at"], control["price_usd"], control["mcap_usd"],
                          control["liquidity_usd"], control["age_hours"], control["stratum"], control["status"]),
                         ("control", iso(0), 1.0, 100000.0, 20000.0, 48.0, 0, "primary"))
        self.assertEqual((control["1h"], control["6h"]), (0.5, "pending"))
        self.assertEqual(rows["control-b"]["1h"], "missing")
        self.assertEqual(rows["signal"]["1h"], 1.2)
        limited = evaluate_signals({"generated_at": iso(130 * 60), "signal_evaluation_dataset": state["signal_evaluation_dataset"]},
                                   options=EvaluationOptions(bootstrap_samples=100), capture_row_limit=1)["capture_rows"]
        self.assertEqual([row[0] for row in limited["rows"]], ["signal", "control"])
        with self.assertRaises(ValueError):
            evaluate_signals({"generated_at": iso(0), "episodes": []}, capture_row_limit=-1)

    def test_report_and_dashboard_summary_publish_control_rows(self):
        state = self.dataset()
        evaluation = s.shadow_signal_evaluation(state, {"signal_evaluation_published_rows_per_cohort": 5}, iso(130 * 60))
        self.assertEqual(evaluation["mode"], "shadow")
        self.assertEqual(evaluation["capture_rows"]["totals"]["control"], 2)
        self.assertEqual(evaluation["collection"]["deep"]["kline_checkpoints"], 2)
        listed = list_report({"signal_evaluation": evaluation})["signal_evaluation"]
        self.assertEqual(listed["capture_rows"], evaluation["capture_rows"])
        self.assertIn("collection", listed)
        ready, _ = dashboard_documents({"report": {"generated_at": iso(130 * 60), "signal_evaluation": evaluation}})
        controls = [row for row in ready["report"]["signal_evaluation"]["capture_rows"]["rows"] if row[0] == "control"]
        self.assertEqual({row[1] for row in controls}, {"control-a", "control-b"})

    def test_default_published_rows_keep_the_compact_report_small(self):
        base = {"strategy_version": "v", "config_version": "c", "signal_family": "f", "caught_age_hours": 48,
                "caught_mcap_usd": 100000, "caught_liquidity_usd": 20000, "caught_price_usd": 1}

        def horizons(index):
            return {name: {"target_at": iso(index * 60 + hours * HOUR), "at": iso(index * 60 + hours * HOUR + 30),
                           "price_usd": 1.5, "quality_status": "complete"}
                    for name, hours in (("1h", 1), ("6h", 6), ("24h", 24), ("72h", 72))}

        mint = "So1anaMint{:05d}pumpxxxxxxxxxxxxxxxxxxxxxxx"
        signals = [{**base, "token_address": mint.format(i), "caught_at": iso(i * 60), "horizons": horizons(i)}
                   for i in range(600)]
        controls = [{**base, "token_address": mint.format(10000 + i), "caught_at": iso(i * 60), "horizons": horizons(i),
                     "signal_present": False, "selection_at": iso(i * 60), "selection_method": "m"}
                    for i in range(600)]
        state = {"signal_evaluation_dataset": {"episodes": signals, "controls": controls, "collection": {}}}
        evaluation = s.shadow_signal_evaluation(state, {}, iso(100 * HOUR))
        rows = evaluation["capture_rows"]["rows"]
        self.assertEqual(len([row for row in rows if row[0] == "signal"]), 250)
        self.assertEqual(len([row for row in rows if row[0] == "control"]), 250)
        self.assertLess(len(s.json.dumps(evaluation, separators=(",", ":"))), 300 * 1024)

    def test_unmatched_signal_reasons_explain_zero_pairs(self):
        base = {"strategy_version": "v", "config_version": "c", "signal_family": "f", "caught_age_hours": 48,
                "caught_mcap_usd": 100000, "caught_liquidity_usd": 20000, "caught_price_usd": 1, "horizons": {}}
        control = {**base, "signal_present": False, "selection_at": iso(0), "selection_method": "m"}
        signals = [
            {**base, "token_address": "no-stratum", "caught_at": iso(0), "signal_family": "other"},
            {**base, "token_address": "far", "caught_at": iso(5 * HOUR)},
            {**base, "token_address": "caliper", "caught_at": iso(60), "caught_mcap_usd": 1000000},
            {**base, "token_address": "first", "caught_at": iso(0)},
            {**base, "token_address": "second", "caught_at": iso(30)},
        ]
        result = evaluate_signals({"generated_at": iso(HOUR), "episodes": signals,
                                   "controls": [{**control, "token_address": "control", "caught_at": iso(0)}]},
                                  as_of=iso(6 * HOUR),
                                  options=EvaluationOptions(bootstrap_samples=100, holdout_start=iso(1000 * HOUR)))
        counts = result["summary"]["counts"]
        self.assertEqual(counts["selected_pairs"], 1)
        self.assertEqual(counts["unmatched_signal_reasons"], {"no_control_in_stratum_split": 1,
            "no_control_within_capture_gap": 1, "controls_already_matched": 2})
        alone = evaluate_signals({"generated_at": iso(HOUR), "episodes": [signals[2]],
                                  "controls": [{**control, "token_address": "control", "caught_at": iso(0)}]},
                                 options=EvaluationOptions(bootstrap_samples=100))["summary"]["counts"]
        self.assertEqual(alone["unmatched_signal_reasons"], {"covariate_caliper": 1})
        self.assertEqual(alone["caliper_failures"], {"caught_mcap_usd": 1})

    def test_kline_fetcher_needs_gmgn_and_honours_the_circuit(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(s.evaluation_kline_fetcher({}))
        with patch.dict(os.environ, {"GMGN_API_KEY": "key"}):
            self.assertIsNone(s.evaluation_kline_fetcher({"signal_evaluation_kline_fallback_enabled": False}))
            self.assertIsNone(s.evaluation_kline_fetcher({"gmgn_enabled": False}))
            config = {}
            fetch = s.evaluation_kline_fetcher(config)
            with patch.object(s, "fetch_gmgn_kline", return_value=[{"time": 1}]) as kline:
                self.assertEqual(fetch("token", "5m", 1, 2), [{"time": 1}])
                kline.assert_called_once_with(config, "token", "5m", 1, 2)
                config["_gmgn_circuit_open"] = True
                with self.assertRaises(RuntimeError):
                    fetch("token", "5m", 1, 2)


if __name__ == "__main__":
    unittest.main()
