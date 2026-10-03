# Prospective Signal Evaluation

This is an offline, observational **shadow** evaluator. It does not import the
scanner, call APIs, update confirmation, change ranking, execute trades, or
write scanner state. `edge_claim` is always `false`, even when samples pass the
review gates. Sample readiness is permission to review evidence, not evidence
of an edge.

## Integration API

```python
from signal_evaluation import CostScenario, EvaluationOptions, evaluate_signals

result = evaluate_signals(
    saved_payload,
    as_of="2026-10-03T00:00:00Z",  # optional if saved generated_at/as_of exists
    options=EvaluationOptions(
        horizons=("24h", "72h"),
        holdout_start="2026-10-01T00:00:00Z",
        min_sample=30,
        cost_scenarios=(CostScenario("base", 100, 200),),
    ),
)
report["signal_evaluation"] = result["summary"]  # integration by the owner
```

The evaluator returns a new JSON-safe object and never mutates `saved_payload`.
No scanner/dashboard integration is installed by these files. Callers should
attach the summary explicitly; Learning must not interpret it as confirmation
or a replacement for `stats.signal_outcomes` methodology version 2.

For the current raw state plus aggregate report, the parent can evaluate without
changing either input:

```python
legacy = evaluate_signals({
    "report": report,
    "state": {"signal_outcomes": state.get("signal_outcomes", {})},
}, options=EvaluationOptions(holdout_start=frozen_holdout_start))
report["signal_evaluation"] = legacy["summary"]
```

When prospective captures exist, export the dataset below instead. Match
signal checkpoints from `state.signal_outcomes` only when **both token and
normalized caught time match**; never attach an earliest-token outcome to a
later reactivation. Controls need their own checkpoints from already collected
market snapshots; existing signal-only outcomes cannot supply their returns.

### Summary Contract, Version 1

`result["summary"]` contains:

- `schema_version`, `mode="shadow"`, `offline=true`, `as_of`, `source`.
- `edge_claim=false`, `production_effect="none"`, `execution="estimate_only"`.
- `policy_id`: fingerprint of evaluation settings, not of the input dataset.
  `policy` includes costs, calipers, horizon delay tolerance, sample/coverage
  thresholds, holdout selection and bootstrap settings.
- `counts`: input signals/controls, invalid/future rows, unique episodes/tokens,
  duplicate/conflicting/repeated captures, primary signals/controls, selected
  pairs, baseline exclusion reasons and rejected feature values. Exception
  counters are sparse: an absent counter means zero.
- `holdout`: explicit or automatically selected boundary and purge rule.
- `horizons`: primary signal/control outcome-status counts, complete price pairs in
  non-purged splits, and the number of sample-ready strata. Counts of missing
  outcomes include all primary captures, not only price-complete pairs.
- `strata`: separate strategy/config/family results. Each horizon exposes review
  status/blockers, purged signals, and train/holdout sample sizes, missingness,
  gross returns and net scenarios. Each metric exposes `n`, mean/median,
  positive percentage, mean 95% interval and positive-rate 95% interval.
- `warnings`: caveats that must remain visible alongside any displayed returns.

Learning can display missing/late/pending counts, matched-pair coverage, sample
readiness, return basis, holdout boundary and cost scenarios from this contract.
Always display the `n` and interval with a return; do not label gross or paired
returns as profit, alpha, confirmed quality or execution performance.

Full output also contains `strata` with control status counts and per-feature
diagnostics, `episode_diagnostics` with each exclusion/outcome/partition, and
`control_matches` identifying both tokens and the capture-time matching distance.
The full output contains token identifiers; the summary contains no wallet data.

## Saved Inputs

Supported inputs are local JSON objects:

1. A dashboard report, or a saved snapshot containing `report`.
2. A `signal_outcomes` map keyed by token or list of scanner outcome rows. A
   snapshot can provide these at `state.signal_outcomes` with a `report` time.
   A bare saved state requires `--as-of` when it has no saved evaluation time.
3. A prospective `signal_evaluation_dataset` inside a report, or a standalone
   object with `episodes`, `controls`, and `generated_at`/`as_of`.

