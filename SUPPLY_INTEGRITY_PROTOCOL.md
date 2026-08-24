# Supply Integrity Protocol

## Purpose

Supply Integrity is a second-stage risk layer for a Reactivation signal. It does
not discover tokens and it does not replace the buy-wave score. It answers four
separate questions after a token is caught:

1. How concentrated are the largest token accounts against verified total
   supply?
2. How concentrated do resolved owners appear after an identifiable pool or
   burn reserve is excluded?
3. How much of the signal cohort is still visible among the resolved top
   holders?
4. Do independently observed wallet links support a coordinated supply cluster?

There is deliberately no combined opaque score. Each answer remains visible so
the operator can distinguish concentration, coordination, retention, and data
quality.

## Data Contract

For each eligible caught token the scanner reads:

- verified mint supply through `getTokenSupply`;
- up to 20 largest token accounts through `getTokenLargestAccounts`;
- parsed token-account owners through one `getMultipleAccounts` batch;
- the persisted signal cohort and its latest verified balances;
- common-funder and common-executor groups already proven by the wallet graph;
- exact priority-fee estimates from the caught buy transactions.

The RPC router applies its normal provider budget and failover policy. Supply
Integrity has an additional per-scan unique-token budget so overlapping windows
for one mint cannot consume several slots or crowd out signal discovery and
cohort rechecks.

## Denominators

`raw_concentration` always uses verified total token supply. It is the safest
holder concentration view, but can include pool-controlled or burn accounts.

`estimated_circulating_concentration` is emitted only when a pool-controlled
reserve is resolved from the checked token accounts. Any resolved burn balance
is excluded as well. The result is explicitly marked `approximate`; it is not
claimed to be authoritative circulating supply.

`cohort_top_holder_supply_pct` is the current balance of signal-cohort owners
that are present in the resolved top-holder set, divided by verified total
supply. It is not a measure of all cohort holdings when a cohort wallet falls
outside the top accounts.

`max_linked_cluster_current_supply_pct` measures current balances held by a
linked signal-cohort cluster. `max_linked_cluster_signal_supply_pct` includes
only tokens attributed to the caught buy wave and later verified as retained.

## Relationship Evidence

The scanner recognizes three evidence families:

- `common_funder`: at least two signal wallets share a verified funding source;
- `common_executor`: at least two signal wallets share a verified routed
  executor that was not mistaken for the buyer identity;
- `priority_fee`: at least the configured wallet count and wallet share used the
  same estimated priority fee.

Priority-fee matching is supporting evidence only. It does not join wallets into
a supply cluster by itself. Coordination is confirmed only when at least two
independent evidence families converge inside the same connected signal-cohort
cluster. Two unrelated groups with different evidence families do not satisfy
this rule. The dashboard exposes every group, its key, its member wallets, and
whether it is direct or support-only evidence.

## Statuses

- `Supply concentrated`: a configured top-holder threshold is exceeded, or a
  coordinated linked cluster exceeds the linked current/signal supply threshold.
- `Supply watch`: some link evidence or cohort/top-holder concentration is
  present, but the concentrated threshold is not met.
- `Supply distributed`: owner resolution is complete, the pool reserve is
  resolved, and none of the watch thresholds are met.
- `Supply unverified`: required data is unavailable, invalid, below owner
  resolution coverage, or insufficient for an honest circulating estimate.

The status never changes the original Reactivation tier. It is a separate risk
decision shown beside the signal status.

## Validation And Failure Rules

The snapshot is quarantined as `unverified` when:

- the largest-account total exceeds verified supply beyond tolerance;
- top-1, top-5, top-10, and top-20 concentration is not monotonic;
- resolved owner amount exceeds the observed largest-account amount;
- supply or the largest-account set cannot be verified.

Partial RPC results are not treated as clean distribution. Missing owner labels,
pool reserve identification, CEX labels, or terminal labels are stated as data
boundaries rather than inferred.

## Refresh And Storage

- At most 12 tokens receive a Supply Integrity check in one deep scan.
- Complete snapshots refresh every 180 minutes.
- Partial or unavailable snapshots retry after 60 minutes.
- A temporary provider or budget failure does not erase the last verified
  snapshot. The dashboard keeps its original `checked_at`, records the failed
  refresh attempt, and retries on the shorter cadence.
- The current full snapshot and up to 56 compact historical snapshots are kept
  in the token thesis document in Cloudflare D1.
- The public dashboard list receives only compact decision fields. Full owners,
  linkage groups, clusters, validation errors, and limitations load from the
  D1 token-detail document when the user opens a token.

The default history therefore covers roughly seven days when a token is checked
on the nominal three-hour cadence. A provider outage or per-scan budget can make
the real cadence slower; each snapshot carries its own `checked_at` timestamp.

## Known Limits

- The largest 20 token accounts are a bounded view, not a complete holder census.
- A wallet can split supply below the top-account boundary.
- A common funder or executor supports linkage but does not prove common legal
  ownership or insider intent.
- Matching transaction fees can come from shared defaults, bots, or fee markets.
- CEX, bridge, locker, market-maker, and terminal labels require a maintained
  external address-label source and are not fabricated when that source is absent.
- Supply concentration is risk context, not a prediction of price performance.
