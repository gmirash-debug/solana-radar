# Full audit remediation, 2026-10-03

Scope: the 49 confirmed findings in the full scanner audit. This is an
implementation and release record, not a claim of profitable signals or
complete on-chain coverage. The original read-only audit is preserved at
`/Users/mirash/Documents/New project/reports/scanner-full-audit-20261003/report.md`.

Release status: local integration verification passed; production rollout is
pending. Production deployment must follow the migration order below.

## Before / After

| Before | After |
| --- | --- |
| Failed runs could lose locally saved scan progress. | Versioned, non-regressing runtime checkpoints can be cached even after a failed scan. |
| Partial or malformed evidence could look confirmed. | Invalid holder replies are rejected; confirmation requires a complete, post-deadline cohort check. |
| New purchases or fresh balances could hide known original sales. | Original buy cohorts remain frozen; known post-signal sales reduce their balance cap. Legacy missing sale history stays unknown. |
| Learning could use fabricated entry prices or future quotes. | Timestamp-matched entry/endpoint evidence is required. Old unverifiable outcomes are retained but excluded from trusted statistics. |
| Wallet details could belong to another report generation. | Cohort, signal window, check time and report generation must match. Otherwise only a pending summary is returned. |
| History jobs restarted large work instead of advancing. | Durable bounded phase cursors preserve progress; retention observations do not rebuild unchanged wallet graphs. |
| UI could display stale market values as current or open the wrong token. | Quote freshness uses the quote clock; refreshes cannot roll back a snapshot; token navigation preserves identity. |
| Local web pages could start scans or change deletions without authorization. | Loopback default, Host/Origin validation, JSON requests and a per-process mutation capability are required. LAN serving also requires authentication. |

## Finding Coverage

| IDs | Change | Regression evidence |
| --- | --- | --- |
| S01 | One shared ATH token allowance across targeted passes and ATH providers; no unrelated broad history sweep. | `tests/test_audit_scanner_regressions.py` |
| S02 | Discovery updates only discovery-owned market fields, preserving catch and validated ATH evidence. | Same scanner regression suite |
| S03 | Deduplicated post-signal resolved sales reduce original attributed inventory; legacy cohorts and missing history cannot be silently replaced or reconfirmed. | Same suite; `tests/test_coordination_pipeline.py` |
| S04 | All included balance rows must be checked after the hold deadline. Incompatible old check cycles restart safely. | Same scanner regression suite |
| S05 | Replacing a market pool clears the previous pool's creation timestamp. | Same suite |
| S06 | Native outflow plus a token gift is not a buy without a supported swap witness. | Same suite; `tests/test_scanner.py` |
| S07 | Wave sellers use the resolved swap owner, not an unrelated transaction signer. | Same scanner regression suite |
| S08 | Rechecks wait only the remaining hold duration. | Same suite |
| O01 | Failure-time runtime cache validation checks schema, JSON shape, revision and timestamp. | `tests/test_runtime_cache.py` |
| O02 | Retention events and wallet estimates cannot attach quotes observed after the balance check. | Scanner regression suite |
| O03 | History gap ratios use the correct eligible scan population and cannot exceed 100%. | Same suite |
| O04 | Typed failure taxonomy separates programming/invariant faults from provider limits; transient timeouts do not create a six-hour quota backoff. | Scanner regression suite; runtime cache/workflow tests |
| O05 | Both routed and standalone largest-holder calls reject invalid amounts and duplicate addresses before caching. | `tests/test_rpc_routing.py` |
| I01 | Accepted history jobs advance in bounded, restart-safe phases; observations are bundled instead of repeatedly rebuilding graphs. | Worker `runtime-history.test.mjs` |
| I02 | Failed or deferred flushes publish a fresh health timestamp, budget state and actual error. | Worker runtime/history tests |
| I03 | Replaced runtime blobs receive a supersession clock; GC preserves a bounded reader grace period. | Worker `runtime.test.mjs` |
| I04 | Detail generation and cohort identity are checked on the server and in the browser. | Worker `index.test.mjs`; dashboard regression suite |
| I05 | Unknown numeric values remain null. Real zero stays zero. Legacy ambiguous derived statistics are not trusted. | Worker runtime/history tests |
| I06 | Wallet pagination cursors include every ordering key and reject obsolete cursors. | Same tests |
| I07 | Connected wallet components are rebuilt incrementally; overlapping old clusters are retired, not deleted. Public infrastructure does not create ownership edges. | Same tests |
| I08 | Prior-score lookup chunks 99 addresses plus its timestamp parameter within D1's 100-bind limit. | Same tests |
| I09 | Restoring a mint also clears its associated old pool unless a separate deletion still owns that pool exclusion. | Worker index tests; local server tests |
| I10 | Published details use content-derived filenames; a mismatched previous detail can be skipped without failing the whole partial publication. | `tests/test_audit_publication_regressions.py`; pages publication tests |
| U01-U03 | Quote-clock freshness, monotonic snapshot/request guards and timeouts covering the response body. | `dashboard/test/frontend-remediation.test.mjs` |
| U04-U05 | Unknown ATH is not Low range; historical native-token FX is not presented as current wallet PnL. | Same suite; token-state tests |
| U06 | Learning and Narrative links open the selected in-scope mint, including Closed positions. Out-of-scope records show an explicit notice. | Frontend regression suite |
| U07 | Robinhood freshness expires while idle and is refreshed on return to the page. | Same suite |
| U08-U10 | Optional fallback failures do not block valid legacy data; fallback deletions and pool-only deletions remain effective. | Same suite |
| U11-U13 | Narratives use the unified detail route, unknown market state and keyboard-accessible controls. | Same suite |
| U14 | Failed Restore is stored as a pending Restore, never retried as Delete. | Same suite |
| U15-U16 | Unavailable Learning or Robinhood data is visibly unavailable, not a successful empty result. | Same suite |
| A01 | Local scanner lock releases after timeout/start failure; output bytes are decoded; the server's Python environment is reused. | `tests/test_server.py` |
| A02 | Routed Robinhood buyers need attributable swap payment evidence; unrelated funding and ambiguous custody are rejected. | `tests/test_robinhood.py` |
| A03-A05 | Explicit zero is preserved; current pool values cannot invent an old entry; future endpoints cannot freeze early horizons. Legacy outcomes are quarantined from trusted metrics. | Scanner regression suite |
| A06 | Optional social-provider poll failures do not abort a valid on-chain scan. | Same suite |
| A07 | GMGN profile identity must match the requested mint before use or caching. | Same suite |
| A08-A09 | Official X bios are matched to the exact account; numeric/alias duplicates do not multiply social heat. | Same suite |
| A10 | Local mutations require the same-origin session capability and LAN authentication. Published Worker authorization is unchanged. | Server tests; frontend regression suite |