An ordinary saved dashboard report currently contains **aggregate**
`stats.signal_outcomes`, not per-token outcome checkpoints. Aggregates cannot
be reconstructed into a dataset. Such reports evaluate exported alerts as
pending/missing observations and report that coverage gap, with no claim of
matched evidence. The exported alert adapter uses `obs_mcap_at` (then
`created_at`/`window_start`), `obs_price_usd`, `obs_mcap_usd`, and
`obs_liquidity_usd`. It never substitutes current enriched pool values, age,
scores, wallet rankings, or the report's current configuration for historical
catch data. Pair creation time can derive age at the catch. Alerts already
represented by a tracked token are not re-added as synthetic repeat episodes.

Existing scanner outcomes supply frozen `caught_at`, price, market cap,
liquidity, `config_version`, family and horizon checkpoints. Existing rows may
lack `strategy_version` and catch-time age: these omissions are explicit and
prevent matching rather than being guessed. Unknown versions remain separate
descriptive strata. The current tracker retains the earliest catch per token,
has retention/size limits, and is not a complete episode/control archive.

History-ledger `events` and current Learning wallet aggregates are **not**
automatically adapted. The ledger can attach token outcomes to a different
reactivation episode; an integration must export the correct original catch and
matching horizon targets into the contract below. Otherwise targets fail
validation. No historical wallet score is read or reconstructed.

### Prospective Capture Contract

Freeze strategy/configuration identifiers and matching covariates at selection
time; do not backfill them from later market/wallet snapshots. Use the actual
effective per-lane configuration fingerprint, not the report's latest config.
Changing strategy logic or evaluation settings creates a new version/policy.

The exact supported fields are below. Extra fields are ignored, not evaluated.
Required capture identity fields apply to both signals and controls. A missing
matching field permits descriptive counts but blocks matched comparison.

| Field | Type | Requirement/meaning |
| --- | --- | --- |
| `token_address` | nonempty string | Required; actual mint, case-sensitive; no pool fallback |
| `caught_at` | offset-aware ISO string or Unix seconds | Required; frozen decision/capture time and horizon anchor |
| `episode_id` | string | Optional stable ID; derived from token/time if omitted |
| `strategy_version` | nonempty string | Required for matching; frozen deployed logic version |
| `config_version` | nonempty string | Required for matching; frozen effective lane fingerprint |
| `signal_family` | nonempty string | Required for matching; strategy family tested on either cohort |
| `caught_age_hours` | finite number >=0 | Required for matching, or derive from frozen `token_age_days`/`pair_created_at` |
| `caught_mcap_usd` | finite number >0 | Required for matching |
| `caught_liquidity_usd` | finite number >0 | Required for matching |
| `caught_price_usd` | finite number >0 | Required for complete matched-price returns |
| `captured_at` | offset-aware ISO string or Unix seconds | Optional capture-decision timestamp <= `caught_at`; invalid/late values block matching and features |
| `caught_score` | finite number | Optional immutable score at catch; not a current wallet score |
| `feature_snapshot` | object | Optional `{ "at": timestamp, "values": { allowlisted_name: finite_number } }`; time <= catch |
| `horizons` | object | Keys `1h`, `6h`, `24h`, `72h`, `7d`; start as `{}` and freeze first checkpoints |
| `signal_present` | boolean | Controls only: must be exactly `false` at selection |
| `selection_at` | offset-aware ISO string or Unix seconds | Controls only: required, <= `caught_at` |
| `selection_method` | nonempty string | Controls only: required prospective selection-rule identifier |

`captured_at` refers to the capture decision, not a later log-write or report
publication time. Price observations must actually have been available at that
decision. A collector may separately retain `stored_at`/`market_observed_at` for
audit; these extra fields are not used as permission to backdate captures.

Each horizon checkpoint has this exact evaluated contract:

