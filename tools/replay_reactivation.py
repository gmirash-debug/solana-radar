#!/usr/bin/env python3
"""Replay deterministic Reactivation fixtures without RPC or market API calls."""

import argparse
import json
import statistics
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "reactivation_replay.json"
sys.path.insert(0, str(ROOT))

import scanner


def run_case(case, base_config):
    pool = scanner.Pool(**case["pool"])
    config = scanner.reactivation_stage_config(
        pool,
        scanner.apply_lane(base_config, "reactivation"),
    )
    config.update(case.get("config_overrides") or {})
    tier, reasons, penalties, quality = scanner.classify_alert_tier(
        pool,
        dict(case["alert"]),
        dict(case.get("evidence") or {}),
        config,
    )
    expected = case["expected"]
    detected_at = case.get("detected_at")
    pump_started_at = case.get("pump_started_at")
    lead_minutes = None
    if detected_at and pump_started_at:
        lead_minutes = max(
            0,
            (scanner.parse_timestamp(pump_started_at) - scanner.parse_timestamp(detected_at)) / 60,
        )
    return {
        "id": case["id"],
        "expected_tier": expected["tier"],
        "actual_tier": tier,
        "passed": tier == expected["tier"],
        "lead_minutes": lead_minutes,
        "outcome_24h_pct": expected.get("outcome_24h_pct"),
        "estimated_rpc_credits": int(case.get("estimated_rpc_credits") or 0),
        "reasons": reasons,
        "penalties": penalties,
        "quality": quality,
    }


def replay(fixtures):
    base_config = scanner.load_json(scanner.DEFAULT_CONFIG_PATH, {})
    cases = [run_case(case, base_config) for case in fixtures["cases"]]
    detected = [case for case in cases if case["actual_tier"] in {"actionable", "hot_reactivation", "watch"}]
    expected_detectable = [case for case in cases if case["expected_tier"] in {"actionable", "hot_reactivation", "watch"}]
    false_positive = [case for case in cases if case["actual_tier"] in {"actionable", "hot_reactivation", "watch"} and case["expected_tier"] == "noise"]
    lead_times = [case["lead_minutes"] for case in detected if case["lead_minutes"] is not None]
    outcomes = [case["outcome_24h_pct"] for case in detected if case["outcome_24h_pct"] is not None]
    return {
        "fixture_set": fixtures.get("fixture_set", "reactivation_replay"),
        "offline": True,
        "cases": cases,
        "summary": {
            "cases": len(cases),
            "passed": sum(case["passed"] for case in cases),
            "recall_pct": round(100 * sum(case["actual_tier"] in {"actionable", "hot_reactivation", "watch"} for case in expected_detectable) / max(1, len(expected_detectable)), 2),
            "false_positive_pct": round(100 * len(false_positive) / max(1, len(detected)), 2),
            "median_lead_minutes": statistics.median(lead_times) if lead_times else None,
            "median_outcome_24h_pct": statistics.median(outcomes) if outcomes else None,
            "estimated_rpc_credits": sum(case["estimated_rpc_credits"] for case in cases),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = replay(json.loads(args.fixtures.read_text()))
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(serialized)
    else:
        print(serialized, end="")
    if payload["summary"]["passed"] != payload["summary"]["cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
