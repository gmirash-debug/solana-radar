import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_pages_publication as pages_tests
from tools.build_pages import preserve_published_details, publish_robinhood, publish_token_details


class AuditPublicationRegressions(unittest.TestCase):
    def test_ui_only_robinhood_publication_quarantines_legacy_ordinary_cohort_without_mutation(self):
        selected = {"chain_id": 4663, "attempted_at": "2026-09-04T22:00:00Z", "tokens": [{
            "token": "legacy-token", "cohort_reason": "Distributed net buy wave", "status": "confirmed",
            "attribution_complete": True, "retained_supply_lower_bound_pct": 5,
            "retained_supply_upper_bound_pct": 8,
            "wallets": [{"address": "legacy-wallet", "bought_raw": 100,
                "retained_lower_bound_raw": 90, "retained_upper_bound_raw": 100}],
            "position_flow": {"status": "confirmed"}, "preparation": {"status": "confirmed"},
            "coordinated_activity": {"status": "confirmed"},
        }]}
        original = copy.deepcopy(selected)
        response = mock.Mock()
        response.json.return_value = selected
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            local = dict(selected, attempted_at="2026-09-03T22:00:00Z")
            local_path = root / "data/robinhood.json"
            local_path.write_text(json.dumps(local))
            original_local_bytes = local_path.read_bytes()
            (root / ".pages/data").mkdir(parents=True)
            cwd = os.getcwd()
            try:
                os.chdir(root)
                with mock.patch("tools.build_pages.requests.get", return_value=response), \
                        mock.patch("robinhood.Rpc.call", side_effect=AssertionError("publication must not call RPC")):
                    publish_robinhood()
            finally:
                os.chdir(cwd)
            published = json.loads((root / ".pages/data/robinhood.json").read_text())["tokens"][0]
            self.assertEqual(local_path.read_bytes(), original_local_bytes)
        self.assertEqual(selected, original)
        self.assertEqual(published["cohort_attribution_status"], "legacy_unverified")
        self.assertEqual(published["status"], "needs_data")
        self.assertFalse(published["attribution_complete"])
        self.assertIsNone(published["retained_supply_lower_bound_pct"])
        self.assertIsNone(published["wallets"][0]["retained_lower_bound_raw"])
        self.assertEqual(published["legacy_ordinary_attribution"]["status"], "confirmed")
        self.assertEqual(published["legacy_ordinary_attribution"]["wallets"][0]["bought_raw"], 100)

    def test_i10_detail_urls_are_immutable_across_generations(self):
        snapshot, state = pages_tests.PagesPublicationTests().fixture()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            first = publish_token_details(snapshot, state, destination)
            first_path = destination / Path(first["files"]["mint/unsafe"]).name
            original = first_path.read_bytes()
            snapshot["report"]["generated_at"] = "2026-09-04T22:00:00Z"
            second = publish_token_details(snapshot, state, destination)
            self.assertNotEqual(first["files"], second["files"])
            self.assertEqual(first_path.read_bytes(), original)
            self.assertEqual(len(list(destination.glob("*.json"))), 2)

    def test_i10_partial_publication_omits_replaced_or_malformed_detail(self):
        for mutation in ("generation", "cohort", "window", "json", "shape"):
            snapshot, detail, _ = pages_tests.PagesPublicationTests().published_fixture()
            if mutation == "generation":
                detail["report_source_updated_at"] = "new generation"
            elif mutation == "cohort":
                detail["thesis"]["cohort_id"] = "replacement"
            elif mutation == "window":
                detail["thesis"]["signal_window_end"] = "different"
            elif mutation == "shape":
                detail = []
            response = mock.Mock()
            response.json.side_effect = ValueError("Invalid JSON") if mutation == "json" else None
            response.json.return_value = detail
            with tempfile.TemporaryDirectory() as directory:
                manifest = preserve_published_details(snapshot,
                    {"generation": snapshot["report"]["generated_at"], "files": {}},
                    Path(directory), fetcher=mock.Mock(return_value=response), allow_partial=True)
                self.assertEqual(manifest["files"], {})
                self.assertEqual(manifest["preservation_status"], "partial_detail_mismatch")
                self.assertEqual(list(Path(directory).glob("*.json")), [])

    def test_i10_partial_mode_still_rejects_unsafe_paths_without_fetching(self):
        snapshot, _, _ = pages_tests.PagesPublicationTests().published_fixture()
        snapshot["token_details"]["files"]["mint/unsafe"] = "../../private.json"
        fetcher = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                preserve_published_details(snapshot,
                    {"generation": snapshot["report"]["generated_at"], "files": {}},
                    Path(directory), fetcher=fetcher, allow_partial=True)
        fetcher.assert_not_called()

    def test_i04_i10_static_details_check_frozen_window_boundaries(self):
        for field in ("signal_window_start", "signal_window_end"):
            snapshot, state = pages_tests.PagesPublicationTests().fixture()
            snapshot["report"]["signal_theses"][0][field] = "new-window"
            state["pools"]["pool"]["signal_thesis"][field] = "old-window"
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(publish_token_details(snapshot, state, Path(directory))["files"], {})


if __name__ == "__main__":
    unittest.main()
