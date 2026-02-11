// SPDX-License-Identifier: MIT
pragma solidity ^0.8.18;

import {EVMTransactionProof, EVMReceiptProof} from "../lib/ProvethVerifier.sol";

interface IProvethVerifier {
    function validateTxProof(
        EVMTransactionProof calldata txProof
    ) external pure returns (bytes memory);

    function validateReceiptProof(
        bytes memory rlpBlockHeader,
        EVMReceiptProof calldata receiptProof
    ) external pure returns (bytes memory);
}
