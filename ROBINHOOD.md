# Robinhood Radar

Read-only Robinhood mainnet pipeline, chain ID 4663. Solana provider keys,
filters, D1 state, deletions and signals are independent. No wallets are
connected and no transactions are sent.

## Provider routing (verified 2026-09-07)

- Alchemy Free, configured as GitHub secret `ROBINHOOD_RPC_URL`: historical
  bytecode/state for token age; fallback for point reads and receipts.
  Real archive eth_getCode and eth_call succeeded. Free eth_getLogs is limited
  to 10 blocks: NEVER fan out an hour into thousands of paid-unit calls.
- PublicNode: head, blocks, receipts and recent contract reads. Historical
  logs/state returned access errors without an archive token, so large log
  reads do not route there.
- Robinhood public RPC: large filtered log ranges and initialization events.
  Throttles with both HTTP 429 and JSON-RPC error code 429 under HTTP 200.
- Ordo public RPC: independent log fallback, verified chain ID, receipts/logs
  and recent state. Historical state is not available.
- Optional `ROBINHOOD_ARCHIVE_RPC_URL` accepts a separate archive provider.
  Never substitute a Solana endpoint. Every provider is chain-checked before
  its data is used. Wrong-chain endpoints are disabled for the run.
- Backoff and per-provider pacing apply. Attempts, including retries, consume
  a shared hard budget: 900 calls and 510 seconds. Pools wait when the
  remaining budget is too small. This is a bounded free-tier setup, not an SLA
  or unlimited network history. Alchemy quota is shared with the Solana app.
- Retry-After and JSON-RPC network-busy responses back off within the same
  deadline. Failure diagnostics include provider and numeric codes, not keys.

Sources:
- https://docs.robinhood.com/chain/connecting/
- https://robinhood.publicnode.com/
- https://chainid.network/chains.json
- https://developers.uniswap.org/deployments.json
- https://raw.githubusercontent.com/Uniswap/v4-core/main/src/interfaces/IPoolManager.sol
- https://docs.robinhood.com/chain/stock-token-apis/
- https://api.gopluslabs.io/api/v1/supported_chains

Arrow RPC did not return usable JSON during testing. PublicNode archive access
and Alchemy's unrestricted log range are NOT claimed as free capabilities.

## Universe and identity

- GeckoTerminal: two network new-pool pages plus three pages each for Uniswap
  v3 and v4. Deduplicate pools, exclude unsupported DEXs. This is bounded
  discovery, not a complete launch feed; at most 16 new-token/cohort checks per run.
- New discovery: contract age 0-360 hours (from launch through 15 days), liquidity >= $3,000,
  FDV > 0 and <= $5 million. Pool age is only an inexpensive prefilter.
- Archive bytecode at age boundaries and a binary deployment-block search
  establish contract age. Unsupported archive reads yield unknown, never
  a fabricated launch date. Verified deployment metadata is cached.
- Exclude canonical stock/ETF addresses from the official live
  https://api.robinhood.com/rhj/assets registry, plus native ETH/WETH/USDG.
  A ticker match alone does NOT exclude a token. Registry failure pauses
  new discovery instead of treating tokenized stocks as memes.
- V3 must match the deployed factory AND its getPool registry.
- V4 must have a matching Initialize event from the official PoolManager
  `0x8366a39cc670b4001a1121b8f6a443a643e40951`. Recompute the PoolKey hash;
  verify currencies and inspect the hook address. Bytes32 pool IDs never
  become EVM wallet addresses.
  Initialization lookup is bounded to a 20-minute interval around the reported
  pool creation time, not the entire chain. Incorrect discovery timestamps fail
  verification rather than creating an unverified pool identity.
- One selected pool per token. Other DEXs, v2 and complete network launch
  discovery are outside this coverage. Existing frozen cohorts remain tracked
  when a pool drops from discovery; old market quotes are marked stale.

## History and attribution

- Per-pool block cursor resumes after the last indexed block; log chunks are
  validated and deduplicated. Capped responses are split; even single-block
  truncation fails closed. Up to 720,000 blocks of catch-up per pool/run.
- A stored checkpoint hash is checked before continuing. Reorganized history
  fails closed and requires an explicit rebuild rather than silently changing
  the original thesis. The scan head is checked again before committing.
- Successful transaction receipts must match the observed swap hash/block.
  V4 deltas are normalized to the V3 sign convention.
- Direct and ordinary routed buys are attributed to the transaction sender only if
  the full pool/custody token output equals that sender's positive net receipt.
  Ambiguous routes, transfer-only activity, transfer-tax discrepancies,
  smart-account beneficiaries and same-pool multi-swaps are excluded, except
  independently reconciled Relay recipients described below.
- Up to 160 buy receipts per pool/window, bounded by the shared RPC/time budget.
  Partial attribution is shown as partial; ordinary distributed waves require
  complete attribution. A verified Relay subset can meet its own conservative
  thresholds without claiming all buyers were attributed. Sell event counts
  are not proof that the original buyers sold.
