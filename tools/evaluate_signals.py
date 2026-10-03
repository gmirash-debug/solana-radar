#!/usr/bin/env python3
"""Evaluate a saved dashboard report or prospective dataset without API calls."""

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signal_evaluation import CostScenario, EvaluationOptions, evaluate_signals


def _cost(value):
    try:
        name, fees, slippage = value.split(":")
        return CostScenario(name, float(fees), float(slippage))
    except (ValueError, TypeError) as error:
        raise argparse.ArgumentTypeError("use NAME:ROUNDTRIP_FEES_BPS:ROUNDTRIP_SLIPPAGE_BPS") from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="local JSON report/snapshot or evaluation dataset")
    parser.add_argument("--as-of", help="UTC/offset-aware evaluation time; defaults to saved generated_at")
    parser.add_argument("--horizons", nargs="+", default=list(EvaluationOptions().horizons))
    parser.add_argument("--holdout-start", help="predeclared timezone-aware holdout boundary")
    parser.add_argument("--holdout-fraction", type=float, default=0.3)
    parser.add_argument("--min-sample", type=int, default=30, help="complete pairs required in EACH train/holdout stratum")
    parser.add_argument("--max-delay-seconds", type=float, default=3600)
    parser.add_argument("--max-capture-gap-seconds", type=float, default=3600)
    parser.add_argument("--age-ratio", type=float, default=1.5)
    parser.add_argument("--mcap-ratio", type=float, default=1.5)
    parser.add_argument("--liquidity-ratio", type=float, default=1.5)
    parser.add_argument("--min-pair-coverage", type=float, default=0.8)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--cost-scenario", action="append", type=_cost,
                        help="NAME:ROUNDTRIP_FEES_BPS:ROUNDTRIP_SLIPPAGE_BPS; repeat to replace defaults")
    parser.add_argument("--summary-only", action="store_true", help="compact report/Learning summary contract")
    args = parser.parse_args(argv)
    try:
        options = EvaluationOptions(
            horizons=tuple(args.horizons), holdout_start=args.holdout_start,
            holdout_fraction=args.holdout_fraction, min_sample=args.min_sample,
            max_delay_seconds=args.max_delay_seconds, max_capture_gap_seconds=args.max_capture_gap_seconds,
            age_ratio=args.age_ratio, mcap_ratio=args.mcap_ratio, liquidity_ratio=args.liquidity_ratio,
            min_pair_coverage=args.min_pair_coverage, bootstrap_samples=args.bootstrap_samples,
            random_seed=args.seed,
            cost_scenarios=tuple(args.cost_scenario) if args.cost_scenario else EvaluationOptions().cost_scenarios,
        )
        result = evaluate_signals(json.loads(args.report.read_text(encoding="utf-8")), as_of=args.as_of, options=options)
        print(json.dumps(result["summary"] if args.summary_only else result, indent=2, sort_keys=True, allow_nan=False))
    except (OSError, ValueError, TypeError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
