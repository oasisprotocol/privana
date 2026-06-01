"""Accounting module ABI.

Bridge and lock selectors live in separate ``BridgeModule`` and ``LockModule``
contracts that are delegate-called from the Accounting proxy. The Python side
still binds to the proxy address; ``ACCOUNTING_ABI`` is the merged union of the
``Accounting``, ``BridgeModule``, and ``LockModule`` artifact ABIs so callers
can encode any surface against a single contract handle.
"""

import json
from pathlib import Path
from typing import Any

from web3 import Web3

_artifacts = Path(__file__).parent.parent.parent / "solidity" / "artifacts" / "contracts"
_accounting_path = _artifacts / "Accounting.sol" / "Accounting.json"
_bridge_module_path = _artifacts / "BridgeModule.sol" / "BridgeModule.json"
_lock_module_path = _artifacts / "LockModule.sol" / "LockModule.json"
_bridge_lib_path = _artifacts / "BridgeLib.sol" / "BridgeLib.json"


def _canonical_key(item: dict[str, Any]) -> tuple | None:
    """Canonical key for ABI fragment dedup.

    Functions and errors key on (type, name, input-types) — the selector.
    Events key on (type, name, (input-type, indexed-flag)*) — the topic plus
    indexed shape, since divergent ``indexed`` flags would yield divergent
    decoders even for matching topic hashes.
    Constructors / fallback / receive return ``None`` and are skipped from
    the dedup map (only one of each can exist anyway).
    """
    kind = item.get("type")
    inputs = item.get("inputs") or []
    if kind in ("function", "error"):
        return (kind, item["name"], tuple(i["type"] for i in inputs))
    if kind == "event":
        return (
            "event",
            item["name"],
            tuple((i["type"], bool(i.get("indexed"))) for i in inputs),
        )
    return None


def _merge_abis(*abis: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge ABI fragment lists, throwing on canonical-key conflicts.

    Identical duplicates are silently kept once. Divergent declarations of the
    same selector / topic raise ``RuntimeError`` so configuration drift
    between the two artifacts surfaces loudly at import time instead of
    producing ambiguous decoders downstream.
    """
    out: dict[tuple, dict[str, Any]] = {}
    keyless: list[dict[str, Any]] = []
    for abi in abis:
        for item in abi:
            key = _canonical_key(item)
            if key is None:
                keyless.append(item)
                continue
            existing = out.get(key)
            if existing is None:
                out[key] = item
                continue
            if existing != item:
                raise RuntimeError(
                    f"ABI fragment conflict at {key}:\n  first:  {existing}\n  second: {item}"
                )
    return list(out.values()) + keyless


def _load_abi(path: Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return json.load(f)["abi"]


_accounting_abi = _load_abi(_accounting_path)
_bridge_module_abi = _load_abi(_bridge_module_path)
_lock_module_abi = _load_abi(_lock_module_path)
# BridgeLib's errors (InvalidRouteAddress, RoflBridgeNotSet, GasBudgetExceeded,
# ...) bubble up through `delegatecall` from BridgeModule into the proxy, so
# their selectors must decode here too.
_bridge_lib_abi = _load_abi(_bridge_lib_path)

ACCOUNTING_ABI = _merge_abis(_accounting_abi, _bridge_module_abi, _lock_module_abi, _bridge_lib_abi)


def _build_error_selectors(abi: list) -> dict[bytes, str]:
    """Build a mapping of error selectors (bytes) to names from the ABI."""
    selectors: dict[bytes, str] = {}
    for item in abi:
        if item.get("type") == "error":
            name = item["name"]
            inputs = item.get("inputs", [])
            types = ",".join(inp["type"] for inp in inputs)
            signature = f"{name}({types})"
            selector = bytes(Web3.keccak(text=signature)[:4])
            selectors[selector] = name
    return selectors


# Pre-computed error selectors from the merged ABI.
ERROR_SELECTORS = _build_error_selectors(ACCOUNTING_ABI)


def get_error_name(selector: bytes) -> str | None:
    """Get error name from 4-byte selector, or None if unknown."""
    return ERROR_SELECTORS.get(selector)
