"""Read-only, consistent Turso -> verified SQLite -> private GitHub Release.

Only Python's standard library is used; no Cloudflare, scanner or RPC requests.
Coverage is the complete Turso SQL schema and table contents, including stored
references to external objects. Referenced R2 evidence/blob contents are NOT
copied; this is a SQL recovery point, not a backup of all external archives.
An interrupted read transaction is discarded, never resumed against a new
snapshot. Native exports are not assumed complete, so this uses a single Hrana
v3 read transaction with autocommit guards for every page instead.

Private-repository installation (no scanner checkout or dependencies required):
  tools/storage_backup.py
  tests/test_storage_backup.py
  .github/workflows/storage-backup.yml (from ops/storage-backup-workflow.yml)
Configure repository variable TURSO_DATABASE_URL and secret TURSO_AUTH_TOKEN,
containing a read-only database token, not an account-wide token. The workflow
maps that secret to TURSO_BACKUP_AUTH_TOKEN and uses its own GITHUB_TOKEN for
private Releases. Python 3.12 and trusted HTTPS CA roots are sufficient.

Run/verify:
  python tools/storage_backup.py backup --repository OWNER/PRIVATE_REPO --publish --keep 7 --max-seconds 1000
  python tools/storage_backup.py verify radar.sqlite.gz manifest.json
For local tests, set SSL_CERT_FILE to a trusted CA file if the Python runtime
lacks system trust roots; never bypass TLS verification. A non-published local
backup requires --output pointing to an empty private directory.

The 1000-second workflow budget includes export, full local verification, gzip
restoration verification and upload. Requests have at most 20-second timeouts.
The job is capped at 20 minutes; configurable tool budgets cannot exceed 1080
seconds, preserving setup/termination headroom. Runtime on the complete remote
dataset must be measured before declaring the daily recovery path ready.
Payload groups target 8 MiB; size planning reads at most 1000 upcoming keys,
not a whole-table MAX. Its CPU time is still subject to the request timeout.
Expired streams, transport errors and interrupted reads are never replayed in
the old read transaction. Private publication may restart one transient failure
from a completely new snapshot, within the original total deadline. Local
output and non-transient failures still require a new run.
Only checksum-verified replacements may rotate the seven retained recovery
points. A verified draft after a publication failure remains recoverable;
unverified/uncertain drafts never replace previous verified backups.

The manifest contains complete stable-order per-table row counts and lossless
typed SHA-256 digests (including hidden rowids), a schema fingerprint and raw/
compressed file hashes. sqlite_fingerprints(), compare_fingerprints() and
validate_sqlite() are reusable for original-vs-restored SQL verification.
Schema/table names and fingerprints stay in the private release manifest.
Logs show only numeric progress and allowlisted failure reasons.
"""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib


VERSION = 1
MAX_BACKUP_SECONDS = 1080
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MAX_ASSET_BYTES = 1900 * 1024 * 1024
MAX_TABLES = 1000
MAX_SCHEMA_BYTES = 4 * 1024 * 1024
MAX_PAGE_BYTES = 8 * 1024 * 1024
RELEASE_PREFIX = "storage-backup-"
RELEASE_MARKER = "Verified private Turso backup; format=storage-backup-v1"
SCHEMA_SQL = "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
SAFE_FAILURE_MESSAGES = frozenset({
    "backup deadline exhausted; previous backups retained",
    "database snapshot request failed; incomplete backup discarded",
    "database response exceeds the bounded size limit",
    "invalid JSON database response",
    "unsupported database transfer encoding",
    "invalid compressed database response",
    "invalid database pipeline response",
    "database returned a SQL/protocol failure",
    "database snapshot baton missing or invalid",
    "database sticky URL refused; credentials not forwarded",
    "database authentication rejected",
    "database rate limit reached",
    "database HTTP request failed",
    "database request timed out; read snapshot discarded",
    "read snapshot was lost; restart the entire backup",
    "read transaction was not established",
    "read transaction expired; incomplete backup discarded",
    "invalid typed database response",
    "invalid database rows",
    "invalid database schema",
    "invalid database table metrics",
    "invalid byte-budget pagination metadata",
    "single database row exceeds bounded export capacity",
    "payload rows differ from the byte-budgeted keys",
    "invalid paginated database rows",
    "database pagination did not advance",
    "snapshot row count differs from the read transaction",
    "SQLite snapshot construction or verification failed",
    "full SQLite schema/row-count/digest verification failed",
    "SQLite integrity or foreign-key verification failed",
    "backup archive SHA-256 or manifest verification failed",
    "restored SQLite SHA-256 verification failed",
    "private GitHub backup request failed",
    "private backup upload or remote SHA-256 verification failed",
    "backup repository must be private; refusing to upload",
})


class BackupError(Exception):
    """Fixed operator-facing messages only; no remote bodies or credentials."""


class BackupTLSCertificateError(BackupError):
    def __init__(self):
        super().__init__("TLS certificate verification failed; configure a trusted CA bundle with SSL_CERT_FILE")


