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

    function initialize(address _shoyubashi, address _provethVerifier) external initializer {
        __EVMSignerAndVerifier_init(_shoyubashi, _provethVerifier, msg.sender);
        __UUPSUpgradeable_init();
    }

    function _authorizeUpgrade(address newImplementation) internal override onlyOwner {}

    function _generateKeypair() internal pure override returns (address, bytes32) {
        return (TEST_ADDRESS, TEST_SECRET);
    }

    function exposedDecodeEVMTransaction(
        bytes memory evmTransactionData
    )
        external
        returns (
            uint256 chainId,
            bytes32 hash,
            address from,
            address to,
            uint256 value,
            bytes memory txData,
            uint256 v,
            uint256 r,
            uint256 s
        )
    {
        return EVMSignerAndVerifier.decodeEVMTransaction(evmTransactionData);
    }

    function exposedDecodeTxReceipt(
        bytes memory txReceiptData
    ) external returns (uint256 status, uint256 gasUsed) {
        return EVMSignerAndVerifier.decodeEVMTxReceipt(txReceiptData);
    }

    function exposedDecodeTxDataForErc20Transfer(
        bytes memory txData
    ) external returns (address to, uint256 amount) {
        return EVMSignerAndVerifier.decodeTxDataForErc20Transfer(txData);
    }
}
