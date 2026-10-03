#!/usr/bin/env python3
import argparse
import base64
import binascii
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "dashboard"
DATA_DIR = ROOT / "data"
REPORT_JSON_PATH = DATA_DIR / "latest_report.json"
ALERTS_PATH = DATA_DIR / "alerts.jsonl"
STATE_PATH = DATA_DIR / "state.json"
DELETED_TOKENS_PATH = DATA_DIR / "deleted_tokens.json"
SCANNER_STATUS_PATH = DATA_DIR / "scanner_status.json"
SCANNER_PATH = ROOT / "scanner.py"
LANES = {"reactivation"}
CSRF_HEADER = "X-Radar-CSRF"
MAX_JSON_BYTES = 16384

scan_lock = threading.Lock()
deleted_tokens_lock = threading.Lock()
scan_status = {
    "running": False,
    "lane": None,
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "stdout": "",
    "stderr": "",
    "source": None,
    "auto_enabled": True,
    "auto_interval_seconds": 3600,
    "next_scan_at": None,
    "timeout_seconds": 840,
}


def utc_stamp(offset_seconds=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_seconds))


def json_response(handler, status, payload):
    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler, status, text, content_type="text/plain; charset=utf-8"):
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def read_json(path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return fallback


def default_deleted_tokens():
    return {
        "tokens": [],
        "pools": [],
        "entries": {},
        "updated_at": None,
    }


def read_deleted_tokens():
    data = read_json(DELETED_TOKENS_PATH, default_deleted_tokens())
    if not isinstance(data, dict):
        data = default_deleted_tokens()
    data.setdefault("tokens", [])
    data.setdefault("pools", [])
    data.setdefault("entries", {})
    data.setdefault("updated_at", None)
    return data


def read_scan_status():
    persisted = read_json(SCANNER_STATUS_PATH, {})
    with scan_lock:
        runtime = dict(scan_status)
    combined = {**persisted, **runtime}
    if runtime.get("running"):
        combined["status"] = "running"
    else:
        combined["running"] = False
        if persisted.get("status"):
            combined["status"] = persisted["status"]
            combined["last_attempt_at"] = persisted.get("last_attempt_at")
            combined["last_success_at"] = persisted.get("last_success_at")
            combined["error"] = persisted.get("error")
            combined["scan_health"] = persisted.get("scan_health") or {}
            combined["finished_at"] = persisted.get("last_attempt_at") or runtime.get("finished_at")
            combined["returncode"] = 0 if persisted.get("status") == "ok" else 1
    return combined


def normalize_id(value):
    text = str(value or "").strip()
    return text if text else None


def unique_sorted(values):
    return sorted({value for value in (normalize_id(item) for item in values) if value})


def update_deleted_token(payload):
    with deleted_tokens_lock:
        return _update_deleted_token(payload)


def _update_deleted_token(payload):
    action = payload.get("action") or "delete"
    token_address = normalize_id(payload.get("token_address") or payload.get("token_key"))
    pool_address = normalize_id(payload.get("pool_address"))
    if not token_address and not pool_address:
        return False, {"error": "token_address_or_pool_address_required"}

    data = read_deleted_tokens()
    tokens = set(unique_sorted(data.get("tokens", [])))
    pools = set(unique_sorted(data.get("pools", [])))
    entries = data.get("entries") if isinstance(data.get("entries"), dict) else {}
    entry_key = token_address or pool_address

    if action == "restore":
        if token_address:
            tokens.discard(token_address)
        previous = entries.pop(entry_key, None)
        previous_pool = normalize_id(previous.get("pool_address")) if isinstance(previous, dict) else None
        for restored_pool in {pool_address, previous_pool} - {None}:
            if restored_pool not in entries and not any(
                    isinstance(entry, dict) and normalize_id(entry.get("pool_address")) == restored_pool
                    for entry in entries.values()):
                pools.discard(restored_pool)
    elif action == "delete":
        if token_address:
            tokens.add(token_address)
        if pool_address:
            pools.add(pool_address)
        entries[entry_key] = {
            "token_address": token_address,
            "pool_address": pool_address,
            "symbol": payload.get("symbol") or "",
            "name": payload.get("name") or "",
            "deleted_at": utc_stamp(),
        }
    else:
        return False, {"error": "invalid_action", "actions": ["delete", "restore"]}

    data["tokens"] = sorted(tokens)
    data["pools"] = sorted(pools)
    data["entries"] = entries
    data["updated_at"] = utc_stamp()
    DELETED_TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DELETED_TOKENS_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return True, {"ok": True, "deleted_tokens": data}


def read_recent_alerts(limit=100):
    if not ALERTS_PATH.exists():
        return []
    lines = [line for line in ALERTS_PATH.read_text().splitlines() if line.strip()]
    alerts = []
    for line in lines[-limit:]:
        try:
            alerts.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(alerts))