class Deadline:
    def __init__(self, seconds=1000, clock=time.monotonic):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 1 <= seconds <= MAX_BACKUP_SECONDS:
            raise BackupError("backup deadline must be between 1 and 1080 seconds")
        self.clock = clock
        self.end = clock() + seconds

    def check(self):
        if self.clock() >= self.end:
            raise BackupError("backup deadline exhausted; previous backups retained")

    def timeout(self):
        self.check()
        return max(0.01, min(20, self.end - self.clock()))


def compact(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def quote(name):
    if not isinstance(name, str) or not name or "\0" in name:
        raise BackupError("invalid SQLite identifier")
    return '"' + name.replace('"', '""') + '"'


def typed(value):
    if value is None:
        return ["null"]
    if type(value) is int and -(1 << 63) <= value < (1 << 63):
        return ["integer", str(value)]
    if type(value) is float and math.isfinite(value):
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, bytes):
        return ["blob", base64.b64encode(value).decode("ascii")]
    raise BackupError("unsupported or non-finite SQLite value")


def row_bytes(row):
    return compact([typed(value) for value in row]).encode("utf-8")


def add_digest(digest, row):
    data = row_bytes(row)
    digest.update(struct.pack(">Q", len(data)))
    digest.update(data)


def rows_digest(rows):
    """Same lossless framing as the migration tool; streams rows, no sampling."""
    digest = hashlib.sha256()
    for row in rows:
        add_digest(digest, row)
    return digest.hexdigest()


def file_sha(path, deadline=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            if deadline:
                deadline.check()
            digest.update(block)
    return digest.hexdigest()


def encode(value):
    kind = typed(value)
    if kind[0] == "null":
        return {"type":"null"}
    if kind[0] == "blob":
        return {"type":"blob", "base64":kind[1]}
    return {"type":kind[0], "value":value if kind[0] == "float" else kind[1]}


def decode(value):
    try:
        kind = value["type"]
        if kind == "null":
            return None
        if kind == "integer" and isinstance(value["value"], str) and re.fullmatch(r"-?(0|[1-9]\d*)", value["value"]):
            number = int(value["value"])
            if -(1 << 63) <= number < (1 << 63):
                return number
        if kind == "float" and type(value["value"]) in (int, float) and math.isfinite(value["value"]):
            return float(value["value"])
        if kind == "text" and isinstance(value["value"], str):
            return value["value"]
        if kind == "blob" and isinstance(value["base64"], str):
            return base64.b64decode(value["base64"], validate=True)
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    raise BackupError("invalid typed database response")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        raise BackupError("HTTP redirect refused; credentials not forwarded")


def turso_endpoint(url):
    try:
        parsed = urllib.parse.urlsplit(re.sub(r"^libsql:", "https:", url, flags=re.I))
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or not host.endswith(".turso.io") or parsed.username or parsed.password
                or parsed.port or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                or any(ord(char) <= 32 or ord(char) == 127 for char in url)):
            raise ValueError
    except (TypeError, ValueError):
        raise BackupError("invalid Turso database URL") from None
    return "https://" + host + "/v3/pipeline"


def parse_result(result):
    try:
        names = [column["name"] for column in result["cols"]]
        if any(not isinstance(name, str) for name in names) or not isinstance(result["rows"], list):
            raise ValueError
        rows = []
        for row in result["rows"]:
            if not isinstance(row, list) or len(row) != len(names):
                raise ValueError
            rows.append(tuple(decode(cell) for cell in row))
        return names, rows
    except (KeyError, TypeError, ValueError):
        raise BackupError("invalid database rows") from None


