"""Offline, lossless D1 SQLite -> a fresh Turso libSQL database migration.

Dry-run is the default. Only --execute contacts/writes the destination; --verify-only
contacts it read-only. All rows, including pending outbox payloads, are preserved.
"""
import argparse
import base64
from contextlib import ExitStack
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import struct
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MIGRATION_NAME = "history/0003_resumable_history.sql"
LEGACY_HISTORY_MIGRATION = "0003_resumable_history.sql"
STORAGE_MIGRATION_NAME = "storage/0001_daily_learning.sql"
STORAGE_MIGRATION_PATH = Path(__file__).resolve().parents[1] / "cloudflare/scan-dispatcher/migrations-storage/0001_daily_learning.sql"
CONTROL_TABLES = {"_radar_import_lock", "_radar_import_guard",
                  "_radar_import_pages", "_radar_import_migrations"}
CONTROL_SQL = (
    "CREATE TABLE _radar_import_lock (id INTEGER PRIMARY KEY CHECK(id=1), "
    "plan_hash TEXT NOT NULL, lock_id TEXT NOT NULL, status TEXT NOT NULL "
    "CHECK(status IN ('importing','verified')), sources_json TEXT NOT NULL)",
    "CREATE TABLE _radar_import_guard (id INTEGER PRIMARY KEY CHECK(id=1), "
    "allowed INTEGER NOT NULL CHECK(allowed=1))",
    "CREATE TABLE _radar_import_pages (page_id TEXT PRIMARY KEY, table_name TEXT NOT NULL, "
    "row_count INTEGER NOT NULL, sha256 TEXT NOT NULL)",
    "CREATE TABLE _radar_import_migrations (source TEXT NOT NULL, name TEXT NOT NULL, "
    "metadata_json TEXT NOT NULL, PRIMARY KEY(source,name))",
)
ADDITIONS = (
    ("wallet_clusters", "active", "INTEGER", 1, "1"),
    ("wallet_clusters", "retired_at", "TEXT", 0, None),
    ("wallet_clusters", "replaced_by", "TEXT", 0, None),
    ("wallet_cluster_edges", "is_infrastructure", "INTEGER", 1, "0"),
    ("signal_outcomes", "numeric_contract_version", "INTEGER", 1, "1"),
    ("signal_outcomes", "entry_verified", "INTEGER", 1, "0"),
    ("signal_wallets", "numeric_contract_version", "INTEGER", 1, "1"),
    ("wallet_observations", "numeric_contract_version", "INTEGER", 1, "1"),
    ("wallet_scores", "numeric_contract_version", "INTEGER", 1, "1"),
    ("market_baselines", "numeric_contract_version", "INTEGER", 1, "1"),
    ("wallet_clusters", "numeric_contract_version", "INTEGER", 1, "1"),
)
NEW_HISTORY_SQL = (
    "CREATE TABLE IF NOT EXISTS history_cluster_work (job_id TEXT NOT NULL, "
    "wallet_address TEXT NOT NULL, component TEXT, visited INTEGER NOT NULL DEFAULT 0, "
    "PRIMARY KEY(job_id,wallet_address))",
    "CREATE INDEX IF NOT EXISTS idx_history_cluster_frontier ON "
    "history_cluster_work(job_id,component,visited,wallet_address)",
    "CREATE TABLE IF NOT EXISTS history_cluster_lock (id INTEGER PRIMARY KEY CHECK(id=1), job_id TEXT)",
    "CREATE TABLE IF NOT EXISTS wallet_observation_bundles (event_id TEXT PRIMARY KEY, "
    "episode_id TEXT NOT NULL, observed_at TEXT NOT NULL, observations_json TEXT NOT NULL, "
    "numeric_contract_version INTEGER NOT NULL DEFAULT 2, "
    "FOREIGN KEY(episode_id) REFERENCES signal_episodes(episode_id))",
    "CREATE INDEX IF NOT EXISTS idx_wallet_observation_bundles_episode ON "
    "wallet_observation_bundles(episode_id,observed_at DESC)",
)


class MigrationError(Exception):
    """Operator-facing error; never includes HTTP bodies, SQL values or credentials."""


class UncertainWrite(MigrationError):
    """A request may have committed. Reconcile receipts; never blindly retry."""


def compact(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"),
                      sort_keys=True, allow_nan=False)


def quote(name):
    if not isinstance(name, str) or "\x00" in name:
        raise MigrationError("invalid SQL identifier")
    return '"' + name.replace('"', '""') + '"'


def typed(value):
    if value is None:
        return ["null"]
    if type(value) is int:
        return ["integer", str(value)]
    if type(value) is float and math.isfinite(value):
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, bytes):
        return ["blob", base64.b64encode(value).decode("ascii")]
    raise MigrationError("unsupported or non-finite SQLite value")


def row_bytes(row):
    return compact([typed(value) for value in row]).encode("utf-8")


def add_digest(digest, row):
    data = row_bytes(row)
    digest.update(struct.pack(">Q", len(data)))
    digest.update(data)


