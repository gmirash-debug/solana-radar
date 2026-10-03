# Runtime reliability and shadow evaluation

## Unchanged strategy

Only Reactivation is enabled. The token-age window remains 24-360 hours;
existing market, retention, migration and data-quality gates are unchanged.
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

`RuntimeSnapshots` is a SQLite-backed Durable Object, separate from D1 quotas.
It stores a ready dashboard and private deep/discovery checkpoints. Checkpoints
retain buffers, provider-bound cursors, original cohorts, evaluation observations
and the shared monthly RPC ledger. Rebuildable top-level caches are omitted.
Atomic writes reject stale source times/revisions and never truncate a document.
Individual documents are bounded to 8 MiB. Checkpoints above 6 MiB encoded are
split into immutable 1 MiB pieces with content digests, then an atomic manifest
is committed only after all pieces are present. Decoding remains bounded to
128 MiB (192 MiB encoded). A failed upload never replaces the previous complete
checkpoint, and a corrupt or missing part never partially restores state.
Dashboard lists and full per-token details are separate documents; the ready
list references exact immutable detail versions, so source generations and
cohorts cannot mix. Per-token details are bounded to 6 MiB and 1,024 references.
Old unreferenced parts are collected in bounded batches after a one-hour staging
grace period; content-addressed unchanged parts are not rewritten. The limits
are explicit safety bounds, not an unlimited free storage promise.

Public dashboard projections and token details are produced inside the object
and streamed through the Worker. Health reads metadata, not the whole snapshot.
Private checkpoint/history ingestion requires the existing server ingest secret.
GitHub Actions cache is a backup/performance layer, not the only durable copy.
Graceful SIGTERM interruption follows the failed-attempt checkpoint path; an
abrupt machine failure or forced kill before that write cannot preserve new work.
When a snapshot is absent, the previous D1/static fallback remains available.

## Historical event outbox

The scanner first queues complete, idempotently identified historical events in
`HistoryQueue`, then attempts operational D1 publication. D1 quota exhaustion
therefore does not erase newly acknowledged event payloads. Every five minutes
a bounded flush writes to `RADAR_HISTORY_DB` and refreshes derived rows before
acknowledging the events. Failed/poison items back off; newer work can proceed.
New events use only this durable queue, not a second duplicate operational D1
outbox write. The pre-existing legacy D1 backlog continues draining separately.

The queue has explicit byte/row/write limits. Full or oversize batches are
rejected, not silently dropped. The existing local compressed outbox preserves
unacknowledged backlog and its batch cursors. Legacy backlog is replayed slowly;
invalid legacy event identities/times are kept in the cached outbox's quarantine
with their original payload, not submitted to analytics or silently erased.
Valid events in the same old snapshot can proceed. Price horizons are attributed
only to their original catch; a renewed thesis cannot inherit old outcomes.
it is not claimed to have been fully migrated. D1's free daily quotas are
account-wide: another D1 database does not create a new independent quota.
The Durable Object queue itself also uses bounded, estimated free-tier writes.

## RPC budgets and historical evidence

RPC attempts, including failed retries, reserve units in one UTC-calendar-month
ledger across targeted/deep passes. Defaults: Alchemy 25M estimated CU, Helius
900k estimated credits, Chainstack 2.7M estimated requests. These are safety
allowances, NOT account billing data: other applications and earlier untracked
usage are not included. Providers remain eligible only within their existing
per-pass/method limits and the shared estimated allowance.

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
