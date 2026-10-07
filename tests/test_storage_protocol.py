import copy
import unittest
from unittest.mock import patch
import scanner

from scanner import compact_state
from storage_generation import storage_epoch


class StorageProtocolTests(unittest.TestCase):
    def test_cli_maintenance_cannot_start_an_rpc_writer(self):
        with patch("sys.argv",["scanner.py","--once"]), \
             patch.object(scanner,"load_json",return_value={"storage_maintenance":True}), \
             patch.object(scanner,"run_once") as run, \
             patch.object(scanner,"write_scanner_status") as status:
            scanner.main()
        run.assert_not_called()
        status.assert_not_called()

    def state(self, status):
        return {"pools": {"pool": {"last_scanned_at": "2026-01-01T00:00:00Z",
                    "signal_thesis": {"token_address": "mint", "status": status,
                                      "updated_at": "2026-01-01T00:00:00Z"}}},
                "market": {"mint": {"pool_address": "pool", "latest_seen_at": "2026-01-01T00:00:00Z"}}}

    def test_active_or_unresolved_cohort_never_expires_by_age(self):
        for status in ("intact", "weakening", "unknown", "recheck_due"):
            with self.subTest(status=status):
                state = self.state(status)
                compact_state(state, [], [], {}, "2026-10-07T00:00:00Z")
                self.assertIn("pool", state["pools"])
                self.assertIn("mint", state["market"])

    def test_terminal_thesis_can_expire(self):
        state = self.state("invalidated")
        compact_state(state, [], [], {}, "2026-10-07T00:00:00Z")
        self.assertNotIn("pool", state["pools"])

    def test_epoch_default_and_environment_override(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(storage_epoch(), "20261007-clean-v1")
            self.assertEqual(storage_epoch({"storage_epoch": "next-generation"}), "next-generation")
        with patch.dict("os.environ", {"RADAR_STORAGE_EPOCH": "manual-generation"}):
            self.assertEqual(storage_epoch(), "manual-generation")
        with patch.dict("os.environ", {"RADAR_STORAGE_EPOCH": "bad/epoch"}):
            with self.assertRaises(ValueError):
                storage_epoch()
