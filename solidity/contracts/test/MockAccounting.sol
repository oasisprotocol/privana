// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Accounting} from "../Accounting.sol";
import {ChainType, FundLock, InvalidAddress} from "../Types.sol";

/**
 * @title MockAccounting
 * @notice Mock version of Accounting for testing on non-Sapphire chains (e.g., Hardhat).
 * @dev Overrides the keypair generation to avoid calling Sapphire precompiles.
 */
contract MockAccounting is Accounting {
    // Real keypair from Hardhat default accounts[0]. TEST_SECRET is the actual
    // secp256k1 private key matching TEST_ADDRESS so EIP155Signer.sign produces a
    // tx whose recovered signer equals evmAddress() — required for bridge-mint
    // signer-recovery tests on Sapphire. Public well-known test value, not a credential.
    address private constant TEST_ADDRESS = 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266;
    // nosemgrep: generic.secrets.security.detected-generic-secret.detected-generic-secret
    bytes32 private constant TEST_SECRET = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor(address siweAuthAddress) Accounting(siweAuthAddress) {}

    function initialize(bytes21 _roflAppID, address _owner) external override initializer {
        __Accounting_init(_roflAppID, _owner);
    }

    function _generateKeypair() internal pure override returns (address, bytes32) {
        return (TEST_ADDRESS, TEST_SECRET);
    }

    /// @dev Deterministic deposit keypair for Hardhat. The production derivation
    ///      calls a Sapphire precompile that is unavailable here; without this
    ///      override the deposit-address paths (sweeps, emergency withdraw)
    ///      revert blank before reaching `EIP155Signer.sign`.
    function _deriveDepositKeypair(
        address beneficiary,
        ChainType /* chainType */,
        uint256 /* version */
    ) internal pure override returns (address depositAddr, bytes32 depositSecret) {
        depositSecret = keccak256(abi.encode(TEST_SECRET, beneficiary));
        depositAddr = address(uint160(uint256(depositSecret)));
    }

    /**
     * @notice Hardhat override of `setRoflBridge` dropping the `onlyROFL` gate.
     * @dev The Sapphire `roflEnsureAuthorizedOrigin` precompile is unavailable on
     *      Hardhat; reuse `_setRoflBridge` so the body cannot drift from production.
     */
    function setRoflBridge(
        uint256 chainId,
        address bridge
    ) external override {
        _setRoflBridge(chainId, bridge);
    }

    /**
     * @notice Hardhat override of `reserveBridgeBurn` dropping the `onlyROFL` gate.
     * @dev Reuses `_reserveBridgeBurn` so test and production bodies cannot drift.
     */
    function reserveBridgeBurn(
        bytes32 depositId,
        uint256 chainId,
        address bridge,
        uint256 amount
    ) external override {
        _reserveBridgeBurn(depositId, chainId, bridge, amount);
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
     * @dev Bypasses ROFL auth for Hardhat testing. Delegates to the real
     *      `_creditDeposit` so test and production paths cannot drift.
     */
    function mockCreditDeposit(
        address beneficiary,
        bytes32 tokenId,
        uint256 amount,
        bytes32 depositId
    ) external {
        _creditDeposit(beneficiary, tokenId, amount, depositId);
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

    /**
     * @notice Test helper: bump the per-chain custody-EOA nonce counter.
     * @dev `clearCustodyTx` requires `nonce < nonces[chainId]`, and `nonces`
     *      defaults to zero, so the clear surface is unreachable until the
     *      counter is advanced. `nonces` is an inherited public mapping writable
     *      from this derived mock. Only for testing — NOT for production use.
     */
    function mockSetNonce(uint256 chainId, uint64 value) external {
        nonces[chainId] = value;
    }

    /**
     * @notice Test helper: append a FundLock directly to a user's activeLocks,
     *         bypassing createLock entirely.
     * @dev Required to test downstream lock-path guards (modifyLock /
     *      withdrawFromLock) once createLock itself begins rejecting BridgeAsset.
     *      Only for testing — NOT for production use.
     */
    function mockForceLock(
        address userAddress,
        uint256 lockId,
        address serviceId,
        bytes32 tokenId,
        uint256 amount,
        uint256 expiry
    ) external {
        userInfo[userAddress].activeLocks.push(
            FundLock({
                lockId: lockId,
                serviceId: serviceId,
                tokenId: tokenId,
                amount: amount,
                expiry: expiry
            })
        );
    }
}
