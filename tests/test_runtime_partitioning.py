import copy
import json
import random
import tempfile
import unittest
from unittest.mock import Mock
from pathlib import Path
from unittest.mock import patch

import scanner as s
from runtime_checkpoint import build_checkpoint, checkpoint_documents, hydrate_checkpoint, decode_checkpoint
from runtime_dashboard import dashboard_documents


class RuntimePartitioningTests(unittest.TestCase):
    def test_balance_only_report_publishes_without_inventing_a_new_signal_or_deep_scan(self):
        state = {"last_deep_scan_at": "2026-10-03T00:00:00Z"}
        rpc = Mock(calls={})
        rpc.provider_stats.return_value = {}
        config = {"_scan_profile": "targeted"}
        with patch.object(s, "report_config_for_lanes", return_value={}), \
             patch.object(s, "save_runtime_state"), \
             patch.object(s, "build_report_payload", return_value={"stats": {}, "alerts": [], "signal_theses": [{"token_address": "updated"}]}), \
             patch.object(s, "sync_remote_snapshot", return_value={"current_synced": True}) as sync, \
             patch.object(s, "write_report_json"), patch.object(s, "write_dashboard_fallback"), patch.object(s, "render_report"):
            s.publish_targeted_balance_report(rpc, [], state, config, {}, {})
        report = sync.call_args.args[0]
        self.assertEqual(report["signal_theses"][0]["token_address"], "updated")
        self.assertEqual(report["alerts"], [])
        self.assertEqual(report["scan_profile"], "targeted")
        self.assertEqual(report["check_scope"], "cohort_balances_only")
        self.assertEqual(report["last_deep_scan_at"], state["last_deep_scan_at"])
        self.assertEqual(report["stats"]["scan_health"]["scanned_pools"], 0)

    def test_large_compressed_evidence_survives_partitioned_round_trip(self):
        rng = random.Random(13)
        state = {"evidence": rng.randbytes(10 * 1024 * 1024).hex()}
        with patch("runtime_checkpoint.INLINE_BYTES", 2 * 1024 * 1024):
            payload = build_checkpoint(state)
            manifest, pieces = checkpoint_documents(payload)
        self.assertEqual(manifest["schema_version"], 2)
        self.assertGreater(manifest["encoded_bytes"], 7 * 1024 * 1024)
        stored = {p["sha256"]: p for p in pieces}
        self.assertEqual(decode_checkpoint(hydrate_checkpoint(manifest, stored.__getitem__)), state)
        with self.assertRaises(KeyError):
            hydrate_checkpoint(manifest, {}.__getitem__)
        stored[pieces[0]["sha256"]] = {**pieces[0], "data": "A" * pieces[0]["encoded_bytes"]}
        with self.assertRaisesRegex(ValueError, "integrity"):
            hydrate_checkpoint(manifest, stored.__getitem__)

    def test_dashboard_separates_every_wallet_without_truncation(self):
        rows = [{"token_address": str(i), "cohort_wallets": [{"owner": str(i), "raw": "x" * 900_000}]} for i in range(10)]
        body = {"report": {"generated_at": "2026-10-03T01:00:00Z"}, "detail_signal_theses": rows}
        root, parts = dashboard_documents(body)
        self.assertLess(len(json.dumps(root)), 10_000)
        self.assertEqual(len(root["token_detail_refs"]), 10)
        self.assertEqual([json.loads(p["data"])["thesis"] for p in parts], rows)

    def test_old_outcomes_cannot_be_attributed_to_a_renewed_thesis(self):
        state = {"pools": {"pool": {"signal_thesis": {"token_address": "mint", "pool_address": "pool",
                  "signal_at": "2026-10-02T00:00:00Z"}}},
                 "signal_outcomes": {"mint": {"caught_at": "2026-09-01T00:00:00Z", "horizons": {
                     "1h": {"at": "2026-09-01T01:00:00Z"}, "7d": {"at": "2026-10-02T01:00:00Z"}}}}}
        original = copy.deepcopy(state)
        with patch.object(s, "load_deleted_tokens", return_value={"tokens": set(), "pools": set()}):
            ledger = s.build_history_ledger({}, state, {}, "2026-10-03T00:00:00Z")
        self.assertEqual([row["event"]["event_type"] for row in ledger["events"]], ["signal"])
        self.assertEqual(ledger["foreign_outcome_episodes_deferred"], 1)
        self.assertEqual(state, original)

    def test_bad_legacy_event_is_preserved_but_does_not_poison_valid_batch(self):
        valid = {"event_id": "valid", "episode": {"episode_id": "episode", "token_address": "mint", "caught_at": "2026-09-01T00:00:00Z"},
                 "event": {"event_type": "signal", "observed_at": "2026-09-01T00:00:00Z"}}
        invalid = {**copy.deepcopy(valid), "event_id": "wrong"}
        invalid["event"]["observed_at"] = "2026-08-31T00:00:00Z"
        body = {"report": {"generated_at": "2026-10-03T00:00:00Z"}, "history_ledger": {"events": [invalid, valid]}}
        with patch.object(s, "remote_api_call", return_value={"ok": True}) as remote:
            s.send_remote_snapshot(body, {})
        self.assertEqual(body["history_ledger"]["events"][0], invalid)
        self.assertEqual(body["_sync_rejected_history"]["0"]["event_id"], "wrong")
        for call in remote.call_args_list:
            if "history_ledger" in call.args[3]:
                self.assertEqual(call.args[3]["history_ledger"]["events"], [valid])
        self.assertEqual(body["_sync_progress"]["durable_history_ledger"], 2)
        self.assertEqual(body["_sync_progress"]["history_ledger"], 2)
        self.assertEqual([call.args[1] for call in remote.call_args_list if "history_ledger" in call.args[3]], ["/api/runtime/history"])

    def test_successful_replay_moves_bad_original_payload_to_quarantine(self):
        invalid = {"event_id": "wrong", "episode": {"episode_id": "episode", "token_address": "mint", "caught_at": "2026-09-02T00:00:00Z"},
                   "event": {"event_type": "outcome_1h", "observed_at": "2026-09-01T01:00:00Z"}}
        report = {"generated_at": "2026-10-03T00:00:00Z"}
        body = {"report": report, "history_ledger": {"events": [invalid]}}
        with tempfile.TemporaryDirectory() as directory, patch.object(s, "REMOTE_OUTBOX_DIR", Path(directory)), \
             patch.object(s, "remote_data_url_from_env", return_value="https://example.invalid"), \
             patch.object(s, "remote_ingest_secret", return_value="test"), \
             patch.object(s, "build_dashboard_snapshot", return_value=body), \
             patch.object(s, "remote_api_call", return_value={"ok": True, "accepted": True}):
            result = s.sync_remote_snapshot(report, {}, {})
            self.assertEqual(result["pending"], 0)
            self.assertEqual(result["quarantined"], 1)
            import gzip
            saved = json.loads(gzip.decompress(next((Path(directory) / "quarantine").glob("*.gz")).read_bytes()))
            self.assertEqual(saved["history_ledger"]["events"], [invalid])
