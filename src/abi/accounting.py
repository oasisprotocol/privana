"""Accounting module ABI."""

ACCOUNTING_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "_shoyubashi", "type": "address"}
        ],
        "stateMutability": "nonpayable",
        "type": "constructor",
    },
    {"inputs": [], "name": "AddressMismatch", "type": "error"},
    {"inputs": [], "name": "ChainIdMismatch", "type": "error"},
    {"inputs": [], "name": "DER_Split_Error", "type": "error"},
    {"inputs": [], "name": "ECDSAInvalidSignature", "type": "error"},
    {
        "inputs": [{"internalType": "uint256", "name": "length", "type": "uint256"}],
        "name": "ECDSAInvalidSignatureLength",
        "type": "error",
    },
    {
        "inputs": [{"internalType": "bytes32", "name": "s", "type": "bytes32"}],
        "name": "ECDSAInvalidSignatureS",
        "type": "error",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "chainId", "type": "uint256"}],
        "name": "GasPriceNotSet",
        "type": "error",
    },
    {"inputs": [], "name": "InsufficientBalance", "type": "error"},
    {"inputs": [], "name": "InsufficientLockedAmount", "type": "error"},
    {"inputs": [], "name": "InvalidAmount", "type": "error"},
    {"inputs": [], "name": "WithdrawalAlreadyResolved", "type": "error"},
    {"inputs": [], "name": "WithdrawalTooSoon", "type": "error"},
    {"inputs": [], "name": "InvalidDeposit", "type": "error"},
    {"inputs": [], "name": "InvalidExpiry", "type": "error"},
    {"inputs": [], "name": "InvalidLockIndex", "type": "error"},
    {"inputs": [], "name": "InvalidShortString", "type": "error"},
    {"inputs": [], "name": "InvalidSignature", "type": "error"},
    {"inputs": [], "name": "InvalidTransactionData", "type": "error"},
    {"inputs": [], "name": "LockNotExpired", "type": "error"},
    {
        "inputs": [{"internalType": "address", "name": "owner", "type": "address"}],
        "name": "OwnableInvalidOwner",
        "type": "error",
    },
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "OwnableUnauthorizedAccount",
        "type": "error",
    },
    {
        "inputs": [{"internalType": "string", "name": "str", "type": "string"}],
        "name": "StringTooLong",
        "type": "error",
    },
    {"inputs": [], "name": "TooManyActiveLocks", "type": "error"},
    {"inputs": [], "name": "UnsupportedTokenType", "type": "error"},
    {"inputs": [], "name": "UsedSignature", "type": "error"},
    {"inputs": [], "name": "expmod_Error", "type": "error"},
    {"inputs": [], "name": "k256Decompress_Invalid_Length_Error", "type": "error"},
    {"inputs": [], "name": "k256DeriveY_Invalid_Prefix_Error", "type": "error"},
    {"inputs": [], "name": "recoverV_Error", "type": "error"},
    {
        "anonymous": False,
        "inputs": [
            {
                "indexed": True,
                "internalType": "address",
                "name": "fromAddress",
                "type": "address",
            },
            {
                "indexed": True,
                "internalType": "address",
                "name": "toAddress",
                "type": "address",
            },
            {
                "indexed": True,
                "internalType": "bytes32",
                "name": "tokenId",
                "type": "bytes32",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "amount",
                "type": "uint256",
            },
        ],
        "name": "BalanceTransferred",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {
                "indexed": True,
                "internalType": "address",
                "name": "userAddress",
                "type": "address",
            },
            {
                "indexed": True,
                "internalType": "bytes32",
                "name": "tokenId",
                "type": "bytes32",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "amount",
                "type": "uint256",
            },
        ],
        "name": "Deposit",
        "type": "event",
    },
    {"anonymous": False, "inputs": [], "name": "EIP712DomainChanged", "type": "event"},
    {
        "anonymous": False,
        "inputs": [
            {
                "indexed": True,
                "internalType": "uint256",
                "name": "chainId",
                "type": "uint256",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "gasPrice",
                "type": "uint256",
            },
        ],
        "name": "GasPriceSet",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {
                "indexed": True,
                "internalType": "address",
                "name": "userAddress",
                "type": "address",
            },
            {
                "indexed": True,
                "internalType": "address",
                "name": "serviceAddress",
                "type": "address",
            },
            {
                "indexed": True,
                "internalType": "bytes32",
                "name": "tokenId",
                "type": "bytes32",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "amount",
                "type": "uint256",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "expiry",
                "type": "uint256",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "lockIndex",
                "type": "uint256",
            },
        ],
        "name": "LockCreated",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {
                "indexed": True,
                "internalType": "address",
                "name": "userAddress",
                "type": "address",
            },
            {
                "indexed": True,
                "internalType": "address",
                "name": "serviceAddress",
                "type": "address",
            },
            {
                "indexed": True,
                "internalType": "bytes32",
                "name": "tokenId",
                "type": "bytes32",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "amount",
                "type": "uint256",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "newExpiry",
                "type": "uint256",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "lockIndex",
                "type": "uint256",
            },
        ],
        "name": "LockFundsAdded",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {
                "indexed": True,
                "internalType": "address",
                "name": "userAddress",
                "type": "address",
            },
            {
                "indexed": True,
                "internalType": "bytes32",
                "name": "tokenId",
                "type": "bytes32",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "amount",
                "type": "uint256",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "lockIndex",
                "type": "uint256",
            },
        ],
        "name": "LockUnlocked",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {
                "indexed": True,
                "internalType": "address",
                "name": "userAddress",
                "type": "address",
            },
            {
                "indexed": True,
                "internalType": "address",
                "name": "serviceAddress",
                "type": "address",
            },
            {
                "indexed": True,
                "internalType": "address",
                "name": "toAddress",
                "type": "address",
            },
            {
                "indexed": False,
                "internalType": "bytes32",
                "name": "tokenId",
                "type": "bytes32",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "amount",
                "type": "uint256",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "lockIndex",
                "type": "uint256",
            },
        ],
        "name": "LockedFundsTransferred",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {
                "indexed": True,
                "internalType": "address",
                "name": "previousOwner",
                "type": "address",
            },
            {
                "indexed": True,
                "internalType": "address",
                "name": "newOwner",
                "type": "address",
            },
        ],
        "name": "OwnershipTransferred",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {
                "indexed": True,
                "internalType": "bytes32",
                "name": "tokenId",
                "type": "bytes32",
            },
            {
                "indexed": False,
                "internalType": "enum TokenType",
                "name": "tokenType",
                "type": "uint8",
            },
        ],
        "name": "TokenRegistered",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {
                "indexed": True,
                "internalType": "address",
                "name": "userAddress",
                "type": "address",
            },
            {
                "indexed": True,
                "internalType": "bytes32",
                "name": "tokenId",
                "type": "bytes32",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "amount",
                "type": "uint256",
            },
            {
                "indexed": False,
                "internalType": "uint256",
                "name": "chainId",
                "type": "uint256",
            },
        ],
        "name": "Withdrawal",
        "type": "event",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "user", "type": "address"},
            {"internalType": "bytes32", "name": "tokenId", "type": "bytes32"},
        ],
        "name": "balances",
        "outputs": [{"internalType": "uint256", "name": "balance", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "userAddress", "type": "address"},
            {"internalType": "address", "name": "serviceAddress", "type": "address"},
            {"internalType": "bytes32", "name": "tokenId", "type": "bytes32"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "uint256", "name": "expiry", "type": "uint256"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"},
        ],
        "name": "createLock",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "userAddress", "type": "address"},
            {"internalType": "uint256", "name": "lockIndex", "type": "uint256"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "uint256", "name": "newExpiry", "type": "uint256"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"},
        ],
        "name": "addToLock",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "userAddress", "type": "address"},
            {"internalType": "bytes32", "name": "tokenId", "type": "bytes32"},
            {
                "components": [
                    {
                        "internalType": "bytes",
                        "name": "rlpBlockHeader",
                        "type": "bytes",
                    },
                    {
                        "internalType": "bytes",
                        "name": "transactionIndexRlp",
                        "type": "bytes",
                    },
                    {
                        "internalType": "bytes",
                        "name": "transactionProofStack",
                        "type": "bytes",
                    },
                ],
                "internalType": "struct EVMTransactionProof",
                "name": "txProof",
                "type": "tuple",
            },
            {
                "components": [
                    {
                        "internalType": "bytes",
                        "name": "receiptIndexRlp",
                        "type": "bytes",
                    },
                    {
                        "internalType": "bytes",
                        "name": "receiptProofStack",
                        "type": "bytes",
                    },
                ],
                "internalType": "struct EVMReceiptProof",
                "name": "receiptProof",
                "type": "tuple",
            },
        ],
        "name": "creditEVMDeposit",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "userAddress", "type": "address"},
            {"internalType": "bytes32", "name": "tokenId", "type": "bytes32"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "creditEVMDepositMock",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "bytes", "name": "data", "type": "bytes"}],
        "name": "decodeEVMErc20TokenData",
        "outputs": [
            {"internalType": "uint256", "name": "chainId", "type": "uint256"},
            {"internalType": "address", "name": "tokenAddress", "type": "address"},
        ],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "bytes", "name": "data", "type": "bytes"}],
        "name": "decodeEVMNativeTokenData",
        "outputs": [{"internalType": "uint256", "name": "chainId", "type": "uint256"}],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "eip712Domain",
        "outputs": [
            {"internalType": "bytes1", "name": "fields", "type": "bytes1"},
            {"internalType": "string", "name": "name", "type": "string"},
            {"internalType": "string", "name": "version", "type": "string"},
            {"internalType": "uint256", "name": "chainId", "type": "uint256"},
            {"internalType": "address", "name": "verifyingContract", "type": "address"},
            {"internalType": "bytes32", "name": "salt", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "extensions", "type": "uint256[]"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "chainId", "type": "uint256"},
            {"internalType": "address", "name": "tokenAddress", "type": "address"},
        ],
        "name": "encodeEVMErc20TokenData",
        "outputs": [{"internalType": "bytes", "name": "data", "type": "bytes"}],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "chainId", "type": "uint256"}],
        "name": "encodeEVMNativeTokenData",
        "outputs": [{"internalType": "bytes", "name": "data", "type": "bytes"}],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "evmAddress",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "bytes32", "name": "rootHash", "type": "bytes32"},
            {"internalType": "bytes", "name": "mptPath", "type": "bytes"},
            {"internalType": "bytes", "name": "rlpStack", "type": "bytes"},
        ],
        "name": "exposedValidateMPTProof",
        "outputs": [{"internalType": "bytes", "name": "value", "type": "bytes"}],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "gasLimitERC20",
        "outputs": [{"internalType": "uint64", "name": "", "type": "uint64"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "gasLimitNative",
        "outputs": [{"internalType": "uint64", "name": "", "type": "uint64"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "chainId", "type": "uint256"}],
        "name": "gasPrices",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "user", "type": "address"},
            {"internalType": "bytes32[]", "name": "tokenIds", "type": "bytes32[]"},
        ],
        "name": "getBalances",
        "outputs": [{"internalType": "uint256[]", "name": "", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "bytes", "name": "rlpBlockHeader", "type": "bytes"}
        ],
        "name": "getBlockNumber",
        "outputs": [
            {"internalType": "uint256", "name": "blockNumber", "type": "uint256"}
        ],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getExpiredLocks",
        "outputs": [
            {
                "components": [
                    {"internalType": "address", "name": "serviceId", "type": "address"},
                    {"internalType": "bytes32", "name": "tokenId", "type": "bytes32"},
                    {"internalType": "uint256", "name": "amount", "type": "uint256"},
                    {"internalType": "uint256", "name": "expiry", "type": "uint256"},
                ],
                "internalType": "struct FundLock[]",
                "name": "expiredLocks",
                "type": "tuple[]",
            },
            {"internalType": "uint256[]", "name": "lockIndices", "type": "uint256[]"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {
                        "internalType": "enum TokenType",
                        "name": "tokenType",
                        "type": "uint8",
                    },
                    {"internalType": "bytes", "name": "data", "type": "bytes"},
                ],
                "internalType": "struct TokenInfo",
                "name": "info",
                "type": "tuple",
            }
        ],
        "name": "getTokenId",
        "outputs": [{"internalType": "bytes32", "name": "", "type": "bytes32"}],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "user", "type": "address"},
            {"internalType": "bytes32", "name": "tokenId", "type": "bytes32"},
        ],
        "name": "getTotalLockedBalance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserLocks",
        "outputs": [
            {
                "components": [
                    {"internalType": "address", "name": "serviceId", "type": "address"},
                    {"internalType": "bytes32", "name": "tokenId", "type": "bytes32"},
                    {"internalType": "uint256", "name": "amount", "type": "uint256"},
                    {"internalType": "uint256", "name": "expiry", "type": "uint256"},
                ],
                "internalType": "struct FundLock[]",
                "name": "",
                "type": "tuple[]",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "chainId", "type": "uint256"}],
        "name": "nonces",
        "outputs": [{"internalType": "uint64", "name": "", "type": "uint64"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "owner",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "renounceOwnership",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "chainId", "type": "uint256"},
            {"internalType": "uint256", "name": "gasPrice", "type": "uint256"},
        ],
        "name": "setGasPrice",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {
                        "internalType": "enum TokenType",
                        "name": "tokenType",
                        "type": "uint8",
                    },
                    {"internalType": "bytes", "name": "data", "type": "bytes"},
                ],
                "internalType": "struct TokenInfo",
                "name": "info",
                "type": "tuple",
            }
        ],
        "name": "setTokenInfo",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "shoyuBashi",
        "outputs": [
            {"internalType": "contract IShoyuBashi", "name": "", "type": "address"}
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "bytes32", "name": "tokenId", "type": "bytes32"}],
        "name": "tokens",
        "outputs": [
            {"internalType": "enum TokenType", "name": "tokenType", "type": "uint8"},
            {"internalType": "bytes", "name": "data", "type": "bytes"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "userAddress", "type": "address"},
            {"internalType": "address", "name": "toAddress", "type": "address"},
            {"internalType": "bytes32", "name": "tokenId", "type": "bytes32"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"},
        ],
        "name": "transferBalance",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "userAddress", "type": "address"},
            {"internalType": "address", "name": "toAddress", "type": "address"},
            {"internalType": "uint256", "name": "lockIndex", "type": "uint256"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"},
        ],
        "name": "transferFromLock",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "newOwner", "type": "address"}],
        "name": "transferOwnership",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "userAddress", "type": "address"}
        ],
        "name": "unlockAllExpiredLocks",
        "outputs": [
            {"internalType": "uint256", "name": "unlockedCount", "type": "uint256"}
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "userAddress", "type": "address"},
            {"internalType": "uint256", "name": "lockIndex", "type": "uint256"},
        ],
        "name": "unlockSingleLock",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "bytes", "name": "signature", "type": "bytes"}],
        "name": "usedSignatures",
        "outputs": [{"internalType": "bool", "name": "used", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "bytes32", "name": "rootHash", "type": "bytes32"},
            {"internalType": "bytes", "name": "mptKey", "type": "bytes"},
            {
                "components": [
                    {"internalType": "uint256", "name": "len", "type": "uint256"},
                    {"internalType": "uint256", "name": "memPtr", "type": "uint256"},
                ],
                "internalType": "struct RLPReader.RLPItem[]",
                "name": "stack",
                "type": "tuple[]",
            },
        ],
        "name": "validateMPTProof",
        "outputs": [{"internalType": "bytes", "name": "value", "type": "bytes"}],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {
                        "internalType": "bytes",
                        "name": "rlpBlockHeader",
                        "type": "bytes",
                    },
                    {"internalType": "address", "name": "addr", "type": "address"},
                    {
                        "internalType": "uint256",
                        "name": "storageSlot",
                        "type": "uint256",
                    },
                    {
                        "internalType": "bytes",
                        "name": "accountProofStack",
                        "type": "bytes",
                    },
                    {
                        "internalType": "bytes",
                        "name": "storageProofStack",
                        "type": "bytes",
                    },
                ],
                "internalType": "struct StorageProof",
                "name": "storageProof",
                "type": "tuple",
            }
        ],
        "name": "validateStorageProof",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {
                        "internalType": "bytes",
                        "name": "rlpBlockHeader",
                        "type": "bytes",
                    },
                    {
                        "internalType": "bytes",
                        "name": "transactionIndexRlp",
                        "type": "bytes",
                    },
                    {
                        "internalType": "bytes",
                        "name": "transactionProofStack",
                        "type": "bytes",
                    },
                ],
                "internalType": "struct EVMTransactionProof",
                "name": "txProof",
                "type": "tuple",
            }
        ],
        "name": "validateTxProof",
        "outputs": [{"internalType": "bytes", "name": "", "type": "bytes"}],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "bytes32", "name": "expectedBlockHash", "type": "bytes32"},
            {"internalType": "uint256", "name": "blockNumber", "type": "uint256"},
            {"internalType": "uint256", "name": "chainId", "type": "uint256"},
        ],
        "name": "verifyBlockHash",
        "outputs": [],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "userAddress", "type": "address"},
            {"internalType": "address", "name": "serviceAddress", "type": "address"},
            {"internalType": "bytes32", "name": "tokenId", "type": "bytes32"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "uint256", "name": "expiry", "type": "uint256"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"},
        ],
        "name": "verifyLockSignature",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "serviceAddress", "type": "address"},
            {"internalType": "address", "name": "userAddress", "type": "address"},
            {"internalType": "address", "name": "toAddress", "type": "address"},
            {"internalType": "uint256", "name": "lockIndex", "type": "uint256"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"},
        ],
        "name": "verifyTransferLockedSignature",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "userAddress", "type": "address"},
            {"internalType": "address", "name": "toAddress", "type": "address"},
            {"internalType": "bytes32", "name": "tokenId", "type": "bytes32"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"},
        ],
        "name": "verifyTransferSignature",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "userAddress", "type": "address"},
            {"internalType": "bytes32", "name": "tokenId", "type": "bytes32"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"},
        ],
        "name": "verifyWithdrawSignature",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "userAddress", "type": "address"},
            {"internalType": "bytes32", "name": "tokenId", "type": "bytes32"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"},
        ],
        "name": "requestWithdrawal",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "index", "type": "uint256"}],
        "name": "resolveWithdrawal",
        "outputs": [{"internalType": "bytes", "name": "signedTx", "type": "bytes"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "withdrawals",
        "outputs": [
            {"internalType": "address", "name": "userAddress", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "uint256", "name": "blockNumber", "type": "uint256"},
            {"internalType": "bytes32", "name": "tokenId", "type": "bytes32"},
            {"internalType": "bool", "name": "resolved", "type": "bool"},
            {"internalType": "bytes", "name": "txIdentifier", "type": "bytes"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]