class ReadSnapshot:
    """One sequential Hrana stream; no retry or stream replacement on failure."""
    def __init__(self, url, token, deadline=None, opener=None):
        self.url = turso_endpoint(url)
        self.initial_url = self.url
        if not isinstance(token, str) or not token or any(ord(char) <= 32 or ord(char) == 127 for char in token):
            raise BackupError("missing read-only Turso token")
        self._token = token
        self.deadline = deadline or Deadline()
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.baton = None
        self.active = False
        self.broken = False
        self.request_count = 0
        self.response_bytes = 0

    def _request(self, requests, closing=False):
        self.deadline.check()
        if self.broken:
            raise BackupError("read snapshot was lost; restart the entire backup")
        payload = compact({"baton":self.baton, "requests":requests}).encode()
        if len(payload) > MAX_REQUEST_BYTES:
            raise BackupError("database request limit exceeded")
        request = urllib.request.Request(self.url, payload, {"Authorization":"Bearer " + self._token,
            "Content-Type":"application/json", "Accept":"application/json", "Accept-Encoding":"gzip"})
        try:
            self.request_count += 1
            with self.opener.open(request, timeout=self.deadline.timeout()) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                encoding = getattr(response, "headers", {}).get("Content-Encoding", "identity").strip().lower()
            self.response_bytes += len(body)
            if len(body) > MAX_RESPONSE_BYTES:
                raise BackupError("database response exceeds the bounded size limit")
            if encoding == "gzip":
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                        body = compressed.read(MAX_RESPONSE_BYTES + 1)
                except (OSError, EOFError, ValueError, zlib.error):
                    raise BackupError("invalid compressed database response") from None
                if len(body) > MAX_RESPONSE_BYTES:
                    raise BackupError("database response exceeds the bounded size limit")
            elif encoding != "identity":
                raise BackupError("unsupported database transfer encoding")
            try:
                parsed = json.loads(body)
            except ValueError:
                raise BackupError("invalid JSON database response") from None
            results = parsed["results"]
            if not isinstance(results, list) or len(results) != len(requests) or any(not isinstance(item, dict) for item in results):
                raise BackupError("invalid database pipeline response")
            if any(item.get("type") == "error" for item in results):
                raise BackupError("database returned a SQL/protocol failure")
            if any(item.get("type") != "ok" or not isinstance(item.get("response"), dict)
                    or item["response"].get("type") != requested["type"] for item, requested in zip(results, requests)):
                raise BackupError("invalid database pipeline response")
            baton = parsed.get("baton")
            if (closing and baton is not None) or (not closing and (not isinstance(baton, str) or not baton or len(baton) > 16384)):
                raise BackupError("database snapshot baton missing or invalid")
            if parsed.get("base_url"):
                target = urllib.parse.urlsplit(parsed["base_url"])
                original = urllib.parse.urlsplit(self.initial_url)
                origin_label = original.hostname.split(".")[0]
                hostname = target.hostname or ""
                if (target.scheme != "https" or target.username or target.password or target.port
                        or target.query or target.fragment or target.path not in ("", "/")
                        or not (hostname == original.hostname or
                                (hostname.startswith(origin_label + ".") and hostname.endswith(".turso.io")))):
                    raise BackupError("database sticky URL refused; credentials not forwarded")
                self.url = "https://" + hostname + "/v3/pipeline"
            self.baton = baton
            return [item["response"] for item in results]
        except urllib.error.HTTPError as error:
            self.broken = True
            if error.code in (401, 403):
                raise BackupError("database authentication rejected") from None
            if error.code == 429:
                raise BackupError("database rate limit reached") from None
            raise BackupError("database HTTP request failed") from None
        except urllib.error.URLError as error:
            self.broken = True
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                raise BackupTLSCertificateError() from None
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise BackupError("database request timed out; read snapshot discarded") from None
            raise BackupError("database snapshot request failed; incomplete backup discarded") from None
        except ssl.SSLCertVerificationError:
            self.broken = True
            raise BackupTLSCertificateError() from None
        except BackupError:
            self.broken = True
            raise
        except (TimeoutError, socket.timeout):
            self.broken = True
            raise BackupError("database request timed out; read snapshot discarded") from None
        except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException):
            self.broken = True
            raise BackupError("database snapshot request failed; incomplete backup discarded") from None

    @staticmethod
    def _statement(sql, args=()):
        return {"type":"execute", "stmt":{"sql":sql, "args":[encode(arg) for arg in args], "want_rows":True}}

    def begin(self):
        if self.active or self.baton is not None:
            raise BackupError("read snapshot already started")
        responses = self._request([self._statement("BEGIN"), self._statement(SCHEMA_SQL), {"type":"get_autocommit"}])
        if responses[2].get("is_autocommit") is not False:
            self.broken = True
            raise BackupError("read transaction was not established")
        self.active = True
        names, rows = parse_result(responses[1]["result"])
        if names != ["type", "name", "tbl_name", "sql"] or len(rows) > MAX_TABLES * 10:
            raise BackupError("invalid database schema")
        schema = [dict(zip(names, row)) for row in rows]
        if len(compact(schema).encode()) > MAX_SCHEMA_BYTES:
            raise BackupError("database schema limit exceeded")
        return schema

    def query(self, sql, args=()):
        if not self.active:
            raise BackupError("read snapshot has not started")
        # The only externally callable remote SQL is a SELECT or read PRAGMA.
        if not (sql.startswith("SELECT ") or re.fullmatch(r"PRAGMA (?:table_xinfo|index_list|index_xinfo)\(.+\)", sql)):
            raise BackupError("backup refused a non-read SQL statement")
        responses = self._request([{ "type":"get_autocommit"}, self._statement(sql, args), {"type":"get_autocommit"}])
        if any(responses[index].get("is_autocommit") is not False for index in (0, 2)):
            self.broken = True
            raise BackupError("read transaction expired; incomplete backup discarded")
        return parse_result(responses[1]["result"])

    def close(self):
        if self.baton is not None and not self.broken:
            self._request([self._statement("ROLLBACK"), {"type":"close"}], closing=True)
        self.active = False


class LocalReader:
    def __init__(self, connection):
        self.connection = connection

    def query(self, sql, args=()):
        cursor = self.connection.execute(sql, args)
        return [row[0] for row in cursor.description], [tuple(row) for row in cursor.fetchall()]


def dictionaries(reader, sql, args=()):
    names, rows = reader.query(sql, args)
    return [dict(zip(names, row)) for row in rows]


