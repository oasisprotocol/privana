// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// This oracle checks if the deposit has been made on the source chain by verifying the transaction against the block hash, which is fetched from the BlockHashOracle.

interface IEVMDepositVerifier {
    function verifyEVMNativeDeposit(
        bytes calldata evmTransactionData
    ) external returns (bool);

    function verifyEVMErc20Deposit(
        bytes calldata evmTransactionData
    ) external returns (bool);
}
