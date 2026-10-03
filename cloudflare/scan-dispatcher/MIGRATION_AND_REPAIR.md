# I01-I10 remediation and forward rollout

Status: implemented and locally verified on `codex/full-audit-remediation`.
No production calls, writes, deploys or commits were performed by this owner.
The live queue payloads and their exact failure causes have not been inspected.

## Required rollout order

1. Preserve backups of both D1 databases and existing queue/runtime documents.
2. Apply `migrations-history/0003_resumable_history.sql` to the history database
   **before deploying this Worker code in normal mode**. The explicit guarded
   exception below permits deployment while migration 0003 is quota-blocked.
   From `cloudflare/scan-dispatcher`, the
   operator can use `wrangler d1 migrations list RADAR_HISTORY_DB --remote`, then
   `wrangler d1 migrations apply RADAR_HISTORY_DB --remote`. Use migration tracking;
   do not replay the ALTER statements manually against an already migrated DB.
3. Deploy the Worker using the existing bindings/class migration tags. Queue DO
   initialization adds progress/health columns idempotently without replacing any
   existing row, payload, receipt, counter or cursor. No new DO class tag is needed.
4. Check effective limits, pending bytes/rows, oldest pending age, delivered count,
   last flush failure and current as-of time. If an existing environment override
   keeps `HISTORY_QUEUE_MAX_RECEIPT_ROWS=20000`, remove or deliberately adjust that
   metadata-only override: it does not support the demonstrated 30-day workload.
5. Verify report/detail pending behavior and UI-only Robinhood publication after
   deployment. The parent owns frontend acceptance and production verification.

### Release-specific guarded exception

`wrangler.toml` explicitly sets
`HISTORY_SCHEMA_AUTO_UPGRADE="0003_resumable_history.sql"` for this release.
With that exact flag, an absent marker never permits history flush or public
intelligence reads against the new schema. Without the flag, the normal
migration-first contract applies with **zero additional D1 queries**.
Unknown/empty flag values fail clearly; the guard never guesses future versions.

Only the existing five-minute cron and authenticated manual flush may attempt
the upgrade. Public fetches are read-only gates, not migration triggers. An
attempt reads the exact marker once, then sends at most one atomic `db.batch()`:
17 fixed statements byte-for-byte matching migration 0003, followed by
`INSERT INTO d1_migrations(name) VALUES('0003_resumable_history.sql')`.
The existing tracker must have its verified unique name/default timestamp schema;
the guard does not create or repair it. After batch failure it performs one
bounded marker recheck: another writer's committed marker means ready, otherwise
the original failure is retained. It never blindly retries ALTER within a call.
[D1 documents transactional batch rollback](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch).

While daily quota blocks this batch, runtime DO health reports
`history_schema_upgrade_pending`, `history_schema.ready=false`, the underlying
quota cause, fresh queue counts/age/as-of and `flush_skipped=true`. No queue lease,
progress or payload is changed by a skipped flush. Authenticated history intake
remains durable subject to its existing pending capacities. Public intelligence
returns 503 with clear schema state; token detail may retain a null optional
history edge. Scanner dispatch and dashboard publication remain independent.
Both runtime projections and the dashboard document fallback overlay fresh
`history_status`, so schema/quota pending is not hidden behind an old snapshot.

Cron retries are bounded to one attempt per invocation and stop writing once
the marker is present. It can succeed after the account's UTC reset when the
actual platform allows the batch; it does not reset quotas, raise write budgets
or upgrade/pay for the plan. Avoid concurrent manual retry loops. Non-quota SQL,
missing schema/tracker and configuration errors report explicit failed state and
cause rather than being silently labelled quota. A batch may still be blocked
after reset by genuine account limits or schema prerequisites.

**Prerequisite:** tables from migrations 0001 and 0002 must already exist. If a
complete `sqlite_master` inventory literally contains only `d1_migrations`,
migration 0003 cannot ALTER the missing baseline tables. The guard intentionally
fails closed; the operator must apply the earlier tracked migrations after quota
availability, not manufacture a marker or partially alter the schema.

After a committed marker, verify new schema tables, healthy flush/backlog
progress and public intelligence. Then remove the release-specific opt-in in a
follow-up deployment if desired; migration 0003 remains tracked for Wrangler.
Backups reported by the parent are preserved privately at
`/Users/mirash/.codex/backups/solana-radar/20261003/`; this owner did not read or
modify them. Local tests prove rollback/reset/concurrency routing, **not live
automatic upgrade after reset**. Parent owns pending-mode production verification.

## Resolved IDs and files

