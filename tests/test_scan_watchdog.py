import unittest
from datetime import datetime, timezone
from tools.scan_watchdog import watchdog_decision


class WatchdogTests(unittest.TestCase):
    now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)

    def test_fresh_snapshot_prevents_duplicate(self):
        snapshot = {"report": {"generated_at": "2026-10-03T11:30:00Z", "lane": "reactivation"}}
        self.assertFalse(watchdog_decision([snapshot], [], self.now)[0])

    def test_queued_scan_prevents_dispatch(self):
        self.assertFalse(watchdog_decision([], [{"event": "workflow_dispatch", "status": "queued"}], self.now)[0])

    def test_unknown_github_status_fails_closed(self):
        self.assertFalse(watchdog_decision([], None, self.now)[0])

    def test_old_or_unavailable_primary_dispatches(self):
        self.assertTrue(watchdog_decision([], [], self.now)[0])

    def test_failed_attempts_have_bounded_cooldown(self):
        run = {"event": "workflow_dispatch", "status": "completed", "created_at": "2026-10-03T11:45:00Z"}
        self.assertFalse(watchdog_decision([], [run], self.now)[0])

    def test_newest_static_fallback_is_used_and_future_times_do_not_suppress(self):
        snapshots = [{"report": {"generated_at": "2026-10-03T11:40:00Z"}},
                     {"report": {"generated_at": "2026-10-04T12:00:00Z"}}]
        self.assertFalse(watchdog_decision(snapshots, [], self.now)[0])


if __name__ == "__main__":
    unittest.main()