def rows_digest(rows):
    digest = hashlib.sha256()
    for row in rows:
        add_digest(digest, row)
    return digest.hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def private_json(path, data, replace=False):
    path = Path(path)
    if path.is_symlink() or (path.exists() and not replace):
        raise MigrationError("refuse to overwrite an existing report/manifest")
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(compact(data) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temp, path)
        else:
            os.link(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def schema_records(connection, exclude_migrations=False):
    records = [dict(row) for row in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY type,name")]
    return [row for row in records if not exclude_migrations or row["tbl_name"] != "d1_migrations"]


def columns(connection, table):
    return [dict(row) for row in connection.execute("PRAGMA table_xinfo(" + quote(table) + ")")]


@dataclass
class Table:
    name: str
    source: object
    columns: list
    keys: list
    collations: list
    count: int = 0
    sha256: str = ""
    payload_bytes: int = 0
    max_row_bytes: int = 0
    source_name: str = ""
    native_key_collation: bool = True

    @property
    def names(self):
        return [col["name"] for col in self.columns]

    def key(self, row):
        return tuple(row[self.names.index(key)] for key in self.keys)

    def select(self, after=None, through=None, limit=100, source=False, exclude_id=None):
        expressions = [quote(key) + ("" if self.native_key_collation else " COLLATE " + collation)
                       for key, collation in zip(self.keys, self.collations)]
        key_sql = "(" + ",".join(expressions) + ")" if len(expressions) > 1 else expressions[0]
        args, where = [], []
        for bound, operator in ((after, ">"), (through, "<=")):
            if bound is not None:
                placeholders = "(" + ",".join("?" for _ in bound) + ")" if len(bound) > 1 else "?"
                where.append(key_sql + operator + placeholders)
                args.extend(bound)
        if exclude_id is not None:
            where.append('"id"!=?')
            args.append(exclude_id)
        sql = "SELECT " + ",".join(quote(name) for name in self.names) + " FROM " + quote(self.source_name if source and self.source_name else self.name)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY " + ",".join(expression + " ASC" for expression in expressions) + " LIMIT ?"
        return sql, [*args, limit]

    def pages(self, page_size=100):
        after = None
        while True:
            sql, args = self.select(after, limit=page_size, source=True)
            page = [tuple(row) for row in self.source.connection.execute(sql, args)]
            if not page:
                break
            yield page
            after = self.key(page[-1])

    def fingerprint(self):
        return {"columns": self.columns, "keys": self.keys, "collations": self.collations,
                "count": self.count, "sha256": self.sha256}


class Source:
    def __init__(self, role, path):
        self.role, self.path = role, Path(path).resolve(strict=True)
        self.assert_standalone()
        self.stat_signature = self.signature()
        self.sha256 = file_sha(self.path)
        uri = self.path.as_uri() + "?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only=ON")
        self.connection.execute("BEGIN")
        try:
            if [tuple(row) for row in self.connection.execute("PRAGMA integrity_check")] != [("ok",)]:
                raise MigrationError("source SQLite integrity check failed: " + role)
            if self.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise MigrationError("source contains foreign-key violations: " + role)
            self.schema = schema_records(self.connection, exclude_migrations=True)
            if any(row["type"] not in ("table", "index") for row in self.schema):
                raise MigrationError("views/triggers are not supported; do not silently omit them")
            kinds = {row["name"]: row["type"] for row in self.connection.execute("PRAGMA table_list")}
            self.tables = {}
            for entry in self.schema:
                if entry["type"] != "table":
                    continue
                name = entry["name"]
                if name in CONTROL_TABLES or kinds.get(name) != "table":
                    raise MigrationError("reserved, virtual or shadow source table: " + name)
                cols = columns(self.connection, name)
                keys = [col["name"] for col in sorted(cols, key=lambda col: col["pk"]) if col["pk"]]
                if not keys:
                    raise MigrationError("source table has no primary key: " + name)
                if any(col["hidden"] == 1 for col in cols):
                    raise MigrationError("hidden virtual columns are not supported")
                nulls = " OR ".join(quote(key) + " IS NULL" for key in keys)
                if self.connection.execute("SELECT 1 FROM " + quote(name) + " WHERE " + nulls + " LIMIT 1").fetchone():
                    raise MigrationError("nullable primary-key value cannot be paged safely: " + name)
                collations = ["BINARY"] * len(keys)
                for index in self.connection.execute("PRAGMA index_list(" + quote(name) + ")"):
                    if index["origin"] == "pk":
                        info = {row["name"]: row["coll"] for row in self.connection.execute(
                            "PRAGMA index_xinfo(" + quote(index["name"]) + ")") if row["key"]}
                        collations = [info[key].upper() for key in keys]
                if any(collation not in ("BINARY", "NOCASE", "RTRIM") for collation in collations):
                    raise MigrationError("unsupported primary-key collation")
                table = Table(name, self, cols, keys, collations)
                probe = sqlite3.connect(":memory:")
                probe.row_factory = sqlite3.Row
                try:
                    probe.execute(entry["sql"])
                    probe.execute("CREATE INDEX __radar_collation_probe ON " + quote(name) + "(" + ",".join(quote(key) for key in keys) + ")")
                    defaults = [row["coll"].upper() for row in probe.execute(
                        "PRAGMA index_xinfo(__radar_collation_probe)") if row["key"]]
                    table.native_key_collation = defaults == collations
                finally:
                    probe.close()
                digest = hashlib.sha256()
                for page in table.pages():
                    for row in page:
                        add_digest(digest, row)
                        size = len(row_bytes(row))
                        table.count += 1
                        table.payload_bytes += sum(len(value.encode("utf-8")) if isinstance(value, str)
                                                   else len(value) if isinstance(value, bytes) else 0 for value in row)
                        table.max_row_bytes = max(table.max_row_bytes, size)
                table.sha256 = digest.hexdigest()
                actual = self.connection.execute("SELECT COUNT(*) FROM " + quote(name)).fetchone()[0]
                if actual != table.count:
                    raise MigrationError("keyset scan missed source rows: " + name)
                self.tables[name] = table
            self.has_tracking = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='d1_migrations'").fetchone()
            self.migrations = [dict(row) for row in self.connection.execute(
                "SELECT * FROM d1_migrations ORDER BY name,id")] if self.has_tracking else []
            has_sequence = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'").fetchone()
            self.all_sequences = {row["name"]: row["seq"] for row in self.connection.execute(
                "SELECT name,seq FROM sqlite_sequence")} if has_sequence else {}
            self.sequences = {name: seq for name, seq in self.all_sequences.items() if name in self.tables}
            self.assert_unchanged()
        except Exception:
            self.close()
            raise

    def assert_standalone(self):
        if not self.path.is_file():
            raise MigrationError("source is not a regular SQLite file")
        if any(Path(str(self.path) + suffix).exists() for suffix in ("-wal", "-journal")):
            raise MigrationError("source has WAL/journal; provide a quiescent SQLite backup instead")

    def signature(self):
        stat = self.path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def assert_unchanged(self, full=True):
        self.assert_standalone()
        if self.signature() != self.stat_signature or (full and file_sha(self.path) != self.sha256):
            raise MigrationError("source changed since the snapshot was inspected: " + self.role)

    def close(self):
        self.connection.close()


def upgrade_history(connection):
    statements, added = [], []
    names = {row["name"] for row in schema_records(connection) if row["type"] == "table"}
    required = {row[0] for row in ADDITIONS} | {"signal_episodes"}
    if not required.issubset(names):
        raise MigrationError("history export lacks migration 0003 prerequisite tables")
    for table, name, kind, notnull, default in ADDITIONS:
        current = {col["name"]: col for col in columns(connection, table)}
        if name in current:
            col = current[name]
            if (col["type"].upper(), col["notnull"], col["dflt_value"], col["hidden"]) != (kind, notnull, default, 0):
                raise MigrationError("existing history 0003 column has incompatible definition: " + table + "." + name)
            continue
        sql = "ALTER TABLE " + quote(table) + " ADD COLUMN " + quote(name) + " " + kind
        if notnull:
            sql += " NOT NULL"
        if default is not None:
            sql += " DEFAULT " + default
        connection.execute(sql)
        statements.append((sql, []))
        added.append((table, name, int(default) if default is not None else None))
    # Compare existing 0003 objects with a compiled, structured SQLite schema,
    # rather than trusting a migration name or rewriting CREATE SQL text.
    reference = sqlite3.connect(":memory:")
    reference.row_factory = sqlite3.Row
    try:
        for sql in NEW_HISTORY_SQL:
            reference.execute(sql)
        reference_schema = schema_records(reference)
        for expected in sorted(reference_schema, key=lambda row: (row["type"] != "table", row["name"])):
            present = connection.execute("SELECT type FROM sqlite_master WHERE name=?", (expected["name"],)).fetchone()
            if present:
                if present["type"] != expected["type"]:
                    raise MigrationError("history 0003 schema name collision")
                if expected["type"] == "table":
                    if columns(connection, expected["name"]) != columns(reference, expected["name"]):
                        raise MigrationError("incompatible existing history 0003 table")
                    actual_fk = [tuple(row) for row in connection.execute(
                        "PRAGMA foreign_key_list(" + quote(expected["name"]) + ")")]
                    desired_fk = [tuple(row) for row in reference.execute(
                        "PRAGMA foreign_key_list(" + quote(expected["name"]) + ")")]
                    if actual_fk != desired_fk:
                        raise MigrationError("incompatible existing history 0003 foreign keys")
                else:
                    actual = [tuple(row) for row in connection.execute("PRAGMA index_xinfo(" + quote(expected["name"]) + ")")]
                    desired = [tuple(row) for row in reference.execute("PRAGMA index_xinfo(" + quote(expected["name"]) + ")")]
                    if actual != desired:
                        raise MigrationError("incompatible existing history 0003 index")
            else:
                connection.execute(expected["sql"])
                statements.append((expected["sql"], []))
    finally:
        reference.close()
    statements.append(("INSERT OR IGNORE INTO history_cluster_lock(id) VALUES(1)", []))
    connection.execute(statements[-1][0])
    return statements, added


def upgrade_storage(connection):
    reference = sqlite3.connect(":memory:")
    reference.row_factory = sqlite3.Row
    statements = []
    sql = STORAGE_MIGRATION_PATH.read_text(encoding="utf-8")
    try:
        reference.executescript(sql)
        desired = schema_records(reference)
        if any(row["type"] not in ("table", "index") or row["name"] in CONTROL_TABLES for row in desired):
            raise MigrationError("storage migration must be additive ordinary tables/indexes")
        for expected in sorted(desired, key=lambda row: (row["type"] != "table", row["name"])):
            present = connection.execute("SELECT type,tbl_name FROM sqlite_master WHERE name=?", [expected["name"]]).fetchone()
            if present:
                if present["type"] != expected["type"] or present["tbl_name"] != expected["tbl_name"]:
                    raise MigrationError("storage migration object collision")
                pragma = "table_xinfo" if expected["type"] == "table" else "index_xinfo"
                actual = [tuple(row) for row in connection.execute("PRAGMA " + pragma + "(" + quote(expected["name"]) + ")")]
                wanted = [tuple(row) for row in reference.execute("PRAGMA " + pragma + "(" + quote(expected["name"]) + ")")]
                if actual != wanted:
                    raise MigrationError("existing daily Learning schema is incompatible")
            else:
                connection.execute(expected["sql"])
                statements.append((expected["sql"], []))
    finally:
        reference.close()
    return statements, hashlib.sha256(sql.encode("utf-8")).hexdigest()


class Plan:
    def __init__(self, sources):
        self.sources, self.tables, objects = sources, {}, {}
        self.sequences = {}
        for source in sources:
            for name, table in source.tables.items():
                if name in self.tables and self.tables[name].fingerprint() != table.fingerprint():
                    raise MigrationError("conflicting table across source databases: " + name)
                self.tables.setdefault(name, table)
            for row in source.schema:
                name = row["name"]
                if name in objects and objects[name] != row:
                    raise MigrationError("cross-source table/index name collision: " + name)
                objects[name] = row
            for name, sequence in source.sequences.items():
                if name in self.sequences and self.sequences[name] != sequence:
                    raise MigrationError("conflicting autoincrement sequence across sources")
                self.sequences[name] = sequence
            if source.has_tracking:
                tracking_name = "ops_d1_migrations" if source.role == "op" else "d1_migrations"
                if tracking_name in self.tables or tracking_name in objects:
                    raise MigrationError("migration provenance table name collision")
                tracking_sql = source.connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='d1_migrations'").fetchone()[0]
                tracking_shadow = sqlite3.connect(":memory:")
                tracking_shadow.row_factory = sqlite3.Row
                try:
                    tracking_shadow.execute(tracking_sql)
                    if tracking_name != "d1_migrations":
                        tracking_shadow.execute("ALTER TABLE d1_migrations RENAME TO " + quote(tracking_name))
                    for entry in schema_records(tracking_shadow):
                        objects[entry["name"]] = entry
                finally:
                    tracking_shadow.close()
                table = Table(tracking_name, source, columns(source.connection, "d1_migrations"),
                              ["id"], ["BINARY"], source_name="d1_migrations")
                digest = hashlib.sha256()
                for page in table.pages():
                    for row in page:
                        add_digest(digest, row)
                        table.count += 1
                        table.payload_bytes += sum(len(value.encode()) for value in row if isinstance(value, str))
                        table.max_row_bytes = max(table.max_row_bytes, len(row_bytes(row)))
                table.sha256 = digest.hexdigest()
                self.tables[tracking_name] = table
                if "d1_migrations" in source.all_sequences:
                    self.sequences[tracking_name] = source.all_sequences["d1_migrations"]
        self.base_schema = sorted(objects.values(), key=lambda row: (row["type"] != "table", row["name"]))
        shadow = sqlite3.connect(":memory:")
        shadow.row_factory = sqlite3.Row
        try:
            # The sqlite_master definitions are parsed by SQLite itself; execute()
            # rejects multiple statements and we never substitute identifiers in DDL.
            for row in self.base_schema:
                shadow.execute(row["sql"])
            self.base_schema = schema_records(shadow)
            if any(source.role == "history" for source in sources):
                self.upgrade, self.added_columns = upgrade_history(shadow)
            else:
                self.upgrade, self.added_columns = [], []
            self.history_statement_count = len(self.upgrade)
            storage_upgrade, self.storage_migration_sha256 = upgrade_storage(shadow)
            self.upgrade += storage_upgrade
            self.final_schema = schema_records(shadow)
            self.final_columns = {row["name"]: columns(shadow, row["name"])
                                  for row in self.final_schema if row["type"] == "table"}
        finally:
            shadow.close()
        self.identity = {
            "version": VERSION,
            "sources": {source.role: {"sha256": source.sha256, "migrations": source.migrations}
                        for source in sources},
            "tables": {name: table.fingerprint() for name, table in sorted(self.tables.items())},
            "schema": self.base_schema, "final_schema": self.final_schema,
            "sequences": self.sequences,
            "storage_migration_sha256": self.storage_migration_sha256,
        }
        self.sha256 = hashlib.sha256(compact(self.identity).encode()).hexdigest()

    def assert_sources(self, full=True):
        for source in self.sources:
            source.assert_unchanged(full=full)

    def report(self):
        return {"version": VERSION, "plan_sha256": self.sha256, "raw_payload_policy": "preserve_all",
                "canonical_row_contract": "typed-json-length-prefix-v1",
                "sources": {source.role: {"path": str(source.path), "file_sha256": source.sha256,
                            "bytes": source.path.stat().st_size, "migrations": source.migrations}
                            for source in self.sources},
                "tables": {name: {"rows": table.count, "sha256": table.sha256,
                           "payload_bytes": table.payload_bytes, "max_canonical_row_bytes": table.max_row_bytes,
                           "primary_key": table.keys} for name, table in sorted(self.tables.items())},
                "total_rows": sum(table.count for table in self.tables.values()),
                "total_payload_bytes": sum(table.payload_bytes for table in self.tables.values()),
                "history_0003_statements": self.history_statement_count,
                "storage_0001_statements": len(self.upgrade) - self.history_statement_count,
                "storage_migration": STORAGE_MIGRATION_NAME,
                "storage_migration_sha256": self.storage_migration_sha256,
                "history_0003_columns_added": [table + "." + name for table, name, _ in self.added_columns]}

    def upgrade_statements(self):
        statements = list(self.upgrade)
        table = self.tables.get("d1_migrations")
        history = next((source for source in self.sources if source.role == "history"), None)
        needs_marker = (table is not None and history is not None
                        and not any(row.get("name") == LEGACY_HISTORY_MIGRATION for row in history.migrations))
        if needs_marker:
            if table.names != ["id", "name", "applied_at"]:
                raise MigrationError("unsupported history migration tracking columns")
            statements += [
                ("INSERT INTO d1_migrations(name) VALUES(?)", [LEGACY_HISTORY_MIGRATION]),
                ("INSERT INTO _radar_import_migrations(source,name,metadata_json) "
                 "SELECT 'canonical',?,json_object('plan_sha256',?,'history_migration',"
                 "json_object('id',id,'name',name,'applied_at',applied_at)) FROM d1_migrations WHERE name=?",
                 [MIGRATION_NAME, self.sha256, LEGACY_HISTORY_MIGRATION]),
            ]
        else:
            statements.append(("INSERT INTO _radar_import_migrations VALUES('canonical',?,?)",
                               [MIGRATION_NAME, compact({"plan_sha256": self.sha256})]))
        statements.append(("INSERT INTO _radar_import_migrations VALUES('canonical',?,?)",
                           [STORAGE_MIGRATION_NAME, compact({"plan_sha256": self.sha256,
                                                           "file_sha256": self.storage_migration_sha256})]))
        return statements


def turso_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme not in ("libsql", "https") or parsed.username or parsed.password or parsed.port
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){1,4}turso\.io", host)
                or any(char.isspace() for char in value)):
            raise ValueError
    except ValueError:
        raise MigrationError("TURSO_DATABASE_URL must be a libsql/HTTPS *.turso.io host without credentials, port or path") from None
    return "https://" + host + "/v2/pipeline"