class TablePlan:
    def __init__(self, reader, record):
        self.name = record["name"]
        if "CREATE VIRTUAL TABLE" in (record["sql"] or "").upper():
            raise BackupError("virtual tables require a native SQLite export")
        if self.name.startswith("sqlite_") and self.name not in ("sqlite_sequence", "sqlite_stat1"):
            raise BackupError("unsupported SQLite internal table requires a native export")
        self.columns = dictionaries(reader, "PRAGMA table_xinfo(" + quote(self.name) + ")")
        if not self.columns or any(row["hidden"] not in (0, 2, 3) for row in self.columns):
            raise BackupError("unsupported table columns")
        names = [row["name"] for row in self.columns]
        self.rowid = None
        self.collations = []
        without_rowid = bool(re.search(r"\bWITHOUT\s+ROWID\b", record["sql"] or "", re.I))
        if not without_rowid:
            self.rowid = next((alias for alias in ("_rowid_", "rowid", "oid") if alias.lower() not in {name.lower() for name in names}), None)
            if self.rowid is None:
                raise BackupError("all rowid aliases are shadowed; native export required")
            self.keys = [self.rowid]
            self.collations = ["BINARY"]
        else:
            primary = sorted((col for col in self.columns if col["pk"]), key=lambda col:col["pk"])
            if not primary:
                raise BackupError("table lacks a stable pagination key")
            self.keys = [col["name"] for col in primary]
            indexes = dictionaries(reader, "PRAGMA index_list(" + quote(self.name) + ")")
            primary_index = next((row["name"] for row in indexes if row["origin"] == "pk"), None)
            if not primary_index:
                raise BackupError("primary key metadata unavailable")
            index_columns = dictionaries(reader, "PRAGMA index_xinfo(" + quote(primary_index) + ")")
            collations = {row["name"]:row["coll"] for row in index_columns if row["key"]}
            self.collations = [collations.get(key) for key in self.keys]
            if any(collation not in ("BINARY", "NOCASE", "RTRIM") for collation in self.collations):
                raise BackupError("unsupported collation requires a native export")
        self.names = ([self.rowid] if self.rowid else []) + names
        self.key_indices = [self.names.index(key) for key in self.keys]
        self.insert_indices = ([0] if self.rowid else []) + [index + bool(self.rowid)
            for index, col in enumerate(self.columns) if col["hidden"] == 0]
        self.insert_names = [self.names[index] for index in self.insert_indices]

    def wire_bytes_sql(self):
        estimates = []
        for column in self.columns:
            name = quote(column["name"])
            escaped = "json_quote(" + name + ")"
            utf8 = "length(CAST(" + escaped + " AS BLOB))"
            # Exact escaped ASCII length, with a conservative expansion only
            # for non-ASCII bytes (including clients encoding Unicode as ASCII).
            estimates.append("CASE typeof(" + name + ") WHEN 'blob' THEN ((length(" + name
                + ")+2)/3)*4 WHEN 'text' THEN " + utf8 + "+4*(" + utf8 + "-length(" + escaped + ")) ELSE 32 END")
        return "(" + "+".join(estimates) + ")+" + str(len(self.names) * 64 + 256)

    def select(self, after, limit, through=None, projection=None):
        expressions = [quote(key) + " COLLATE " + quote(collation) for key, collation in zip(self.keys, self.collations)]
        key = "(" + ",".join(expressions) + ")" if len(expressions) > 1 else expressions[0]
        placeholders = "(" + ",".join("?" for _ in self.keys) + ")" if len(self.keys) > 1 else "?"
        conditions, args = [], []
        if after is not None:
            conditions.append(key + ">" + placeholders)
            args.extend(after)
        if through is not None:
            conditions.append(key + "<=" + placeholders)
            args.extend(through)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        sql = ("SELECT " + (projection or ",".join(quote(name) for name in self.names)) + " FROM " + quote(self.name) + where
                + " ORDER BY " + ",".join(expressions))
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return sql, args

    def bounded_pages(self, reader, page_size, deadline):
        """Size only the next keys, then fetch byte-bounded verified key ranges."""
        after = None
        projection = ",".join(quote(key) for key in self.keys) + "," + self.wire_bytes_sql() + ' AS "__backup_wire_bytes"'
        while True:
            deadline.check()
            sql, args = self.select(after, page_size, projection=projection)
            _, metadata = reader.query(sql, args)
            if not metadata:
                break
            if (len(metadata) > page_size or any(len(row) != len(self.keys) + 1
                    or type(row[-1]) is not int or row[-1] <= 0 or any(key is None for key in row[:-1]) for row in metadata)):
                raise BackupError("invalid byte-budget pagination metadata")
            group, group_bytes = [], 0
            groups = []
            for row in metadata:
                if row[-1] > MAX_RESPONSE_BYTES - MAX_SCHEMA_BYTES:
                    raise BackupError("single database row exceeds bounded export capacity")
                if group and group_bytes + row[-1] > MAX_PAGE_BYTES:
                    groups.append(group)
                    group, group_bytes = [], 0
                group.append(tuple(row[:-1]))
                group_bytes += row[-1]
            if group:
                groups.append(group)
            for keys in groups:
                deadline.check()
                if keys[-1] == after:
                    raise BackupError("database pagination did not advance")
                sql, args = self.select(after, len(keys), through=keys[-1])
                _, rows = reader.query(sql, args)
                if (len(rows) != len(keys) or any(len(row) != len(self.names) for row in rows)
                        or [tuple(row[index] for index in self.key_indices) for row in rows] != keys):
                    raise BackupError("payload rows differ from the byte-budgeted keys")
                yield rows
                after = keys[-1]

    def pages(self, reader, page_size, deadline):
        after = None
        while True:
            deadline.check()
            sql, args = self.select(after, page_size)
            _, rows = reader.query(sql, args)
            if not rows:
                break
            if len(rows) > page_size or any(len(row) != len(self.names) for row in rows):
                raise BackupError("invalid paginated database rows")
            yield rows
            next_key = tuple(rows[-1][index] for index in self.key_indices)
            if next_key == after:
                raise BackupError("database pagination did not advance")
            after = next_key


