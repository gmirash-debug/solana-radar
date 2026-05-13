# Solana Radar Scan Dispatcher

Cloudflare Worker that triggers the GitHub Actions scanner every hour.

## Deploy

```bash
cd cloudflare/scan-dispatcher
npx wrangler login
npx wrangler secret put GITHUB_TOKEN
npx wrangler secret put DISPATCH_SECRET
npx wrangler deploy
```

`GITHUB_TOKEN` must be able to run Actions for `gmirash-debug/solana-radar`.
For a fine-grained GitHub token, grant this repository read/write access to Actions.

## Manual Trigger

```bash
curl -X POST "https://solana-radar-scan-dispatcher.<account>.workers.dev/dispatch" \
  -H "x-dispatch-secret: <DISPATCH_SECRET>"
```

The cron trigger runs at minute 7 every hour. GitHub's native schedule remains
as a backup and should skip when the Cloudflare-triggered report is fresh.
