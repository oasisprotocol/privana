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

/// @notice Chain family for deposit-address derivation.
/// @dev Coarser than chainId: one deposit address serves all chains within a family.
enum ChainType {
    EVM
}

/// @notice Raised when a caller dispatches on an unknown TokenType variant,
///         or references a tokenId that has not been registered via registerToken.
error UnsupportedTokenType();

/// @notice User-visible history entry kinds.
/// @dev Append-only: `HistoryKind` values are persisted inside `HistoryEntry`
///      storage and decoded off-chain by ordinal, so existing members must
///      never be reordered or removed.
enum HistoryKind {
    Deposit,
    Withdraw,
    CreateLock,
    TransferFromLock,
    TransferBalance,
    ModifyLock,
    UnlockLock
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
    uint256 lockId;
    address serviceId;
    bytes32 tokenId;
    uint256 amount;
    uint256 expiry;
}

struct HistoryEntry {
    HistoryKind kind;
    uint64 timestamp;
    bytes payload;
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
