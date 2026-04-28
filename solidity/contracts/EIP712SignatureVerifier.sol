// SPDX-License-Identifier: MIT
/* solhint-disable no-console */
pragma solidity ^0.8.20;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {EIP712Upgradeable} from "@openzeppelin/contracts-upgradeable/utils/cryptography/EIP712Upgradeable.sol";

/**
 * @title EIP712SignatureVerifier
 * @notice Provides EIP-712 typed data signature verification for accounting operations.
 *
 * This contract implements signature verification for user-initiated operations in the
 * accounting module. It uses EIP-712 typed data signatures to ensure that users
 * explicitly authorize specific operations with their funds.
 *
 * Features:
 * - EIP-712 compliant typed data signatures for better UX and security
 * - Nonce-based replay protection per user/service per operation type
 * - Support for withdraw, lock, transfer, and locked transfer operations
 * - Domain separation with name "AccountingModule" and version "1"
 *
 * The contract prevents signature replay attacks via per-operation nonces and ensures
 * that only the rightful user can authorize operations on their funds.
 */
abstract contract EIP712SignatureVerifier is Initializable, EIP712Upgradeable {
    /**
     * @notice Initializes the EIP-712 domain separator for typed data signatures.
     * @dev This replaces the constructor for upgradeable contracts.
     */
    function __EIP712SignatureVerifier_init() internal onlyInitializing {
        __EIP712_init("AccountingModule", "1");
    }

    /// @notice Mapping to track withdrawal nonces per user for replay protection
    mapping(address user => uint256 nonce) public withdrawalNonces;

    /// @notice Mapping to track transfer nonces per user for replay protection
    mapping(address user => uint256 nonce) public transferNonces;

    /// @notice Mapping to track createLock nonces per user for replay protection
    mapping(address user => uint256 nonce) public createLockNonces;

    /// @notice Mapping to track modifyLock nonces per user for replay protection
    mapping(address user => uint256 nonce) public modifyLockNonces;

    /// @notice Mapping to track transferLocked nonces per service for replay protection
    mapping(address service => uint256 nonce) public transferLockedNonces;

    /// @notice Mapping to track withdrawFromLock nonces per service
    mapping(address service => uint256 nonce) public withdrawFromLockNonces;

    /// @notice Thrown when signature recovery fails or signer doesn't match expected address
    error InvalidSignature();
    /// @notice Thrown when the provided nonce doesn't match the expected nonce
    error InvalidNonce();

    /// @notice EIP-712 type hash for withdraw operations
    bytes32 private constant WITHDRAW_TYPEHASH =
        keccak256(
            "Withdraw(address userAddress,bytes32 tokenId,uint256 amount,uint256 nonce)"
        );

    /// @notice EIP-712 type hash for lock operations
    bytes32 private constant LOCK_TYPEHASH =
        keccak256(
            "Lock(address userAddress,address serviceAddress,bytes32 tokenId,uint256 amount,uint256 expiry,uint256 nonce)"
        );

    /// @notice EIP-712 type hash for transfer operations
    bytes32 private constant TRANSFER_TYPEHASH =
        keccak256(
            "Transfer(address userAddress,address toAddress,bytes32 tokenId,uint256 amount,uint256 nonce)"
        );

    /// @notice EIP-712 type hash for locked fund transfer operations
    bytes32 private constant TRANSFER_LOCKED_TYPEHASH =
        keccak256(
            "TransferLocked(address userAddress,address toAddress,uint256 lockId,uint256 amount,uint256 nonce,address serviceAddress)"
        );

    /// @notice EIP-712 type hash for modifying an existing lock (add funds and/or extend expiry)
    bytes32 private constant MODIFY_LOCK_TYPEHASH =
        keccak256(
            "ModifyLock(address userAddress,uint256 lockId,uint256 amount,uint256 newExpiry,uint256 nonce)"
        );

    /// @notice EIP-712 type hash for withdrawing directly from a lock to an external address
    bytes32 private constant WITHDRAW_FROM_LOCK_TYPEHASH =
        keccak256(
            "WithdrawFromLock(address userAddress,address toAddress,uint256 lockId,uint256 amount,uint256 nonce)"
        );


    /**
     * @notice Verifies a user's EIP-712 signature for withdrawing funds.
     * @dev Internal function to prevent front-running attacks where an attacker
     *      could call this directly to consume the nonce before requestWithdrawal.
     *
     * @param userAddress The address of the user requesting the withdrawal
     * @param tokenId The identifier of the token to withdraw
     * @param amount The amount of tokens to withdraw
     * @param nonce The nonce for replay protection (must match user's current nonce)
     * @param signature The EIP-712 signature authorizing the withdrawal
     */
    function verifyWithdrawSignature(
        address userAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) internal {
        if (nonce != withdrawalNonces[userAddress]) {
            revert InvalidNonce();
        }

        bytes32 structHash = keccak256(
            abi.encode(WITHDRAW_TYPEHASH, userAddress, tokenId, amount, nonce)
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(digest, signature);
        if (signer != userAddress) {
            revert InvalidSignature();
        }

        withdrawalNonces[userAddress]++;
    }

    /**
     * @notice Verifies a user's EIP-712 signature for locking funds to a service.
     *
     * @param userAddress The address of the user whose funds are being locked
     * @param serviceAddress The address of the service that will have access to the locked funds
     * @param tokenId The identifier of the token to lock
     * @param amount The amount of tokens to lock
     * @param expiry The timestamp when the lock expires
     * @param nonce The nonce for replay protection (must match user's current createLockNonces)
     * @param signature The EIP-712 signature authorizing the lock
     */
    function verifyLockSignature(
        address userAddress,
        address serviceAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 expiry,
        uint256 nonce,
        bytes calldata signature
    ) internal {
        if (nonce != createLockNonces[userAddress]) {
            revert InvalidNonce();
        }

        bytes32 structHash = keccak256(
            abi.encode(
                LOCK_TYPEHASH,
                userAddress,
                serviceAddress,
                tokenId,
                amount,
                expiry,
                nonce
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(digest, signature);
        if (signer != userAddress) {
            revert InvalidSignature();
        }

        createLockNonces[userAddress]++;
    }

    /**
     * @notice Verifies a user's EIP-712 signature for transferring funds to another address.
     * @dev Internal function to prevent front-running attacks where an attacker
     *      could call this directly to consume the nonce before transferBalance.
     *
     * @param userAddress The address of the user initiating the transfer (sender)
     * @param toAddress The address receiving the funds
     * @param tokenId The identifier of the token to transfer
     * @param amount The amount of tokens to transfer
     * @param nonce The nonce for replay protection (must match user's current transfer nonce)
     * @param signature The EIP-712 signature authorizing the transfer
     */
    function verifyTransferSignature(
        address userAddress,
        address toAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) internal {
        if (nonce != transferNonces[userAddress]) {
            revert InvalidNonce();
        }

        bytes32 structHash = keccak256(
            abi.encode(
                TRANSFER_TYPEHASH,
                userAddress,
                toAddress,
                tokenId,
                amount,
                nonce
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(digest, signature);
        if (signer != userAddress) {
            revert InvalidSignature();
        }

        transferNonces[userAddress]++;
    }

    /**
     * @notice Verifies a service's EIP-712 signature for transferring locked funds.
     * @dev Internal function to prevent front-running attacks where an attacker
     *      could call this directly to consume the nonce before transferFromLock.
     *
     * @param serviceAddress The address of the service authorized to transfer the locked funds
     * @param userAddress The address of the original user who locked the funds
     * @param toAddress The address receiving the transferred locked funds
     * @param lockId The unique identifier of the lock being transferred from
     * @param amount The amount of locked tokens to transfer
     * @param nonce The nonce for replay protection (must match service's current transferLockedNonces)
     * @param signature The EIP-712 signature from the service authorizing the transfer
     */
    function verifyTransferLockedSignature(
        address serviceAddress,
        address userAddress,
        address toAddress,
        uint256 lockId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) internal {
        if (nonce != transferLockedNonces[serviceAddress]) {
            revert InvalidNonce();
        }

        bytes32 structHash = keccak256(
            abi.encode(
                TRANSFER_LOCKED_TYPEHASH,
                userAddress,
                toAddress,
                lockId,
                amount,
                nonce,
                serviceAddress
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(digest, signature);
        if (signer != serviceAddress) {
            revert InvalidSignature();
        }

        transferLockedNonces[serviceAddress]++;
    }

    function verifyWithdrawFromLockSignature(
        address serviceAddress,
        address userAddress,
        address toAddress,
        uint256 lockId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) internal {
        if (nonce != withdrawFromLockNonces[serviceAddress]) {
            revert InvalidNonce();
        }

        bytes32 structHash = keccak256(
            abi.encode(
                WITHDRAW_FROM_LOCK_TYPEHASH,
                userAddress,
                toAddress,
                lockId,
                amount,
                nonce
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(digest, signature);
        if (signer != serviceAddress) {
            revert InvalidSignature();
        }
        withdrawFromLockNonces[serviceAddress]++;
    }

    /**
     * @notice Verifies a user's EIP-712 signature for modifying an existing lock.
     * @dev Internal function to prevent front-running attacks.
     *
     * @param userAddress The address of the user modifying the lock
     * @param lockId The unique identifier of the lock to modify
     * @param amount Additional funds to add to the lock (0 if only extending expiry)
     * @param newExpiry The new expiry timestamp for the lock
     * @param nonce The nonce for replay protection (must match user's current modifyLockNonces)
     * @param signature The EIP-712 signature authorizing the modification
     */
    function verifyModifyLockSignature(
        address userAddress,
        uint256 lockId,
        uint256 amount,
        uint256 newExpiry,
        uint256 nonce,
        bytes calldata signature
    ) internal {
        if (nonce != modifyLockNonces[userAddress]) {
            revert InvalidNonce();
        }

        bytes32 structHash = keccak256(
            abi.encode(
                MODIFY_LOCK_TYPEHASH,
                userAddress,
                lockId,
                amount,
                newExpiry,
                nonce
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(digest, signature);
        if (signer != userAddress) {
            revert InvalidSignature();
        }

        modifyLockNonces[userAddress]++;
    }

    /**
     * @dev Reserved storage gap for future upgrades.
     * This allows adding new state variables without shifting storage layout.
     */
    uint256[44] private __gap;
}
