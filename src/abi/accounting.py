"""Accounting module ABI."""

import json
from pathlib import Path

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
