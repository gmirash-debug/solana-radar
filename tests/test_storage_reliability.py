import copy
import base64
import gzip
import hashlib
import json
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import scanner
from tools import recover_storage
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
        self.assertEqual(result["parts_total"],len(parts))
        self.assertEqual(result["parts_reused"],1)
        self.assertEqual(result["parts_uploaded"],len(parts)-1)
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

    def test_malformed_legacy_rows_do_not_block_fresh_history_or_escape_quarantine(self):
        valid={"episode":{"episode_id":"episode","token_address":"token","caught_at":"2026-10-05T10:00:00Z"},
               "event":{"event_type":"signal","observed_at":"2026-10-05T12:00:00Z"}}
        malformed=["bad",{"episode":"bad","event":{}},{"episode":{},"event":[]}]
        body={"report":{"generated_at":"2026-10-05T12:00:00Z"},
              "history_ledger":{"events":[*malformed,valid]},"_sync_progress":{"source_archive":1}}
        with patch.object(scanner,"remote_api_call",return_value={"ok":True}) as api:
            self.assertTrue(scanner.send_remote_snapshot(body,{"remote_legacy_snapshot_enabled":False}))
        self.assertEqual(api.call_count,1)
        sent=api.call_args.args[3]
        self.assertEqual(sent["history_ledger"]["events"],[valid])
        self.assertEqual(sent["priority_episodes"],["episode"])
        self.assertEqual(len(body["_sync_rejected_history"]),3)
        self.assertEqual(body["history_ledger"]["events"][1:],malformed)

    def test_recovery_reports_unacknowledged_checkpoint_as_failure(self):
        for saved,status,exit_code in ((False,"checkpoint_pending",1),(True,"partial",0)):
            result={"checkpoint_saved":saved,"deferred":1,"archived":1,"replayed":0,"rpc_calls":0}
            with patch.object(recover_storage,"recover",return_value=result), \
                 patch("sys.argv",["recover_storage"]),patch("builtins.print") as output:
                self.assertEqual(recover_storage.main(),exit_code)
            self.assertEqual(json.loads(output.call_args.args[0])["status"],status)

    def test_checkpoint_identity_survives_sorted_state_round_trip(self):
        state={"z":{"last":1,"first":2},"a":[{"y":3,"x":4}],
               "_runtime":{"writer":"test","updated_at":"2026-10-05T12:00:00Z","revision":3}}
        reloaded=json.loads(json.dumps(state,sort_keys=True))
        self.assertEqual(build_checkpoint(state),build_checkpoint(reloaded))
        self.assertEqual(json.dumps(build_checkpoint(state)),json.dumps(build_checkpoint(reloaded)))

    def test_verified_legacy_order_migration_keeps_source_dates_and_advances_only_private_revision(self):
        remote_state={"pools":{"token":{"cohort":[{"owner":"holder","caught_at":"2026-09-02T00:00:00Z"}]}},
                      "_runtime":{"revision":3,"updated_at":"2026-10-05T12:00:00Z","schema_version":1}}
        raw=json.dumps(remote_state,separators=(",",":"),ensure_ascii=True).encode()
        remote={"schema_version":1,"encoding":"gzip+base64","sha256":hashlib.sha256(raw).hexdigest(),
                "decoded_bytes":len(raw),"data":base64.b64encode(gzip.compress(raw,mtime=0)).decode(),
                "runtime":remote_state["_runtime"]}
        local=json.loads(json.dumps(remote_state,sort_keys=True))
        with patch.object(scanner,"remote_api_call",return_value={"document":{"value":remote}}), \
             patch.object(scanner,"save_json") as write, \
             patch.object(scanner,"sync_runtime_checkpoint",return_value={"ok":True,"accepted":True}) as sync:
            result=recover_storage.recover_checkpoint(local,{},time.monotonic()+420)
        self.assertTrue(result["canonicalized"])
        saved=sync.call_args.args[0]
        self.assertEqual(saved["_runtime"]["revision"],4)
        self.assertEqual(saved["_runtime"]["updated_at"],remote_state["_runtime"]["updated_at"])
        self.assertEqual(saved["pools"],remote_state["pools"])
        write.assert_called_once()

    def test_same_version_changed_evidence_or_corruption_cannot_be_migrated(self):
        local={"pools":{"token":{"held":10}},"_runtime":{"revision":3,"updated_at":"2026-10-05T12:00:00Z"}}
        different=copy.deepcopy(local);different["pools"]["token"]["held"]=0
        for corrupt in (False,True):
            remote=build_checkpoint(different)
            if corrupt:
                remote["sha256"]="0"*64
            with patch.object(scanner,"remote_api_call",return_value={"document":{"value":remote}}), \
                 patch.object(scanner,"save_json") as write,patch.object(scanner,"sync_runtime_checkpoint") as sync:
                with self.assertRaises((RuntimeError,ValueError)):
                    recover_storage.recover_checkpoint(local,{},time.monotonic()+420)
            write.assert_not_called();sync.assert_not_called()
        self.assertEqual(local["pools"]["token"]["held"],10)

    def test_newer_verified_remote_state_is_restored_without_replacing_source_dates(self):
        remote_state={"pools":{"new":{"held":9,"caught_at":"2026-09-02T00:00:00Z"}},
                      "_runtime":{"revision":4,"updated_at":"2026-10-05T13:00:00Z"}}
        local={"_runtime":{"revision":3,"updated_at":"2026-10-05T12:00:00Z"},"wallet_cache":{"kept":1}}
        with patch.object(scanner,"remote_api_call",return_value={"document":{"value":build_checkpoint(remote_state)}}), \
             patch.object(scanner,"save_json"), \
             patch.object(scanner,"sync_runtime_checkpoint",return_value={"ok":True,"accepted":True}) as sync:
            result=recover_storage.recover_checkpoint(local,{},time.monotonic()+420)
        self.assertTrue(result["restored_newer"])
        self.assertFalse(result["canonicalized"])
        self.assertEqual(sync.call_args.args[0]["pools"],remote_state["pools"])
        self.assertEqual(sync.call_args.args[0]["wallet_cache"],{"kept":1})

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
