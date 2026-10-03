"""Offline, point-in-time signal diagnostics. Never imports the live scanner."""

import copy
import hashlib
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


SCHEMA_VERSION = 1
HORIZONS = {"1h": 3600, "6h": 21600, "24h": 86400, "72h": 259200, "7d": 604800}
FEATURES = (
    "caught_score", "retained_supply_pct", "buyer_coverage_pct",
    "volume_1h_to_mcap", "unique_buyers",
)
UNKNOWN = "unknown"


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _timestamp(value):
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc).timestamp()
        if not isinstance(value, str) or not value.strip():
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _label(value):
    return value.strip() if isinstance(value, str) and value.strip() else UNKNOWN


@dataclass(frozen=True)
class CostScenario:
    """Total roundtrip estimates, not per-side fees or simulated executions."""

    name: str = "base"
    roundtrip_fees_bps: float = 100
    roundtrip_slippage_bps: float = 200

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("cost scenario name must be nonempty")
        object.__setattr__(self, "name", self.name.strip())
        for name in ("roundtrip_fees_bps", "roundtrip_slippage_bps"):
            value = _number(getattr(self, name))
            if value is None or not 0 <= value <= 10000:
                raise ValueError("roundtrip costs must be finite, nonnegative basis points <= 10000")
            object.__setattr__(self, name, value)

    @property
    def cost_pct(self):
        return (float(self.roundtrip_fees_bps) + float(self.roundtrip_slippage_bps)) / 100


