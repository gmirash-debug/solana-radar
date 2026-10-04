import copy
import json
import unittest
from unittest.mock import patch

import scanner as s
from runtime_dashboard import dashboard_documents


class RuntimeDeliveryTests(unittest.TestCase):
    def body(self):
        thesis = {"token_address": "mint", "status": "weakening", "token_retention_pct": 20,
                  "original_sale_history_status": "reconstructed_from_capture",
                  "signal_confirmation": {"status": "confirmed"},
                  "wallet_activity": {"status": "checked", "interpretation_complete": True,
                      "wallet_coverage_pct": 100, "token_coverage_pct": 100, "checked_at": "2026-10-04T20:00:00Z",
                      "amounts_tokens": {"sold": 10, "transferred": 2}, "observations": ["detail"] * 10000},
                  "outflow_evidence": {"balance_check_complete": True, "observed_sale_transactions": 3},
                  "coordinated_activity": {"metrics": {"market_rotation_observations": 2},
                      "signals": [{"kind": "rotation", "members": ["wallet"] * 10000,
                                   "detail": {"count": 4, "transactions": ["detail"] * 10000}}]}}
        return {"report": {"generated_at": "2026-10-04T20:00:00Z", "alerts": [], "signal_theses": [thesis]},
                "detail_signal_theses": [thesis], "history": [], "market": {}}

    def test_light_list_preserves_decision_facts_and_full_immutable_evidence(self):
        body = self.body()
        original = copy.deepcopy(body)
        ready, documents = dashboard_documents(body)
        thesis = ready["report"]["signal_theses"][0]
        audit = thesis["wallet_activity"]
        self.assertTrue(audit["interpretation_complete"])
        self.assertEqual(audit["token_coverage_pct"], 100)
        self.assertEqual(audit["amounts_tokens"], {"sold": 10, "transferred": 2})
        self.assertEqual(thesis["coordinated_activity"]["metrics"]["market_rotation_observations"], 2)
        self.assertEqual(thesis["signal_confirmation"], {"status": "confirmed"})
        self.assertNotIn("observations", audit)
        self.assertNotIn("members", thesis["coordinated_activity"]["signals"][0])
        self.assertEqual(json.loads(documents[0]["data"])["thesis"], original["detail_signal_theses"][0])
        self.assertEqual(body, original)
        self.assertLess(len(json.dumps(ready)), len(documents[0]["data"]) / 10)

    def test_summary_is_saved_before_a_failed_detail_without_false_complete_ack(self):
        calls, config = [], {}
        def remote(method, path, cfg, payload, **kwargs):
            calls.append(payload)
            if "detail" in payload:
                raise RuntimeError("invalid detail")
            return {"ok": True, "accepted": True}
        with patch.object(s, "remote_api_call", side_effect=remote):
            with self.assertRaisesRegex(RuntimeError, "invalid detail"):
                s.publish_runtime_dashboard(self.body(), config)
        self.assertEqual(calls[0]["runtime_snapshot_stage"], "summary")
        self.assertNotIn("token_detail_refs", calls[0])
        self.assertTrue(config["_runtime_dashboard_summary_saved"]["accepted"])
        self.assertFalse(any(call.get("runtime_snapshot_stage") == "complete" for call in calls))

    def test_only_idempotent_runtime_documents_retry_after_uncertain_sql_reply(self):
        calls = []
        def remote(method, path, cfg, payload, **kwargs):
            calls.append(payload)
            if len(calls) == 2:
                raise RuntimeError("Remote HTTP 503: storage_sql_http_error")
            return {"ok": True, "accepted": True}
        with patch.object(s, "remote_api_call", side_effect=remote), patch.object(s.time, "sleep"):
            self.assertTrue(s.publish_runtime_dashboard(self.body(), {})["accepted"])
        self.assertEqual(calls[1], calls[2])
        self.assertEqual(calls[-1]["runtime_snapshot_stage"], "complete")
        self.assertEqual(calls[-1]["revision"], 1)


if __name__ == "__main__":
    unittest.main()
