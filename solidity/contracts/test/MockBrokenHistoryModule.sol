// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// solhint-disable custom-errors, reason-string

import {HistoryEntry, HistoryKind} from "../Types.sol";
import {IAccountingHistoryModule} from "../interfaces/IAccountingHistoryModule.sol";

contract MockBrokenHistoryModule is IAccountingHistoryModule {
    function appendHistory(address, HistoryKind, bytes calldata) external pure {
        revert();
    }

    function appendTransferHistory(
        address,
        address,
        HistoryKind,
        bytes calldata
    ) external pure {
        revert();
    }

    function getHistory(
        int256,
        uint256,
        bytes calldata
    ) external pure returns (HistoryEntry[] memory, uint256) {
        revert();
    }
}
