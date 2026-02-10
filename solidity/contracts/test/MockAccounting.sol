// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Accounting} from "../Accounting.sol";

/**
 * @title MockAccounting
 * @notice Mock version of Accounting for testing on non-Sapphire chains (e.g., Hardhat).
 * @dev Overrides the keypair generation to avoid calling Sapphire precompiles.
 */
contract MockAccounting is Accounting {
    // Address matching the test transaction on Base Sepolia (block 32680090, tx 45)
    address private constant TEST_ADDRESS = 0x284a3Fe2939a4e4859e6321537d4264533E3D549;
    bytes32 private constant TEST_SECRET = bytes32(uint256(1));

    constructor(address _shoyubashi) Accounting(_shoyubashi) {}

    function _generateKeypair() internal pure override returns (address, bytes32) {
        return (TEST_ADDRESS, TEST_SECRET);
    }

    /**
     * @notice Test helper to set user balance directly (bypassing deposit verification)
     * @dev Only for testing purposes - NOT for production use
     */
    function setBalance(address user, bytes32 tokenId, uint256 amount) external {
        balances[user][tokenId] = amount;
    }
}