| Field | Type | Requirement/meaning |
| --- | --- | --- |
| `target_at` | offset-aware ISO string or Unix seconds | Required; catch + fixed horizon within one second |
| `at` | offset-aware ISO string or Unix seconds | Required; actual endpoint market observation time, not evaluation/report generation time |
| `price_usd` | finite number >=0 | Price endpoint; needed for matched-price returns |
| `return_pct` | finite number >=-100 | Optional tracker gross return; used descriptively only when comparable prices are absent |
| `mcap_usd` | finite number >=0 | Optional market-cap proxy if prices and reported return are absent; descriptive only |
| `quality_status` | string or null | `complete`, `ready` or absent/null permit timing checks; `delayed` is late, other values excluded |
| `market_snapshot_stale` | boolean | Optional; truthy stale flag excludes endpoint |

Scanner fields such as `delay_seconds`, peak return, drawdown and time-to-X are
not inputs to return eligibility; delay is independently recomputed. Omitted
checkpoint keys mean pending/missing by evaluation time. Do not put fabricated
zero-valued checkpoints in missing slots. The module validates the documented
evaluated fields, not the truthfulness of external point-in-time declarations.

```json
{
  "generated_at": "2026-10-03T00:00:00Z",
  "signal_evaluation_dataset": {
    "episodes": [{
      "episode_id": "signal-capture-1",
      "token_address": "SIGNAL_MINT",
      "caught_at": "2026-10-01T00:00:00Z",
      "captured_at": "2026-10-01T00:00:00Z",
      "strategy_version": "reactivation-v3",
      "config_version": "sha256:effective-lane-config",
      "signal_family": "reactivation_wave",
      "caught_age_hours": 48,
      "caught_mcap_usd": 100000,
      "caught_liquidity_usd": 20000,
      "caught_price_usd": 0.001,
      "caught_score": 60,
      "feature_snapshot": {
        "at": "2026-10-01T00:00:00Z",
        "values": {"unique_buyers": 10, "retained_supply_pct": 2.5}
      },
      "horizons": {
        "24h": {
          "target_at": "2026-10-02T00:00:00Z",
          "at": "2026-10-02T00:10:00Z",
          "price_usd": 0.0012,
          "return_pct": 20,
          "quality_status": "complete"
        }
      }
    }],
    "controls": [{
      "episode_id": "control-capture-1",
      "token_address": "CONTROL_MINT",
      "caught_at": "2026-10-01T00:00:00Z",
      "selection_at": "2026-10-01T00:00:00Z",
      "selection_method": "prospective_universe",
      "signal_present": false,
      "strategy_version": "reactivation-v3",
      "config_version": "sha256:effective-lane-config",
      "signal_family": "reactivation_wave",
      "caught_age_hours": 48,
      "caught_mcap_usd": 100000,
      "caught_liquidity_usd": 20000,
      "caught_price_usd": 0.001,
      "horizons": {}
    }]
  }
}
```

Both cohorts must be selected and logged before outcomes are known. For a
control, `signal_present` must be exactly JSON `false`, `selection_method` must
be supplied, and `selection_at` must be no later than `caught_at`. The strategy
and family identify the strategy **tested on** the control, not a fired signal.
Keep all selected controls, including later failed, delisted and missing tokens.
Universe rows, current noise tiers, random historical winners and wallet scores
are not eligible implicit controls. Timestamp declarations are trusted source
data, not cryptographic proof of prospective capture; retain the original log.

`caught_age_hours` can alternatively derive from frozen `token_age_days` or
`pair_created_at` (Unix seconds or timezone-aware ISO time). Current `age_hours`
is deliberately ignored. Prices/covariates must use comparable units across
cohorts. Naive times, boolean numbers and nonfinite numbers are not accepted.

## Methodology

- Deduplicate by case-sensitive token plus normalized catch timestamp, not alert,
  pool, family or supplied episode ID. Duplicate capture metadata conflicts fail
  closed; reusing an episode ID for different captures also fails closed.
  Horizon exports merge without selecting the highest return: the earliest
  checkpoint is frozen; conflicting checkpoints at the same time are excluded.
