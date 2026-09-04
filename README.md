# Solana Radar

Local BBB-lite scanner for Solana meme pools.

Boundary: this project is a market/onchain alert scanner, not the primary narrative-discovery workflow. It may start from DEX/Helius/GMGN because it is looking for caught tokens. For any open-ended narrative scan, start from the top-level universal source-first router before using Solana Radar outputs.

It uses free DEX data for market discovery and a routed Solana RPC layer for
onchain work:

- focus the production pipeline on one signal family: token reactivation;
- keep only migrated pump.fun ecosystem pools by default: `pumpfun-amm`, `pumpswap`;
- retain a rolling registry of known pools and refresh it through free market APIs, so discovery is not limited to current trending pages;
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
reads the current snapshot from the Cloudflare Worker backed by D1; it falls
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

## Keep watching

```bash
python3 solana-radar/scanner.py --watch --lane reactivation
```

## GitHub automation

The repository includes `.github/workflows/scan-and-pages.yml`.

It runs the scanner at most once per hour, keeps its private runtime state in
GitHub Actions Cache, writes the current dashboard payload to Cloudflare D1,
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

`GMGN_API_KEY` is strongly recommended. It supplies migrated Pump.fun Trenches,
multi-window trending discovery, token metadata, and ATH market cap/date.
`BRIGHTDATA_API_KEY` can be empty if social enrichment should be disabled.

Production uses two scan layers:

- `Reactivation discovery pulse` runs every 5 minutes without Solana RPC calls.
  It uses a narrow low-cost GMGN set (`1m`/`5m` volume and swaps plus two
  Trenches rankings), updates current market snapshots, quiet-regime baselines,
  and the priority queue in Cloudflare D1.
- `Scan and deploy dashboard` runs the deep onchain pass hourly. It restores raw
  cursors and swap buffers from GitHub Actions Cache, loads the latest discovery
  context from D1, scans selected pools, and publishes the dashboard.

Cloudflare D1 is the primary dashboard read path. GitHub Pages keeps a compact
snapshot fallback. Market/baseline/outcome data is stored one row per token; raw
transaction buffers, RPC cursors, and the wallet cache remain only in the Actions
runtime cache. A temporary D1 outage does not stop the hourly scan: it publishes
the prior verified fallback and retries the remote sync on the next run.

Cloudflare production setup:

```bash
# GitHub Actions secret and Worker secret with the same value
RADAR_INGEST_SECRET=...
```

Apply `cloudflare/scan-dispatcher/migrations/0001_radar_data.sql` to the bound
`solana-radar` D1 database, then deploy the worker. The scanner syncs a compact
report, alert history, token market rows, discovery baselines, queues, and signal
outcomes through the protected Worker ingestion API.

The Cloudflare delete worker writes Delete/Restore actions to GitHub and mirrors
the deletion index to D1.

Production lane:

- `reactivation`: `1d-15d`, any positive mcap up to `$5m`, `liq >=3k`, and at least `$100` of reported hourly volume. The market rank combines 5-minute burst velocity with 1-hour activity. Stage-balanced scan capacity prevents `ignition` and `early` pools from being crowded out by larger tokens. A signal still requires distributed net buying and current holder retention. ATH is entry-risk context, not a discovery gate.

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
  D1. The main dashboard payload keeps only the compact decision fields, and up
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
- The default view contains confirmed signals and previously confirmed holding
  cohorts. Candidates and overdue checks remain accessible through visible
  counters and the Workflow menu; historical records are not deleted.

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
- D1 history delivery uses the existing pending-status index and recalculates
  baselines once per batch, not once per event. Derived-write failures leave
  events pending. A stale snapshot can still deliver historical ledger events.
- Endpoint outcomes more than one hour late are excluded from horizon metrics
  and new wallet-score calculations. Prices take precedence over market cap for
  returns. Historical cached scores are not a validated trading backtest.
- Method-specific RPC plan restrictions disable only that method on that
  provider; real authentication/quota errors still block the provider route.

Run `npm test` for regression coverage of these contracts. The age window,
pool migration requirement, hourly deep scan and five-minute discovery cadence
remain unchanged. Fewer confirmed results immediately after rollout are
expected while fresh baseline and cohort evidence accumulates.

Disabled filters: `micro_sticky`, `cheap_sticky`, `breakout`, `incubation`, and
`young` are not scanned or shown in the production dashboard. Their old alert
records remain in Git history but cannot consume Reactivation monitor capacity.

The production lane scans only migrated pump.fun ecosystem pools through
`dex_allowlist` in `config.example.json`. Pre-migration `pumpfun` bonding-curve
pools are excluded before onchain analysis.

## Outputs

- `solana-radar/data/state.json` - private scanner runtime state. It is cached
  in GitHub Actions and never published.
- `solana-radar/data/alerts.jsonl` and `latest_report.*` - private local scan
  artifacts. D1 is the production source for the dashboard.
- `solana-radar/data/dashboard_fallback.json` - compact public fallback for
  Pages. It has a short raw-alert window; the full dashboard history remains in D1.
- `solana-radar/data/deleted_tokens.json` - small scanner blacklist for false
  catches deleted from the dashboard.

## Notes

The market universe is intentionally composite. GMGN Trenches supplies completed
Pump.fun launches, GMGN Trending covers `1m`, `5m`, `1h`, `6h`, and `24h`
activity, the persistent registry keeps older candidates visible, and
DexScreener/GeckoTerminal refresh pool market data. No single trending endpoint
is treated as a complete market census.

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
score, or a scheduled audit slot. Launch backfill has its own provider-aware
cursor, runs at most one page per scan, and stops once the launch is older than
the retained 24-hour signal window.

Already caught tokens get a separate hourly market refresh through DexScreener.
That pass updates dashboard `Market now` fields without re-running expensive
Helius wallet analysis for every historical catch.

When a Bright Data key is present (`BRIGHTDATA_API_KEY`, `BRIGHT_DATA_API_KEY`,
or `BRIGHT_DATA_API_TOKEN`), only triggered alerts are enriched with X search
results. This keeps costs controlled: the scanner does not call Bright Data for
every pool in the universe.
