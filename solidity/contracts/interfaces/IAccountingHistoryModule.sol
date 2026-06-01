// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {HistoryEntry} from "../Types.sol";

/**
 * @title IAccountingHistoryModule
 * @notice Selector list for Accounting's delegated history reader.
 * @dev Accounting owns the history storage. The module executes against
 *      Accounting storage via `delegatecall`.
 */
interface IAccountingHistoryModule {
    // solhint-disable-next-line func-name-mixedcase
    function MODULE_ID() external view returns (bytes32);

    function getHistory(
        int256 offset,
        uint256 limit,
        bytes calldata token
    ) external view returns (HistoryEntry[] memory page, uint256 total);
}
