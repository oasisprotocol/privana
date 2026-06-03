// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {FundLock} from "../Types.sol";

/**
 * @title ILockModule
 * @notice Selector list for the delegated lock runtime served at the Accounting
 *         proxy address. Used by `Accounting.fallback` to compile a fixed
 *         allowlist of selectors that route through `delegatecall`.
 * @dev Adding a function here without also adding it to the fallback allowlist
 *      is a no-op; adding it to the fallback without listing it here will not
 *      compile. Both ends must match.
 */
interface ILockModule {
    function createLock(
        address serviceAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 expiry,
        uint256 nonce,
        bytes calldata signature
    ) external;

    function modifyLock(
        uint256 lockId,
        uint256 amount,
        uint256 newExpiry,
        uint256 nonce,
        bytes calldata signature
    ) external;

    function unlockSingleLock(address userAddress, uint256 lockId) external;

    function unlockAllExpiredLocks(
        address userAddress
    ) external returns (uint256 unlockedCount);

    function transferFromLock(
        address userAddress,
        address toAddress,
        uint256 lockId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) external;

    function withdrawFromLock(
        address userAddress,
        address toAddress,
        uint256 lockId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) external;

    function getUserLocks(
        bytes calldata token
    ) external view returns (FundLock[] memory);

    function getServiceLocks(
        address user,
        bytes calldata token
    ) external view returns (FundLock[] memory);
}
