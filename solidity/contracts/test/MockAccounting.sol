// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Accounting} from "../Accounting.sol";
import {HistoryKind, UnsupportedTokenType} from "../Types.sol";
import {TokenCodec} from "../lib/TokenCodec.sol";

/**
 * @title MockAccounting
 * @notice Mock version of Accounting for testing on non-Sapphire chains (e.g., Hardhat).
 * @dev Exposes ledger/auth helpers; signer precompile mocks live in MockAccountingSigner.
 */
contract MockAccounting is Accounting {
    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor(address siweAuthAddress) Accounting(siweAuthAddress) {}

    function initialize(bytes21 _roflAppID, address _owner) external override initializer {
        __Accounting_init(_roflAppID, _owner);
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

    function decodedErc20TokenAddressWord(
        bytes memory data
    ) external pure returns (uint256 rawWord) {
        (, address tokenAddress) = TokenCodec.decodeEVMErc20TokenData(data);
        assembly ("memory-safe") {
            rawWord := tokenAddress
        }
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
     * @notice Test helper: set roflSignerAddress through the linked signer.
     * @dev Bypasses only ROFL auth; still exercises the Accounting -> signer call path.
     */
    function mockSetRoflSignerAddress(address newSigner) external {
        _signer().setRoflSignerAddress(newSigner);
        emit RoflSignerUpdated(newSigner);
    }
}
