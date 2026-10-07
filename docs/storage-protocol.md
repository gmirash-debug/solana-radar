# Storage Protocol

Production generation: `20261007-clean-v1`.

## Keep Data For A Purpose

| Data | Update Frequency | Retention |
| --- | --- | --- |
| GMGN Trending and Hot Searches membership | Every 5 minutes | Latest membership; inactive candidate registry for 7 days |
| Current market and discovery queue | Every discovery pulse | Untracked market/pool entries for 7 days; queue items expire under their configured task TTL |
| Activity baseline | Every discovery pulse | 5-minute buckets for 48 hours; hourly buckets for 7 days; inactive baseline containers for 14 days |
| Processed transaction signatures | Incremental onchain passes | 24 hours, bounded dedup index; incomplete history remains explicitly incomplete |
| Runtime recovery checkpoint | After a state writer | Immutable content-addressed parts; unchanged parts are reused; superseded/unreferenced parts have a 60-minute reader/upload grace |
| Current signal cohort, catch evidence, balances and cursors | At capture and scheduled rechecks | An active or unresolved position never expires merely because it is old |
| Local closed/invalidated thesis | On close and subsequent passes | 30 days, then normal state compaction may retire it |
| Compact alert list | On a completed scan | 7 days, capped by the existing token/alert limits |
| Delivered history-outbox duplicates | Hourly maintenance | At least 7 days; remove only with an identical canonical event and no pending dependency |
| Scan diagnostics | On a completed pass | 30 days; retain rows referenced by a signal episode |
| Durable episodes and wallet evidence | New evidence / rechecks | Do not discard pending or active evidence; closed-episode expiry must prove completed outcomes and absence of replay dependencies |
| RPC monthly reservations | Before paid-provider work | Never reset or refund as a side effect of deleting trading results |
| R2 operation/storage guard | Before R2 operations | Independent Durable Object; unchanged by a SQL reset |
| Verified private backups | Daily | Last 7 verified recovery points; source deletion does not create a new backup |

The scanner's admission scope is unchanged: GMGN Trending OR Hot Searches in
any configured window, current verified market cap $30,000-$500,000 inclusive.

The closed-episode collector supports a 90-day terminal-closure contract, but
is deliberately not enabled for ordinary `invalidated` signals. Low balances
or unresolved sale/transfer history do not prove a terminal close. Until a
producer supplies `terminal-closed-v1` evidence, these compact learning records
remain protected. This restriction does not retain obsolete runtime copies.

## Prevent Full Copy Amplification

Checkpoint compression is partitioned before content-addressing. A metadata
revision must not invalidate all token/pool parts. Transport encoding, hashes,
decoded-size bounds and atomic complete-manifest publication remain verified.
The writer sends only missing parts. Pending evidence is not truncated to fit a
storage budget.

The runtime garbage collector runs on the 5-minute pulse, with at most 2 batches
of 256 candidates and 16 SQL calls. It rechecks current references, restaging
time and the grace cutoff inside deletion. Hourly historical cleanup shares the
same bounded budget discipline. GC errors are reported, not silently ignored.
`GET /api/storage/retention` is an authenticated, read-only preview.

The 256 MiB runtime target is an operational target, not a promise about the
entire SQL database. Historical evidence and indexes are measured separately.
The fresh cloud database has a 4 GB maximum to leave headroom below the 5 GB
organization free-storage allowance. This is a last-resort safeguard, not a
replacement for retention or monitoring.

## Clean Restart

1. Freeze the Worker writers and scheduler; drain or cancel existing writers.
2. Retain an existing verified backup separately. A quota-blocked export may
   fail and must not be described as a new successful recovery point.
3. Build an empty schema with `tools/prepare_storage_reset.py`. Merge RPC
   counters conservatively from the verified SQL backup and a fresh published
   report. Preserve manual token exclusions. Do not import old trading tables.
4. Delete/recreate only the explicitly selected SQL database. Preserve R2,
   its independent budget guard and private recovery copies.
5. Rotate database credentials for the same Worker and read-only backup
   recipient; no account upgrade or overage enablement is part of this procedure.
6. Advance shared runtime and outbox cache namespaces. Disable legacy DO
   runtime reads and history migration. Require the new storage-epoch header
   on API writes so an older branch/run cannot repopulate the new database.
7. Verify schema, ledger counters, empty trading tables, retention preview,
   publication and an actual bounded scan before unfreezing scheduling.

A free-plan organization can remain blocked after deleting its database if
monthly sync/read/write quotas are exhausted or storage usage has not been
recalculated. Never claim the restart is complete before the new cloud DB is
actually reachable. Paid-plan activation requires the user's separate action.

## Pending Provider Unblock

On 2026-10-07 the user explicitly requested a clean restart. The old cloud DB
was deleted; creating its replacement was rejected by the organization's
monthly quota block. Support was contacted at `support@turso.tech`.
No paid plan or overages were enabled. The source configuration remains frozen
and the Solana scan, discovery, watchdog and private SQL-backup workflows are
paused. R2 monitoring and private R2 backups remain enabled.

After provider unblock: regenerate the verified empty seed with migration
`0006`, create `solana-radar` in `radar-eu`, rotate equivalent DB credentials,
verify preserved RPC counters and retention, and update the source freeze flags
before reenabling workflows. Set `storage_maintenance=false` only after the
new database is verified. This CLI guard prevents an independently scheduled
GitHub job from spending RPC while the Worker is frozen. Do not restore old
`state-v4` or `outbox-v1` caches. Use the already prepared verified empty seed;
do not rederive monthly counters from the empty public maintenance snapshot.

## R2 Is Not An Unlimited Replacement

Archive unique evidence, not a full mutable checkpoint at every scan. Keep the
existing 80% warning and pre-90% fail-closed guard. Batch PUT reservations can
finish late and their storage tombstones are intentionally conservative:
deleting objects is not permission to decrement counters blindly. A safe
fenced reconciliation is required before treating released storage as reusable.
