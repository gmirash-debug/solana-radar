# Runtime reliability and shadow evaluation

## Active strategy

Only Reactivation is enabled. The pool-age window is 0.5-360 hours (30 minutes
to 15 days), measured from the existing pair creation timestamp;
existing market, retention, migration and data-quality gates are unchanged.
The age gate admits earlier candidates; it does not shorten confirmation,
holding, baseline or purchase-analysis windows.
No supporting movement or statistical evidence confirms an alert by itself.

## Scheduling and fair bounded work

- Cloudflare discovery pulse: every five minutes, market observations only.
- Deep scanner: hourly at minute 7 UTC, up to the existing 40-pool limit.
- Targeted scanner: minutes 22, 37 and 52 UTC, up to six unexpired queued
  candidates or due theses. Same detector and shared state; lower RPC limits.
- One shared GitHub state-writer lock. Targeted/discovery dispatch checks the
  deep workflow first and does not replace an active or pending deep scan.
- A large original cohort is checked in pieces. Each piece keeps its actual
  timestamp; a partial cycle is unknown, not evidence of sale or a full check.
  A cycle expires after two hours; a completed cycle is not an atomic snapshot.
- Balance-only targeted passes publish updated cohorts without inventing new
  alerts or advancing the deep-scan date. An idle pass leaves the previous report
  and successful-scan time intact.
- The extra balance allowance is 200 for deep and 60 for targeted passes.

Timing is best effort: GitHub outages, long runs and exhausted provider budgets
can postpone a pass. These crons are not a guaranteed fifteen-minute SLA.

## Durable operational state

With `RUNTIME_STORAGE_BACKEND=turso_sql`, Turso stores the ready dashboard and
private deep/discovery checkpoints. Legacy Durable Objects and D1 remain read
fallbacks, not the primary write path. Checkpoints
retain buffers, provider-bound cursors, original cohorts, evaluation observations
and the shared monthly RPC ledger. Rebuildable top-level caches are omitted.
Atomic writes reject stale source times/revisions and never truncate a document.
Individual documents are bounded to 8 MiB. Checkpoints above 256 KiB encoded are
split into immutable 256 KiB pieces with content digests, then an atomic manifest
is committed only after all pieces are present. Decoding remains bounded to
128 MiB (192 MiB encoded). A failed upload never replaces the previous complete
checkpoint, and a corrupt or missing part never partially restores state.
Dashboard lists and full per-token details are separate documents; the ready
list references exact immutable detail versions, so source generations and
cohorts cannot mix. Per-token details are bounded to 6 MiB and 1,024 references.
Old unreferenced SQL parts are collected in bounded batches after a 48-hour
restore grace period; content-addressed unchanged parts are not rewritten. The limits
are explicit safety bounds, not an unlimited free storage promise.

Public dashboard projections and token details are read from SQL through the
Worker. Health reads metadata, not the whole snapshot. New checkpoint writes
and dashboard reads do not require R2 or a writable Durable Object.
Private checkpoint/history ingestion requires the existing server ingest secret.
GitHub Actions cache is a backup/performance layer, not the only durable copy.
Graceful SIGTERM interruption follows the failed-attempt checkpoint path; an
abrupt machine failure or forced kill before that write cannot preserve new work.
When a snapshot is absent, the previous D1/static fallback remains available.

Checkpoint data and its runtime envelope use canonical sorted JSON. Saving and
reloading the same state must preserve both the decoded digest and wire envelope.
Legacy format repair verifies the complete old checksum and canonical contents
first; it cannot promote a true same-version evidence conflict. A verified
format migration advances only the private revision, preserving observed/caught
dates and RPC counters. Storage recovery has a bounded 420-second workflow window,
performs no RPC calls, and reports failure when the root is unacknowledged.
Successful checkpoint recovery can still leave historical replay partial.

## Historical event outbox

The scanner first queues complete, idempotently identified historical events in
the Turso SQL history queue. Hourly and post-publication bounded flushes refresh
historical and derived SQL rows before acknowledging events. Failed/poison items
back off. Current episodes have 512 additional reserved slots above the 2,048
cold ceiling and 90,000 of the existing 180,000 daily history work units reserved
for live work. This does not increase the daily allowance or remove the shared
byte limit. Earlier episode prerequisites remain ordered before recent observations.
New events use only this durable queue, not a second duplicate operational D1
outbox write. The pre-existing legacy backlog continues draining separately.

The queue has explicit byte/row/write limits. Full or oversize batches are
rejected, not silently dropped. The existing local compressed outbox preserves
unacknowledged backlog and its batch cursors. Legacy backlog is replayed slowly;
invalid legacy event identities/times are kept in the cached outbox's quarantine
with their original payload, not submitted to analytics or silently erased.
Valid events in the same old snapshot can proceed. Price horizons are attributed
only to their original catch; a renewed thesis cannot inherit old outcomes.
Cold backlog enqueue yields at 128 pending durable events, using actual enqueue
responses, to retain headroom for current passes. A deferred old batch is not
acknowledged, removed or classified as a scanner failure. The legacy archive
is not claimed to have been fully migrated. D1's free daily quotas are
account-wide: another D1 database does not create a new independent quota.
The active queue's data and control writes use Turso, not Durable Object storage.

