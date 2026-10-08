"""Task-specific routes and read-result contracts, independent of billing units."""
import math
from urllib.parse import urlparse

STANDARD_ORDER = ["chainstack", "helius", "alchemy", "drpc", "publicnode"]
HISTORY_ORDER = ["alchemy", "helius"]
BALANCE_ORDER = ["helius", "alchemy", "drpc", "publicnode", "chainstack"]
METHOD_ORDERS = {
    "getTransaction": STANDARD_ORDER,
    "getMultipleAccounts": STANDARD_ORDER,
    "getTokenSupply": STANDARD_ORDER,
    "getAccountInfo": STANDARD_ORDER,
    "getSlot": STANDARD_ORDER,
    "getSignaturesForAddress": ["helius", "alchemy", "drpc", "publicnode", "chainstack"],
    "getTokenLargestAccounts": ["alchemy", "helius", "drpc", "publicnode", "chainstack"],
    "getTransactionsForAddress": HISTORY_ORDER,
    "getTokenAccountsByOwner": BALANCE_ORDER,
}


def history_page_params(provider, params):
    """Full-history page caps differ; reservations must use the actual request."""
    address, options = params
    options = dict(options)
    cap = 1000 if provider == "helius" else 100
    options["limit"] = min(cap, max(1, int(options.get("limit", 100))))
    return [address, options]


def evm_endpoint_provider(endpoint):
    """No credentials in provider labels; custom proxy classification is parent-owned."""
    host = (urlparse(endpoint).hostname or "").lower()
    if host == "alchemy.com" or host.endswith(".alchemy.com"):
        return "alchemy"
    if host == "robinhood-rpc.publicnode.com":
        return "publicnode"
    if host == "rpc.mainnet.chain.robinhood.com":
        return "robinhood_public"
    if host == "rpc.ordofi.network":
        return "ordo"
    return "configured"


def evm_route_supports(provider, method, params):
    """Conservative known plan gates; dynamic unsupported routes are learned by Rpc."""
    if method != "eth_getLogs":
        return True
    if provider == "publicnode":
        return False  # Existing probe: historical logs require a provider token.
    if provider != "alchemy":
        return True
    if not params or not isinstance(params[0], dict):
        return False
    query = params[0]
    if query.get("blockHash"):
        return True
    start, end = query.get("fromBlock", "latest"), query.get("toBlock", "latest")
    if start == end and start in {"latest", "pending", "safe", "finalized"}:
        return True
    try:
        return 1 <= int(end, 16) - int(start, 16) + 1 <= 10
    except (ValueError, TypeError):
        return False  # Do not send an unbounded range to the Free-plan route.


def validate_result(method, params, result):
    """An absent/malformed balance is unknown, never evidence of a sale."""
    if method == "getTransaction":
        if result is not None and not isinstance(result, dict):
            raise ValueError("invalid transaction response")
    elif method == "getSignaturesForAddress":
        if not isinstance(result, list) or any(not isinstance(row, dict) or not row.get("signature") for row in result):
            raise ValueError("invalid signature list")
    elif method == "getTransactionsForAddress":
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise ValueError("invalid indexed history response")
    elif method in {"getTokenAccountsByOwner", "getTokenLargestAccounts", "getMultipleAccounts"}:
        if not isinstance(result, dict) or not isinstance(result.get("value"), list):
            raise ValueError("missing account list")
        if method == "getMultipleAccounts" and len(result["value"]) != len(params[0]):
            raise ValueError("incomplete account batch")
        if method == "getTokenAccountsByOwner":
            mint = params[1].get("mint")
            for row in result["value"]:
                info = (((row or {}).get("account") or {}).get("data") or {}).get("parsed", {}).get("info", {})
                if not isinstance(info, dict) or not isinstance(info.get("tokenAmount"), dict):
                    raise ValueError("unparsed token balance")
                if info.get("mint") and mint and info["mint"] != mint:
                    raise ValueError("token balance mint mismatch")
                validate_amount(info["tokenAmount"])
        elif method == "getTokenLargestAccounts":
            seen = set()
            for row in result["value"]:
                if not isinstance(row, dict):
                    raise ValueError("invalid largest token account")
                address = row.get("address")
                if not isinstance(address, str) or not address.strip() or address != address.strip():
                    raise ValueError("missing largest token account address")
                if address in seen:
                    raise ValueError("duplicate largest token account")
                seen.add(address)
                validate_amount(row)
    elif method == "getTokenSupply":
        if not isinstance(result, dict) or not isinstance(result.get("value"), dict):
            raise ValueError("missing token supply")
        validate_amount(result["value"])


def validate_amount(value):
    raw = value.get("amount")
    if raw is not None:
        if (not str(raw).isascii() or not str(raw).isdigit()
                or type(value.get("decimals")) is not int or not 0 <= value["decimals"] <= 255):
            raise ValueError("invalid raw token amount")
        return
    amount = value.get("uiAmountString", value.get("uiAmount"))
    try:
        if amount is None or isinstance(amount, bool) or not math.isfinite(float(amount)) or float(amount) < 0:
            raise ValueError("invalid token amount")
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid token amount") from exc
