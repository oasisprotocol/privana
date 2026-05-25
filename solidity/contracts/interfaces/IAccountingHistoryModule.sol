// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {HistoryEntry, HistoryKind} from "../Types.sol";

/**
 * @title IAccountingHistoryModule
 * @notice Selector list for the delegated history runtime served at the
 *         Accounting proxy address.
 * @dev Accounting owns the history storage. The module only supplies code that
 *      executes against Accounting storage via `delegatecall`.
 */
interface IAccountingHistoryModule {
    function appendHistory(
        address user,
        HistoryKind kind,
        bytes calldata payload
    ) external;

    function appendTransferHistory(
        address fromAddress,
        address toAddress,
        HistoryKind kind,
        bytes calldata payload
    ) external;

    function getHistory(
        int256 offset,
        uint256 limit,
        bytes calldata token
    ) external view returns (HistoryEntry[] memory page, uint256 total);
}
