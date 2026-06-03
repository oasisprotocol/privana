// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {AccountingStorage} from "./AccountingStorage.sol";
import {EIP712SignatureVerifier} from "./EIP712SignatureVerifier.sol";
import {EVMSignerAndVerifier} from "./EVMSignerAndVerifier.sol";
import {BridgeLib, RoflBridgeNotSet, InvalidRouteAddress} from "./BridgeLib.sol";
import {IBridgeModule} from "./interfaces/IBridgeModule.sol";
import {ChainType, TokenType, UnsupportedTokenType, InvalidAmount, GasPriceNotSet, InvalidAddress} from "./Types.sol";
import {EIP155Signer} from "@oasisprotocol/sapphire-contracts/contracts/EIP155Signer.sol";

/**
 * @title BridgeModule
 * @notice Delegated runtime for ROSE bridge selectors served at the Accounting
 *         proxy address. Bodies execute against the proxy's storage via
 *         `delegatecall` — `BridgeModule` itself is never the integration
 *         endpoint.
 *
 * @dev Inherits `AccountingStorage` so the storage prefix is byte-identical
 *      to `Accounting`'s. Does NOT inherit `UUPSUpgradeable`: this contract
 *      is not a proxy implementation. Has no state variables of its own.
 */
contract BridgeModule is AccountingStorage, IBridgeModule {
    /// @notice Chain-agnostic tokenId for bridge-asset ROSE.
    /// @dev Mirrors `Accounting.ROSE_TOKEN_ID`. Pinned literal so off-chain
    ///      consumers (ROFL TEE) read a stable value without recomputing the hash.
    bytes32 public constant ROSE_TOKEN_ID =
        0xca91975d6c6810eb4077546d4fbdb49fa231f351cddfc915862f7c0dad81a7aa;

    /// @dev Reservation rejected: depositId is the zero hash.
    error InvalidDepositId();

    /// @dev Re-call with the same depositId but at least one mismatched field.
    ///      `depositId` is included so log filters can identify the offending key.
    error BridgeBurnMismatch(bytes32 depositId);

    /// @dev Burn signing requested for a depositId with no reservation.
    error BridgeBurnNotFound(bytes32 depositId);

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    /**
     * @inheritdoc IBridgeModule
     * @dev `virtual` so `MockBridgeModule` can drop `onlyROFL` on Hardhat
     *      (Sapphire `roflEnsureAuthorizedOrigin` precompile is unavailable
     *      there). Mock override re-uses `_setRoflBridge` so test and
     *      production bodies cannot drift.
     */
    function setRoflBridge(
        uint256 chainId,
        address bridge
    ) external virtual override onlyROFL {
        _setRoflBridge(chainId, bridge);
    }

    /**
     * @dev Body of `setRoflBridge`. Split out so `MockBridgeModule` can bypass
     *      `onlyROFL` (Sapphire precompile) on Hardhat — same shape as
     *      `_reserveBridgeBurn`.
     */
    function _setRoflBridge(uint256 chainId, address bridge) internal {
        if (bridge == address(0)) revert InvalidAddress();
        roflBridgeAddress[chainId] = bridge;
        emit RoflBridgeUpdated(chainId, bridge);
    }

    /**
     * @inheritdoc IBridgeModule
     * @dev `virtual` so `MockBridgeModule` can drop `onlyROFL` on Hardhat
     *      (Sapphire `roflEnsureAuthorizedOrigin` precompile is unavailable
     *      there). Mock override re-uses `_reserveBridgeBurn` so test and
     *      production bodies cannot drift.
     */
    function reserveBridgeBurn(
        bytes32 depositId,
        uint256 chainId,
        address bridge,
        uint256 amount
    ) external virtual override onlyROFL {
        _reserveBridgeBurn(depositId, chainId, bridge, amount);
    }

    /**
     * @dev Body of `reserveBridgeBurn`. Split out so `MockBridgeModule` can
     *      bypass `onlyROFL` (Sapphire precompile) on Hardhat — same shape as
     *      `MockAccounting.mockCreditDeposit -> _creditDeposit`.
     */
    function _reserveBridgeBurn(
        bytes32 depositId,
        uint256 chainId,
        address bridge,
        uint256 amount
    ) internal {
        if (depositId == bytes32(0)) revert InvalidDepositId();
        address configured = roflBridgeAddress[chainId];
        if (configured == address(0)) revert RoflBridgeNotSet(chainId);
        if (bridge != configured) revert InvalidRouteAddress();
        if (amount == 0) revert InvalidAmount();

        BridgeBurnRequest storage stored = bridgeBurnRequests[depositId];
        if (stored.exists) {
            if (
                stored.chainId != chainId ||
                stored.bridge != bridge ||
                stored.amount != amount
            ) revert BridgeBurnMismatch(depositId);
            return;
        }

        uint64 reservedNonce = uint64(getEVMNonceAndIncrement(chainId));
        bridgeBurnRequests[depositId] = BridgeBurnRequest({
            chainId: chainId,
            bridge: bridge,
            amount: amount,
            nonce: reservedNonce,
            exists: true
        });
        emit BridgeBurnReserved(
            depositId,
            chainId,
            bridge,
            amount,
            reservedNonce
        );
    }

    /// @inheritdoc IBridgeModule
    function generateSweepERC20TransferToBridge(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        address tokenAddress,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    ) external view override onlyROFLQuery returns (bytes memory signedTx) {
        address bridge = roflBridgeAddress[chainId];
        if (bridge == address(0)) revert RoflBridgeNotSet(chainId);

        (address depositAddr, bytes32 depositSecret) = _deriveDepositKeypair(
            beneficiary,
            chainType,
            version
        );
        bytes memory data = abi.encodeWithSignature(
            "transfer(address,uint256)",
            bridge,
            amount
        );
        signedTx = EIP155Signer.sign(
            depositAddr,
            depositSecret,
            EIP155Signer.EthTx({
                nonce: sourceChainNonce,
                gasPrice: gasPrice,
                gasLimit: gasLimitERC20Sweep,
                to: tokenAddress,
                value: 0,
                data: data,
                chainId: chainId
            })
        );
    }

    /// @inheritdoc IBridgeModule
    function requestBridgeWithdrawal(
        address userAddress,
        address toAddress,
        uint256 destChainId,
        address routeAddress,
        uint256 amount,
        uint256 maxGasCost,
        uint256 userNonce,
        bytes calldata signature
    ) external override {
        BridgeLib.validateBridgeWithdrawal(
            userAddress,
            toAddress,
            destChainId,
            routeAddress,
            amount,
            maxGasCost,
            tokens[ROSE_TOKEN_ID].tokenType == TokenType.BridgeAsset,
            roflBridgeAddress[destChainId],
            block.chainid
        );

        EIP712SignatureVerifier.verifyBridgeWithdrawSignature(
            userAddress,
            toAddress,
            destChainId,
            routeAddress,
            amount,
            maxGasCost,
            userNonce,
            signature
        );

        if (balances[userAddress][ROSE_TOKEN_ID] < amount)
            revert InsufficientBalance();
        balances[userAddress][ROSE_TOKEN_ID] -= amount;

        // Inlined `_decreaseLedgerTotal`: sole caller is this function, fixed
        // tokenId. Saves a function dispatch and keeps the bridge runtime
        // self-contained.
        _ledgerTotal[ROSE_TOKEN_ID] -= amount;

        uint64 destTxNonce = getEVMNonceAndIncrement(destChainId);

        bytes memory txIdentifier = abi.encode(
            destChainId,
            destTxNonce,
            routeAddress,
            maxGasCost
        );

        withdrawals.push(
            WithdrawalRequest({
                userAddress: userAddress,
                toAddress: toAddress,
                amount: amount,
                blockNumber: block.number,
                tokenId: ROSE_TOKEN_ID,
                txIdentifier: txIdentifier,
                resolved: false
            })
        );

        emit Withdrawal(userAddress, ROSE_TOKEN_ID, amount, destChainId);
    }

    /// @inheritdoc IBridgeModule
    function resolveBridgeWithdrawal(
        uint256 index
    ) external override returns (bytes memory signedTx) {
        WithdrawalRequest storage req = withdrawals[index];
        // No `WithdrawalTooSoon` floor here: for bridge, the EIP-712 user signature
        // at request time is the auth gate, and the signed destination tx targets
        // the user's chosen recipient — simulation-extraction is not a useful
        // attack vector on this path.
        if (req.tokenId != ROSE_TOKEN_ID) revert UnsupportedTokenType();

        uint256 destChainId = abi.decode(req.txIdentifier, (uint256));
        if (destChainId != block.chainid && roflBridgeAddress[destChainId] == address(0)) {
            revert RoflBridgeNotSet(destChainId);
        }
        (signedTx, destChainId) = BridgeLib.resolveSign(
            req.toAddress,
            req.amount,
            req.txIdentifier,
            gasPrices[destChainId],
            evmAddress,
            secretKey,
            address(this),
            block.chainid,
            index
        );

        _markResolved(req, index, destChainId);
    }

    /// @dev Idempotent finalize: flips `resolved` and emits `WithdrawalResolved` only on
    ///      first call. Duplicates the body in `Accounting._markResolved`; sharing via
    ///      a delegatecall hop would cost more than the duplicated emit-encoding.
    function _markResolved(
        WithdrawalRequest storage req,
        uint256 index,
        uint256 chainId
    ) internal {
        if (req.resolved) return;
        req.resolved = true;
        emit WithdrawalResolved(
            index,
            req.userAddress,
            req.tokenId,
            req.toAddress,
            req.amount,
            chainId
        );
    }

    /// @inheritdoc IBridgeModule
    function generateBridgeBurnTransfer(
        bytes32 depositId
    ) external view override onlyROFLQuery returns (bytes memory signedTx) {
        BridgeBurnRequest memory r = bridgeBurnRequests[depositId];
        if (!r.exists) revert BridgeBurnNotFound(depositId);

        uint256 destGasPrice = gasPrices[r.chainId];
        if (destGasPrice == 0) revert GasPriceNotSet(r.chainId);

        signedTx = EIP155Signer.sign(
            evmAddress,
            secretKey,
            EIP155Signer.EthTx({
                nonce: r.nonce,
                gasPrice: destGasPrice,
                gasLimit: BridgeLib.GAS_LIMIT_BRIDGE_BURN,
                to: r.bridge,
                value: 0,
                data: abi.encodeWithSignature(
                    "burn(uint256,bytes32)",
                    r.amount,
                    depositId
                ),
                chainId: r.chainId
            })
        );
    }

    /// @inheritdoc IBridgeModule
    function getBridgeBurnRequest(
        bytes32 depositId
    )
        external
        view
        override
        returns (
            uint256 chainId,
            address bridge,
            uint256 amount,
            uint64 nonce,
            bool exists
        )
    {
        BridgeBurnRequest memory r = bridgeBurnRequests[depositId];
        return (r.chainId, r.bridge, r.amount, r.nonce, r.exists);
    }
}
