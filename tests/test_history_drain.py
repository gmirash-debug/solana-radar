import unittest
from unittest.mock import Mock

from tools.flush_history_queue import drain


def response(body, status=200):
    return Mock(ok=status < 300, status_code=status, json=Mock(return_value=body))


class HistoryDrainTests(unittest.TestCase):
    def test_progressing_legacy_migration_continues_only_within_request_limit(self):
        session = Mock()
        session.post.return_value = response({"ok": True, "migration_pending": True, "migration_progressed": 25})
        result = drain("https://worker.invalid", "secret", session=session, clock=lambda: 100)
        self.assertEqual(session.post.call_count, 10)
        self.assertEqual(result["status"], "legacy_migration_pending")
    def test_hard_request_bound_and_no_redirect_secret_leak(self):
        session = Mock()
        session.post.return_value = response({"ok": True, "pending": 50, "delivered": 1})
        result = drain("https://worker.invalid", "secret", session=session, clock=lambda: 100)
        self.assertEqual(session.post.call_count, 10)
        self.assertEqual(result["delivered"], 10)
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])

    def test_empty_and_blocked_queue_do_not_become_scan_failure(self):
        for body, status, expected in (({"ok": True, "pending": 0}, 200, "empty"),
                                       ({"ok": False, "pending": 9, "error": "r2_budget_unavailable"}, 503, "queue_deferred"),
                                       ({"ok": True, "migration_pending": True}, 200, "legacy_migration_pending")):
            session = Mock()
            session.post.return_value = response(body, status)
            self.assertEqual(drain("https://worker.invalid", "secret", session=session)["status"], expected)
            self.assertEqual(session.post.call_count, 1)

    def test_no_unbounded_work_or_authenticated_redirect(self):
        for url in ("http://worker.invalid", "https://user:secret@worker.invalid", "https://worker.invalid?token=x"):
            with self.assertRaises(ValueError):
                drain(url, "secret")
        for maximum in (0, 11):
            with self.assertRaises(ValueError):
                drain("https://worker.invalid", "secret", maximum=maximum)
        session = Mock()
        session.post.return_value = response({"ok": False}, 401)
        with self.assertRaises(RuntimeError):
            drain("https://worker.invalid", "secret", session=session)


if __name__ == "__main__":
    unittest.main()
