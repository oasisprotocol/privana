// SPDX-License-Identifier: MIT
pragma solidity ^0.8.18;

struct EVMTransactionProof {
    bytes rlpBlockHeader;
    bytes transactionIndexRlp;
    bytes transactionProofStack;
}

struct EVMReceiptProof {
    bytes receiptIndexRlp;
    bytes receiptProofStack;
}

interface IProvethVerifier {
    function validateTxProof(
        EVMTransactionProof calldata txProof
    ) external pure returns (bytes memory);

    function validateReceiptProof(
        bytes memory rlpBlockHeader,
        EVMReceiptProof calldata receiptProof
    ) external pure returns (bytes memory);
}
