import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.prepare_storage_reset import build_seed, merge_ledger, report_ledger


class StorageResetTests(unittest.TestCase):
    def report(self, now):
        return {"generated_at": now.isoformat(), "stats": {"rpc_providers": {
            name: {"monthly_usage": {"provider": name, "period": "2026-10",
                                     "estimated_units": 400, "attempts": 20}}
            for name in ("helius", "alchemy", "chainstack")}}}

    def test_counter_merge_never_refunds(self):
        ledger = {"2026-10": {"helius": {"estimated_units": 600}}}
        merge_ledger(ledger, {"2026-10": {"helius": {"estimated_units": 400, "allocated_units": 800}}})
        self.assertEqual(ledger["2026-10"]["helius"]["estimated_units"], 600)
        self.assertEqual(ledger["2026-10"]["helius"]["allocated_units"], 800)

    def test_invalid_counter_and_missing_provider_fail_closed(self):
        for value in (-1, True, 3.5):
            with self.assertRaises(ValueError):
                merge_ledger({}, {"2026-10": {"helius": {"attempts": value}}})
        now = datetime(2026, 10, 7, tzinfo=timezone.utc)
        report = self.report(now)
        del report["stats"]["rpc_providers"]["alchemy"]
        with self.assertRaises(ValueError):
            report_ledger(report, now)

    def test_old_report_fails_closed(self):
        now = datetime(2026, 10, 7, tzinfo=timezone.utc)
        report = self.report(now)
        report["generated_at"] = "2026-10-05T00:00:00Z"
        with self.assertRaises(ValueError):
            report_ledger(report, now)

    def test_seed_is_empty_except_operational_state(self):
        now = datetime(2026, 10, 7, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory)/"old.sqlite", Path(directory)/"seed.sqlite"
            with sqlite3.connect(source) as old:
                old.executescript("""CREATE TABLE runtime_sql_documents(name TEXT,payload_json TEXT);
                    CREATE TABLE deleted_tokens(key TEXT PRIMARY KEY,kind TEXT,token_address TEXT,
                      pool_address TEXT,symbol TEXT,name TEXT,active INTEGER,deleted_at TEXT,
                      restored_at TEXT,updated_at TEXT);""")
                old.execute("INSERT INTO runtime_sql_documents VALUES (?,?)", ("rpc_ledger",
                            json.dumps({"ledger": {"2026-10": {"helius": {"estimated_units": 600}}}})))
                old.execute("INSERT INTO deleted_tokens VALUES ('mint','token','mint',NULL,NULL,NULL,1,'old',NULL,'old')")
            result = build_seed(source, output, "20261007-clean-v1", self.report(now), now)
            self.assertEqual(result["trading_rows"], 0)
            self.assertLess(result["bytes"], 1024*1024)
            with sqlite3.connect(output) as fresh:
                self.assertEqual(fresh.execute("SELECT COUNT(*) FROM signal_episodes").fetchone()[0], 0)
                self.assertEqual(fresh.execute("SELECT COUNT(*) FROM history_sql_queue_events").fetchone()[0], 0)
                self.assertEqual(fresh.execute("SELECT legacy_complete FROM history_sql_queue_meta").fetchone()[0], 1)
                self.assertEqual(fresh.execute("SELECT COUNT(*) FROM deleted_tokens").fetchone()[0], 1)
                ledger = json.loads(fresh.execute("SELECT payload_json FROM runtime_sql_documents WHERE name='rpc_ledger'").fetchone()[0])
                self.assertEqual(ledger["ledger"]["2026-10"]["helius"]["estimated_units"], 600)
            with self.assertRaises(ValueError):
                build_seed(source, output, "20261007-clean-v1", self.report(now), now)
