# Solana Radar

Local BBB-lite scanner for Solana meme pools.

Boundary: this project is a market/onchain alert scanner, not the primary narrative-discovery workflow. It may start from DEX/Helius/GMGN because it is looking for caught tokens. For any open-ended narrative scan, start from the top-level universal source-first router before using Solana Radar outputs.

It uses GMGN Trending and Hot Searches for candidate discovery, free DEX data for market resolution, and a routed Solana RPC layer for
onchain work:

- focus the production pipeline on one signal family: token reactivation;
- keep only migrated pump.fun ecosystem pools by default: `pumpfun-amm`, `pumpswap`;
- retain GMGN memberships and per-pool history checkpoints, while continuing to monitor already caught positions independently;
- rotate scan capacity between high-activity pools and pools that have gone longest without an onchain check;
- use Chainstack first for recent transaction details and token supply;
- use PublicNode for address signatures and token balances, and as the no-key
  standard-history fallback; use dRPC ahead of it when Solana is enabled on the
  configured dRPC plan;
- use Alchemy for paginated full address history and wallet balances, with
  Helius as the enhanced-history fallback;
- read the newest transaction tail first, then advance a separate bounded
  launch backfill only when it still fits the retained signal window;
- parse swaps;
- classify buy wallets as fresh, freshish, low-tx, normal, or dormant;
- attribute retention only to tokens bought in the detected wave, excluding balances held before the wave;
- enrich triggered alerts with public X activity through Bright Data Discover;
- write scanner-health diagnostics into the dashboard report;
- write alerts and a compact Markdown report.

## Setup

Create `.env` in the repository root or inside `solana-radar/`:

```bash
HELIUS_API_KEY=...
ALCHEMY_SOLANA_RPC_URL=...
CHAINSTACK_SOLANA_RPC_URL=...
DRPC_SOLANA_RPC_URL=...
DRPC_API_KEY=...
PUBLICNODE_SOLANA_RPC_URL=https://solana-rpc.publicnode.com
GMGN_API_KEY=...
BRIGHTDATA_API_KEY=...
```

`BRIGHTDATA_API_KEY` is optional. Without it, the scanner runs on market and
onchain data only.

Copy the config if you want to edit thresholds:

```bash
cp solana-radar/config.example.json solana-radar/config.json
```

If this folder is checked out as its own repository, use:

```bash
cp config.example.json config.json
```

## Run once

```bash
python3 solana-radar/scanner.py --once
```

Run the production lane:

```bash
python3 solana-radar/scanner.py --once --lane reactivation
```

## Local dashboard

```bash
python3 solana-radar/server.py --port 8765 --auto-lane reactivation --auto-interval-seconds 3600
```

Open `http://127.0.0.1:8765`.

The local dashboard reads the scanner's local runtime files, shows recent alert
history, and auto-runs the lane scanner. The published GitHub Pages dashboard
reads the current snapshot from the Cloudflare Worker backed by Turso; it falls
back to a compact static snapshot if the Worker is temporarily unavailable.
The scan button is only a force-refresh. Each scan also refreshes current
market snapshots for already caught tokens when their dashboard market data is
older than about one hour.

Narrative assignment follows [`NARRATIVE_PROTOCOL.md`](NARRATIVE_PROTOCOL.md):
one primary narrative per caught token, optional secondary flavor, source-ranked
token facts, and explicit labels for project/news overlays, ATH source, and
social status.

Holder concentration and wallet-link verification follow
[`SUPPLY_INTEGRITY_PROTOCOL.md`](SUPPLY_INTEGRITY_PROTOCOL.md). The protocol
keeps supply concentration, cohort retention, coordination evidence, and data
quality separate instead of compressing them into one unexplained score.

## Reading the radar

Start with **Ready to review**, not the raw score or number of candidates.
Confirmed activity is a reason to inspect a token, not a recommendation to buy.
**Holding** describes the original cohort, not a fresh entry. **Early observations**
and **Needs data** still lack confirmation; the card lists the missing evidence.

