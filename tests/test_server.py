import base64
import http.client
import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class DeletedTokenTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "deleted_tokens.json"
        path_patch = patch.object(server, "DELETED_TOKENS_PATH", self.path)
        path_patch.start()
        self.addCleanup(path_patch.stop)

    def update(self, action="delete", token="token", pool="old-pool"):
        ok, response = server.update_deleted_token({"action": action, "token_address": token, "pool_address": pool})
        self.assertTrue(ok)
        persisted = json.loads(self.path.read_text())
        self.assertEqual(response["deleted_tokens"], persisted)
        return persisted

    def test_restore_token_clears_recorded_old_pool_after_pool_change(self):
        self.update()
        self.update(token="unrelated-token", pool="unrelated-pool")
        restored = self.update("restore", pool="current-pool")
        self.assertEqual(restored["tokens"], ["unrelated-token"])
        self.assertEqual(restored["pools"], ["unrelated-pool"])
        self.assertEqual(set(restored["entries"]), {"unrelated-token"})

    def test_restore_token_without_supplied_pool_clears_recorded_pool(self):
        self.update()
        restored = self.update("restore", pool=None)
        self.assertEqual(restored["tokens"], [])
        self.assertEqual(restored["pools"], [])
        self.assertEqual(restored["entries"], {})

    def test_restore_token_preserves_independent_pool_delete(self):
        for independent_pool in ("old-pool", "current-pool"):
            with self.subTest(independent_pool=independent_pool):
                self.path.unlink(missing_ok=True)
                self.update()
                self.update(token=None, pool=independent_pool)
                restored = self.update("restore", pool="current-pool")
                self.assertEqual(restored["tokens"], [])
                self.assertEqual(restored["pools"], [independent_pool])
                self.assertEqual(set(restored["entries"]), {independent_pool})

    def test_restore_token_preserves_pool_referenced_by_another_token(self):
        self.update()
        self.update(token="other-token")
        restored = self.update("restore", pool="current-pool")
        self.assertEqual(restored["tokens"], ["other-token"])
        self.assertEqual(restored["pools"], ["old-pool"])

    def test_pool_only_restore_still_clears_independent_pool_delete(self):
        self.update(token=None)
        restored = self.update("restore", token=None)
        self.assertEqual(restored["pools"], [])
        self.assertEqual(restored["entries"], {})

    def test_read_modify_write_is_serialized(self):
        original_read = server.read_deleted_tokens
        original_write = Path.write_text

        def read():
            self.assertTrue(server.deleted_tokens_lock.locked())
            return original_read()

        def write(path, *args, **kwargs):
            self.assertTrue(server.deleted_tokens_lock.locked())
            return original_write(path, *args, **kwargs)

        with patch.object(server, "read_deleted_tokens", side_effect=read), patch.object(Path, "write_text", new=write):
            self.update()
            self.update("restore", pool="current-pool")
        self.assertFalse(server.deleted_tokens_lock.locked())

    def test_failed_write_releases_deletion_lock(self):
        with patch.object(Path, "write_text", side_effect=OSError("test write failure")):
            with self.assertRaises(OSError):
                server.update_deleted_token({"token_address": "token", "pool_address": "old-pool"})
        self.assertFalse(server.deleted_tokens_lock.locked())
        self.update()


