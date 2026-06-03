// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ChainType} from "../Types.sol";

/**
 * @title IBridgeModule
 * @notice Selector list for the delegated bridge runtime served at the
 *         Accounting proxy address. Used by `Accounting.fallback` to compile
 *         a fixed allowlist of selectors that route through `delegatecall`.
 * @dev Adding a function here without also adding it to the fallback allowlist
 *      is a no-op; adding it to the fallback without listing it here will not
 *      compile. Both ends must match.
 */
interface IBridgeModule {
    /**
     * @notice Request a bridge withdrawal of ROSE to a configured destination chain.
     * @dev `userAddress` is the EIP-712 signer, never `msg.sender`. `userNonce` is the
     *      EIP-712 replay nonce shared with `requestWithdrawal`; the destination-EOA
     *      tx nonce reserved here is an independent counter. `txIdentifier` is encoded
     *      as `(uint256 destChainId, uint64 destTxNonce, address routeAddress,
     *      uint256 maxGasCost)`.
     *
     *      Behavior order: validate → verify sig → debit balance → debit ledger →
     *      reserve dest nonce → encode txIdentifier → enqueue → emit. Validation runs
     *      before signature math so failed requests cost a cheap config check, not
     *      ECDSA recovery.
     *
     *      Does NOT read `gasPrices[block.chainid]` for Sapphire native releases;
     *      the signed `maxGasCost` is the request-side fee authority.
     * @param userAddress   EIP-712 signer authorizing the withdrawal.
     * @param toAddress     Destination-chain recipient.
     * @param destChainId   Destination chain id; either the Sapphire chain id or any chain id with a registered ROFL bridge route.
     * @param routeAddress  Per-chain route: zero for Sapphire native release, registered ROFLBridge for non-Sapphire.
     * @param amount        Gross debit; must exceed `maxGasCost` on Sapphire.
     * @param maxGasCost    User-signed Sapphire reserve (must be 0 on non-Sapphire).
     * @param userNonce     EIP-712 replay nonce (shared with `requestWithdrawal`).
     * @param signature     EIP-712 signature over the BridgeWithdraw payload.
     */
    function requestBridgeWithdrawal(
        address userAddress,
        address toAddress,
        uint256 destChainId,
        address routeAddress,
        uint256 amount,
        uint256 maxGasCost,
        uint256 userNonce,
        bytes calldata signature
    ) external;

    /**
     * @notice Resolve a queued ROSE bridge withdrawal into a signed destination-chain tx.
     * @dev Reads gas from `gasPrices[destChainId]`; reverts `GasPriceNotSet`
     *      if zero. Sapphire branch additionally reverts `GasBudgetExceeded`
     *      if `GAS_LIMIT_NATIVE_RELEASE * gasPrices[Sapphire] > maxGasCost`
     *      (user must re-quote). Non-Sapphire branch uses the queued
     *      `routeAddress` so retries are stable across `setRoflBridge` updates.
     *      State-idempotent: only the first call emits `WithdrawalResolved`.
     *      Signed bytes can differ between calls if `gasPrices[destChainId]` was
     *      updated in between.
     * @param index     Withdrawal queue index.
     * @return signedTx RLP-encoded signed transaction ready for broadcast on the destination chain.
     */
    function resolveBridgeWithdrawal(
        uint256 index
    ) external returns (bytes memory signedTx);

    /**
     * @notice Register or update the off-chain ROFL bridge route for a destination chain.
     * @dev Bridge route is per-chain and admin-managed. Owner can pass `address(0)`
     *      to intentionally clear a misconfigured bridge. Served at the Accounting
     *      proxy address via `delegatecall`; the `OwnableUpgradeable` owner check
     *      reads ERC-7201 namespaced state from the proxy, so authorization tracks
     *      the proxy's owner.
     * @param chainId  Destination chain id (e.g. 84532 for Base Sepolia).
     * @param bridge   Address of the ROFLBridge contract used to mint to `chainId`.
     */
    function setRoflBridge(uint256 chainId, address bridge) external;

