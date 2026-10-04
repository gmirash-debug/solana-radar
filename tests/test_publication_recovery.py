import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import recover_runtime_publication as recovery


class PublicationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def save(self, stamp="2026-10-04T20:10:36Z", **fields):
        path = self.directory / (recovery.re.sub(r"[^0-9]", "", stamp) + ".json.gz")
        path.write_bytes(gzip.compress(json.dumps({"report": {"generated_at": stamp}, **fields}).encode()))
        return path

    def test_latest_source_is_replayed_without_mutating_or_deleting_files(self):
        self.save("2026-10-03T20:10:36Z")
        latest = self.save(detail_history=[{"evidence": "original"}])
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        calls = []
        def publish(body, config):
            calls.append(body)
            return {"ok": True, "accepted": True}
        result = recovery.recover(self.directory, publish)
        self.assertEqual(result["source_generated_at"], "2026-10-04T20:10:36Z")
        self.assertEqual(calls[0]["detail_history"], [{"evidence": "original"}])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory.iterdir()})
        self.assertTrue(latest.exists())

    def test_invalid_latest_fails_closed_instead_of_replaying_older_source(self):
        self.save("2026-10-03T20:10:36Z")
        self.save().write_bytes(gzip.compress(b"{}"))
        with self.assertRaisesRegex(ValueError, "invalid saved"):
            recovery.recover(self.directory, lambda *args: self.fail("must not publish"))

    def test_empty_and_quarantined_sources_are_not_published(self):
        (self.directory / "quarantine").mkdir()
        (self.directory / "quarantine" / "20261004201036.json.gz").write_bytes(b"bad")
        with self.assertRaisesRegex(ValueError, "no saved"):
            recovery.saved_dashboard(self.directory)

    def test_unacknowledged_publication_is_not_success(self):
        self.save()
        with self.assertRaisesRegex(RuntimeError, "not acknowledged"):
            recovery.recover(self.directory, lambda *args: {"ok": True, "accepted": False})

    def test_compressed_and_decoded_limits_fail_before_publication(self):
        self.save()
        for name in ("MAX_COMPRESSED_BYTES", "MAX_DECODED_BYTES"):
            with self.subTest(name=name), patch.object(recovery, name, 1):
                with self.assertRaisesRegex(ValueError, "exceeds"):
                    recovery.saved_dashboard(self.directory)

    def test_filename_timestamp_mismatch_fails_closed(self):
        path = self.save()
        path.rename(self.directory / "20261004201037.json.gz")
        with self.assertRaisesRegex(ValueError, "timestamp mismatch"):
            recovery.saved_dashboard(self.directory)


if __name__ == "__main__":
    unittest.main()
