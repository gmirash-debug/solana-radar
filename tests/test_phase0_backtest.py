import unittest

from tools.phase0_backtest import build_captures, evaluate, first_touch, summarize

T0 = 1_791_000_000


def candle(offset_minutes, low, high, close=None):
    return {"time": (T0 + offset_minutes * 60) * 1000, "open": close or low, "close": close or high,
            "low": low, "high": high}


class FirstTouchTests(unittest.TestCase):
    def test_take_profit_before_stop(self):
        path = [candle(5, 0.95, 1.2), candle(10, 1.0, 1.55), candle(15, 0.5, 0.6)]
        self.assertEqual(first_touch(path, 1.0, 0.5, -0.3), "win")

    def test_stop_before_take_profit(self):
        path = [candle(5, 0.69, 1.1), candle(10, 1.0, 2.5)]
        self.assertEqual(first_touch(path, 1.0, 0.5, -0.3), "loss")

    def test_same_candle_is_resolved_by_minutes_or_counted_as_loss(self):
        path = [candle(5, 0.6, 1.6)]
        self.assertEqual(first_touch(path, 1.0, 0.5, -0.3), "loss_ambiguous")
        minutes = [candle(5, 0.9, 1.6), candle(6, 0.6, 0.9)]
        self.assertEqual(first_touch(path, 1.0, 0.5, -0.3, refine=lambda start: minutes), "win")
        self.assertEqual(first_touch(path, 1.0, 0.5, -0.3, refine=lambda start: [candle(5, 0.6, 1.6)]),
                         "loss_ambiguous")

    def test_neither_threshold_is_open(self):
        self.assertEqual(first_touch([candle(5, 0.8, 1.3)], 1.0, 0.5, -0.3), "open")


class EvaluateTests(unittest.TestCase):
    capture = {"token": "t", "caught_at": T0 + 120, "caught_price": 1.0}

    def test_entry_is_last_candle_starting_before_capture_and_path_excludes_it(self):
        data = {"complete": True, "rows": [
            candle(-5, 0.2, 9.0, close=0.9),   # pre-capture spike must not count
            candle(0, 0.95, 1.05, close=1.0),  # candle containing the capture: entry
            candle(5, 0.98, 1.6, close=1.5),
            candle(10, 0.4, 1.5, close=0.5),
        ]}
        result = evaluate(self.capture, data, now=T0 + 2 * 86400, refine=False)
        self.assertEqual(result["entry"], 1.0)
        self.assertEqual(result["tp50_sl30"], "win")
        self.assertEqual(result["tp100_sl30"], "loss")
        self.assertAlmostEqual(result["mfe"], 0.6)
        self.assertAlmostEqual(result["mae"], -0.6)
        self.assertAlmostEqual(result["ret_24h"], -0.5)
        self.assertAlmostEqual(result["bracket_net"], 0.48)

    def test_rug_stop_fills_at_the_close_of_the_crossing_candle(self):
        data = {"rows": [candle(0, 0.95, 1.05, close=1.0), candle(5, 0.1, 1.1, close=0.2), candle(10, 0.1, 0.3)]}
        result = evaluate(self.capture, data, now=T0 + 2 * 86400, refine=False)
        self.assertEqual(result["tp50_sl30"], "loss")
        self.assertAlmostEqual(result["bracket_net"], -0.32)
        self.assertAlmostEqual(result["bracket_close_fill_net"], -0.82)
        self.assertTrue(result["crash80"])

    def test_immature_and_missing_reference_are_not_scored(self):
        self.assertEqual(evaluate(self.capture, {"rows": []}, now=T0 + 3600)["status"], "immature")
        later_only = {"rows": [candle(30, 0.9, 1.1)]}
        self.assertEqual(evaluate(self.capture, later_only, now=T0 + 2 * 86400)["status"], "no_reference_candle")

    def test_summary_counts_ambiguous_as_loss(self):
        rows = [{"outcome": {"tp50_sl30": outcome, "tp100_sl30": "open", "mfe": 0, "mae": 0, "ret_24h": 0,
                             "bracket_net": 0}} for outcome in ("win", "loss_ambiguous", "open", "loss")]
        stats = summarize(rows)
        self.assertEqual(stats["n"], 4)
        self.assertEqual(stats["win50"], 0.25)
        self.assertEqual(stats["loss30"], 0.5)


class CaptureTests(unittest.TestCase):
    def test_captures_join_market_and_thesis_risk_layers(self):
        dashboard = {
            "market": {
                "tok": {"caught_obs_price_usd": 0.001, "caught_obs_mcap_at": "2026-10-08T00:00:00Z",
                        "pair_created_at": 1_791_000_000 - 3600 * 30, "symbol": "TOK"},
                "uncaught": {"latest_price_usd": 1},
            },
            "report": {"signal_theses": [{
                "token_address": "tok", "signal_family": "classified_wallets",
                "supply_integrity": {"status": "unverified"},
                "coordinated_activity": {"signals": [{"code": "young_low_activity"}, {"code": "synchronous_buys"}]},
            }]},
        }
        [capture] = build_captures(dashboard)
        self.assertEqual(capture["family"], "classified_wallets")
        self.assertEqual(capture["supply"], "unverified")
        self.assertEqual(capture["flags"], ["synchronous_buys", "young_low_activity"])
        self.assertIsNotNone(capture["age_hours"])


if __name__ == "__main__":
    unittest.main()
