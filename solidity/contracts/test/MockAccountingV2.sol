// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {MockAccounting} from "./MockAccounting.sol";

/**
 * @title MockAccountingV2
 * @notice V2 mock for testing upgrades with new state variables and reinitializer.
 */
contract MockAccountingV2 is MockAccounting {
    uint256 public newStateVar;

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor(address siweAuthAddress) MockAccounting(siweAuthAddress) {}

    function initializeV2(uint256 _newStateVar) external reinitializer(2) {
        newStateVar = _newStateVar;
    }
}
