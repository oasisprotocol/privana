// SPDX-License-Identifier: MIT
/* solhint-disable no-console */
pragma solidity ^0.8.20;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";
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
 * - Support for withdraw, lock, transfer, locked transfer, lock modification,
 *   and locked-fund withdrawal operations
 * - Domain separation with name "AccountingModule" and version "1"
 *
 * The domain deliberately omits `chainId` so wallets sign these messages
 * regardless of which network they are connected to (MetaMask and Rabby
 * refuse eth_signTypedData_v4 when domain.chainId differs from the active
 * chain, which forced users to add and switch to Sapphire for every
 * signature). Cross-deployment replay protection rests on `verifyingContract`
 * instead: deployments on different chains MUST NOT share an address, so
 * these contracts must never be deployed through a public deterministic
 * factory (same-address CREATE2 on another chain would break the guarantee).
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

    /// @notice EIP-712 domain typehash without the chainId field (see contract docs)
    bytes32 private constant CHAINLESS_DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,address verifyingContract)");

    /// @notice keccak256 of the domain name; must stay in sync with __EIP712_init above
    bytes32 private constant DOMAIN_NAME_HASH = keccak256(bytes("AccountingModule"));

    /// @notice keccak256 of the domain version; must stay in sync with __EIP712_init above
    bytes32 private constant DOMAIN_VERSION_HASH = keccak256(bytes("1"));

    function _chainlessDomainSeparator() private view returns (bytes32) {
        return keccak256(
            abi.encode(
                CHAINLESS_DOMAIN_TYPEHASH,
                DOMAIN_NAME_HASH,
                DOMAIN_VERSION_HASH,
                address(this)
            )
        );
    }

    /**
     * @dev Replaces OZ's domain separator, which bakes `block.chainid` into the
     *      digest and would force signers onto the Sapphire network.
     */
    function _hashTypedDataV4(bytes32 structHash) internal view override returns (bytes32) {
        return MessageHashUtils.toTypedDataHash(_chainlessDomainSeparator(), structHash);
    }

    /**
     * @notice ERC-5267 domain introspection, reporting the chainless domain.
     * @dev Wallets and libraries build the signing domain from this, so it must
     *      describe exactly what `_hashTypedDataV4` verifies: the fields bitmap
     *      0x0b sets name (0x01), version (0x02) and verifyingContract (0x08)
     *      but not chainId (0x04).
     */
    function eip712Domain()
        public
        view
        override
        returns (
            bytes1 fields,
            string memory name,
            string memory version,
            uint256 chainId,
            address verifyingContract,
            bytes32 salt,
            uint256[] memory extensions
        )
    {
        return (
            hex"0b",
            "AccountingModule",
            "1",
            0,
            address(this),
            bytes32(0),
            new uint256[](0)
        );
    }

    /// @notice EIP-712 type hash for withdraw operations
    bytes32 private constant WITHDRAW_TYPEHASH =
        keccak256("Withdraw(bytes32 tokenId,uint256 amount,uint256 nonce)");

    /// @notice EIP-712 type hash for lock operations
    bytes32 private constant LOCK_TYPEHASH =
        keccak256(
            "Lock(address serviceAddress,bytes32 tokenId,uint256 amount,uint256 expiry,uint256 nonce)"
        );

    /// @notice EIP-712 type hash for transfer operations
    bytes32 private constant TRANSFER_TYPEHASH =
        keccak256(
            "Transfer(address toAddress,bytes32 tokenId,uint256 amount,uint256 nonce)"
        );

    /// @notice EIP-712 type hash for locked fund transfer operations
    bytes32 private constant TRANSFER_LOCKED_TYPEHASH =
        keccak256(
            "TransferLocked(address userAddress,address toAddress,uint256 lockId,uint256 amount,uint256 nonce,address serviceAddress)"
        );

    /// @notice EIP-712 type hash for modifying an existing lock (add funds and/or extend expiry)
    bytes32 private constant MODIFY_LOCK_TYPEHASH =
        keccak256(
            "ModifyLock(uint256 lockId,uint256 amount,uint256 newExpiry,uint256 nonce)"
        );

    /// @notice EIP-712 type hash for withdrawing directly from a lock to an external address
    bytes32 private constant WITHDRAW_FROM_LOCK_TYPEHASH =
        keccak256(
            "WithdrawFromLock(address userAddress,address toAddress,uint256 lockId,uint256 amount,uint256 nonce)"
        );

    /**
     * @notice Verifies a user's EIP-712 signature for withdrawing funds.
     * @dev Internal-only verifier; consumes the recovered signer's nonce so callers
     *      cannot bump another user's nonce.
     *
     * @param tokenId The identifier of the token to withdraw
     * @param amount The amount of tokens to withdraw
     * @param nonce The nonce for replay protection (must match signer's current nonce)
     * @param signature The EIP-712 signature authorizing the withdrawal
     * @return userAddress The recovered signer authorizing the withdrawal
     */
    function verifyWithdrawSignature(
        bytes32 tokenId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) internal returns (address userAddress) {
        bytes32 structHash = keccak256(
            abi.encode(WITHDRAW_TYPEHASH, tokenId, amount, nonce)
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        userAddress = ECDSA.recover(digest, signature);
        if (userAddress == address(0)) {
            revert InvalidSignature();
        }

        if (nonce != withdrawalNonces[userAddress]) {
            revert InvalidNonce();
        }
        withdrawalNonces[userAddress]++;
    }

    /**
     * @notice Verifies a user's EIP-712 signature for locking funds to a service.
     * @dev Internal-only verifier; consumes the recovered signer's nonce so callers
     *      cannot bump another user's nonce.
     *
     * @param serviceAddress The address of the service that will have access to the locked funds
     * @param tokenId The identifier of the token to lock
     * @param amount The amount of tokens to lock
     * @param expiry The timestamp when the lock expires
     * @param nonce The nonce for replay protection (must match signer's current createLockNonces)
     * @param signature The EIP-712 signature authorizing the lock
     * @return userAddress The recovered signer authorizing the lock
     */
    function verifyLockSignature(
        address serviceAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 expiry,
        uint256 nonce,
        bytes calldata signature
    ) internal returns (address userAddress) {
        bytes32 structHash = keccak256(
            abi.encode(
                LOCK_TYPEHASH,
                serviceAddress,
                tokenId,
                amount,
                expiry,
                nonce
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        userAddress = ECDSA.recover(digest, signature);
        if (userAddress == address(0)) {
            revert InvalidSignature();
        }

        if (nonce != createLockNonces[userAddress]) {
            revert InvalidNonce();
        }
        createLockNonces[userAddress]++;
    }

    /**
     * @notice Verifies a user's EIP-712 signature for transferring funds to another address.
     * @dev Internal-only verifier; consumes the recovered signer's nonce so callers
     *      cannot bump another user's nonce.
     *
     * @param toAddress The address receiving the funds
     * @param tokenId The identifier of the token to transfer
     * @param amount The amount of tokens to transfer
     * @param nonce The nonce for replay protection (must match signer's current transfer nonce)
     * @param signature The EIP-712 signature authorizing the transfer
     * @return userAddress The recovered signer authorizing the transfer
     */
    function verifyTransferSignature(
        address toAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) internal returns (address userAddress) {
        bytes32 structHash = keccak256(
            abi.encode(
                TRANSFER_TYPEHASH,
                toAddress,
                tokenId,
                amount,
                nonce
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        userAddress = ECDSA.recover(digest, signature);
        if (userAddress == address(0)) {
            revert InvalidSignature();
        }

        if (nonce != transferNonces[userAddress]) {
            revert InvalidNonce();
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
     * @dev Internal-only verifier; consumes the recovered signer's nonce so callers
     *      cannot bump another user's nonce.
     *
     * @param lockId The unique identifier of the lock to modify
     * @param amount Additional funds to add to the lock (0 if only extending expiry)
     * @param newExpiry The new expiry timestamp for the lock
     * @param nonce The nonce for replay protection (must match signer's current modifyLockNonces)
     * @param signature The EIP-712 signature authorizing the modification
     * @return userAddress The recovered signer authorizing the modification
     */
    function verifyModifyLockSignature(
        uint256 lockId,
        uint256 amount,
        uint256 newExpiry,
        uint256 nonce,
        bytes calldata signature
    ) internal returns (address userAddress) {
        bytes32 structHash = keccak256(
            abi.encode(
                MODIFY_LOCK_TYPEHASH,
                lockId,
                amount,
                newExpiry,
                nonce
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        userAddress = ECDSA.recover(digest, signature);
        if (userAddress == address(0)) {
            revert InvalidSignature();
        }

        if (nonce != modifyLockNonces[userAddress]) {
            revert InvalidNonce();
        }
        modifyLockNonces[userAddress]++;
    }

    /**
     * @dev Reserved storage gap for future upgrades.
     * This allows adding new state variables without shifting storage layout.
     */
    uint256[44] private __gap;
}