@dataclass(frozen=True)
class EvaluationOptions:
    horizons: tuple = ("1h", "6h", "24h", "72h", "7d")
    max_delay_seconds: float = 3600
    max_capture_gap_seconds: float = 3600
    age_ratio: float = 1.5
    mcap_ratio: float = 1.5
    liquidity_ratio: float = 1.5
    holdout_fraction: float = 0.3
    holdout_start: str = None
    min_sample: int = 30
    min_pair_coverage: float = 0.8
    bootstrap_samples: int = 1000
    random_seed: int = 7
    cost_scenarios: tuple = (
        CostScenario(), CostScenario("stress", 100, 1000),
    )

    def __post_init__(self):
        if not self.horizons or len(set(self.horizons)) != len(self.horizons) or any(
            horizon not in HORIZONS for horizon in self.horizons
        ):
            raise ValueError("horizons must be unique supported horizons")
        object.__setattr__(self, "horizons", tuple(self.horizons))
        for name in ("max_delay_seconds", "max_capture_gap_seconds"):
            value = _number(getattr(self, name))
            if value is None or value < 0:
                raise ValueError(name + " must be finite and nonnegative")
            object.__setattr__(self, name, value)
        for name in ("age_ratio", "mcap_ratio", "liquidity_ratio"):
            value = _number(getattr(self, name))
            if value is None or value < 1:
                raise ValueError(name + " must be finite and >= 1")
            object.__setattr__(self, name, value)
        fraction = _number(self.holdout_fraction)
        if fraction is None or not 0 < fraction < 1:
            raise ValueError("holdout_fraction must be between 0 and 1")
        object.__setattr__(self, "holdout_fraction", fraction)
        if self.holdout_start is not None and _timestamp(self.holdout_start) is None:
            raise ValueError("holdout_start must be a timezone-aware timestamp")
        coverage = _number(self.min_pair_coverage)
        if coverage is None or not 0 <= coverage <= 1:
            raise ValueError("min_pair_coverage must be between 0 and 1")
        object.__setattr__(self, "min_pair_coverage", coverage)
        for name, minimum in (("min_sample", 2), ("bootstrap_samples", 100)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(name + " must be an integer >= " + str(minimum))
        if isinstance(self.random_seed, bool) or not isinstance(self.random_seed, int):
            raise ValueError("random_seed must be an integer")
        if not self.cost_scenarios or any(not isinstance(c, CostScenario) for c in self.cost_scenarios):
            raise ValueError("cost_scenarios must contain CostScenario instances")
        if len({c.name for c in self.cost_scenarios}) != len(self.cost_scenarios):
            raise ValueError("cost scenario names must be unique")
        object.__setattr__(self, "cost_scenarios", tuple(self.cost_scenarios))


def _rows(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [dict(row, token_address=row.get("token_address") or token)
                if isinstance(row, dict) else row for token, row in value.items()]
    if value is None:
        return []
    raise ValueError("cohort/outcome collection must be a list or token-keyed object")


def _alert_row(alert):
    pool = _mapping(alert.get("pool"))
    # Current enriched pool prices, ages, scores and wallet metrics are not catch snapshots.
    return {
        "token_address": pool.get("token_address"),
        "caught_at": alert.get("obs_mcap_at") or alert.get("created_at") or alert.get("window_start"),
        "caught_price_usd": alert.get("obs_price_usd"),
        "caught_mcap_usd": alert.get("obs_mcap_usd"),
        "caught_liquidity_usd": alert.get("obs_liquidity_usd"),
        "caught_age_hours": alert.get("obs_age_hours"),
        "pair_created_at": pool.get("pair_created_at"),
        "strategy_version": alert.get("strategy_version"),
        "config_version": alert.get("config_version"),
        "signal_family": alert.get("signal_family"),
        "horizons": {},
    }


def _extract(payload):
    report = _mapping(payload.get("report")) or payload
    dataset = _mapping(report.get("signal_evaluation_dataset"))
    if not dataset and ("episodes" in report or "controls" in report):
        dataset = report
    if dataset:
        return report, _rows(dataset.get("episodes")), _rows(dataset.get("controls")), "prospective_dataset"
    state = _mapping(payload.get("state"))
    outcomes = _rows(report.get("signal_outcomes") or state.get("signal_outcomes"))
    tracked = {row.get("token_address") for row in outcomes if isinstance(row, dict)}
    alerts = [_alert_row(alert) if isinstance(alert, dict) else alert for alert in _rows(report.get("alerts"))]
    outcomes.extend(row for row in alerts if not isinstance(row, dict) or row.get("token_address") not in tracked)
    return report, outcomes, _rows(report.get("control_cohort")), "saved_tracker_or_report"


def _normalize(row, cohort):
    if not isinstance(row, dict):
        return None
    token = row.get("token_address")
    caught = _timestamp(row.get("caught_at"))
    if not isinstance(token, str) or not token.strip() or caught is None:
        return None
    age = _number(row.get("caught_age_hours"))
    if age is None and _number(row.get("token_age_days")) is not None:
        age = _number(_number(row["token_age_days"]) * 24)
    created = _timestamp(row.get("pair_created_at"))
    if age is None and created is not None:
        age = (caught - created) / 3600
    key = (token.strip(), caught)
    capture = _timestamp(row.get("captured_at"))
    invalid_capture = "captured_at" in row and capture is None
    capture_late = capture is not None and capture > caught
    features = {}
    rejected_features = 0
    score = _number(row.get("caught_score"))
    if score is not None and not invalid_capture and not capture_late:
        features["caught_score"] = score
    elif score is not None:
        rejected_features += 1
    snapshot = _mapping(row.get("feature_snapshot"))
    feature_at = _timestamp(snapshot.get("at"))
    for name, value in _mapping(snapshot.get("values")).items():
        number = _number(value)
        if name not in FEATURES or feature_at is None or feature_at > caught or number is None or invalid_capture or capture_late:
            rejected_features += 1
        else:
            features[name] = number
    result = {
        "key": key,
        "episode_id": str(row.get("episode_id") or "capture:" + hashlib.sha256(
            (key[0] + "|" + _iso(caught)).encode()).hexdigest()[:24]),
        "token_address": key[0], "caught_ts": caught, "caught_at": _iso(caught),
        "cohort": cohort,
        "strategy_version": _label(row.get("strategy_version")),
        "config_version": _label(row.get("config_version")),
        "signal_family": _label(row.get("signal_family")),
        "caught_age_hours": age,
        "caught_mcap_usd": _number(row.get("caught_mcap_usd")),
        "caught_liquidity_usd": _number(row.get("caught_liquidity_usd")),
        "caught_price_usd": _number(row.get("caught_price_usd")),
        "selection_ts": _timestamp(row.get("selection_at")),
        "capture_ts": capture, "invalid_capture_timestamp": invalid_capture,
        "signal_present": row.get("signal_present"),
        "selection_method": _label(row.get("selection_method")),
        "features": features, "rejected_features": rejected_features,
        "horizons": copy.deepcopy(_mapping(row.get("horizons"))),
        "exclusion": None,
    }
    result["stratum"] = tuple(result[name] for name in ("strategy_version", "config_version", "signal_family"))
    return result


def _checkpoint_evidence(checkpoint):
    checkpoint = _mapping(checkpoint)
    quality = checkpoint.get("quality_status")
    return (
        _timestamp(checkpoint.get("at")), _timestamp(checkpoint.get("target_at")),
        _number(checkpoint.get("price_usd")), _number(checkpoint.get("mcap_usd")),
        _number(checkpoint.get("return_pct")),
        "complete" if quality in (None, "complete", "ready") else quality,
        bool(checkpoint.get("market_snapshot_stale")), bool(checkpoint.get("conflicting")),
    )


def _deduplicate(signal_rows, control_rows, as_of):
    counts = Counter(input_signals=len(signal_rows), input_controls=len(control_rows))
    captures = {}
    ids = defaultdict(set)
    for cohort, rows in (("signal", signal_rows), ("control", control_rows)):
        for raw in rows:
            row = _normalize(raw, cohort)
            if row is None:
                counts["invalid_rows"] += 1
                continue
            if row["caught_ts"] > as_of:
                counts["future_captures"] += 1
                continue
            ids[row["episode_id"]].add(row["key"])
            existing = captures.get(row["key"])
            if existing is None:
                captures[row["key"]] = row
                continue
            counts["duplicate_captures"] += 1
            immutable = (
                "cohort", "stratum", "caught_age_hours", "caught_mcap_usd",
                "caught_liquidity_usd", "caught_price_usd", "selection_ts",
                "capture_ts", "invalid_capture_timestamp", "signal_present", "selection_method", "features",
            )
            if any(existing[name] != row[name] for name in immutable):
                existing["exclusion"] = "conflicting_capture"
            for horizon, checkpoint in row["horizons"].items():
                if not checkpoint:
                    continue
                old = existing["horizons"].get(horizon)
                if not old:
                    existing["horizons"][horizon] = checkpoint
                    continue
                old_at = _timestamp(_mapping(old).get("at"))
                new_at = _timestamp(_mapping(checkpoint).get("at"))
                if old_at == new_at:
                    if _checkpoint_evidence(old) != _checkpoint_evidence(checkpoint):
                        existing["horizons"][horizon] = {"conflicting": True, "at": _mapping(old).get("at")}
                elif old_at is None or new_at is None:
                    # Unplaceable exports cannot silently disappear depending on input order.
                    existing["horizons"][horizon] = {"conflicting": True}
                elif old_at is not None and new_at is not None and new_at < old_at:
                    existing["horizons"][horizon] = checkpoint
    for keys in ids.values():
        if len(keys) > 1:
            for key in keys:
                captures[key]["exclusion"] = "reused_episode_id"
    ordered = sorted(captures.values(), key=lambda r: (r["caught_ts"], r["token_address"]))
    seen = set()
    for row in ordered:
        if row["token_address"] in seen and row["exclusion"] is None:
            row["exclusion"] = "repeated_token"
        seen.add(row["token_address"])
    counts["unique_episodes"] = len(ordered)
    counts["unique_tokens"] = len(seen)
    for row in ordered:
        if row["exclusion"]:
            counts[row["exclusion"]] += 1
    return ordered, dict(counts)


def _outcome(row, horizon, as_of, options):
    due = row["caught_ts"] + HORIZONS[horizon]
    checkpoint = row["horizons"].get(horizon)
    result = {"status": "pending" if as_of < due else "missing", "return_pct": None,
              "return_basis": None, "delay_seconds": None, "target_at": _iso(due)}
    if not checkpoint:
        return result
    checkpoint = _mapping(checkpoint)
    if checkpoint.get("conflicting"):
        result["status"] = "conflicting_checkpoint"
        return result
    at = _timestamp(checkpoint.get("at"))
    target = _timestamp(checkpoint.get("target_at"))
    if at is None or target is None or abs(target - due) > 1:
        result["status"] = "invalid_timestamp"
        return result
    result["delay_seconds"] = at - due
    if at > as_of:
        result["status"] = "future_outcome"
    elif at < due:
        result["status"] = "early"
    elif at - due > options.max_delay_seconds or checkpoint.get("quality_status") == "delayed":
        result["status"] = "late"
    elif checkpoint.get("quality_status") not in (None, "complete", "ready") or checkpoint.get("market_snapshot_stale"):
        result["status"] = "incomplete"
    else:
        entry = row["caught_price_usd"]
        exit_price = _number(checkpoint.get("price_usd"))
        value = None
        basis = None
        if entry is not None and entry > 0 and exit_price is not None and exit_price >= 0:
            value, basis = (exit_price / entry - 1) * 100, "price"
        elif _number(checkpoint.get("return_pct")) is not None:
            value, basis = _number(checkpoint["return_pct"]), "reported_unverified_basis"
        elif row["caught_mcap_usd"] is not None and row["caught_mcap_usd"] > 0:
            mcap = _number(checkpoint.get("mcap_usd"))
            if mcap is not None and mcap >= 0:
                value, basis = (mcap / row["caught_mcap_usd"] - 1) * 100, "mcap_proxy"
        if value is None or not math.isfinite(value) or value < -100:
            result["status"] = "invalid_return"
        else:
            result.update(status="eligible", return_pct=value, return_basis=basis)
    return result


def _baseline_reason(row):
    if UNKNOWN in row["stratum"]:
        return "unknown_stratum"
    if row["caught_age_hours"] is None or row["caught_age_hours"] < 0 or any(
        row[name] is None or row[name] <= 0 for name in ("caught_mcap_usd", "caught_liquidity_usd")
    ):
        return "missing_matching_covariates"
    if row["invalid_capture_timestamp"]:
        return "invalid_capture_timestamp"
    if row["capture_ts"] is not None and row["capture_ts"] > row["caught_ts"]:
        return "late_capture"
    if row["cohort"] == "control" and (
        row["signal_present"] is not False or row["selection_ts"] is None
        or row["selection_ts"] > row["caught_ts"] or row["selection_method"] == UNKNOWN
    ):
        return "control_not_prospectively_selected"
    return None


def _cutoff(rows, options):
    if options.holdout_start is not None:
        return _timestamp(options.holdout_start)
    times = sorted({r["caught_ts"] for r in rows if r["cohort"] == "signal" and not r["exclusion"]})
    if len(times) < 2:
        return None
    index = min(len(times) - 1, max(1, math.floor(len(times) * (1 - options.holdout_fraction))))
    return times[index]


def _distance(signal, control, options):
    gap = abs(signal["caught_ts"] - control["caught_ts"])
    if gap > options.max_capture_gap_seconds:
        return None
    distance = gap / max(1, options.max_capture_gap_seconds)
    for name, maximum, floor in (
        ("caught_age_hours", options.age_ratio, 1),
        ("caught_mcap_usd", options.mcap_ratio, 0),
        ("caught_liquidity_usd", options.liquidity_ratio, 0),
    ):
        first, second = max(floor, signal[name]), max(floor, control[name])
        ratio = max(first, second) / min(first, second)
        if ratio > maximum:
            return None
        distance += abs(math.log(first / second))
    return distance


def _match(rows, options):
    controls = [r for r in rows if r["cohort"] == "control" and not r["exclusion"] and not r["baseline_exclusion"]]
    by_stratum = defaultdict(list)
    for control in controls:
        by_stratum[(control["stratum"], control["split"])].append(control)
    timestamps = {key: [r["caught_ts"] for r in group] for key, group in by_stratum.items()}
    used = set()
    matches = []
    for signal in rows:
        if signal["cohort"] != "signal" or signal["exclusion"] or signal["baseline_exclusion"]:
            continue
        candidates = []
        key = (signal["stratum"], signal["split"])
        times = timestamps.get(key, [])
        start = bisect_left(times, signal["caught_ts"] - options.max_capture_gap_seconds)
        end = bisect_right(times, signal["caught_ts"] + options.max_capture_gap_seconds)
        for control in by_stratum[key][start:end]:
            if control["key"] in used:
                continue
            distance = _distance(signal, control, options)
            if distance is not None:
                candidates.append((distance, control["caught_ts"], control["token_address"], control))
        if candidates:
            distance, _, _, control = min(candidates, key=lambda item: item[:3])
            used.add(control["key"])
            matches.append((signal, control, distance))
    return matches


def _percentile(values, percentile):
    position = (len(values) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _metrics(values, options):
    n = len(values)
    result = {"n": n, "mean_pct": None, "median_pct": None, "mean_ci95_pct": None,
              "positive_pct": None, "positive_ci95_pct": None,
              "confidence_method": "token-independent percentile bootstrap mean; Wilson positive-rate interval"}
    if not values:
        return result
    mean = statistics.mean(values)
    positives = sum(v > 0 for v in values)
    p = positives / n
    z = 1.959963984540054
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    result.update(mean_pct=mean, median_pct=_percentile(sorted(values), 0.5), positive_pct=p * 100,
                  positive_ci95_pct=[max(0, center - half) * 100, min(1, center + half) * 100])
    if n >= 2:
        rng = random.Random(options.random_seed)
        scale = max(abs(v) for v in values) or 1
        normalized = [v / scale for v in values]
        means = sorted(statistics.fmean(rng.choices(normalized, k=n)) * scale for _ in range(options.bootstrap_samples))
        result["mean_ci95_pct"] = [_percentile(means, 0.025), _percentile(means, 0.975)]
    return result


def _correlation(pairs):
    if len(pairs) < 3:
        return None
    x_scale = max(abs(x) for x, _ in pairs)
    y_scale = max(abs(y) for _, y in pairs)
    if not x_scale or not y_scale:
        return None
    pairs = [(x / x_scale, y / y_scale) for x, y in pairs]
    x_mean = statistics.mean(x for x, _ in pairs)
    y_mean = statistics.mean(y for _, y in pairs)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
    denominator = math.sqrt(sum((x - x_mean) ** 2 for x, _ in pairs) * sum((y - y_mean) ** 2 for _, y in pairs))
    return max(-1, min(1, numerator / denominator)) if denominator else None


def _group_result(rows, matches, horizon, split, options):
    signals = [r for r in rows if r["cohort"] == "signal" and r["partitions"][horizon] == split]
    controls = [r for r in rows if r["cohort"] == "control" and r["partitions"][horizon] == split]
    selected = [(s, c) for s, c, _ in matches if s["partitions"][horizon] == split]
    pairs = [(s, c) for s, c in selected if c["partitions"][horizon] == split and all(
        r["outcomes"][horizon]["status"] == "eligible" and r["outcomes"][horizon]["return_basis"] == "price"
        for r in (s, c)
    )]
    values = [r["outcomes"][horizon]["return_pct"] for r in signals if r["outcomes"][horizon]["status"] == "eligible"]
    gross = _metrics(values, options)
    gross["return_basis_counts"] = dict(Counter(r["outcomes"][horizon]["return_basis"] for r in signals
                                               if r["outcomes"][horizon]["status"] == "eligible"))
    scenarios = {}
    for cost in options.cost_scenarios:
        signal_net = [s["outcomes"][horizon]["return_pct"] - cost.cost_pct for s, _ in pairs]
        control_net = [c["outcomes"][horizon]["return_pct"] - cost.cost_pct for _, c in pairs]
        scenarios[cost.name] = {
            **asdict(cost), "cost_pct": cost.cost_pct, "execution": "estimate_only",
            "all_signal_net": _metrics([v - cost.cost_pct for v in values], options),
            "matched_signal_net": _metrics(signal_net, options),
            "matched_control_net": _metrics(control_net, options),
            "paired_excess": _metrics([s - c for s, c in zip(signal_net, control_net)], options),
        }
    diagnostics = {}
    for name in FEATURES:
        feature_pairs = [(r["features"][name], r["outcomes"][horizon]["return_pct"]) for r in signals
                         if name in r["features"] and r["outcomes"][horizon]["status"] == "eligible"]
        diagnostics[name] = {
            "n": len(feature_pairs), "missing_at_capture": sum(name not in r["features"] for r in signals),
            "pearson_r": _correlation(feature_pairs), "shadow_only": True, "production_applied": False,
        }
    return {
        "signals": len(signals), "controls": len(controls),
        "signal_outcome_status_counts": dict(Counter(r["outcomes"][horizon]["status"] for r in signals)),
        "control_outcome_status_counts": dict(Counter(r["outcomes"][horizon]["status"] for r in controls)),
        "selected_pairs": len(selected), "complete_price_pairs": len(pairs),
        "pair_completion_pct": 100 * len(pairs) / len(selected) if selected else None,
        "unmatched_signals": len(signals) - len(selected),
        "gross_signal": gross, "net_scenarios": scenarios, "feature_diagnostics": diagnostics,
    }


def evaluate_signals(payload, *, as_of=None, options=None):
    """Return JSON-safe shadow evaluation without mutating input or fetching data.

    Accepts saved reports/snapshots, scanner signal_outcomes, or the documented
    signal_evaluation_dataset. Summary is suitable for report['signal_evaluation'].
    """
    if not isinstance(payload, dict):
        raise ValueError("input must be a JSON object")
    options = EvaluationOptions() if options is None else options
    if not isinstance(options, EvaluationOptions):
        raise ValueError("options must be EvaluationOptions")
    report, signal_rows, control_rows, source = _extract(payload)
    saved_time = report.get("as_of") if report.get("as_of") is not None else report.get("generated_at")
    as_of_ts = _timestamp(as_of if as_of is not None else saved_time)
    if as_of_ts is None:
        raise ValueError("as_of or report generated_at must be a timezone-aware timestamp")
    rows, counts = _deduplicate(signal_rows, control_rows, as_of_ts)
    cutoff = _cutoff(rows, options)
    active = [row for row in rows if not row["exclusion"]]
    for row in rows:
        row["baseline_exclusion"] = _baseline_reason(row)
        row["split"] = "holdout" if cutoff is not None and row["caught_ts"] >= cutoff else "train"
        row["outcomes"] = {h: _outcome(row, h, as_of_ts, options) for h in options.horizons}
        row["partitions"] = {}
        for horizon in options.horizons:
            checkpoint_at = _timestamp(_mapping(row["horizons"].get(horizon)).get("at"))
            end = row["caught_ts"] + HORIZONS[horizon] + options.max_delay_seconds
            purged = row["split"] == "train" and cutoff is not None and (
                end >= cutoff or (checkpoint_at is not None and checkpoint_at >= cutoff)
            )
            row["partitions"][horizon] = "purged" if purged else row["split"]
    matches = _match(rows, options)
    strata = []
    for stratum in sorted({r["stratum"] for r in active if r["cohort"] == "signal"}):
        group = [r for r in active if r["stratum"] == stratum]
        group_matches = [(s, c, d) for s, c, d in matches if s["stratum"] == stratum]
        horizons = {}
        for horizon in options.horizons:
            splits = {split: _group_result(group, group_matches, horizon, split, options)
                      for split in ("train", "holdout", "purged")}
            reasons = []
            if UNKNOWN in stratum:
                reasons.append("unknown_strategy_config_or_family")
            if cutoff is None or cutoff > as_of_ts:
                reasons.append("chronological_holdout_unavailable")
            for split in ("train", "holdout"):
                metrics = splits[split]
                if metrics["complete_price_pairs"] < options.min_sample:
                    reasons.append(split + "_insufficient_complete_pairs")
                if not metrics["selected_pairs"] or metrics["complete_price_pairs"] / metrics["selected_pairs"] < options.min_pair_coverage:
                    reasons.append(split + "_insufficient_pair_coverage")
            horizons[horizon] = {
                "splits": splits, "sample_ready": not reasons,
                "review_status": "ready_for_shadow_review" if not reasons else "insufficient_evidence",
                "review_blockers": reasons, "edge_claim": False,
            }
        strata.append({"strategy_version": stratum[0], "config_version": stratum[1],
                       "signal_family": stratum[2], "horizons": horizons})
    counts.update(primary_signals=sum(r["cohort"] == "signal" for r in active),
                  primary_controls=sum(r["cohort"] == "control" for r in active), selected_pairs=len(matches))
    counts["baseline_exclusion_counts"] = dict(Counter(r["baseline_exclusion"] for r in active if r["baseline_exclusion"]))
    counts["rejected_feature_values"] = sum(r["rejected_features"] for r in active)
    warnings = [
        "Observational shadow diagnostics, not trading edge, causality, or execution evidence.",
        "Missing, late, delisted and unobserved outcomes are not zero returns; complete-case results may be biased.",
        "Bootstrap/Wilson intervals ignore shared market shocks and are exploratory, not multiple-testing adjusted.",
        "Net scenarios subtract estimated roundtrip costs; identical costs cancel in paired excess.",
        "Chronological holdout is not proof of a preregistered or untouched experiment; freeze policy before collection.",
    ]
    if not control_rows:
        warnings.append("No explicit prospective control cohort supplied; universe/noise rows are not inferred as controls.")
    raw_outcomes = report.get("signal_outcomes") or _mapping(payload.get("state")).get("signal_outcomes")
    if _mapping(report.get("stats")).get("signal_outcomes") and source == "saved_tracker_or_report" and not raw_outcomes:
        warnings.append("Saved dashboard contains aggregate outcomes only; exported alerts have no attached historical checkpoints.")
    policy = asdict(options)
    policy_id = "sha256:" + hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()[:16]
    horizon_summary = {}
    for horizon in options.horizons:
        horizon_summary[horizon] = {
            "signal_outcome_status_counts": dict(Counter(r["outcomes"][horizon]["status"] for r in active if r["cohort"] == "signal")),
            "control_outcome_status_counts": dict(Counter(r["outcomes"][horizon]["status"] for r in active if r["cohort"] == "control")),
            "complete_price_pairs": sum(item["horizons"][horizon]["splits"][split]["complete_price_pairs"]
                                        for item in strata for split in ("train", "holdout")),
            "ready_strata": sum(item["horizons"][horizon]["sample_ready"] for item in strata),
        }
    summary = {
        "schema_version": SCHEMA_VERSION, "mode": "shadow", "offline": True,
        "as_of": _iso(as_of_ts), "source": source, "policy_id": policy_id,
        "edge_claim": False, "production_effect": "none", "execution": "estimate_only",
        "counts": counts, "horizons": horizon_summary, "warnings": warnings,
        "holdout": {"start_at": _iso(cutoff) if cutoff is not None else None,
                    "selection": "explicit" if options.holdout_start is not None else "chronological_signal_time_blocks",
                    "purge": "training horizon plus delay must end strictly before holdout"},
        "matching": "one-to-one, no replacement, capture-only calipers, same version/family and split",
        "policy": policy,
        "strata": [{
            "strategy_version": item["strategy_version"], "config_version": item["config_version"],
            "signal_family": item["signal_family"],
            "horizons": {horizon: {
                "sample_ready": details["sample_ready"], "review_status": details["review_status"],
                "review_blockers": details["review_blockers"],
                "splits": {split: {
                    **{key: metrics[key] for key in ("signals", "controls", "selected_pairs", "complete_price_pairs",
                                                     "pair_completion_pct", "signal_outcome_status_counts", "control_outcome_status_counts", "gross_signal")},
                    "net_scenarios": {name: {
                        **{key: scenario[key] for key in ("cost_pct", "all_signal_net", "matched_signal_net", "matched_control_net", "paired_excess")},
                    } for name, scenario in metrics["net_scenarios"].items()},
                } for split, metrics in details["splits"].items() if split != "purged"},
                "purged_signals": details["splits"]["purged"]["signals"],
            } for horizon, details in item["horizons"].items()},
        } for item in strata],
    }
    diagnostics = [{
        **{name: row[name] for name in ("episode_id", "token_address", "cohort", "caught_at", "strategy_version",
                                      "config_version", "signal_family", "exclusion", "baseline_exclusion", "rejected_features")},
        "outcomes": row["outcomes"], "partitions": row["partitions"],
    } for row in rows]
    return {
        "summary": summary, "strata": strata, "episode_diagnostics": diagnostics,
        "control_matches": [{"signal_episode_id": s["episode_id"], "control_episode_id": c["episode_id"],
                             "signal_token": s["token_address"], "control_token": c["token_address"],
                             "capture_distance": distance,
                             "capture_gap_seconds": abs(s["caught_ts"] - c["caught_ts"])} for s, c, distance in matches],
    }
