# Offline storage migration

`tools/migrate_storage.py` combines **quiescent local SQLite exports** into one
Turso libSQL-compatible database. It uses Python's standard library only. No
production connection or credentials are needed for planning, local building or
offline verification. Dry-run is the default.

## Preferred cutover: verified local build, official CLI import

1. Stop/freeze all destination writers. Take complete, consistent exports of the
   operational and history D1 databases. Restore the SQL exports into standalone
   `.sqlite` files; close/checkpoint their writers. Never use a partially restored
   database. The tool rejects WAL/journal sidecars, failed integrity checks,
   foreign-key violations, null primary keys and unsupported schema objects.
2. Run dry-run to get source file SHA-256, table counts, full ordered row digests,
   payload sizes, migration provenance and planned schema additions:

   ```bash
   python3 tools/migrate_storage.py \
     --source-op /tmp/radar-cutover-operational.sqlite \
     --source-history /tmp/radar-cutover-history-fast.sqlite
   ```

3. Build the **new**, private standalone database. Existing output/report files
   are not overwritten. The file is atomically published only after committed
   full-table counts/digests, schema/index definitions, FK checks and integrity
   checks pass. A failed build removes its temporary SQLite files.

   ```bash
   python3 tools/migrate_storage.py \
     --source-op /tmp/radar-cutover-operational.sqlite \
     --source-history /tmp/radar-cutover-history-fast.sqlite \
     --build-local /Users/mirash/.codex/backups/solana-radar/20261003/solana-radar.sqlite \
     --manifest /Users/mirash/.codex/backups/solana-radar/20261003/storage-build-report.json \
     --cutover-lock storage-20261003-v1
   ```

   In this mode `--manifest` is a private **build report**, not an HTTP resume
   journal. Rebuilding after a failed build starts from scratch. The final file
   uses DELETE journaling with no WAL sidecars, suitable for single-file import;
   do not enable WAL while preparing/uploading it. Files are mode `0600`.

4. Import that file into a **fresh** database using the authenticated official
   Turso CLI. Provisioning, CLI upload, credentials and runtime cutover belong to
   the operator/orchestrator, not this tool. Inspect the installed CLI's `db
   create --help` for its current `--from-file`/import syntax. Never replace an
   active database to bypass a verification failure.
5. Export the newly imported database through the CLI before enabling writers;
   if the export is SQL, restore it into another standalone SQLite file. Verify
   the export against the **same original D1 files**, not just the uploaded
   merged file. This re-derives the full original plan and checks every row:

   ```bash
   python3 tools/migrate_storage.py \
     --source-op /tmp/radar-cutover-operational.sqlite \
     --source-history /tmp/radar-cutover-history-fast.sqlite \
     --verify-local /tmp/solana-radar-turso-export.sqlite \
     --cutover-lock storage-20261003-v1
   ```

   No environment variables, network access, destination writes or manifest
   updates occur in `--verify-local`. Nonzero exit means **do not cut over**.
6. Reconcile data created after the exports, including Durable Object pending
   history events, runtime documents/checkpoints and scanner outboxes. These are
   **not** included just because the SQLite files verified. Preserve/replay them
   idempotently through the new runtime. Retain the source exports and D1 for
   rollback, then change the runtime database routing and verify a real scan.

The `_radar_import_lock` marker remains `verified`, with the plan hash and
operator lock ID. This is a **cooperative cutover marker**, not a database-wide
lock against arbitrary external clients. The runtime/orchestrator must honor it
and keep writers paused until reconciliation finishes. This tool never enables
the production scheduler or releases an application write lock.

## Preserved data and schema

- All application tables, rows and original explicit indexes are copied,
  including **pending and delivered** outbox events and full raw JSON/blobs.
  No token, payload, negative outcome or cohort is removed or reclassified.
- Table definitions come from `sqlite_master`, parsed/executed by SQLite; column
  and primary-key metadata come from PRAGMAs. DDL identifiers are not rewritten
  with regex/string substitution. Generated columns are recomputed from their
  original schema and included in verification. Autoincrement sequences are
  preserved, including sequences above the maximum surviving row ID.
- Original history `d1_migrations` rows remain in `d1_migrations`; operational
  rows remain in `ops_d1_migrations`, with the rename compiled by SQLite. Their
  provenance also goes into `_radar_import_migrations` under separate `op` and
  `history` namespaces; the two D1 migration sequences are never conflated.
- History `0003_resumable_history.sql` is additive and validated against actual
  columns/defaults/types, existing tables and indexes. A migration name alone
  is not evidence that its columns exist. A missing legacy 0003 tracking row is
  inserted **only with the completed upgrade**, then recorded with its actual
  generated ID/timestamp in the canonical receipt. Original tracking rows still
  receive full digest verification; that one derived row receives separate
  receipt/content verification.
- `migrations-storage/0001_daily_learning.sql` is compiled independently by
  SQLite and included in the plan/schema verification. Its source file SHA and
  completion receipt are recorded under `storage/0001_daily_learning.sql`.
  Newly added Learning tables must be empty; existing source Learning rows, if
  present, are retained and verified. The known new history lock row is checked
  separately. Added 0003 columns are checked for their expected default values.
- Identically named application tables can be deduplicated only when original
  schema, counts and complete row digests agree. Conflicting tables/index names
  are refused before destination writes; there is no lossy "last source wins".
- Views, triggers, virtual/shadow tables, missing/null primary keys and custom
  primary-key collations are refused, not silently skipped. This is a bounded
  migration tool for these D1 schemas, not a universal SQLite dump importer.

The report's `verified_rows` counts original source rows, not derived control
receipts, newly added lock rows or the generated 0003 migration row. `schema`
verification also covers the added tables/indexes. Source file SHA and table
digests must stay unchanged between build and verification. A later repository
migration change or an amended source export requires a fresh migration plan.

## Optional direct HTTP import and resume

For smaller databases or testing, `--execute` uses conditional Hrana v2 batches
over HTTPS. Only URLs under `*.turso.io` are accepted (including regional
subdomains); `libsql://` is converted to HTTPS. Credentials, ports, query strings,
custom hosts and redirects are refused. Auth comes **only** from
`TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN`. Never put tokens in command arguments,
reports or Git. HTTP bodies, SQL error messages and credential values are not
logged.

