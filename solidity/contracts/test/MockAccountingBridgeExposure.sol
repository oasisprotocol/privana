// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {MockAccounting} from "./MockAccounting.sol";

/**
 * @title MockAccountingBridgeExposure
 * @notice Adds a synthetic-row injector for testing the `GasPriceNotSet` backstop
 *         in `resolveBridgeWithdrawal`. Request-time validation rejects any chain
 *         id without a registered ROFL bridge route, so the only way to drive a
 *         resolve against an unregistered chain id is to push directly into
 *         `withdrawals`.
 * @dev Kept separate from MockAccounting so MockAccountingV2 (which inherits
 *      MockAccounting) stays under the EIP-170 contract-size limit.
 */
contract MockAccountingBridgeExposure is MockAccounting {
    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor(address siweAuthAddress) MockAccounting(siweAuthAddress) {}

    /// @notice Push a synthetic bridge withdrawal directly into the queue, bypassing
    ///         `BridgeLib.validateBridgeWithdrawal`.
    /// @dev Only for testing — NOT for production use.
    function mockPushBridgeWithdrawal(
        address userAddress,
        address toAddress,
        uint256 amount,
        uint256 destChainId,
        uint64 destTxNonce,
        address routeAddress,
        uint256 maxGasCost
    ) external returns (uint256 index) {
        bytes memory txIdentifier = abi.encode(
            destChainId,
            destTxNonce,
            routeAddress,
            maxGasCost
        );
        withdrawals.push(
            WithdrawalRequest({
                userAddress: userAddress,
                toAddress: toAddress,
                amount: amount,
                blockNumber: block.number,
                tokenId: ROSE_TOKEN_ID,
                txIdentifier: txIdentifier,
                resolved: false
            })
        );
        return withdrawals.length - 1;
    }
}
