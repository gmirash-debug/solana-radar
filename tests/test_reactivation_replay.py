import copy
import json
import unittest

import scanner
from tools.replay_reactivation import DEFAULT_FIXTURES, replay, run_case


class ReactivationReplayTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = json.loads(DEFAULT_FIXTURES.read_text())
        self.config = scanner.load_json(scanner.DEFAULT_CONFIG_PATH, {})

    def test_offline_envelopes_cover_positive_and_negative_guards(self):
        result = replay(self.fixtures)
        self.assertEqual(result["summary"]["passed"], result["summary"]["cases"])
        self.assertTrue(result["offline"])
        self.assertEqual(result["metrics_scope"], "synthetic_contract_cases_not_empirical_strategy_accuracy")
        self.assertEqual({case["actual_tier"] for case in result["cases"]},
                         {"watch", "hot_reactivation", "late_chase"})

    def test_hot_case_requires_both_baseline_and_seasoned_retention(self):
        positive = next(case for case in self.fixtures["cases"]
                        if case["id"] == "seasoned_low_cap_distributed_reactivation")
        self.assertEqual(run_case(positive, self.config)["actual_tier"], "hot_reactivation")
        for guard in ("baseline", "hold"):
            case = copy.deepcopy(positive)
            if guard == "baseline":
                case["alert"].pop("reactivation_baseline")
            else:
                case["alert"]["wave"]["hold_age_minutes"] = 0
            with self.subTest(guard=guard):
                self.assertNotEqual(run_case(case, self.config)["actual_tier"], "hot_reactivation")


if __name__ == "__main__":
    unittest.main()
