// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Accounting} from "../Accounting.sol";
import {ChainType} from "../Types.sol";

/**
 * @title MockAccounting
 * @notice Mock version of Accounting for testing on non-Sapphire chains (e.g., Hardhat).
 * @dev Overrides the keypair generation to avoid calling Sapphire precompiles.
 * @custom:oz-upgrades-unsafe-allow missing-initializer
 */
contract MockAccounting is Accounting {
    // Test keypair: #4 of "chimney theory present latin find behave ankle clock shadow earn suit reflect"
    address private constant TEST_ADDRESS = 0xe6F321Fb3D912Db48DE460560B8bB99B57AeAcA2;
    bytes32 private constant TEST_SECRET = bytes32(0x9147e5178b1ee427d704dcdb699f1adf9c8a3b58480a6118635a3486ad3a35ce);

    /**
     * @notice Test helper to read user balance directly (bypassing privacy checks)
     * @dev Only for testing purposes - NOT for production use
     */
    function getBalance(address user, bytes32 tokenId) external view returns (uint256) {
        return balances[user][tokenId];
    }

    /**
     * @notice Test helper to set user balance directly (bypassing deposit verification)
     * @dev Only for testing purposes - NOT for production use
     */
    function setBalance(address user, bytes32 tokenId, uint256 amount) external {
        balances[user][tokenId] = amount;
    }

    /**
     * @notice Ignore ROFL check if app id is zero.
     * @dev Only for testing purposes - NOT for production use
     */
    function _checkRoflAppId() internal view override {
        if (roflAppId() != bytes21(0)) {
            super._checkRoflAppId();
        }
    }

    /**
     * @notice Calls creditDeposit n times from solidity to speed up the sapphire-localnet tests.
     */
    function creditDepositNTimes(
        address beneficiary,
        bytes32 tokenId,
        uint256 amount,
        bytes32 depositId,
        uint256 n
    ) external {
        for (uint256 i = 0; i < n; i++) {
            this.creditDeposit(beneficiary, tokenId, i + amount, keccak256(abi.encodePacked(depositId, i)));
        }
    }
}
