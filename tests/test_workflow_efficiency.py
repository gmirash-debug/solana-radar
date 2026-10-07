import ast
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tools.runtime_cache import capture, contents_changed, outbox_fingerprints, validate
from tools.scan_watchdog import main as watchdog_main
from tools.workflow_plan import workflow_plan


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def workflow_steps(name):
    """Match the existing text-based workflow contract test convention."""
    source = (WORKFLOWS / name).read_text()
    return {match[1]: match[2] for match in re.finditer(
        r"^      - name: ([^\n]+)\n(.*?)(?=^      - |^  [a-z]|\Z)", source, re.M | re.S)}


def condition(step, values=None, succeeded=True):
    expression = re.search(r"^        if: (.*)$", step, re.M)
    if not expression:
        return succeeded
    value = expression[1]
    always = "always()" in value
    if not succeeded and not always:
        return False
    value = value.replace("always()", "True")
    value = re.sub(r"steps\.[\w-]+\.(?:outputs\.[\w-]+|outcome)",
                   lambda match: repr((values or {}).get(match[0], "")), value)
    value = value.replace("&&", " and ").replace("||", " or ")
    return bool(eval(value, {"__builtins__": {}}, {}))


class WorkflowPlanTests(unittest.TestCase):
    def test_scheduled_storage_recovery_never_runs_rpc_or_pages(self):
        plan=workflow_plan({"event_name":"schedule"})
        self.assertTrue(plan["recover_storage"])
        self.assertFalse(plan["publish_pages"])
        self.assertFalse(plan["observe_robinhood"])

    def setUp(self):
        self.now = datetime(2026, 10, 4, 6, 45, tzinfo=timezone.utc)

    def plan(self, source="manual", bucket="manual", event="workflow_dispatch", now=None):
        return workflow_plan({"event_name": event, "inputs": {
            "source": source, "dispatch_bucket": bucket}}, now or self.now)

    def test_targeted_never_builds_pages_or_runs_robinhood(self):
        for source in ("cloudflare-targeted", "manual-targeted"):
            with self.subTest(source=source):
                plan = self.plan(source, "targeted:2026-10-04T06:00:00.000Z")
                self.assertTrue(plan["targeted"])
                self.assertFalse(plan["publish_pages"])
                self.assertFalse(plan["observe_robinhood"])
                self.assertEqual(plan["validate"], source == "manual-targeted")

    def test_ui_push_and_manual_ui_publish_without_observing(self):
        for source, event in (("manual", "push"), ("manual-ui", "workflow_dispatch")):
            plan = self.plan(source, event=event)
            self.assertTrue(plan["publish_pages"])
            self.assertFalse(plan["observe_robinhood"])
            self.assertTrue(plan["validate"])

    def test_recovery_only_publishes_saved_runtime(self):
        plan = self.plan("publication-recovery")
        self.assertTrue(plan["recover_runtime"])
        self.assertTrue(plan["validate"])
        self.assertFalse(plan["publish_pages"])
        self.assertFalse(plan["observe_robinhood"])
        steps = workflow_steps("scan-and-pages.yml")
        values = {"steps.freshness.outputs.should_scan": "false",
                  "steps.plan.outputs.recover_runtime": "true"}
        self.assertTrue(condition(steps["Preserve pending cloud writes"], values))
        self.assertTrue(condition(steps["Recover runtime publication without scanning"], values))
        for name in ("Run scanner", "Observe Robinhood mainnet", "Save pending cloud writes",
                     "Install GMGN CLI when used", "Build Pages artifact"):
            self.assertFalse(condition(steps[name], values))

    def test_six_hour_deep_and_watchdog_slots(self):
        for hour in range(24):
            now = self.now.replace(hour=hour)
            for source, prefix, suffix in (("cloudflare-deep_scan", "deep_scan", ":00:00.000Z"),
                                            ("cloudflare-watchdog", "watchdog", "")):
                bucket = f"{prefix}:2026-10-04T{hour:02d}{suffix}"
                with self.subTest(bucket=bucket):
                    self.assertEqual(self.plan(source, bucket, now=now)["publish_pages"], hour % 6 == 0)

    def test_delayed_jobs_use_dispatch_slot_not_runner_hour(self):
        now = self.now.replace(hour=7)
        self.assertTrue(self.plan("cloudflare-deep_scan", "deep_scan:2026-10-04T06:00:00Z", now=now)["publish_pages"])
        self.assertFalse(self.plan("cloudflare-deep_scan", "deep_scan:2026-10-04T05:00:00Z")["publish_pages"])

    def test_malformed_stale_future_and_unknown_buckets_do_not_publish(self):
        for bucket in (None, [], "manual", "deep_scan:bad", "deep_scan:2026-10-04T06:00:00",
                       "deep_scan:2026-10-04T00:00:00Z", "deep_scan:2026-10-04T12:00:00Z",
                       "targeted:2026-10-04T06:00:00Z"):
            self.assertFalse(self.plan("cloudflare-deep_scan", bucket)["publish_pages"])
        self.assertFalse(self.plan("cloudflare-new-kind", "deep_scan:2026-10-04T06:00:00Z")["publish_pages"])

    def test_manual_deep_and_robinhood_keep_publication_and_backup_path(self):
        for source in ("manual", "manual-http", "robinhood"):
            plan = self.plan(source)
            self.assertTrue(plan["publish_pages"])
            self.assertTrue(plan["observe_robinhood"])

    def test_plan_cli_uses_event_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps({"inputs": {"source": "manual-targeted"}}))
            output = subprocess.check_output([sys.executable, str(ROOT / "tools/workflow_plan.py")],
                env={**os.environ, "GITHUB_EVENT_PATH": str(path), "GITHUB_EVENT_NAME": "workflow_dispatch"}, text=True)
        self.assertIn("targeted=true", output)
        self.assertIn("publish_pages=false", output)


class CacheChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.outbox = self.data / "remote_outbox"
        self.outbox.mkdir()

    def test_unchanged_bytes_need_no_duplicate_even_if_mtime_changes(self):
        path = self.outbox / "pending.json.gz"
        path.write_bytes(b"unique backlog")
        before = outbox_fingerprints(self.data)
        path.touch()
        self.assertFalse(contents_changed(before, outbox_fingerprints(self.data)))
        self.assertEqual(path.read_bytes(), b"unique backlog")

    def test_same_size_and_mtime_rewrite_is_not_missed(self):
        path = self.outbox / "pending.json.gz"
        path.write_bytes(b"abcd")
        before = outbox_fingerprints(self.data)
        stamp = path.stat().st_mtime_ns
        path.write_bytes(b"efgh")
        os.utime(path, ns=(stamp, stamp))
        self.assertTrue(contents_changed(before, outbox_fingerprints(self.data)))

    def test_new_partial_and_quarantined_evidence_always_requires_save(self):
        for name in ("new.json.gz", "pending.tmp", "quarantine/rejected.json.gz"):
            before = outbox_fingerprints(self.data)
            path = self.outbox / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"original evidence")
            self.assertTrue(contents_changed(before, outbox_fingerprints(self.data)))
            self.assertEqual(path.read_bytes(), b"original evidence")

    def test_rename_and_scanner_ack_removal_are_changes_not_helper_deletions(self):
        path = self.outbox / "pending.json.gz"
        path.write_bytes(b"pending")
        before = outbox_fingerprints(self.data)
        path = path.rename(self.outbox / "renamed.json.gz")
        self.assertTrue(contents_changed(before, outbox_fingerprints(self.data)))
        before = outbox_fingerprints(self.data)
        path.unlink()  # Simulated scanner-owned ack; the helper never deletes.
        self.assertTrue(contents_changed(before, outbox_fingerprints(self.data)))

    def test_unknown_legacy_or_failed_comparison_is_conservative(self):
        current = outbox_fingerprints(self.data)
        for before in (None, [], {}, {"errors": [], "files": None}, {"files": {}, "errors": ["io failure"]}):
            self.assertTrue(contents_changed(before, current))
        self.assertTrue(contents_changed(current, {"files": {}, "errors": ["io failure"]}))

    def test_symlink_never_proves_unchanged_and_does_not_modify_target(self):
        target = self.data / "private.json"
        target.write_bytes(b"private evidence")
        (self.outbox / "link").symlink_to(target)
        before = outbox_fingerprints(self.data)
        self.assertTrue(before["errors"])
        self.assertTrue(contents_changed(before, outbox_fingerprints(self.data)))
        self.assertEqual(target.read_bytes(), b"private evidence")

    def test_runtime_and_status_change_without_losing_revision_validation(self):
        path = self.data / "state.json"
        state = {"_runtime": {"schema_version": 1, "revision": 3,
                             "updated_at": "2026-10-04T06:00:00Z"}, "pools": {}}
        path.write_text(json.dumps(state))
        before = capture(self.data)
        self.assertTrue(validate(self.data, before)[0])
        self.assertFalse(contents_changed(before["fingerprints"], capture(self.data)["fingerprints"]))
        (self.data / "discovery_status.json").write_text('{"status":"failed"}')
        self.assertTrue(contents_changed(before["fingerprints"], capture(self.data)["fingerprints"]))
        state["_runtime"]["revision"] = 2
        path.write_text(json.dumps(state))
        self.assertFalse(validate(self.data, before)[0])

    def test_cache_cli_missing_baseline_requires_save(self):
        output = subprocess.check_output([sys.executable, str(ROOT / "tools/runtime_cache.py"),
            "check-outbox", str(self.data / "missing.json"), "--data-dir", str(self.data)], text=True)
        self.assertEqual(output.strip(), "changed=true")

    def test_cli_capture_and_validate_reports_unchanged_valid_state(self):
        (self.data / "state.json").write_text(json.dumps({"_runtime": {
            "schema_version": 1, "revision": 1, "updated_at": "2026-10-04T06:00:00Z"}}))
        args = [sys.executable, str(ROOT / "tools/runtime_cache.py")]
        baseline = self.data / "baseline.json"
        subprocess.check_call([*args, "capture", str(baseline), "--data-dir", str(self.data)])
        output = subprocess.check_output([*args, "validate", str(baseline), "--data-dir", str(self.data)], text=True)
        self.assertIn("valid=true", output)
        self.assertIn("changed=false", output)


