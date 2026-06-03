// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {BridgeModule} from "../BridgeModule.sol";
import {ChainType} from "../Types.sol";

/**
 * @title MockBridgeModule
 * @notice Test variant of BridgeModule that overrides Sapphire-only paths.
 * @dev Mirrors the precompile overrides in `MockAccounting`. The mock keypair
 *      is irrelevant in delegatecall context (BridgeModule's own storage is
 *      never read), but the overrides keep the contract deployable on Hardhat.
 */
contract MockBridgeModule is BridgeModule {
    address private constant TEST_ADDRESS =
        0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266;
    // nosemgrep: generic.secrets.security.detected-generic-secret.detected-generic-secret
    bytes32 private constant TEST_SECRET =
        0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;

    function _deriveDepositKeypair(
        address beneficiary,
        ChainType /* chainType */,
        uint256 /* version */
    )
        internal
        pure
        override
        returns (address depositAddr, bytes32 depositSecret)
    {
        depositSecret = keccak256(abi.encode(TEST_SECRET, beneficiary));
        depositAddr = address(uint160(uint256(depositSecret)));
    }

    function _generateKeypair()
        internal
        pure
        override
        returns (address, bytes32)
    {
        return (TEST_ADDRESS, TEST_SECRET);
    }

    /**
     * @dev Hardhat-only override of `BridgeModule.reserveBridgeBurn` that
     *      drops `onlyROFL` (Sapphire `roflEnsureAuthorizedOrigin` precompile
     *      is unavailable). Same selector, same body — the fallback allowlist
     *      keeps routing the production call through `delegatecall` to this
     *      module's address, so the body executes against proxy storage as
     *      it does in production. Mirrors `_deriveDepositKeypair` /
     *      `_generateKeypair` overrides above.
     */
    function reserveBridgeBurn(
        bytes32 depositId,
        uint256 chainId,
        address bridge,
        uint256 amount
    ) external override {
        _reserveBridgeBurn(depositId, chainId, bridge, amount);
    }

    /// @dev Hardhat-only override that drops `onlyROFL` on `setRoflBridge`.
    ///      Same pattern as the `reserveBridgeBurn` override above.
    function setRoflBridge(uint256 chainId, address bridge) external override {
        _setRoflBridge(chainId, bridge);
    }
}