- Initial coverage begins one hour before the first observation, NOT launch.
  Indexing continuity is distinct from complete receipt attribution. Old
  indexed logs are compacted after the current window into hourly summaries;
  this is not a permanent raw chain archive.

## Relay waves (2026-09-11)

Watch as soon as an eligible trading pool is discoverable and contract age can
be verified. There is no 24-hour waiting period. The scheduler is still hourly:
5/15-minute analysis windows do NOT mean scans run every five minutes.
During the first 24 hours, only the stronger Relay path can create a cohort;
ordinary three-buyer waves remain observations. This prevents lifting the age
gate from turning routine launch buys into confirmed accumulation candidates.

The known Relay solver is only a lookup hint. Each accepted buy needs a
successful Relay order for this destination chain, token and transaction;
successful source/destination legs; positive paid quote and bought base in a
verified pool swap; and the executed output exactly matching the recipient's
receipt net token gain. Pool custody loss must reconcile, and unrelated net
token sources, refunds, transfer-only receipts and ambiguous multi-swaps are
rejected. Origin-chain funding is provider-attributed, not an independently
audited origin-chain history. Final recipients, not relayers, count as buyers.

Initial, unvalidated detection thresholds (not fit for a profitability claim):

| Window | Material recipients | Gross supply purchased |
| --- | --- | --- |
| 5 minutes | 5 | 1% |
| 15 minutes | 8 | 2% |
| 60 minutes | 15 | 5% |

Each counted recipient must buy >=0.01% supply; no recipient may account for
more than 40% of that window's material gross purchases. Duplicate transaction
hashes count once. Windows use canonical destination block timestamps, not
interpolated price times. The first qualifying observed window is selected.
Gross buys can include turnover; they are never labelled retained supply.
Comparison with a normal baseline remains explicitly `not_established`.

A Relay cohort is frozen only after complete log indexing plus same-block
balance/outflow checks retain >=60% of its attributed purchases and >=0.5% of
total supply conservatively. The initial check is a new wave, NOT holding
confirmation. Rechecks use the existing frozen-cohort rules below; later
buyers never replace the original recipients. Contract risks remain visible
and override the status. A gross wave without sufficient holding remains an
observation. Partial receipt coverage may establish a verified subset, never
a claim of exhaustive market coverage or coordinated ownership.

Relay lookups are cached seven days and capped at 96 requests and 90 seconds
of request time per scan, also respecting the scan deadline. Errors/rate limits
stop new Relay lookups without failing the whole scan. No new paid plan.
`RELAY_API_KEY` in GitHub Actions secrets selects v3 (`fillTxHash`, actual route
output only). Without it the currently working public v2 is used. v2 retires
on 2026-11-24 and has progressively reduced limits; the snapshot/UI expose this
migration warning. v3 schema handling is unit-tested; authenticated live v3
has not been verified without a key. No automatic fallback bypasses a v3
authentication error.

Sources: https://docs.relay.link/references/api/get-requests and
https://docs.relay.link/references/api/api_guides/migrating-to-requests-v3 .

## Cohorts and interpretation

A new buy wave requires a complete current log window, full buy attribution,
at least three buyers, at least 0.25% conservatively retained supply, and
buy events >= twice sell events (minimum denominator one). These are initial
evidence thresholds, not a profitability-validated model.

Freeze the original cohort. New unrelated buyers cannot replace it on recheck:
- Lower bound = attributed purchases minus ALL outgoing token transfers.
- Upper bound = min(current wallet balance, original attributed purchases).
- Later incoming transfers cannot replenish the lower bound.
- Every balance and totalSupply is read at the same block.
- At least 30 minutes and a later block are needed for another confirmation.
- Holding confirmed: second complete cohort check, >=60% of the initial lower
  bound remains. This proves bounded holding evidence, NOT insider identity.
- Cohort reduced: lower bound fell below 60%; outflows include sales/transfers.
- Supply changes or GoPlus flags produce Contract risk.

GoPlus checks ownership/mint/pause/blacklist/proxy/honeypot/tax fields on chain
4663. Empty tax values are unknown, not zero. Nonzero v4 hooks are flagged.
No-flags is NOT a guarantee of sellability. Nonstandard token accounting,
undiscovered administrative controls, rebases and malicious contracts can
invalidate ordinary ERC20 assumptions; there is no production trade simulation.

## State and execution

`python robinhood.py --db data/robinhood.sqlite --output data/robinhood.json`

## Accumulation evidence (2026-09-11)

Three evidence layers augment an existing cohort; preparation coincidences do
not independently promote a token or assert insider ownership. Solana is unchanged.

- Cross-chain inflow: receipt-reconciled Relay source/destination chains, distinct
  recipients, gross bought supply, and frozen at-catch context. Same-chain Relay
  swaps are excluded from the cross-chain count. Other services remain unverified.
  Two extra unresolved-route lookups per pool remove the single-solver lookup
  dependency without making unlimited API requests. Generic single-beneficiary
  EOA routes can be attributed through receipt reconciliation, but their source
  network stays unknown. Contract beneficiaries, mixed sources, fee-adjusted
  unknown routes, and ambiguous swaps fail closed.