Check wallet and market timestamps independently. **Position left** uses the
original purchased position as its denominator; **Retained supply** uses total
token supply. A Solana balance cap is an upper bound, not a complete inventory
ledger. New purchases do not restore a previously reduced original position.
Without parsed swaps, reduced balances can mean either sales or transfers.

Review holder concentration, coverage, liquidity, and price extension in GMGN.
Shared CEX/Relay/router usage or fees do not establish common ownership.
Funding-linked sell/rebuy rotation is a risk hypothesis, not new accumulation.
Hover, focus, or tap terminology in cards to read its explanation.

### Publication reliability

The cloud publisher sends the operational list before bounded evidence batches.
Acknowledged batches are not resent after a failure. The latest snapshot takes
priority over the historical outbox, and old snapshots cannot overwrite newer
records. A pending historical replay does not by itself mean the current list
is stale: compare report time and `persistence.current_synced` separately.
The historical outbox is preserved until all its batches are acknowledged.
Historical analytics are processed independently in bounded background batches.
Five-minute discovery pauses while a deep/manual scan is queued or running, so
it cannot replace that scan in GitHub's single pending slot. The state-writer
lock remains shared to prevent concurrent cache overwrites.

Known limits: incomplete RPC windows remain unconfirmed; the largest-account
snapshot is a sample of up to 20 token accounts, not a full holder census.
Full Solana position lineage across transfers is not implemented. Funding checks
are bounded to three pre-buy transactions by default, not a complete source audit.

## Keep watching

```bash
python3 solana-radar/scanner.py --watch --lane reactivation
```

## GitHub automation

The repository includes `.github/workflows/scan-and-pages.yml`.

It runs the scanner at most once per hour, keeps its private runtime state in
GitHub Actions Cache, writes the current dashboard payload through the Worker to Turso,
and deploys a compact fallback snapshot to GitHub Pages. Hourly triggers share a
50-minute freshness guard: fresh reports are skipped before paid API work, while
stale reports trigger the full scan. Already running scans are not cancelled.
Scanner data commits do not retrigger the workflow, which prevents a
scan-commit-scan loop. Scanner failures are logged as workflow warnings and the
previous dashboard snapshot is preserved. A completed report separately exposes data
freshness and scan health (`healthy`, `degraded`, or `unhealthy`). GitHub Pages publishes
only a compact fallback snapshot; it never publishes the scanner's full runtime state.

- `data/dashboard_fallback.json`

Recommended production GitHub Actions secrets:

```bash
HELIUS_API_KEY
ALCHEMY_SOLANA_RPC_URL
CHAINSTACK_SOLANA_RPC_URL
GMGN_API_KEY
BRIGHTDATA_API_KEY
RADAR_INGEST_SECRET
```

At least one Solana RPC provider is required. For the intended production
layout, configure Helius, Alchemy, and Chainstack, then keep PublicNode as the
no-key standard fallback. `DRPC_SOLANA_RPC_URL` is optional and is used ahead of
PublicNode only when Solana access is enabled on that dRPC plan. `DRPC_API_KEY`
is supported as an alternative to the full dRPC endpoint URL. Complete
Alchemy/Chainstack/dRPC endpoint URLs are preferred; `ALCHEMY_API_KEY` is also
supported as an alternative to the full Alchemy endpoint. Verify that the dRPC
key's plan includes Solana before setting either dRPC variable; otherwise leave
both unset and the scanner will use the remaining providers.

`GMGN_API_KEY` is required for the production attention discovery mode. It supplies
Trending and Hot Searches across all configured windows, token metadata, and
ATH market cap/date. There is no unrelated-pool fallback when these lists fail.
`BRIGHTDATA_API_KEY` can be empty if social enrichment should be disabled.

Production uses two scan layers:

- `Reactivation discovery pulse` runs every 5 minutes without Solana RPC calls.
  It refreshes both GMGN Trending and Hot Searches for `1m`, `5m`, `1h`, `6h`,
  and `24h`, then updates current market snapshots, quiet-regime baselines,
  and the priority queue through the Worker into Turso.
