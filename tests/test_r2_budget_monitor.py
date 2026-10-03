from datetime import datetime, timezone

import unittest

from tools.r2_budget_monitor import API, ISSUES, monitor


def fixture():
    event = {"id": "radar-r2-budget-2026-10-paused", "kind": "paused", "notified_at": None,
             "class_a": 899999, "class_b": 1, "storage_bytes": 500}
    budget = {"ok": True, "enabled": True, "initialized": True, "notifications": [event], "usage": {}}
    calls, issues = [], []

    def call(url, **kw):
        calls.append((url, kw))
        if url.endswith("/r2-budget"):
            return budget
        if url.startswith(ISSUES + "?"):
            return issues.copy()
        if url == ISSUES:
            issue = {"html_url": "https://github.com/gmirash-debug/solana-radar/issues/88", **kw["body"]}
            issues.append(issue)
            return issue
        if url.endswith("/ack"):
            return {"ok": True}
        raise AssertionError(url)

    return budget, calls, issues, call


def check_cloud_notification_mentions_owner_and_acks_after_issue_confirmation():
    budget, calls, issues, call = fixture()
    result = monitor("bot-token", "secret", call=call)
    assert result["created"] == result["acknowledged"] == 1
    assert "@gmirash-debug" in issues[0]["body"]
    assert "ALL billable" in issues[0]["body"]
    assert "not a Cloudflare invoice" in issues[0]["body"]
    assert calls[-1][0] == API + "/api/storage/r2-budget/ack"
    assert calls[-1][1]["secret"] == "secret"
    assert all("archive" not in url for url, _ in calls)


def check_ack_retry_finds_existing_issue_without_duplicate_notification():
    budget, calls, issues, call = fixture()
    monitor("bot", "secret", call=call)
    assert monitor("bot", "secret", call=call)["created"] == 0
    assert len(issues) == 1


def check_silent_below_warning_with_no_github_reads():
    budget, calls, issues, call = fixture()
    budget["notifications"] = []
    assert monitor("bot", "secret", call=call)["created"] == 0
    assert len(calls) == 1


def check_test_notice_is_explicit_and_does_not_fake_or_ack_quota_event():
    budget, calls, issues, call = fixture()
    budget["notifications"] = []
    result = monitor("bot", "secret", True, call, datetime(2026, 10, 3, tzinfo=timezone.utc))
    assert result["created"] == 1 and result["acknowledged"] == 0
    assert "No quota was simulated" in issues[0]["body"]
    assert not any(url.endswith("/ack") for url, _ in calls)


def check_failed_ack_keeps_event_for_retry():
    budget, calls, issues, call = fixture()

    def failing(url, **kw):
        if url.endswith("/ack"):
            return {"ok": False}
        return call(url, **kw)

    with unittest.TestCase().assertRaisesRegex(ValueError, "ack_pending"):
        monitor("bot", "secret", call=failing)
    assert budget["notifications"][0]["notified_at"] is None
    assert monitor("bot", "secret", call=call)["created"] == 0


def check_uncertain_issue_response_is_not_acknowledged():
    budget, calls, issues, call = fixture()

    def failing(url, **kw):
        if url == ISSUES:
            return {"html_url": "https://evil.example/88"}
        return call(url, **kw)

    with unittest.TestCase().assertRaisesRegex(ValueError, "not_confirmed"):
        monitor("bot", "secret", call=failing)
    assert not any(url.endswith("/ack") for url, _ in calls)


def check_guard_unavailable_creates_one_daily_attention_notice():
    budget, calls, issues, call = fixture()
    budget["initialized"] = False
    assert monitor("bot", "secret", call=call)["created"] == 1
    assert "monitoring is unavailable" in issues[0]["body"]
    assert monitor("bot", "secret", call=call)["created"] == 0
    assert not any(url.endswith("/ack") for url, _ in calls)


class R2BudgetMonitorTests(unittest.TestCase):
    test_cloud_notification = staticmethod(check_cloud_notification_mentions_owner_and_acks_after_issue_confirmation)
    test_duplicate_retry = staticmethod(check_ack_retry_finds_existing_issue_without_duplicate_notification)
    test_silent = staticmethod(check_silent_below_warning_with_no_github_reads)
    test_explicit_test = staticmethod(check_test_notice_is_explicit_and_does_not_fake_or_ack_quota_event)
    test_failed_ack = staticmethod(check_failed_ack_keeps_event_for_retry)
    test_uncertain_issue = staticmethod(check_uncertain_issue_response_is_not_acknowledged)
    test_unavailable = staticmethod(check_guard_unavailable_creates_one_daily_attention_notice)
