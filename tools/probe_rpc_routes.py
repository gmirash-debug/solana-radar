#!/usr/bin/env python3
"""Read-only access checks. Does not write scanner state or print endpoint secrets."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scanner
from rpc_routing import validate_result

DEFAULT_MINT = "7bK4jRMa3aY85wJMQEQqWsmY9mmrRT32AFWZ23cBpump"


def probe(config, mint=DEFAULT_MINT, owner="11111111111111111111111111111111"):
    config = dict(config)
    for name in ("helius", "alchemy", "chainstack", "drpc", "publicnode"):
        config[name + "_rpc_max_retries"] = 0
        config[name + "_rpc_timeout_seconds"] = 12
        config[name + "_transactions_timeout_seconds"] = 15
        config[name + "_rpc_credit_budget_per_scan"] = 500
    rpc = scanner.build_rpc_router(config)
    rows = []
    now = int(time.time())
    requests = [
        ("getSlot", [{"commitment": "finalized"}], "head"),
        ("getTokenSupply", [mint], "supply"),
        ("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}], "balance_access"),
        ("getSignaturesForAddress", [mint, {"limit": 1}], "signature_access"),
        ("getTokenLargestAccounts", [mint], "holder_access"),
        ("getTransactionsForAddress", [mint, {"transactionDetails": "full", "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
            "limit": 1, "sortOrder": "asc", "filters": {"status": "succeeded", "blockTime": {"gte": now - 15 * 86400, "lte": now - 7 * 86400}}}], "history_7_15_days"),
    ]
    signature = None
    for name in rpc.standard_order:
        provider = rpc.providers[name]
        for method, params, purpose in requests:
            if not provider.can_call(method, params) or (method == "getTransactionsForAddress" and not provider.enhanced_history):
                rows.append({"provider": name, "method": method, "status": "skipped_unsupported_or_budget", "purpose": purpose})
                continue
            try:
                result = provider.call(method, params)
                validate_result(method, params, result)
                row = {"provider": name, "method": method, "status": "ok", "purpose": purpose}
                values = result.get("data", result.get("value")) if isinstance(result, dict) else result
                if isinstance(values, list):
                    row["rows"] = len(values)
                if method == "getSignaturesForAddress" and result:
                    signature = signature or result[0]["signature"]
                if purpose == "history_7_15_days":
                    row["archive_coverage"] = "sample_returned_not_completeness_proof" if values else "no_rows_not_proof_of_absence"
                rows.append(row)
            except (scanner.HeliusRpcError, scanner.HeliusCircuitOpen, ValueError, TypeError, AttributeError) as exc:
                category = getattr(exc, "category", "invalid_or_unavailable")
                rows.append({"provider": name, "method": method, "status": category, "purpose": purpose})
                rpc._record_provider_error(name, method, exc)
                if category in {"auth", "quota"}:
                    break
    if signature:
        try:
            result = rpc.transaction(signature)
            rows.append({"purpose": "transaction_route", "status": "ok" if result else "unavailable",
                         "provider": rpc.last_provider_by_method.get("getTransaction")})
        except scanner.RpcProvidersUnavailable:
            rows.append({"purpose": "transaction_route", "status": "unavailable"})
    return {"read_only": True, "mint": mint, "checks": rows, "providers": rpc.provider_stats(),
            "coverage": "bounded_access_and_history_samples_not_full_market_or_archive_audit"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mint", default=DEFAULT_MINT)
    parser.add_argument("--owner", default="11111111111111111111111111111111")
    args = parser.parse_args()
    if not scanner.clean_solana_address(args.mint) or not scanner.clean_solana_address(args.owner):
        parser.error("mint and owner must be Solana addresses")
    scanner.load_env()
    config = scanner.load_json(scanner.CONFIG_PATH if scanner.CONFIG_PATH.exists() else scanner.DEFAULT_CONFIG_PATH, {})
    result = probe(config, args.mint, args.owner)
    # Provider diagnostic messages are intentionally omitted from public CI logs.
    for provider in result["providers"].values():
        provider.pop("last_error", None)
        provider.pop("method_circuits", None)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
