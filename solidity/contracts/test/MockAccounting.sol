// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Accounting} from "../Accounting.sol";
import {ChainType, HistoryKind, UnsupportedTokenType} from "../Types.sol";

/**
 * @title MockAccounting
 * @notice Mock version of Accounting for testing on non-Sapphire chains (e.g., Hardhat).
 * @dev Overrides the keypair generation to avoid calling Sapphire precompiles.
 */
contract MockAccounting is Accounting {
    // Test keypair: #4 of "chimney theory present latin find behave ankle clock shadow earn suit reflect"
    address private constant TEST_ADDRESS = 0xe6F321Fb3D912Db48DE460560B8bB99B57AeAcA2;
    bytes32 private constant TEST_SECRET = bytes32(0x9147e5178b1ee427d704dcdb699f1adf9c8a3b58480a6118635a3486ad3a35ce);

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor(address siweAuthAddress) Accounting(siweAuthAddress) {}

    function initialize(bytes21 _roflAppID, address _owner) external override initializer {
        __Accounting_init(_roflAppID, _owner);
    }

    function _generateKeypair() internal pure override returns (address, bytes32) {
        return (TEST_ADDRESS, TEST_SECRET);
    }

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
     * @notice Test helper: creditDeposit without onlyROFL check.
     * @dev Bypasses ROFL auth for Hardhat testing.
     */
    function mockCreditDeposit(
        address beneficiary,
        bytes32 tokenId,
        uint256 amount,
        bytes32 depositId
    ) external {
        if (processedDeposits[depositId]) revert DepositAlreadyProcessed();
        if (amount == 0) revert InvalidAmount();
        if (tokens[tokenId].data.length == 0) revert UnsupportedTokenType();
        processedDeposits[depositId] = true;
        balances[beneficiary][tokenId] += amount;
        _appendHistory(
            beneficiary,
            HistoryKind.Deposit,
            abi.encodePacked(tokenId, amount, depositId)
        );
        emit Deposit(tokenId, amount, depositId);
    }

    /**
     * @notice Test helper: set roflSignerAddress without onlyROFL check.
     * @dev Bypasses ROFL auth for Hardhat testing; onlyROFL calls the Sapphire
     *      roflEnsureAuthorizedOrigin precompile which doesn't exist on Hardhat.
     */
    function mockSetRoflSignerAddress(address newSigner) external {
        if (newSigner == address(0)) revert InvalidAddress();
        roflSignerAddress = newSigner;
        emit RoflSignerUpdated(newSigner);
    }

    /**
     * @notice Calls mockCreditDeposit n times from solidity to speed up the sapphire-localnet tests.
     */
    function mockCreditDepositNTimes(
        address beneficiary,
        bytes32 tokenId,
        uint256 amount,
        bytes32 depositId,
        uint256 n
    ) external {
        for (uint256 i = 0; i < n; i++) {
            this.mockCreditDeposit(beneficiary, tokenId, i + amount, keccak256(abi.encodePacked(depositId, i)));
        }
    }
}
