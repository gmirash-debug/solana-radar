import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from tools import migrate_storage as migration


ROOT = Path(__file__).resolve().parents[1]


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class FakeHrana:
    """Simulates the actual HTTP typed values and conditional Hrana transactions."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.payloads = []
        self.executed = []
        self.fault = None
        self.fault_table = "sample"

    def condition(self, condition, results, errors):
        if condition is None:
            return True
        kind = condition["type"]
        if kind == "ok":
            return results[condition["step"]] is not None
        if kind == "error":
            return errors[condition["step"]] is not None
        if kind == "not":
            return not self.condition(condition["cond"], results, errors)
        values = [self.condition(cond, results, errors) for cond in condition["conds"]]
        return all(values) if kind == "and" else any(values)

    def execute(self, stmt):
        sql = stmt["sql"]
        args = [migration.decode_value(arg) for arg in stmt.get("args", [])]
        self.executed.append((sql, args))
        cursor = self.db.execute(sql, args)
        names = [col[0] for col in cursor.description] if cursor.description else []
        rows = [[migration.encode_value(value) for value in row] for row in cursor.fetchall()] if names else []
        if not stmt.get("want_rows", True):
            rows = []
        return {"cols": [{"name": name} for name in names], "rows": rows,
                "affected_row_count": max(cursor.rowcount, 0)}

    def open(self, request, timeout):
        payload = json.loads(request.data)
        self.payloads.append(payload)
        batches = [entry for entry in payload["requests"] if entry["type"] == "batch"]
        is_data = bool(batches and any('INSERT INTO "' + self.fault_table + '"' in entry["stmt"]["sql"]
                                     for entry in batches[0]["batch"]["steps"]))
        fault = self.fault if is_data else None
        if fault:
            self.fault = None
        if fault == "disconnect_before":
            raise urllib.error.URLError("injected secret must not escape")
        responses = []
        for entry in payload["requests"]:
            if entry["type"] == "close":
                result = {"type": "close"}
            elif entry["type"] == "execute":
                result = {"type": "execute", "result": self.execute(entry["stmt"])}
            else:
                successes, errors = [], []
                for step in entry["batch"]["steps"]:
                    if not self.condition(step.get("condition"), successes, errors):
                        successes.append(None)
                        errors.append(None)
                        continue
                    try:
                        if fault == "statement_error" and 'INSERT INTO "' + self.fault_table + '"' in step["stmt"]["sql"]:
                            raise sqlite3.OperationalError("SQL body with a credential must never be logged")
                        successes.append(self.execute(step["stmt"]))
                        errors.append(None)
                    except sqlite3.Error:
                        successes.append(None)
                        errors.append({"message": "private SQL error", "code": "SQLITE_ERROR"})
                result = {"type": "batch", "result": {"step_results": successes, "step_errors": errors}}
                if fault == "truncated_response":
                    result["result"]["step_results"] = []
            responses.append({"type": "ok", "response": result})
        if fault == "disconnect_after_commit":
            raise urllib.error.URLError("response lost after commit")
        return Response(json.dumps({"results": responses}).encode())


class StorageMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.op, self.history = self.root / "op.sqlite", self.root / "history.sqlite"
        with sqlite3.connect(self.op) as db:
            db.executescript("""
                CREATE TABLE d1_migrations(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL);
                INSERT INTO d1_migrations(name) VALUES('0001_operational.sql');
                CREATE TABLE sample(id TEXT PRIMARY KEY, n INTEGER, real_value REAL,
                    payload_json TEXT, raw BLOB, status TEXT);
                CREATE INDEX idx_sample_status ON sample(status,id);
                CREATE TABLE composite(a TEXT NOT NULL,b INTEGER NOT NULL,data TEXT,
                    PRIMARY KEY(a,b)) WITHOUT ROWID;
            """)
            db.executemany("INSERT INTO sample VALUES(?,?,?,?,?,?)", [
                ("a", 2**60 + 1, 1.25, '{"unicode":"\\u0430","unchanged":true}', b"\0\xff", "pending"),
                ("b", None, None, "x" * 10_000, b"", "delivered"),
                ("c", -5, 0.0, "not JSON; preserve raw", None, "pending"),
            ])
            db.executemany("INSERT INTO composite VALUES(?,?,?)", [("a", 1, "one"), ("a", 2, "two"), ("b", 0, "three")])
        with sqlite3.connect(self.history) as db:
            db.executescript("""
                CREATE TABLE d1_migrations(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL);
                INSERT INTO d1_migrations(name) VALUES('0001_wallet_edge_history.sql');
                INSERT INTO d1_migrations(name) VALUES('0002_cluster_edge_evidence.sql');
            """)
            for name in ("0001_wallet_edge_history.sql", "0002_cluster_edge_evidence.sql"):
                db.executescript((ROOT / "cloudflare/scan-dispatcher/migrations-history" / name).read_text())
        self.sources = []
        self.addCleanup(self.close_sources)

    def close_sources(self):
        for source in self.sources:
            source.close()

    def plan(self):
        sources = [migration.Source("op", self.op), migration.Source("history", self.history)]
        self.sources.extend(sources)
        return migration.Plan(sources)

    def runner(self, plan, fake=None, manifest_name="progress.json", **kwargs):
        fake = fake or FakeHrana()
        self.addCleanup(fake.db.close)
        client = migration.Hrana("libsql://test-org.turso.io", "secret-token", opener=fake)
        manifest = migration.Manifest(self.root / manifest_name, plan.sha256, "target-hash", "cutover-test")
        self.addCleanup(manifest.close)
        return migration.Migrator(plan, client, manifest, "cutover-test", batch_rows=2, **kwargs), fake

    def test_dry_run_never_contacts_destination_or_modifies_sources(self):
        before = (migration.file_sha(self.op), migration.file_sha(self.history))
        output = io.StringIO()
        with redirect_stdout(output), patch.object(migration.Hrana, "__init__", side_effect=AssertionError("network")):
            self.assertEqual(migration.main(["--source-op", str(self.op), "--source-history", str(self.history)]), 0)
        report = json.loads(output.getvalue())
        self.assertFalse(report["destination_contacted"])
        self.assertEqual(report["raw_payload_policy"], "preserve_all")
        self.assertEqual(report["tables"]["sample"]["rows"], 3)
        self.assertGreaterEqual(report["tables"]["sample"]["payload_bytes"], 10_000)
        self.assertEqual(before, (migration.file_sha(self.op), migration.file_sha(self.history)))

    def test_local_build_preserves_every_row_and_separate_tracking_and_is_atomic(self):
        plan = self.plan()
        output = self.root / "merged.sqlite"
        before = (migration.file_sha(self.op), migration.file_sha(self.history))
        result = migration.build_local(plan, output, "cutover-test")
        self.assertTrue(result["history_0003_ready"])
        self.assertFalse(result["destination_contacted"])
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(output) as db:
            db.row_factory = sqlite3.Row
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sample WHERE status='pending'").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT n FROM sample WHERE id='a'").fetchone()[0], 2**60 + 1)
            self.assertEqual(db.execute("SELECT raw FROM sample WHERE id='a'").fetchone()[0], b"\0\xff")
            self.assertEqual(db.execute("SELECT name FROM ops_d1_migrations").fetchone()[0], "0001_operational.sql")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM d1_migrations").fetchone()[0], 3)
            self.assertTrue(result["legacy_history_0003_marker"])
            self.assertEqual(db.execute("SELECT status FROM _radar_import_lock").fetchone()[0], "verified")
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            runner = migration.Migrator(plan, migration.LocalDatabase(db), None, "cutover-test")
            runner.verify()
        self.assertEqual(before, (migration.file_sha(self.op), migration.file_sha(self.history)))
        original = migration.file_sha(output)
        with self.assertRaisesRegex(migration.MigrationError, "already exists"):
            migration.build_local(plan, output, "cutover-test")
        self.assertEqual(migration.file_sha(output), original)

    def test_local_build_failure_does_not_publish_partial_database(self):
        plan = self.plan()
        output = self.root / "must-not-exist.sqlite"
        with patch.object(migration.Migrator, "verify", side_effect=migration.MigrationError("mismatch")):
            with self.assertRaises(migration.MigrationError):
                migration.build_local(plan, output, "cutover-test")
        self.assertFalse(output.exists())
        self.assertFalse(list(self.root.glob(".radar-import-*")))

    def test_atomic_http_full_import_full_digest_and_pending_payloads(self):
        plan = self.plan()
        runner, fake = self.runner(plan)
        result = runner.run()
        self.assertTrue(result["history_0003_ready"])
        self.assertEqual(fake.db.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 3)
        self.assertEqual(fake.db.execute("SELECT n FROM sample WHERE id='a'").fetchone()[0], 2**60 + 1)
        self.assertEqual(fake.db.execute("SELECT status FROM _radar_import_lock").fetchone()[0], "verified")
        self.assertTrue(all("OFFSET" not in sql.upper() for sql, _ in fake.executed))
        for payload in fake.payloads:
            self.assertLessEqual(len(migration.compact(payload).encode()), migration.MAX_REQUEST_BYTES)
            batches = [entry for entry in payload["requests"] if entry["type"] == "batch"]
            for batch in batches:
                steps = batch["batch"]["steps"]
                self.assertEqual(steps[0]["stmt"]["sql"], "BEGIN IMMEDIATE")
                self.assertEqual(steps[-2]["stmt"]["sql"], "COMMIT")
                self.assertEqual(steps[-1]["stmt"]["sql"], "ROLLBACK")

    def test_disconnect_after_commit_reconciles_receipt_without_repeating_insert(self):
        runner, fake = self.runner(self.plan())
        fake.fault = "disconnect_after_commit"
        runner.run()
        inserts = [args for sql, args in fake.executed if sql.startswith('INSERT INTO "sample"')]
        self.assertEqual(len(inserts), 3)

    def test_truncated_write_response_is_reconciled_by_receipt_and_rows(self):
        runner, fake = self.runner(self.plan())
        fake.fault = "truncated_response"
        runner.run()
        self.assertEqual(fake.db.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 3)

    def test_disconnect_before_commit_stops_then_resumes_safely(self):
        plan = self.plan()
        runner, fake = self.runner(plan)
        fake.fault = "disconnect_before"
        with self.assertRaises(migration.UncertainWrite):
            runner.run()
        self.assertEqual(fake.db.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 0)
        runner.manifest.close()
        manifest = migration.Manifest(self.root / "progress.json", plan.sha256, "target-hash", "cutover-test")
        self.addCleanup(manifest.close)
        resumed = migration.Migrator(plan, runner.client, manifest, "cutover-test", batch_rows=2)
        resumed.run()
        self.assertEqual(fake.db.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 3)

    def test_statement_error_rolls_back_rows_and_receipt(self):
        runner, fake = self.runner(self.plan())
        fake.fault = "statement_error"
        with self.assertRaisesRegex(migration.MigrationError, "not fully committed"):
            runner.run()
        self.assertEqual(fake.db.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 0)
        self.assertEqual(fake.db.execute("SELECT COUNT(*) FROM _radar_import_pages WHERE table_name='sample'").fetchone()[0], 0)
        self.assertFalse(fake.db.in_transaction)

    def test_restart_rechecks_completed_table_instead_of_trusting_manifest(self):
        runner, fake = self.runner(self.plan())
        runner.run()
        fake.db.execute("UPDATE sample SET payload_json='newer live data' WHERE id='a'")
        before = len(fake.executed)
        with self.assertRaisesRegex(migration.MigrationError, "digest mismatch"):
            runner.run()
        self.assertEqual(fake.db.execute("SELECT payload_json FROM sample WHERE id='a'").fetchone()[0], "newer live data")
        self.assertFalse(any(sql.startswith('INSERT INTO "sample"') for sql, _ in fake.executed[before:]))

    def test_unreceipted_or_live_rows_are_never_replaced(self):
        runner, fake = self.runner(self.plan())
        runner.initialize()
        fake.db.execute("INSERT INTO sample(id,status) VALUES('a','newer')")
        with self.assertRaisesRegex(migration.MigrationError, "unreceipted"):
            runner.run()
        self.assertEqual(fake.db.execute("SELECT status FROM sample WHERE id='a'").fetchone()[0], "newer")

    def test_nonempty_target_and_wrong_lock_are_refused(self):
        runner, fake = self.runner(self.plan())
        fake.db.execute("CREATE TABLE live(id INTEGER PRIMARY KEY)")
        with self.assertRaisesRegex(migration.MigrationError, "not a fresh"):
            runner.run()
        fake.db.execute("DROP TABLE live")
        runner.initialize()
        fake.db.execute("UPDATE _radar_import_lock SET lock_id='other-import'")
        with self.assertRaisesRegex(migration.MigrationError, "does not match"):
            runner.run()

    def test_source_corrupt_database_is_refused(self):
        bad = self.root / "bad.sqlite"
        bad.write_bytes(b"not sqlite")
        with self.assertRaises(sqlite3.DatabaseError):
            migration.Source("op", bad)

    def test_source_mutation_and_wal_are_refused(self):
        plan = self.plan()
        with self.op.open("ab") as handle:
            handle.write(b"snapshot changed externally")
        with self.assertRaisesRegex(migration.MigrationError, "source changed"):
            plan.assert_sources()
        wal = Path(str(self.history) + "-wal")
        wal.write_bytes(b"unmerged snapshot")
        with self.assertRaisesRegex(migration.MigrationError, "WAL/journal"):
            migration.Source("history", self.history)

    def test_null_or_missing_primary_key_is_refused(self):
        for name, ddl in (("no-key", "CREATE TABLE bad(value TEXT)"),
                          ("null-key", "CREATE TABLE bad(id TEXT PRIMARY KEY); INSERT INTO bad VALUES(NULL)")):
            path = self.root / (name + ".sqlite")
            with sqlite3.connect(path) as db:
                db.executescript(ddl)
            with self.assertRaises(migration.MigrationError):
                migration.Source("op", path)

    def test_cross_source_index_collision_is_refused_before_destination(self):
        with sqlite3.connect(self.history) as db:
            db.executescript("CREATE INDEX idx_sample_status ON wallet_scores(confidence)")
        with self.assertRaisesRegex(migration.MigrationError, "collision"):
            self.plan()

    def test_cross_source_conflicting_table_is_refused(self):
        with sqlite3.connect(self.history) as db:
            db.executescript("CREATE TABLE sample(id TEXT PRIMARY KEY,other TEXT)")
        with self.assertRaisesRegex(migration.MigrationError, "conflicting table"):
            self.plan()

    def test_existing_0003_is_idempotent_and_marker_does_not_mask_missing_columns(self):
        with sqlite3.connect(self.history) as db:
            db.executescript((ROOT / "cloudflare/scan-dispatcher/migrations-history/0003_resumable_history.sql").read_text())
            db.execute("INSERT INTO d1_migrations(name) VALUES('0003_resumable_history.sql')")
        plan = self.plan()
        self.assertEqual(plan.added_columns, [])
        result = migration.build_local(plan, self.root / "already-v3.sqlite", "cutover-test")
        self.assertTrue(result["history_0003_ready"])

    def test_false_migration_marker_still_plans_missing_actual_columns(self):
        with sqlite3.connect(self.history) as db:
            db.execute("INSERT INTO d1_migrations(name) VALUES('0003_resumable_history.sql')")
        self.assertEqual(len(self.plan().added_columns), len(migration.ADDITIONS))

    def test_incompatible_existing_0003_column_is_refused(self):
        with sqlite3.connect(self.history) as db:
            db.execute("ALTER TABLE wallet_clusters ADD COLUMN active TEXT")
        with self.assertRaisesRegex(migration.MigrationError, "incompatible definition"):
            self.plan()

    def test_sqlite_handles_quoted_identifiers_and_generated_columns(self):
        with sqlite3.connect(self.op) as db:
            db.executescript('CREATE TABLE "strange table"("key" TEXT PRIMARY KEY,"a" INTEGER,"a;derived" INTEGER GENERATED ALWAYS AS (a+1)); INSERT INTO "strange table"("key",a) VALUES(\'k\',3);')
        plan = self.plan()
        result = migration.build_local(plan, self.root / "quoted.sqlite", "cutover-test")
        self.assertGreater(result["verified_rows"], 6)

    def test_canonical_digest_disambiguates_types_boundaries_blobs_and_large_integers(self):
        self.assertNotEqual(migration.rows_digest([(1,)]), migration.rows_digest([("1",)]))
        self.assertNotEqual(migration.rows_digest([(1.0,)]), migration.rows_digest([(1,)]))
        self.assertNotEqual(migration.rows_digest([("ab", "c")]), migration.rows_digest([("a", "bc")]))
        self.assertNotEqual(migration.rows_digest([(b"abc",)]), migration.rows_digest([("abc",)]))
        self.assertEqual(migration.decode_value(migration.encode_value(2**60 + 1)), 2**60 + 1)
        with self.assertRaises(migration.MigrationError):
            migration.row_bytes([float("inf")])

    def test_url_whitelist_redirect_refusal_and_credential_safe_errors(self):
        for url in ("http://test.turso.io", "https://test.turso.io.evil.test", "https://evil.test",
                    "https://user:pass@test.turso.io", "https://test.turso.io/?token=secret",
                    "libsql://test.turso.io:443", "libsql://test.turso.io/v2/pipeline"):
            with self.assertRaises(migration.MigrationError):
                migration.turso_url(url)
        self.assertEqual(migration.turso_url("libsql://db-org.aws-eu-west-1.turso.io"),
                         "https://db-org.aws-eu-west-1.turso.io/v2/pipeline")
        with self.assertRaises(migration.MigrationError):
            migration.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test")
        class Broken:
            def open(self, *_args, **_kwargs):
                raise urllib.error.URLError("secret-token and private SQL")
        client = migration.Hrana("libsql://db-org.turso.io", "secret-token", opener=Broken())
        with self.assertRaises(migration.MigrationError) as caught:
            client.query("SELECT 1")
        self.assertNotIn("secret-token", str(caught.exception))

    def test_manifest_lock_identity_and_private_permissions(self):
        plan = self.plan()
        path = self.root / "manifest.json"
        one = migration.Manifest(path, plan.sha256, "target", "lock-test")
        self.addCleanup(one.close)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(migration.MigrationError, "another migration"):
            migration.Manifest(path, plan.sha256, "target", "lock-test")
        one.close()
        with self.assertRaisesRegex(migration.MigrationError, "different sources"):
            migration.Manifest(path, "different", "target", "lock-test")

    def test_sql_schema_not_silently_dropping_views_or_triggers(self):
        with sqlite3.connect(self.op) as db:
            db.execute("CREATE VIEW view_sample AS SELECT * FROM sample")
        with self.assertRaisesRegex(migration.MigrationError, "views/triggers"):
            migration.Source("op", self.op)

    def test_cli_requires_explicit_write_intent_without_logging_tokens(self):
        output = io.StringIO()
        with redirect_stderr(output), patch.dict("os.environ", {"TURSO_AUTH_TOKEN": "secret-token"}):
            code = migration.main(["--source-op", str(self.op), "--source-history", str(self.history), "--execute"])
        self.assertEqual(code, 2)
        self.assertIn("writers-paused", output.getvalue())
        self.assertNotIn("secret-token", output.getvalue())

    def test_daily_learning_migration_is_in_schema_and_independent_receipt(self):
        plan = self.plan()
        output = self.root / "daily.sqlite"
        result = migration.build_local(plan, output, "cutover-test")
        self.assertTrue(result["storage_0001_ready"])
        with sqlite3.connect(output) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM history_maintenance_dirty").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM history_maintenance_jobs").fetchone()[0], 0)
            receipt = json.loads(db.execute("SELECT metadata_json FROM _radar_import_migrations WHERE name=?",
                                           [migration.STORAGE_MIGRATION_NAME]).fetchone()[0])
            self.assertEqual(receipt["file_sha256"], plan.storage_migration_sha256)
            self.assertEqual(receipt["plan_sha256"], plan.sha256)

    def test_preexisting_daily_learning_rows_are_preserved_not_reset(self):
        with sqlite3.connect(self.history) as db:
            db.executescript(migration.STORAGE_MIGRATION_PATH.read_text())
            db.execute("INSERT INTO history_maintenance_dirty VALUES('event','episode','2026-10-01','2026-10-02','2026-10-01')")
        plan = self.plan()
        output = self.root / "existing-daily.sqlite"
        migration.build_local(plan, output, "cutover-test")
        with sqlite3.connect(output) as db:
            self.assertEqual(db.execute("SELECT event_id FROM history_maintenance_dirty").fetchone()[0], "event")

    def test_offline_verify_cli_uses_no_network_and_no_manifest_writes(self):
        plan = self.plan()
        output = self.root / "export.sqlite"
        migration.build_local(plan, output, "cutover-test")
        manifest = self.root / "untouched.json"
        stdout = io.StringIO()
        with redirect_stdout(stdout), patch.object(migration.Hrana, "__init__", side_effect=AssertionError("network")):
            code = migration.main(["--source-op", str(self.op), "--source-history", str(self.history),
                                   "--verify-local", str(output), "--cutover-lock", "cutover-test",
                                   "--manifest", str(manifest)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["verification"]["storage_0001_ready"])
        self.assertFalse(manifest.exists())

    def test_offline_destination_row_mismatch_is_rejected_read_only(self):
        plan = self.plan()
        output = self.root / "modified-export.sqlite"
        migration.build_local(plan, output, "cutover-test")
        with sqlite3.connect(output) as db:
            db.execute("UPDATE sample SET n=999 WHERE id='a'")
        before = migration.file_sha(output)
        with self.assertRaisesRegex(migration.MigrationError, "digest mismatch"):
            migration.verify_local(plan, output, "cutover-test")
        self.assertEqual(migration.file_sha(output), before)

    def test_build_local_cli_saves_private_report_and_does_not_overwrite_it(self):
        output = self.root / "local-cli.sqlite"
        report = self.root / "build-report.json"
        stdout = io.StringIO()
        args = ["--source-op", str(self.op), "--source-history", str(self.history),
                "--build-local", str(output), "--manifest", str(report), "--cutover-lock", "cutover-test"]
        with redirect_stdout(stdout):
            self.assertEqual(migration.main(args), 0)
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        saved = json.loads(report.read_text())
        self.assertEqual(saved["verification"]["file_sha256"], migration.file_sha(output))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(migration.main(args), 2)
        self.assertEqual(json.loads(report.read_text()), saved)

    def test_batch_size_resume_changes_and_single_large_row_are_refused(self):
        with sqlite3.connect(self.history) as db:
            db.executescript("CREATE TABLE large_blob(id INTEGER PRIMARY KEY,payload TEXT); INSERT INTO large_blob VALUES(1,printf('%50000s','x')); ")
        plan = self.plan()
        runner, fake = self.runner(plan, batch_bytes=32768)
        with self.assertRaisesRegex(migration.MigrationError, "original batch"):
            migration.Migrator(plan, runner.client, runner.manifest, "cutover-test", batch_rows=3, batch_bytes=32768)
        with self.assertRaisesRegex(migration.MigrationError, "single source row"):
            list(runner.bounded_pages(plan.tables["large_blob"]))

    def test_primary_key_collation_preserves_rows_and_native_keyset_uses_index(self):
        with sqlite3.connect(self.op) as db:
            db.executescript("CREATE TABLE collated(k TEXT PRIMARY KEY COLLATE NOCASE); INSERT INTO collated VALUES('Z'),('a'),('b');")
        plan = self.plan()
        self.assertEqual([row[0] for page in plan.tables["collated"].pages(1) for row in page], ["a", "b", "Z"])
        table = plan.tables["composite"]
        sql, args = table.select(("a", 1))
        explanation = [tuple(row) for row in table.source.connection.execute("EXPLAIN QUERY PLAN " + sql, args)]
        self.assertTrue(any("SEARCH" in row[-1] for row in explanation))

    def test_page_receipt_mismatch_stops_before_rewrite(self):
        runner, fake = self.runner(self.plan())
        runner.initialize()
        rows = next(runner.bounded_pages(runner.plan.tables["sample"]))
        runner.client.atomic(runner.page_statements(runner.plan.tables["sample"], rows))
        fake.db.execute("UPDATE _radar_import_pages SET sha256='wrong' WHERE table_name='sample'")
        with self.assertRaisesRegex(migration.MigrationError, "receipt differs"):
            runner.run()
        self.assertEqual(fake.db.execute("SELECT COUNT(*) FROM sample").fetchone()[0], len(rows))

    def test_partial_0003_schema_adds_only_missing_columns(self):
        with sqlite3.connect(self.history) as db:
            db.execute("ALTER TABLE wallet_clusters ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        plan = self.plan()
        self.assertEqual(len(plan.added_columns), len(migration.ADDITIONS) - 1)
        migration.build_local(plan, self.root / "partial-0003.sqlite", "cutover-test")


if __name__ == "__main__":
    unittest.main()
