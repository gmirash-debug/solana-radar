import unittest
import json
import tempfile
from unittest import mock
from pathlib import Path
from datetime import datetime, timezone

from tools.build_pages import choose_snapshot, publish_token_details, preserve_published_details


class PagesPublicationTests(unittest.TestCase):
    def test_ui_publish_preserves_newer_published_scan(self):
        old = {"report": {"generated_at": "2026-09-04T19:19:00Z"}}
        new = {"report": {"generated_at": "2026-09-04T20:45:00Z"}}
        self.assertIs(choose_snapshot(old, new), new)
        self.assertIs(choose_snapshot(new, old), new)
        self.assertIs(choose_snapshot(new, None, datetime(2026, 9, 4, 21, tzinfo=timezone.utc)), new)
        with self.assertRaisesRegex(ValueError, "stale Git data"):
            choose_snapshot(old, None, datetime(2026, 9, 5, tzinfo=timezone.utc))

    def test_invalid_or_missing_timestamps_cannot_replace_valid_data(self):
        valid = {"report": {"generated_at": "2026-09-04T20:45:00Z"}}
        self.assertIs(choose_snapshot({}, valid), valid)
        self.assertIs(choose_snapshot(valid, {"report": {"generated_at": "broken"}}, datetime(2026, 9, 4, 21, tzinfo=timezone.utc)), valid)
        with self.assertRaises(ValueError):
            choose_snapshot({}, None)

    def fixture(self):
        thesis = {"token_address": "mint/unsafe", "cohort_id": "original", "signal_at": "2026-09-04T20:00:00Z",
                  "last_checked_at": "2026-09-04T20:45:00Z", "updated_at": "2026-09-04T20:45:00Z",
                  "token_retention_pct": 0}
        raw = {**thesis, "cohort": [{"owner": "wallet", "buy_sol": 2, "attributed_tokens": 100,
               "current_balance": 0, "current_retained_tokens": 0, "retention_pct": 0,
               "checked_at": thesis["last_checked_at"]}], "private_cache": "not public"}
        return {"report": {"generated_at": "2026-09-04T21:00:00Z", "signal_theses": [thesis]}}, {"pools": {"pool": {"signal_thesis": raw}}}

    def test_static_details_preserve_explicit_zero_and_original_check_time(self):
        snapshot, state = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            manifest = publish_token_details(snapshot, state, Path(directory))
            self.assertEqual(manifest["generation"], snapshot["report"]["generated_at"])
            self.assertEqual(len(manifest["files"]), 1)
            detail = json.loads(next(Path(directory).glob("*.json")).read_text())
            self.assertEqual(detail["token_key"], "mint/unsafe")
            self.assertEqual(detail["thesis"]["cohort_wallets"][0]["current_balance"], 0)
            self.assertEqual(detail["thesis"]["last_checked_at"], "2026-09-04T20:45:00Z")
            self.assertNotIn("private_cache", detail["thesis"])
            self.assertEqual(detail["current_alerts"], [])

    def test_stale_state_or_replacement_cohort_never_mixes_with_new_summary(self):
        for field in ("cohort_id", "signal_at", "last_checked_at", "updated_at"):
            snapshot, state = self.fixture()
            state["pools"]["pool"]["signal_thesis"][field] = "different"
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(publish_token_details(snapshot, state, Path(directory))["files"], {})

    def test_future_snapshot_cannot_hide_a_valid_current_scan(self):
        current = {"report": {"generated_at": "2026-10-03T01:00:00Z"}}
        future = {"report": {"generated_at": "2099-01-01T00:00:00Z"}}
        now = datetime(2026, 10, 3, 1, tzinfo=timezone.utc)
        self.assertIs(choose_snapshot(current, future, now), current)
        self.assertIs(choose_snapshot(future, current, now), current)
        with self.assertRaises(ValueError):
            choose_snapshot(future, None, now)

    def published_fixture(self):
        snapshot, _ = self.fixture()
        generation = snapshot["report"]["generated_at"]
        path = "data/token-details/" + "a" * 64 + ".json"
        snapshot["token_details"] = {"generation": generation, "files": {"mint/unsafe": path}}
        detail = {"ok": True, "token_key": "mint/unsafe", "report_source_updated_at": generation,
                  "thesis": {**snapshot["report"]["signal_theses"][0],
                             "cohort_wallets": [{"owner": "wallet", "current_balance": 0}]}}
        return snapshot, detail, path

    def test_older_runtime_cache_preserves_matching_published_wallets(self):
        snapshot, detail, path = self.published_fixture()
        fetcher = mock.Mock(return_value=mock.Mock(json=mock.Mock(return_value=detail)))
        with tempfile.TemporaryDirectory() as directory:
            manifest = publish_token_details(snapshot, {}, Path(directory))
            preserved = preserve_published_details(snapshot, manifest, Path(directory), fetcher=fetcher)
            self.assertEqual(preserved["files"]["mint/unsafe"], path)
            saved = json.loads((Path(directory) / Path(path).name).read_text())
            self.assertEqual(saved["thesis"]["cohort_wallets"][0]["current_balance"], 0)

    def test_existing_runtime_wallet_details_do_not_need_public_downloads(self):
        snapshot, detail, path = self.published_fixture()
        fetcher = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            manifest = {"generation": snapshot["report"]["generated_at"], "files": {"mint/unsafe": path}}
            preserve_published_details(snapshot, manifest, Path(directory), fetcher=fetcher)
        fetcher.assert_not_called()

    def test_unsafe_or_mismatched_published_wallets_block_publication(self):
        for mutation in ("unsafe", "generation", "cohort"):
            snapshot, detail, path = self.published_fixture()
            if mutation == "unsafe":
                snapshot["token_details"]["files"]["mint/unsafe"] = "../../private.json"
            elif mutation == "generation":
                detail["report_source_updated_at"] = "old"
            else:
                detail["thesis"]["cohort_id"] = "replacement"
            fetcher = mock.Mock(return_value=mock.Mock(json=mock.Mock(return_value=detail)))
            with tempfile.TemporaryDirectory() as directory:
                manifest = {"generation": snapshot["report"]["generated_at"], "files": {}}
                with self.assertRaises(ValueError):
                    preserve_published_details(snapshot, manifest, Path(directory), fetcher=fetcher)
                self.assertEqual(list(Path(directory).glob("*.json")), [])

    def test_detail_reuse_never_exceeds_the_run_budget(self):
        snapshot, _, _ = self.published_fixture()
        fetcher = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "budget"):
                preserve_published_details(snapshot,
                    {"generation": snapshot["report"]["generated_at"], "files": {}},
                    Path(directory), fetcher=fetcher, max_seconds=0)
        fetcher.assert_not_called()
