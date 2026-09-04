import unittest
from datetime import datetime, timezone

from tools.build_pages import choose_snapshot


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
