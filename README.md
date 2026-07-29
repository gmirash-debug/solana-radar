# Solana Radar

Local BBB-lite scanner for Solana meme pools.

Boundary: this project is a market/onchain alert scanner, not the primary narrative-discovery workflow. It may start from DEX/Helius/GMGN because it is looking for caught tokens. For any open-ended narrative scan, start from the top-level universal source-first router before using Solana Radar outputs.

It uses free DEX data for market discovery and a routed Solana RPC layer for
onchain work:

- find pools across active lane-based filters: micro sticky, cheap sticky, breakout, and reactivation;
- keep only migrated pump.fun ecosystem pools by default: `pumpfun-amm`, `pumpswap`;
- retain a rolling registry of known pools and refresh it through free market APIs, so discovery is not limited to current trending pages;
- rotate scan capacity between high-activity pools and pools that have gone longest without an onchain check;
- use Chainstack first for recent signature probes, transaction details, and
  token supply;
- use Alchemy for paginated full address history and wallet balances, with
  Helius as the enhanced-history fallback;
- read the newest transaction tail first, then advance a separate bounded
  launch backfill only when it still fits the retained signal window;
- parse swaps;
- classify buy wallets as fresh, freshish, low-tx, normal, or dormant;
- attribute retention only to tokens bought in the detected wave, excluding balances held before the wave;
- enrich triggered alerts with public X activity through Bright Data Discover;
- reject incomplete all-lane snapshots and write scanner-health diagnostics into the dashboard report;
- write alerts and a compact Markdown report.

## Setup

Create `.env` in the repository root or inside `solana-radar/`:

```bash
HELIUS_API_KEY=...
ALCHEMY_SOLANA_RPC_URL=...
CHAINSTACK_SOLANA_RPC_URL=...
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

Run all agreed lanes:

```bash
python3 solana-radar/scanner.py --once --lane all
```

Run one lane:

```bash
python3 solana-radar/scanner.py --once --lane micro_sticky
python3 solana-radar/scanner.py --once --lane cheap_sticky
python3 solana-radar/scanner.py --once --lane breakout
python3 solana-radar/scanner.py --once --lane reactivation
```

## Local dashboard

```bash
python3 solana-radar/server.py --port 8765 --auto-lane all --auto-interval-seconds 3600
```

Open `http://127.0.0.1:8765`.

The dashboard reads `data/latest_report.json`, shows recent alert history, and
auto-runs the lane scanner. The scan button is only a force-refresh. Each scan
also refreshes current market snapshots for already caught tokens when their
dashboard market data is older than about one hour.

Narrative assignment follows [`NARRATIVE_PROTOCOL.md`](NARRATIVE_PROTOCOL.md):
one primary narrative per caught token, optional secondary flavor, source-ranked
token facts, and explicit labels for project/news overlays, ATH source, and
social status.

## Keep watching

```bash
python3 solana-radar/scanner.py --watch --lane all
```

## GitHub automation

The repository includes `.github/workflows/scan-and-pages.yml`.

It runs the scanner at most once per hour, commits updated `data/` snapshots,
and deploys the static dashboard to GitHub Pages. Hourly triggers share a
50-minute freshness guard: fresh reports are skipped before paid API work, while
stale reports trigger the full scan. Already running scans are not cancelled.
Pushes to `data/` redeploy Pages from the new snapshot without forcing another
scanner pass. Scanner failures are logged as workflow warnings and the previous
dashboard snapshot is preserved. A completed report separately exposes data
freshness and scan health (`healthy`, `degraded`, or `unhealthy`). The dashboard reads:

- `data/latest_report.json`
- `data/alerts.jsonl`
- `data/state.json`
- `data/deleted_tokens.json`

Recommended production GitHub Actions secrets:

```bash
HELIUS_API_KEY
ALCHEMY_SOLANA_RPC_URL
CHAINSTACK_SOLANA_RPC_URL
GMGN_API_KEY
BRIGHTDATA_API_KEY
```

At least one Solana RPC provider is required. For the intended production
layout, configure all three. `ALCHEMY_SOLANA_RPC_URL` and
`CHAINSTACK_SOLANA_RPC_URL` should contain the complete HTTPS endpoints copied
from their dashboards. `ALCHEMY_API_KEY` is also supported as an alternative to
the full Alchemy endpoint, but complete endpoint URLs are preferred.

