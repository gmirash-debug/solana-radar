import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from tools.scan_watchdog import main, parse_time, watchdog_decision


class WatchdogTests(unittest.TestCase):
    now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)

    def test_workflow_run_name_preserves_exact_source_marker(self):
        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/scan-and-pages.yml"
        expected = (
            'run-name: "Scan and deploy dashboard [source=${{ '
            "github.event_name == 'workflow_dispatch' && (inputs.source || 'manual') "
            '|| github.event_name }}]"'
        )
        self.assertIn(expected, workflow.read_text(encoding="utf-8").splitlines())

    def scan_run(self, source="cloudflare-deep_scan", status="completed", created_at="2026-10-03T11:45:00Z"):
        return {"event": "workflow_dispatch", "status": status, "created_at": created_at,
                "display_title": f"Scan and deploy dashboard [source={source}]"}

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
        run = {**self.scan_run(), "conclusion": "failure"}
        self.assertFalse(watchdog_decision([], [run], self.now)[0])

    def test_newest_static_fallback_is_used_and_future_times_do_not_suppress(self):
        snapshots = [{"report": {"generated_at": "2026-10-03T11:40:00Z"}},
                     {"report": {"generated_at": "2026-10-04T12:00:00Z"}}]
        self.assertFalse(watchdog_decision(snapshots, [], self.now)[0])

    def test_completed_targeted_runs_do_not_renew_deep_cooldown(self):
        runs = [self.scan_run("cloudflare-targeted", created_at=f"2026-10-03T11:{minute}:00Z")
                for minute in ("07", "22", "37", "52")]
        snapshot = {"report": {"generated_at": "2026-10-03T11:52:00Z", "scan_profile": "targeted",
                                "last_deep_scan_at": "2026-10-03T10:00:00Z"},
                    "scan_status": {"scan_profile": "targeted", "last_attempt_at": "2026-10-03T11:52:00Z"}}
        self.assertEqual(watchdog_decision([snapshot], runs, self.now),
                         (True, "deep_scan_stale_or_unreachable"))

    def test_targeted_run_after_deep_cooldown_expires_does_not_reset_it(self):
        runs = [self.scan_run(created_at="2026-10-03T11:10:00Z"),
                self.scan_run("cloudflare-targeted", created_at="2026-10-03T11:59:00Z")]
        self.assertTrue(watchdog_decision([], runs, self.now)[0])

    def test_any_active_dispatch_blocks_serialized_scan_even_without_type(self):
        for source in ("cloudflare-targeted", "manual-targeted", "unknown", None):
            for status in ("queued", "in_progress", "pending", "waiting", "requested"):
                with self.subTest(source=source, status=status):
                    run = self.scan_run(source, status)
                    if source is None:
                        run.pop("display_title")
                    self.assertEqual(watchdog_decision([], [run], self.now),
                                     (False, "scan_already_queued_or_running"))

    def test_only_exact_known_deep_source_markers_apply_cooldown(self):
        for source in ("manual", "manual-http", "cloudflare-deep_scan", "cloudflare-watchdog"):
            with self.subTest(source=source):
                self.assertEqual(watchdog_decision([], [self.scan_run(source)], self.now),
                                 (False, "dispatch_cooldown"))
        for source in ("cloudflare-targeted", "manual-targeted", "robinhood", "push", "made-up-deep"):
            with self.subTest(source=source):
                self.assertTrue(watchdog_decision([], [self.scan_run(source)], self.now)[0])

    def test_legacy_generic_and_malformed_titles_cannot_claim_a_deep_attempt(self):
        for title in (None, "Scan and deploy dashboard", "cloudflare-deep_scan", 123,
                      "prefix Scan and deploy dashboard [source=manual]",
                      "Scan and deploy dashboard [source=manual] suffix",
                      "Scan and deploy dashboard [source=manual]\n"):
            with self.subTest(title=title):
                run = {**self.scan_run(), "display_title": title, "name": "Scan and deploy dashboard [source=manual]"}
                self.assertTrue(watchdog_decision([], [run], self.now)[0])

    def test_explicit_deep_status_supplies_a_cooldown_for_legacy_runs(self):
        for profile in ("deep", "deep_scan"):
            with self.subTest(profile=profile):
                snapshot = {"scan_status": {"status": "failed", "scan_profile": profile,
                                             "last_attempt_at": "2026-10-03T11:45:00Z"}}
                self.assertEqual(watchdog_decision([snapshot], [], self.now), (False, "dispatch_cooldown"))

    def test_untyped_or_non_deep_status_does_not_extend_cooldown(self):
        for profile in (None, "targeted", "discovery", "robinhood", "DEEP", [], {}):
            with self.subTest(profile=profile):
                snapshot = {"scan_status": {"scan_profile": profile, "last_attempt_at": "2026-10-03T11:59:00Z"}}
                self.assertTrue(watchdog_decision([snapshot], [], self.now)[0])

    def test_future_invalid_and_naive_attempt_times_do_not_suppress(self):
        for value in ("2026-10-03T12:01:00Z", "2026-10-03T11:59:00", "invalid", None):
            with self.subTest(value=value):
                snapshot = {"scan_status": {"scan_profile": "deep", "last_attempt_at": value}}
                self.assertTrue(watchdog_decision([snapshot], [self.scan_run(created_at=value)], self.now)[0])
        self.assertIsNone(parse_time("2026-10-03T11:59:00"))
        self.assertEqual(parse_time("2026-10-03T13:59:00+02:00"),
                         datetime(2026, 10, 3, 11, 59, tzinfo=timezone.utc))

    def test_cooldown_expires_at_exactly_fifty_minutes(self):
        self.assertFalse(watchdog_decision([], [self.scan_run(created_at="2026-10-03T11:10:01Z")], self.now)[0])
        self.assertTrue(watchdog_decision([], [self.scan_run(created_at="2026-10-03T11:10:00Z")], self.now)[0])

    def test_push_runs_do_not_apply_dispatch_cooldown(self):
        run = {**self.scan_run(), "event": "push", "status": "in_progress"}
        self.assertTrue(watchdog_decision([], [run], self.now)[0])

    def test_malformed_run_list_fails_closed_and_malformed_snapshots_are_ignored(self):
        self.assertEqual(watchdog_decision([], [None], self.now), (False, "github_status_unavailable"))
        self.assertTrue(watchdog_decision([None, {"report": []}, {"scan_status": []}], [], self.now)[0])

    def test_main_dispatches_after_only_targeted_runs_without_live_calls(self):
        run = self.scan_run("cloudflare-targeted", created_at="2026-10-03T11:59:00Z")
        response = MagicMock()
        response.__enter__.return_value.status = 204
        with patch.dict("os.environ", {"GH_TOKEN": "test-token", "GITHUB_REPOSITORY": "test/repo"}), \
                patch("tools.scan_watchdog.fetch_json", side_effect=[{}, {}, {"workflow_runs": [run]}]) as fetched, \
                patch("tools.scan_watchdog.urlopen", return_value=response) as opened, \
                patch("tools.scan_watchdog.print"):
            main()
        self.assertEqual(fetched.call_count, 3)
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://api.github.com/repos/test/repo/actions/workflows/scan-and-pages.yml/dispatches")
        self.assertEqual(json.loads(request.data)["inputs"]["source"], "cloudflare-watchdog")

    def test_main_never_dispatches_over_active_targeted_run_without_live_calls(self):
        run = self.scan_run("cloudflare-targeted", status="in_progress")
        with patch.dict("os.environ", {"GH_TOKEN": "test-token"}), \
                patch("tools.scan_watchdog.fetch_json", side_effect=[{}, {}, {"workflow_runs": [run]}]), \
                patch("tools.scan_watchdog.urlopen") as opened, patch("tools.scan_watchdog.print"):
            main()
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