def run_scan(lane, source="manual"):
    started = utc_stamp()
    with scan_lock:
        scan_status.update(
            {
                "running": True,
                "lane": lane,
                "started_at": started,
                "finished_at": None,
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "source": source,
            }
        )
    command = [sys.executable, str(SCANNER_PATH), "--once", "--lane", lane]
    returncode, stdout, stderr = -1, "", "scan failed before completion"
    try:
        timeout_seconds = int(scan_status.get("timeout_seconds") or 840)
        completed = subprocess.run(command, cwd=str(ROOT.parent), capture_output=True, text=True, timeout=timeout_seconds)
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        returncode = -15
        stdout = output_text(exc.stdout)
        stderr = output_text(exc.stderr) + f"\nscan timed out after {timeout_seconds}s"
    except OSError as exc:
        stderr = f"scan could not start ({type(exc).__name__})"
    finally:
        with scan_lock:
            scan_status.update(
                {
                    "running": False,
                    "finished_at": utc_stamp(),
                    "returncode": returncode,
                    "stdout": output_text(stdout)[-8000:],
                    "stderr": output_text(stderr)[-8000:],
                }
            )


def output_text(value):
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


def trigger_scan(lane, source="manual"):
    if lane not in LANES:
        return False, {"error": "invalid_lane", "lanes": sorted(LANES)}
    with scan_lock:
        if scan_status["running"]:
            return False, {"error": "scan_already_running", "scan_status": dict(scan_status)}
        scan_status.update({"running": True, "lane": lane, "source": source})
    try:
        thread = threading.Thread(target=run_scan, args=(lane, source), daemon=True)
        thread.start()
    except RuntimeError:
        with scan_lock:
            scan_status.update(running=False, finished_at=utc_stamp(), returncode=-1,
                               stderr="scan worker could not start")
        return False, {"error": "scan_worker_unavailable"}
    return True, {"ok": True, "scan_status": dict(scan_status)}


def scheduler_loop(interval_seconds, lane):
    while True:
        with scan_lock:
            scan_status["auto_enabled"] = True
            scan_status["auto_interval_seconds"] = interval_seconds
            scan_status["next_scan_at"] = utc_stamp(interval_seconds)
        time.sleep(interval_seconds)
        trigger_scan(lane, source="auto")


def loopback_host(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def authority(value):
    if not value or any(char.isspace() for char in value):
        raise ValueError("invalid authority")
    parsed = urlparse("http://" + value)
    if parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("invalid authority")
    if not parsed.hostname:
        raise ValueError("missing hostname")
    return parsed.hostname.lower(), 80 if parsed.port is None else parsed.port


class RadarHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, *, auth_token=None, allowed_hosts=()):
        host = server_address[0]
        if not loopback_host(host) and not auth_token:
            raise ValueError("Non-loopback serving requires RADAR_AUTH_TOKEN")
        self.auth_token = auth_token
        self.csrf_token = secrets.token_urlsafe(32)
        self.allowed_hosts = {"localhost", "127.0.0.1", "::1"}
        if host not in ("0.0.0.0", "::"):
            self.allowed_hosts.add(host.lower())
        for allowed in allowed_hosts:
            try:
                name = str(ipaddress.ip_address(allowed))
            except ValueError:
                name, _ = authority(allowed)
                if allowed.lower() != name and allowed.lower() != f"[{name}]":
                    raise ValueError("Allowed hosts must be hostnames/IPs without ports")
            self.allowed_hosts.add(name)
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__(server_address, handler_class)