## Verification

- Full Python suite: 636 tests passed. Combined Worker and dashboard suite:
  257 tests passed. Three synthetic replay cases passed. JavaScript syntax,
  workflow YAML and whitespace checks passed.
- A two-day queue simulation processed 4,952 events, including an initial
  backlog, 100 forty-wallet cohort checks per hour, outcomes, new signals,
  hourly process restarts and a full receipt store. Both days ended with zero
  pending events. Peak pending count was 198. Daily conservative history write
  units were 78,048 and 75,744; DO units were 40,889 and 47,216. Existing daily
  allowances remain 80,000 and 50,000. This workload test is not a guarantee for
  arbitrary graph sizes or a growing cohort population.
- Delivered-event receipts retain 30 days in a bounded 160,000-row metadata
  store. Pending payload limits are unchanged. Only expired delivered receipts
  are eligible for removal; pending evidence is never evicted.
- Additional boundary checks cover recovery from a corrupt old checkpoint,
  missing predecessor files, Unicode RPC amount strings and transient provider
  failures incorrectly triggering long quota backoff. Legacy ordinary Robinhood
  rows are also downgraded during UI-only publication, without new RPC calls.
- Replay cases are synthetic detector contracts, not measured strategy recall,
  precision or achievable trading returns.
- Browser checks use a captured public snapshot without launching scans or
  deleting tokens: desktop 1440x900, mobile 390x844, both networks, token
  navigation, terminology and explicit provider-unavailable states.
- GitHub PR validation runs without production secrets or live provider calls.

## Safe Rollout

1. Export both D1 databases before schema changes. Backups for this release are
   `/tmp/solana-radar-pre-remediation-20261003.sql` and
   `/tmp/solana-radar-history-pre-remediation-20261003.sql`.
2. Apply the additive history migration `0003_resumable_history.sql` to
   `solana-radar-history`. Do not edit migrations already applied in production.
3. Deploy the Worker only after the migration and full tests succeed.
4. Merge the validated scanner/dashboard release and verify the actual Pages
   assets, Worker endpoints and a fresh scan using the new code.
5. Inspect history backlog, oldest pending age, daily allowance and latest flush
   error. A completed deploy does not mean an existing backlog has drained.

The migration is additive. Old raw records, original cohorts and retired
cluster identities remain available. Do not reverse columns on a live database
as a rollback shortcut. Roll back code only to a compatible version; recovery
of an entire database needs an explicit write-safe restoration plan.

## Known Evidence Limits

- Old null-to-zero conversions and fabricated entry prices cannot be repaired
  reliably without their original evidence. They remain stored and untrusted.
- Checked balances are an upper bound for original holdings, not proof that no
  sales, transfers or repurchases occurred outside the observed history.
- Common infrastructure, shared funding and correlated execution do not prove
  common ownership, an insider group or an exact transaction bundle.
- RPC history gaps and provider quotas still constrain coverage. Missing
  evidence is not a negative finding and is never a buy recommendation.
- Old published snapshots and archived observations are not retroactively
  rewritten merely because a new calculation is correct.
