"""Dependency-free failed-run classification for scanner and workflow callers."""

from subprocess import TimeoutExpired

PROGRAMMING_ERRORS = {
    "AssertionError", "AttributeError", "IndexError", "KeyError", "NameError",
    "TypeError", "UnboundLocalError", "ValueError", "StaleStateRevisionError",
}
SOFT_CATEGORIES = {"rpc_all_unavailable", "rate_limit", "quota", "auth", "circuit_open", "transport", "timeout"}
SOFT_SUFFIXES = ("_rate_limit", "_quota", "_auth", "_circuit_open", "_transport", "_timeout")


def failure_metadata(error):
    if not isinstance(error, BaseException):
        return {}
    category = getattr(error, "category", None)
    name = type(error).__name__
    if name == "RpcProvidersUnavailable":
        category = "rpc_all_unavailable"
    elif name == "HeliusCircuitOpen":
        category = "circuit_open"
    elif isinstance(error, (TimeoutError, TimeoutExpired)):
        category = "timeout"
    elif name == "HeliusRpcError" and category == "temporary":
        category = "transport"
    return {"error_type": name, "error_category": category,
            "error_kind": "programming" if isinstance(error, (AssertionError, AttributeError, LookupError,
                NameError, TypeError, ValueError)) or name == "StaleStateRevisionError" else "runtime"}


def scanner_failure_class(status_payload, exit_code):
    if int(exit_code or 0) == 0:
        return "success"
    payload = status_payload if isinstance(status_payload, dict) else {}
    if payload.get("error_type") in PROGRAMMING_ERRORS or payload.get("error_kind") == "programming":
        return "hard_failure"
    category = payload.get("error_category")
    if category in SOFT_CATEGORIES:
        return "soft_provider_failure"
    health = payload.get("scan_health") or {}
    categories = {str(name) for name, count in (health.get("scan_error_categories") or {}).items() if count}
    if categories and all(name in SOFT_CATEGORIES or name.endswith(SOFT_SUFFIXES) for name in categories):
        return "soft_provider_failure"
    return "hard_failure"