class ScanRunnerTests(unittest.TestCase):
    def setUp(self):
        self.saved_status = dict(server.scan_status)
        self.addCleanup(self.restore_status)

    def restore_status(self):
        with server.scan_lock:
            server.scan_status.clear()
            server.scan_status.update(self.saved_status)

    def test_timeout_decodes_bytes_and_releases_next_scan(self):
        failure = subprocess.TimeoutExpired(["scanner"], 12, output=b"partial\xff", stderr=b"warning\xff")
        with patch.object(server.subprocess, "run", side_effect=failure):
            server.run_scan("reactivation")
        self.assertFalse(server.scan_status["running"])
        self.assertEqual(server.scan_status["returncode"], -15)
        self.assertEqual(server.scan_status["stdout"], "partial\ufffd")
        self.assertIn("warning\ufffd\nscan timed out", server.scan_status["stderr"])
        self.assertIsNotNone(server.scan_status["finished_at"])
        with patch.object(server.threading, "Thread") as worker:
            self.assertTrue(server.trigger_scan("reactivation")[0])
            worker.return_value.start.assert_called_once()

    def test_timeout_handles_absent_or_text_output(self):
        for stdout, stderr in [(None, None), ("partial", "warning")]:
            with self.subTest(stdout=stdout), patch.object(
                    server.subprocess, "run", side_effect=subprocess.TimeoutExpired("scanner", 12, stdout, stderr)):
                server.run_scan("reactivation")
                self.assertFalse(server.scan_status["running"])
                self.assertIsInstance(server.scan_status["stdout"], str)
                self.assertIsInstance(server.scan_status["stderr"], str)

    def test_process_launch_failure_releases_runner(self):
        with patch.object(server.subprocess, "run", side_effect=FileNotFoundError("private process details")):
            server.run_scan("reactivation")
        self.assertFalse(server.scan_status["running"])
        self.assertEqual(server.scan_status["returncode"], -1)
        self.assertIn("FileNotFoundError", server.scan_status["stderr"])
        self.assertNotIn("private process details", server.scan_status["stderr"])
        self.assertIsNotNone(server.scan_status["finished_at"])

    def test_unexpected_exception_also_releases_runner(self):
        with patch.object(server.subprocess, "run", side_effect=RuntimeError("unexpected")):
            with self.assertRaises(RuntimeError):
                server.run_scan("reactivation")
        self.assertFalse(server.scan_status["running"])
        self.assertEqual(server.scan_status["returncode"], -1)
        self.assertIsNotNone(server.scan_status["finished_at"])

    def test_success_keeps_bounded_output(self):
        result = subprocess.CompletedProcess(["scanner"], 0, "x" * 9000, "y" * 9000)
        with patch.object(server.subprocess, "run", return_value=result):
            server.run_scan("reactivation")
        self.assertFalse(server.scan_status["running"])
        self.assertEqual(server.scan_status["returncode"], 0)
        self.assertEqual(server.scan_status["stdout"], "x" * 8000)
        self.assertEqual(server.scan_status["stderr"], "y" * 8000)

    def test_scan_uses_the_server_interpreter(self):
        result = subprocess.CompletedProcess(["scanner"], 0, "", "")
        with patch.object(server.sys, "executable", "/venv/bin/python"), patch.object(
                server.subprocess, "run", return_value=result) as run:
            server.run_scan("reactivation")
        self.assertEqual(run.call_args.args[0],
                         ["/venv/bin/python", str(server.SCANNER_PATH), "--once", "--lane", "reactivation"])

    def test_thread_start_failure_releases_reservation(self):
        server.scan_status["running"] = False
        with patch.object(server.threading, "Thread") as worker:
            worker.return_value.start.side_effect = RuntimeError("cannot start")
            ok, payload = server.trigger_scan("reactivation")
        self.assertFalse(ok)
        self.assertEqual(payload["error"], "scan_worker_unavailable")
        self.assertFalse(server.scan_status["running"])

    def test_thread_construction_failure_releases_reservation(self):
        server.scan_status["running"] = False
        with patch.object(server.threading, "Thread", side_effect=RuntimeError("cannot create")):
            self.assertFalse(server.trigger_scan("reactivation")[0])
        self.assertFalse(server.scan_status["running"])


class LocalServerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.httpd = self.start_server()
        self.port = self.httpd.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        deletion_patch = patch.object(server, "update_deleted_token", return_value=(True, {"ok": True}))
        scan_patch = patch.object(server, "trigger_scan", return_value=(True, {"ok": True}))
        self.delete = deletion_patch.start()
        self.scan = scan_patch.start()
        self.addCleanup(deletion_patch.stop)
        self.addCleanup(scan_patch.stop)

    def start_server(self, host="127.0.0.1", **kwargs):
        httpd = server.RadarHTTPServer((host, 0), server.RadarHandler, **kwargs)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            httpd.shutdown()
            thread.join(timeout=2)
            httpd.server_close()
            self.assertFalse(thread.is_alive())

        self.addCleanup(stop)
        return httpd

    def request(self, method, path, *, body=None, headers=None, httpd=None):
        connection = http.client.HTTPConnection("127.0.0.1", (httpd or self.httpd).server_address[1], timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), json.loads(data) if data else None
        finally:
            connection.close()

    def mutation_headers(self, **overrides):
        headers = {"Origin": self.origin, "Content-Type": "application/json", server.CSRF_HEADER: self.httpd.csrf_token}
        headers.update(overrides)
        return headers

    def assert_blocked_mutation(self, headers, expected=403, body=b'{}'):
        for path in ("/api/deleted-token", "/api/scan?lane=reactivation"):
            with self.subTest(path=path, headers=list(headers)):
                status, _, _ = self.request("POST", path, headers=headers, body=body)
                self.assertEqual(status, expected)
        self.delete.assert_not_called()
        self.scan.assert_not_called()

    def test_browser_session_and_legitimate_local_actions(self):
        status, headers, session = self.request("GET", "/api/session", headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        csrf = session["csrf_token"]
        self.assertGreaterEqual(len(csrf), 32)
        headers = self.mutation_headers(**{server.CSRF_HEADER: csrf})
        payload = {"action": "delete", "token_address": "test-token"}
        self.assertEqual(self.request("POST", "/api/deleted-token", body=json.dumps(payload), headers=headers)[0], 200)
        self.delete.assert_called_once_with(payload)
        self.assertEqual(self.request("POST", "/api/scan?lane=reactivation", headers=headers)[0], 202)
        self.scan.assert_called_once_with("reactivation", source="manual")

    def test_foreign_origin_is_rejected_even_with_capability(self):
        self.assert_blocked_mutation(self.mutation_headers(Origin="https://attacker.invalid"))

    def test_null_missing_or_wrong_port_origin_is_rejected(self):
        for origin in ("null", "", "http://127.0.0.1:1"):
            self.assert_blocked_mutation(self.mutation_headers(Origin=origin))
        headers = self.mutation_headers()
        headers.pop("Origin")
        self.assert_blocked_mutation(headers)

    def test_foreign_or_wrong_port_host_is_rejected(self):
        for host in (f"attacker.invalid:{self.port}", "127.0.0.1:1", f"user@127.0.0.1:{self.port}"):
            self.assert_blocked_mutation(self.mutation_headers(Host=host))

    def test_duplicate_security_headers_are_rejected(self):
        headers = self.mutation_headers(Host=f"127.0.0.1:{self.port}")
        for duplicate in ("Host", "Origin", "Content-Type", server.CSRF_HEADER):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            try:
                connection.putrequest("POST", "/api/scan", skip_host=True)
                for name, value in headers.items():
                    connection.putheader(name, value)
                connection.putheader(duplicate, headers[duplicate])
                connection.putheader("Content-Length", "0")
                connection.endheaders()
                response = connection.getresponse()
                response.read()
                self.assertIn(response.status, (403, 415))
            finally:
                connection.close()
        self.scan.assert_not_called()
        self.delete.assert_not_called()

    def test_simple_form_or_text_json_is_rejected(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            self.assert_blocked_mutation(self.mutation_headers(**{"Content-Type": content_type}), 415)
        headers = self.mutation_headers()
        headers.pop("Content-Type")
        self.assert_blocked_mutation(headers, 415)

    def test_missing_wrong_or_foreign_session_capability_is_rejected(self):
        other = self.start_server()
        for token in ("", "wrong", other.csrf_token):
            self.assert_blocked_mutation(self.mutation_headers(**{server.CSRF_HEADER: token}))
        headers = self.mutation_headers()
        headers.pop(server.CSRF_HEADER)
        self.assert_blocked_mutation(headers)

    def test_cross_site_session_and_actions_are_rejected(self):
        self.assertEqual(self.request("GET", "/api/session", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("GET", "/api/session", headers={"Origin": "https://attacker.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/api/session", headers={"Host": f"attacker.invalid:{self.port}"})[0], 403)
        self.assert_blocked_mutation(self.mutation_headers(**{"Sec-Fetch-Site": "cross-site"}))

    def test_invalid_or_non_object_json_does_not_mutate(self):
        for body in (b"[1]", b"null", b"invalid", b"\xff"):
            self.assertEqual(self.request("POST", "/api/deleted-token", body=body, headers=self.mutation_headers())[0], 400)
        self.delete.assert_not_called()

    def test_unbounded_or_negative_body_length_does_not_read_or_mutate(self):
        for length in ("-1", str(server.MAX_JSON_BYTES + 1), "invalid"):
            self.assertEqual(self.request("POST", "/api/deleted-token", body=b"", headers=self.mutation_headers(
                **{"Content-Length": length}))[0], 400)
        self.delete.assert_not_called()

    def test_localhost_alias_works_without_login(self):
        headers = self.mutation_headers(Host=f"localhost:{self.port}", Origin=f"http://localhost:{self.port}")
        self.assertEqual(self.request("POST", "/api/scan", headers=headers)[0], 202)

    def test_nonloopback_bind_without_explicit_auth_fails_before_work_starts(self):
        with self.assertRaisesRegex(ValueError, "RADAR_AUTH_TOKEN"):
            server.RadarHTTPServer(("0.0.0.0", 0), server.RadarHandler)
        with patch.dict(server.os.environ, {"RADAR_AUTH_TOKEN": ""}), patch(
                "sys.argv", ["server.py", "--host", "0.0.0.0"]), patch.object(
                server.threading, "Timer") as timer, patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as failure:
                server.main()
            self.assertEqual(failure.exception.code, 2)
            timer.assert_not_called()
        self.scan.assert_not_called()

    def test_nonloopback_static_and_api_access_require_auth_and_csrf(self):
        remote = self.start_server("0.0.0.0", auth_token="test-password", allowed_hosts=["radar.test"])
        port = remote.server_address[1]
        origin = f"http://radar.test:{port}"
        headers = {"Host": f"radar.test:{port}"}
        for path in ("/", "/api/session", "/api/report", "/data/state.json"):
            status, response_headers, _ = self.request("GET", path, headers=headers, httpd=remote)
            self.assertEqual(status, 401)
            self.assertIn("Basic", response_headers["WWW-Authenticate"])
        mutation = dict(headers, Origin=origin, **{"Content-Type": "application/json", server.CSRF_HEADER: remote.csrf_token})
        for path in ("/api/scan", "/api/deleted-token"):
            self.assertEqual(self.request("POST", path, headers=mutation, body=b'{}', httpd=remote)[0], 401)
        self.scan.assert_not_called()
        self.delete.assert_not_called()
        for authorization in ("Basic !!!", "Basic " + base64.b64encode(b"radar:wrong").decode(), "Bearer test-password"):
            self.assertEqual(self.request("GET", "/api/session", headers=dict(headers, Authorization=authorization), httpd=remote)[0], 401)
        auth = "Basic " + base64.b64encode(b"radar:test-password").decode()
        status, _, session = self.request("GET", "/api/session", headers=dict(headers, Authorization=auth), httpd=remote)
        self.assertEqual(status, 200)
        mutation["Authorization"] = auth
        mutation[server.CSRF_HEADER] = "wrong"
        self.assertEqual(self.request("POST", "/api/scan", headers=mutation, httpd=remote)[0], 403)
        self.scan.assert_not_called()
        mutation[server.CSRF_HEADER] = session["csrf_token"]
        self.assertEqual(self.request("POST", "/api/scan", headers=mutation, httpd=remote)[0], 202)
        self.scan.assert_called_once_with("reactivation", source="manual")


if __name__ == "__main__":
    unittest.main()
