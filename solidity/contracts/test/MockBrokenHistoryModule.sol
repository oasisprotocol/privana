// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// solhint-disable custom-errors, reason-string

import {HistoryEntry} from "../Types.sol";
import {IAccountingHistoryModule} from "../interfaces/IAccountingHistoryModule.sol";

contract MockBrokenHistoryModule is IAccountingHistoryModule {
    bytes32 public constant MODULE_ID =
        keccak256("privana.accounting.historyModule.v1");

    function getHistory(
        int256,
        uint256,
        bytes calldata
    ) external pure returns (HistoryEntry[] memory, uint256) {
        revert();
    }
}
