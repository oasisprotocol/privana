// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {MockAccounting} from "./MockAccounting.sol";

/**
 * @title MockAccountingHelper
 * @notice Helpers that should have been part of MockAccounting contract, but would exceed the contract code size limitation (EIP-170).
 */
contract MockAccountingHelper {
    MockAccounting public mockAccounting;

    constructor(MockAccounting _mockAccounting) {
        mockAccounting = _mockAccounting;
    }

    /**
     * @notice Calls creditDeposit n times from solidity to speed up the sapphire-localnet tests.
     */
    function mockCreditDepositNTimes(
        address beneficiary,
        bytes32 tokenId,
        uint256 amount,
        bytes32 depositId,
        uint256 n
    ) external {
        for (uint256 i=0; i<n; i++) {
            mockAccounting.mockCreditDeposit(beneficiary, tokenId, i+amount, keccak256(abi.encodePacked(depositId, i)));
        }
    }
}
