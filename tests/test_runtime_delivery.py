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
        self.assertEqual(thesis["signal_confirmation"], {"status": "confirmed", "reasons": []})
        self.assertNotIn("observations", audit)
        self.assertNotIn("members", thesis["coordinated_activity"]["signals"][0])
        self.assertEqual(json.loads(documents[0]["data"])["thesis"], original["detail_signal_theses"][0])
        self.assertEqual(body, original)
        self.assertLess(len(json.dumps(ready)), len(documents[0]["data"]) / 10)

    def test_repeated_explanations_stay_in_detail_not_in_the_list(self):
        body = self.body()
        thesis = body["detail_signal_theses"][0]
        thesis["coordinated_activity"]["limitations"] = ["explanation" * 500] * 20
        thesis["coordinated_activity"]["metrics"]["material_pattern"] = True
        thesis["wallet_activity"]["scope"] = "explanation" * 500
        thesis["signal_confirmation"]["reasons"] = ["explanation" * 500] * 20
        ready, documents = dashboard_documents(body)
        summary = ready["report"]["signal_theses"][0]
        self.assertTrue(summary["coordinated_activity"]["metrics"]["material_pattern"])
        self.assertNotIn("limitations", summary["coordinated_activity"])
        self.assertNotIn("scope", summary["wallet_activity"])
        self.assertEqual(len(summary["signal_confirmation"]["reasons"][0]), 240)
        self.assertEqual(json.loads(documents[0]["data"])["thesis"], thesis)

    def test_summary_is_saved_before_a_failed_detail_without_false_complete_ack(self):
        calls, config = [], {}
        def remote(method, path, cfg, payload, **kwargs):
            if path.endswith("dashboard-parts"):
                return {"ok": True, "parts": []}
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
            if path.endswith("dashboard-parts"):
                return {"ok": True, "parts": []}
            calls.append(payload)
            if len(calls) == 2:
                raise RuntimeError("Remote HTTP 503: storage_sql_http_error")
            return {"ok": True, "accepted": True}
        with patch.object(s, "remote_api_call", side_effect=remote), patch.object(s.time, "sleep"):
            self.assertTrue(s.publish_runtime_dashboard(self.body(), {})["accepted"])
        self.assertEqual(calls[1], calls[2])
        self.assertEqual(calls[-1]["runtime_snapshot_stage"], "complete")
        self.assertEqual(calls[-1]["revision"], 3)

    def test_verified_existing_parts_skip_upload_but_not_manifest_publication(self):
        _, documents = dashboard_documents(self.body())
        calls = []
        def remote(method, path, cfg, payload, **kwargs):
            calls.append((path, payload))
            if path.endswith("dashboard-parts"):
                return {"ok": True, "parts": [{"id": part["sha256"], "bytes": part["encoded_bytes"]}
                                             for part in documents]}
            return {"ok": True, "accepted": True}
        with patch.object(s, "remote_api_call", side_effect=remote):
            self.assertTrue(s.publish_runtime_dashboard(self.body(), {})["accepted"])
        self.assertFalse(any("detail" in body for _, body in calls))
        self.assertEqual(calls[-1][1]["runtime_snapshot_stage"], "complete")

    def test_failed_or_mismatched_probe_never_skips_part_upload(self):
        for failed in (True, False):
            calls = []
            def remote(method, path, cfg, payload, **kwargs):
                calls.append(payload)
                if path.endswith("dashboard-parts"):
                    if failed:
                        raise RuntimeError("probe unavailable")
                    return {"ok": True, "parts": [{"id": payload["ids"][0], "bytes": 1}]}
                return {"ok": True, "accepted": True}
            with self.subTest(failed=failed), patch.object(s, "remote_api_call", side_effect=remote):
                self.assertTrue(s.publish_runtime_dashboard(self.body(), {})["accepted"])
            self.assertTrue(any("detail" in body for body in calls))

    def test_list_only_has_market_rows_for_visible_tokens_and_preserves_full_market_detail(self):
        body = self.body()
        body["market"] = {"mint": {"mcap_usd": 42, "source": "actual"},
                          "unrelated": {"mcap_usd": 999}}
        ready, documents = dashboard_documents(body)
        self.assertEqual(ready["market"], {"mint": body["market"]["mint"]})
        self.assertEqual(json.loads(documents[0]["data"])["market"], body["market"]["mint"])
        self.assertIn("unrelated", body["market"])


if __name__ == "__main__":
    unittest.main()
