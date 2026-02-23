"""Accounting SIWE auth contract ABI."""

import json
from pathlib import Path

_abi_path = (
    Path(__file__).parent.parent.parent
    / "solidity"
    / "artifacts"
    / "contracts"
    / "auth"
    / "AccountingSiweAuth.sol"
    / "AccountingSiweAuth.json"
)

with open(_abi_path) as f:
    _contract_json = json.load(f)

ACCOUNTING_SIWE_AUTH_ABI = _contract_json["abi"]
