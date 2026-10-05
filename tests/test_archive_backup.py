"""Offline archive backup failure, privacy, retention and restore contracts."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import struct
import tarfile
import tempfile
from threading import Event, Lock
import unittest
from unittest.mock import patch
import urllib.parse

import requests
from tools import archive_backup as backup


SECRET = "unit-secret-never-log"
URL = "https://unit.example/api/storage/archive-backup"


def row(index=0, body=b"body"):
    return {"key": f"history/{index}.json.gz", "bytes": len(body), "etag": f"etag-{index}"}


class Response:
    def __init__(self, data=b"", status=200, headers=None, hook=None, chunk_size=None):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.status_code, self.headers, self.hook = status, headers or {}, hook
        self.closed = False
        self.chunk_size = chunk_size

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(SECRET)

    def iter_content(self, size):
        size = self.chunk_size or size
        for start in range(0, len(self.data), size):
            if self.hook:
                self.hook()
            yield self.data[start:start + size]


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def frames(rows, bodies, headers=None):
    output = bytearray()
    for entry, body in zip(headers or rows, bodies):
        header = json.dumps(entry, separators=(",", ":")).encode()
        output.extend(struct.pack(">I", len(header)))
        output.extend(header)
        output.extend(body)
    return bytes(output)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.deadline = backup.Deadline()

    def test_module_import_and_paginated_inventory_are_bounded_and_read_only(self):
        responses = [Response({"ok": True, "objects": [row(1)], "cursor": "next"}),
                     Response({"ok": True, "objects": [row(0)], "cursor": None})]
        session = Session(responses)
        self.assertEqual(backup.inventory(URL, SECRET, self.deadline, session), [row(0), row(1)])
        self.assertEqual(session.calls[1][1]["params"], {"cursor": "next"})
        self.assertTrue(all(value.closed for value in responses))
        for url, options in session.calls:
            self.assertEqual(url, URL)
            self.assertFalse(options["allow_redirects"])
            self.assertTrue(options["stream"])
            self.assertLessEqual(options["timeout"], 20)

    def test_duplicate_inventory_fails_even_for_identical_repeated_rows(self):
        for duplicate in (row(), {**row(), "etag": "changed"}):
            session = Session([Response({"ok": True, "objects": [row()], "cursor": "next"}),
                               Response({"ok": True, "objects": [duplicate], "cursor": None})])
            with self.assertRaisesRegex(backup.BackupError, "duplicate"):
                backup.inventory(URL, SECRET, self.deadline, session)

    def test_invalid_cursor_page_status_rows_and_size_fail_closed(self):
        variants = [[], {"ok": True, "objects": []},
            {"ok": True, "objects": [None], "cursor": None},
            {"ok": True, "objects": [row()], "cursor": 7},
            {"ok": True, "objects": [row()], "cursor": "x" * 2049},
            {"ok": True, "objects": [], "cursor": "next"},
            {"ok": True, "objects": [{**row(), "key": "history/../secret"}], "cursor": None},
            {"ok": True, "objects": [{**row(), "bytes": True}], "cursor": None}]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(backup.BackupError):
                backup.inventory(URL, SECRET, self.deadline, Session([Response(value)]))
        with patch.object(backup, "MAX_PAGE_BYTES", 20), self.assertRaises(backup.BackupError):
            backup.inventory(URL, SECRET, self.deadline, Session([Response(b"x" * 21)]))
        with self.assertRaises(backup.BackupError):
            backup.inventory(URL, SECRET, self.deadline, Session([Response({}, status=302)]))

    def test_cursor_stall_object_cap_and_pagination_cap(self):
        pages = [Response({"ok": True, "objects": [row(0)], "cursor": "same"}),
                 Response({"ok": True, "objects": [row(1)], "cursor": "same"})]
        with self.assertRaises(backup.BackupError):
            backup.inventory(URL, SECRET, self.deadline, Session(pages))
        with patch.object(backup, "MAX_OBJECTS", 1), self.assertRaises(backup.BackupError):
            backup.inventory(URL, SECRET, self.deadline, Session([
                Response({"ok": True, "objects": [row(0), row(1)], "cursor": None})]))
        with patch.object(backup, "MAX_INVENTORY_PAGES", 1), self.assertRaises(backup.BackupError):
            backup.inventory(URL, SECRET, self.deadline, Session([
                Response({"ok": True, "objects": [row(0)], "cursor": "next"})]))

    def test_download_verifies_complete_body_and_private_permissions(self):
        body = b"\0original gzip-like bytes\xff"
        entry = row(body=body)
        response = Response(body, headers={"x-radar-source-etag": entry["etag"],
                                         "Content-Length": str(len(body))})
        session = Session([response])
        result = backup.download(URL, SECRET, entry, self.root, self.deadline, session)
        path = self.root / result["file"]
        self.assertEqual(path.read_bytes(), body)
        self.assertEqual(result["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(session.calls[0][1]["headers"]["Accept-Encoding"], "identity")
        self.assertTrue(response.closed)

    def test_missing_changed_incomplete_oversize_encoding_and_redirect_objects_never_survive(self):
        entry = row()
        variants = [Response(status=404), Response(status=302),
            Response(b"body", headers={"x-radar-source-etag": "other"}),
            Response(b"bo", headers={"x-radar-source-etag": entry["etag"]}),
            Response(b"bodyextra", headers={"x-radar-source-etag": entry["etag"]}),
            Response(b"body", headers={"x-radar-source-etag": entry["etag"], "Content-Encoding": "gzip"}),
            Response(b"body", headers={"x-radar-source-etag": entry["etag"], "Content-Length": "999"})]
        for response in variants:
            with self.subTest(status=response.status_code), self.assertRaises((backup.BackupError, requests.HTTPError)):
                backup.download(URL, SECRET, entry, self.root, self.deadline, Session([response]))
            self.assertEqual(list(self.root.iterdir()), [])

    def test_deadline_and_cancellation_remove_incomplete_bodies_without_new_reads(self):
        now = [0]
        deadline = backup.Deadline(10, clock=lambda: now[0])
        response = Response(b"body", headers={"x-radar-source-etag": "etag-0"},
                            hook=lambda: now.__setitem__(0, 10))
        with self.assertRaisesRegex(backup.BackupError, "deadline"):
            backup.download(URL, SECRET, row(), self.root, deadline, Session([response]))
        self.assertEqual(list(self.root.iterdir()), [])
        stop = Event()
        stop.set()
        session = Session([])
        with self.assertRaisesRegex(backup.BackupError, "cancelled"):
            backup.download(URL, SECRET, row(), self.root, self.deadline, session, stop)
        self.assertEqual(session.calls, [])

    def test_existing_partial_destination_is_never_deleted_on_collision(self):
        path = self.root / (hashlib.sha256(row()["key"].encode()).hexdigest() + ".partial")
        path.write_bytes(b"preserve")
        response = Response(b"body", headers={"x-radar-source-etag": "etag-0"})
        with self.assertRaises(FileExistsError):
            backup.download(URL, SECRET, row(), self.root, self.deadline, Session([response]))
        self.assertEqual(path.read_bytes(), b"preserve")

    def test_four_way_failure_cancels_peers_and_never_submits_entire_inventory(self):
        calls, cancelled, lock = [], [], Lock()
        peers_started = Event()
        def fake_download(url, secret, entries, directory, deadline, session, stop):
            entry = entries[0]
            with lock:
                calls.append(entry["key"])
                if len(calls) == 4:
                    peers_started.set()
            self.assertTrue(peers_started.wait(2))
            if entry["key"] == row(0)["key"]:
                raise backup.BackupError("archive object is incomplete")
            self.assertTrue(stop.wait(2))
            cancelled.append(entry["key"])
            raise backup.BackupError("archive downloads cancelled")
        with patch.object(backup, "download_group", side_effect=fake_download), self.assertRaises(backup.BackupError):
            backup.download_all(URL, SECRET, [row(index) for index in range(100)], self.root, self.deadline)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(cancelled), 3)

    def test_four_way_success_preserves_inventory_order(self):
        entries = [row(index) for index in range(20)]
        with patch.object(backup, "download_group", side_effect=lambda u, s, r, d, t, session, stop: r):
            self.assertEqual(backup.download_all(URL, SECRET, entries, self.root, self.deadline), entries)


class BatchTests(unittest.TestCase):
    setUp = DownloadTests.setUp

    def response(self, entries, bodies, **kwargs):
        body = frames(entries, bodies)
        headers = {"x-radar-batch-count": str(len(entries)), "Content-Length": str(len(body))}
        return Response(body, headers=headers, **kwargs)

    def test_grouping_preserves_order_and_exact_object_and_byte_boundaries(self):
        entries = [row(index, b"") for index in range(33)]
        groups = backup.download_groups(entries)
        self.assertEqual([len(group) for group in groups], [16, 16, 1])
        self.assertEqual([entry for group in groups for entry in group], entries)
        entries = [{**row(index), "bytes": size} for index, size in enumerate([
            backup.MAX_BATCH_BYTES // 2, backup.MAX_BATCH_BYTES // 2,
            1, backup.MAX_BATCH_BYTES, backup.MAX_BATCH_BYTES + 1, 0, 1])]
        groups = backup.download_groups(entries)
        self.assertEqual([len(group) for group in groups], [2, 1, 1, 1, 2])
        self.assertEqual([entry for group in groups for entry in group], entries)
        for group in groups:
            self.assertLessEqual(len(group), 16)
            self.assertTrue(sum(entry["bytes"] for entry in group) <= backup.MAX_BATCH_BYTES
                            or len(group) == 1)

    def test_duplicate_overfull_and_oversized_batch_requests_fail_before_network(self):
        variants = [[], [row(), row()], [row(index) for index in range(17)],
                    [{**row(), "bytes": backup.MAX_BATCH_BYTES + 1}]]
        for entries in variants:
            session = Session([])
            with self.subTest(entries=entries), self.assertRaises(backup.BackupError):
                backup.download_batch(URL, SECRET, entries, self.root, self.deadline, session)
            self.assertEqual(session.calls, [])
        with self.assertRaises(backup.BackupError):
            backup.download_groups([row(), row()])

    def test_fragmented_frames_preserve_exact_binary_body_sha_and_private_modes(self):
        bodies = [b"\x00\xff\x1f\x8bunaltered compressed bytes", b"", b"last body"]
        entries = [row(index, body) for index, body in enumerate(bodies)]
        response = self.response(entries, bodies, chunk_size=1)
        session = Session([response])
        objects = backup.download_batch(URL, SECRET, entries, self.root, self.deadline, session)
        self.assertTrue(response.closed)
        for entry, body in zip(objects, bodies):
            path = self.root / entry["file"]
            self.assertEqual(path.read_bytes(), body)
            self.assertEqual(entry["sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        url, options = session.calls[0]
        self.assertEqual(url, URL + "/batch")
        self.assertEqual(options["json"], {"keys": [entry["key"] for entry in entries]})
        self.assertFalse(options["allow_redirects"])
        self.assertTrue(options["stream"])
        self.assertEqual(options["headers"]["Accept-Encoding"], "identity")
        self.assertLessEqual(options["timeout"], 20)

    def test_eight_mib_body_uses_batch_but_larger_body_uses_individual_get(self):
        body = b"x" * backup.MAX_BATCH_BYTES
        entry = row(body=body)
        session = Session([self.response([entry], [body])])
        result = backup.download_group(URL, SECRET, [entry], self.root, self.deadline, session)
        self.assertEqual(result[0]["sha256"], hashlib.sha256(body).hexdigest())
        self.assertIn("json", session.calls[0][1])
        large = {**row(1), "bytes": backup.MAX_BATCH_BYTES + 1}
        with patch.object(backup, "download", return_value=large) as individual, \
                patch.object(backup, "download_batch", side_effect=AssertionError("individual GET required")):
            self.assertEqual(backup.download_group(URL, SECRET, [large], self.root, self.deadline, session), [large])
            individual.assert_called_once_with(URL, SECRET, large, self.root, self.deadline, session, None)

    def test_malformed_header_order_etag_size_duplicate_fields_and_truncation_rollback_all(self):
        entries, bodies = [row(0), row(1)], [b"body", b"body"]
        valid = frames(entries, bodies)
        duplicate = b'{"key":"history/1.json.gz","key":"history/1.json.gz","bytes":4,"etag":"etag-1"}'
        variants = [valid[:-1], valid[:2], valid + b"extra", valid + frames([row(2)], [b"body"]),
            frames(entries, bodies, headers=[entries[0], {**entries[1], "etag": "changed"}]),
            frames(entries, bodies, headers=[entries[0], {**entries[1], "bytes": 3}]),
            frames(entries, bodies, headers=[entries[1], entries[0]]),
            frames(entries, bodies, headers=[entries[0], {**entries[1], "unexpected": True}]),
            frames([entries[0]], [b"body"]) + struct.pack(">I", len(duplicate)) + duplicate + b"body",
            struct.pack(">I", 0), struct.pack(">I", backup.MAX_FRAME_HEADER_BYTES + 1),
            struct.pack(">I", 1) + b"{", struct.pack(">I", 1) + b"\xff"]
        for body in variants:
            session = Session([Response(body, headers={"x-radar-batch-count": "2"}, chunk_size=7)])
            with self.subTest(body=body[:20]), self.assertRaises((backup.BackupError, ValueError)):
                backup.download_batch(URL, SECRET, entries, self.root, self.deadline, session)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_exact_maximum_header_size_is_valid(self):
        entry = row()
        header = json.dumps(entry).encode().ljust(backup.MAX_FRAME_HEADER_BYTES, b" ")
        body = struct.pack(">I", len(header)) + header + b"body"
        session = Session([Response(body, headers={"x-radar-batch-count": "1"}, chunk_size=3)])
        objects = backup.download_batch(URL, SECRET, [entry], self.root, self.deadline, session)
        self.assertEqual((self.root / objects[0]["file"]).read_bytes(), b"body")

    def test_wrong_count_status_encoding_length_and_remote_errors_do_not_fallback(self):
        body = frames([row()], [b"body"])
        variants = [Response(body), Response(body, headers={"x-radar-batch-count": "2"}),
            Response(body, headers={"x-radar-batch-count": "01"}),
            Response(body, status=206, headers={"x-radar-batch-count": "1"}),
            Response(body, status=302), Response(status=403), Response(status=503),
            Response(body, headers={"x-radar-batch-count": "1", "Content-Encoding": "gzip"}),
            Response(body, headers={"x-radar-batch-count": "1", "Content-Length": "invalid"}),
            Response(body, headers={"x-radar-batch-count": "1", "Content-Length": str(len(body) - 1)})]
        for response in variants:
            session = Session([response])
            with self.subTest(headers=response.headers, status=response.status_code), \
                    patch.object(backup, "download", side_effect=AssertionError("must not fallback")), \
                    self.assertRaises((backup.BackupError, requests.HTTPError)):
                backup.download_group(URL, SECRET, [row()], self.root, self.deadline, session)
            self.assertTrue(response.closed)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_deadline_and_cancellation_remove_all_created_partial_bodies(self):
        entries, bodies = [row(0), row(1)], [b"body", b"body"]
        for expire in (False, True):
            now, reads, cancelled = [0], [0], Event()
            deadline = backup.Deadline(10, clock=lambda: now[0])
            def hook():
                reads[0] += 1
                if reads[0] == 2:
                    if expire:
                        now[0] = 10
                    else:
                        cancelled.set()
            response = self.response(entries, bodies, chunk_size=len(frames([entries[0]], [b"body"])), hook=hook)
            with self.subTest(expire=expire), self.assertRaises(backup.BackupError):
                backup.download_batch(URL, SECRET, entries, self.root, deadline, Session([response]), cancelled)
            self.assertEqual(list(self.root.iterdir()), [])
            self.assertTrue(response.closed)

    def test_preexisting_destination_is_preserved_and_promotion_failure_rolls_back_batch(self):
        entries, bodies = [row(0), row(1)], [b"body", b"body"]
        existing = self.root / (hashlib.sha256(entries[0]["key"].encode()).hexdigest() + ".partial")
        existing.write_bytes(b"preserve")
        session = Session([])
        with self.assertRaises(backup.BackupError):
            backup.download_batch(URL, SECRET, entries, self.root, self.deadline, session)
        self.assertEqual(session.calls, [])
        self.assertEqual(existing.read_bytes(), b"preserve")
        existing.unlink()
        original, calls = Path.rename, []
        def promote(source, destination):
            calls.append(source)
            if len(calls) == 2:
                raise OSError("synthetic promotion failure")
            return original(source, destination)
        with patch.object(Path, "rename", promote), self.assertRaises(OSError):
            backup.download_batch(URL, SECRET, entries, self.root, self.deadline,
                                  Session([self.response(entries, bodies)]))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_four_way_batch_pipeline_keeps_source_sha_and_offline_restore_complete(self):
        bodies = {row(index)["key"]: f"source-{index}".encode() for index in range(33)}
        entries = [row(index, bodies[row(index)["key"]]) for index in range(33)]
        class Source:
            calls = []
            def post(inner, url, **options):
                keys = options["json"]["keys"]
                inner.calls.append(keys)
                selected = [next(entry for entry in entries if entry["key"] == key) for key in keys]
                return self.response(selected, [bodies[key] for key in keys], chunk_size=11)
        source = Source()
        objects = backup.download_all(URL, SECRET, entries, self.root, self.deadline, source)
        self.assertEqual(sorted(len(call) for call in source.calls), [1, 16, 16])
        self.assertEqual([entry["key"] for entry in objects], [entry["key"] for entry in entries])
        for entry in objects:
            self.assertEqual(entry["sha256"], hashlib.sha256(bodies[entry["key"]]).hexdigest())
        shards = backup.bundle(objects, self.root, self.deadline)
        backup.write_manifest(objects, shards, self.root, "offline test", self.deadline)
        for entry in objects:
            (self.root / entry["file"]).unlink()
        self.assertTrue(backup.verify_backup(self.root))

    def test_progress_is_numeric_throttled_to_thirty_seconds_and_final_completion(self):
        now, logs, pending_counts = [0], [], []
        deadline = backup.Deadline(1000, clock=lambda: now[0])
        def one_complete(pending, **options):
            pending_counts.append(len(pending))
            future = next(iter(pending))
            future.result()
            now[0] += 15
            return {future}, set(pending) - {future}
        def progress(*values):
            logs.append((now[0], values))
        with patch.object(backup, "wait", side_effect=one_complete), \
                patch.object(backup, "download_group", side_effect=lambda u, s, r, d, t, session, stop: r):
            backup.download_all(URL, SECRET, [row(index) for index in range(80)], self.root,
                                deadline, progress=progress)
        self.assertEqual([time for time, values in logs], [30, 60, 75])
        self.assertEqual(logs[-1][1], (80, 80, 320, 320))
        self.assertTrue(all(type(value) is int for time, values in logs for value in values))
        self.assertLessEqual(max(pending_counts), 4)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.deadline = backup.Deadline()

    def objects(self, count=3, body=b"body", long_keys=False):
        result = []
        for index in range(count):
            entry = row(index, body)
            if long_keys:
                entry["key"] = "history/" + ("x" * 600) + f"/{index}.gz"
            name = hashlib.sha256(entry["key"].encode()).hexdigest()
            (self.root / name).write_bytes(body)
            result.append({**entry, "file": name, "sha256": hashlib.sha256(body).hexdigest()})
        return result

    def backup(self, count=3, body=b"body", long_keys=False):
        objects = self.objects(count, body, long_keys)
        shards = backup.bundle(objects, self.root, self.deadline)
        path, manifest = backup.write_manifest(objects, shards, self.root, "2026-10-05T00:00:00Z", self.deadline)
        return path, manifest

    def test_all_object_bodies_restore_using_only_release_assets(self):
        path, manifest = self.backup()
        for entry in manifest["objects"]:
            (self.root / entry["file"]).unlink()
        self.assertTrue(backup.verify_backup(self.root))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / manifest["shards"][0]["file"]).stat().st_mode & 0o777, 0o600)
        out = io.StringIO()
        with redirect_stdout(out), patch.object(backup, "GitHub", side_effect=AssertionError("offline only")):
            self.assertEqual(backup.main(["verify", str(self.root)]), 0)

    def test_tar_overhead_is_included_in_shard_byte_budget(self):
        objects = self.objects(3, b"x" * 10000)
        with patch.object(backup, "SHARD_BYTES", 20480):
            shards = backup.bundle(objects, self.root, self.deadline)
            self.assertEqual(len(shards), 3)
            self.assertTrue(all(shard["bytes"] <= 20480 for shard in shards))
            backup.write_manifest(objects, shards, self.root, "now", self.deadline)

    def test_source_body_tampering_missing_and_symlink_never_bundle(self):
        for kind in ("changed", "missing", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entry = row()
                entry.update(file=hashlib.sha256(entry["key"].encode()).hexdigest(),
                             sha256=hashlib.sha256(b"body").hexdigest())
                source = root / entry["file"]
                if kind == "changed":
                    source.write_bytes(b"fake")
                elif kind == "symlink":
                    (root / "target").write_bytes(b"body")
                    source.symlink_to(root / "target")
                with self.assertRaises(backup.BackupError):
                    backup.bundle([entry], root, self.deadline)

    def test_duplicate_objects_unassigned_missing_shards_and_traversal_are_rejected(self):
        _, manifest = self.backup()
        variants = []
        value = copy.deepcopy(manifest)
        value["objects"].append(copy.deepcopy(value["objects"][0]))
        variants.append(value)
        value = copy.deepcopy(manifest)
        value["objects"][0]["shard"] = "missing.tar"
        variants.append(value)
        value = copy.deepcopy(manifest)
        value["shards"] = []
        variants.append(value)
        value = copy.deepcopy(manifest)
        value["objects"][0]["file"] = "../secret"
        variants.append(value)
        value = copy.deepcopy(manifest)
        value["shards"][0]["file"] = "../secret.tar"
        variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(backup.BackupError):
                backup.verify_backup(self.root, value, self.deadline)

    def test_corrupt_tar_digest_and_duplicate_tar_members_cannot_verify(self):
        _, manifest = self.backup(1)
        shard = manifest["shards"][0]
        path = self.root / shard["file"]
        path.write_bytes(b"x" * shard["bytes"])
        with self.assertRaisesRegex(backup.BackupError, "SHA-256"):
            backup.verify_backup(self.root, manifest, self.deadline)
        entry = manifest["objects"][0]
        with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as archive:
            for _ in range(2):
                info = tarfile.TarInfo(entry["file"])
                info.size = 4
                archive.addfile(info, io.BytesIO(b"body"))
        shard.update(bytes=path.stat().st_size, sha256=backup.file_sha(path))
        with self.assertRaisesRegex(backup.BackupError, "duplicate"):
            backup.verify_backup(self.root, manifest, self.deadline)

    def test_manifest_larger_than_one_mb_is_supported_and_offline_parse_is_bounded(self):
        path, manifest = self.backup(1300, b"", long_keys=True)
        self.assertGreater(path.stat().st_size, 1024 * 1024)
        self.assertTrue(backup.verify_backup(self.root))
        with patch.object(backup, "MAX_MANIFEST_BYTES", 1024 * 1024), self.assertRaises(backup.BackupError):
            backup.verify_backup(self.root)

    def test_empty_inventory_is_a_valid_but_explicit_empty_recovery_point(self):
        _, manifest = self.backup(0)
        self.assertEqual(manifest["inventory_count"], 0)
        self.assertTrue(backup.verify_backup(self.root))


class FakeGitHub:
    def __init__(self):
        self.prefix = "/repos/owner/private"
        self.private = True
        self.releases, self.calls, self.uploads = [], [], []
        self.fail_upload, self.fail_publish, self.hide_current = False, False, False
        self.id = 10000

    def assert_private(self):
        self.calls.append(("private",))
        if not self.private:
            raise backup.BackupError("backup repository must be private; refusing to upload")

    def request(self, path, method="GET", payload=None):
        self.calls.append((method, path, payload))
        if method == "POST":
            # GitHub draft tags can be temporary until publication.
            value = {**payload, "id": self.id, "tag_name": "untagged-draft"}
            self.releases.append(value)
            return value.copy()
        if method == "GET":
            page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["page"][0])
            values = [row for row in self.releases if not (self.hide_current and row["id"] == self.id)]
            return copy.deepcopy(values[(page - 1) * 100:page * 100])
        if method == "DELETE":
            if "/releases/" in path:
                self.releases = [row for row in self.releases if row["id"] != int(path.rsplit("/", 1)[1])]
            return None
        if method == "PATCH":
            if self.fail_publish and payload.get("draft") is False:
                raise backup.BackupError("private GitHub backup request failed")
            value = next(row for row in self.releases if row["id"] == int(path.rsplit("/", 1)[1]))
            value.update(payload)
            return value.copy()
        raise AssertionError((method, path))

    def upload(self, release, path, sha):
        self.assert_private()
        self.uploads.append((release, Path(path).name, sha))
        if self.fail_upload or backup.file_sha(path) != sha:
            raise backup.BackupError("private backup upload or remote SHA-256 verification failed")

    def old(self, index, **kwargs):
        self.releases.append({"id": index, "tag_name": f"r2-backup-20260101T{index:06d}Z-12345678",
                              "draft": False, "body": backup.MARKER, **kwargs})


class PublishTests(unittest.TestCase):
    setUp = BundleTests.setUp
    objects = BundleTests.objects
    backup = BundleTests.backup

    def test_private_only_upload_and_complete_before_publish_then_keep_two(self):
        path, manifest = self.backup()
        github = FakeGitHub()
        for index in range(1, 4):
            github.old(index)
        github.old(80, tag_name="unrelated", body="unrelated")
        release = backup.publish(github, self.root, path, manifest, self.deadline)
        self.assertEqual({row["id"] for row in github.releases}, {3, 80, release})
        self.assertEqual(len(github.uploads), len(manifest["shards"]) + 1)
        patch_index = next(i for i, call in enumerate(github.calls)
                           if call[0] == "PATCH" and call[2].get("draft") is False)
        delete_index = next(i for i, call in enumerate(github.calls) if call[0] == "DELETE")
        self.assertLess(patch_index, delete_index)
        self.assertTrue(any(call[0] == "DELETE" and "/git/refs/tags/" in call[1] for call in github.calls))

    def test_public_repository_refused_before_create_or_upload(self):
        path, manifest = self.backup()
        github = FakeGitHub()
        github.private = False
        with self.assertRaises(backup.BackupError):
            backup.publish(github, self.root, path, manifest, self.deadline)
        self.assertEqual(github.uploads, [])
        self.assertEqual(github.calls, [("private",)])

    def test_remote_digest_failure_never_publishes_or_prunes_previous_copies(self):
        path, manifest = self.backup()
        github = FakeGitHub()
        github.old(1)
        github.old(2)
        github.fail_upload = True
        with self.assertRaises(backup.BackupError):
            backup.publish(github, self.root, path, manifest, self.deadline)
        self.assertEqual({row["id"] for row in github.releases}, {1, 2})
        self.assertFalse(any(call[0] == "PATCH" for call in github.calls))

    def test_actual_github_transport_rejects_remote_digest_mismatch(self):
        from tests.test_storage_backup import FakeGitHub as Transport, TOKEN
        path, manifest = self.backup()
        transport = Transport()
        transport.bad_digest = True
        github = backup.GitHub(transport.repository, TOKEN, self.deadline,
                               opener=transport, connection_factory=transport.connection)
        with self.assertRaises(backup.BackupError):
            backup.publish(github, self.root, path, manifest, self.deadline)
        self.assertFalse(any(method == "PATCH" for method, _, _ in transport.calls))
        self.assertEqual(transport.releases, [])

    def test_changed_manifest_is_not_uploaded_as_a_verified_restore_point(self):
        path, manifest = self.backup()
        value = copy.deepcopy(manifest)
        value["objects"] = []
        path.write_text(json.dumps(value))
        github = FakeGitHub()
        with self.assertRaisesRegex(backup.BackupError, "changed"):
            backup.publish(github, self.root, path, manifest, self.deadline)
        self.assertEqual(github.calls, [])

    def test_publication_failure_keeps_verified_draft_and_both_previous_copies(self):
        path, manifest = self.backup()
        github = FakeGitHub()
        github.old(1)
        github.old(2)
        github.fail_publish = True
        with self.assertRaises(backup.BackupError):
            backup.publish(github, self.root, path, manifest, self.deadline)
        self.assertEqual({row["id"] for row in github.releases}, {1, 2, github.id})
        latest = next(row for row in github.releases if row["id"] == github.id)
        self.assertTrue(latest["draft"])
        self.assertTrue(latest["body"].startswith(backup.MARKER))
        self.assertFalse(any(call[0] == "DELETE" for call in github.calls))

    def test_missing_replacement_listing_never_deletes_good_backups(self):
        path, manifest = self.backup()
        github = FakeGitHub()
        for index in range(1, 4):
            github.old(index)
        github.hide_current = True
        with self.assertRaisesRegex(backup.BackupError, "absent"):
            backup.publish(github, self.root, path, manifest, self.deadline)
        self.assertFalse(any(call[0] == "DELETE" for call in github.calls))

    def test_paginated_retention_preserves_unverified_foreign_and_draft_releases(self):
        github = FakeGitHub()
        for index in range(1, 103):
            github.old(index)
        github.old(300, body="unverified")
        github.old(301, draft=True)
        github.old(302, tag_name="r2-backup-wrongtag")
        github.old(303, body=backup.MARKER + "\nmanifest_sha256=not-verified")
        backup.prune(github, 1)
        self.assertEqual({row["id"] for row in github.releases}, {1, 102, 300, 301, 302, 303})
        self.assertTrue(any(call[0] == "GET" and "page=2" in call[1] for call in github.calls))

    def test_large_manifest_upload_uses_existing_streaming_github_method(self):
        from tests.test_storage_backup import FakeGitHub as Transport, TOKEN
        path, manifest = self.backup(1300, b"", long_keys=True)
        transport = Transport()
        github = backup.GitHub(transport.repository, TOKEN, self.deadline,
                               opener=transport, connection_factory=transport.connection)
        github.upload(123, path, backup.file_sha(path))
        self.assertGreater(len(transport.uploads[-1].block_lengths), 1)
        self.assertEqual(transport.uploads[-1].total, path.stat().st_size)
        self.assertLessEqual(max(transport.uploads[-1].block_lengths), 1024 * 1024)
        self.assertGreater(path.stat().st_size, 1024 * 1024)


class CLITests(unittest.TestCase):
    def test_inventory_and_download_logs_contain_only_safe_numeric_progress(self):
        out, err, rows = io.StringIO(), io.StringIO(), [row(0), row(1)]
        def download(url, secret, entries, root, deadline, **kwargs):
            kwargs["progress"](1, 2, 4, 8)
            return entries
        with patch.dict(backup.os.environ, {"RADAR_DATA_API_URL": "https://unit.example",
                "RADAR_ARCHIVE_BACKUP_SECRET": SECRET}, clear=True), \
                patch.object(backup, "GitHub", return_value=FakeGitHub()), \
                patch.object(backup, "inventory", return_value=rows), \
                patch.object(backup, "download_all", side_effect=download), \
                patch.object(backup, "bundle", return_value=[]), \
                patch.object(backup, "write_manifest", return_value=(Path("offline"), {})), \
                patch.object(backup, "publish"), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(backup.main([]), 0, err.getvalue())
        self.assertIn("R2 backup inventory: objects=2 bytes=8", out.getvalue())
        self.assertIn("R2 backup download progress: objects=1/2 bytes=4/8", out.getvalue())
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue())
        self.assertNotIn("history/", out.getvalue())
        self.assertNotIn("etag-", out.getvalue())

    def test_invalid_url_is_refused_and_failure_does_not_log_secrets(self):
        for url in ("https://", "http://unit.example", "https://user:pass@unit.example", "https://unit.example/x",
                    "https://unit.example?secret=x", "https://unit.example\n", "https://unit.example:99"):
            err = io.StringIO()
            with patch.dict(backup.os.environ, {"RADAR_DATA_API_URL": url,
                    "RADAR_ARCHIVE_BACKUP_SECRET": SECRET}, clear=True), redirect_stderr(err), \
                    patch.object(backup, "GitHub", side_effect=AssertionError("must fail before GitHub")):
                self.assertEqual(backup.main([]), 1)
            self.assertNotIn(SECRET, err.getvalue())


if __name__ == "__main__":
    unittest.main()