def schema_fingerprint(schema):
    return hashlib.sha256(compact(schema).encode()).hexdigest()


def fingerprint_table(reader, plan, page_size, deadline):
    digest, count = hashlib.sha256(), 0
    if isinstance(reader, LocalReader):
        # Verification streams the SQLite cursor row by row; a fixed 1000-row
        # fetchall could allocate gigabytes when several legacy rows are large.
        sql, args = plan.select(None, None)
        for row in reader.connection.execute(sql, args):
            deadline.check()
            add_digest(digest, row)
            count += 1
        return {"columns":plan.names, "row_count":count, "row_sha256":digest.hexdigest()}
    for page in plan.pages(reader, page_size, deadline):
        for row in page:
            add_digest(digest, row)
        count += len(page)
    return {"columns":plan.names, "row_count":count, "row_sha256":digest.hexdigest()}


@contextmanager
def local_connection(path, deadline):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.set_progress_handler(lambda:int(deadline.clock() >= deadline.end), 10000)
    try:
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def sqlite_fingerprints(path, page_size=1000, deadline=None):
    """Full stable-order row digest/count and schema hash for verification reuse."""
    deadline = deadline or Deadline()
    with local_connection(path, deadline) as connection:
        reader = LocalReader(connection)
        schema = dictionaries(reader, SCHEMA_SQL)
        tables = {}
        for record in schema:
            if record["type"] == "table":
                plan = TablePlan(reader, record)
                tables[plan.name] = fingerprint_table(reader, plan, page_size, deadline)
        return {"schema_sha256":schema_fingerprint(schema), "tables":tables}


def compare_fingerprints(expected, actual):
    if expected != actual:
        raise BackupError("full SQLite schema/row-count/digest verification failed")


def validate_sqlite(path, expected=None, deadline=None):
    deadline = deadline or Deadline()
    with local_connection(path, deadline) as connection:
        results = connection.execute("PRAGMA integrity_check").fetchall()
        if results != [("ok",)] or connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("SQLite integrity or foreign-key verification failed")
    actual = sqlite_fingerprints(path, deadline=deadline)
    if expected is not None:
        compare_fingerprints(expected, actual)
    return actual


def build_snapshot(client, destination, page_size=1000, progress=None):
    if not 1 <= page_size <= 1000:
        raise BackupError("invalid backup page size")
    destination = Path(destination)
    if destination.exists():
        raise BackupError("backup destination already exists")
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    local = sqlite3.connect(destination)
    local.set_progress_handler(lambda:int(client.deadline.clock() >= client.deadline.end), 10000)
    success = False
    try:
        schema = client.begin()
        tables = [row for row in schema if row["type"] == "table"]
        if len(tables) > MAX_TABLES or not tables:
            raise BackupError("invalid or empty database schema")
        if any(not isinstance(row["sql"], str) for row in tables):
            raise BackupError("table creation SQL missing")
        plans = [TablePlan(client, row) for row in tables]
        local.execute("PRAGMA foreign_keys=OFF")
        local.execute("BEGIN")
        for row in tables:
            if not row["name"].startswith("sqlite_"):
                local.execute(row["sql"])
        if any(row["name"] == "sqlite_stat1" for row in tables):
            local.execute("ANALYZE")
        expected = {"schema_sha256":schema_fingerprint(schema), "tables":{}}
        for plan in plans:
            client.deadline.check()
            if plan.name.startswith("sqlite_"):
                local.execute("DELETE FROM " + quote(plan.name))
            # COUNT uses the table/index structure; do not scan/format every
            # payload for a global MAX. Even byte-only MAX timed out on a
            # production table, while bounded upcoming-key windows succeeded.
            _, metrics = client.query("SELECT COUNT(*) FROM " + quote(plan.name))
            if len(metrics) != 1 or len(metrics[0]) != 1 or type(metrics[0][0]) is not int or metrics[0][0] < 0:
                raise BackupError("invalid database table metrics")
            source_count = metrics[0][0]
            pages = plan.bounded_pages(client, page_size, client.deadline) if source_count else ()
            digest, count = hashlib.sha256(), 0
            insert = "INSERT INTO " + quote(plan.name) + "(" + ",".join(quote(name) for name in plan.insert_names)
            insert += ") VALUES(" + ",".join("?" for _ in plan.insert_names) + ")"
            for page in pages:
                local.executemany(insert, (tuple(row[index] for index in plan.insert_indices) for row in page))
                for row in page:
                    add_digest(digest, row)
                count += len(page)
                if progress:
                    progress(len(expected["tables"]), len(plans), sum(table["row_count"] for table in expected["tables"].values()) + count,
                             client.request_count)
            if count != source_count:
                raise BackupError("snapshot row count differs from the read transaction")
            expected["tables"][plan.name] = {"columns":plan.names, "row_count":count, "row_sha256":digest.hexdigest()}
        for row in schema:
            if row["type"] in ("index", "view", "trigger") and row["sql"]:
                local.execute(row["sql"])
        local.commit()
        client.close()
        local.close()
        validate_sqlite(destination, expected, client.deadline)
        success = True
        return expected
    except sqlite3.Error:
        raise BackupError("SQLite snapshot construction or verification failed") from None
    finally:
        local.close()
        try:
            client.close()
        except BackupError:
            pass
        if not success:
            for suffix in ("", "-journal", "-wal", "-shm"):
                destination.with_name(destination.name + suffix).unlink(missing_ok=True)


