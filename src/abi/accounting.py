"""Accounting module ABI."""

import json
from pathlib import Path

from web3 import Web3

_abi_path = (
    Path(__file__).parent.parent.parent
    / "solidity"
    / "artifacts"
    / "contracts"
    / "Accounting.sol"
    / "Accounting.json"
)

with open(_abi_path) as f:
    _contract_json = json.load(f)

ACCOUNTING_ABI = _contract_json["abi"]


def _build_error_selectors(abi: list) -> dict[bytes, str]:
    """Build a mapping of error selectors (bytes) to names from the ABI."""
    selectors: dict[bytes, str] = {}
    for item in abi:
        if item.get("type") == "error":
            name = item["name"]
            inputs = item.get("inputs", [])
            # Build signature: ErrorName(type1,type2,...)
            types = ",".join(inp["type"] for inp in inputs)
            signature = f"{name}({types})"
            # Compute selector (first 4 bytes of keccak256)
            selector = bytes(Web3.keccak(text=signature)[:4])
            selectors[selector] = name
    return selectors


# Pre-computed error selectors from the ABI
ERROR_SELECTORS = _build_error_selectors(ACCOUNTING_ABI)


def get_error_name(selector: bytes) -> str | None:
    """Get error name from 4-byte selector, or None if unknown."""
    return ERROR_SELECTORS.get(selector)