## Cold originals and independent backups

An unacknowledged local snapshot can move to immutable, deterministic gzip in R2.
The local original is removed only after R2 confirmation and a Turso manifest/
progress acknowledgement. Source dates remain immutable; replay cursors are
monotonic. Invalid event rows remain in the original archive even when excluded
from analytics. Successful runtime publication disables the duplicate legacy
snapshot upload; history delivery remains independent.

The private backup repository retains seven verified full SQL recovery points
and two verified R2 body recovery points. SQL backups verify schema and typed
row digests from one guarded read transaction and check a restored SQLite copy.
R2 backups copy and verify actual bodies, not only manifests, using up to four
concurrent read-only batches of sixteen objects / 8 MiB; larger bodies use an
individual bounded read. Every body, shard and uploaded asset is checked.
Both backups retain prior good copies on failure. They are separate snapshots,
not one atomic cross-service backup, and R2 reads still consume guarded quotas.

## RPC budgets and historical evidence

RPC attempts, including failed retries, reserve units in one UTC-calendar-month
ledger across targeted/deep passes. Defaults: Alchemy 25M estimated CU, Helius
900k estimated credits, Chainstack 2.7M estimated requests. These are safety
allowances, NOT account billing data: other applications and earlier untracked
usage are not included. Providers remain eligible only within their existing
per-pass/method limits and the shared estimated allowance.

### Task-specific RPC routing

Routes use capabilities and native budgets, not a comparison between unrelated
provider credit units. Defaults and per-method overrides live in `rpc_routing.py`
and `rpc_method_provider_orders`:

| Task | Primary | Fallback |
| --- | --- | --- |
| Full transaction, supply, parsed account batches | Chainstack | Helius, Alchemy, configured public reserves |
| Owner balances and signature lists | Helius | Alchemy, configured public reserves |
| Largest token accounts | Alchemy | Helius, configured public reserves |
| Live indexed pool pages | Alchemy | Helius |
| New launch backfill and receipt-position history | Helius | Alchemy |
| Market discovery and reported ATH | GMGN | Existing market/registry fallbacks, never synthetic RPC ATH |

Existing indexed cursors remain pinned to their originating provider. Live
window failover restarts the bounded window without the old cursor and deduplicates
signatures. Shadow receipt checks fail closed if their provider changes. Routing
does not make a query-exhausted history sample proof of complete archive coverage.

Known Chainstack shared-node restrictions skip owner, signature and largest-account
indexed reads. Newly learned unsupported methods persist for 24 hours and reset
when the endpoint changes. Rate-limit/temporary failures cool down only that method;
other reads continue. Auth or actual account quota failures disable the provider.
A local budget too small for a costly method does not disable remaining cheap reads.
Attempts and retries reserve the per-scan allowance, not only successful responses.

Missing/malformed balance lists are unavailable, not zero. An explicit empty
account list is a valid zero. Null transaction details try another archive source.
GMGN 1.6.1 Trenches sorting is local: one server query replaces repeated identical
completed-list requests, without claiming full pool coverage.

`tools/probe_rpc_routes.py` and the manual `Read-only RPC routing check` workflow
test the configured credentials without touching scanner state. They report method
access and bounded 7-15-day history samples, not full-market or full-archive coverage.
Runtime provider stats include task assignments, null counts, unsupported methods,
attempted units and native estimated monthly usage. Never print endpoint credentials.

Live gap repair retains its own cursors. A separate launch task investigates one
pool's first six hours per deep pass with bounded pagination and independent
cursors. Pool creation is not necessarily token creation; cursor exhaustion
does not prove complete archival coverage or current holdings.

At capture, exact raw receipt components can be frozen once per thesis. A later
buy never replaces them. One reduced position task per deep pass can follow up
to two owners, four history pages, 64 transactions and twelve token accounts,
with a finalized horizon and receipt-slot boundary verification. Transfer,
service custody, proven protocol sale and unresolved flow remain distinct.
Public infrastructure does not establish common ownership. This evidence stays
shadow-only and does not alter original-cohort retention or invalidation.
Older catches without original raw receipts remain unavailable, not backfilled
from later buys. The adapter currently has no protocol sale-witness decoder.

## Prospective measurement

`signal_evaluation_dataset` captures entry features once, actual fresh market
times and explicitly fully-scanned non-signal controls. It never reconstructs
entry features from later balances. One first observation per horizon is frozen;
late/missing values are not zero returns. Versions, controls, price coverage,
cost assumptions and the October 17 holdout boundary remain explicit.
Collection is bounded to 5,000 signal and 5,000 control rows; pruning is counted.
Learning shows a compact shadow summary, not a claim of a profitable strategy.
See `SIGNAL_EVALUATION.md` for sampling and statistical limitations.

## Verification and rollout

Run all Python tests and all Worker/dashboard JavaScript tests. Deploy the Worker
with its SQLite Durable Object migrations, merge the scanner/UI changes and run
one real deep scan. Verify a ready durable snapshot, acknowledged checkpoints,
budget metadata, queued history and the unchanged filter before calling rollout
complete. Check mobile/desktop Learning and token details independently of D1.
No paid storage/RPC upgrade or R2 binding is required by this change.
