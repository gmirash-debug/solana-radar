import json
import contextlib
import io
import os
import re
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import scanner
from tools.runtime_cache import capture, validate


class RuntimeCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, revision=4, timestamp="2026-10-03T11:00:00Z", name="state.json"):
        state = {"_runtime": {"schema_version": 1, "revision": revision, "updated_at": timestamp},
                 "pools": {"pool": {"cursor": f"cursor-{revision}"}},
                 "rpc_monthly_usage": {"2026-10": {"helius": {"estimated_units": revision}}}}
        (self.root / name).write_text(json.dumps(state))
        return state

    def test_failed_attempt_can_preserve_advanced_state_without_remote_ack(self):
        self.write()
        before = capture(self.root)
        saved = self.write(5, "2026-10-03T11:05:00Z")
        self.assertTrue(validate(self.root, before)[0])
        self.assertEqual(json.loads((self.root / "state.json").read_text()), saved)

    def test_unchanged_state_remains_valid_after_early_failure(self):
        self.write()
        self.assertTrue(validate(self.root, capture(self.root))[0])

    def test_real_failure_checkpoint_keeps_cursor_when_remote_ack_is_false(self):
        state = self.write()
        before = capture(self.root)
        state["pools"]["pool"]["cursor"] = "new-failed-attempt-cursor"
        with patch.object(scanner, "STATE_PATH", self.root / "state.json"), \
                patch.object(scanner, "sync_runtime_checkpoint", return_value={"ok": False}):
            scanner.save_runtime_state(state, {}, "failed_attempt", "2026-10-03T11:05:00Z")
        self.assertTrue(validate(self.root, before)[0])
        saved = json.loads((self.root / "state.json").read_text())
        self.assertEqual(saved["_runtime"]["revision"], 5)
        self.assertEqual(saved["pools"]["pool"]["cursor"], "new-failed-attempt-cursor")

    def test_first_discovery_checkpoint_is_preserved(self):
        before = capture(self.root)
        self.write(1, name="discovery_state.json")
        self.assertTrue(validate(self.root, before)[0])

    def test_missing_versioned_state_is_not_cached(self):
        self.assertFalse(validate(self.root, capture(self.root))[0])
        (self.root / "state.json").write_text("{}")
        self.assertFalse(validate(self.root, {"states": {}, "errors": []})[0])

    def test_corrupt_state_is_not_cached(self):
        self.write()
        before = capture(self.root)
        for content in ("{truncated", "[]", '{"market":NaN}', '{"_runtime":{"revision":true}}'):
            with self.subTest(content=content):
                (self.root / "state.json").write_text(content)
                self.assertFalse(validate(self.root, before)[0])

    def test_repaired_corrupt_or_unversioned_baseline_can_be_saved(self):
        for previous in ("{truncated", "{}", "[]"):
            with self.subTest(previous=previous):
                (self.root / "state.json").write_text(previous)
                before = capture(self.root)
                self.assertTrue(before["errors"])
                self.write(1)
                self.assertTrue(validate(self.root, before)[0])

    def test_corrupt_predecessor_cannot_disappear_behind_another_valid_file(self):
        (self.root / "state.json").write_text("{truncated")
        self.write(name="discovery_state.json")
        before = capture(self.root)
        (self.root / "state.json").unlink()
        self.assertFalse(validate(self.root, before)[0])

    def test_boolean_schema_version_is_not_supported_metadata(self):
        state = self.write()
        state["_runtime"]["schema_version"] = True
        (self.root / "state.json").write_text(json.dumps(state))
        self.assertTrue(capture(self.root)["errors"])

    def test_revision_timestamp_rollback_and_disappearance_are_rejected(self):
        self.write()
        before = capture(self.root)
        for revision, timestamp in [(3, "2026-10-03T11:05:00Z"), (5, "2026-10-03T10:00:00Z")]:
            self.write(revision, timestamp)
            self.assertFalse(validate(self.root, before)[0])
        (self.root / "state.json").unlink()
        self.assertFalse(validate(self.root, before)[0])

    def test_naive_clock_and_wrong_sections_are_rejected(self):
        self.write(timestamp="2026-10-03T11:00:00")
        self.assertTrue(capture(self.root)["errors"])
        state = self.write()
        state["pools"] = []
        (self.root / "state.json").write_text(json.dumps(state))
        self.assertTrue(capture(self.root)["errors"])

    def test_both_workflows_validate_and_save_after_nonzero_exit(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("scan-and-pages.yml", "discovery-pulse.yml"):
            workflow = (root / ".github/workflows" / name).read_text()
            self.assertIn("python tools/runtime_cache.py capture", workflow)
            self.assertIn("python tools/runtime_cache.py validate", workflow)
            self.assertIn("if: always() && steps.runtime_cache.outputs.valid == 'true'", workflow)


class WorkflowFailureContractTests(unittest.TestCase):
    def decision(self, error_type, category, health=None):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/scan-and-pages.yml").read_text()
        block = re.search(r"python - <<'PY'[^\n]*\n(.*?)^          PY$", workflow, re.M | re.S)
        self.assertIsNotNone(block)
        payload = {"ok": True, "report": {"generated_at": "2000-01-01T00:00:00Z"},
                   "scan_status": {"status": "failed", "error_type": error_type,
                                   "error_category": category, "scan_health": health,
                                   "error": "credit quota timeout",
                                   "last_attempt_at": datetime.now(timezone.utc).isoformat()}}
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            event=Path(directory)/"event.json"
            event.write_text(json.dumps({"inputs":{"source":"cloudflare-deep_scan"}}))
            with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_EVENT_PATH": str(event),
                                        "RADAR_DATA_API_URL": "https://example.invalid"}), \
                    patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())), \
                    contextlib.redirect_stdout(output):
                try:
                    exec(compile(textwrap.dedent(block[1]), "workflow-freshness", "exec"), {})
                except SystemExit:
                    pass
        return output.getvalue()

    def test_programming_error_with_quota_word_or_health_never_causes_backoff(self):
        result = self.decision("KeyError", None, {"scan_error_categories": {"helius_quota": 1}})
        self.assertIn("should_scan=true", result)
        self.assertNotIn("provider_backoff", result)

    def test_typed_persistent_provider_failure_retains_backoff(self):
        result = self.decision("HeliusRpcError", "quota")
        self.assertIn("should_scan=false", result)
        self.assertIn("provider_backoff", result)

    def test_transient_timeout_does_not_add_six_hour_backoff(self):
        self.assertIn("should_scan=true", self.decision("TimeoutError", "timeout"))

    def test_all_provider_wrapper_requires_proven_persistent_underlying_failures(self):
        for categories in ({"rpc_all_unavailable": 1}, {"rpc_all_unavailable": 1, "alchemy_transport": 1},
                           {"helius_quota": 1, "alchemy_timeout": 1}):
            with self.subTest(categories=categories):
                result = self.decision("RpcProvidersUnavailable", "rpc_all_unavailable",
                                       {"scan_error_categories": categories})
                self.assertIn("should_scan=true", result)
                self.assertNotIn("provider_backoff", result)
        result = self.decision("RpcProvidersUnavailable", "rpc_all_unavailable",
                               {"scan_error_categories": {"rpc_all_unavailable": 1, "helius_quota": 1, "alchemy_auth": 1}})
        self.assertIn("provider_backoff", result)


if __name__ == "__main__":
    unittest.main()
