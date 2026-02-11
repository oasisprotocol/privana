// SPDX-License-Identifier: MIT
/* solhint-disable no-console */
pragma solidity ^0.8.20;

import {EVMSignerAndVerifier} from "../EVMSignerAndVerifier.sol";
import {ProvethVerifier} from "../lib/ProvethVerifier.sol";

contract MockEVMSignerAndVerifier is EVMSignerAndVerifier {
    // Test keypair - NOT for production use
    address private constant TEST_ADDRESS = 0x1234567890123456789012345678901234567890;
    bytes32 private constant TEST_SECRET = bytes32(uint256(1));

    constructor() EVMSignerAndVerifier(address(0)) {}

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
