"""xERC20-based XRose ABI bound at startup for sanity checks."""

import json
from pathlib import Path
from typing import Any

_path = (
    Path(__file__).parent.parent.parent
    / "solidity"
    / "artifacts"
    / "contracts"
    / "bridge"
    / "XRose.sol"
    / "XRose.json"
)

with open(_path) as _f:
    XROSE_ABI: list[dict[str, Any]] = json.load(_f)["abi"]
