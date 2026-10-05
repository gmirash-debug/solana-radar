"""Credential-free, stdlib contract tests for the private database backup."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import gzip
import http.client
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import ssl
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
import zlib


spec = importlib.util.spec_from_file_location("storage_backup", Path(__file__).resolve().parents[1] / "tools/storage_backup.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
URL = "libsql://unit-test.turso.io"
TOKEN = "secret-never-log-this"


class Response:
    def __init__(self, data, status=200):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.status = status

    def read(self, bound):
        return self.data[:bound]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class FakeHrana:
    def __init__(self, connection):
        self.db = connection
        self.calls = []
        self.baton = None
        self.hook = None
        self.base_url = None
        self.mutate_response = None

    def open(self, request, timeout):
        payload = json.loads(request.data)
        self.calls.append((request.full_url, payload, timeout))
        assert request.get_header("Authorization") == "Bearer " + TOKEN
        assert payload["baton"] == self.baton
        if self.hook:
            self.hook(payload)
        results = []
        for action in payload["requests"]:
            kind = action["type"]
            response = {"type":kind}
            if kind == "execute":
                stmt = action["stmt"]
                try:
                    cursor = self.db.execute(stmt["sql"], [backup.decode(value) for value in stmt["args"]])
                    response["result"] = {"cols":[{"name":col[0]} for col in cursor.description or []],
                        "rows":[[backup.encode(cell) for cell in row] for row in cursor.fetchall()],
                        "affected_row_count":0, "last_insert_rowid":None}
                except sqlite3.Error:
                    results.append({"type":"error", "error":{"message":TOKEN, "code":"SQL_ERROR"}})
                    continue
            elif kind == "get_autocommit":
                response["is_autocommit"] = not self.db.in_transaction
            elif kind == "close":
                if self.db.in_transaction:
                    self.db.rollback()
            else:
                raise AssertionError(kind)
            results.append({"type":"ok", "response":response})
        self.baton = None if payload["requests"][-1]["type"] == "close" else "baton-" + str(len(self.calls))
        result = {"baton":self.baton, "base_url":self.base_url, "results":results}
        if self.mutate_response:
            result = self.mutate_response(result)
        return Response(result)


def fill(connection):
    connection.executescript("""
        CREATE TABLE parent(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, data BLOB, n REAL);
        INSERT INTO parent(id,text,data,n) VALUES(2,'text',X'000102ff',1.25),(7,NULL,NULL,-3.5);
        INSERT INTO parent(id,text) VALUES(100,'removed');
        DELETE FROM parent WHERE id=100;
        CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id), value TEXT,
            computed TEXT GENERATED ALWAYS AS (value || '!') STORED,
            virtual_value TEXT GENERATED ALWAYS AS (upper(value)) VIRTUAL);
        INSERT INTO child VALUES(3,2,'v');
        CREATE INDEX child_value ON child(value) WHERE value IS NOT NULL;
        CREATE VIEW child_view AS SELECT value FROM child;
        CREATE TABLE audit(message TEXT);
        CREATE TRIGGER child_audit AFTER INSERT ON child BEGIN INSERT INTO audit VALUES(new.value); END;
        CREATE TABLE compound(a TEXT COLLATE NOCASE,b INTEGER,payload BLOB,PRIMARY KEY(a,b)) WITHOUT ROWID;
        INSERT INTO compound VALUES('a',1,X'ff'),('A',2,X'00'),('b',1,X'10');
        CREATE TABLE sparse(text TEXT);
        INSERT INTO sparse(rowid,text) VALUES(-7,'old'),(2,'a'),(90,'b');
        CREATE TABLE typed_values(i INTEGER,t TEXT,b BLOB,f REAL);
        INSERT INTO typed_values VALUES(9223372036854775807,'unicode ' || char(233) || char(0),X'',-0.125);
        ANALYZE;
    """)
    connection.commit()


class PersistentTransportTests(unittest.TestCase):
    def fake_factory(self,status=200,failure=None):
        created=[]
        class Connection:
            def __init__(self,host,timeout,context):
                self.host,self.timeout,self.context=host,timeout,context
                self.sock=Mock();self.calls=[];self.closed=0
                created.append(self)
            def request(self,*args,**kwargs):
                self.calls.append((args,kwargs))
                if failure:
                    raise failure
            def getresponse(self):
                reply=Response(b"{}",status)
                reply.headers={"Location":"https://outside.invalid"}
                reply.close=Mock()
                return reply
            def close(self):
                self.closed+=1
        return Connection,created

    def request(self,host="unit-test.turso.io"):
        return urllib.request.Request("https://"+host+"/v3/pipeline",b"{}",{"Authorization":"Bearer "+TOKEN})

    def test_reuses_verified_tls_connection_and_closes_it_on_sticky_host_change(self):
        factory,created=self.fake_factory();context=object()
        with patch.object(backup.http.client,"HTTPSConnection",factory), \
             patch.object(backup.ssl,"create_default_context",return_value=context):
            transport=backup.PersistentHTTPS()
            transport.open(self.request(),20).read(100)
            transport.open(self.request(),5).read(100)
            self.assertEqual(len(created),1)
            self.assertIs(created[0].context,context)
            self.assertEqual(created[0].sock.settimeout.call_args.args,(5,))
            self.assertEqual(created[0].calls[0][1]["headers"]["Authorization"],"Bearer "+TOKEN)
            transport.open(self.request("unit-test.eu.turso.io"),3).read(100)
            self.assertEqual(len(created),2);self.assertEqual(created[0].closed,1)
            transport.close();self.assertEqual(created[1].closed,1)

    def test_redirect_and_auth_failure_never_forward_or_retry(self):
        for status,kind in ((302,backup.BackupError),(401,urllib.error.HTTPError)):
            factory,created=self.fake_factory(status)
            with patch.object(backup.http.client,"HTTPSConnection",factory):
                transport=backup.PersistentHTTPS()
                with self.assertRaises(kind) as error:
                    transport.open(self.request(),20)
                self.assertNotIn(TOKEN,str(error.exception))
                self.assertEqual(len(created),1)
                self.assertEqual(len(created[0].calls),1)
                self.assertEqual(created[0].closed,1)

    def test_network_failure_closes_connection_without_resuming_the_snapshot(self):
        factory,created=self.fake_factory(failure=TimeoutError(TOKEN))
        with patch.object(backup.http.client,"HTTPSConnection",factory):
            client=backup.ReadSnapshot(URL,TOKEN)
            with self.assertRaises(backup.BackupError) as error:
                client.begin()
            self.assertNotIn(TOKEN,str(error.exception));self.assertTrue(client.broken)
            with self.assertRaises(backup.BackupError):
                client.begin()
            client.close()
        self.assertEqual(len(created),1);self.assertEqual(created[0].closed,1)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.path = Path(self.root.name)
        self.source = sqlite3.connect(self.path / "source.sqlite")
        fill(self.source)
        self.server = FakeHrana(self.source)

    def tearDown(self):
        self.source.close()
        self.root.cleanup()

    def client(self, **kwargs):
        return backup.ReadSnapshot(URL, TOKEN, opener=self.server, **kwargs)

    def test_full_consistent_snapshot_preserves_schema_values_and_hidden_rowids(self):
        expected = backup.build_snapshot(self.client(), self.path / "copy.sqlite", page_size=2)
        self.assertEqual(expected, backup.sqlite_fingerprints(self.path / "source.sqlite", page_size=3))
        restored = sqlite3.connect(self.path / "copy.sqlite")
        self.addCleanup(restored.close)
        self.assertEqual(restored.execute("SELECT rowid,text FROM sparse ORDER BY rowid").fetchall(), [(-7,"old"),(2,"a"),(90,"b")])
        self.assertEqual(restored.execute("SELECT * FROM sqlite_sequence").fetchall(), [("parent",100)])
        self.assertEqual(restored.execute("SELECT computed,virtual_value FROM child").fetchall(), [("v!","V")])
        restored.execute("INSERT INTO child(id,parent_id,value) VALUES(4,2,'after')")
        self.assertEqual(restored.execute("SELECT message FROM audit").fetchall(), [("after",)])
        self.assertEqual((self.path / "copy.sqlite").stat().st_mode & 0o777, 0o600)
        self.assertIsNone(self.server.baton)
        self.assertTrue(all(timeout <= 20 for _, _, timeout in self.server.calls))

    def test_concurrent_writes_cannot_change_read_transaction_snapshot(self):
        self.source.execute("PRAGMA journal_mode=WAL")
        original = backup.sqlite_fingerprints(self.path / "source.sqlite")
        writer = sqlite3.connect(self.path / "source.sqlite")
        self.addCleanup(writer.close)
        wrote = False
        def write_after_begin(payload):
            nonlocal wrote
            if self.source.in_transaction and not wrote:
                writer.execute("INSERT INTO sparse(rowid,text) VALUES(999,'new after snapshot')")
                writer.commit()
                wrote = True
        self.server.hook = write_after_begin
        actual = backup.build_snapshot(self.client(), self.path / "copy.sqlite", page_size=1)
        self.assertTrue(wrote)
        self.assertEqual(actual, original)
        self.assertNotEqual(actual, backup.sqlite_fingerprints(self.path / "source.sqlite"))

    def test_lost_transaction_fails_instead_of_reading_a_new_snapshot(self):
        client = self.client()
        client.begin()
        self.source.rollback()
        with self.assertRaisesRegex(backup.BackupError, "expired"):
            client.query("SELECT * FROM parent")
        before = len(self.server.calls)
        with self.assertRaises(backup.BackupError):
            client.query("SELECT * FROM parent")
        self.assertEqual(len(self.server.calls), before)

    def test_sql_error_discards_partial_copy_and_is_not_replayed(self):
        original = self.server.open
        calls = 0
        def bad_query(request, timeout):
            nonlocal calls
            calls += 1
            if calls == 3:
                return Response({"baton":"b", "results":[{"type":"error", "error":{"message":TOKEN}}] * 3})
            return original(request, timeout)
        self.server.open = bad_query
        with self.assertRaises(backup.BackupError) as error:
            backup.build_snapshot(self.client(), self.path / "bad.sqlite")
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertEqual(calls, 3)
        self.assertFalse((self.path / "bad.sqlite").exists())

    def test_remote_mutations_are_refused(self):
        client = self.client()
        client.begin()
        before = len(self.server.calls)
        for sql in ("DELETE FROM parent", "PRAGMA user_version=7", "BEGIN", "INSERT INTO parent VALUES(1)"):
            with self.assertRaises(backup.BackupError):
                client.query(sql)
        self.assertEqual(len(self.server.calls), before)
        client.close()

    def test_small_page_prefetch_halves_round_trips_without_changing_rows(self):
        self.source.execute("CREATE TABLE narrow_prefetch(value TEXT)")
        self.source.executemany("INSERT INTO narrow_prefetch VALUES(?)",[("small",)]*2500)
        self.source.commit()
        client=self.client();schema=client.begin()
        plan=backup.TablePlan(client,next(row for row in schema if row["name"]=="narrow_prefetch"))
        before=len(self.server.calls)
        pages=list(plan.bounded_pages(client,1000,client.deadline))
        self.assertEqual([len(page) for page in pages],[1000,1000,500])
        self.assertEqual(len(self.server.calls)-before,4)
        self.assertEqual(backup.rows_digest(row for page in pages for row in page),
            backup.rows_digest(self.source.execute("SELECT rowid,value FROM narrow_prefetch ORDER BY rowid")))
        client.close()

    def test_batched_reads_keep_every_autocommit_guard_and_refuse_mutations(self):
        client=self.client();client.begin()
        before=len(self.server.calls)
        for queries in ([],[("SELECT 1",())]*3,[("SELECT 1",()),("DELETE FROM parent",())]):
            with self.assertRaises(backup.BackupError):
                client.queries(queries)
        self.assertEqual(len(self.server.calls),before)
        rows=client.queries([("SELECT 1",()),("SELECT 2",())])
        self.assertEqual([result[1] for result in rows],[[(1,)],[(2,)]])
        self.assertEqual(len(self.server.calls)-before,1)
        actions=self.server.calls[-1][1]["requests"]
        self.assertEqual([item["type"] for item in actions],
            ["get_autocommit","execute","get_autocommit"]*2)
        def expire(result):
            result["results"][3]["response"]["is_autocommit"]=True
            return result
        self.server.mutate_response=expire
        with self.assertRaisesRegex(backup.BackupError,"transaction expired"):
            client.queries([("SELECT 1",()),("SELECT 2",())])
        self.assertTrue(client.broken)

    def test_failed_prefetched_query_discards_entire_snapshot(self):
        def fail_prefetch(result):
            if len(result["results"])==6:
                result["results"][4]={"type":"error","error":{"message":TOKEN}}
            return result
        self.server.mutate_response=fail_prefetch
        with self.assertRaises(backup.BackupError) as error:
            backup.build_snapshot(self.client(),self.path/"incomplete.sqlite")
        self.assertNotIn(TOKEN,str(error.exception))
        self.assertFalse((self.path/"incomplete.sqlite").exists())

    def test_adaptive_page_bound_for_large_raw_records(self):
        self.source.execute("CREATE TABLE large_records(raw TEXT)")
        self.source.executemany("INSERT INTO large_records VALUES(?)", [("\\\"" * 65000,)] * 20)
        self.source.commit()
        with patch.object(backup, "MAX_PAGE_BYTES", 1024 * 1024):
            backup.build_snapshot(self.client(), self.path / "copy.sqlite")
        requests = [action["stmt"] for _, payload, _ in self.server.calls for action in payload["requests"] if action["type"] == "execute"]
        limits = [backup.decode(stmt["args"][-1]) for stmt in requests if 'FROM "large_records"' in stmt["sql"]
                  and "LIMIT ?" in stmt["sql"] and "__backup_wire_bytes" not in stmt["sql"]]
        self.assertTrue(limits)
        self.assertLess(max(limits), 10)

    def test_single_legacy_outlier_does_not_shrink_entire_table(self):
        self.source.execute("CREATE TABLE mixed_records(raw TEXT)")
        self.source.execute("INSERT INTO mixed_records VALUES(?)", ("large" * 300000,))
        self.source.executemany("INSERT INTO mixed_records VALUES(?)", [("ordinary" * 1625,)] * 600)
        self.source.commit()
        expected = backup.build_snapshot(self.client(), self.path / "copy.sqlite")
        requests = [action["stmt"] for _, payload, _ in self.server.calls for action in payload["requests"] if action["type"] == "execute"]
        pages = [stmt for stmt in requests if 'FROM "mixed_records"' in stmt["sql"] and "LIMIT ?" in stmt["sql"]]
        payloads = [stmt for stmt in pages if "__backup_wire_bytes" not in stmt["sql"]]
        self.assertLessEqual(len(pages), 5)
        self.assertGreater(max(backup.decode(stmt["args"][-1]) for stmt in payloads), 400)
        self.assertTrue(all('OFFSET' not in stmt['sql'] for stmt in requests))
        self.assertEqual(expected["tables"]["mixed_records"]["row_count"], 601)
        metrics = [stmt for stmt in requests if stmt["sql"].startswith("SELECT COUNT(*)")]
        self.assertFalse(any("json_quote" in stmt["sql"] or "MAX(" in stmt["sql"] for stmt in metrics))

    def test_narrow_table_keeps_thousand_row_payloads_with_bounded_metadata(self):
        self.source.execute("CREATE TABLE narrow(value TEXT)")
        self.source.executemany("INSERT INTO narrow VALUES(?)", [("small",)] * 2500)
        self.source.commit()
        backup.build_snapshot(self.client(), self.path / "copy.sqlite")
        statements = [action["stmt"] for _, payload, _ in self.server.calls for action in payload["requests"] if action["type"] == "execute"]
        pages = [stmt for stmt in statements if 'FROM "narrow"' in stmt["sql"] and "LIMIT ?" in stmt["sql"]]
        payloads = [stmt for stmt in pages if "__backup_wire_bytes" not in stmt["sql"]]
        metadata = [stmt for stmt in pages if "__backup_wire_bytes" in stmt["sql"]]
        self.assertEqual(len(payloads), 3)
        self.assertEqual(len(metadata), 4)
        self.assertEqual(backup.decode(payloads[0]["args"][-1]), 1000)

    def test_metadata_payload_keys_must_match(self):
        self.source.execute("CREATE TABLE mixed_records(raw TEXT)")
        self.source.executemany("INSERT INTO mixed_records VALUES(?)", [("x" * 10000,)] * 5)
        self.source.commit()
        original = self.server.open
        def wrong_keys(request, timeout):
            response = original(request, timeout)
            payload = json.loads(request.data)
            parsed = json.loads(response.data)
            for action, result in zip(payload["requests"], parsed["results"]):
                if action["type"] == "execute" and 'FROM "mixed_records"' in action["stmt"]["sql"] and '<=' in action["stmt"]["sql"]:
                    result["response"]["result"]["rows"][0][0] = backup.encode(999)
            return Response(parsed)
        self.server.open = wrong_keys
        with patch.object(backup, "MAX_PAGE_BYTES", 12000), self.assertRaisesRegex(backup.BackupError, "byte-budgeted keys"):
            backup.build_snapshot(self.client(), self.path / "bad.sqlite")
        self.assertFalse((self.path / "bad.sqlite").exists())

    def test_byte_budget_matches_ascii_unicode_blob_and_compound_keys(self):
        reader = backup.LocalReader(self.source)
        schema = backup.dictionaries(reader, backup.SCHEMA_SQL)
        for record in schema:
            if record["type"] != "table":
                continue
            plan = backup.TablePlan(reader, record)
            for page in plan.bounded_pages(reader, 2, backup.Deadline()):
                keys = tuple(page[0][i] for i in plan.key_indices)
                sql,args = plan.select(None,1000, through=keys, projection=plan.wire_bytes_sql())
                estimates = reader.query(sql,args)[1]
                self.assertGreaterEqual(estimates[-1][0], len(json.dumps([backup.encode(v) for v in page[0]]).encode()))
            full = backup.fingerprint_table(reader, plan, 2, backup.Deadline())
            limited = backup.rows_digest(row for page in plan.bounded_pages(reader, 2, backup.Deadline()) for row in page)
            self.assertEqual(full["row_sha256"], limited)

    def test_row_count_mismatch_is_detected(self):
        original = self.server.open
        def wrong_count(request, timeout):
            response = original(request, timeout)
            payload = json.loads(request.data)
            parsed = json.loads(response.data)
            for action, result in zip(payload["requests"], parsed["results"]):
                if action["type"] == "execute" and action["stmt"]["sql"].startswith("SELECT COUNT(*)"):
                    result["response"]["result"]["rows"][0][0] = backup.encode(9999)
            return Response(parsed)
        self.server.open = wrong_count
        with self.assertRaisesRegex(backup.BackupError, "row count"):
            backup.build_snapshot(self.client(), self.path / "bad.sqlite")
        self.assertFalse((self.path / "bad.sqlite").exists())

    def test_backup_archive_is_restore_verified_and_private(self):
        archive, manifest_path, manifest = backup.create_backup(URL, TOKEN, self.path / "out", opener=self.server, page_size=3)
        self.assertTrue(backup.verify_backup(archive, manifest_path))
        self.assertFalse((archive.parent / "radar.sqlite").exists())
        self.assertEqual(manifest["consistency"], "single_read_transaction")
        self.assertEqual(manifest["archive_bytes"], archive.stat().st_size)
        self.assertEqual(manifest["export_stats"]["http_requests"], len(self.server.calls))
        self.assertGreater(manifest["export_stats"]["response_bytes"], 0)
        self.assertNotIn(TOKEN, manifest_path.read_text())
        self.assertNotIn("unit-test.turso.io", manifest_path.read_text())
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(archive.parent.stat().st_mode & 0o777, 0o700)
        manifest["fingerprints"]["tables"]["parent"]["row_count"] += 1
        with self.assertRaisesRegex(backup.BackupError, "digest"):
            backup.verify_backup(archive, manifest)

    def test_corrupted_archive_and_size_bombs_fail_verification(self):
        archive, _, manifest = backup.create_backup(URL, TOKEN, self.path / "out", opener=self.server)
        bad = {**manifest, "sqlite_bytes":10}
        with self.assertRaisesRegex(backup.BackupError, "size limit"):
            backup.verify_backup(archive, bad)
        with archive.open("ab") as handle:
            handle.write(b"tamper")
        with self.assertRaisesRegex(backup.BackupError, "SHA-256"):
            backup.verify_backup(archive, manifest)

    def test_malformed_manifest_is_bounded_and_rejected(self):
        archive, manifest_path, manifest = backup.create_backup(URL, TOKEN, self.path / "out", opener=self.server)
        for invalid in ([], {**manifest, "sqlite_bytes":"621MB"}, {**manifest, "consistency":"multiple_reads"},
                        {**manifest, "fingerprints":None}):
            manifest_path.write_text(json.dumps(invalid))
            with self.assertRaises(backup.BackupError):
                backup.verify_backup(archive, manifest_path)
        manifest_path.write_bytes(b"x" * 100)
        with patch.object(backup, "MAX_SCHEMA_BYTES", 50), self.assertRaises(backup.BackupError):
            backup.verify_backup(archive, manifest_path)

    def test_destination_never_overwritten(self):
        destination = self.path / "existing.sqlite"
        destination.write_bytes(b"keep")
        with self.assertRaises(backup.BackupError):
            backup.build_snapshot(self.client(), destination)
        self.assertEqual(destination.read_bytes(), b"keep")
        self.assertEqual(self.server.calls, [])

    def test_no_double_begin(self):
        client = self.client()
        client.begin()
        with self.assertRaises(backup.BackupError):
            client.begin()
        client.close()

    def test_virtual_tables_fail_closed(self):
        self.source.execute("CREATE VIRTUAL TABLE v USING fts5(text)")
        self.source.commit()
        with self.assertRaises(backup.BackupError):
            backup.build_snapshot(self.client(), self.path / "bad.sqlite")
        self.assertFalse((self.path / "bad.sqlite").exists())


class ProtocolTests(unittest.TestCase):
    def client(self, result):
        class Opener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                if isinstance(result, Exception):
                    raise result
                return Response(result)
        opener = Opener()
        return backup.ReadSnapshot(URL, TOKEN, opener=opener), opener

    def test_url_credentials_and_non_turso_hosts_refused(self):
        for url in (None, "http://unit-test.turso.io", "https://evil.example", "https://turso.io", "https://unit-test.turso.io:443", "https://u:secret@unit-test.turso.io", "https://unit-test.turso.io?token="+TOKEN, "https://unit-test.turso.io/v3/pipeline", "https://unit-test.turso.io/#secret", "https://unit-test.turso.io\n"):
            with self.subTest(url=url), self.assertRaises(backup.BackupError):
                backup.turso_endpoint(url)
        self.assertEqual(backup.turso_endpoint(URL), "https://unit-test.turso.io/v3/pipeline")

    def test_malformed_and_auth_network_http_errors_are_secret_safe_and_not_retried(self):
        variants = [b"bad-json", [], {"baton":"b", "results":[]},
            {"baton":"b", "results":[None]}, urllib.error.URLError(TOKEN),
            urllib.error.HTTPError("https://secret/"+TOKEN, 401, TOKEN, {}, None)]
        for result in variants:
            with self.subTest(result=type(result).__name__):
                client, opener = self.client(result)
                with self.assertRaises(backup.BackupError) as error:
                    client.begin()
                self.assertNotIn(TOKEN, str(error.exception))
                self.assertEqual(opener.calls, 1)
                with self.assertRaises(backup.BackupError):
                    client.begin()
                self.assertEqual(opener.calls, 1)

    def test_bad_typed_values_and_nonfinite_or_unsafe_ints(self):
        for value in ({"type":"integer","value":"9223372036854775808"}, {"type":"integer","value":1}, {"type":"float","value":float("inf")}, {"type":"blob","base64":"@@"}, {"type":"text","value":[]}, {"type":"unknown"}, None):
            with self.subTest(value=value), self.assertRaises(backup.BackupError):
                backup.decode(value)
        for value in (None, -(1<<63), (1<<63)-1, -0.125, "a\0unicode \u00e9", b"\x00\xff"):
            self.assertEqual(backup.decode(backup.encode(value)), value)

    def test_redirect_credentials_never_forwarded(self):
        with self.assertRaises(backup.BackupError):
            backup.NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_sticky_url_cannot_exfiltrate_credentials(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE x(a)")
        for base in ("https://evil.example", "https://other.turso.io", "https://unit-test.turso.io?key=x", "https://u:pw@unit-test.turso.io", "https://unit-test.turso.io/v3/pipeline"):
            server = FakeHrana(db)
            server.base_url = base
            client = backup.ReadSnapshot(URL, TOKEN, opener=server)
            with self.assertRaises(backup.BackupError):
                client.begin()
            db.rollback()
            self.assertEqual(len(server.calls), 1)
        server = FakeHrana(db)
        server.base_url = "https://unit-test.eu.turso.io/"
        client = backup.ReadSnapshot(URL, TOKEN, opener=server)
        client.begin()
        client.query("SELECT * FROM x")
        self.assertEqual(server.calls[-1][0], "https://unit-test.eu.turso.io/v3/pipeline")
        client.close()

    def test_reply_body_bound(self):
        client, opener = self.client(b"x"*50)
        with patch.object(backup, "MAX_RESPONSE_BYTES", 30), self.assertRaises(backup.BackupError):
            client.begin()
        self.assertEqual(opener.calls, 1)

    def test_gzip_transport_preserves_complete_snapshot_and_counts_wire_bytes(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE compressed_rows(value TEXT)")
        db.execute("INSERT INTO compressed_rows VALUES(?)", ("a" * 5000,))
        db.commit()
        server = FakeHrana(db)
        original = server.open
        wire = []
        def compressed_response(request, timeout):
            self.assertEqual(request.get_header("Accept-encoding"), "gzip")
            response = original(request, timeout)
            response.data = gzip.compress(response.data, mtime=0)
            response.headers = {"Content-Encoding": "gzip"}
            wire.append(len(response.data))
            return response
        server.open = compressed_response
        client = backup.ReadSnapshot(URL, TOKEN, opener=server)
        client.begin()
        self.assertEqual(client.query("SELECT value FROM compressed_rows")[1], [("a" * 5000,)])
        client.close()
        self.assertEqual(client.response_bytes, sum(wire))

    def test_compressed_bomb_corruption_and_unknown_encoding_are_refused(self):
        for encoding, body, bound in [("gzip", gzip.compress(b"x" * 10000), 100),
                                     ("gzip", gzip.compress(b"private " + TOKEN.encode())[:-4], 1000),
                                     ("br", b"{}", 100)]:
            with self.subTest(encoding=encoding, bound=bound):
                class Opener:
                    calls = 0
                    def open(self, request, timeout):
                        self.calls += 1
                        response = Response(body)
                        response.headers = {"Content-Encoding": encoding}
                        return response
                opener = Opener()
                client = backup.ReadSnapshot(URL, TOKEN, opener=opener)
                with patch.object(backup, "MAX_RESPONSE_BYTES", bound), self.assertRaises(backup.BackupError) as error:
                    client.begin()
                self.assertNotIn(TOKEN, str(error.exception))
                self.assertTrue(client.broken)
                self.assertEqual(opener.calls, 1)

    def test_deflate_and_incomplete_http_failures_break_snapshot_without_private_errors(self):
        class Opener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                response = Response(b"compressed")
                response.headers = {"Content-Encoding":"gzip"}
                return response
        opener = Opener()
        client = backup.ReadSnapshot(URL, TOKEN, opener=opener)
        with patch.object(backup.gzip, "GzipFile", side_effect=zlib.error(TOKEN)), self.assertRaisesRegex(backup.BackupError, "invalid compressed") as error:
            client.begin()
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertTrue(client.broken)
        self.assertEqual(opener.calls, 1)
        client, opener = self.client(http.client.IncompleteRead(TOKEN.encode(), 100))
        with self.assertRaises(backup.BackupError) as error:
            client.begin()
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertTrue(client.broken)
        self.assertEqual(opener.calls, 1)

    def test_tls_error_has_fixed_safe_guidance_and_never_retries(self):
        failure = urllib.error.URLError(ssl.SSLCertVerificationError(TOKEN))
        client, opener = self.client(failure)
        with self.assertRaises(backup.BackupTLSCertificateError) as error:
            client.begin()
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertIn("SSL_CERT_FILE", str(error.exception))
        self.assertEqual(opener.calls, 1)
        self.assertTrue(client.broken)
        err = io.StringIO()
        with patch.object(backup, "GitHub", side_effect=backup.BackupTLSCertificateError()), redirect_stderr(err):
            result = backup.main(["backup", "--publish", "--repository", "owner/private-backups"])
        self.assertEqual(result, 1)
        self.assertIn("never disable TLS verification", err.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())

    def test_timeout_reason_is_safe_specific_and_never_retried(self):
        for failure in (TimeoutError(TOKEN), urllib.error.URLError(TimeoutError(TOKEN))):
            client, opener = self.client(failure)
            with self.assertRaisesRegex(backup.BackupError, "request timed out") as error:
                client.begin()
            self.assertNotIn(TOKEN, str(error.exception))
            self.assertEqual(opener.calls, 1)

    def test_row_digest_is_lossless_framed_and_streamed(self):
        rows = [(None, 1, 1.25, "text", b"\xff"), ((1<<63)-1,)]
        digest = hashlib.sha256()
        for row in rows:
            encoded = json.dumps([backup.typed(value) for value in row], ensure_ascii=True, sort_keys=True, separators=(",",":"), allow_nan=False).encode()
            digest.update(struct.pack(">Q", len(encoded)))
            digest.update(encoded)
        self.assertEqual(backup.rows_digest(iter(rows)), digest.hexdigest())
        self.assertNotEqual(backup.rows_digest([(1,)]), backup.rows_digest([("1",)]))

    def test_deadline_expiration_and_local_mode_no_discarded_output(self):
        now = [1]
        deadline = backup.Deadline(1, clock=lambda:now[0])
        now[0] = 2
        with self.assertRaises(backup.BackupError):
            deadline.check()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = backup.main(["backup"])
        self.assertEqual(result, 1)
        self.assertNotIn(TOKEN, err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_extended_deadline_is_bounded_and_request_timeout_stays_twenty_seconds(self):
        deadline = backup.Deadline(1080, clock=lambda:10)
        self.assertEqual(deadline.end, 1090)
        self.assertEqual(deadline.timeout(), 20)
        self.assertEqual(deadline.upload_timeout(),120)
        self.assertEqual(backup.Deadline(10,clock=lambda:10).upload_timeout(),10)
        self.assertEqual(backup.Deadline(clock=lambda:10).end, 1010)
        for invalid in (0,1081,True,float('inf'),float('nan'),None):
            with self.assertRaises(backup.BackupError):
                backup.Deadline(invalid)

    def test_cli_failure_reason_is_allowlisted_not_remote_text(self):
        for message, visible in (("invalid typed database response", True), (TOKEN, False)):
            err = io.StringIO()
            with patch.object(backup, "create_backup", side_effect=backup.BackupError(message)), redirect_stderr(err):
                result = backup.main(["backup", "--output", "/unused-test-output"])
            self.assertEqual(result, 1)
            self.assertNotIn(TOKEN, err.getvalue())
            self.assertEqual(message in err.getvalue(), visible)


class FakeGitHub:
    def __init__(self):
        self.repository = "owner/private-backups"
        self.private = True
        self.releases = []
        self.calls = []
        self.uploads = []
        self.next_id = 100
        self.fail_upload = False
        self.bad_digest = False
        self.fail_publish = False
        self.temporary_draft_tag = False
        self.assets={}
        self.commit_before_error=False
        self.delayed_digest=False

    def open(self, request, timeout):
        path = urllib.parse.urlsplit(request.full_url).path
        payload = json.loads(request.data) if request.data is not None else None
        method = request.get_method()
        self.calls.append((method,path,payload))
        assert request.get_header("Authorization") == "Bearer " + TOKEN
        prefix = "/repos/" + self.repository
        if path == prefix:
            return Response({"full_name":self.repository, "private":self.private})
        if path == prefix + "/releases" and method == "GET":
            page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)["page"][0])
            return Response(self.releases[(page-1)*100:page*100])
        if path == prefix + "/releases" and method == "POST":
            row = {**payload, "id":self.next_id}
            if self.temporary_draft_tag:
                row["tag_name"] = "untagged-test-draft"
            self.next_id += 1
            self.releases.append(row)
            return Response(row)
        if path.endswith("/assets") and method=="GET":
            release_id=int(path.split("/")[-2])
            return Response([value for (owner,_),value in self.assets.items() if owner==release_id])
        if "/releases/" in path:
            release_id = int(path.rsplit("/",1)[1])
            row = next(row for row in self.releases if row["id"] == release_id)
            if method == "PATCH":
                if self.fail_publish and payload.get("draft") is False:
                    raise urllib.error.URLError(TOKEN)
                row.update(payload)
                return Response(row)
            if method == "DELETE":
                self.releases.remove(row)
                return Response(b"")
        if "/git/refs/tags/" in path and method == "DELETE":
            return Response(b"")
        raise AssertionError((method,path,payload))

    def connection(self, host, timeout):
        self.assert_host = host
        outer = self
        class Connection:
            def __init__(self):
                self.digest = hashlib.sha256()
                self.total = 0
                self.block_lengths = []
                self.headers = {}
            def putrequest(self, method, path):
                self.path = path
            def putheader(self, name, value):
                self.headers[name] = value
            def endheaders(self):
                pass
            def send(self, block):
                self.block_lengths.append(len(block))
                self.total += len(block)
                self.digest.update(block)
            def getresponse(self):
                outer.uploads.append(self)
                if outer.fail_upload and not outer.commit_before_error:
                    raise OSError(TOKEN)
                name = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)["name"][0]
                asset={"size":self.total,"name":name,"state":"uploaded",
                    "digest":"sha256:"+("wrong" if outer.bad_digest else self.digest.hexdigest())}
                release_id=int(urllib.parse.urlsplit(self.path).path.split("/")[-2])
                outer.assets[(release_id,name)]=asset
                if outer.fail_upload:
                    raise TimeoutError(TOKEN)
                return Response({**asset,**({"digest":None} if outer.delayed_digest else {})},201)
            def close(self):
                pass
        return Connection()

    def add_old(self, index, managed=True, draft=False):
        self.releases.append({"id":index, "tag_name":f"storage-backup-202601{index:02d}T020000Z-12345678" if managed else "unrelated-v1",
            "body":backup.RELEASE_MARKER if managed else "Other project release", "draft":draft})


class SnapshotRestartTests(unittest.TestCase):
    def test_cli_retry_discards_old_snapshot_and_exports_a_fresh_one_with_original_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_path = Path(temporary) / "source.sqlite"
            writer = sqlite3.connect(source_path)
            self.addCleanup(writer.close)
            writer.execute("PRAGMA journal_mode=WAL")
            fill(writer)
            now = [0]
            deadline = backup.Deadline(1000, clock=lambda: now[0])
            original_client = backup.ReadSnapshot
            clients, servers, outputs = [], [], []
            class Publisher:
                def assert_private(inner):
                    pass
                def publish(inner, archive, manifest_path, manifest, keep):
                    outputs.append((Path(archive), manifest))
                    self.assertTrue(backup.verify_backup(archive, manifest_path, deadline))
                    self.assertEqual(manifest["fingerprints"], backup.sqlite_fingerprints(source_path, deadline=deadline))
            def new_client(url, token, shared_deadline, opener=None):
                db = sqlite3.connect(source_path)
                self.addCleanup(db.close)
                server = FakeHrana(db)
                if not servers:
                    original = server.open
                    def interrupted(request, timeout):
                        # The first attempt already has a read transaction and
                        # partial local pages. A new source version is published
                        # after its transport dies, before the CLI retries.
                        if len(server.calls) == 3:
                            now[0] = 200
                            writer.execute("INSERT INTO sparse(rowid,text) VALUES(999,'new snapshot')")
                            writer.commit()
                            raise TimeoutError(TOKEN)
                        return original(request, timeout)
                    server.open = interrupted
                client = original_client(url, token, shared_deadline, server)
                clients.append(client)
                servers.append(server)
                return client
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(backup.os.environ, {"TURSO_DATABASE_URL": URL,
                    "TURSO_BACKUP_AUTH_TOKEN": TOKEN}, clear=True), \
                    patch.object(backup, "Deadline", return_value=deadline) as deadlines, \
                    patch.object(backup, "ReadSnapshot", side_effect=new_client), \
                    patch.object(backup, "GitHub", return_value=Publisher()), \
                    redirect_stdout(out), redirect_stderr(err):
                result = backup.main(["backup", "--publish", "--repository", "owner/private-backups"])
            self.assertEqual(result, 0, err.getvalue())
            self.assertEqual(len(clients), 2)
            self.assertTrue(clients[0].broken)
            self.assertIsNot(clients[0], clients[1])
            self.assertTrue(all(client.deadline is deadline for client in clients))
            self.assertEqual(deadline.end, 1000)
            deadlines.assert_called_once_with(1000)
            self.assertIsNone(servers[1].calls[0][1]["baton"])
            self.assertEqual(servers[1].calls[0][1]["requests"][0]["stmt"]["sql"], "BEGIN")
            self.assertEqual(len(servers[0].calls), 3)
            self.assertEqual(len(outputs), 1)
            self.assertEqual(outputs[0][0].parent.name, "attempt-1")
            self.assertFalse(outputs[0][0].exists())
            self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())

    def run_failures(self, failures, consumed=0, local=False):
        now = [0]
        deadline = backup.Deadline(1000, clock=lambda: now[0])
        attempts, published = [], []
        class Publisher:
            def assert_private(inner):
                pass
            def publish(inner, *args):
                published.append(args)
        failures = iter(failures)
        def fail(url, token, output, shared_deadline, **kwargs):
            attempts.append((Path(output).name, shared_deadline))
            now[0] += consumed
            raise next(failures)
        out, err = io.StringIO(), io.StringIO()
        args = ["backup", "--output", "/unused-test-output"] if local else [
            "backup", "--publish", "--repository", "owner/private-backups"]
        with patch.dict(backup.os.environ, {"TURSO_DATABASE_URL": URL,
                "TURSO_BACKUP_AUTH_TOKEN": TOKEN}, clear=True), \
                patch.object(backup, "Deadline", return_value=deadline), \
                patch.object(backup, "create_backup", side_effect=fail), \
                patch.object(backup, "GitHub", return_value=Publisher()), \
                redirect_stdout(out), redirect_stderr(err):
            result = backup.main(args)
        self.assertEqual(result, 1)
        self.assertEqual(published, [])
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        return attempts, deadline

    def test_only_one_whole_snapshot_retry_is_permitted(self):
        attempts, deadline = self.run_failures([
            backup.BackupError("database request timed out; read snapshot discarded"),
            backup.BackupError("database snapshot request failed; incomplete backup discarded")], consumed=100)
        self.assertEqual([name for name, _ in attempts], ["attempt-0", "attempt-1"])
        self.assertTrue(all(value is deadline for _, value in attempts))
        self.assertEqual(deadline.end, 1000)

    def test_retry_is_not_started_without_original_deadline_headroom(self):
        attempts, _ = self.run_failures([
            backup.BackupError("database request timed out; read snapshot discarded")], consumed=881)
        self.assertEqual(len(attempts), 1)

    def test_no_snapshot_retry_for_auth_protocol_tls_or_local_output(self):
        for error in (backup.BackupError("database authentication rejected"),
                      backup.BackupError("invalid typed database response"), backup.BackupTLSCertificateError()):
            with self.subTest(error=type(error).__name__):
                attempts, _ = self.run_failures([error])
                self.assertEqual(len(attempts), 1)
        attempts, _ = self.run_failures([
            backup.BackupError("database request timed out; read snapshot discarded")], local=True)
        self.assertEqual(len(attempts), 1)


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.path = Path(self.root.name)
        self.server = FakeGitHub()
        self.client = backup.GitHub(self.server.repository, TOKEN, opener=self.server, connection_factory=self.server.connection)
        self.archive = self.path / "radar.sqlite.gz"
        self.archive.write_bytes(b"a" * (2 * 1024 * 1024 + 7))
        self.manifest = {"archive_sha256":backup.file_sha(self.archive)}
        self.manifest_path = self.path / "manifest.json"
        self.manifest_path.write_text(backup.compact(self.manifest))

    def tearDown(self):
        self.root.cleanup()

    def test_private_only_before_any_upload(self):
        self.server.private = False
        with self.assertRaises(backup.BackupError):
            self.client.publish(self.archive, self.manifest_path, self.manifest)
        self.assertEqual(self.server.uploads, [])
        self.assertEqual(len(self.server.calls), 1)

    def test_streaming_checksum_upload_and_seven_copy_retention(self):
        for index in range(1,8):
            self.server.add_old(index)
        self.server.add_old(80, managed=False)
        result = self.client.publish(self.archive, self.manifest_path, self.manifest)
        managed = [row for row in self.server.releases if row["body"].startswith(backup.RELEASE_MARKER)]
        self.assertEqual(len(managed), 7)
        self.assertNotIn(1, [row["id"] for row in managed])
        self.assertIn(80, [row["id"] for row in self.server.releases])
        self.assertIn(result, [row["id"] for row in managed])
        self.assertFalse(next(row for row in managed if row["id"] == result)["draft"])
        self.assertEqual(self.server.assert_host, "uploads.github.com")
        archive_upload=self.server.uploads[-1]
        self.assertEqual(max(archive_upload.block_lengths), 1024 * 1024)
        self.assertEqual(archive_upload.total, self.archive.stat().st_size)
        self.assertEqual(archive_upload.headers["Content-Length"], str(self.archive.stat().st_size))
        self.assertTrue(any(method == "DELETE" and "/git/refs/tags/" in path for method,path,_ in self.server.calls))

    def test_upload_failure_keeps_all_old_backups_and_reconcilable_private_draft(self):
        for index in range(1,8):
            self.server.add_old(index)
        self.server.fail_upload = True
        with self.assertRaises(backup.BackupError) as error:
            self.client.publish(self.archive, self.manifest_path, self.manifest)
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertEqual([row["id"] for row in self.server.releases], [*range(1,8),100])
        self.assertTrue(self.server.releases[-1]["draft"])
        self.assertFalse(self.server.releases[-1]["body"].startswith(backup.RELEASE_MARKER))

    def test_checksum_mismatch_never_prunes_good_backups(self):
        self.server.add_old(1)
        self.server.bad_digest = True
        with self.assertRaises(backup.BackupError):
            self.client.publish(self.archive, self.manifest_path, self.manifest)
        self.assertEqual([row["id"] for row in self.server.releases], [1,100])
        self.assertTrue(self.server.releases[-1]["draft"])

    def test_lost_upload_response_reconciles_hash_without_posting_the_asset_twice(self):
        self.server.fail_upload=True;self.server.commit_before_error=True
        result=self.client.publish(self.archive,self.manifest_path,self.manifest)
        self.assertEqual(result,100)
        self.assertEqual(len(self.server.uploads),2)
        self.assertFalse(self.server.releases[0]["draft"])

    def test_delayed_digest_must_be_confirmed_before_release_publication(self):
        self.server.delayed_digest=True
        result=self.client.publish(self.archive,self.manifest_path,self.manifest)
        self.assertEqual(result,100)
        reads=[path for method,path,_ in self.server.calls if method=="GET" and path.endswith("/assets")]
        self.assertEqual(len(reads),2)
        self.assertEqual(len(self.server.uploads),2)

    def test_publication_failure_preserves_verified_draft_and_all_old_copies(self):
        for index in range(1,8):
            self.server.add_old(index)
        self.server.fail_publish = True
        with self.assertRaises(backup.BackupError):
            self.client.publish(self.archive, self.manifest_path, self.manifest)
        self.assertEqual(len(self.server.releases), 8)
        self.assertTrue(set(range(1, 8)).issubset(row["id"] for row in self.server.releases))
        latest = next(row for row in self.server.releases if row["id"] == 100)
        self.assertTrue(latest["draft"])
        self.assertTrue(latest["body"].startswith(backup.RELEASE_MARKER))

    def test_temporary_draft_tag_is_published_before_retention(self):
        for index in range(1, 8):
            self.server.add_old(index)
        self.server.temporary_draft_tag = True
        result = self.client.publish(self.archive, self.manifest_path, self.manifest)
        published = next(row for row in self.server.releases if row["id"] == result)
        self.assertFalse(published["draft"])
        self.assertTrue(published["tag_name"].startswith("storage-backup-"))
        publication = next(index for index, (method, _, payload) in enumerate(self.server.calls)
            if method == "PATCH" and payload.get("draft") is False)
        deletion = next(index for index, (method, _, _) in enumerate(self.server.calls) if method == "DELETE")
        self.assertLess(publication, deletion)

    def test_unverified_drafts_foreign_releases_and_tags_not_pruned(self):
        for index in range(1,9):
            self.server.add_old(index)
        self.server.add_old(80, managed=False)
        self.server.releases.append({"id":81, "tag_name":"storage-backup-20260201T020000Z-12345678", "body":"Not verified", "draft":True})
        self.client.prune()
        self.assertEqual(len([row for row in self.server.releases if row["body"].startswith(backup.RELEASE_MARKER)]), 7)
        self.assertIn(80, [row["id"] for row in self.server.releases])
        self.assertIn(81, [row["id"] for row in self.server.releases])

    def test_repository_token_and_path_validation(self):
        for repository in ("owner/x/y", "https://github.com/o/r", "owner/r?secret=x", None):
            with self.assertRaises(backup.BackupError):
                backup.GitHub(repository, TOKEN)
        with self.assertRaises(backup.BackupError):
            self.client.request("https://evil.example")
        for keep in (0,8,True):
            with self.assertRaises(backup.BackupError):
                self.client.prune(keep)

    def test_github_tls_error_never_leaks_http_details(self):
        with patch.object(self.server, "open", side_effect=urllib.error.URLError(ssl.SSLCertVerificationError(TOKEN))):
            with self.assertRaises(backup.BackupTLSCertificateError) as error:
                self.client.assert_private()
        self.assertNotIn(TOKEN, str(error.exception))


if __name__ == "__main__":
    unittest.main()