| ID | Implementation | Regression evidence |
| --- | --- | --- |
| I01 | `src/history-progress.js`, `src/runtime-history.js`, `src/history.js`: persisted phases, bounded pairs, independent genuine acknowledgments, packed retention observations, no repeated frozen graph writes, reported SQLite write accounting, two bounded cron flushes, 160,000 metadata receipts | 60/100-wallet restart/drain; minimum admitted budgets; full two-day workload with receipt expiry |
| I02 | `src/index.js`, `src/runtime-history.js`: health published on failures with fresh counters, backlog age and last failure | scheduled rejection and queue daily-cap tests |
| I03 | `src/runtime-documents.js`, `src/runtime.js`: fresh one-hour part protection starting at manifest supersession | reader A / writer B / delayed part fetch / later reclamation |
| I04 | `src/index.js`, `tools/build_pages.py`: source generation and frozen cohort/window identity must match; otherwise summary-only pending | D1 generation/window and static detail tests |
| I05 | `src/history.js`, migration: explicit null preserved, versioned numeric trust, legacy learning quarantined | unknown versus actual zero; legacy raw retained / aggregate excluded |
| I06 | `src/history.js`, `src/index.js`: versioned full score/lift/sample/address cursor including null lift | tied/null-lift rows returned exactly once; malformed/old cursor rejected |
| I07 | `src/history-clusters.js`, `src/history-progress.js`: persisted connected-component traversal, row-bounded fragment retirement and cleanup, active-owner lock recovery, infrastructure exclusion | AB/BC/CD merge; infrastructure split; crash/restart; waiting owner; 30-fragment retirement at 24-unit budget |
| I08 | `src/history.js`: maximum 99 addresses plus cutoff; traversal pages 25 | every executable D1 query enforces 100 binds; 100-wallet archive finishes |
| I09 | `src/index.js`: restore resolves associated old pool identities, retaining independent deletions | mint/old-pool restore and unrelated identity checks |
| I10 | `tools/build_pages.py`: content-addressed immutable detail files; partial mode omits incompatible data rather than failing operational publication | generation race, cohort/window mismatch, malformed detail and unsafe path tests |

Additional publisher integration: `publish_robinhood()` deep-copies the selected
local/published snapshot before calling `robinhood.guard_legacy_ordinary_row()`.
No RPC is invoked. Old confirmed ordinary cohorts publish as `legacy_unverified` /
`needs_data`, retain raw attribution evidence, and do not mutate preserved input.

## Capacity and accounting

- Pending payload limits remain 2,048 rows / 16 MiB; each event is at most 128 KiB,
  each request 1 MiB / 25 events. A cap rejects new work atomically; it never drops
  accepted pending work or truncates observations.
- Daily budgets remain 80,000 history units and 50,000 DO units. Each flush has
  at most 40 history queries and 40,000 history units. Five-minute cron performs
  at most two serialized flushes (576 calls / 23,040 history queries per day).
- A known 40-wallet retention check consumes 24 history units and five queries:
  episode/event upserts, two catch-existence reads, one immutable observation
  bundle. All raw observations remain archived. Outcomes refresh scores without
  regenerating structural edges/frontier; new signals still archive edge evidence.
- Receipts retain first-payload-wins idempotency for at least 30 days from genuine
  delivery. The metadata allowance is now 160,000 rows, including pending rows.
  It covers 31 UTC budget windows at the theoretical 5,000 minimum-cost events/day
  plus two 2,048-row burst/pending allowances (159,096). It does not raise D1/DO
  write quotas. Only expired **delivered** receipts are removed, on intake and only
  as needed; their cleanup writes are charged. Pending payloads are never purged.
- DO reservations are refunded using SQLite `rowsWritten`, including index writes
  when reported; absent that counter, conservative reservations remain. History
  units are the existing eight-unit-per-single-row-write/index-headroom model,
  not a measurement of actual Cloudflare billing. New cluster writes are bounded
  by row; read scans/aggregate costs and live platform execution limits remain
  deployment verification responsibilities.

### Demonstrated workload

`test/runtime-history.test.mjs` executes production SQL locally for two UTC days:
100 existing 40-wallet cohorts checked hourly (2,400 retention events/day),
24 verified outcome events/day, four new 40-wallet signals/day, and an initial
96-event backlog. Each existing cohort has a warm two-wallet structural cluster.
The receipt store is preloaded with 157,952 metadata rows, including a protected
unexpired receipt, so the run reaches the cap and then continuously expires old
delivered receipts. Queue restart occurs hourly; cron scheduling is exercised.

| Day | Workload delivered, cumulative | Pending at day end | History units | DO units | Receipt rows |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 2,524 | 0 | 78,048 | 40,889 | 160,000 |
| 2 | 4,952 | 0 | 75,744 | 47,216 | 160,000 |

