// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {InvalidAddress, InvalidAmount, UnsupportedTokenType, GasPriceNotSet} from "./Types.sol";
import {EIP155Signer} from "@oasisprotocol/sapphire-contracts/contracts/EIP155Signer.sol";

/// @notice Raised when the routeAddress in a bridge withdrawal violates per-chain rules
///         (Sapphire: must be zero; non-Sapphire: must equal `roflBridgeAddress[destChainId]`).
error InvalidRouteAddress();

/// @notice Raised when maxGasCost violates the per-chain rule
///         (Sapphire: 0 < maxGasCost <= MAX_SAPPHIRE_RELEASE_RESERVE; non-Sapphire: must be 0).
error InvalidMaxGasCost();

/// @notice Raised when `roflBridgeAddress[chainId]` is unset for a chain that requires it.
error RoflBridgeNotSet(uint256 chainId);

/// @notice Raised when `GAS_LIMIT_NATIVE_RELEASE * gasPrices[Sapphire]` exceeds the user-signed `maxGasCost`.
error GasBudgetExceeded();

/**
 * @title BridgeLib
 * @notice Pure-ish helpers for the ROSE bridge withdrawal surface. Storage reads
 *         (token registration, configured bridge address, gas prices) happen at the
 *         call site in `Accounting.sol` and the values are passed in.
 * @dev    Library is deployed separately and DELEGATECALL-linked so `Accounting`
 *         stays under the EIP-170 24576-byte cap. All bridge-specific dispatch +
 *         signing logic lives here; `Accounting.resolveBridgeWithdrawal` is a thin
 *         wrapper that handles storage choreography (queue lookup + idempotent emit)
 *         only.
 */