`GMGN_API_KEY` is strongly recommended. It supplies migrated Pump.fun Trenches,
multi-window trending discovery, token metadata, and ATH market cap/date.
`BRIGHTDATA_API_KEY` can be empty if social enrichment should be disabled.

The production dashboard uses the versioned GitHub Pages snapshot directly.
This is intentional: the report, market history, and alert history already have
one source of truth in Git, while the Cloudflare worker writes token deletions
back to the same repository. The experimental Convex code remains in `convex/`
for future server-side features, but it is not part of the production read path
or hourly scanner job.

Optional Convex development setup:

```bash
# GitHub Actions variable, public client/backend URL
CONVEX_URL=https://your-deployment.convex.cloud

# GitHub Actions secrets
CONVEX_DEPLOY_KEY=...
CONVEX_INGEST_SECRET=...
```

These variables are only used when running or deploying Convex manually. The
GitHub Actions production workflow does not send the large scanner snapshot to
Convex.

The Cloudflare delete worker writes Delete/Restore actions to GitHub without
Convex. Its Convex variables are optional and should only be set for backend
experiments:

```bash
CONVEX_URL=https://your-deployment.convex.cloud
CONVEX_INGEST_SECRET=...
```

Lanes:

- `micro_sticky`: `3h-7d`, `$10k-$50k mcap`, `liq >=3k`, low-cap migrated pump.fun tokens with strict sticky buyer supply and net-buy retention. This is the TinyWorld-before-$50k catch lane.
- `cheap_sticky`: `12h-10d`, `$50k-$250k mcap`, `liq >=10k`, cheap migrated pump.fun tokens with stronger sticky buyer supply before repricing.
- `breakout`: `3d-30d`, `5m-25m mcap`, `liq >=50k`, `1h vol >=100k`, momentum/anomaly expansion.
- `reactivation`: `30d+`, `100k-5m mcap`, `liq >=10k`, current mcap `<=40%` of GMGN ATH, low-volume old-token reactivation.

Retired filters: `incubation` and `young` are no longer scanned or shown as active dashboard filters because they produced too much noise relative to useful catches. The new sticky lanes replace them with a narrower market prefilter plus a balance-retention check: low_tx/freshish buyers only become a dashboard signal when current buyer balances still hold meaningful supply.

By default, all lanes scan only migrated pump.fun ecosystem pools through
`dex_allowlist` in `config.example.json`. Pre-migration `pumpfun` bonding-curve
pools are excluded before onchain analysis.

## Outputs

- `solana-radar/data/state.json` - provider-aware transaction cursors, pool state, and wallet cache.
- `solana-radar/data/alerts.jsonl` - machine-readable alerts.
- `solana-radar/data/latest_report.md` - human-readable latest scan.
- `solana-radar/data/latest_report.json` - structured dashboard data.
- `solana-radar/data/deleted_tokens.json` - scanner blacklist for false catches deleted from the dashboard.

## Notes

The market universe is intentionally composite. GMGN Trenches supplies completed
Pump.fun launches, GMGN Trending covers `1m`, `5m`, `1h`, `6h`, and `24h`
activity, the persistent registry keeps older candidates visible, and
DexScreener/GeckoTerminal refresh pool market data. No single trending endpoint
is treated as a complete market census.

GMGN token info supplies ATH market cap. The scanner locates its timestamp with
a bounded `1d -> 1h -> 5m` K-line search. Reactivation filtering first reads only
the ATH value; the more expensive timestamp lookup is limited to dashboard
enrichment candidates.

Onchain buy extraction routes each method to the provider that fits it best.
Chainstack is first for recent transaction details, token supply, and health
checks. Alchemy is first for `getTransactionsForAddress` in full/jsonParsed
mode, `getSignaturesForAddress`, and token-account balances, while Helius is its
fallback. Chainstack is skipped for `getSignaturesForAddress` and
`getTokenAccountsByOwner`, which are not available on its free Developer plan.
Pagination cursors are pinned to the provider that created them. If that
provider fails mid-window, the scanner restarts the same bounded time range on
the next enhanced provider and deduplicates by signature, so it never reuses an
Alchemy cursor on Helius or vice versa. If every enhanced-history provider is
unavailable, the scanner falls back to standard signatures plus transaction
details.

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