Peak pending: 198. All 4,896 retention/backlog observation bundles survive;
unexpired dedupe survives restart and cleanup. This is two days of full SQL work
against a mature near-capacity store, not a claim of a 30-day full-workload soak.
Separate tests prove the 30-day TTL boundary and refusal to evict unexpired rows.

The admitted fully connected 60-wallet signal takes 14 bounded flushes / 46,328
history units; 100 wallets take 30 flushes / 124,888 units across one UTC reset.
These expensive signals now finish rather than replaying completed phases, but
frequent such arrivals can still exceed daily delivery capacity. Outcome-heavy
or larger/denser workloads, repeated crashes/outages, and lower configured limits
can grow backlog. The demonstrated steady state has only 4,256 history units and
2,784 DO units/day spare after mature receipt cleanup. No guaranteed drain time
for the actual live backlog can be inferred without its payload mix/arrival rate.

## Bounded manual drain and legacy handoff

Use the existing ingest secret header on POST requests. Do not put the secret in
URLs, documentation, console output or committed files.

- `POST /api/runtime/history/flush`: one bounded flush and a fresh health update;
  returns counters/status. Budget failures return 429, other failures 503.
  Repeated authorized calls can supplement cron query throughput, not daily
  budgets. Wait for `next_attempt_at` or UTC reset; do not spin on 429.
- `POST /api/runtime/history/migrate-outbox`: bounded 25-event legacy D1 handoff.
  Raw legacy rows remain pending until the durable queue reports actual completed
  delivery. Repeat handoff/check calls between flushes until legacy pending drains.
  Oversized/invalid legacy events remain pending with their original payload.

Every accepted event retains its raw payload and persisted stage cursor through
failure/restart until genuine completion. Fragment batches are idempotent; replay
does not inflate edge evidence strength. Crash-lost history reservations may defer
work until UTC reset. The global structural lock has no lease-based takeover: an
active pending owner is never automatically cleared. It resumes after queue lease
expiry and releases only after scratch cleanup. There is no supersede/discard/
quarantine path that removes its pending owner. Manual DB/queue edits that orphan
an owner require investigation and recovery from preserved payload/cursor, not
blind unlocking or deleting queued work.

## Forward-only data repair limits

Migration labels old derived numeric data version 1. It cannot determine whether
a historical zero was real or an earlier serialization of unknown. Do not convert
all zeros to null, delete old episodes, or bulk mark old outcomes verified. Raw
episode events, observations, outcomes, edge evidence and historical memberships
are retained; raw detail exposes legacy values with trust/version metadata.
Trusted aggregates, scores and exports exclude unverified legacy outcomes/scores.

An eligible forward outcome correction needs independently verified historical
entry evidence (`entry_evidence_version=2`, matching frozen `caught_at`, positive
historical caught price or market cap) and a correctly timed horizon checkpoint.
Use a distinct correction event ID to preserve first-payload-wins semantics and
original raw event evidence. A present-day price is not verification of an old
entry. If historical provenance cannot be established, leave it unverified.

For legacy cluster repair, submit a distinct authenticated history event with
`event.event_type="cluster_repair"`, valid original episode identity/caught time,
and a valid `observed_at`. Existing catch wallets seed a resumable component job.
It retains edge evidence, excludes known infrastructure and retires old fragments
without deleting identities/memberships. It does not establish wallet ownership
or make legacy outcomes trustworthy. `HISTORY_INFRASTRUCTURE_ADDRESSES` accepts a
JSON address list for locally established additional public services; built-ins,
episode token/pool addresses and recognized source/service kinds are excluded.
Unknown infrastructure identities still require evidence/explicit classification.

Checkpoint readers have a fresh one-hour supersession grace, not an unbounded
reader pin. A reader stalled beyond that window must fetch the latest manifest
and retry integrity validation. Static immutable files omitted by a later Pages
deployment may return missing rather than mixed data; frontend fallback is owned
by the parent. These limits do not amount to a claim that historical data or the
live backlog is fully repaired.

## Reproduce locally

From `/tmp/solana-radar-audit-20261001`:

```sh
node --test cloudflare/scan-dispatcher/test/*.test.mjs
/tmp/radar-audit-venv-20261001/bin/python -B -m unittest tests.test_pages_publication tests.test_audit_publication_regressions tests.test_runtime_architecture tests.test_runtime_partitioning tests.test_history_coverage tests.test_robinhood
```

Verification after the release guard: combined Worker run passed all 97 tests,
including 10 schema/gating tests and the two-day mature receipt-store workload.
Python: 78 tests passed before this Cloudflare-only change. No network/API calls
are used by these regression fixtures. Original pre-fix scratch repros remain at
`/tmp/radar-audit-evidence-20261001`; they assert the old broken behavior and are
not expected to pass against remediated code.