library BridgeLib {
    /// @notice Maximum signed Sapphire release reserve, in wei (0.01 ROSE).
    /// @dev Cross-Layer Contract rule 20.
    uint256 internal constant MAX_SAPPHIRE_RELEASE_RESERVE =
        10_000_000_000_000_000;

    /// @notice Gas limit for the Sapphire native release tx.
    /// @dev Intrinsic 21k for a no-data value transfer + headroom for nonce dispatch.
    uint64 internal constant GAS_LIMIT_NATIVE_RELEASE = 25000;

    /// @notice Gas limit for the Base ROFLBridge.mint(...) tx.
    /// @dev xerc20 first-mint + wrapper overhead consumes ~119k; 200k carries headroom.
    uint64 internal constant GAS_LIMIT_BRIDGE_MINT = 200000;

    /// @notice Gas limit for the Base xROSE.burn(uint256,bytes32) tx.
    /// @dev Mirrors mint sizing; same xerc20 + wrapper overhead profile.
    uint64 internal constant GAS_LIMIT_BRIDGE_BURN = 200000;

    /**
     * @notice Reject an ill-formed bridge withdrawal request before any signature
     *         verification or state mutation.
     * @param userAddress         EIP-712 signer (Cross-Layer rule 7).
     * @param toAddress           Destination-chain recipient.
     * @param destChainId         Destination chain id; either the Sapphire chain id or any chain id with a registered ROFL bridge route.
     * @param routeAddress        Per-chain route: zero on Sapphire, registered ROFLBridge address on non-Sapphire.
     * @param amount              Gross debit; must exceed `maxGasCost` on Sapphire.
     * @param maxGasCost          User-signed Sapphire reserve (must be 0 on non-Sapphire).
     * @param roseIsBridgeAsset   `tokens[ROSE_TOKEN_ID].tokenType == TokenType.BridgeAsset`.
     * @param destBridgeAddress   `roflBridgeAddress[destChainId]` (or zero if unset).
     * @param sapphireChainId     Caller's `block.chainid`; identifies the Sapphire-native release branch.
     */
    function validateBridgeWithdrawal(
        address userAddress,
        address toAddress,
        uint256 destChainId,
        address routeAddress,
        uint256 amount,
        uint256 maxGasCost,
        bool roseIsBridgeAsset,
        address destBridgeAddress,
        uint256 sapphireChainId
    ) external pure {
        if (userAddress == address(0)) revert InvalidAddress();
        if (toAddress == address(0)) revert InvalidAddress();
        if (amount == 0) revert InvalidAmount();
        if (!roseIsBridgeAsset) revert UnsupportedTokenType();

        if (destChainId == sapphireChainId) {
            if (routeAddress != address(0)) revert InvalidRouteAddress();
            if (maxGasCost == 0) revert InvalidMaxGasCost();
            if (maxGasCost > MAX_SAPPHIRE_RELEASE_RESERVE)
                revert InvalidMaxGasCost();
            if (amount <= maxGasCost) revert InvalidAmount();
        } else if (destBridgeAddress != address(0)) {
            if (routeAddress != destBridgeAddress) revert InvalidRouteAddress();
            if (maxGasCost != 0) revert InvalidMaxGasCost();
        } else {
            revert RoflBridgeNotSet(destChainId);
        }
    }

    /**
     * @notice Decode a queued bridge withdrawal's `txIdentifier`, build the
     *         destination-chain `EthTx`, and sign it.
     * @dev Caller passes `destGasPrice = gasPrices[destChainId]`; reverts
     *      `GasPriceNotSet(destChainId)` if zero. Sapphire branch also reverts
     *      `GasBudgetExceeded` when `GAS_LIMIT_NATIVE_RELEASE * destGasPrice >
     *      maxGasCost`. `txIdentifier` decodes to
     *      `(destChainId, destTxNonce, routeAddress, maxGasCost)` (Cross-Layer
     *      rule 5); `accountingProxy` + `sapphireChainId` namespace the
     *      `withdrawalId = keccak256(abi.encode(proxy, chainid, index))`
     *      (Cross-Layer rule 13).
     * @return signedTx RLP-encoded signed tx for the destination chain.
     * @return destChainId Decoded destination chain id (Accounting echoes it in the event).
     */
    function resolveSign(
        address toAddress,
        uint256 amount,
        bytes memory txIdentifier,
        uint256 destGasPrice,
        address evmAddress,
        bytes32 secretKey,
        address accountingProxy,
        uint256 sapphireChainId,
        uint256 index
    ) external view returns (bytes memory signedTx, uint256 destChainId) {
        uint64 destTxNonce;
        address routeAddress;
        uint256 maxGasCost;
        (destChainId, destTxNonce, routeAddress, maxGasCost) = abi.decode(
            txIdentifier,
            (uint256, uint64, address, uint256)
        );

        if (destGasPrice == 0) revert GasPriceNotSet(destChainId);

        EIP155Signer.EthTx memory ethTx;
        if (destChainId == sapphireChainId) {
            if (uint256(GAS_LIMIT_NATIVE_RELEASE) * destGasPrice > maxGasCost)
                revert GasBudgetExceeded();
            ethTx = EIP155Signer.EthTx({
                nonce: destTxNonce,
                gasPrice: destGasPrice,
                gasLimit: GAS_LIMIT_NATIVE_RELEASE,
                to: toAddress,
                value: amount - maxGasCost,
                data: "",
                chainId: destChainId
            });
        } else {
            bytes32 withdrawalId = keccak256(
                abi.encode(accountingProxy, sapphireChainId, index)
            );
            ethTx = EIP155Signer.EthTx({
                nonce: destTxNonce,
                gasPrice: destGasPrice,
                gasLimit: GAS_LIMIT_BRIDGE_MINT,
                to: routeAddress,
                value: 0,
                data: abi.encodeWithSignature(
                    "mint(address,uint256,bytes32)",
                    toAddress,
                    amount,
                    withdrawalId
                ),
                chainId: destChainId
            });
        }

        signedTx = EIP155Signer.sign(evmAddress, secretKey, ethTx);
    }
}
