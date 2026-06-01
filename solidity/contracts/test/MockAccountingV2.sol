// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Accounting} from "../Accounting.sol";

/**
 * @title MockAccountingV2
 * @notice V2 mock for testing upgrades with new state variables and reinitializer.
 */
contract MockAccountingV2 is Accounting {
    uint256 public newStateVar;

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor(address siweAuthAddress) Accounting(siweAuthAddress) {}

    function initializeV2(uint256 _newStateVar) external reinitializer(2) {
        newStateVar = _newStateVar;
    }
}