- Keep all unique episodes in diagnostics, but only the earliest observation
  per token across both cohorts enters primary inference. Later episodes cannot
  replace missing first outcomes, increase independent sample sizes, reuse a
  control or move a token between train and holdout. A later signal on an earlier
  control token does not retroactively relabel the control. Additional episodes
  are descriptive exclusions, not separate independent bets.
- Horizon targets must equal catch plus `1h/6h/24h/72h/7d` within one second.
  Default eligible delay is 0 through 3600 seconds inclusive, matching scanner
  methodology version 2. Missing horizons are `pending` before the target and
  `missing` afterwards. Future, early, late, incomplete, conflicting or invalid
  checkpoints remain separate; never use latest/peak/ATH returns to fill them.
- Recompute price returns from positive entry price and nonnegative endpoint
  price. Explicit zero endpoint is a -100% gross loss. With no comparable prices,
  finite reported returns or market-cap proxies are descriptive only and cannot
  enter complete matched-price pairs. Gross returns below -100% are invalid.
- Match greedily in chronological signal order using capture covariates only,
  within identical known strategy/config/family strata and the same split.
  Defaults: capture-time gap <= 3600 seconds; age, market cap and liquidity ratio
  <= 1.5. Ages use a one-hour floor for the ratio; market cap and liquidity must
  be positive. Nearest distance is normalized time gap plus absolute log ratios;
  token/time tie-breaks are deterministic. Matching is one-to-one without
  replacement and is identical across horizons. It is a descriptive baseline,
  not optimal matching, propensity weighting, or randomization.
- Match **before** outcome eligibility. A matched missing/late control is kept
  in the selected-pair denominator, not replaced with an eligible alternative.
  Report selected pairs, complete price pairs, unmatched signals and completion.
- Holdout is an explicit declared timestamp or the latest 30% of distinct signal
  capture-time blocks. Tokens captured at the same time stay together. Fewer
  than two time blocks cannot define an automatic holdout. For each horizon,
  training catch + horizon + maximum delay must end strictly before the cutoff;
  actual checkpoints reaching/crossing it also purge the training observation.
  Matches cannot cross the split; a purged control cannot complete a train pair.
  Missing holdout policy defaults to automatic chronological splitting, with a
  warning that this is not preregistration. Explicit future cutoffs, a single
  time block, or empty train/holdout samples cannot satisfy review gates.
- A stratum/horizon is `ready_for_shadow_review` only with known versions/family,
  an available chronological holdout, >=30 complete independent price pairs in
  **each** train and holdout split and >=80% selected-pair completion in each.
  Thresholds are configurable, not scientifically calibrated power guarantees.
  Defaults intentionally block small samples. Even passing does not claim edge.
- Mean 95% intervals use seeded token-level percentile bootstrap (default 1000
  resamples). Positive-rate intervals use Wilson's method. Singleton mean
  intervals are unavailable; empty metrics are null, not zero. These intervals
  do not remove market/time dependence, fat-tail uncertainty, selection bias or
  multiplicity. No significance/p-value or causal attribution is generated.
- Net estimate = gross return percentage minus
  `(roundtrip_fees_bps + roundtrip_slippage_bps) / 100`. Defaults are base
  (100 fee + 200 slippage bps) and stress (100 + 1000 bps). These are illustrative
  assumptions, not live quotes or per-side charges. No clipping conceals costs
  on a total loss. Same costs on signals/controls cancel in paired excess.
  Notional size, MEV, failed fills, route impact, transfer taxes, solvency and
  tradability are unmodeled; endpoint liquidity does not prove executability.
- Feature diagnostics use only frozen `caught_score` and a timestamped
  `feature_snapshot` no later than catch, restricted to `caught_score`,
  `retained_supply_pct`, `buyer_coverage_pct`, `volume_1h_to_mcap`, `unique_buyers`.
  Historical/current wallet scores and all other fields are ignored. Report
  sample size, missingness and Pearson correlation per version/split/horizon.
  Correlations need >=3 rows and nonzero variance, are exploratory only, and
  never influence production confirmation, weights, thresholds or ranking.

## CLI and Verification