class WorkflowContracts(unittest.TestCase):
    def setUp(self):
        self.steps = workflow_steps("scan-and-pages.yml")

    def test_single_shared_lock_for_all_mutable_writers(self):
        source = (WORKFLOWS / "scan-and-pages.yml").read_text()
        expression = re.search(r"^  group: (.*)$", source, re.M)[1]
        self.assertIn("'solana-radar-state-writer'", expression)
        self.assertIn("inputs.source == 'manual-ui'", expression)
        self.assertNotIn("targeted", expression)
        discovery = (WORKFLOWS / "discovery-pulse.yml").read_text()
        self.assertIn("group: solana-radar-state-writer", discovery)
        self.assertIn("cancel-in-progress: false", discovery)
        self.assertIn("cancel-in-progress: false", source)

    def test_targeted_pages_and_dashboard_installs_are_skipped(self):
        values = {"steps.plan.outputs.publish_pages": "false", "steps.plan.outputs.validate": "false",
                  "steps.freshness.outputs.should_scan": "true", "steps.run_scanner.outputs.exit_code": "0"}
        self.assertFalse(condition(self.steps["Build Pages artifact"], values))
        self.assertFalse(condition(self.steps["Install dashboard test dependencies"], values))
        self.assertTrue(condition(self.steps["Install Python dependencies"], values))
        self.assertTrue(condition(self.steps["Run scanner"], values))
        self.assertIn("RADAR_DATA_API_URL:", self.steps["Run scanner"])
        self.assertIn("--targeted", self.steps["Run scanner"])

    def test_push_manual_and_successful_due_deep_publish_but_failed_scan_does_not(self):
        for should_scan, exit_code, expected in (("false", "", True), ("true", "0", True),
                                                 ("true", "1", False), ("true", "124", False)):
            values = {"steps.plan.outputs.publish_pages": "true", "steps.freshness.outputs.should_scan": should_scan,
                      "steps.run_scanner.outputs.exit_code": exit_code}
            self.assertEqual(condition(self.steps["Build Pages artifact"], values), expected)
        for name in ("Configure Pages", "Upload Pages artifact"):
            self.assertFalse(condition(self.steps[name], {"steps.build_pages.outcome": "skipped"}))
            self.assertTrue(condition(self.steps[name], {"steps.build_pages.outcome": "success"}))

    def test_cache_save_skips_only_proven_unchanged_successful_restore(self):
        pairs = [(self.steps["Save pending cloud writes"], "outbox_changes", "outbox_restore"),
                 (self.steps["Save scanner runtime state"], "runtime_cache", "state_restore"),
                 (workflow_steps("discovery-pulse.yml")["Save shared scanner state"], "runtime_cache", "state_restore")]
        for step, check, restore in pairs:
            for changed, key, outcome, expected in (("false", "restored", "success", False),
                                                   ("true", "restored", "success", True),
                                                   ("", "restored", "success", True),
                                                   ("false", "", "success", True),
                                                   ("false", "restored", "failure", True)):
                values = {"steps.freshness.outputs.should_scan": "true", "steps.runtime_cache.outputs.valid": "true",
                          f"steps.{check}.outputs.changed": changed, f"steps.{restore}.outputs.cache-matched-key": key,
                          f"steps.{restore}.outcome": outcome}
                with self.subTest(check=check, changed=changed, key=key, outcome=outcome):
                    self.assertEqual(condition(step, values, succeeded=False), expected)

    def test_invalid_runtime_is_not_saved_and_cache_prefixes_are_compatible(self):
        for name, save in (("scan-and-pages.yml", "Save scanner runtime state"),
                           ("discovery-pulse.yml", "Save shared scanner state")):
            source = (WORKFLOWS / name).read_text()
            self.assertFalse(condition(workflow_steps(name)[save], {"steps.runtime_cache.outputs.valid": "false"}))
            self.assertIn("solana-radar-state-v5-20261007-clean-v1-", source)
            self.assertIn("github.run_attempt", source)
            self.assertIn("restore-keys:", source)
        self.assertIn("solana-radar-outbox-v2-20261007-clean-v1-", (WORKFLOWS / "scan-and-pages.yml").read_text())

    def test_baselines_precede_installs_and_validation(self):
        names = list(self.steps)
        self.assertLess(names.index("Record pre-writer runtime revisions"), names.index("Install Python dependencies"))
        self.assertLess(names.index("Record pre-writer runtime revisions"), names.index("Validate scanner and dashboard"))
        self.assertLess(names.index("Record pre-scan outbox contents"), names.index("Run scanner"))
        discovery = list(workflow_steps("discovery-pulse.yml"))
        self.assertLess(discovery.index("Record pre-pulse runtime revisions"), discovery.index("Install discovery dependencies"))

    def test_dependency_download_hit_never_skips_install_on_new_runner(self):
        for name in ("Install Python dependencies", "Install dashboard test dependencies", "Install GMGN CLI when used"):
            self.assertNotIn("cache-hit", self.steps[name])
        source = (WORKFLOWS / "scan-and-pages.yml").read_text()
        self.assertEqual(source.count("uses: actions/setup-node@"), 1)
        self.assertIn('if [ -n "$GMGN_API_KEY" ]', self.steps["Install GMGN CLI when used"])
        self.assertNotIn("npm ci", (WORKFLOWS / "discovery-pulse.yml").read_text())

    def test_robinhood_only_restores_captures_validates_and_saves_shared_ledger(self):
        values = {"steps.freshness.outputs.should_scan": "false", "steps.plan.outputs.observe_robinhood": "true",
                  "steps.plan.outputs.publish_pages": "false"}
        self.assertTrue(condition(self.steps["Restore scanner runtime state"], values))
        self.assertTrue(condition(self.steps["Record pre-writer runtime revisions"], values))
        self.assertTrue(condition(self.steps["Validate failure-safe runtime cache"], values, succeeded=False))
        self.assertFalse(condition(self.steps["Record pre-scan outbox contents"], values))
        names = list(self.steps)
        self.assertLess(names.index("Observe Robinhood mainnet"), names.index("Validate failure-safe runtime cache"))
        self.assertLess(names.index("Validate failure-safe runtime cache"), names.index("Save scanner runtime state"))
        self.assertIn("data/state.json", self.steps["Save scanner runtime state"])
        observer = self.steps["Observe Robinhood mainnet"]
        self.assertIn("RADAR_DATA_API_URL:", observer)
        self.assertIn("secrets.RADAR_DATA_URL", observer)
        self.assertIn("RADAR_INGEST_SECRET: ${{ secrets.RADAR_INGEST_SECRET }}", observer)

    def test_history_consumer_hook_is_bounded_optional_and_after_rpc_and_live_publish(self):
        names = list(self.steps)
        name = "Flush bounded SQL history queue after live publication"
        self.assertLess(names.index("Run scanner"), names.index(name))
        self.assertLess(names.index(name), names.index("Save pending cloud writes"))
        step = self.steps[name]
        self.assertIn("continue-on-error: true", step)
        self.assertIn("steps.run_scanner.outcome == 'success'", step)
        self.assertIn("steps.run_scanner.outputs.exit_code == '0'", step)
        self.assertIn("hashFiles('tools/flush_history_queue.py') != ''", step)
        self.assertIn("--max-flushes 10 --max-seconds 55", step)
        self.assertIn("--kill-after=5s 60s", step)
        self.assertIn("RADAR_INGEST_SECRET:", step)

    def test_history_consumer_never_runs_for_a_skipped_or_failed_scanner(self):
        step = self.steps["Flush bounded SQL history queue after live publication"].replace(
            "hashFiles('tools/flush_history_queue.py') != ''", "True")
        for outcome, code, expected in (("success", "0", True), ("success", "1", False),
                                         ("skipped", "", False), ("skipped", "0", False),
                                         ("failure", "0", False)):
            with self.subTest(outcome=outcome, code=code):
                values = {"steps.run_scanner.outcome": outcome, "steps.run_scanner.outputs.exit_code": code}
                self.assertEqual(condition(step, values), expected)

    def test_manual_ui_freshness_step_does_not_scan(self):
        source = self.steps["Check scan freshness"]
        code = re.search(r"python - <<'PY'[^\n]*\n(.*?)^          PY$", source, re.M | re.S)[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps({"inputs": {"source": "manual-ui"}}))
            output = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(path), "GITHUB_EVENT_NAME": "workflow_dispatch"}), \
                    patch("urllib.request.urlopen", side_effect=AssertionError("UI publication must not probe scan freshness")), \
                    contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit):
                    exec(compile(textwrap.dedent(code), "workflow-freshness", "exec"), {})
        self.assertIn("should_scan=false", output.getvalue())

    def test_recovery_freshness_never_probes_or_scans(self):
        source = self.steps["Check scan freshness"]
        code = re.search(r"python - <<'PY'[^\n]*\n(.*?)^          PY$", source, re.M | re.S)[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps({"inputs": {"source": "publication-recovery"}}))
            output = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(path), "GITHUB_EVENT_NAME": "workflow_dispatch"}), \
                    patch("urllib.request.urlopen", side_effect=AssertionError("recovery must not probe freshness")), \
                    contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit):
                    exec(compile(textwrap.dedent(code), "workflow-freshness", "exec"), {})
        self.assertIn("should_scan=false", output.getvalue())

    def test_snapshot_timestamp_and_existing_backup_are_preserved(self):
        step = self.steps["Build Pages artifact"]
        self.assertIn('"$publisher/tools/build_pages.py"', step)
        self.assertNotIn("generated_at=", step)
        self.assertIn("Daily Robinhood database backup", self.steps)
        source = (WORKFLOWS / "scan-and-pages.yml").read_text()
        self.assertNotIn("storage_backup.py", source)
        self.assertNotIn("gh cache delete", source)
        self.assertNotIn("rm -rf data/remote_outbox", source)

    def test_config_pacing_keys_have_real_callsites_and_no_new_whole_scan_pause(self):
        config = json.loads((ROOT / "config.example.json").read_text())
        tree = ast.parse((ROOT / "scanner.py").read_text())
        functions = {node.name: ast.get_source_segment((ROOT / "scanner.py").read_text(), node)
                     for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ("fetch_gecko_universe", "run_gmgn_cli")}
        self.assertIn('config.get("gmgn_min_interval_seconds"', functions["run_gmgn_cli"])
        self.assertIn('config.get("market_request_delay_seconds"', functions["fetch_gecko_universe"])
        self.assertEqual(config["gmgn_min_interval_seconds"], 1.2)
        self.assertEqual(config["market_request_delay_seconds"], 3.0)


class WatchdogEfficiencyTests(unittest.TestCase):
    def test_fresh_live_deep_scan_needs_no_pages_or_github_probe(self):
        payload = {"report": {"generated_at": datetime.now(timezone.utc).isoformat(), "scan_profile": "deep"}}
        with patch.dict(os.environ, {"GH_TOKEN": "test-token"}), \
                patch("tools.scan_watchdog.fetch_json", return_value=payload) as fetched, \
                patch("tools.scan_watchdog.urlopen") as opened, patch("tools.scan_watchdog.print") as output:
            watchdog_main()
        self.assertEqual(fetched.call_count, 1)
        opened.assert_not_called()
        self.assertIn("deep_scan_fresh", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