def compress_snapshot(source, target, deadline):
    with open(source, "rb") as input_file, open(target, "xb") as output_file:
        os.chmod(target, 0o600)
        with gzip.GzipFile(filename="", mode="wb", fileobj=output_file, mtime=0, compresslevel=6) as compressed:
            while True:
                deadline.check()
                block = input_file.read(1024 * 1024)
                if not block:
                    break
                compressed.write(block)


def verify_backup(archive, manifest, deadline=None):
    deadline = deadline or Deadline()
    if not isinstance(manifest, dict):
        with Path(manifest).open("rb") as handle:
            raw = handle.read(MAX_SCHEMA_BYTES + 1)
        if len(raw) > MAX_SCHEMA_BYTES:
            raise BackupError("backup manifest size limit exceeded")
        try:
            manifest = json.loads(raw)
        except (ValueError, TypeError):
            raise BackupError("invalid backup manifest") from None
    if (not isinstance(manifest, dict) or manifest.get("format_version") != VERSION or manifest.get("compression") != "gzip"
            or manifest.get("consistency") != "single_read_transaction"
            or type(manifest.get("sqlite_bytes")) is not int or manifest["sqlite_bytes"] <= 0
            or type(manifest.get("archive_bytes")) is not int or manifest["archive_bytes"] != Path(archive).stat().st_size
            or not isinstance(manifest.get("fingerprints"), dict)
            or file_sha(archive, deadline) != manifest.get("archive_sha256")):
        raise BackupError("backup archive SHA-256 or manifest verification failed")
    with tempfile.TemporaryDirectory(prefix="radar-backup-verify-") as root:
        destination = Path(root) / "restored.sqlite"
        with gzip.open(archive, "rb") as input_file, destination.open("xb") as output:
            os.chmod(destination, 0o600)
            size = 0
            for block in iter(lambda:input_file.read(1024 * 1024), b""):
                deadline.check()
                size += len(block)
                if size > manifest.get("sqlite_bytes", -1):
                    raise BackupError("backup decoded size limit exceeded")
                output.write(block)
        if size != manifest.get("sqlite_bytes") or file_sha(destination, deadline) != manifest.get("sqlite_sha256"):
            raise BackupError("restored SQLite SHA-256 verification failed")
        validate_sqlite(destination, manifest["fingerprints"], deadline)
    return True