- `Scan and deploy dashboard` runs the deep onchain pass hourly. It restores raw
  cursors and swap buffers from GitHub Actions Cache, loads the latest discovery
  context from Turso, scans selected pools, and publishes the dashboard.

Turso is the production SQL and runtime backend; the Cloudflare Worker provides
authenticated ingestion and public reads. GitHub Pages keeps a compact fallback.
Raw buffers, cursors and wallet caches have content-addressed SQL checkpoints;
Actions Cache is secondary recovery, not the durable acknowledgement. Runtime
publication saves the lightweight list first, then generation-bound token
details. Full evidence remains unchanged in the per-token documents.

To replay the latest saved publication without scanning, dispatch
`Scan and deploy dashboard` with `source=publication-recovery`. It uses the
global writer lock, restores the outbox, preserves the original scan timestamp,
and never deletes outbox files, invokes RPC or rebuilds Pages.

History ingestion uses a bounded SQL queue, with raw archive copies in private R2.
Fresh episodes have 512 reserved queue places and 90,000 reserved work units
within the unchanged 180,000 daily allowance; their older prerequisite events
retain source order and original catch times. Cold recovery cannot consume this
reservation. Pending full snapshots move to content-addressed private R2 only
after both object verification and a Turso manifest acknowledgement. A bounded
`storage-recovery` pass every six hours restores checkpoints, archives the old
outbox and resumes its SQL delivery without RPC or Pages work. R2 guard failure
keeps the local original. The private backup repository separately copies SQL
and the R2 bodies, verifies recovery checksums and retains confirmed copies.
R2 pauses fail closed if its budget guard is unavailable. Legacy DO/D1 data is
retained for explicit migration and rollback; new runtime writes are not mirrored
there. Legacy receipt export is read-only, up to 500 rows per page, capped at 25
pending full events and 1 MiB. See `INFRASTRUCTURE_CAPACITY_PLAN_2026-10-04.md` for
remaining blockers, actual checks and unverified capacity assumptions.

Cloudflare production setup:

```bash
# GitHub Actions secret and Worker secret with the same value
RADAR_INGEST_SECRET=...
```

Production uses the Worker's Turso SQL backend. See
`INFRASTRUCTURE_CAPACITY_PLAN_2026-10-04.md` for backend configuration and recovery.
The scanner syncs a compact report, alert history, token market rows, discovery
baselines, queues, and signal outcomes through the protected Worker ingestion API.
`cloudflare/scan-dispatcher/migrations/0001_radar_data.sql` belongs to the retained
legacy D1 backend; applying it alone does not configure production Turso storage.

The Cloudflare delete worker writes Delete/Restore actions to GitHub and mirrors
the deletion index to D1.

Production lane:

- `reactivation`: GMGN attention candidates, all ages. Discovery does not exclude tokens by migration, mcap, or liquidity; these remain entry-risk and signal-quality context. A signal still requires distributed net buying and current holder retention. ATH is context, not a discovery gate.

RPC roles and safeguards:

- Alchemy: enhanced paginated address history.
- Chainstack: standard transaction and supply calls.
- dRPC: optional signatures/balance fallback when its plan permits Solana.
- PublicNode: no-key signatures, balances, and standard-history fallback.
- Helius: fallback and health coverage.
- Each provider has a per-scan credit budget. The router fails over before the
  configured budget is exceeded and reports per-method p50/p95 latency.
- Every scan reserves capacity for live discovery, unresolved history gaps, due
  signal rechecks, and oldest-pool rotation.

Signal quality:

- Routed swaps are attributed to the final token recipient only when ownership
  resolution is high-confidence.
- Top wave buyers are checked for common funders and common routed executors.
  Connected addresses count as one effective buyer cluster.
