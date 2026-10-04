# Solana Radar Scan Dispatcher

Cloudflare Worker that triggers the five-minute discovery pulse, the hourly deep
scan, serves the live dashboard from Turso, and syncs dashboard token deletions.
Operational data, checkpoints, queue leases and Learning SQL live in Turso.
Verified compressed evidence archives live in private R2. D1 and old DO bindings
remain for lossless migration/rollback, not new runtime mirrors.

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

Apply `migrations-storage/0002_runtime_sql.sql` through `0005_cutover_staging.sql`
to the canonical Turso database before deploying the SQL runtime/queue flags.
Do not run them against the legacy Durable Object or automatically on every read.
`HISTORY_LEGACY_MIGRATION=verified_turso_v1` is specific to the verified canonical
history/resume-state import. Do not enable it on an empty replacement database.

During that verified cutover, new raw events first enter a bounded separate Turso
staging queue (2048 rows / 16 MiB). The old DO is export-only; consumers wait until
all old receipts and progress have been acknowledged in SQL. Automatic flushes
advance at most 25 old rows per invocation. DO quota failures keep migration
pending without deleting data or blocking current SQL dashboard/checkpoints.
After the sweep, staged events move into the primary queue before their staged
copies are removed. Overflow is explicit backpressure, never a successful drop.

Live queue consumption caps at 35 counted requests, leaving headroom under the
free Worker's 50 external-request limit. Learning and archive cleanup run at
minute 47, separately from discovery (every 5 minutes), targeted checks (22/37/52)
and hourly deep checks (7). Archive backfill and legacy SQL forwarding use the
minute-37 maintenance invocation, not the discovery critical path.

SQL checkpoint manifests publish only after every immutable part is present and
hash/identity-valid. Superseded parts have a 48-hour restore grace. No R2 request
is needed for new checkpoints. Unarchived delivered raw events remain in SQL;
their last copy cannot expire just because analytics has completed.

Native RPC usage is reserved in small durable SQL grants before requests:
Helius 50 credits, Alchemy 1000 CU, Chainstack 100 RU. Unused/uncertain grants stay
charged; these are conservative scanner reservations, not provider invoices.
Robinhood reserves its bounded Alchemy grant in the same ledger. The private
`/api/runtime/rpc-ledger` rejects counter regression and stale acknowledgements.
Missing ledger access disables paid routes, not public fallbacks. Keep the global
GitHub state-writer lock: do not replay grants or run independent shared-state
writers concurrently. Known Solana accounts batch in groups of at most 100;
declines/closures and six-hour expiry immediately require owner re-enumeration.

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

Budget-paused scans still publish current operational data, checkpoints and token
details to Turso. R2 evidence stays durable in SQL with an archive-pending marker.
The public dashboard and token routes use SQL while paused.
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
