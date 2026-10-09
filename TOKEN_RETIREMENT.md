# Low-cap token retirement

This policy is independent from the $30k-$500k GMGN discovery range, the manual
blacklist, and wallet sale/transfer verdicts. Falling market cap is not proof of a
sale. Original holder retention does not prevent this explicit market-cap cleanup.

## Retirement

Enabled by `token_retirement_enabled` in the scanner and
`TOKEN_RETIREMENT_ENABLED=true` in the Worker. Apply storage migration
`0007_token_retirement.sql` to the canonical Turso database before enabling it.
Do not apply it to the old Durable Object or reset any storage epoch or quota.

Only tracked/caught tokens are monitored. A token must have positive, verified
market-cap quotes below $20,000 spanning at least 24 hours. Quotes must be at most
20 minutes old. Duplicate timestamps do not count. The maximum gap between price
observations is two hours; there must be at least 13 distinct observations (and
enough observations for the measured maximum gap). At $20,000 or higher, the
timer resets. A prolonged price-provider outage restarts it rather than treating
missing prices as a prolonged low. Missing, zero-default, negative, future, stale,
boolean and nonfinite prices cannot retire a token.

Do not infer an initial 24-hour low from the age of a single stored quote.
Existing tokens begin their observation window on a fresh verified quote. Market
refresh and lifecycle processing run before RPC analysis, including passes with
no new transaction history. Retirement is saved before later RPC failures can
leave an old private checkpoint as the current state.
The observation clock has its own small versioned SQL document, independent from
large private history checkpoints. An ambiguous write requires exact read-back.
If it cannot be acknowledged, the scanner keeps its local data and defers automatic
retirement rather than treating an unpersisted clock as authoritative evidence.
Local clock saves do not upload the full history checkpoint before RPC analysis.
Known low-cap tokens use a 15-minute public market-quote refresh TTL, within the
20-minute freshness contract. This reuses the existing batched market provider;
it does not add wallet-history RPC work. Other caught-token quotes keep their
existing refresh cadence.

## Return

Retirement is not a permanent blacklist. Discovery can analyze a previously
retired token again; none of its previous cohort, first-catch price/date, outcomes,
evaluation rows or receipt buffer may be used as a new entry.

A return requires all of these:

- Current membership in GMGN Trending or Hot Searches, verified within 30 minutes.
- A fresh current market cap within inclusive $30,000-$500,000.
- A new scanner `reactivation_wave` satisfying the existing detector rules.
- Its buy window starts after the authoritative retirement cutoff.
- Positive net buy flow and at least four dated on-chain execution-price samples.
- The median of its last two execution prices exceeds the first two by at least
  3%; both closing price samples are within 20 minutes of capture. Use the full
  candidate window, not the potentially truncated public event export. Ranking
  membership or a price bounce alone is insufficient.

The Worker validates the authenticated recapture evidence and acknowledges the
activation before publication. The new episode receives a new catch and cohort.
Old episode IDs remain fenced even after recapture. A concurrent old cleanup job
is scoped to its original cutoff and cannot remove a later legitimate episode.

## Storage And Recovery

Turso owns small retirement fences: token ID, cutoff, reactivation date and cleanup
cursor. A second ID-only index fences previously normalized or queued episode IDs.
Neither retains old wallet lists, balances, transaction bodies or market history.
Do not use `data/deleted_tokens.json` for automatic retirement: it is a permanent
manual discovery blacklist and has different restoration semantics.

The scanner removes retired pool/cohort state, discovery records, market cache,
buy buffers, alerts and local evaluation/outcome rows. Discovery checkpoints and
remote discovery ingestion respect the same fences. Public lists and direct token
details filter old snapshots; browser caches/static fallbacks also apply known
fences, including a retirement received with an older report timestamp.

SQL cleanup is resumable, bounded and independent of R2 availability. It removes
episode children, queued raw data, per-token legacy documents and parents. Shared
derived claims are invalidated and relationship proofs are adjusted; other tokens
and new post-cutoff episodes are retained. Native RPC/R2 accounting is not reset
or refunded by retirement. Each child deletion page is at most 128 rows, and each
cleanup invocation is at most 8 queries on the regular scheduler (12 on ingestion).
It defers while a derived-history lease is live. Cleanup shares the existing
scheduled SQL request budget rather than introducing another cron.

Dashboard detail blobs become unreferenced and expire after the existing one-hour
GC safety period. Shared compressed checkpoints are rewritten by their owning
scanner, never erased wholesale: deleting a shared blob would also destroy other
tokens. Immutable R2/GitHub backups are not edited in place and keep their normal
backup-retention lifecycle; restoring them must still load authoritative fences.
An unavailable fence authority stops processing instead of silently resurrecting
old positions. If the authority has been disabled unexpectedly, the scanner also
fails closed. Learning statistics after explicit data cleanup describe retained
samples only, not an unbiased all-time performance history.

## Verification

Python policy tests exercise the 24-hour/20k boundaries, gaps, duplicate quotes,
invalid prices, clean recapture and preservation of unrelated state/quotas.
Real SQLite Worker tests exercise authentication, epoch fencing, rollback,
resumable child deletion, replay triggers and concurrent recapture safety.
Native SQL-queue tests verify that retired raw events never request R2 or recreate
an episode. Browser source-selection tests cover stale static snapshots and
second retirement versus an older saved recapture.
