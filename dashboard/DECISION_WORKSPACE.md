# Decision workspace

## Problems addressed

- Scanner diagnostics and duplicate KPIs displaced the actual token list.
- `Candidate` and `Check needed` concealed known reductions in original balances.
- `Supply watch` looked like a trading recommendation instead of ownership risk.
- Loading errors left a permanent loading label or an empty wallet table that looked conclusive.
- Mobile navigation lost the list position; deletion sync errors were easy to miss.

## Presentation contract

The new queues are derived UI views, not new scanner filters or buy recommendations.
The detector, scoring, confirmation thresholds, age limits, deleted-token blacklist,
and historical signal records are unchanged.

1. **Ready to review**: current scanner-confirmed activity, intact cohort, fresh
   balance check, required cohort coverage, fresh market, and complete distributed
   supply evidence. This does not imply a profitable or safe entry.
2. **Holding**: the backend's intact original position with sufficient coverage.
   Unversioned historical signals remain explicitly unconfirmed. Stale checks stay
   visible as *Held at last check* and cannot enter Ready to review.
3. **Early observations**: fresh, unconfirmed activity. Unknown cohort coverage
   and invalidated or weakening positions are not hidden here.
4. **Reduced positions**: the backend recorded weakening, even if confirmation is
   missing or checks became overdue. A balance decline is not proof of a sale.
5. **Needs data**: missing, invalid, or insufficient evidence. Coverage of stored
   wallets is distinct from coverage of all original signal wallets.
6. **Closed**: backend-invalidated positions. Excluded from the default overview.

Overview expands review, holding and early observations; reduced and incomplete
groups remain accessible as counted collapsed groups and dedicated views. Search
expands matching groups. Sorting is newest catch first *within each group*, with
an optional retained-position sort. This ordering is not a probability score.

## Metrics and navigation

- Position left: original acquired token amount remaining at the last check.
- Supply share: retained tokens as a percentage of total token supply, not the
  percentage of the original position. Partial and stale evidence is labeled.
- Since catch: token price change, not realized wallet profit.
- Historical ATH, narrative, and scanner evidence remain accessible in the detail
  folds and Wallets / Supply / Social / Evidence tabs.
- Current age scope comes from the report configuration, not a fixed UI string.

## Detail loading

Requests are bounded by the existing 12-second timeout. Failures clear loading,
show a summary-only notice, and allow explicit Retry. Automatic retries have a
five-minute cooldown. Details with an older report/check timestamp, wrong token,
or an obsolete in-flight generation cannot overwrite the summary. Same-generation
details are cached across refreshes only while they are at least as fresh as the
incoming thesis. Mobile list browsing does not request hidden detail panels.

## Verification

- 144 Python tests and 48 JavaScript tests pass (12 new presentation/regression tests).
- Browser checks: 320, 390, 768, 1024, 1440, and 1920px; list and detail navigation,
  search, queue empty states, partial coverage, error display and detail retry.
- Network-failure and deletion tests use intercepted responses in an isolated
  browser, not real mutations to production records.
- Source icons: Lucide static 0.468.0, license included in `icons/LICENSE`.

## Remaining limitation

The UI cannot fill missing original-wallet coverage or restore an exhausted D1
quota. It exposes these limitations rather than turning candidates into confirmed
signals. The 2026-09-04 21:16 UTC snapshot has 14 intact, 43 weakening and 11 unknown
open theses, with no queue-ready confirmed current signal. These are dated counts.
