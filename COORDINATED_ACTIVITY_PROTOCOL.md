# Coordinated Accumulation Evidence

Research and implementation snapshot: 2026-10-01. This is a descriptive risk layer,
not a trade recommendation, identity attribution, or a replacement for confirmation.

## Research Conclusions

Proxima is a multi-wallet launch and position-management platform. Its official
[site](https://proxima.tools/) links the requested JoinProxima account and its
documentation. The unrelated proxima.one was excluded. Documentation and 18
published changelog entries were reviewed; the running authenticated application
was not inspected. Latest published changelog examined: [v0.9.5](https://proxima.tools/changelog/v.0.9.5/),
2026-07-30. Documentation is not a transaction-level verification of its claims.

| Documented function | Forensic implication | Important limit |
| --- | --- | --- |
| [Block-0 launch configuration](https://docs.proxima.tools/create-launch/launch-settings/block-0-configuration/) | Examine early orders, signers, concentration and subsequent flows. | Same slot is not a proven bundle or a common operator. |
| [Quick, Organic, Manual and scheduled modes](https://docs.proxima.tools/create-launch/launch-settings/launch-modes/) | Delayed buying can be automated; coordinated activity need not happen in one block. | Organic is a product mode, not evidence of independent demand. Execution order is not guaranteed. |
| [Wallet marketplace](https://docs.proxima.tools/wallets/marketplace/) | Age, holdings and existing transaction history cannot establish independence. | Transfer of wallet control is not normally an on-chain event. |
| [Generated activity](https://docs.proxima.tools/wallets/generate-activity/) | Examine repeated turnover, counterparties and behavior changes, rather than transaction count alone. | Genuine transactions can still be automated preparation. |
| [Classic/proxy and CEX funding](https://docs.proxima.tools/wallets/fund-wallets/) | Direct edges and timing can be examined separately. | A common CEX hot wallet is not a common customer. Unknown source identity stays unknown. |
| [Cleanup and alternate routing](https://docs.proxima.tools/live-settings/cleanup/) | Distinguish direct transfers from market-mediated replacement and claims. | A sell followed by another wallet's purchase does not establish a one-to-one position transfer. Terminal-like routing is not operator attribution. |
| [AutoBuy/AutoSell](https://docs.proxima.tools/live-settings/auto-buy/) | Repeated response delays, DCA and inventory behavior may indicate automation. | DCA, arbitrage and legitimate market making can look similar. |
| [Volume bot](https://docs.proxima.tools/live-settings/volume-bot/) | Compare gross turnover with retained inventory; both sides contribute to volume. | Round trips are not unique to Proxima and are not automatically wash trading. |
| [Sniper guard](https://docs.proxima.tools/create-launch/funding/sniper-guard/) | Correlated cohort exits may follow external buying. | This is reactive behavior, not a guarantee that external traders cannot buy. |
| [Token transfers](https://docs.proxima.tools/live-settings/send-tokens/), [dust recovery](https://docs.proxima.tools/account/recover-dust/) | Track actual distribution and consolidation, including emptied intermediaries. | Treasury, airdrop and maintenance operations can be legitimate. |
| [Fee claims](https://docs.proxima.tools/live-settings/claim-creator-fees/), [fees](https://docs.proxima.tools/other/platform-fees/) | Protocol rewards and fee recipients provide context. | A stated fee percentage is not a universal identifying fingerprint. |
| [Launchpad options](https://docs.proxima.tools/create-launch/launchpad-options/), [LP controls](https://docs.proxima.tools/live-settings/liquidity-pool/) | Creator, vault, tax and LP actions need their own interpretation. | No single creator/program label provides universal attribution. |

Marketing claims about untraceability were not independently verified. The
appropriate scanner objective is to expose concentrated/coordinated behavior,
not to declare that a token was launched using Proxima.

## Requested X Post

[MidCurveMortal's post](https://x.com/MidCurveMortal/status/2105305853339619640),
2026-09-30 14:36:44 UTC, alleges a Super Inu/SI cluster. The text cites more than
30 of the top 50 holders having less than two weeks of history, a cluster above
20% supply, and 8 of the top 20 FOMO holders sharing a start date with little
other activity. These are the author's observations and ownership inference,
not findings independently reproduced by this implementation.

The exact canonical post text was retrieved via the public FxTwitter JSON
mirror. Bright Data was inactive. The three attached images and the alleged
wallet cluster were not independently verified; no contract was inferred from
the text. The post motivated hypotheses, not hardcoded wallet labels.

## Comparable Primary Sources

- [Bubblemaps Time Nodes](https://wiki.bubblemaps.io/bubblemaps-v2/time-nodes):
  time-bounded interactions with shared services, rather than merging every
  service user. Useful as a behavioral hypothesis, not shared-owner proof.
- [Bubblemaps Magic Nodes](https://wiki.bubblemaps.io/bubblemaps-v2/magic-nodes):
  intermediaries can link holders while holding none of the target asset.
- [Jito documentation](https://docs.jito.wtf/lowlatencytxnsend/): bundles are
  sequential, atomic groups within a slot; the service returns bundle IDs.
  Same-slot public transactions alone do not identify their submission group.
- [Jito observational research](https://ben-weintraub.com/files/solanamev.pdf):
  explains limits of reconstructing private execution from public chain data.
- [GMGN labels](https://docs.gmgn.ai/index/featured-icon-definition): useful
  provider context, not a published reproducible proof of common control.
- Public case studies: [BULLISH](https://blog.bubblemaps.io/live-on-monad-latest-insights/),
  [WOLF](https://intel.bubblemaps.io/cases/12/hayden-daviss-wolf-rug),
  [MELANIA](https://blog.bubblemaps.io/huge-sell-offs-on-melania/). These reports
  illustrate funding and distribution questions; their trades were not replayed
  here and their labels are not imported as scanner truth.

## Implemented Evidence

`coordinated_activity.py` is a deterministic, dependency-free, no-RPC analyzer.
It consumes already-observed transactions, pre-buy profiles and verified positions.

1. **Synchronous purchases:** at least three owners assignable to three distinct
   transactions within 120 seconds. One router transaction with many recipients
   cannot manufacture this signal. Slots, executors, fees and equal amounts are
   not independent ownership evidence.
2. **Young low-activity cohort:** same first observed activity UTC date, at most
   three pre-buy transactions and up to 14 days before buying, with explicitly
   complete supplied pre-buy history. A capped 50-signature page cannot satisfy
   this requirement. Cached profiles must match the analyzed first-buy signature
   and time. This is observed activity, not cryptographic wallet creation.
3. **Common parsed direct funding:** successful System transfers, including
   inner instructions, before buying. Balance-delta guesses remain unverified.
   Returned/outgoing legs are subtracted and account net credit caps the amount
   when available. Transfers must be at least 0.05 SOL and, for material evidence, at least 10%
   of the first known buy cost. Unknown amounts and unsolicited dust abstain.
4. **Synchronized preparation:** the same parsed source funds at least three
   buyers within 15 minutes; their purchases also span at most 15 minutes and
   follow funding within an hour. Funding plus funding timing is one family.
5. **Inventory churn:** at least two observed buys and sells by the same owner,
   at least 80% observed turnover and verified retention at most 20%. Supporting
   only. Low inventory is independently balance-derived, not manufactured by
   the observed-sales cap. Seller attribution requires a parsed token transfer
   from its owned account into the pool vault, not the largest transaction-wide
   debit or fee payer. Missing provenance abstains. Same-transaction round trips
   may be invisible to net-delta parsing.

### Materiality And Presentation

The default material gate is at least three holders, at least 1% total supply,
and at least two different evidence families intersecting on those same holders.
Funding-linked watch additionally requires material funding plus purchase timing.
Turnover is not eligible to manufacture this gate. Percentages use the union of
qualifying holders, deduplicated across groups; unrelated holders are excluded.

Known infrastructure sources are excluded through labels or configured addresses.
An unclassified source can still be a service. Even a parsed financial link is
not a proof of control: every result preserves `ownership=not_established` and
`bundle=not_established`. No action tier or Ready gate is promoted by this layer.

Only material combinations appear as warning badges in token lists. Supporting
coincidences stay in Supply/Wallets details. The UI displays scope, coverage,
verification age and the held-supply percentage of the qualifying group.
Unknown balances/supply are not zero. No match means no match in the checked
subset, never an all-clear for the entire token.

## Pipeline Integration And Cost

- Solana wave analysis retains up to 200 already-fetched trade observations per
  alert, at the existing minimum trade size, plus existing wallet profiles and
  balances. Truncation is explicitly partial. Profile reads are not expanded.
- The thesis stores bounded inputs only for its original cohort, including the
  verified original retention cap using provenance-resolved sales. An initial
  failed balance read never freezes a zero cap. Balance rechecks cannot resurrect
  that sold inventory from replacement deposits; failed or stale reads stay unknown.
  Known sales after the triggering window are counted through the balance check
  from already-fetched swaps, separately from the window-scoped timing evidence.
  Proven-sale totals persist independently of whether the initial balance read
  succeeded, so a later successful read cannot erase known exits.
  Same-second sales are included conservatively because second-level timestamps
  cannot order transactions; no claim is made about which inventory was sold.
- Robinhood uses the same analyzer and renderer, cached canonical block times,
  raw-unit balances and conservative retained lower bounds. Missing times are
  not guessed. Existing EVM preparation evidence remains separate because the
  aggregate groups do not supply all material per-wallet funding fields.
- Summary payloads in Python and Cloudflare strip transaction inputs, member
  lists and funding addresses. Full evidence remains in the existing detail
  path. No new D1 tables or migrations are required.
- **Incremental analyzer RPC requests: zero.** This does not mean the full scan
  is free; discovery, existing classification and balance checks still cost API
  calls. Computation and bounded JSON storage increase modestly.
- Transaction detail requests now explicitly permit version 1. Unsupported
  versions/invalid request errors are classified as client errors and do not
  repeat the identical request across providers.

## Settings And Rollback

`config.example.json` contains `coordinated_activity_enabled`, the nested
`coordinated_activity` settings and an infrastructure-address exclusion list.
All thresholds are initial hypotheses, not backtest-calibrated optima. Defaults
are the same in the pure analyzer and both adapters. Disable computation with
`coordinated_activity_enabled=false`; historical evidence stays timestamped.

The existing age, liquidity, migration and wave-selection filters are unchanged.
In particular, this does not introduce a new launch-age discovery lane. Events
below those existing gates and pools not selected for inspection can still be
missed. The overlay does not repair discovery starvation or the existing D1
backlog/freshness problems documented in the scanner audit.

## Not Implemented Or Not Proven

- Complete top-50 holder profiling, full-lifetime history, or identification of
  marketplace-purchased accounts.
- Cross-token repeated cohorts, multi-hop common deposit/consolidation graphs,
  source-chain CEX account attribution or pattern-response automation models.
- Proxima attribution, exact Jito membership, manipulative intent or a guaranteed
  profitable entry. There is no universal public Proxima fingerprint.
- Market-mediated cleanup is not treated as a proven wallet-to-wallet transfer.
- Positive precision/recall is not established. Measure future outcomes before
  changing trade eligibility based on these labels.

## Verification

The automated suite covers duplicates, overlapping/disjoint groups, dust,
service exclusions, incomplete profiles, missing balances/supply, bounded
output, deterministic behavior, churn, frozen-cohort rechecks, summary redaction,
RPC version routing, escaped UI text and material-only list badges. Local UI
fixtures are never published as real observations. Production rollout and
fresh real-world evidence must be reported separately from passing unit tests.

The existing offline reactivation replay still matches one of two fixture tiers;
the unseasoned early-wave fixture expects a stronger tier than the current
confirmation policy allows. This baseline mismatch is not hidden by weakening
the policy and is not a precision/recall estimate for the new risk layer.
