# Signal integrity release, 2026-09-04

## Scope

Keep the production universe unchanged: migrated pump.fun pools aged 1-15 days,
positive market cap up to $5m and liquidity at least $3k. Keep five-minute market
discovery and hourly deep scans. Do not delete existing catches or raise paid
provider budgets.

## Fixed failure modes

1. Single classified-wallet buys and incomplete waves remain candidates, not
   confirmed entries. Missing balance/owner data is unknown, not 100% coverage.
2. Missing observation intervals no longer count as quiet trading. Confirmation
   expires from the original activity break, not from each subsequent snapshot.
3. Hot signals require the same baseline and retention seasoning safeguards.
   A later original-cohort check can confirm retention, but cannot substitute
   for missing history, baseline or relationship evidence.
4. A new wave cannot lend its confirmation to another cohort. Existing confirmed
   cohorts stay fixed until invalidation; unconfirmed cohorts may be replaced by
   a genuinely confirmed new wave.
5. An independent, bounded balance-only pass checks oldest due cohorts before
   deep pool selection, without fetching their full transaction history again.
6. RPC method-level 403 restrictions no longer disable unrelated methods on a
   usable provider. Authentication and quota failures remain blocking.
7. Snapshot failures enter a compressed retry outbox retained by Actions cache.
   A successful scan and a successful cloud save are displayed separately.
8. D1 pending outbox reads use the existing compound index. History baselines and
   wallet scores are computed once per batch, with acknowledgement after success.
   Old snapshot retries can still preserve historical events.
9. Outcome endpoints over one hour late are excluded from horizon statistics and
   future score computations. Price-based returns take priority over market cap.
10. The dashboard separates candidates, overdue checks and confirmed holdings.
    Old unversioned signals do not inherit the new confirmation label.

## Validation

- `npm test`: 144 Python and 32 JavaScript tests pass, plus dashboard syntax check.
- Regression fixtures cover the audit reproductions, cohort confirmation and
  replacement, bounded balance monitoring, outbox replay and derived-write failure.
- Worker production bundle passes `wrangler deploy --dry-run`.
- Browser checks use the saved 2026-09-04 production snapshot and a simulated
  unavailable cloud endpoint. Candidate records remain accessible; old records
  are not promoted to confirmed merely because they held tokens.
- Live run 33916888647 completed all 40 selected pools with no failed pools or
  transaction parse errors. All four providers remained available. Alchemy used
  14,900 of its 25,000 per-scan credits; the balance-only pass checked 11 cohorts
  with 200 balance attempts. Its one failed cloud write was saved to Actions cache
  and the fresh 20:45 UTC snapshot was published through Pages.
- UI-only pushes have a separate queue from discovery. Pages builds use current
  main assets and preserve the newer published/local snapshot instead of restoring
  the stale Git-tracked fallback. If the public fallback cannot be read, an old
  Git snapshot cannot be republished silently.

## Operational caveats

- D1 returned error 7500: the account had exceeded its free daily row-read quota.
  Code changes cannot replenish today's quota. It resets at 00:00 UTC. No billing
  upgrade is performed by this release.
- Actions cache can expire or be evicted. It is short-term outage recovery, not a
  permanent offsite archive. A prolonged pending queue needs operator attention.
- At rollout, old quiet periods and signal confirmations are deliberately not
  trusted. New baseline evidence and cohort checks must accumulate.
- The 40-pool deep-scan cap is unchanged. Discovery is not exhaustive, and hourly
  deep scans can still miss short-lived moves. The new monitor cap is a limit,
  not a guarantee that every overdue wallet is checked every hour.
- Cached historical wallet scores are not retroactively rebuilt by this release.
  Candidate/confirmed profitability must be evaluated prospectively with a
  versioned sample, trading costs and liquidity constraints before claiming edge.
