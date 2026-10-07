"""Fence pre-reset writers without changing quota or archive identities."""
import os
import re

DEFAULT_STORAGE_EPOCH = "20261007-clean-v1"


def storage_epoch(config=None):
    value = os.environ.get("RADAR_STORAGE_EPOCH") or (config or {}).get("storage_epoch") or DEFAULT_STORAGE_EPOCH
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value):
        raise ValueError("Invalid storage epoch")
    return value
