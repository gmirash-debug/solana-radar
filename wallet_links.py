"""Shared evidence policy. Infrastructure overlap is not a wallet ownership link."""

SERVICE_KINDS = frozenset({
    "service", "cex", "exchange", "bridge", "router", "relay", "lifi",
    "terminal", "executor", "pool", "burn", "program",
})
PRIVATE_KINDS = frozenset({"private", "wallet", "eoa"})


def source_kind(address, config=None, hint=None):
    config = config or {}
    labels = config.get("wallet_source_labels") or {}
    label = labels.get(address) if isinstance(labels, dict) else None
    if isinstance(label, dict):
        label = label.get("kind")
    excluded = set(config.get("coordinated_activity_infrastructure_addresses") or [])
    excluded.update(config.get("supply_integrity_infrastructure_addresses") or [])
    if address in excluded:
        return "service"
    return str(label or hint or "unknown").strip().lower().replace(".", "")


def normalize_link(group, family, config=None, profiles=None):
    """Old unverified groups stay descriptive; never inherit their old proof flag."""
    group = dict(group)
    members = sorted({str(owner) for owner in group.get("members") or [] if owner})
    key = str(group.get("key") or group.get("source") or group.get("executor") or "")
    kind = source_kind(key, config, group.get("source_kind"))
    if profiles and any(source_kind(key, config, (profiles.get(owner) or {}).get("source_kind")) in SERVICE_KINDS
            for owner in members):
        kind = "service"
    verified = group.get("transfer_verified") is True
    if family == "common_funder" and not verified and profiles:
        verified = bool(members) and all(
            (profiles.get(owner) or {}).get("funding_source") == key
            and (profiles.get(owner) or {}).get("funding_verified") is True
            and float((profiles.get(owner) or {}).get("funding_sol") or 0)
                >= float(((config or {}).get("coordinated_activity") or {}).get("min_funding_native", 0.05))
            and float((profiles.get(owner) or {}).get("funding_sol") or 0)
                >= 0.1 * float((profiles.get(owner) or {}).get("buy_sol") or 0)
            for owner in members
        )
    direct = family == "common_funder" and verified and bool(key) and len(members) >= 2 and kind not in SERVICE_KINDS
    # A shared signer can be a terminal/router even when it has no public label.
    if family == "common_executor":
        direct = kind in PRIVATE_KINDS and group.get("link_verified") is True
    return {**group, "family": family, "key": key, "members": members,
            "wallets": len(members), "source_kind": kind,
            "transfer_verified": verified, "supporting_only": not direct,
            "ownership": "not_established"}


def infrastructure_sources(config):
    config = config or {}
    excluded = set(config.get("coordinated_activity_infrastructure_addresses") or [])
    excluded.update(config.get("supply_integrity_infrastructure_addresses") or [])
    labels = config.get("wallet_source_labels") or {}
    if isinstance(labels, dict):
        excluded.update(address for address in labels if source_kind(address, config) in SERVICE_KINDS)
    return excluded