def encode_value(value):
    result = typed(value)
    if result[0] == "null":
        return {"type": "null"}
    if result[0] == "blob":
        return {"type": "blob", "base64": result[1]}
    return {"type": result[0], "value": value if result[0] == "float" else result[1]}


def decode_value(value):
    kind = value["type"]
    if kind == "null":
        return None
    if kind == "integer":
        return int(value["value"])
    if kind == "float":
        number = float(value["value"])
        if not math.isfinite(number):
            raise MigrationError("non-finite destination value")
        return number
    if kind == "text":
        return value["value"]
    if kind == "blob":
        return base64.b64decode(value["base64"], validate=True)
    raise MigrationError("unsupported Hrana value type")


def statement(sql, args=(), want_rows=True):
    return {"sql": sql, "args": [encode_value(value) for value in args], "want_rows": want_rows}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise MigrationError("HTTP redirect refused; credentials were not forwarded")


def atomic_payload(statements):
    steps = [{"stmt": statement("BEGIN IMMEDIATE", want_rows=False)}]
    for sql, args in statements:
        steps.append({"condition": {"type": "ok", "step": len(steps) - 1},
                      "stmt": statement(sql, args, False)})
    commit = len(steps)
    steps.append({"condition": {"type": "ok", "step": commit - 1},
                  "stmt": statement("COMMIT", want_rows=False)})
    steps.append({"condition": {"type": "and", "conds": [
        {"type": "ok", "step": 0}, {"type": "not", "cond": {"type": "ok", "step": commit}}]},
        "stmt": statement("ROLLBACK", want_rows=False)})
    return {"requests": [{"type": "execute", "stmt": statement("PRAGMA foreign_keys=OFF", want_rows=False)},
                         {"type": "batch", "batch": {"steps": steps}}, {"type": "close"}]}, commit