- Each caught cohort gets a separate Supply Integrity snapshot: verified token
  supply, the 20 largest token accounts, resolved owners, raw concentration,
  estimated circulating concentration when the pool reserve is identifiable,
  cohort overlap with top holders, and linked-wallet supply concentration.
- A matching priority fee is supporting evidence only. Coordinated supply is
  asserted only when at least two independent evidence families converge in the
  same connected cohort cluster; missing owner or pool-reserve data is shown as
  unverified instead of being guessed.
- Full holder and linkage evidence is stored in the token detail document in
  Turso. The main dashboard payload keeps only the compact decision fields, and up
  to 56 point-in-time snapshots preserve roughly one week of three-hour checks.
- A ready, continuous quiet-regime baseline with a confirmed activity break is
  required for confirmed Reactivation. Version-1 quiet periods are not trusted;
  after an observation gap over 90 minutes, quiet duration starts again.
- First-catch outcomes are tracked at 1h, 6h, 24h, and 72h, including maximum
  favorable and adverse movement.

Signal lifecycle:

- `Candidate` preserves early observations without calling them confirmed
  accumulation. A wallet class or a high score alone is insufficient. Complete
  onchain coverage, a verified quiet-regime break, at least six checked buyer
  groups covering 60% of observed buy flow, and seasoned retention are required.
- A wave waiting only for retention seasoning can become confirmed after a
  complete later balance check of its original cohort, with at least 80% of
  attributed tokens and 65% of holders remaining. This produces `Holding`, not
  a new entry recommendation. Missing baseline/graph/history evidence cannot
  be repaired by checking balances alone.
- The default `all tracked` view contains confirmed signals, holding cohorts,
  candidates and overdue checks with their distinct statuses. Closed signals
  and noise stay outside this view. `confirmed + holding` is an optional strict
  view; the Tracked counter returns to the full tracked list. Candidate alerts
  remain available from history even when absent from the latest scan.

- The first Reactivation alert persists its qualifying buyer cohort and the
  number of signal-attributed tokens each wallet retained.
- A later qualifying alert is retained as a pending cohort. If the original
  thesis is eventually invalidated, the latest pending cohort is promoted
  automatically instead of losing the newer Reactivation setup.
- The scanner rechecks that same cohort on a reserved hourly monitor queue.
  Due cohort rechecks take priority over new discovery-queue candidates.
  A separate balance-only pass checks the oldest due cohorts, capped at 200
  wallet balance attempts per scan within the existing provider credit limits.
  An unconfirmed cohort may be replaced by a newly confirmed wave, but its
  confirmation must never be copied onto a different older cohort.
- `Accumulation intact` means the tracked cohort still retains the original
  accumulation, while `Weakening` means it has distributed a material share.
- `Recheck due` is used when the scan is stale or wallet coverage is
  insufficient. Missing data never invalidates a signal.
- `Inactive` is assigned only after sufficient wallet and token coverage
  confirms on two consecutive checks that both the retained token amount and
  the breadth of original holders have collapsed. A low balance alone or a
  single incomplete check cannot invalidate the signal.

Persistence and outcome integrity:

- Failed cloud snapshot writes remain in a compressed local outbox, preserved
  across Actions runs in a separate cache. Each scan retries two oldest writes
  and its current snapshot; an entry is removed only after acknowledgement.
  `Cloud save pending` is separate from successful scanning. GitHub cache is a
  recovery mechanism, not a permanent archive; sustained outages still need
  operator attention. No paid-plan change is made automatically.
- SQL history delivery uses the existing pending-status index and recalculates
  baselines once per batch, not once per event. Derived-write failures leave
  events pending. A stale snapshot can still deliver historical ledger events.
- Endpoint outcomes more than one hour late are excluded from horizon metrics
  and new wallet-score calculations. Prices take precedence over market cap for
  returns. Historical cached scores are not a validated trading backtest.
- Method-specific RPC plan restrictions disable only that method on that
  provider; real authentication/quota errors still block the provider route.

Run `npm test` for regression coverage of these contracts. Hourly deep scans
and five-minute discovery retain their existing cadence. Fewer confirmed results immediately after rollout are
expected while fresh baseline and cohort evidence accumulates.

