// SPDX-License-Identifier: MIT
/* solhint-disable no-console */
pragma solidity ^0.8.20;

import {EVMSignerAndVerifier} from "../EVMSignerAndVerifier.sol";

contract MockEVMSignerAndVerifier is EVMSignerAndVerifier {
    // Test keypair: #4 of "chimney theory present latin find behave ankle clock shadow earn suit reflect"
    address private constant TEST_ADDRESS = 0xe6F321Fb3D912Db48DE460560B8bB99B57AeAcA2;
    bytes32 private constant TEST_SECRET = bytes32(0x9147e5178b1ee427d704dcdb699f1adf9c8a3b58480a6118635a3486ad3a35ce);

    function initialize(bytes21 _roflAppID) external initializer {
        __EVMSignerAndVerifier_init(_roflAppID);
    }

    function _generateKeypair() internal pure override returns (address, bytes32) {
        return (TEST_ADDRESS, TEST_SECRET);
    }

    /**
     * @notice Ignore ROFL check if app id is zero.
     * @dev Only for testing purposes - NOT for production use
     */
    function _checkRoflAppId() internal view override {
        if (roflAppID != bytes21(0)) {
            super._checkRoflAppId();
        }
    }
}
