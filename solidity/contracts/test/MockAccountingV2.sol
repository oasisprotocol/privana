// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Accounting} from "../Accounting.sol";

/**
 * @title MockAccountingV2
 * @notice V2 mock for testing upgrades with new state variables and reinitializer.
 */
contract MockAccountingV2 is Accounting {
    // Test keypair: #4 of "chimney theory present latin find behave ankle clock shadow earn suit reflect"
    address private constant TEST_ADDRESS =
        0xe6F321Fb3D912Db48DE460560B8bB99B57AeAcA2;
    bytes32 private constant TEST_SECRET =
        bytes32(
            0x9147e5178b1ee427d704dcdb699f1adf9c8a3b58480a6118635a3486ad3a35ce
        );

    uint256 public newStateVar;

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor(address siweAuthAddress) Accounting(siweAuthAddress) {}

    function _generateKeypair()
        internal
        pure
        override
        returns (address, bytes32)
    {
        return (TEST_ADDRESS, TEST_SECRET);
    }

    function initializeV2(uint256 _newStateVar) external reinitializer(2) {
        newStateVar = _newStateVar;
    }
}
