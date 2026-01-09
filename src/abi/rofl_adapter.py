"""ROFL Adapter ABI for block hash storage events."""

ROFL_ADAPTER_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "uint256", "name": "id", "type": "uint256"},
            {"indexed": True, "internalType": "bytes32", "name": "hash", "type": "bytes32"},
        ],
        "name": "HashStored",
        "type": "event",
    }
]
