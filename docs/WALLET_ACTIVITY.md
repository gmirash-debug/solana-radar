# Wallet Activity

The wallet audit is independent of pool discovery and partial pool history. It
queries finalized, full JSON-parsed transactions for each original signal owner,
including their token accounts (`filters.tokenAccounts = all`). The provider's
opaque cursor and fixed time window persist in the private runtime checkpoint.
Only the summary and bounded signature-linked facts are public.

## Evidence

- `sold`: successful PumpSwap sell instruction, official program/discriminator,
  matching mint/user/vault and exact SPL input amount.
- `transferred`: direct SPL movement to a resolved different owner. It does not
  establish whether that recipient shares control or eventually sells.
- `service`: destination matches configured infrastructure; not proof of a sale.
- `unclassified`: unresolved destination or unexplained debit; not a sale.
- `internal`: movement between accounts with the same on-chain owner.

Amounts are gross activity since catch, potentially including subsequent buys.
They are not a partition of the original lot and may exceed circulating supply
through turnover. Decoded sales do not automatically confirm accumulation or
close a thesis. Complete post-catch history cannot repair legacy purchase-window
attribution; those original-position bounds remain explicitly uncertain.

## Bounds and Repair

Each run allows at most 24 pages / 90 seconds, one page per owner, up to 100
transactions per page. The existing provider routing, method capability checks,
monthly credit limits and circuit breakers apply. At least a quarter of slots
are reserved for older catches when both recent and old work exist. Oldest
attempts run first within each group. Targeted passes continue audits even if no
new discovery candidates need deep scanning.

RPC failures preserve evidence and cursor, retry after 15 minutes, and restart
the fixed window after three errors. Duplicate receipts never inflate amounts.
Incomplete receipt windows do not advance the checkpoint; they replay after an
hour. Unrecognized position instructions and the 2048-event per-owner safety cap
remain incomplete, rather than silently dropping evidence. No full-history
claim is made from ordinary wallet-only signature pagination.

UI: inspect Wallets for progress, gross amounts, destination links and transaction
links. `Sales observed` or `Transfers observed` means facts were found, not that
all other wallet histories are complete. `None found in checked history` appears
only after all stored original-owner histories have fresh complete coverage.

Decoder source: [official PumpSwap IDL](https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump_amm.json).
History contract: [Alchemy getTransactionsForAddress](https://www.alchemy.com/docs/chains/solana/solana-api-endpoints/get-transactions-for-address).
