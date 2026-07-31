# Solana Radar Scan Dispatcher

Cloudflare Worker that triggers the five-minute discovery pulse, the hourly deep
scan, serves the live dashboard from D1, and syncs dashboard token deletions.

## Deploy

```bash
cd cloudflare/scan-dispatcher
npx wrangler login
npx wrangler d1 create solana-radar
# Copy the returned database id into wrangler.toml.
npx wrangler d1 migrations apply solana-radar --remote
npx wrangler secret put GITHUB_TOKEN
npx wrangler secret put RADAR_INGEST_SECRET
npx wrangler deploy
```

`GITHUB_TOKEN` must be able to run Actions for `gmirash-debug/solana-radar`.
For a fine-grained GitHub token, grant this repository read/write access to Actions.
`RADAR_INGEST_SECRET` must have the same value as the GitHub Actions repository
secret of the same name. It protects the scanner-to-D1 ingestion endpoints.

The public dashboard reads `GET /api/dashboard`; it is CORS-restricted to the
configured Pages origin. The scanner writes compact reports, alert history,
token-scoped market/baseline state, and every terminal scan status through
protected `/api/*` ingestion routes. Raw scanner state, wallet cache, and
transaction buffers do not leave the runtime cache.

## Access-protected writes

`/dispatch` and `/deleted-token` are protected by Cloudflare Access. This keeps
all write credentials out of the browser and prevents secrets from being stored
in dashboard local storage.

1. In Cloudflare Zero Trust, create a self-hosted application for
   `https://solana-radar-scan-dispatcher.gmirash-solana-radar.workers.dev/*`.
2. Allow only the intended operator email identity.
3. Add the two non-secret Worker variables from the Access application:

```bash
npx wrangler secret put CLOUDFLARE_ACCESS_AUD
npx wrangler secret put CLOUDFLARE_ACCESS_TEAM_DOMAIN
```

The first is the Access audience (`AUD`) value. The second is the team domain,
for example `your-team.cloudflareaccess.com`. The Worker verifies the JWT
signature against Cloudflare's published signing keys. `/health` returns
`delete_access_configured: true` only when both values are present.

The included `DISPATCH_BUCKETS` Durable Object claims a short-lived time bucket
before dispatching discovery or deep-scan work. Its serialized storage makes a
duplicate Cron event a no-op instead of another GitHub Actions run.

## Manual Trigger

```bash
curl -X POST "https://solana-radar-scan-dispatcher.gmirash-solana-radar.workers.dev/dispatch" \
  --cookie "CF_Authorization=<Cloudflare Access session cookie>"
```

The Worker runs discovery every five minutes and the deep scan at minute 7 of each
hour. GitHub's native schedules remain a backup and skip when the Worker snapshot
is already fresh.

## Deleted token sync

The deployed dashboard can POST deleted false catches to:

```bash
curl -X POST "https://solana-radar-scan-dispatcher.gmirash-solana-radar.workers.dev/deleted-token" \
  -H "content-type: application/json" \
  --cookie "CF_Authorization=<Cloudflare Access session cookie>" \
  -d '{"action":"delete","token_address":"<mint>","pool_address":"<pool>","symbol":"<symbol>"}'
```

The Worker commits the update into `data/deleted_tokens.json`; future GitHub
Actions scans skip those pools before the on-chain scan starts.