class GitHub:
    def __init__(self, repository, token, deadline=None, opener=None, connection_factory=http.client.HTTPSConnection):
        if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise BackupError("invalid private backup repository")
        if not token or any(ord(char) <= 32 or ord(char) == 127 for char in token):
            raise BackupError("missing private repository token")
        self.repository, self._token = repository, token
        self.prefix = "/repos/" + repository
        self.deadline = deadline or Deadline()
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.connection_factory = connection_factory

    def headers(self):
        return {"Authorization":"Bearer " + self._token, "Accept":"application/vnd.github+json",
                "X-GitHub-Api-Version":"2026-03-10", "User-Agent":"solana-radar-private-backup"}

    def request(self, path, method="GET", payload=None):
        if not path.startswith(self.prefix) or ".." in path or "#" in path:
            raise BackupError("GitHub request path refused")
        data = compact(payload).encode() if payload is not None else None
        headers = {**self.headers(), **({"Content-Type":"application/json"} if data is not None else {})}
        request = urllib.request.Request("https://api.github.com" + path, data, headers, method=method)
        try:
            with self.opener.open(request, timeout=self.deadline.timeout()) as response:
                raw = response.read(MAX_REQUEST_BYTES + 1)
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError
            return json.loads(raw) if raw else None
        except urllib.error.URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                raise BackupTLSCertificateError() from None
            raise BackupError("private GitHub backup request failed") from None
        except ssl.SSLCertVerificationError:
            raise BackupTLSCertificateError() from None
        except (OSError, ValueError, TypeError):
            raise BackupError("private GitHub backup request failed") from None

    def assert_private(self):
        result = self.request(self.prefix)
        if not isinstance(result, dict) or result.get("private") is not True or result.get("full_name", "").lower() != self.repository.lower():
            raise BackupError("backup repository must be private; refusing to upload")

    def upload(self, release_id, path, expected_sha):
        self.assert_private()
        if type(release_id) is not int or release_id <= 0:
            raise BackupError("invalid backup release identifier")
        path = Path(path)
        size = path.stat().st_size
        if size > MAX_ASSET_BYTES:
            raise BackupError("backup exceeds GitHub release asset size limit")
        endpoint = self.prefix + f"/releases/{release_id}/assets?name=" + urllib.parse.quote(path.name, safe="")
        connection = self.connection_factory("uploads.github.com", timeout=self.deadline.timeout())
        try:
            connection.putrequest("POST", endpoint)
            for name, value in {**self.headers(), "Content-Type":"application/octet-stream", "Content-Length":str(size)}.items():
                connection.putheader(name, value)
            connection.endheaders()
            with path.open("rb") as handle:
                for block in iter(lambda:handle.read(1024 * 1024), b""):
                    self.deadline.check()
                    connection.send(block)
            response = connection.getresponse()
            raw = response.read(MAX_REQUEST_BYTES + 1)
            if response.status != 201 or len(raw) > MAX_REQUEST_BYTES:
                raise ValueError
            asset = json.loads(raw)
            if (asset.get("size") != size or asset.get("name") != path.name or asset.get("state") != "uploaded"
                    or asset.get("digest") != "sha256:" + expected_sha):
                raise ValueError
            return asset
        except ssl.SSLCertVerificationError:
            raise BackupTLSCertificateError() from None
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            raise BackupError("private backup upload or remote SHA-256 verification failed") from None
        finally:
            connection.close()

    def owned_releases(self):
        result = []
        for page in range(1, 21):
            rows = self.request(self.prefix + f"/releases?per_page=100&page={page}")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise BackupError("invalid private release listing")
            result.extend(row for row in rows if re.fullmatch(r"storage-backup-\d{8}T\d{6}Z-[a-f0-9]{8}", row.get("tag_name", ""))
                          and row.get("body", "").startswith(RELEASE_MARKER))
            if len(rows) < 100:
                return result
        raise BackupError("private release pagination limit exceeded")

    def prune(self, keep=7, protected=None):
        if type(keep) is not int or not 1 <= keep <= 7:
            raise BackupError("backup retention must be between one and seven copies")
        self.assert_private()
        releases = sorted(self.owned_releases(), key=lambda row:row["tag_name"], reverse=True)
        if protected is not None and not any(row.get("id") == protected for row in releases):
            raise BackupError("verified backup absent from retention listing")
        preserved = set()
        if protected is not None:
            preserved.add(protected)
        for row in releases:
            if len(preserved) < keep:
                preserved.add(row["id"])
        for row in releases:
            if row["id"] not in preserved:
                self.assert_private()
                self.request(self.prefix + "/releases/" + str(row["id"]), "DELETE")
                # Release deletion does not remove the tag. Prune only our own
                # exact validated tag, never unrelated project releases/tags.
                self.request(self.prefix + "/git/refs/tags/" + row["tag_name"], "DELETE")

    def publish(self, archive, manifest_path, manifest, keep=7):
        self.assert_private()
        tag = RELEASE_PREFIX + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        release = self.request(self.prefix + "/releases", "POST", {"tag_name":tag,
            "name":"Private database backup", "body":"Private backup upload in progress; not a verified recovery point",
            "draft":True, "make_latest":"false"})
        if not isinstance(release, dict) or type(release.get("id")) is not int or release.get("draft") is not True:
            raise BackupError("private draft backup release was not created")
        release_id = release["id"]
        verified = False
        try:
            self.upload(release_id, archive, manifest["archive_sha256"])
            manifest_sha = file_sha(manifest_path, self.deadline)
            self.upload(release_id, manifest_path, manifest_sha)
            verified = True
            self.assert_private()
            self.request(self.prefix + "/releases/" + str(release_id), "PATCH", {"body":RELEASE_MARKER
                + "\narchive_sha256=" + manifest["archive_sha256"] + "\nmanifest_sha256=" + manifest_sha})
            self.assert_private()
            # GitHub can expose an untagged-* name until a draft is published.
            # Publish the verified replacement before relying on its tag for retention.
            published = self.request(self.prefix + "/releases/" + str(release_id), "PATCH",
                {"draft":False, "tag_name":tag, "make_latest":"false"})
            if (not isinstance(published, dict) or published.get("draft") is not False
                    or published.get("tag_name") != tag):
                raise BackupError("verified backup release publication failed")
            self.prune(keep, protected=release_id)
        except Exception:
            # Do not delete a verified recovery point after a publication or
            # retention failure. No prior backup is ever
            # removed before both replacement assets have passed SHA checks.
            if not verified:
                try:
                    self.assert_private()
                    self.request(self.prefix + "/releases/" + str(release_id), "DELETE")
                    self.request(self.prefix + "/git/refs/tags/" + tag, "DELETE")
                except BackupError:
                    pass
            raise
        return release_id