- Preparation: up to 12 selected buyers and 24 buy receipts. Search the available
  Robinhood quote-token transfer window before their purchases. Three recipients
  funded by one unclassified EOA within 15 minutes, with buys within 15 minutes
  and funding less than one hour before buys, are a timing/funding coincidence.
  Known local infrastructure and contract senders are excluded. This is NOT proof
  of one owner; unlabelled exchange payouts can still match. Native transfers and
  historical funding on source chains are NOT covered. Original buy events are
  frozen so a delayed check does not silently inspect unrelated later purchases.
- Position movement: durable, hash-anchored checkpoints in the existing SQLite
  cache. Starts at the first trace checkpoint, not retroactively at launch. Up to
  12 original buyers, eight additional recipient addresses and two transfer hops.
  Reconciles incoming/outgoing transfers and all tracked end balances. Tracks only
  the guaranteed portion of mixed balances; uncertain provenance goes to unknown.
  Reports original holders / transfer recipients / confirmed sold / unresolved as
  percentages of total supply. The four buckets conserve original bought tokens.
  Only reconciled direct sales into the selected pool are confirmed sales; other
  pools, routers, contracts and untraced destinations remain unresolved. Transfers
  to another address do not establish unchanged beneficial ownership. A return
  transfer or repeated event is not a new purchase. Later topups cannot resurrect
  sold provenance. Supply changes invalidate the trace.

Optional evidence is capped at 64 logical RPC operations per helper and 120 per
scan, within the existing RPC/time budget (provider retries still count against
the main budget). It leaves at least 90 main-budget calls and 60 seconds available.
Partial failures keep the previous atomic trace checkpoint. UI never presents a
pending or stale trace as a current result. No new database or paid plan is needed.

SQLite stores metadata, frozen cohorts, cursors, current-window logs, cached
receipts and historical window summaries. Commits occur after head validation.
Receipt cache expires after seven days. No new D1 quota is used.

Hourly GitHub dispatch runs this independently of Solana freshness; an explicit
`source=robinhood` dispatch skips Solana. UI-only pushes do not run scans.

Actions cache restores the working SQLite file. A daily compressed database
artifact retained for 14 days recovers cache eviction. Only same-repository
main-branch artifacts are accepted, with size and SQLite integrity checks.
A backup restore failure pauses Robinhood, not Solana publication. Backup
upload failure is visible in Actions but does not discard the successful
snapshot. This is bounded backup retention, NOT an indefinite archive; a
long shutdown beyond both cache/artifact retention can lose private state.
No database or credentials are published to Pages.

The public snapshot is independently versioned and published to
https://gmirash-debug.github.io/solana-radar/robinhood.html .
The legacy URL redirects to `index.html?network=robinhood`. Both networks use
the same HTML shell, workspace styles, list/detail layout and mobile navigation.
Only the selected network's controller loads. Robinhood's Learning/Narratives
remain disabled; Events shows the latest exported checks, not an invented
lifetime event history. Wallet retention is a range, never substituted for PnL.
API keys stay in Actions secrets; never include them in the dashboard or logs.
No new paid plan or automatic paid overage was enabled.

## GMGN context (2026-09-10)

The Robinhood job receives `GMGN_API_KEY` from the existing Actions secret.
Read-only CLI version 1.6.1 is pinned in both workflows. No private key is
required. `gmgn_context.py` bounds enrichment to 24 calls and 45 seconds:
token info for at most 16 observations, then security and a top-30 holder
sample for at most four original cohorts or distributed-buy candidates.
Info/holder caches last 55 minutes; security caches last six hours. Authentication
or rate-limit failures stop further calls in that run. Enrichment failure does
not fail the on-chain scan; dated cached context is marked stale.

GMGN price, reported price ATH, activity, holder counts and labels appear in
Overview. Provider holder samples appear separately in Wallets; provider risk
flags in Supply. PoolManager, known pool/token addresses, exchange-labelled
addresses and non-wallet/unknown address types are excluded from the sample.
Transfers are not buys. These samples never overwrite RPC balances, the frozen
signal cohort, retained supply or signal status. Tags are provider hypotheses.
GMGN is supplemental context, not a new discovery source or trading executor.

ATH price is not ATH market cap. A creator's `ath_token_info.ath_mc` is usable
only if its `ath_token` matches the requested token. Price times current supply
is not exported as a historical market-cap ATH. Missing peak dates stay unknown.

## Coverage limits

No free setup can guarantee unlimited archival/indexed queries and uptime.
There is no automatic deep-reorg reconstruction, full chain backfill, complete
router/account-abstraction coverage, wallet identity clustering, realized PnL,
Solana Learning parity or lifetime raw-log retention. Signals are evidence
for review, not a promise of entry quality. Incomplete data remains explicit.
