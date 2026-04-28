// SPDX-License-Identifier: MIT
/* solhint-disable no-console */
pragma solidity ^0.8.20;

import {EVMSignerAndVerifier} from "../EVMSignerAndVerifier.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

contract MockEVMSignerAndVerifier is EVMSignerAndVerifier, UUPSUpgradeable {
    // Test keypair - NOT for production use
    address private constant TEST_ADDRESS = 0x1234567890123456789012345678901234567890;
    bytes32 private constant TEST_SECRET = bytes32(uint256(1));

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(bytes21 _roflAppID) external initializer {
        __EVMSignerAndVerifier_init(_roflAppID, msg.sender);
    }

    function _authorizeUpgrade(address newImplementation) internal override onlyOwner {}

    function _generateKeypair() internal pure override returns (address, bytes32) {
        return (TEST_ADDRESS, TEST_SECRET);
    }
}
