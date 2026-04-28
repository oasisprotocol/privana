// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Accounting} from "../Accounting.sol";
import {ChainType, UnsupportedTokenType} from "../Types.sol";

/**
 * @title MockAccounting
 * @notice Mock version of Accounting for testing on non-Sapphire chains (e.g., Hardhat).
 * @dev Overrides the keypair generation to avoid calling Sapphire precompiles.
 */
contract MockAccounting is Accounting {
    // Address matching the test transaction on Base Sepolia (block 32680090, tx 45)
    address private constant TEST_ADDRESS = 0x284a3Fe2939a4e4859e6321537d4264533E3D549;
    bytes32 private constant TEST_SECRET = bytes32(uint256(1));

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor(address siweAuthAddress) Accounting(siweAuthAddress) {}

    function initialize(bytes21 _roflAppID, address _owner) external override initializer {
        __Accounting_init(_roflAppID, _owner);
    }

    function _deriveDepositKeypair(
        address beneficiary,
        ChainType /* chainType */,
        uint256 /* version */
    ) internal pure override returns (address depositAddr, bytes32 depositSecret) {
        // Deterministic mock: derive from beneficiary address
        depositSecret = keccak256(abi.encode(TEST_SECRET, beneficiary));
        depositAddr = address(uint160(uint256(depositSecret)));
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
        emit Deposit(tokenId, amount, depositId);
    }

    /**
     * @notice Test helper: get deposit address for a beneficiary.
     * @dev Bypasses EIP-712 sig verification for testing.
     */
    function mockGetDepositAddress(
        address beneficiary,
        ChainType chainType,
        uint256 version
    ) external pure returns (address depositAddr) {
        (depositAddr, ) = _deriveDepositKeypair(beneficiary, chainType, version);
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
}
