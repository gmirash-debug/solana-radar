"""Build a fresh SQLite seed without carrying old trading data or refunding RPC use."""
import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
COUNTERS = ("estimated_units", "attempts", "allocated_units", "allocations")


def merge_ledger(target, incoming):
    import re
    if not isinstance(incoming, dict):
        raise ValueError("RPC ledger must be a mapping")
    for period, providers in incoming.items():
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period) or not isinstance(providers, dict):
            raise ValueError("Invalid RPC ledger period")
        for provider, entry in providers.items():
            if not isinstance(provider, str) or not provider or not isinstance(entry, dict):
                raise ValueError("Invalid RPC ledger provider")
            current = target.setdefault(period, {}).setdefault(provider, {})
            for counter in COUNTERS:
                value = entry.get(counter, 0)
                if type(value) is not int or not 0 <= value <= 2**53 - 1:
                    raise ValueError("Invalid RPC ledger counter")
                current[counter] = max(current.get(counter, 0), value)
    return target


def report_ledger(report, now):
    observed = datetime.fromisoformat(str(report.get("generated_at", "")).replace("Z", "+00:00"))
    if observed.tzinfo is None or not 0 <= (now - observed).total_seconds() <= 86400:
        raise ValueError("A fresh report is required to preserve RPC use")
    ledger = {}
    providers = (report.get("stats") or {}).get("rpc_providers") or {}
    for provider in ("helius", "alchemy", "chainstack"):
        usage = (providers.get(provider) or {}).get("monthly_usage")
        if not isinstance(usage, dict) or usage.get("provider") != provider or usage.get("period") != now.strftime("%Y-%m"):
            raise ValueError("Current paid-provider usage is missing")
        merge_ledger(ledger, {usage["period"]: {provider: usage}})
    return ledger


def insert_document(db, name, value, now):
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False)
    timestamp = now.isoformat().replace("+00:00", "Z")
    db.execute("""INSERT INTO runtime_sql_documents
        (name,payload_json,payload_sha256,updated_at,source_ms,revision,bytes,touched_at)
        VALUES (?,?,?,?,?,?,?,?)""", (name, payload, hashlib.sha256(payload.encode()).hexdigest(),
        timestamp, int(now.timestamp()*1000), 1, len(payload.encode()), int(now.timestamp()*1000)))


def build_seed(source, output, epoch, report, now=None, root=ROOT):
    import re
    now = now or datetime.now(timezone.utc)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", epoch):
        raise ValueError("Invalid storage epoch")
    if output.exists():
        raise ValueError("Refusing to replace an existing seed")
    ledger = report_ledger(report, now)
    with sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro&immutable=1", uri=True) as old:
        row = old.execute("SELECT payload_json FROM runtime_sql_documents WHERE name='rpc_ledger'").fetchone()
        if row:
            merge_ledger(ledger, json.loads(row[0]).get("ledger", {}))
        exclusions = old.execute("SELECT * FROM deleted_tokens").fetchall()
        exclusion_columns = [row[1] for row in old.execute("PRAGMA table_info(deleted_tokens)")]
    output.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(output) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""CREATE TABLE d1_migrations
            (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE NOT NULL,applied_at TEXT);
            CREATE TABLE ops_d1_migrations
            (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE NOT NULL,applied_at TEXT);""")
        for directory, marker in (("migrations", "ops_d1_migrations"),
                                  ("migrations-history", "d1_migrations"),
                                  ("migrations-storage", None)):
            for migration in sorted((root / "cloudflare/scan-dispatcher" / directory).glob("*.sql")):
                db.executescript(migration.read_text())
                if marker:
                    db.execute(f"INSERT INTO {marker}(name,applied_at) VALUES (?,?)",
                               (migration.name, now.isoformat()))
        # A clean generation must never sweep the old DO queue back into SQL.
        db.execute("UPDATE history_sql_queue_meta SET legacy_complete=1 WHERE id=1")
        insert_document(db, "rpc_ledger", {"version": 1, "ledger": ledger}, now)
        insert_document(db, "storage_epoch", {"id": epoch, "reset_at": now.isoformat(),
                                             "protocol_version": 1}, now)
        if exclusions:
            columns = ",".join(f'"{column}"' for column in exclusion_columns)
            db.executemany(f"INSERT INTO deleted_tokens({columns}) VALUES ({','.join('?' for _ in exclusion_columns)})", exclusions)
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Fresh seed violates foreign keys")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Fresh seed is not valid SQLite")
    return {"epoch": epoch, "rpc_periods": sorted(ledger), "manual_exclusions": len(exclusions),
            "bytes": output.stat().st_size, "trading_rows": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--epoch", required=True)
    parser.add_argument("--public-fallback", type=Path)
    args = parser.parse_args()
    url = "https://gmirash-debug.github.io/solana-radar/data/dashboard_fallback.json"
    with urlopen(url, timeout=30) as response:
        body = response.read(16*1024*1024+1)
    if len(body) > 16*1024*1024:
        raise ValueError("Report exceeds the reset safety bound")
    report = json.loads(body)["report"]
    result=build_seed(args.source, args.output, args.epoch, report)
    if args.public_fallback:
        now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        config=json.loads((ROOT/"config.example.json").read_text())
        lane={**config,**config["lanes"]["reactivation"],"lane":"reactivation"}
        health={"status":"unavailable","reasons":["Clean storage restart; Turso quota unblock pending"]}
        snapshot={"schema_version":1,"storage_epoch":args.epoch,"generated_at":now,
                  "report":{"generated_at":now,"scan_profile":"storage_reset","config":lane,
                            "lanes_scanned":[],"alerts":[],"signal_theses":[],"summaries":[],
                            "universe":[],"active_pools":[],"stats":{"scan_health":health}},
                  "history":[],"market":{},"deleted_tokens":{},"discovery_status":{},
                  "scan_status":{"status":"maintenance","running":False,"last_success_at":None,
                                 "error":"Waiting for Turso free-account quota unblock","scan_health":health}}
        args.public_fallback.parent.mkdir(parents=True,exist_ok=True)
        args.public_fallback.write_text(json.dumps(snapshot,separators=(",",":"),allow_nan=False)+"\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
