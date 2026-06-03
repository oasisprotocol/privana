"""ROFLBridge adapter ABI bound at startup for sanity checks."""

import json
from pathlib import Path
from typing import Any

_path = (
    Path(__file__).parent.parent.parent
    / "solidity"
    / "artifacts"
    / "contracts"
    / "bridge"
    / "ROFLBridge.sol"
    / "ROFLBridge.json"
)

with open(_path) as _f:
    ROFL_BRIDGE_ABI: list[dict[str, Any]] = json.load(_f)["abi"]