class RadarHandler(BaseHTTPRequestHandler):
    server_version = "SolanaRadar/0.1"

    def log_message(self, fmt, *args):
        return

    def authorize(self, mutation=False):
        try:
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                raise ValueError("invalid host")
            host, port = authority(hosts[0])
            if host not in self.server.allowed_hosts or port != self.server.server_address[1]:
                raise ValueError("untrusted host")
            origins = self.headers.get_all("Origin", [])
            if len(origins) > 1 or (mutation and not origins):
                raise ValueError("missing or ambiguous origin")
            if origins:
                origin = urlparse(origins[0])
                if (origin.scheme != "http" or origin.path or origin.query or origin.fragment
                        or authority(origin.netloc) != (host, port)):
                    raise ValueError("foreign origin")
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise ValueError("cross-site request")
        except ValueError:
            json_response(self, 403, {"error": "untrusted_origin_or_host"})
            return False
        if self.server.auth_token:
            try:
                credentials = self.headers.get_all("Authorization", [])
                if len(credentials) != 1:
                    raise ValueError("missing authentication")
                scheme, encoded = credentials[0].split(" ", 1)
                supplied = base64.b64decode(encoded, validate=True)
                expected = ("radar:" + self.server.auth_token).encode("utf-8")
                authenticated = scheme.lower() == "basic" and secrets.compare_digest(supplied, expected)
            except (ValueError, binascii.Error):
                authenticated = False
            if not authenticated:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Solana Radar", charset="UTF-8"')
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return False
        if mutation:
            content_types = self.headers.get_all("Content-Type", [])
            if len(content_types) != 1 or self.headers.get_content_type() != "application/json":
                json_response(self, 415, {"error": "application_json_required"})
                return False
            tokens = self.headers.get_all(CSRF_HEADER, [])
            if len(tokens) != 1 or not secrets.compare_digest(tokens[0].encode("utf-8"), self.server.csrf_token.encode("ascii")):
                json_response(self, 403, {"error": "invalid_csrf_token"})
                return False
        return True

    def do_GET(self):
        if not self.authorize():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/session":
            json_response(self, 200, {"csrf_token": self.server.csrf_token})
            return
        if parsed.path == "/api/report":
            report = read_json(REPORT_JSON_PATH, {})
            scanner_state = read_json(STATE_PATH, {})
            payload = {
                "report": report,
                "history": read_recent_alerts(),
                "market": scanner_state.get("market", {}),
                "deleted_tokens": read_deleted_tokens(),
                "scan_status": read_scan_status(),
            }
            json_response(self, 200, payload)
            return
        if parsed.path == "/api/status":
            json_response(self, 200, read_scan_status())
            return
        self.serve_static(parsed.path)

    def do_POST(self):
        if not self.authorize(mutation=True):
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/deleted-token":
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid length")
                length = int(lengths[0])
                if not 0 <= length <= MAX_JSON_BYTES:
                    raise ValueError("invalid length")
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("object required")
            except (ValueError, json.JSONDecodeError):
                json_response(self, 400, {"error": "invalid_json"})
                return
            ok, response = update_deleted_token(payload)
            json_response(self, 200 if ok else 400, response)
            return
        if parsed.path != "/api/scan":
            json_response(self, 404, {"error": "not_found"})
            return
        query = parse_qs(parsed.query)
        lane = query.get("lane", query.get("mode", ["reactivation"]))[0]
        ok, payload = trigger_scan(lane, source="manual")
        json_response(self, 202 if ok else 409 if payload.get("error") == "scan_already_running" else 400, payload)

    def serve_static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        if path.startswith("/data/"):
            base_dir = DATA_DIR
            relative_path = path.removeprefix("/data/")
        else:
            base_dir = DASHBOARD_DIR
            relative_path = path.lstrip("/")
        target = (base_dir / relative_path).resolve()
        if not target.is_relative_to(base_dir.resolve()) or not target.exists() or target.is_dir():
            json_response(self, 404, {"error": "not_found"})
            return
        suffix = target.suffix.lower()
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(suffix, "application/octet-stream")
        text_response(self, 200, target.read_text(), content_type)


def main():
    parser = argparse.ArgumentParser(
        description="Local dashboard server for Solana Radar.",
        epilog="Non-loopback serving requires RADAR_AUTH_TOKEN; HTTP Basic username: radar. "
               "This local server does not provide TLS.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allowed-host", action="append", default=[],
                        help="Additional trusted Host hostname/IP, without scheme or port.")
    parser.add_argument("--auto-lane", choices=sorted(LANES), default="reactivation")
    parser.add_argument("--auto-interval-seconds", type=int, default=3600)
    parser.add_argument("--scan-timeout-seconds", type=int, default=840)
    parser.add_argument("--no-auto", action="store_true")
    parser.add_argument("--initial-scan-delay-seconds", type=int, default=5)
    args = parser.parse_args()
    try:
        server = RadarHTTPServer((args.host, args.port), RadarHandler,
                                 auth_token=os.environ.get("RADAR_AUTH_TOKEN"), allowed_hosts=args.allowed_host)
    except ValueError as exc:
        parser.error(str(exc))
    with scan_lock:
        scan_status["auto_enabled"] = not args.no_auto
        scan_status["auto_interval_seconds"] = args.auto_interval_seconds
        scan_status["timeout_seconds"] = args.scan_timeout_seconds
        scan_status["next_scan_at"] = utc_stamp(args.initial_scan_delay_seconds if not args.no_auto else 0)
    if not args.no_auto:
        threading.Timer(args.initial_scan_delay_seconds, lambda: trigger_scan(args.auto_lane, source="auto")).start()
        threading.Thread(target=scheduler_loop, args=(args.auto_interval_seconds, args.auto_lane), daemon=True).start()
    print(f"Solana Radar dashboard: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
