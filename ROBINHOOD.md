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

- GeckoTerminal: three pages each for Uniswap v3 and v4, currently at most 120
  discovered pools, at most 16 new-token/cohort checks per run.
- New discovery: contract age 24-360 hours, liquidity >= $3,000,
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
- Direct and routed buys are attributed to the transaction sender only if
  the full pool/custody token output equals that sender's positive net receipt.
  Ambiguous routes, transfer-only activity, transfer-tax discrepancies,
  smart-account beneficiaries and same-pool multi-swaps are excluded.
- Up to 48 buy receipts per pool/window. Partial attribution is shown as
  partial; it cannot create a new confirmed buy wave. Sell event counts are
  not proof that the original buyers sold.
- Initial coverage begins one hour before the first observation, NOT launch.
  Indexing continuity is distinct from complete receipt attribution. Old
  indexed logs are compacted after the current window into hourly summaries;
  this is not a permanent raw chain archive.

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

## Remaining limits

No free setup can guarantee unlimited archival/indexed queries and uptime.
There is no automatic deep-reorg reconstruction, full chain backfill, complete
router/account-abstraction coverage, wallet identity clustering, realized PnL,
Solana Learning parity or lifetime raw-log retention. Signals are evidence
for review, not a promise of entry quality. Incomplete data remains explicit.
