// SPDX-License-Identifier: MIT
/* solhint-disable no-console */
pragma solidity ^0.8.20;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";

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
 * - Signature replay protection to prevent duplicate operations
 * - Support for withdraw, lock, transfer, and locked transfer operations
 * - Domain separation with name "AccountingModule" and version "1"
 *
 * The contract prevents signature replay attacks by tracking used signatures and ensures
 * that only the rightful user can authorize operations on their funds.
 */
contract EIP712SignatureVerifier is EIP712 {
    /**
     * @notice Sets up the EIP-712 domain separator for typed data signatures.
     */
    constructor() EIP712("AccountingModule", "1") {}

    /// @notice Mapping to track used signatures to prevent replay attacks
    mapping(bytes signature => bool used) public usedSignatures;

    /// @notice Thrown when signature recovery fails or signer doesn't match expected address
    error InvalidSignature();
    /// @notice Thrown when attempting to reuse a previously used signature
    error UsedSignature();

    /// @notice EIP-712 type hash for withdraw operations
    bytes32 private constant WITHDRAW_TYPEHASH =
        keccak256(
            "Withdraw(address userAddress,bytes32 tokenId,uint256 amount)"
        );

    /// @notice EIP-712 type hash for lock operations
    bytes32 private constant LOCK_TYPEHASH =
        keccak256(
            "Lock(address userAddress,address serviceAddress,bytes32 tokenId,uint256 amount,uint256 expiry)"
        );

    /// @notice EIP-712 type hash for transfer operations
    bytes32 private constant TRANSFER_TYPEHASH =
        keccak256(
            "Transfer(address userAddress,address toAddress,bytes32 tokenId,uint256 amount)"
        );

    /// @notice EIP-712 type hash for locked fund transfer operations
    bytes32 private constant TRANSFER_LOCKED_TYPEHASH =
        keccak256(
            "TransferLocked(address userAddress,address toAddress,uint256 lockIndex,uint256 amount)"
        );

    /// @notice EIP-712 type hash for modifying an existing lock (add funds and/or extend expiry)
    bytes32 private constant MODIFY_LOCK_TYPEHASH =
        keccak256(
            "ModifyLock(address userAddress,uint256 lockIndex,uint256 amount,uint256 newExpiry)"
        );

    /**
     * @notice Verifies a user's EIP-712 signature for withdrawing funds.
     *
     * @param userAddress The address of the user requesting the withdrawal
     * @param tokenId The identifier of the token to withdraw
     * @param amount The amount of tokens to withdraw
     * @param signature The EIP-712 signature authorizing the withdrawal
     */
    function verifyWithdrawSignature(
        address userAddress,
        bytes32 tokenId,
        uint256 amount,
        bytes calldata signature
    ) public {
        bytes32 structHash = keccak256(
            abi.encode(WITHDRAW_TYPEHASH, userAddress, tokenId, amount)
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(digest, signature);
        if (signer != userAddress) {
            revert InvalidSignature();
        }

        if (usedSignatures[signature]) {
            revert UsedSignature();
        }

        usedSignatures[signature] = true;
    }

    /**
     * @notice Verifies a user's EIP-712 signature for locking funds to a service.
     *
     * @param userAddress The address of the user whose funds are being locked
     * @param serviceAddress The address of the service that will have access to the locked funds
     * @param tokenId The identifier of the token to lock
     * @param amount The amount of tokens to lock
     * @param expiry The timestamp when the lock expires
     * @param signature The EIP-712 signature authorizing the lock
     */
    function verifyLockSignature(
        address userAddress,
        address serviceAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 expiry,
        bytes calldata signature
    ) public {
        bytes32 structHash = keccak256(
            abi.encode(
                LOCK_TYPEHASH,
                userAddress,
                serviceAddress,
                tokenId,
                amount,
                expiry
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(digest, signature);
        if (signer != userAddress) {
            revert InvalidSignature();
        }

        if (usedSignatures[signature]) {
            revert UsedSignature();
        }

        usedSignatures[signature] = true;
    }

    /**
     * @notice Verifies a user's EIP-712 signature for transferring funds to another address.
     *
     * @param userAddress The address of the user initiating the transfer (sender)
     * @param toAddress The address receiving the funds
     * @param tokenId The identifier of the token to transfer
     * @param amount The amount of tokens to transfer
     * @param signature The EIP-712 signature authorizing the transfer
     */
    function verifyTransferSignature(
        address userAddress,
        address toAddress,
        bytes32 tokenId,
        uint256 amount,
        bytes calldata signature
    ) public {
        bytes32 structHash = keccak256(
            abi.encode(
                TRANSFER_TYPEHASH,
                userAddress,
                toAddress,
                tokenId,
                amount
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(digest, signature);
        if (signer != userAddress) {
            revert InvalidSignature();
        }

        if (usedSignatures[signature]) {
            revert UsedSignature();
        }

        usedSignatures[signature] = true;
    }

    /**
     * @notice Verifies a service's EIP-712 signature for transferring locked funds.
     *
     * @param serviceAddress The address of the service authorized to transfer the locked funds
     * @param userAddress The address of the original user who locked the funds
     * @param toAddress The address receiving the transferred locked funds
     * @param lockIndex The index of the specific lock being transferred from
     * @param amount The amount of locked tokens to transfer
     * @param signature The EIP-712 signature from the service authorizing the transfer
     */
    function verifyTransferLockedSignature(
        address serviceAddress,
        address userAddress,
        address toAddress,
        uint256 lockIndex,
        uint256 amount,
        bytes calldata signature
    ) public {
        bytes32 structHash = keccak256(
            abi.encode(
                TRANSFER_LOCKED_TYPEHASH,
                userAddress,
                toAddress,
                lockIndex,
                amount
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(digest, signature);
        if (signer != serviceAddress) {
            revert InvalidSignature();
        }

        if (usedSignatures[signature]) {
            revert UsedSignature();
        }

        usedSignatures[signature] = true;
    }

    function verifyModifyLockSignature(
        address userAddress,
        uint256 lockIndex,
        uint256 amount,
        uint256 newExpiry,
        bytes calldata signature
    ) public {
        bytes32 structHash = keccak256(
            abi.encode(
                MODIFY_LOCK_TYPEHASH,
                userAddress,
                lockIndex,
                amount,
                newExpiry
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(digest, signature);
        if (signer != userAddress) {
            revert InvalidSignature();
        }

        if (usedSignatures[signature]) {
            revert UsedSignature();
        }

        usedSignatures[signature] = true;
    }
}
