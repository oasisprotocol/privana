"""Accounting module ABI.

``ACCOUNTING_ABI`` is the ``Accounting`` contract ABI, bound to the proxy.
Bridge errors from the internal ``BridgeLib`` library are present in this ABI.
"""

import json
from pathlib import Path
from typing import Any

from web3 import Web3

_artifacts = Path(__file__).parent.parent.parent / "solidity" / "artifacts" / "contracts"
_accounting_path = _artifacts / "Accounting.sol" / "Accounting.json"


def _load_abi(path: Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return json.load(f)["abi"]


ACCOUNTING_ABI = _load_abi(_accounting_path)


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


# Pre-computed error selectors from the Accounting ABI.
ERROR_SELECTORS = _build_error_selectors(ACCOUNTING_ABI)


def get_error_name(selector: bytes) -> str | None:
    """Get error name from 4-byte selector, or None if unknown."""
    return ERROR_SELECTORS.get(selector)
