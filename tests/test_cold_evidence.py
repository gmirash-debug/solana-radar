import copy
import gzip
import hashlib
import json
import unittest
from unittest.mock import Mock, patch

import cold_evidence


AT = "2026-10-04T12:00:00Z"
URL = "https://radar.example.invalid"
SECRET = "private-ingest-secret"
EPOCH = "configured-generation"


def payload():
    return {"version": 1, "token_address": "token", "pool_address": "pool", "cohort_id": "cohort",
            "signal_at": AT, "evidence": {"cohort": [{"owner": "holder", "balance": "42"}],
                                        "history": {"complete": False}, "label": "\u0430\u0440\u0445\u0438\u0432"}}


def reference(data):
    digest = hashlib.sha256(data).hexdigest()
    return {"key": f"evidence/v1/sha256/{digest[:2]}/{digest}.json.gz", "sha256": digest, "bytes": len(data)}


def encoded(body):
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    return gzip.compress(raw, mtime=0)


def read_response(data, ref=None, chunks=None):
    ref = ref or reference(data)
    return Mock(ok=True, status_code=200,
                headers={"content-length": str(ref["bytes"]), "x-radar-sha256": ref["sha256"]},
                iter_content=Mock(return_value=iter(chunks if chunks is not None else [data])))


class ColdEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.no_post = patch.object(cold_evidence.requests, "post", side_effect=AssertionError("live POST forbidden"))
        self.no_get = patch.object(cold_evidence.requests, "get", side_effect=AssertionError("live GET forbidden"))
        self.no_post.start()
        self.no_get.start()
        self.addCleanup(self.no_post.stop)
        self.addCleanup(self.no_get.stop)
        self.session = Mock()

        def post(*args, **kwargs):
            ref = reference(kwargs["data"])
            return Mock(ok=True, status_code=200,
                        json=Mock(return_value={"ok": True, "accepted": True, "id": ref["sha256"], "archive_ref": ref}))

        self.session.post.side_effect = post

    def archive(self, body=None, **kwargs):
        return cold_evidence.archive_evidence(payload() if body is None else body, URL, SECRET, AT,
                                             session=self.session, epoch=EPOCH, **kwargs)

    def read(self, data, ref=None, **kwargs):
        ref = ref or reference(data)
        self.session.get.return_value = read_response(data, ref)
        return cold_evidence.read_evidence(ref, URL, SECRET, session=self.session, epoch=EPOCH, **kwargs)

    def test_canonical_gzip_roundtrip_is_stable_and_never_changes_original(self):
        body = payload()
        original = copy.deepcopy(body)
        ref = self.archive(body)
        sent = self.session.post.call_args.kwargs["data"]
        self.assertEqual(reference(sent), ref)
        self.assertEqual(gzip.decompress(sent), json.dumps(body, sort_keys=True, separators=(",", ":"),
                                                         ensure_ascii=True).encode())
        self.assertEqual(sent[4:8], b"\x00\x00\x00\x00")
        self.assertEqual(sent[9], 255)
        reordered = dict(reversed(list(body.items())))
        reordered["evidence"] = dict(reversed(list(body["evidence"].items())))
        self.assertEqual(self.archive(reordered), ref)
        self.assertEqual(self.read(sent, ref), body)
        self.assertEqual(body, original)
        self.session.patch.assert_not_called()
        self.session.delete.assert_not_called()

    def test_missing_version_is_added_without_mutating_the_payload(self):
        body = payload()
        del body["version"]
        self.archive(body)
        snapshot = json.loads(gzip.decompress(self.session.post.call_args.kwargs["data"]))
        self.assertEqual(snapshot["version"], 1)
        self.assertNotIn("version", body)

    def test_transport_carries_source_time_and_explicit_epoch_without_falling_back(self):
        with patch.object(cold_evidence, "storage_epoch", side_effect=AssertionError("wrong default epoch")):
            ref = self.archive(timeout=7)
            sent = self.session.post.call_args
            self.assertEqual(sent.args, (URL + "/api/storage/evidence-archive",))
            self.assertEqual(sent.kwargs["params"], {"id": ref["sha256"]})
            self.assertEqual(sent.kwargs["headers"], {"x-radar-ingest-secret": SECRET,
                "x-radar-storage-epoch": EPOCH, "content-type": "application/gzip", "x-radar-generated-at": AT})
            self.assertEqual(sent.kwargs["timeout"], 7)
            self.assertIs(sent.kwargs["allow_redirects"], False)
            self.read(sent.kwargs["data"], ref, timeout=9)
            read = self.session.get.call_args.kwargs
            self.assertEqual(read["headers"]["x-radar-storage-epoch"], EPOCH)
            self.assertEqual(read["params"], {"id": ref["sha256"]})
            self.assertTrue(read["stream"])
            self.assertFalse(read["allow_redirects"])
            self.assertEqual(read["timeout"], 9)

    def test_default_epoch_is_imported_for_both_helpers_only_when_not_passed(self):
        data = encoded(payload())
        self.session.get.return_value = read_response(data)
        with patch.object(cold_evidence, "storage_epoch", return_value="default-epoch") as default:
            cold_evidence.archive_evidence(payload(), URL, SECRET, AT, session=self.session)
            cold_evidence.read_evidence(reference(data), URL, SECRET, session=self.session)
        self.assertEqual(default.call_count, 2)
        self.assertEqual(self.session.post.call_args.kwargs["headers"]["x-radar-storage-epoch"], "default-epoch")
        self.assertEqual(self.session.get.call_args.kwargs["headers"]["x-radar-storage-epoch"], "default-epoch")

    def test_only_an_exact_accepted_receipt_returns_an_archive_reference(self):
        self.archive()
        data = self.session.post.call_args.kwargs["data"]
        ref = reference(data)
        good = {"ok": True, "accepted": True, "id": ref["sha256"], "archive_ref": ref}
        cases = [{**good, "ok": False}, {**good, "accepted": False}, {**good, "accepted": 1},
                 {**good, "id": "0" * 64}, {}, [], None]
        for change in ({"sha256": "0" * 64}, {"bytes": ref["bytes"] + 1}, {"bytes": True},
                       {"bytes": float(ref["bytes"])}, {"key": ref["key"].replace("evidence", "outbox")},
                       {"key": ref["key"] + "/../other"}, {"sha256": ref["sha256"] + "\n"}):
            cases.append({**good, "archive_ref": {**ref, **change}})
        body = payload()
        before = copy.deepcopy(body)
        self.session.post.side_effect = None
        for result in cases:
            with self.subTest(result=result):
                response = Mock(ok=True, status_code=200, json=Mock(return_value=result))
                self.session.post.return_value = response
                with self.assertRaisesRegex(RuntimeError, "original retained"):
                    self.archive(body)
                response.close.assert_called_once()
                self.assertEqual(body, before)

    def test_pause_http_redirect_timeout_and_manifest_errors_are_sanitized(self):
        body = payload()
        before = copy.deepcopy(body)
        for response in [Mock(ok=False, status_code=503), Mock(ok=True, status_code=302),
                         Mock(ok=True, status_code=200, json=Mock(side_effect=ValueError(SECRET + URL)))]:
            self.session.post.side_effect = None
            self.session.post.return_value = response
            with self.assertRaises(RuntimeError) as error:
                self.archive(body)
            self.assertNotIn(SECRET, str(error.exception))
            self.assertNotIn(URL, str(error.exception))
            self.assertIsNone(error.exception.__cause__)
            self.assertEqual(body, before)
        self.session.post.side_effect = RuntimeError(SECRET + " network error " + URL)
        with self.assertRaisesRegex(RuntimeError, "unverified or unavailable; original retained"):
            self.archive(body)
        self.assertEqual(body, before)

    def test_invalid_configuration_and_reference_never_send_credentials(self):
        for url in ["http://radar.example.invalid", "https://", "https:///missing", "https://user:pass@radar.example.invalid",
                    URL + "?id=other", URL + "#fragment", URL + "\\private", URL + "/space here",
                    URL + ":bad", URL + ":0"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                cold_evidence.archive_evidence(payload(), url, SECRET, AT, session=self.session, epoch=EPOCH)
        for epoch in ["", "with space", "stale\n", 3, "a" * 65]:
            with self.subTest(epoch=epoch), self.assertRaises(ValueError):
                cold_evidence.archive_evidence(payload(), URL, SECRET, AT, session=self.session, epoch=epoch)
        for secret in ["", " bad", "private\nsecret", None]:
            with self.subTest(secret=secret), self.assertRaises(ValueError):
                cold_evidence.archive_evidence(payload(), URL, secret, AT, session=self.session, epoch=EPOCH)
        ref = reference(encoded(payload()))
        for change in [{"key": "../../private"}, {"key": ref["key"].replace("evidence", "outbox")},
                       {"key": ref["key"].replace(ref["sha256"][:2], "xx", 1)}, {"sha256": ref["sha256"].upper()},
                       {"sha256": ref["sha256"] + "\n"}, {"bytes": 0}, {"bytes": True},
                       {"bytes": cold_evidence.MAX_BYTES + 1}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                cold_evidence.read_evidence({**ref, **change}, URL, SECRET, session=self.session, epoch=EPOCH)
        self.session.post.assert_not_called()
        self.session.get.assert_not_called()

    def test_schema_and_identity_validation_rejects_ambiguous_snapshots_before_post(self):
        cases = [[], {**payload(), "version": True}, {**payload(), "version": 2}, {**payload(), "evidence": []},
                 {**payload(), "extra": "secret"}, {**payload(), "token_address": ""},
                 {**payload(), "pool_address": "pool\nother"}, {**payload(), "cohort_id": " cohort"},
                 {**payload(), "signal_at": "2026-02-31T12:00:00Z"},
                 {**payload(), "evidence": {"token_address": "different-token"}},
                 {**payload(), "evidence": {"signal_at": "2026-10-01T12:00:00Z"}},
                 {**payload(), "evidence": {"not_json": (1, 2)}},
                 {**payload(), "evidence": {1: "ambiguous key"}},
                 {**payload(), "evidence": {"value": float("nan")}},
                 {**payload(), "evidence": {"value": float("inf")}}]
        missing = payload()
        del missing["cohort_id"]
        cases.append(missing)
        for body in cases:
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.archive(body)
        for at in ["", "later", "2026-10-04T12:00:00", "2026-02-31T12:00:00Z"]:
            with self.subTest(at=at), self.assertRaises(ValueError):
                cold_evidence.archive_evidence(payload(), URL, SECRET, at, session=self.session, epoch=EPOCH)
        self.session.post.assert_not_called()

    def test_decoded_and_compressed_archive_limits_fail_without_modifying_original(self):
        body = payload()
        before = copy.deepcopy(body)
        with patch.object(cold_evidence, "MAX_DECODED_BYTES", 32):
            with self.assertRaisesRegex(ValueError, "decoded capacity"):
                self.archive(body)
        with patch.object(cold_evidence, "MAX_BYTES", 32):
            with self.assertRaisesRegex(ValueError, "compressed capacity"):
                self.archive(body)
        self.assertEqual(body, before)
        self.session.post.assert_not_called()

    def test_read_hash_length_gzip_schema_and_identity_failures_preserve_the_reference(self):
        good = encoded(payload())
        ref = reference(good)
        before = copy.deepcopy(ref)
        self.session.get.return_value = read_response(good[:-1], ref)
        with self.assertRaises(RuntimeError):
            cold_evidence.read_evidence(ref, URL, SECRET, session=self.session, epoch=EPOCH)
        changed = bytearray(good)
        changed[10] ^= 1
        self.session.get.return_value = read_response(bytes(changed), ref)
        with self.assertRaises(RuntimeError):
            cold_evidence.read_evidence(ref, URL, SECRET, session=self.session, epoch=EPOCH)
        self.assertEqual(ref, before)
        invalid = [b"not gzip", good[:-5], encoded([]), encoded({**payload(), "version": True}),
                   encoded({**payload(), "evidence": []}), encoded({**payload(), "token_address": ""}),
                   encoded({**payload(), "evidence": {"pool_address": "different-pool"}}),
                   gzip.compress(b"{not JSON", mtime=0), gzip.compress(b"\xff", mtime=0),
                   gzip.compress(json.dumps(payload(), indent=2).encode(), mtime=0)]
        for data in invalid:
            with self.subTest(data=data[:20]), self.assertRaises(RuntimeError):
                self.read(data)
            self.session.get.return_value.close.assert_called_once()

    def test_read_rejects_duplicate_json_keys_and_nonfinite_values_even_with_valid_hash(self):
        raw = json.dumps(payload(), sort_keys=True, separators=(",", ":"))
        for value in [raw.replace('"version":1', '"version":1,"version":1'),
                      raw.replace('"complete":false', '"complete":NaN')]:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.read(gzip.compress(value.encode(), mtime=0))

    def test_read_decoding_is_bounded_and_exact_capacity_is_allowed(self):
        body = payload()
        body["evidence"]["repeated"] = "x" * 4096
        data = encoded(body)
        length = len(gzip.decompress(data))
        with patch.object(cold_evidence, "MAX_DECODED_BYTES", length):
            self.assertEqual(self.read(data), body)
        with patch.object(cold_evidence, "MAX_DECODED_BYTES", length - 1):
            with self.assertRaises(RuntimeError):
                self.read(data)
        self.session.get.return_value.close.assert_called_once()

    def test_stream_overflow_is_stopped_and_responses_are_closed(self):
        data = encoded(payload())
        ref = reference(data)
        visited = []

        def chunks():
            visited.append(1)
            yield data
            visited.append(2)
            yield b"overflow"
            visited.append(3)
            raise AssertionError("must stop on overflow")

        self.session.get.return_value = read_response(data, chunks=chunks())
        with self.assertRaises(RuntimeError):
            cold_evidence.read_evidence(ref, URL, SECRET, session=self.session, epoch=EPOCH)
        self.assertEqual(visited, [1, 2])
        self.session.get.return_value.close.assert_called_once()

    def test_read_pauses_metadata_failures_and_private_network_errors_are_sanitized(self):
        data = encoded(payload())
        ref = reference(data)
        for changes in [{"x-radar-sha256": "0" * 64}, {"content-length": str(len(data) + 1)},
                        {"content-encoding": "gzip"}]:
            response = read_response(data)
            response.headers.update(changes)
            self.session.get.return_value = response
            with self.assertRaises(RuntimeError):
                cold_evidence.read_evidence(ref, URL, SECRET, session=self.session, epoch=EPOCH)
            response.iter_content.assert_not_called()
            response.close.assert_called_once()
        for status in [503, 404, 302, 206]:
            response = read_response(data)
            response.status_code = status
            self.session.get.return_value = response
            with self.assertRaises(RuntimeError):
                cold_evidence.read_evidence(ref, URL, SECRET, session=self.session, epoch=EPOCH)
            response.iter_content.assert_not_called()
        self.session.get.side_effect = RuntimeError(SECRET + " detailed network error " + URL)
        with self.assertRaises(RuntimeError) as error:
            cold_evidence.read_evidence(ref, URL, SECRET, session=self.session, epoch=EPOCH)
        self.assertNotIn(SECRET, str(error.exception))
        self.assertNotIn(URL, str(error.exception))


if __name__ == "__main__":
    unittest.main()
