"""ROFL Adapter ABI for block hash queries."""

ROFL_ADAPTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "domain", "type": "uint256"},
            {"internalType": "uint256", "name": "id", "type": "uint256"},
        ],
        "name": "getHash",
        "outputs": [{"internalType": "bytes32", "name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    }
]
