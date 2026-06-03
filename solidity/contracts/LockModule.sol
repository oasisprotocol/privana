// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {AccountingStorage} from "./AccountingStorage.sol";
import {EIP712SignatureVerifier} from "./EIP712SignatureVerifier.sol";
import {ILockModule} from "./interfaces/ILockModule.sol";
import {IAccountingForLockModule} from "./interfaces/IAccountingForLockModule.sol";
import {FundLock, HistoryKind, InvalidAmount, TokenType, UserInfo} from "./Types.sol";

/**
 * @title LockModule
 * @notice Delegated runtime for the lock subsystem served at the Accounting
 *         proxy address. Bodies execute against the proxy's storage via
 *         `delegatecall` — `LockModule` itself is never the integration
 *         endpoint.
 *
 * @dev Inherits `AccountingStorage` so the storage prefix is byte-identical
 *      to `Accounting`'s, and so the lock bodies reach the inherited EIP-712
 *      verifiers, the shared `_scheduleWithdrawal` / history-append helpers,
 *      and the shared lock state (`balances`, `userInfo`, `nextLockId`). Does
 *      NOT inherit `UUPSUpgradeable`: this contract is not a proxy
 *      implementation. Has no state variables of its own.
 */
contract LockModule is AccountingStorage, ILockModule {
    error TooManyActiveLocks();
    error InvalidLockId();
    error LockNotExpired();
    error InsufficientLockedAmount();
    error InvalidExpiry();

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    /// @dev Resolves the SIWE-authenticated caller. Under `delegatecall` the
    ///      proxy's `siweAuth` immutable is unreachable directly (a module reads
    ///      its own), so the token path calls back into the proxy. The empty-token
    ///      path returns `msg.sender`, which delegatecall preserves.
    function _authSender(bytes memory token) internal view returns (address) {
        if (token.length != 0) {
            return
                IAccountingForLockModule(address(this)).siweAuth().authSender(
                    token
                );
        }
        return msg.sender;
    }

    /**
     * @notice Creates a lock on user funds for exclusive access by a designated service.
     *
     * This function allows users to lock a portion of their funds for use by a specific
     * service. Locked funds are removed from the user's available balance but remain
     * owned by the user until the lock expires or the service transfers them.
     *
     * The locking mechanism enables:
     * - Escrow-like functionality for service interactions
     * - Temporary delegation of fund access to trusted services
     * - Time-bounded locks that automatically expire
     * - Multiple concurrent locks per user (up to 10)
     *
     * Security features:
     * - EIP-712 signature verification to authorize the lock
     * - Expiry timestamp validation to ensure locks are created with future expiry
     * - Balance verification before locking
     * - Limited number of active locks per user (max 10)
     *
     * @dev The signature must be from the user whose funds are being locked.
     *      Locked funds are stored in the user's activeLocks array.
     *      The expiry must be a timestamp in the future (> block.timestamp).
     *
     * @param serviceAddress The address of the service that will have access to the locked funds
     * @param tokenId The identifier of the token to lock
     * @param amount The amount of tokens to lock
     * @param expiry The timestamp when the lock expires and funds can be reclaimed (must be in future)
     * @param nonce The nonce for replay protection (must match user's current createLockNonces)
     * @param signature The EIP-712 signature from the user authorizing the lock
     */
    function createLock(
        address serviceAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 expiry,
        uint256 nonce,
        bytes calldata signature
    ) external override {
        if (expiry <= block.timestamp) revert InvalidExpiry();
        if (amount == 0) revert InvalidAmount();
        if (tokens[tokenId].tokenType == TokenType.BridgeAsset)
            revert BridgeAssetNotSupported();

        address userAddress = EIP712SignatureVerifier.verifyLockSignature(
            serviceAddress,
            tokenId,
            amount,
            expiry,
            nonce,
            signature
        );

        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        if (locks.length >= 10) revert TooManyActiveLocks();

        if (balances[userAddress][tokenId] < amount)
            revert InsufficientBalance();

        balances[userAddress][tokenId] -= amount;

        uint256 lockId = nextLockId++;
        locks.push(
            FundLock({
                lockId: lockId,
                serviceId: serviceAddress,
                tokenId: tokenId,
                amount: amount,
                expiry: expiry
            })
        );

        _appendUserCounterpartyHistory(
            userAddress,
            HistoryKind.CreateLock,
            tokenId,
            amount,
            serviceAddress
        );
    }

    function _findLockIndex(
        FundLock[] storage locks,
        uint256 lockId
    ) internal view returns (uint256) {
        for (uint256 i = 0; i < locks.length; i++) {
            if (locks[i].lockId == lockId) {
                return i;
            }
        }
        revert InvalidLockId();
    }

    /**
     * @notice Modifies an existing lock by increasing the locked amount and/or extending expiry.
     * @dev Expiry can only be extended (newExpiry >= current expiry). Amount increases are
     *      drawn from the user's available balance. Authorized via EIP-712 user signature.
     * @param lockId The unique identifier of the lock to modify
     * @param amount Additional amount to add to the lock (0 to only extend expiry)
     * @param newExpiry The new expiry timestamp for the lock
     * @param nonce The nonce for replay protection (must match user's current modifyLockNonces)
     * @param signature The EIP-712 signature from the user authorizing the modification
     */
    function modifyLock(
        uint256 lockId,
        uint256 amount,
        uint256 newExpiry,
        uint256 nonce,
        bytes calldata signature
    ) external override {
        address userAddress = EIP712SignatureVerifier.verifyModifyLockSignature(
            lockId,
            amount,
            newExpiry,
            nonce,
            signature
        );

        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        uint256 lockIndex = _findLockIndex(locks, lockId);
        FundLock storage lock = locks[lockIndex];

        if (tokens[lock.tokenId].tokenType == TokenType.BridgeAsset)
            revert BridgeAssetNotSupported();

        if (newExpiry < lock.expiry) revert InvalidExpiry();

        if (amount == 0 && newExpiry == lock.expiry) revert InvalidAmount();

        if (amount > 0) {
            if (balances[userAddress][lock.tokenId] < amount)
                revert InsufficientBalance();

            balances[userAddress][lock.tokenId] -= amount;
            lock.amount += amount;
        }

        lock.expiry = newExpiry;
        _appendUserCounterpartyHistory(
            userAddress,
            HistoryKind.ModifyLock,
            lock.tokenId,
            amount,
            lock.serviceId
        );
    }

    /**
     * @notice Unlocks a single expired fund lock and returns funds to the user's available balance.
     *
     * This function allows users to reclaim funds from an expired lock. Once a lock's
     * expiry timestamp has passed, the original user can call this function to
     * unlock the funds and restore them to their available balance.
     *
     * The unlocking process:
     * 1. Validates the lock index exists
     * 2. Checks that the lock has expired (block.timestamp >= expiry)
     * 3. Returns any remaining locked amount to the user's balance
     * 4. Removes the lock from the user's active locks array
     *
     * Security features:
     * - Time-based expiry validation to prevent premature unlocking
     * - Index bounds checking to prevent invalid access
     * - Efficient lock removal using swap-and-pop pattern
     *
     * @dev Uses swap-and-pop to remove locks efficiently from the array.
     *      The lock order may change after removal due to the swap operation.
     *      Anyone can call this function for any user if the lock has expired.
     *      The purpose of this function is to allow users to reclaim funds if
     *      a service goes down or becomes unresponsive.
     *
     * @param userAddress The address of the user whose lock should be unlocked
     * @param lockId The unique identifier of the lock to unlock
     */
    function unlockSingleLock(
        address userAddress,
        uint256 lockId
    ) external override {
        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        uint256 lockIndex = _findLockIndex(locks, lockId);
        FundLock memory lock = locks[lockIndex];

        if (lock.amount != 0) {
            if (block.timestamp < lock.expiry) revert LockNotExpired();
            balances[userAddress][lock.tokenId] += lock.amount;
            _appendUserCounterpartyHistory(
                userAddress,
                HistoryKind.UnlockLock,
                lock.tokenId,
                lock.amount,
                lock.serviceId
            );
        }

        locks[lockIndex] = locks[locks.length - 1];
        locks.pop();
    }

    /**
     * @notice Unlocks all expired fund locks for a user and returns funds to available balance.
     *
     * This function iterates through all of a user's active locks and unlocks any that
     * have expired. This is a convenience function to avoid calling unlockSingleLock
     * multiple times when a user has several expired locks.
     *
     * @dev Iterates backwards to handle swap-and-pop without skipping elements.
     *      Anyone can call this function for any user.
     *
     * @param userAddress The address of the user whose expired locks should be unlocked
     * @return unlockedCount The number of locks that were successfully unlocked
     */
    function unlockAllExpiredLocks(
        address userAddress
    ) external override returns (uint256 unlockedCount) {
        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        unlockedCount = 0;
        uint256 i = locks.length;

        while (i > 0) {
            i--;
            FundLock memory lock = locks[i];

            if (block.timestamp >= lock.expiry && lock.amount > 0) {
                balances[userAddress][lock.tokenId] += lock.amount;
                _appendUserCounterpartyHistory(
                    userAddress,
                    HistoryKind.UnlockLock,
                    lock.tokenId,
                    lock.amount,
                    lock.serviceId
                );

                locks[i] = locks[locks.length - 1];
                locks.pop();
                unlockedCount++;
            }
        }

        return unlockedCount;
    }

    /**
     * @notice Transfers locked funds under service authorization.
     *
     * This function allows a service to transfer funds that were previously locked
     * to them by a user. Unlike regular transfers, this requires authorization from
     * the service (not the original user) since the funds are under the service's
     * temporary control.
     *
     * The transfer process:
     * 1. Validates the lock index and retrieves the lock details
     * 2. Verifies the service's EIP-712 signature authorizing the transfer
     * 3. Checks that the lock has sufficient funds for the transfer
     * 4. Reduces the locked amount and credits the recipient
     * 5. Removes the lock if all funds are transferred
     *
     * Security features:
     * - Service signature verification (not user signature)
     * - Lock amount validation before transfer
     * - Automatic lock cleanup when empty
     * - Signature replay protection via EIP712SignatureVerifier
     *
     * @dev The signature must be from the service address associated with the lock.
     *      If the lock amount reaches zero, the lock is automatically removed.
     *      The lock array may be reordered due to swap-and-pop removal. The service is
     *      a user (managed the same way as regular users) and any user can act as a service.
     *
     * @param userAddress The address of the user who originally locked the funds
     * @param toAddress The address receiving the transferred locked funds
     * @param lockId The unique identifier of the lock to transfer from
     * @param amount The amount of locked tokens to transfer
     * @param nonce The nonce for replay protection (must match service's current transferLockedNonces)
     * @param signature The EIP-712 signature from the service authorizing the transfer
     */
    function transferFromLock(
        address userAddress,
        address toAddress,
        uint256 lockId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) external override {
        if (amount == 0) revert InvalidAmount();

        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        uint256 lockIndex = _findLockIndex(locks, lockId);
        FundLock storage lock = locks[lockIndex];

        EIP712SignatureVerifier.verifyTransferLockedSignature(
            lock.serviceId,
            userAddress,
            toAddress,
            lockId,
            amount,
            nonce,
            signature
        );

        if (lock.amount < amount) revert InsufficientLockedAmount();

        lock.amount -= amount;
        balances[toAddress][lock.tokenId] += amount;

        _appendTransferHistoryForParticipants(
            userAddress,
            toAddress,
            HistoryKind.TransferFromLock,
            lock.tokenId,
            amount
        );

        if (lock.amount == 0) {
            locks[lockIndex] = locks[locks.length - 1];
            locks.pop();
        }
    }

    /**
     * @notice Withdraws locked funds to an external on-chain address via scheduled withdrawal.
     * @dev Service-authorized counterpart to `transferFromLock`: instead of crediting an
     *      internal balance, this schedules a cross-chain withdrawal signed by ROFL.
     *      Signature must come from the lock's serviceId. If `lock.amount` hits zero the
     *      slot is removed via swap-and-pop.
     * @param userAddress The user who originally created the lock
     * @param toAddress The external destination address on the token's source chain
     * @param lockId The unique identifier of the lock to withdraw from
     * @param amount The amount of locked tokens to withdraw
     * @param nonce The nonce for replay protection (must match service's current withdrawFromLock nonce)
     * @param signature The EIP-712 signature from the service authorizing the withdrawal
     */
    function withdrawFromLock(
        address userAddress,
        address toAddress,
        uint256 lockId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) external override {
        if (amount == 0) revert InvalidAmount();
        if (toAddress == address(0)) revert AddressMismatch();

        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        uint256 lockIndex = _findLockIndex(locks, lockId);
        FundLock storage lock = locks[lockIndex];

        if (tokens[lock.tokenId].tokenType == TokenType.BridgeAsset)
            revert BridgeAssetNotSupported();

        EIP712SignatureVerifier.verifyWithdrawFromLockSignature(
            lock.serviceId,
            userAddress,
            toAddress,
            lockId,
            amount,
            nonce,
            signature
        );

        if (lock.amount < amount) revert InsufficientLockedAmount();

        lock.amount -= amount;

        bytes32 tokenId = lock.tokenId;

        if (lock.amount == 0) {
            locks[lockIndex] = locks[locks.length - 1];
            locks.pop();
        }

        _scheduleWithdrawal(userAddress, toAddress, tokenId, amount);
        _appendUserCounterpartyHistory(
            userAddress,
            HistoryKind.Withdraw,
            tokenId,
            amount,
            toAddress
        );
    }

    /**
     * @notice Returns active locks for the authenticated user. Requires auth token for private reads.
     * @param token SIWE auth token identifying the caller
     * @return Array of active fund locks owned by the authenticated user
     */
    function getUserLocks(
        bytes calldata token
    ) external view override returns (FundLock[] memory) {
        address user = _authSender(token);
        if (user == address(0)) revert Unauthorized();
        return userInfo[user].activeLocks;
    }

    /**
     * @notice Returns locks for `user` scoped to authenticated service identity.
     * @param user The lock owner whose locks should be filtered
     * @param token SIWE auth token identifying the caller as the service
     * @return Array of `user`'s locks where serviceId matches the authenticated caller
     */
    function getServiceLocks(
        address user,
        bytes calldata token
    ) external view override returns (FundLock[] memory) {
        address service = _authSender(token);
        if (service == address(0)) revert Unauthorized();

        UserInfo storage uInfo = userInfo[user];
        FundLock[] storage allLocks = uInfo.activeLocks;

        uint256 matchCount = 0;
        for (uint256 i = 0; i < allLocks.length; i++) {
            if (allLocks[i].serviceId == service) {
                matchCount++;
            }
        }

        FundLock[] memory serviceLocks = new FundLock[](matchCount);
        uint256 currentIndex = 0;
        for (uint256 i = 0; i < allLocks.length; i++) {
            if (allLocks[i].serviceId == service) {
                serviceLocks[currentIndex] = allLocks[i];
                currentIndex++;
            }
        }

        return serviceLocks;
    }
}