    /**
     * @notice Sign an ERC20 sweep routing a bridge-in token to `roflBridgeAddress[chainId]`.
     * @dev `tokenAddress` is unconstrained on-chain; the off-chain ROFL TEE binds it
     *      to its `settings.xrose_address` at startup (same trust model as
     *      `generateSweepERC20Transfer`). Reuses `gasLimitERC20Sweep` — raise it if
     *      xROSE ever gains a transfer hook.
     * @param beneficiary       Sapphire address whose deposit keypair is derived.
     * @param chainType         Chain family; only `ChainType.EVM` is supported.
     * @param version           Key derivation index for the deposit keypair.
     * @param chainId           Source chain id of the deposit address; must have a registered ROFL bridge route via `setRoflBridge`.
     * @param tokenAddress      ERC20 to be swept (deployed xROSE on the source chain).
     * @param amount            Token amount to transfer to the bridge.
     * @param sourceChainNonce  Source-chain tx nonce for the deposit address.
     * @param gasPrice          Source-chain gas price for the signed tx.
     * @return signedTx         RLP-encoded signed tx ready for broadcast on the source chain.
     */
    function generateSweepERC20TransferToBridge(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        address tokenAddress,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    ) external view returns (bytes memory signedTx);

    /**
     * @notice Reserve a Base custody-EOA tx nonce for a queued xROSE burn.
     * @dev ROFL-only. Idempotent on identical re-call (no state change, no
     *      second event). The reserved nonce is allocated from the same
     *      `nonces[chainId]` pool used by `requestBridgeWithdrawal` so a Base
     *      mint and a Base burn cannot collide on the wire. Off-chain
     *      consumers read the reservation via `getBridgeBurnRequest`
     *      (canonical state) and `BridgeBurnReserved` events as the audit
     *      trail; the storage mapping itself is `internal` for ABI compactness.
     * @param depositId  Caller-supplied identifier for the burn (one slot per id).
     * @param chainId    Destination chain id; must have a registered ROFL bridge route via `setRoflBridge`.
     * @param bridge     Must equal `roflBridgeAddress[chainId]` at reservation time.
     * @param amount     Burn amount; must be > 0.
     */
    function reserveBridgeBurn(
        bytes32 depositId,
        uint256 chainId,
        address bridge,
        uint256 amount
    ) external;

    /**
     * @notice Sign an xROSE burn tx for a previously reserved Base custody-EOA nonce.
     * @dev ROFL-only (signed query). Reads `BridgeBurnRequest` from storage and uses
     *      every field — chain, target bridge, amount, nonce — from the reservation;
     *      the caller supplies only `depositId`. Closes a stale-amount injection by
     *      a compromised orchestrator. Reverts `BridgeBurnNotFound(depositId)` if
     *      `bridgeBurnRequests[depositId].exists == false`. Reverts
     *      `GasPriceNotSet(r.chainId)` if `gasPrices[r.chainId] == 0`. Signer is the
     *      shared Accounting custody EOA (`evmAddress` slot read in delegatecall context).
     * @param depositId  Reservation identifier set by `reserveBridgeBurn`.
     * @return signedTx  RLP-encoded signed tx ready for broadcast on the reserved destination chain.
     */
    function generateBridgeBurnTransfer(
        bytes32 depositId
    ) external view returns (bytes memory signedTx);

    /**
     * @notice Typed accessor for `bridgeBurnRequests[depositId]`.
     * @dev Returns positional fields rather than the struct so off-chain ABI
     *      decoders stay stable across future struct extensions. Unset
     *      reservations return zero-valued fields with `exists == false`.
     * @param depositId  Reservation identifier set by `reserveBridgeBurn`.
     * @return chainId   Destination chain id.
     * @return bridge    Destination ROFLBridge address.
     * @return amount    Burn amount.
     * @return nonce     Reserved destination-EOA tx nonce.
     * @return exists    True iff a reservation has been written for `depositId`.
     */
    function getBridgeBurnRequest(
        bytes32 depositId
    )
        external
        view
        returns (
            uint256 chainId,
            address bridge,
            uint256 amount,
            uint64 nonce,
            bool exists
        );
}
