# Solana Radar Scan Dispatcher

Cloudflare Worker that triggers the five-minute discovery pulse, the hourly deep
scan, serves the live dashboard from Durable Objects, and syncs dashboard token deletions.
Canonical operational and Learning SQL lives in Turso. Verified compressed evidence
and runtime blobs live in a private R2 bucket. D1 bindings remain for rollback only.

## Deploy

```bash
cd cloudflare/scan-dispatcher
npx wrangler login
npx wrangler secret put GITHUB_TOKEN
npx wrangler secret put RADAR_INGEST_SECRET
npx wrangler secret put TURSO_AUTH_TOKEN
# Set TURSO_DATABASE_URL only after the lossless import has been verified.
# See STORAGE_MIGRATION.md in the repository root for cutover and rollback.
npx wrangler deploy
```

`GITHUB_TOKEN` must be able to run Actions for `gmirash-debug/solana-radar`.
For a fine-grained GitHub token, grant this repository read/write access to Actions.
`RADAR_INGEST_SECRET` must have the same value as the GitHub Actions repository
secret of the same name. It protects scanner ingestion and private archive endpoints.
The Turso credential is database-specific, server-side, and never sent to the dashboard.

The public dashboard reads `GET /api/dashboard`; it is CORS-restricted to the
configured Pages origin. That endpoint intentionally returns list-level facts
only. `GET /api/dashboard/token?token_key=<mint>` loads the selected token's
wallet cohort and event evidence on demand, so the dashboard remains responsive
on mobile. The scanner writes compact reports, alert history, token-scoped
market/baseline state, and every terminal scan status through protected `/api/*`
ingestion routes. Raw scanner state and runtime caches remain in authenticated
private checkpoints, not public dashboard assets.

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
hour. `scan-watchdog.yml` is an independent hourly safety net: it checks verified
scan freshness and existing runs before dispatching. It never runs another scanner
while the state-writer workflow is queued or in progress.

## Storage Safety

- During cutover, `STORAGE_WRITES_FROZEN=true` stops all cron tasks and HTTP
  mutations, including manual dispatch. Reads remain available. Use it only after
  ongoing GitHub state-writer runs finish, and remove it after verified reconciliation.
- R2 writes are acknowledged only after confirming native checksums and metadata.
- Queue envelopes keep immutable archive references; SQL delivery reads and verifies
  the complete original event before advancing the resumable cursor.
- Failed uploads, interrupted SQL and legacy pending events remain retryable.
- Learning recomputes derived scores once per UTC day using a fixed cutoff. Source
  wallet observations, cohort retention and original priors are still immediate.
- The queue's estimated write budget is not an account-wide billing guarantee.
- Legacy raw SQL is preserved during cutover. Only verified, identical archived
  delivered outbox copies qualify for cleanup; pending evidence is never TTL-deleted.
- Rolling SQL backups are private and independent of Cloudflare. R2 object archives
  must also be preserved when restoring SQL containing archive references.

## R2 Free-Tier Guard

`R2_BUDGET_GUARD=enabled` wraps every scanner R2 GET, HEAD, PUT and LIST.
One serialized SQLite Durable Object reserves charges **before** the native call.
Concurrent requests cannot overspend the counters. Failed/uncertain requests remain
charged conservatively. Unbounded streams and Infrequent Access are refused.
Missing budget state fails closed, including before initial bootstrap.

| Free Standard allowance | Warning | Stop before reaching |
| --- | --- | --- |
| 10 GB-month storage | 8 GB upper bound | 9 GB upper bound |
| 1 million Class A operations/month | 800,000 | 900,000 |
| 10 million Class B operations/month | 8,000,000 | 9,000,000 |

The **first** exhausted allowance stops **all billable** R2 access for the rest
of the UTC calendar month. Verified free DELETE garbage collection may continue;
it never deletes undelivered evidence or releases the monthly pause. On the first
at 00:00 UTC, operation counters reset, but storage does not. A database still at
9 GB remains paused. A second **rolling 33-day cap** reserves the same operation
allowances across calendar resets. This protects the actual account billing cycle
(verified in this account as October 3 - November 3), whose exact renewal time is
not exposed by the UI. A calendar reset therefore cannot spend a second allowance
before Cloudflare renews it; the safety cap may extend a pause into the next month.
This instantaneous storage ceiling conservatively bounds the
provider's daily-peak GB-month measure. Metadata reserves 4 KB per object.

The bootstrap is authenticated, one-time, and starts from account-wide analytics
operation totals plus a lag cushion and an actual bucket inventory. Repeating
bootstrap cannot reset or lower counters. Counters are reservations, **not an
invoice**. The guard covers this scanner's binding; another bucket, manual upload,
or external API client can consume the account free tier outside this guard.
Cloudflare does not provide an account-wide R2 hard spending cap.

Budget-paused scans still publish current operational data and token details to
Turso. R2 evidence stays in the unacknowledged outbox, with the previous complete
checkpoint retained. The public dashboard and token routes use SQL while paused.
These are degraded archive guarantees, not a claim that archives remain current.

Monitoring: public `GET /api/storage/r2-budget` never accesses R2. The dashboard
shows all three counters in Diagnostics, with warning/pause banners always visible.
Cloudflare checks pending alerts every five minutes and dispatches
`r2-budget-monitor.yml`; its independent GitHub schedule is a 15-minute backstop.
GitHub Actions creates a deduplicated issue, assigns and mentions the operator;
delivery is acknowledged only after GitHub confirms the issue. Email delivery
depends on the owner's GitHub notification settings. This works with a laptop off.
The optional `test_notification` input sends an explicit test without faking usage.

Bootstrap and notification acknowledgement use the existing ingestion secret:
`POST /api/storage/r2-budget/bootstrap` accepts month and conservative account
`class_a`, `class_b`, `account_storage_bytes`; the server obtains object sizes.
`POST /api/storage/r2-budget/ack` requires event ID and a repository issue URL.

Quota source: https://developers.cloudflare.com/r2/pricing/

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
