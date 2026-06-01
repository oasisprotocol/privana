// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

enum Chain {
    Ethereum,
    Arbitrum,
    Optimism,
    Solana,
    Sui
}

/// @dev Append-only. Existing variants encode persisted balances via
///      keccak256(abi.encode(tokenType, data)); reordering or inserting
///      orphans every previously-computed tokenId.
enum TokenType {
    NativeEVM,
    ERC20,
    BridgeAsset
}

/// @notice Chain family for deposit-address derivation.
/// @dev Coarser than chainId: one deposit address serves all chains within a family.
enum ChainType {
    EVM
}

/// @notice Raised when a caller dispatches on an unknown TokenType variant,
///         or references a tokenId that has not been registered via registerToken.
error UnsupportedTokenType();

enum HistoryKind {
    Deposit,
    Withdraw,
    CreateLock,
    TransferFromLock,
    TransferBalance,
    ModifyLock,
    UnlockLock
}

/// @notice Generic zero-address rejection used across deposits, withdrawals, locks, etc.
error InvalidAddress();

/// @notice Generic zero-amount or out-of-bounds amount rejection used across deposits,
///         withdrawals, locks, etc.
error InvalidAmount();

/// @notice Raised when a chain that requires `gasPrices[chainId]` is referenced
///         while that map entry is unset.
error GasPriceNotSet(uint256 chainId);

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