class Hrana:
    def __init__(self, url, token, timeout=30, opener=None):
        self.url = turso_url(url)
        if not token or any(char.isspace() for char in token):
            raise MigrationError("missing or invalid TURSO_AUTH_TOKEN")
        self._token, self.timeout = token, timeout
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def request(self, payload, write=False):
        encoded = compact(payload).encode()
        if len(encoded) > MAX_REQUEST_BYTES:
            raise MigrationError("Hrana request exceeds the bounded request limit")
        request = urllib.request.Request(self.url, encoded,
                                         {"Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError
            body = json.loads(raw)
            results = body["results"]
            if not isinstance(results, list) or len(results) != len(payload["requests"]):
                raise ValueError
            if any(row.get("type") != "ok" for row in results):
                raise ValueError
            return [row["response"] for row in results]
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, MigrationError):
            cls = UncertainWrite if write else MigrationError
            raise cls("destination request failed or returned an invalid response; no automatic retry") from None

    def query(self, sql, args=()):
        responses = self.request({"requests": [{"type": "execute", "stmt": statement(sql, args)}, {"type": "close"}]})
        try:
            result = responses[0]["result"]
            names = [col["name"] for col in result["cols"]]
            return [dict(zip(names, [decode_value(value) for value in row], strict=True)) for row in result["rows"]]
        except (KeyError, TypeError, ValueError):
            raise MigrationError("invalid destination query result") from None

    def atomic(self, statements):
        payload, commit = atomic_payload(statements)
        result = self.request(payload, write=True)[1].get("result", {})
        successes, errors = result.get("step_results"), result.get("step_errors")
        if (not isinstance(successes, list) or not isinstance(errors, list)
                or len(successes) != commit + 2 or len(errors) != commit + 2):
            raise UncertainWrite("incomplete transaction response; reconcile before restarting")
        if any(errors[:commit + 1]) or any(row is None for row in successes[:commit + 1]):
            raise MigrationError("transaction was not fully committed; no blind retry or overwrite")


class Manifest:
    def __init__(self, path, plan_hash, target_hash, lock_id):
        self.path = Path(path)
        self.lock_file = None
        if self.path.is_symlink() or Path(str(self.path) + ".lock").is_symlink():
            raise MigrationError("manifest/lock symlinks are refused")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock_file = os.fdopen(fd, "a+")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            raise MigrationError("another migration owns the local manifest lock") from None
        identity = {"version": VERSION, "plan_sha256": plan_hash, "target_sha256": target_hash, "lock_id": lock_id}
        try:
            if self.path.exists():
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                if any(self.data.get(key) != value for key, value in identity.items()):
                    raise MigrationError("manifest belongs to different sources, destination or cutover lock")
            else:
                self.data = {**identity, "tables": {}, "status": "planned"}
                self.save()
        except Exception:
            self.close()
            raise

    def save(self):
        private_json(self.path, self.data, replace=True)

    def close(self):
        if self.lock_file and not self.lock_file.closed:
            self.lock_file.close()


class Migrator:
    def __init__(self, plan, client, manifest, lock_id, batch_rows=100, batch_bytes=512 * 1024):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", lock_id):
            raise MigrationError("cutover lock must be an 8-128 character operator identifier, not a credential")
        if not 1 <= batch_rows <= 200 or not 32768 <= batch_bytes <= MAX_REQUEST_BYTES:
            raise MigrationError("batch rows must be 1-200 and batch bytes 32768-1048576")
        self.plan, self.client, self.manifest, self.lock_id = plan, client, manifest, lock_id
        self.batch_rows, self.batch_bytes = batch_rows, batch_bytes
        if manifest is not None:
            settings = {"batch_rows": batch_rows, "batch_bytes": batch_bytes}
            recorded = manifest.data.get("settings")
            if recorded is not None and recorded != settings:
                raise MigrationError("resume requires the original batch size/byte limits")
            manifest.data["settings"] = settings
            manifest.data["source_sha256"] = {source.role: source.sha256 for source in plan.sources}
            manifest.data["table_sha256"] = {name: table.sha256 for name, table in plan.tables.items()}
            manifest.save()

    def marker(self):
        exists = self.client.query("SELECT name FROM sqlite_master WHERE type='table' AND name='_radar_import_lock'")
        if not exists:
            return None
        rows = self.client.query("SELECT * FROM _radar_import_lock")
        if (len(rows) != 1 or rows[0].get("id") != 1 or rows[0].get("plan_hash") != self.plan.sha256
                or rows[0].get("lock_id") != self.lock_id
                or rows[0].get("sources_json") != compact(self.plan.identity["sources"])):
            raise MigrationError("destination cutover marker does not match this import")
        if rows[0].get("status") not in ("importing", "verified"):
            raise MigrationError("destination is live/unlocked; offline migration refuses to write")
        return rows[0]

    def guard(self):
        return ("INSERT INTO _radar_import_guard(id,allowed) VALUES(1,COALESCE((SELECT 1 FROM "
                "_radar_import_lock WHERE id=1 AND plan_hash=? AND lock_id=? AND status='importing'),0)) "
                "ON CONFLICT(id) DO UPDATE SET allowed=excluded.allowed", [self.plan.sha256, self.lock_id])

    def upgraded(self):
        if not self.plan.upgrade:
            return False
        rows = self.client.query("SELECT name FROM _radar_import_migrations WHERE source='canonical'")
        return {MIGRATION_NAME, STORAGE_MIGRATION_NAME}.issubset({row["name"] for row in rows})

    def derived_tracking(self):
        if not self.plan.upgrade:
            return None
        records = self.client.query("SELECT metadata_json FROM _radar_import_migrations WHERE source='canonical' AND name=?", [MIGRATION_NAME])
        if not records:
            return None
        try:
            metadata = json.loads(records[0]["metadata_json"])
            if metadata.get("plan_sha256") != self.plan.sha256:
                raise ValueError
            row = metadata.get("history_migration")
            if row is not None:
                if (set(row) != {"id", "name", "applied_at"} or type(row["id"]) is not int
                        or row["name"] != LEGACY_HISTORY_MIGRATION or not isinstance(row["applied_at"], str)):
                    raise ValueError
                if self.client.query("SELECT * FROM d1_migrations WHERE id=?", [row["id"]]) != [row]:
                    raise ValueError
            return row
        except (ValueError, KeyError, TypeError):
            raise MigrationError("derived history migration marker does not match its immutable receipt") from None

    def verify_schema(self):
        rows = self.client.query("SELECT type,name,tbl_name,sql FROM sqlite_master "
                                 "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY type,name")
        actual = {row["name"]: row for row in rows if row["name"] not in CONTROL_TABLES and row["tbl_name"] not in CONTROL_TABLES}
        expected = {row["name"]: row for row in (self.plan.final_schema if self.upgraded() else self.plan.base_schema)}
        if actual != expected:
            raise MigrationError("destination schema/index definitions differ from the validated import plan")

    def initialize(self):
        self.plan.assert_sources()
        marker = self.marker()
        if marker is None:
            existing = self.client.query("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")
            if existing:
                raise MigrationError("destination is not a fresh empty database; live-row overwrite is forbidden")
            statements = [(sql, []) for sql in CONTROL_SQL]
            statements.append(("INSERT INTO _radar_import_lock VALUES(1,?,?, 'importing',?)",
                               [self.plan.sha256, self.lock_id, compact(self.plan.identity["sources"])]))
            statements += [(row["sql"], []) for row in self.plan.base_schema if row["type"] == "table"]
            statements += [(row["sql"], []) for row in self.plan.base_schema if row["type"] == "index" and row["sql"]]
            for source in self.plan.sources:
                for migration in source.migrations:
                    name = migration.get("name")
                    if not isinstance(name, str):
                        raise MigrationError("invalid source migration tracking row")
                    statements.append(("INSERT INTO _radar_import_migrations VALUES(?,?,?)",
                                       [source.role, name, compact(migration)]))
            try:
                self.client.atomic(statements)
            except UncertainWrite:
                if self.marker() is None:
                    raise UncertainWrite("schema initialization outcome is unknown; stop and restart for reconciliation") from None
        self.verify_schema()
        self.manifest.data["status"] = "importing"
        self.manifest.save()

    def insert_statements(self, table, rows):
        writable = [(index, col["name"]) for index, col in enumerate(table.columns) if col["hidden"] == 0]
        sql = "INSERT INTO " + quote(table.name) + "(" + ",".join(quote(name) for _, name in writable) + ") VALUES(" + ",".join("?" for _ in writable) + ")"
        return [(sql, [row[index] for index, _ in writable]) for row in rows]

    def receipt(self, table, rows):
        sha = rows_digest(rows)
        identity = [self.plan.sha256, table.name, [typed(value) for value in table.key(rows[0])],
                    [typed(value) for value in table.key(rows[-1])], len(rows), sha]
        return {"page_id": hashlib.sha256(compact(identity).encode()).hexdigest(),
                "table_name": table.name, "row_count": len(rows), "sha256": sha}

    def page_statements(self, table, rows):
        receipt = self.receipt(table, rows)
        return [self.guard(), *self.insert_statements(table, rows),
                ("INSERT INTO _radar_import_pages VALUES(?,?,?,?)", list(receipt.values()))]

    def bounded_pages(self, table):
        def split(rows):
            payload, _ = atomic_payload(self.page_statements(table, rows))
            if len(compact(payload).encode()) <= self.batch_bytes:
                yield rows
            elif len(rows) == 1:
                raise MigrationError("single source row exceeds the bounded import request limit: " + table.name)
            else:
                middle = len(rows) // 2
                yield from split(rows[:middle])
                yield from split(rows[middle:])
        for rows in table.pages(self.batch_rows):
            yield from split(rows)

    def read_page(self, table, after, through, limit, exclude_id=None):
        sql, args = table.select(after, through, limit, exclude_id=exclude_id)
        return [tuple(row[name] for name in table.names) for row in self.client.query(sql, args)]

    def prove_page(self, table, rows, after):
        actual = self.read_page(table, after, table.key(rows[-1]), len(rows) + 1)
        if len(actual) != len(rows) or rows_digest(actual) != rows_digest(rows):
            raise MigrationError("destination page count/content mismatch: " + table.name)

    def import_table(self, table):
        if self.manifest.data["tables"].get(table.name, {}).get("complete"):
            self.verify_table(table)
            return
        after = None
        for rows in self.bounded_pages(table):
            expected = self.receipt(table, rows)
            receipts = self.client.query("SELECT * FROM _radar_import_pages WHERE page_id=?", [expected["page_id"]])
            if receipts:
                if receipts != [expected]:
                    raise MigrationError("destination page receipt differs from the source")
            else:
                # A missing receipt does not justify overwriting any existing data.
                if self.read_page(table, after, table.key(rows[-1]), 1):
                    raise MigrationError("unreceipted destination rows exist; refuse to overwrite: " + table.name)
                self.plan.assert_sources(full=False)
                try:
                    self.client.atomic(self.page_statements(table, rows))
                except UncertainWrite:
                    receipts = self.client.query("SELECT * FROM _radar_import_pages WHERE page_id=?", [expected["page_id"]])
                    if receipts != [expected]:
                        raise UncertainWrite("page commit is unproven; stop and restart, never replay blindly") from None
            self.prove_page(table, rows, after)
            after = table.key(rows[-1])
            self.manifest.data["tables"][table.name] = {
                "last_verified_key": [typed(value) for value in after], "complete": False}
            self.manifest.save()
        self.verify_table(table)
        self.manifest.data["tables"][table.name] = {"complete": True, "rows": table.count, "sha256": table.sha256}
        self.manifest.save()

    def verify_table(self, table):
        count = self.client.query("SELECT COUNT(*) AS n FROM " + quote(table.name))[0]["n"]
        derived = self.derived_tracking() if table.name == "d1_migrations" else None
        digest, seen, after = hashlib.sha256(), 0, None
        while True:
            safe_rows = min(self.batch_rows, max(1, MAX_RESPONSE_BYTES // (2 * (table.max_row_bytes + 2048))))
            rows = self.read_page(table, after, None, safe_rows, exclude_id=derived["id"] if derived else None)
            if not rows:
                break
            for row in rows:
                add_digest(digest, row)
                seen += 1
            after = table.key(rows[-1])
        if count != table.count + bool(derived) or seen != table.count or digest.hexdigest() != table.sha256:
            raise MigrationError("destination full-table count/digest mismatch: " + table.name)

    def verify(self):
        self.plan.assert_sources()
        if self.marker() is None:
            raise MigrationError("destination lacks this migration's cutover marker")
        self.verify_schema()
        for table in self.plan.tables.values():
            self.verify_table(table)
        expected_migrations = {(source.role, row["name"], compact(row)) for source in self.plan.sources for row in source.migrations}
        actual_migrations = {(row["source"], row["name"], row["metadata_json"]) for row in self.client.query(
            "SELECT * FROM _radar_import_migrations WHERE source!='canonical'")}
        if actual_migrations != expected_migrations:
            raise MigrationError("source migration provenance does not match")
        if self.plan.sequences:
            actual = {row["name"]: row["seq"] for row in self.client.query("SELECT name,seq FROM sqlite_sequence")}
            derived = self.derived_tracking()
            if any(actual.get(name) != seq + (1 if name == "d1_migrations" and derived else 0)
                   for name, seq in self.plan.sequences.items()):
                raise MigrationError("autoincrement sequence mismatch")
        if self.upgraded():
            for table, name, value in self.plan.added_columns:
                wrong = self.client.query("SELECT COUNT(*) AS n FROM " + quote(table) + " WHERE " + quote(name) + " IS NOT ?", [value])[0]["n"]
                if wrong:
                    raise MigrationError("migration 0003 default-column verification failed")
            for entry in self.plan.final_schema:
                name = entry["name"]
                if entry["type"] == "table" and name not in self.plan.tables:
                    desired = [{"id": 1, "job_id": None}] if name == "history_cluster_lock" else []
                    if self.client.query("SELECT * FROM " + quote(name)) != desired:
                        raise MigrationError("new migration table contents differ")
            storage = self.client.query("SELECT metadata_json FROM _radar_import_migrations WHERE source='canonical' AND name=?", [STORAGE_MIGRATION_NAME])
            if storage != [{"metadata_json": compact({"plan_sha256": self.plan.sha256,
                                                       "file_sha256": self.plan.storage_migration_sha256})}]:
                raise MigrationError("daily Learning migration receipt mismatch")
        if self.client.query("PRAGMA foreign_key_check"):
            raise MigrationError("destination foreign-key verification failed")
        integrity = self.client.query("PRAGMA integrity_check")
        if len(integrity) != 1 or list(integrity[0].values()) != ["ok"]:
            raise MigrationError("destination integrity check failed")
        self.plan.assert_sources()
        return {"verified_tables": len(self.plan.tables), "verified_rows": sum(t.count for t in self.plan.tables.values()),
                "history_0003_ready": self.upgraded() if self.plan.upgrade else None,
                "storage_0001_ready": self.upgraded(),
                "legacy_history_0003_marker": bool(self.client.query("SELECT name FROM d1_migrations WHERE name=?", [LEGACY_HISTORY_MIGRATION])) if "d1_migrations" in self.plan.tables else None,
                "writes_remain_locked": True, "source_sha256_unchanged": True}

    def run(self):
        self.initialize()
        if self.marker()["status"] == "verified":
            result = self.verify()
            self.manifest.data["status"] = "verified"
            self.manifest.save()
            return result
        for table in self.plan.tables.values():
            self.import_table(table)
        if self.plan.sequences:
            statements = [self.guard()]
            for name, seq in self.plan.sequences.items():
                statements += [("DELETE FROM sqlite_sequence WHERE name=?", [name]),
                               ("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", [name, seq])]
            self.client.atomic(statements)
        # Full import proof precedes any additive changes. Recheck actual schema
        # and source fingerprints on every restart, even if the manifest says done.
        self.verify()
        if self.plan.upgrade and not self.upgraded():
            self.client.atomic([self.guard(), *self.plan.upgrade_statements()])
        result = self.verify()
        self.client.atomic([self.guard(), ("UPDATE _radar_import_lock SET status='verified' WHERE id=1", [])])
        self.manifest.data["status"] = "verified"
        self.manifest.save()
        return result


class LocalDatabase:
    """Same verification interface as Hrana, without any credentials/network."""
    def __init__(self, connection):
        self.connection = connection

    def query(self, sql, args=()):
        return [dict(row) for row in self.connection.execute(sql, args)]


def build_local(plan, destination, lock_id, batch_rows=1000):
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise MigrationError("local destination already exists; never overwrite an export")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", lock_id):
        raise MigrationError("local build requires a valid --cutover-lock identifier")
    plan.assert_sources()
    fd, temporary = tempfile.mkstemp(prefix=".radar-import-", suffix=".sqlite", dir=destination.parent)
    os.close(fd)
    connection = sqlite3.connect(temporary)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        for sql in CONTROL_SQL:
            connection.execute(sql)
        connection.execute("INSERT INTO _radar_import_lock VALUES(1,?,?,'importing',?)",
                           [plan.sha256, lock_id, compact(plan.identity["sources"])])
        for row in plan.base_schema:
            if row["type"] == "table":
                connection.execute(row["sql"])
        for table in plan.tables.values():
            writable = [(index, col["name"]) for index, col in enumerate(table.columns) if col["hidden"] == 0]
            sql = "INSERT INTO " + quote(table.name) + "(" + ",".join(quote(name) for _, name in writable) + ") VALUES(" + ",".join("?" for _ in writable) + ")"
            for page in table.pages(batch_rows):
                connection.executemany(sql, [tuple(row[index] for index, _ in writable) for row in page])
        for row in plan.base_schema:
            if row["type"] == "index" and row["sql"]:
                connection.execute(row["sql"])
        for source in plan.sources:
            for migration in source.migrations:
                connection.execute("INSERT INTO _radar_import_migrations VALUES(?,?,?)",
                                   [source.role, migration["name"], compact(migration)])
        for name, seq in plan.sequences.items():
            connection.execute("DELETE FROM sqlite_sequence WHERE name=?", [name])
            connection.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", [name, seq])
        if plan.upgrade:
            for sql, args in plan.upgrade_statements():
                connection.execute(sql, args)
        verifier = Migrator(plan, LocalDatabase(connection), None, lock_id)
        result = verifier.verify()
        connection.execute("UPDATE _radar_import_lock SET status='verified' WHERE id=1")
        connection.commit()
        # Verify committed reads too. The output is published only after both proofs.
        result = verifier.verify()
        connection.close()
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        plan.assert_sources()
        os.link(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {**result, "local_path": str(destination), "file_sha256": file_sha(destination),
                "bytes": destination.stat().st_size, "journal_mode": "delete", "destination_contacted": False}
    finally:
        connection.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            if os.path.exists(temporary + suffix):
                os.unlink(temporary + suffix)


def verify_local(plan, path, lock_id):
    path = Path(path).resolve(strict=True)
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise MigrationError("verification export has WAL/journal; provide a standalone snapshot")
    before = file_sha(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        verifier = Migrator(plan, LocalDatabase(connection), None, lock_id)
        result = verifier.verify()
        if file_sha(path) != before:
            raise MigrationError("verification export changed during the comparison")
        return {**result, "destination_file_sha256": before, "destination_contacted": False}
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-op", type=Path, required=True)
    parser.add_argument("--source-history", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    mode.add_argument("--build-local", type=Path, metavar="SQLITE_PATH",
                      help="Build a verified single SQLite file for official Turso CLI import (no network)")
    mode.add_argument("--verify-local", type=Path, metavar="SQLITE_PATH",
                      help="Verify a standalone destination/Turso export fully offline, without writes")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--cutover-lock")
    parser.add_argument("--writers-paused", action="store_true",
                        help="Operator confirms all writers to the destination are stopped")
    parser.add_argument("--batch-rows", type=int, default=100)
    parser.add_argument("--batch-bytes", type=int, default=512 * 1024)
    args = parser.parse_args(argv)
    try:
        with ExitStack() as stack:
            sources = []
            for role, path in (("op", args.source_op), ("history", args.source_history)):
                source = Source(role, path)
                sources.append(source)
                stack.callback(source.close)
            plan = Plan(sources)
            report = plan.report()
            if args.verify_local:
                lock_id = args.cutover_lock or "radar-storage-" + plan.sha256[:16]
                report["mode"] = "verify_local"
                report["destination_contacted"] = False
                report["verification"] = verify_local(plan, args.verify_local, lock_id)
            elif args.build_local:
                if args.manifest and (args.manifest.exists() or args.manifest.is_symlink()
                                      or args.manifest.absolute() == args.build_local.absolute()):
                    raise MigrationError("local build report path exists or conflicts with the output")
                lock_id = args.cutover_lock or "radar-storage-" + plan.sha256[:16]
                report["mode"] = "build_local"
                report["destination_contacted"] = False
                report["cutover_lock"] = lock_id
                report["verification"] = build_local(plan, args.build_local, lock_id)
                if args.manifest:
                    private_json(args.manifest, report)
            elif not args.execute and not args.verify_only:
                report["mode"] = "dry_run"
                report["destination_contacted"] = False
            else:
                if args.execute and (not args.cutover_lock or not args.writers_paused):
                    raise MigrationError("execute requires --writers-paused and --cutover-lock")
                client = Hrana(os.environ.get("TURSO_DATABASE_URL", ""), os.environ.get("TURSO_AUTH_TOKEN", ""))
                lock_id = args.cutover_lock or "radar-storage-" + plan.sha256[:16]
                manifest = None
                if args.execute:
                    manifest = Manifest(args.manifest or args.source_op.with_suffix(".storage-import.json"),
                                        plan.sha256, hashlib.sha256(client.url.encode()).hexdigest(), lock_id)
                    stack.callback(manifest.close)
                migrator = Migrator(plan, client, manifest, lock_id, args.batch_rows, args.batch_bytes)
                report["mode"] = "verify_only" if args.verify_only else "execute"
                report["destination_contacted"] = True
                report["verification"] = migrator.verify() if args.verify_only else migrator.run()
            print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False))
        return 0
    except (MigrationError, sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
        # Third-party/library error strings can contain payloads or credentials.
        message = str(error) if isinstance(error, MigrationError) else "local snapshot/manifest validation failed"
        print("Storage migration stopped: " + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