def create_backup(url, token, output, deadline=None, opener=None, page_size=1000, progress=None):
    deadline = deadline or Deadline()
    output = Path(output)
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    if output.is_symlink() or any(output.iterdir()):
        raise BackupError("backup output must be an empty private directory")
    os.chmod(output, 0o700)
    snapshot = output / "radar.sqlite"
    archive = output / "radar.sqlite.gz"
    manifest_path = output / "manifest.json"
    client = ReadSnapshot(url, token, deadline, opener)
    started = datetime.now(timezone.utc).isoformat()
    started_clock = deadline.clock()
    fingerprints = build_snapshot(client, snapshot, page_size, progress)
    export_stats = {"http_requests":client.request_count, "response_bytes":client.response_bytes,
        "snapshot_seconds":round(deadline.clock() - started_clock, 3),
        "row_count":sum(table["row_count"] for table in fingerprints["tables"].values())}
    compress_snapshot(snapshot, archive, deadline)
    manifest = {"format_version":VERSION, "compression":"gzip", "consistency":"single_read_transaction",
        "snapshot_started_at":started, "snapshot_completed_at":datetime.now(timezone.utc).isoformat(),
        "database_identity_sha256":hashlib.sha256(client.initial_url.encode()).hexdigest(),
        "sqlite_bytes":snapshot.stat().st_size, "sqlite_sha256":file_sha(snapshot, deadline),
        "archive_bytes":archive.stat().st_size, "archive_sha256":file_sha(archive, deadline),
        "fingerprints":fingerprints, "export_stats":export_stats}
    with manifest_path.open("x", encoding="utf-8") as handle:
        os.chmod(manifest_path, 0o600)
        handle.write(compact(manifest) + "\n")
    verify_backup(archive, manifest, deadline)
    snapshot.unlink()
    return archive, manifest_path, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    backup.add_argument("--publish", action="store_true")
    backup.add_argument("--output")
    backup.add_argument("--keep", type=int, default=7, choices=range(1, 8))
    backup.add_argument("--max-seconds", type=int, default=1000)
    verify = commands.add_parser("verify")
    verify.add_argument("archive")
    verify.add_argument("manifest")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            verify_backup(args.archive, args.manifest)
            print("Backup verified: SQLite integrity, schema, all row counts and SHA-256 digests")
            return 0
        deadline = Deadline(args.max_seconds)
        last_progress = deadline.clock()
        def progress(completed_tables, total_tables, rows, requests):
            nonlocal last_progress
            now = deadline.clock()
            if now - last_progress >= 15:
                print(f"Backup export progress: tables={completed_tables}/{total_tables} rows={rows} requests={requests}", flush=True)
                last_progress = now
        if not args.publish and not args.output:
            raise BackupError("local backups require --output; temporary results would be discarded")
        github = None
        if args.publish:
            github = GitHub(args.repository, os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"), deadline)
            github.assert_private()
        with tempfile.TemporaryDirectory(prefix="radar-daily-backup-") as temporary:
            output = args.output or temporary
            for attempt in range(2):
                attempt_output = Path(output) if args.output else Path(output) / f"attempt-{attempt}"
                try:
                    archive, manifest_path, manifest = create_backup(os.environ.get("TURSO_DATABASE_URL"),
                        os.environ.get("TURSO_BACKUP_AUTH_TOKEN") or os.environ.get("TURSO_AUTH_TOKEN"),
                        attempt_output,deadline,progress=progress)
                    break
                except BackupError as error:
                    retryable = str(error) in {"database request timed out; read snapshot discarded",
                                              "database snapshot request failed; incomplete backup discarded"}
                    if args.output or attempt or not retryable or deadline.end-deadline.clock()<120:
                        raise
                    print("Transient backup failure; restarting from a new read snapshot",flush=True)
            if github:
                github.publish(archive, manifest_path, manifest, args.keep)
            print("Verified database backup " + ("stored in private GitHub Releases" if github else "created locally")
                + f"; rows={manifest['export_stats']['row_count']} export_requests={manifest['export_stats']['http_requests']}")
        return 0
    except BackupTLSCertificateError:
        print("Storage backup failed: trusted TLS certificates unavailable. Set SSL_CERT_FILE to a trusted CA bundle; never disable TLS verification.", file=sys.stderr)
        return 1
    except (BackupError, OSError, ValueError, sqlite3.Error, KeyError, TypeError) as error:
        # Even local SQLite/path failures can include private values. Do not log
        # exception details or traceback, and never upload failed output artifacts.
        reason = str(error) if isinstance(error, BackupError) and str(error) in SAFE_FAILURE_MESSAGES else "unclassified safe failure"
        print("Storage backup failed safely: " + reason + ". No incomplete backup was published.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
