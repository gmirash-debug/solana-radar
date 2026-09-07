# Robinhood Chain observations

This is an isolated read-only research adapter, NOT parity with the Solana
accumulation detector. Solana signals, deletions, D1 keys, wallet classification
and provider budgets are unchanged. No wallets are connected; no trades are sent.

## Verified deployment and sources

- Mainnet chain ID: 4663 (never testnet 46630).
- RPC: https://rpc.mainnet.chain.robinhood.com (rate-limited public endpoint).
- Optional GitHub Actions secret `ROBINHOOD_RPC_URL`: a dedicated mainnet endpoint.
  For Alchemy, create/enable Robinhood mainnet and set the URL through GitHub
  Settings > Secrets and variables > Actions. Do not reuse the Solana RPC URL.
- Network documentation: https://docs.robinhood.com/chain/connecting/
- Official Uniswap deployment feed: https://developers.uniswap.org/deployments.json
- Verified Uniswap v3 factory: `0x1f7d7550b1b028f7571e69a784071f0205fd2efa`.
- Pool discovery/market observations: GeckoTerminal, network `robinhood`, DEX
  `uniswap-v3-robinhood`. Prices/FDV are provider observations, not verified ATH.

Each pool must return the expected factory, matching token0/token1, and must be
registered by `factory.getPool`. An address prefix or a DEX label is insufficient.

## Scope and budgets

- Three v3 discovery pages, at most six unique base tokens checked per attempt.
- Pool age 24-360 hours, liquidity >= $3,000, FDV > 0 and <= $5 million.
  **Pool age is not token age.** Old tokens with new pools can appear here and
  are never represented as verified 1-15-day-old tokens. Tokenized stocks are
  not yet excluded by a verified asset registry.
- Oldest checked eligible tokens receive priority, then hourly volume.
- Each attempt examines a rolling hour ending 64 blocks behind head. It is not
  a continuous ledger; rotated pools and delayed runs can leave coverage gaps.
- At most 300 RPC attempts, including retry requests, and 24 receipts per pool.
  Public requests are paced, with bounded backoff on HTTP 429/502/503/504.
- No implicit fallback to Solana keys, paid endpoints, or a different network.
- Uniswap v2/v4, other DEXs, cross-pool aggregation, internal router beneficiaries,
  smart-account attribution and network-wide launch discovery are NOT covered.

## Evidence semantics

`observed` means a completed pool window, not suspicious accumulation.
`buy_wave` requires >=3 directly attributed buyers, all buy swaps attributed,
and buy swaps >=2x sell swaps (at least two buys). This is research evidence,
not a confirmed signal, entry recommendation or proof of coordinated wallets.

A buy requires a successful v3 swap whose direct recipient is the transaction
sender, plus an equal positive ERC20 net transfer to that sender. Transfer-only
events, ambiguous multi-swap transactions, routed final beneficiaries and
transfer-tax mismatches are excluded. Sell counters are pool swap events,
not proof that the tracked cohort sold.

Balances and totalSupply are read at one block. `min(balance, observed_buys)` is
reported as a **retained-supply upper bound**: pre-existing tokens, inbound
transfers, minting or rebases can inflate it. No precise lot-retention or PnL
claim is made. Whole-token supply verification, taxes, honeypots, admin controls
and wallet relationships are not implemented. All rows remain research-only.

Wrong chain, stale head, factory mismatch, capped logs, receipt/window mismatch,
RPC errors and reorgs fail closed. Old snapshots preserve their old timestamps;
the UI does not call them current. Publication chooses the newer chain-specific
attempt, independently of Solana's snapshot, so an unrelated UI deploy cannot
roll Robinhood observations backward.

## Execution and publication

`python robinhood.py` writes `data/robinhood.json` atomically. The file is excluded
from Git. GitHub Actions keeps it in a separate cache and publishes a read-only
copy to Pages. This is bounded observation state, not a durable research database.
Cache eviction loses rotation/first-observed history; continuous historical
analysis requires a separate chain-scoped durable store before production parity.

The existing hourly Cloudflare dispatch also invokes this adapter, even when
Solana is fresh. An isolated manual run uses workflow input `source=robinhood`.
UI pushes publish only and never trigger paid on-chain calls. Robinhood failures
do not block Solana publication. Job/step timeouts remain hard upper bounds.

Dashboard: `robinhood.html`, accessible with the Network selector. There is no
Robinhood delete mutation or Solana wallet-detail API call. All token identities
are `4663:<lowercase address>`. Display order is newest first observation first.

## Before enabling confirmed signals

1. Provision and load-test a dedicated production/archive RPC endpoint.
2. Establish token creation time and exclude tokenized stocks/wrapped assets
   using verified contracts, not symbol heuristics.
3. Add v4 pool IDs, hook risks and router beneficiary decoding with fixtures.
4. Persist incremental pool cursors and backfill gaps with reorg-safe deduplication.
5. Freeze buy cohorts, recheck them later and distinguish sales from transfers.
6. Add contract restrictions/tax testing and measured signal-quality evaluation.
7. Only then enable signal confirmation and integrate Learning/retention history.
