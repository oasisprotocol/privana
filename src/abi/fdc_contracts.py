"""Minimal ABIs for Flare FDC contracts on Coston2."""

FDC_HUB_ABI = [
    {
        "name": "requestAttestation",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "_data", "type": "bytes"}],
        "outputs": [{"name": "", "type": "bool"}],
    }
]

FDC_FEE_CONFIG_ABI = [
    {
        "name": "getRequestFee",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "_data", "type": "bytes"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]

FDC_VERIFICATION_ABI = [
    {
        "name": "verifyEVMTransaction",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {
                "name": "_proof",
                "type": "tuple",
                "components": [
                    {"name": "merkleProof", "type": "bytes32[]"},
                    {
                        "name": "data",
                        "type": "tuple",
                        "components": [
                            {"name": "attestationType", "type": "bytes32"},
                            {"name": "sourceId", "type": "bytes32"},
                            {"name": "votingRound", "type": "uint64"},
                            {"name": "lowestUsedTimestamp", "type": "uint64"},
                            {
                                "name": "requestBody",
                                "type": "tuple",
                                "components": [
                                    {"name": "transactionHash", "type": "bytes32"},
                                    {"name": "requiredConfirmations", "type": "uint16"},
                                    {"name": "provideInput", "type": "bool"},
                                    {"name": "listEvents", "type": "bool"},
                                    {"name": "logIndices", "type": "uint32[]"},
                                ],
                            },
                            {
                                "name": "responseBody",
                                "type": "tuple",
                                "components": [
                                    {"name": "blockNumber", "type": "uint64"},
                                    {"name": "timestamp", "type": "uint64"},
                                    {"name": "sourceAddress", "type": "address"},
                                    {"name": "isDeployment", "type": "bool"},
                                    {"name": "receivingAddress", "type": "address"},
                                    {"name": "value", "type": "uint256"},
                                    {"name": "input", "type": "bytes"},
                                    {"name": "status", "type": "uint8"},
                                    {
                                        "name": "events",
                                        "type": "tuple[]",
                                        "components": [
                                            {"name": "logIndex", "type": "uint32"},
                                            {"name": "emitterAddress", "type": "address"},
                                            {"name": "topics", "type": "bytes32[]"},
                                            {"name": "data", "type": "bytes"},
                                            {"name": "removed", "type": "bool"},
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            }
        ],
        "outputs": [{"name": "", "type": "bool"}],
    }
]
