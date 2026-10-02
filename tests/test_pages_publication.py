import unittest
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from tools.build_pages import choose_snapshot, publish_token_details


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