Disabled filters: `micro_sticky`, `cheap_sticky`, `breakout`, `incubation`, and
`young` are not scanned or shown in the production dashboard. Their old alert
records remain in Git history but cannot consume Reactivation monitor capacity.

The production lane uses `discovery_source_mode: gmgn_attention`. The legacy
`dex_allowlist` does not exclude GMGN candidates or their subsequent holding
checks. A token still needs a resolvable Solana market; unresolved tokens stay
in the candidate registry and are retried rather than replaced by random pools.

## Outputs

- `solana-radar/data/state.json` - private scanner runtime state. It is cached
  in GitHub Actions and never published.
- `solana-radar/data/alerts.jsonl` and `latest_report.*` - private local scan
  artifacts. Turso is the production source for the dashboard.
- `solana-radar/data/dashboard_fallback.json` - compact public fallback for
  Pages. It has a short raw-alert window; the full dashboard history remains in Turso.
- `solana-radar/data/deleted_tokens.json` - small scanner blacklist for false
  catches deleted from the dashboard.

## Notes

The market universe is the union of GMGN Trending and Hot Searches across
`1m`, `5m`, `1h`, `6h`, and `24h`. Presence in either list is sufficient; presence
in both raises priority. Trending uses GMGN's default ranking (top 100 per window);
Hot Searches requests up to 500 per window. These are bounded API rankings,
including GMGN's server-side defaults, not a complete market census or a
guarantee of exact website membership. DexScreener resolves pools and refreshes
prices, but does not introduce unrelated candidates. A missing GMGN list is
reported as partial/unavailable, never a healthy empty result. Membership has
a 60-minute grace period for failed list reads. Successful snapshots remove
absent memberships immediately; previously caught positions keep independent
holding checks, but cannot produce new off-list signals. The old 1,000-pool
light-universe cutoff does not truncate GMGN candidates.
Discovery-state merges compare the list snapshot's observation time, not the
last positive token sighting. Newer removal records win, and older snapshots
cannot reactivate dropped candidates. Local market resolution and deep cursors
remain independently preserved; candidate counts include any valid failed-list
grace memberships and expose the raw snapshot count separately.
If DexScreener cannot resolve an admitted token, GMGN Token Info supplies a
token-identity-checked pool. This queue uses at most 12 lookups per deep scan,
two per targeted check or four per discovery pulse, caches the pool and retries
missing data after 15 minutes. It cannot introduce an off-list token.

Every selected batch first reads one bounded current history head per pool
before cohort monitoring or buyer analysis. Those exact reads are reused by the
detector. Initial-window tails remain resumable; expensive analysis gets a fair
share of each provider's remaining per-scan allowance, without increasing the
monthly limits. Cursor changes are staged until parsing succeeds. Diagnostics
separate full-universe, selected, head-read and analyzed counts, and distinguish
per-scan caps, monthly safety caps, temporary cooldowns and ledger failures.
Even a clean probe advances an unfinished tail by one bounded page; otherwise a
candidate with no immediate buy-wave could remain permanently pending. This
repair reuses the just-read head and stays inside the same fair per-pool budget.
An unavailable balance/supply verification does not discard already parsed
history or emit a confirmed signal. History coverage and wallet-evidence retry
are recorded separately, so the next pass can reuse trades and retry evidence.
Decode errors still roll back cursors, and programming errors still fail loudly.
The first GMGN history context is independent of legacy all-pool cursors. A
legacy checkpoint is retained under `candidate_previous_history_checkpoint`,
while the active read starts at the anchored GMGN window. Already parsed swaps
seed deduplication, and the one-time transition never changes the original
cohort or caught dates. Decode failure restores the previous context.
RPC reservations use unique allocation IDs: a lost write response can be
recovered only by reading back that exact grant, counters and source version.
At most one transient retry sends the identical reservation, never a new charge.
Small grants are now 250 Helius credits, 2,000 Alchemy CU and 500 Chainstack RU,
reducing SQL acknowledgement round trips. Unused reservations remain charged
conservatively; monthly limits are unchanged, and unverified grants stay blocked.