```bash
/tmp/radar-audit-venv-20261001/bin/python tools/evaluate_signals.py data/latest_report.json --summary-only
/tmp/radar-audit-venv-20261001/bin/python tools/evaluate_signals.py /path/to/saved-dataset.json --as-of 2026-10-03T00:00:00Z --holdout-start 2026-10-01T00:00:00Z --horizons 24h 72h --cost-scenario base:100:200 --cost-scenario stress:100:1000
/tmp/radar-audit-venv-20261001/bin/python -m unittest discover -s tests -p test_signal_evaluation.py -v
```

The CLI prints JSON to stdout and never overwrites the source or writes reports.
`--summary-only` prints the parent integration contract; omit it for diagnostics.
Invalid input/settings return exit code 2. Successful evaluation returns 0 even
when evidence is insufficient: lack of evidence is data, not a tool failure.
Use `--help` for calipers, sample/coverage thresholds, bootstrap and delay settings.

## Bounded Collection, No New API Calls

These are parent integration suggestions, not storage/collector code installed
by this module:

1. Freeze a sampling policy, deployed strategy/config identifiers, explicit
   holdout boundary, and evaluation cost scenarios before collection. At each
   decision, use only the scanner's already available candidate/market snapshot
   and strategy result. Capture signals and contemporaneous non-signals tested
   under that same strategy; never use later returns or Learning wallet scores
   to select the cohort. Do not retroactively populate missing historical fields.
2. Cap **combined** signal/control capture rows at 400 per UTC day. Prefer
   predeclared stratified deterministic hash sampling of the eligible universe
   by age/mcap/liquidity buckets, with seed and eligible/captured/deferred counts
   recorded. If signals consume a reserved share, record its coverage explicitly.
   No replacing sampled missing tokens with later successful ones. A hard cap
   means this is a sampled cohort, not all scanner candidates.
3. Use daily compressed JSONL partitions, or a local SQLite store, keyed by
   `(token_address, caught_at)`. Store the immutable capture and <=5 separate
   first checkpoints keyed by `(token_address, caught_at, horizon)`. Keep
   candidate counts, sampling seed, version/policy identifiers and archive
   coverage metadata. Omit wallet arrays, transaction histories and mutable
   enriched dashboard objects; they are unnecessary for this evaluator.
4. Retain 90 days: <=36,000 capture rows and <=180,000 checkpoint rows at the
   400/day cap. An eight-day active queue bounds unresolved work at <=3,200
   captures for a seven-day maximum horizon plus one-hour grace and daily
   maintenance. These are row bounds, not promised byte sizes. Expired archive
   rows must be reported as retention-limited coverage, not outcome failures.
5. At existing scanner/report runs, fill due control horizons using already
   stored fresh market observations whose real timestamps satisfy the horizon.
   Reuse correctly anchored signal checkpoints from `state.signal_outcomes`.
   Do not request prices, balances, wallets or history. No timely observed
   endpoint means missing after the grace window, not a zero loss or a fabricated
   execution. Keep any first late checkpoint as late; do not repair it by choosing
   a later successful snapshot. Evaluations always use a saved `as_of`.
6. Run offline evaluation on the bounded archive outside Learning rendering;
   publish only the summary and retain detailed diagnostics privately. Default
   matching indexes controls by stratum/split and capture-time range. Bootstrap
   CPU still scales with sample size/resamples: evaluate a declared bounded
   window/version and do not shrink it after seeing bad outcomes. A 400-row
   smoke fixture is covered by the focused tests; this is not a production
   throughput guarantee for the full 90-day archive.

Remaining limits: saved data cannot recover omitted controls or missing catch
versions/covariates; retention/deletion/truncated report coverage can create
survivorship bias. Frozen identifiers and capture timestamps require trustworthy
collection. Automatic holdout chosen after inspecting outcomes is not truly
untouched. Inputs are in-memory; bootstrap cost scales with samples.
A durable prospective archive, predeclared
policies, time-block-aware inference, power analysis and real execution evidence
are future integrations, not features installed by this module. Matching uses
time-range indexing but remains quadratic in the worst case of coincident
captures; inputs and bootstrap samples are in memory.