```bash
python3 tools/migrate_storage.py \
  --source-op /tmp/radar-cutover-operational.sqlite \
  --source-history /tmp/radar-cutover-history-fast.sqlite \
  --execute --writers-paused --cutover-lock storage-20261003-v1 \
  --manifest /Users/mirash/.codex/backups/solana-radar/20261003/storage-http-resume.json \
  --batch-rows 100 --batch-bytes 1048576
```

- Destination must be empty or carry the exact matching import marker. Only
  plain INSERTs are used for application rows: no upsert/replace/newer-row
  overwrite. Original exports remain open in read-only transactions throughout.
- Source pages use primary-key keysets, **never OFFSET**. Both rows and HTTP
  request bytes are bounded; a single row exceeding the configured request
  limit stops the import. The local-build/official-CLI route handles large rows
  without this HTTP restriction. Each batch has conditional BEGIN/COMMIT and
  rollback on failure; rows and the matching immutable receipt commit together.
- On uncertain delivery, an existing receipt **and destination page contents**
  must match. Otherwise stop; no automatic blind write retry. Re-running the
  same command reconciles durable receipts and resumes. Unreceipted existing
  rows stop the migration, even if they appear harmless.
- A private atomically saved manifest binds source hashes, full table hashes,
  plan, target, lock and batch settings. A process lock prevents two local
  importers sharing it. Restart checks full completed tables instead of trusting
  `complete=true`. Keep the same batch row/byte limits on resume.
- Final proof re-reads every original row with typed, length-prefixed canonical
  SHA-256. This distinguishes integer/float/text/blob/null and preserves signed
  64-bit integers without JSON precision loss. Counts, schema/index definitions,
  migration provenance, sequence values, FK and integrity checks also run.
- Source file hashes are checked at start/end and verification boundaries;
  file identity/size/mtime/ctime and WAL/journal checks guard every write page.
  A changed export invalidates the plan. No partial source refresh is merged
  into an existing import.

`--verify-only` performs the same remote full proof with SELECT/PRAGMA reads only
and no manifest updates. It may download the entire raw dataset through many
bounded requests; prefer `--verify-local` on a CLI export for a large database.
The exact expected initial schema is required: run this proof before applying
additional runtime migrations or enabling scans that change rows.

## Archive/compaction boundary

**There is no archive or deletion mode in this tool.** It does not call
`/api/storage/archive` and does not replace payloads with unverified references.
The initial import keeps full raw data. Any later compaction needs an agreed
authenticated archive contract, checked uploaded checksum, independent readback,
durable SQL reference and replay proof before removing a payload. Provisioning
R2 alone is not evidence that an archive exists or is recoverable.

## Python API for a CLI-exported local verification

```python
import sqlite3
from tools.migrate_storage import Source, Plan, LocalDatabase, Migrator

sources = [Source("op", "/tmp/radar-cutover-operational.sqlite"),
           Source("history", "/tmp/radar-cutover-history-fast.sqlite")]
connection = sqlite3.connect("file:/tmp/solana-radar-turso-export.sqlite?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
try:
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    plan = Plan(sources)
    result = Migrator(plan, LocalDatabase(connection), None,
                      "storage-20261003-v1").verify()
    print(result)
finally:
    connection.close()
    for source in sources:
        source.close()
```

Prefer `verify_local(plan, path, lock_id)` or the CLI wrapper: it additionally
checks destination snapshot sidecars/file SHA before and after verification.

Tests: `python3 -m unittest tests.test_storage_migration -v`.

Protocol references: [Turso SQL-over-HTTP reference](https://docs.turso.tech/sdk/http/reference)
and [Hrana conditional batches](https://github.com/tursodatabase/libsql/blob/main/docs/HRANA_3_SPEC.md).
Tests simulate that wire protocol; a simulated pass is not a production import.