First analysis is anchored to six hours, or 24 hours for tokens present only
in the long (`6h`/`24h`) rankings. Retries keep the original lower boundary.
Afterwards, only new trades since the committed checkpoint (with a small
deduplicated overlap) and unfinished history pages are requested. An unchanged
ranking does not trigger transaction-detail replay. Checks are spaced at least
15 minutes apart; pending history retries use five minutes. Existing holding
checks remain independent of these history checks. Provider quotas and bounded
page budgets remain in force, so partial coverage stays explicitly unconfirmed.
Parser failures never advance the history checkpoint. The private runtime
checkpoint persists memberships, covered time ranges, pending cursors, and
recently parsed signatures; the dashboard exposes list/window membership.

GMGN token info supplies ATH market cap. The scanner locates its timestamp with
a bounded `1d -> 1h -> 5m` K-line search. Reactivation does not wait for ATH
before scanning: unknown or high-range ATH context can cap conviction, but it
cannot hide a strong early buy-wave. The more expensive timestamp lookup remains
limited to dashboard enrichment candidates.

Onchain buy extraction routes each method to the provider that fits it best.
Chainstack is first for recent transaction details, token supply, and health
checks. Alchemy is first for `getTransactionsForAddress` in full/jsonParsed
mode, while Helius is its enhanced fallback. dRPC, when configured, and then
PublicNode handle `getSignaturesForAddress` and token-account balances before
Alchemy, preserving Alchemy credits for paginated history. Chainstack is
skipped for `getSignaturesForAddress` and
`getTokenAccountsByOwner`, which are not available on its free Developer plan.
Pagination cursors are pinned to the provider that created them. If that
provider fails mid-window, the scanner restarts the same bounded time range on
the next enhanced provider and deduplicates by signature, so it never reuses an
Alchemy cursor on Helius or vice versa. A cursor that is still incomplete after
ten minutes is restarted from its last observed head, so the next hourly scan
cannot remain stuck reading an old tail while new buys happen. If every
enhanced-history provider is unavailable, the scanner falls back to standard
signatures plus transaction details.

Market activity is ranked using both 5-minute burst data and the 1-hour window,
then checked against the pool's standard RPC transaction
head. The tolerated lag scales with reported hourly transaction count. If the
standard head is fresh but enhanced history is behind, the pool is rescanned
through the signatures fallback. If both heads are old, the market snapshot is
marked stale and moved out of Reactivation priority for six hours. A material
change in mcap, volume, or transaction count rearms it immediately, and normal
rotation can still audit it during the cooldown.

Alchemy requests are paced at 450 ms by default and use exponential retries.
`getSignaturesForAddress` has a stricter 1.5-second interval when it falls back
to Alchemy. Chainstack is paced at 220 ms to stay below its Developer-plan
five-request-per-second limit without changing the hourly scan schedule.

Each hourly run checks the newest edge of the market first, with a rolling
overlap that feeds the retained swap buffer. A shallow probe is evaluated
together with that buffer; a deeper scan is triggered only by suspicious wallet
classes, linked wallets, material flow, a sticky/wave precheck, an alert-level
score, or a scheduled audit slot. In GMGN attention mode, a complete probe is
reused and a deeper fetch continues its unfinished pages rather than fetching
the same head again. Legacy composite mode retains launch backfill separately.

Already caught tokens get a separate hourly market refresh through DexScreener.
That pass updates dashboard `Market now` fields without re-running expensive
Helius wallet analysis for every historical catch.

When a Bright Data key is present (`BRIGHTDATA_API_KEY`, `BRIGHT_DATA_API_KEY`,
or `BRIGHT_DATA_API_TOKEN`), only triggered alerts are enriched with X search
results. This keeps costs controlled: the scanner does not call Bright Data for
every pool in the universe.
