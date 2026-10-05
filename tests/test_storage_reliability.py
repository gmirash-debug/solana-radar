import copy
import gzip
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import scanner
from cold_outbox import archive_snapshot, decode_snapshot
from runtime_checkpoint import build_checkpoint, checkpoint_documents


class StorageReliabilityTests(unittest.TestCase):
    def test_checkpoint_uses_small_parts_and_recovers_from_transient_write(self):
        state={"evidence":random.Random(17).randbytes(900000).hex(),
               "_runtime":{"updated_at":"2026-10-05T12:00:00Z","revision":2}}
        manifest,parts=checkpoint_documents(build_checkpoint(state))
        self.assertGreater(len(parts),1)
        self.assertTrue(all(part["encoded_bytes"]<=256*1024 for part in parts))
        calls=[]; failed=False
        def remote(method,path,config,payload,params=None):
            nonlocal failed
            calls.append((path,params))
            if path.endswith("checkpoint-parts"):
                return {"ok":True,"parts":[{"id":parts[0]["sha256"],"bytes":parts[0]["encoded_bytes"]}]}
            if params.get("part") and not failed:
                failed=True
                raise RuntimeError("Remote HTTP 503: storage_sql_http_error")
            return {"ok":True,"accepted":True}
        with patch.object(scanner,"remote_data_url_from_env",return_value="https://example.invalid"), \
             patch.object(scanner,"remote_ingest_secret",return_value="test"), \
             patch.object(scanner,"remote_api_call",side_effect=remote),patch.object(scanner.time,"sleep"):
            result=scanner.sync_runtime_checkpoint(state,{},"deep")
        self.assertTrue(result["accepted"])
        self.assertNotIn(("/api/runtime/checkpoint",{"kind":"deep","part":parts[0]["sha256"]}),calls)
        self.assertEqual(calls[-1],("/api/runtime/checkpoint",{"kind":"deep"}))

    def test_rejected_manifest_never_becomes_a_successful_checkpoint(self):
        with patch.object(scanner,"remote_data_url_from_env",return_value="https://example.invalid"), \
             patch.object(scanner,"remote_ingest_secret",return_value="test"), \
             patch.object(scanner,"remote_api_call",return_value={"ok":True,"accepted":False}):
            result=scanner.sync_runtime_checkpoint({"_runtime":{"updated_at":"2026-10-05T12:00:00Z"}}, {},"deep")
        self.assertFalse(result["ok"])

    def test_runtime_ack_eliminates_duplicate_legacy_publication(self):
        body={"report":{"generated_at":"2026-10-05T12:00:00Z"},"_sync_progress":{"runtime_dashboard":1}}
        with patch.object(scanner,"remote_api_call") as api:
            self.assertTrue(scanner.send_remote_snapshot(body,{"remote_legacy_snapshot_enabled":False}))
        api.assert_not_called()

    def test_failed_runtime_cannot_delete_a_report_without_source_archive(self):
        body={"report":{"generated_at":"2026-10-05T12:00:00Z"}}
        with patch.object(scanner,"remote_api_call") as api:
            self.assertFalse(scanner.send_remote_snapshot(body,{"remote_legacy_snapshot_enabled":False}))
        api.assert_not_called()

    def test_archive_identity_is_stable_across_progress_and_progress_needs_ack(self):
        body={"report":{"generated_at":"2026-10-05T12:00:00Z"},"original":{"caught":"2026-09-02T00:00:00Z"},
              "_sync_progress":{"durable_history_ledger":3}}
        session=Mock()
        def post(*args,**kwargs):
            digest=kwargs["params"]["id"]
            return Mock(ok=True,json=lambda:{"ok":True,"accepted":True,"id":digest,
                "archive_ref":{"sha256":digest,"key":f"outbox/v1/sha256/{digest[:2]}/{digest}.json.gz","bytes":len(kwargs["data"])}})
        session.post.side_effect=post
        session.patch.return_value=Mock(ok=True,json=lambda:{"ok":True,"accepted":True})
        first=archive_snapshot(body,"https://example.invalid","test",session=session)
        changed=copy.deepcopy(body);changed["_sync_progress"]["durable_history_ledger"]=10
        second=archive_snapshot(changed,"https://example.invalid","test",session=session)
        self.assertEqual(first["id"],second["id"])
        data=session.post.call_args.kwargs["data"]
        restored=decode_snapshot(data,first["archive_ref"])
        self.assertEqual(restored["original"],body["original"])
        self.assertNotIn("_sync_progress",restored)
        session.patch.return_value=Mock(ok=False,json=lambda:{"ok":False})
        with self.assertRaisesRegex(RuntimeError,"progress is unacknowledged"):
            archive_snapshot(body,"https://example.invalid","test",session=session)

    def test_archive_failure_keeps_local_original(self):
        report={"generated_at":"2026-10-05T12:00:00Z"}
        body={"report":report,"history_ledger":{"events":[]}}
        with tempfile.TemporaryDirectory() as directory,patch.object(scanner,"REMOTE_OUTBOX_DIR",Path(directory)), \
             patch.object(scanner,"remote_data_url_from_env",return_value="https://example.invalid"), \
             patch.object(scanner,"remote_ingest_secret",return_value="test"), \
             patch.object(scanner,"build_dashboard_snapshot",return_value=body), \
             patch.object(scanner,"publish_runtime_dashboard",side_effect=RuntimeError("unavailable")), \
             patch.object(scanner,"archive_snapshot",side_effect=RuntimeError("guard paused")):
            result=scanner.sync_remote_snapshot(report,{},
                {"remote_legacy_snapshot_enabled":False,"remote_cold_outbox_enabled":True})
            self.assertEqual(result["pending"],1)
            path=next(Path(directory).glob("*.json.gz"))
            self.assertEqual(json.loads(gzip.decompress(path.read_bytes()))["report"],report)


if __name__=="__main__":
    unittest.main()
