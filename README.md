# Solana Radar

Local BBB-lite scanner for Solana meme pools.

It uses free DEX data for market discovery and Helius only for onchain work:

- find pools across lane-based filters: incubation, young, breakout, reactivation;
- keep only pump.fun ecosystem pools by default: `pumpfun-amm`, `pumpswap`, `pumpfun`;
- fetch parsed Helius transactions with pagination instead of relying on raw pool signatures;
- parse swaps;
- classify buy wallets as fresh, freshish, low-tx, normal, or dormant;
- enrich triggered alerts with public X activity through Bright Data Discover;
- write alerts and a compact Markdown report.

## Setup

Create `.env` in the repository root or inside `solana-radar/`:

```bash
HELIUS_API_KEY=...
SOLANA_TRACKER_API_KEY=...
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
python3 solana-radar/scanner.py --once --lane incubation
python3 solana-radar/scanner.py --once --lane young
python3 solana-radar/scanner.py --once --lane breakout
python3 solana-radar/scanner.py --once --lane reactivation
```

## Local dashboard

```bash
python3 solana-radar/server.py --port 8765 --auto-lane all --auto-interval-seconds 3600
```

Open `http://127.0.0.1:8765`.

The dashboard reads `data/latest_report.json`, shows recent alert history, and
auto-runs the lane scanner. The scan button is only a force-refresh.

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
and deploys the static dashboard to GitHub Pages. The workflow has two scheduled
attempts per hour and a freshness guard, so if GitHub drops one cron slot the
next slot can still run, while fresh reports are skipped. The dashboard reads:

- `data/latest_report.json`
- `data/alerts.jsonl`
- `data/state.json`

Required GitHub Actions secrets:

```bash
HELIUS_API_KEY
SOLANA_TRACKER_API_KEY
BRIGHTDATA_API_KEY
```

`BRIGHTDATA_API_KEY` can be empty if social enrichment should be disabled.

Lanes:

- `incubation`: `3h-72h`, `50k-1.5m mcap`, `liq >=3k`, HANTA-style early accumulation.
- `young`: `3d-30d`, `100k-5m mcap`, `liq >=10k`, post-launch accumulation.
- `breakout`: `3d-30d`, `5m-25m mcap`, `liq >=50k`, `1h vol >=100k`, momentum/anomaly expansion.
- `reactivation`: `30d+`, `100k-5m mcap`, `liq >=10k`, current mcap `<=40%` of Solana Tracker ATH, low-volume old-token reactivation.

By default, all lanes scan only pump.fun ecosystem pools through `dex_allowlist`
in `config.example.json`.

## Outputs

- `solana-radar/data/state.json` - last seen Helius transaction cursors, pool state, and wallet cache.
- `solana-radar/data/alerts.jsonl` - machine-readable alerts.
- `solana-radar/data/latest_report.md` - human-readable latest scan.
- `solana-radar/data/latest_report.json` - structured dashboard data.

## Notes

The free market-universe sources are not exhaustive. They are good enough for a
starter radar, but a full "all Solana tokens from 100k to 1m market cap"
universe needs a market data API such as Solana Tracker Data API.

When `SOLANA_TRACKER_API_KEY` is present, the scanner uses Solana Tracker
`/search` as the primary universe source and keeps DexScreener/GeckoTerminal as
fallback discovery.

Onchain buy extraction is Helius-first. The scanner uses
`getTransactionsForAddress` in full/jsonParsed mode, keeps pagination state per
pool, and increases page depth for high-throughput pools. If this Helius path
fails, it can fall back to the older pool-signature scan.

When a Bright Data key is present (`BRIGHTDATA_API_KEY`, `BRIGHT_DATA_API_KEY`,
or `BRIGHT_DATA_API_TOKEN`), only triggered alerts are enriched with X search
results. This keeps costs controlled: the scanner does not call Bright Data for
every pool in the universe.
