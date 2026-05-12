#!/usr/bin/env python3
import argparse
import json
import subprocess
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
SCANNER_PATH = ROOT / "scanner.py"
LANES = {"all", "incubation", "young", "breakout", "reactivation"}

scan_lock = threading.Lock()
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
    command = ["python3", str(SCANNER_PATH), "--once", "--lane", lane]
    timeout_seconds = int(scan_status.get("timeout_seconds") or 840)
    try:
        completed = subprocess.run(command, cwd=str(ROOT.parent), capture_output=True, text=True, timeout=timeout_seconds)
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        returncode = -15
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + f"\nscan timed out after {timeout_seconds}s"
    finished = utc_stamp()
    with scan_lock:
        scan_status.update(
            {
                "running": False,
                "finished_at": finished,
                "returncode": returncode,
                "stdout": stdout[-8000:],
                "stderr": stderr[-8000:],
            }
        )


def trigger_scan(lane, source="manual"):
    if lane not in LANES:
        return False, {"error": "invalid_lane", "lanes": sorted(LANES)}
    with scan_lock:
        if scan_status["running"]:
            return False, {"error": "scan_already_running", "scan_status": dict(scan_status)}
        scan_status.update({"running": True, "lane": lane, "source": source})
    thread = threading.Thread(target=run_scan, args=(lane, source), daemon=True)
    thread.start()
    return True, {"ok": True, "scan_status": dict(scan_status)}


def scheduler_loop(interval_seconds, lane):
    while True:
        with scan_lock:
            scan_status["auto_enabled"] = True
            scan_status["auto_interval_seconds"] = interval_seconds
            scan_status["next_scan_at"] = utc_stamp(interval_seconds)
        time.sleep(interval_seconds)
        trigger_scan(lane, source="auto")


class RadarHandler(BaseHTTPRequestHandler):
    server_version = "SolanaRadar/0.1"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/report":
            report = read_json(REPORT_JSON_PATH, {})
            scanner_state = read_json(STATE_PATH, {})
            payload = {
                "report": report,
                "history": read_recent_alerts(),
                "market": scanner_state.get("market", {}),
                "scan_status": dict(scan_status),
            }
            json_response(self, 200, payload)
            return
        if parsed.path == "/api/status":
            json_response(self, 200, dict(scan_status))
            return
        self.serve_static(parsed.path)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/scan":
            json_response(self, 404, {"error": "not_found"})
            return
        query = parse_qs(parsed.query)
        lane = query.get("lane", query.get("mode", ["all"]))[0]
        ok, payload = trigger_scan(lane, source="manual")
        json_response(self, 202 if ok else 409 if payload.get("error") == "scan_already_running" else 400, payload)

    def serve_static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        target = (DASHBOARD_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(DASHBOARD_DIR.resolve())) or not target.exists() or target.is_dir():
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
    parser = argparse.ArgumentParser(description="Local dashboard server for Solana Radar.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--auto-lane", choices=sorted(LANES), default="all")
    parser.add_argument("--auto-interval-seconds", type=int, default=3600)
    parser.add_argument("--scan-timeout-seconds", type=int, default=840)
    parser.add_argument("--no-auto", action="store_true")
    parser.add_argument("--initial-scan-delay-seconds", type=int, default=5)
    args = parser.parse_args()
    with scan_lock:
        scan_status["auto_enabled"] = not args.no_auto
        scan_status["auto_interval_seconds"] = args.auto_interval_seconds
        scan_status["timeout_seconds"] = args.scan_timeout_seconds
        scan_status["next_scan_at"] = utc_stamp(args.initial_scan_delay_seconds if not args.no_auto else 0)
    if not args.no_auto:
        threading.Timer(args.initial_scan_delay_seconds, lambda: trigger_scan(args.auto_lane, source="auto")).start()
        threading.Thread(target=scheduler_loop, args=(args.auto_interval_seconds, args.auto_lane), daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), RadarHandler)
    print(f"Solana Radar dashboard: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
