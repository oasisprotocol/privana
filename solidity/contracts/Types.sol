// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

enum Chain {
    Ethereum,
    Arbitrum,
    Optimism,
    Solana,
    Sui
}

enum TokenType {
    NativeEVM,
    ERC20
}

struct TokenInfo {
    TokenType tokenType;
    bytes data; // e.g. chainId and contract address for ERC20
}

struct UserInfo {
    // We can add more fields here, like solana deposit address etc
    FundLock[] activeLocks; // Active fund locks for this user
}

struct FundLock {
    address serviceId;
    bytes32 tokenId;
    uint256 amount;
    uint256 expiry;
}

struct EVMKeypair {
    address addr;
    bytes32 secret;
}

struct TransactionProof {
    bytes rlpBlockHeader;
    bytes transactionIndexRlp;
    bytes transactionProofStack;
}
