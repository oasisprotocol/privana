// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {AccountingStorage} from "./AccountingStorage.sol";
import {EVMSignerAndVerifier} from "./EVMSignerAndVerifier.sol";
import {EIP712SignatureVerifier} from "./EIP712SignatureVerifier.sol";
import {ChainType, GasPriceNotSet, HistoryEntry, HistoryKind, InvalidAddress, InvalidAmount, TokenInfo, TokenType, UnsupportedTokenType} from "./Types.sol";
import {IAccountingSiweAuth} from "./interfaces/IAccountingSiweAuth.sol";
import {IBridgeModule} from "./interfaces/IBridgeModule.sol";
import {ILockModule} from "./interfaces/ILockModule.sol";
import {IAccountingHistoryModule} from "./interfaces/IAccountingHistoryModule.sol";
import {EIP155Signer} from "@oasisprotocol/sapphire-contracts/contracts/EIP155Signer.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

/**
 * @title Accounting
 * @notice Cross-chain accounting module for managing user balances and fund operations.
 *
 * Deposits verified off-chain by ROFL TEE, credited via onlyROFL. Per-user deposit
 * addresses derived on-chain from contract's secretKey. Fund locking, P2P transfers,
 * and automated withdrawals via EIP-712 signatures.
 */
contract Accounting is AccountingStorage, UUPSUpgradeable {
    /// @notice Chain-agnostic tokenId for bridge-asset ROSE.
    /// @dev Equals getTokenId(TokenInfo(TokenType.BridgeAsset, encodeBridgeAssetTokenData("ROSE"))).
    ///      Pinned literal so off-chain consumers (ROFL TEE) can read a stable value
    ///      without recomputing the hash. Cross-Layer Contract rule 2.
    bytes32 public constant ROSE_TOKEN_ID =
        0xca91975d6c6810eb4077546d4fbdb49fa231f351cddfc915862f7c0dad81a7aa;

    /// @dev ERC-1967-style unstructured slot for the delegated `BridgeModule`
    ///      pointer. Equals `bytes32(uint256(keccak256("flexvaults.accounting.bridgeModule")) - 1)`.
    ///      Lives outside `__gap` so adding the dispatcher does not shift
    ///      structured storage. Collision-checked against ERC-1967
    ///      `_IMPLEMENTATION_SLOT` and `_ADMIN_SLOT` in tests.
    bytes32 private constant _BRIDGE_MODULE_SLOT =
        bytes32(uint256(keccak256("flexvaults.accounting.bridgeModule")) - 1);

    /// @dev ERC-1967-style unstructured slot for the delegated `LockModule`
    ///      pointer. Equals `bytes32(uint256(keccak256("flexvaults.accounting.lockModule")) - 1)`.
    ///      Lives outside `__gap` so adding the dispatcher does not shift
    ///      structured storage. Collision-checked against ERC-1967
    ///      `_IMPLEMENTATION_SLOT` and `_ADMIN_SLOT` in tests.
    bytes32 private constant _LOCK_MODULE_SLOT =
        bytes32(uint256(keccak256("flexvaults.accounting.lockModule")) - 1);

    /// @custom:oz-upgrades-unsafe-allow state-variable-immutable
    IAccountingSiweAuth public immutable siweAuth;

    /// @notice Emitted when the delegated bridge module pointer is updated.
    event BridgeModuleSet(address indexed module);

    /// @notice Emitted when the delegated lock module pointer is updated.
    event LockModuleSet(address indexed module);

    error InvalidSiweAuth();

    /// @dev Raised when a routed bridge selector is invoked but no bridge
    ///      module address has been configured via `setBridgeModule`.
    error BridgeModuleNotSet();

    /// @dev Raised when a routed lock selector is invoked but no lock module
    ///      address has been configured via `setLockModule`.
    error LockModuleNotSet();

    /// @dev Raised when the proxy receives a calldata selector that is neither
    ///      a resident `Accounting` selector nor in a delegated-module allowlist.
    error UnknownSelector(bytes4 sig);

    /// @dev Raised when `setBridgeModule` is called with a non-zero address
    ///      whose `code.length` is zero (i.e. an EOA or undeployed contract).
    error BridgeModuleNotContract();

    /// @dev Raised when `setLockModule` is called with a non-zero address
    ///      whose `code.length` is zero (i.e. an EOA or undeployed contract).
    error LockModuleNotContract();

    /// @custom:oz-upgrades-unsafe-allow constructor
    /// @custom:oz-upgrades-unsafe-allow state-variable-immutable
    constructor(address siweAuthAddress) {
        _disableInitializers();
        if (siweAuthAddress == address(0)) revert InvalidSiweAuth();
        siweAuth = IAccountingSiweAuth(siweAuthAddress);
    }

    /**
     * @notice Internal initializer for the Accounting contract.
     * @param _roflAppID The ROFL app identifier
     * @param _owner Address that will own this contract
     */
    function __Accounting_init(
        bytes21 _roflAppID,
        address _owner
    ) internal onlyInitializing {
        __EIP712SignatureVerifier_init();
        __EVMSignerAndVerifier_init(_roflAppID, _owner);
        nextLockId = 1;
    }

    /**
     * @notice Initializes the Accounting contract.
     * @dev Replaces the constructor for upgradeable contracts.
     * @param _roflAppID The ROFL app identifier (stable across redeployments)
     * @param _owner Address that will own this contract
     */
    function initialize(
        bytes21 _roflAppID,
        address _owner
    ) external virtual initializer {
        __Accounting_init(_roflAppID, _owner);
    }

    /**
     * @notice Authorizes an upgrade to a new implementation.
     * @dev Required by UUPSUpgradeable. Only the contract owner can upgrade.
     * @param newImplementation Address of the new implementation contract
     */
    function _authorizeUpgrade(
        address newImplementation
    ) internal override onlyOwner {}

    function _authSender(bytes memory token) internal view returns (address) {
        if (token.length != 0) {
            return siweAuth.authSender(token);
        }
        return msg.sender;
    }

    function setHistoryModule(address module) external onlyOwner {
        if (module == address(0)) {
            revert InvalidHistoryModule();
        }
        if (module.code.length == 0) {
            revert InvalidHistoryModule();
        }

        historyModule = module;

        emit HistoryModuleSet(module);
    }

    function getHistory(
        int256 offset,
        uint256 limit,
        bytes calldata token
    ) external returns (HistoryEntry[] memory page, uint256 total) {
        bytes memory result = _delegateHistory(
            abi.encodeCall(
                IAccountingHistoryModule.getHistory,
                (offset, limit, token)
            )
        );
        return abi.decode(result, (HistoryEntry[], uint256));
    }

    /**
     * @notice Get the deposit address for an authenticated user.
     * @param chainType The chain family (see ChainType enum)
     * @param version Key derivation index
     * @param siweToken Opaque SIWE auth token from /auth/login
     * @return depositAddr The deposit address for the authenticated user
     */
    function getDepositAddress(
        ChainType chainType,
        uint256 version,
        bytes calldata siweToken
    ) external view returns (address depositAddr) {
        address beneficiary = _authSender(siweToken);
        (depositAddr, ) = _deriveDepositKeypair(
            beneficiary,
            chainType,
            version
        );
    }

    /**
     * @notice Credit a deposit to a beneficiary. ROFL-only.
     * @param beneficiary The address to credit
     * @param tokenId The token identifier
     * @param amount The deposit amount (verified off-chain by TEE)
     * @param depositId Unique deposit identifier: keccak256(chainId, txHash, tokenId, depositIndex)
     */
    function creditDeposit(
        address beneficiary,
        bytes32 tokenId,
        uint256 amount,
        bytes32 depositId
    ) external onlyROFL {
        _creditDeposit(beneficiary, tokenId, amount, depositId);
    }

    /// @dev Body of `creditDeposit` factored out so test mocks can exercise the
    ///      exact production logic without re-implementing it.
    function _creditDeposit(
        address beneficiary,
        bytes32 tokenId,
        uint256 amount,
        bytes32 depositId
    ) internal {
        if (beneficiary == address(0)) revert AddressMismatch();
        if (processedDeposits[depositId]) revert DepositAlreadyProcessed();
        if (amount == 0) revert InvalidAmount();
        if (tokens[tokenId].data.length == 0) revert UnsupportedTokenType();
        processedDeposits[depositId] = true;
        balances[beneficiary][tokenId] += amount;
        _increaseLedgerTotal(tokenId, amount);
        _appendHistory(
            beneficiary,
            HistoryKind.Deposit,
            abi.encodePacked(tokenId, amount, depositId)
        );
        emit Deposit(tokenId, amount, depositId);
    }

    // ─── Emergency Withdraw ───────────────────────────────────────────

    /**
     * @notice Deterministic key for an emergency withdrawal slot.
     * @param beneficiary The user who owns the deposit address
     * @param tokenId The token identifier
     * @param version Key derivation index
     * @return The request ID for the emergency withdrawal slot
     */
    function emergencyWithdrawKey(
        address beneficiary,
        bytes32 tokenId,
        uint256 version
    ) public pure returns (bytes32) {
        return keccak256(abi.encode(beneficiary, tokenId, version));
    }

    /**
     * @notice Request emergency withdrawal of unswept funds from deposit address.
     * @dev Requires 1-block delay before execution (same as normal withdrawal).
     *      No signature needed — msg.sender is the beneficiary and controls all
     *      parameters directly (unlike normal withdrawals where ROFL submits on
     *      the user's behalf).
     *      One slot per (beneficiary, tokenId, version) — a second request
     *      overwrites the first (which subsumes "cancel": re-request with any
     *      new params to reset the timer/destination).
     *      chainId and chainType are not parameters: they are derived from the
     *      tokenId at execute time, so a caller cannot request a withdrawal for
     *      one token and have it signed on a different chain.
     * @param tokenId The token identifier
     * @param toAddress Destination address for the emergency withdrawal
     * @param version Key derivation index for the deposit keypair
     * @return requestId Deterministic key for the emergency withdrawal slot
     */
    function requestEmergencyWithdraw(
        bytes32 tokenId,
        address toAddress,
        uint256 version
    ) external returns (bytes32 requestId) {
        if (toAddress == address(0)) revert AddressMismatch();
        if (tokens[tokenId].data.length == 0) revert UnsupportedTokenType();
        requestId = emergencyWithdrawKey(msg.sender, tokenId, version);
        emergencyWithdrawRequests[requestId] = EmergencyWithdrawRequest({
            toAddress: toAddress,
            blockNumber: block.number
        });

        emit EmergencyWithdrawRequested(requestId, tokenId);
    }

    /**
     * @notice Execute an emergency withdrawal after 1-block delay.
     * @dev Contract derives deposit keypair, signs a transfer tx, returns raw signed tx bytes.
     *      Caller broadcasts the signed tx on the source chain.
     *      Caller supplies nonce/amount/gasPrice — the contract has no knowledge of source-chain
     *      state when ROFL is down.
     *      No msg.sender check: toAddress is fixed at request time, so any caller
     *      can only send funds to the beneficiary's chosen destination. Safe to
     *      call multiple times — source-chain nonce is the double-spend guard.
     * @param beneficiary The user who owns the deposit address
     * @param tokenId The token identifier (determines chainId and token type)
     * @param version Key derivation index for the deposit keypair
     * @param sourceChainNonce Current nonce of the deposit address on the source chain
     * @param amount Amount to transfer out of the deposit address
     * @param gasPrice Gas price (wei) to embed in the signed source-chain transaction
     * @return signedTx Raw signed transaction ready to broadcast on the source chain
     */
    function executeEmergencyWithdraw(
        address beneficiary,
        bytes32 tokenId,
        uint256 version,
        uint64 sourceChainNonce,
        uint256 amount,
        uint256 gasPrice
    ) public returns (bytes memory signedTx) {
        if (tokens[tokenId].tokenType == TokenType.BridgeAsset)
            revert BridgeAssetNotSupported();

        bytes32 requestId = emergencyWithdrawKey(beneficiary, tokenId, version);
        EmergencyWithdrawRequest memory req = emergencyWithdrawRequests[
            requestId
        ];
        if (req.blockNumber == 0) revert EmergencyWithdrawNotFound();
        if (block.number - req.blockNumber < 1)
            revert EmergencyWithdrawTooSoon();

        TokenInfo memory tInfo = tokens[tokenId];

        // Dispatch on tokenType; the else-revert is the single exhaustiveness guard.
        // Both branches feed into one shared `EIP155Signer.sign` call below — the
        // heavy precompile invocation appears once in bytecode instead of twice.
        address txTo;
        uint256 txValue;
        uint64 txGasLimit;
        bytes memory txData;
        uint256 chainId;

        if (tInfo.tokenType == TokenType.NativeEVM) {
            chainId = EVMSignerAndVerifier.decodeEVMNativeTokenData(tInfo.data);
            txTo = req.toAddress;
            txValue = amount;
            txGasLimit = gasLimitNativeWithdraw;
        } else if (tInfo.tokenType == TokenType.ERC20) {
            address tokenAddress;
            (chainId, tokenAddress) = EVMSignerAndVerifier
                .decodeEVMErc20TokenData(tInfo.data);
            txTo = tokenAddress;
            txGasLimit = gasLimitERC20Withdraw;
            txData = abi.encodeWithSignature(
                "transfer(address,uint256)",
                req.toAddress,
                amount
            );
        } else {
            revert UnsupportedTokenType();
        }

        (address depositAddr, bytes32 depositSecret) = _deriveDepositKeypair(
            beneficiary,
            ChainType.EVM,
            version
        );
        signedTx = EIP155Signer.sign(
            depositAddr,
            depositSecret,
            EIP155Signer.EthTx({
                nonce: sourceChainNonce,
                gasPrice: gasPrice,
                gasLimit: txGasLimit,
                to: txTo,
                value: txValue,
                data: txData,
                chainId: chainId
            })
        );

        emit EmergencyWithdrawExecuted(requestId);
    }

    /**
     * @notice Transfers funds between users within the accounting system.
     *
     * This function enables peer-to-peer transfers of tokens between users
     * without requiring on-chain transactions on the original token's blockchain.
     * The transfer happens entirely within the accounting system's ledger.
     *
     * The transfer process:
     * 1. Verifies the user's EIP-712 signature authorizing the transfer
     * 2. Checks that the sender has sufficient balance
     * 3. Debits the amount from sender's balance
     * 4. Credits the amount to recipient's balance
     *
     * Security features:
     * - EIP-712 signature verification to authorize the transfer
     * - Balance verification before debiting
     *
     * @dev The signature must be from the sender.
     *      This is an internal transfer that doesn't generate blockchain transactions.
     *
     * @param toAddress The address of the user receiving the funds
     * @param tokenId The identifier of the token being transferred
     * @param amount The amount of tokens to transfer
     * @param nonce The nonce for replay protection (must match user's current transfer nonce)
     * @param signature The EIP-712 signature from the sender authorizing the transfer
     */
    function transferBalance(
        address toAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) public {
        if (amount == 0) revert InvalidAmount();

        address userAddress = EIP712SignatureVerifier.verifyTransferSignature(
            toAddress,
            tokenId,
            amount,
            nonce,
            signature
        );

        if (balances[userAddress][tokenId] < amount)
            revert InsufficientBalance();

        balances[userAddress][tokenId] -= amount;
        balances[toAddress][tokenId] += amount;

        _appendTransferHistoryForParticipants(
            userAddress,
            toAddress,
            HistoryKind.TransferBalance,
            tokenId,
            amount
        );
    }

    /**
     * @notice Initiates withdrawal by scheduling it for future resolution.
     *
     * This function processes user withdrawal requests by:
     * 1. Verifying the user's authorization via EIP-712 signature
     * 2. Debiting the requested amount from the user's account
     * 3. Scheduling the withdrawal for resolution in a future block
     *
     * Security features:
     * - EIP-712 signature verification to authorize withdrawal
     * - Balance verification before debiting
     * - Nonce setting when scheduling transactions
     *
     * @param tokenId The identifier of the token to withdraw
     * @param amount The amount of tokens to withdraw
     * @param nonce The user's current withdrawal nonce for replay protection
     * @param signature The EIP-712 signature from the user authorizing the withdrawal
     */
    function requestWithdrawal(
        bytes32 tokenId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) public {
        if (amount == 0) revert InvalidAmount();
        if (tokens[tokenId].tokenType == TokenType.BridgeAsset)
            revert BridgeAssetNotSupported();

        address userAddress = EIP712SignatureVerifier.verifyWithdrawSignature(
            tokenId,
            amount,
            nonce,
            signature
        );

        if (balances[userAddress][tokenId] < amount)
            revert InsufficientBalance();

        balances[userAddress][tokenId] -= amount;

        _scheduleWithdrawal(userAddress, userAddress, tokenId, amount);
        _appendUserCounterpartyHistory(
            userAddress,
            HistoryKind.Withdraw,
            tokenId,
            amount,
            userAddress
        );
    }

    /**
     * @notice Resolves a withdrawal by generating a signed transaction for the destination chain.
     *
     * This function is idempotent - it can be called multiple times for the same withdrawal.
     * On first call, it marks the withdrawal as resolved and emits an event. On subsequent
     * calls, it skips the state change but still returns the signed transaction. This allows:
     *   - Retrying broadcast if the previous attempt failed
     *   - Anyone to get the signed_tx and broadcast if the original resolver didn't
     *   - Prevention of griefing where someone resolves but never broadcasts
     *
     * The function processes withdrawal requests by:
     * 1. Ensuring a minimum block delay has passed to prevent simulation attacks
     * 2. Generating a signed transaction to transfer tokens to the user on the destination chain
     * 3. Marking as resolved (only on first call)
     *
     * Security features:
     *  - Token-type specific transaction generation
     *  - Simulation-attack protection by enforcing a minimum block delay before resolution
     *
     * @dev The returned transaction must be broadcast externally to complete withdrawal.
     *      Replay protection is handled via nonces assigned at request time.
     *
     * @param index The index of the withdrawal request to resolve
     * @return signedTx The raw signed transaction ready for broadcast
     */
    function resolveWithdrawal(
        uint256 index
    ) public returns (bytes memory signedTx) {
        WithdrawalRequest storage withdrawalRequest = withdrawals[index];

        if (block.number - withdrawalRequest.blockNumber < 1) {
            revert WithdrawalTooSoon();
        }
        // BridgeAsset (ROSE) requests have a 4-tuple `txIdentifier` and must use
        // `resolveBridgeWithdrawal`. Here the dispatch's else-branch
        // `UnsupportedTokenType` revert handles them naturally — `TokenType.BridgeAsset`
        // is neither `NativeEVM` nor `ERC20`.
        bytes32 tokenId = withdrawalRequest.tokenId;
        uint256 amount = withdrawalRequest.amount;
        address toAddress = withdrawalRequest.toAddress;
        TokenInfo memory tInfo = tokens[tokenId];
        uint256 chainId;

        if (tInfo.tokenType == TokenType.NativeEVM) {
            chainId = EVMSignerAndVerifier.decodeEVMNativeTokenData(tInfo.data);
            uint64 nonce = abi.decode(withdrawalRequest.txIdentifier, (uint64));
            signedTx = EVMSignerAndVerifier.generateNativeTransfer(
                chainId,
                toAddress,
                amount,
                nonce
            );
        } else if (tInfo.tokenType == TokenType.ERC20) {
            address tokenAddress;
            (chainId, tokenAddress) = EVMSignerAndVerifier
                .decodeEVMErc20TokenData(tInfo.data);
            uint64 nonce = abi.decode(withdrawalRequest.txIdentifier, (uint64));
            signedTx = EVMSignerAndVerifier.generateERC20Transfer(
                chainId,
                toAddress,
                tokenAddress,
                amount,
                nonce
            );
        } else {
            revert UnsupportedTokenType();
        }

        _markResolved(withdrawalRequest, index, chainId);
        return signedTx;
    }

    /// @dev Idempotent finalize: flips `resolved` and emits `WithdrawalResolved` only on
    ///      first call. Duplicated by design — an identical body lives in
    ///      `BridgeModule._markResolved`; sharing via a delegatecall hop would cost more
    ///      bytes than the duplicated emit-encoding.
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

    /**
     * @notice Computes a unique identifier for a token based on its type and metadata.
     *
     * This function generates a deterministic token ID by hashing the token's type
     * and chain-specific data. The same token configuration will always produce
     * the same ID, enabling consistent token identification across the system.
     *
     * @dev Uses keccak256 hash of ABI-encoded tokenType and data for uniqueness.
     *      The resulting ID is used as a key in the tokens mapping.
     *
     * @param info The token information containing type and chain-specific data
     * @return The unique bytes32 identifier for the token
     */
    function getTokenId(TokenInfo calldata info) public pure returns (bytes32) {
        return keccak256(abi.encode(info.tokenType, info.data));
    }

    /**
     * @notice Registers or updates token information in the system.
     *
     * This function adds a new token to the accounting system or updates
     * an existing token's configuration. The token ID is automatically
     * computed from the provided token information.
     *
     * @dev Only callable by the contract owner to prevent unauthorized token configuration.
     * @param info The complete token information including type, data, and metadata
     */
    function setTokenInfo(TokenInfo calldata info) external onlyOwner {
        bytes32 tokenId = getTokenId(info);

        // Track token for enumeration (check array to avoid duplicates)
        bool found = false;
        for (uint256 i = 0; i < registeredTokenIds.length; i++) {
            if (registeredTokenIds[i] == tokenId) {
                found = true;
                break;
            }
        }
        if (!found) {
            registeredTokenIds.push(tokenId);
        }

        tokens[tokenId] = info;

        emit TokenRegistered(tokenId, info.tokenType);
    }

    /**
     * @notice Configure the address of the delegated `BridgeModule` runtime.
     * @dev Owner-only. Rejects `address(0)` and any address that is not a
     *      deployed contract (`code.length == 0`). EOAs are rejected by the
     *      contract check. Stored in an ERC-1967-style unstructured slot to
     *      keep `__gap` untouched.
     * @param module Address of the deployed `BridgeModule` implementation.
     */
    function setBridgeModule(address module) external onlyOwner {
        if (module == address(0)) revert InvalidAddress();
        if (module.code.length == 0) revert BridgeModuleNotContract();
        bytes32 slot = _BRIDGE_MODULE_SLOT;
        assembly {
            sstore(slot, module)
        }
        emit BridgeModuleSet(module);
    }

    /**
     * @notice Read the configured `BridgeModule` address.
     * @return moduleAddr Current bridge module pointer; `address(0)` if unset.
     */
    function bridgeModule() external view returns (address moduleAddr) {
        bytes32 slot = _BRIDGE_MODULE_SLOT;
        assembly {
            moduleAddr := sload(slot)
        }
    }

    /**
     * @notice Configure the address of the delegated `LockModule` runtime.
     * @dev Owner-only. Rejects `address(0)` and any address that is not a
     *      deployed contract (`code.length == 0`). EOAs are rejected by the
     *      contract check. Stored in an ERC-1967-style unstructured slot to
     *      keep `__gap` untouched.
     * @param module Address of the deployed `LockModule` implementation.
     */
    function setLockModule(address module) external onlyOwner {
        if (module == address(0)) revert InvalidAddress();
        if (module.code.length == 0) revert LockModuleNotContract();
        bytes32 slot = _LOCK_MODULE_SLOT;
        assembly {
            sstore(slot, module)
        }
        emit LockModuleSet(module);
    }

    /**
     * @notice Read the configured `LockModule` address.
     * @return moduleAddr Current lock module pointer; `address(0)` if unset.
     */
    function lockModule() external view returns (address moduleAddr) {
        bytes32 slot = _LOCK_MODULE_SLOT;
        assembly {
            moduleAddr := sload(slot)
        }
    }

    // ─── Custody-tx clear surface (operator last-resort recovery) ──────

    /// @notice Owner-authorized clearance action for a stuck custody-tx record;
    ///         the off-chain executor maps it onto a per-record recovery.
    enum ClearAction {
        Requeue,
        Abandon,
        MarkSuccessWithHash,
        BurnNonce
    }

    /// @notice Emitted when the owner signals a clear for a custody-tx record.
    event CustodyTxCleared(
        uint256 indexed chainId,
        uint256 indexed nonce,
        ClearAction action,
        bytes32 vouchedTxHash
    );

    error CustodyTxAlreadyCleared(
        uint256 chainId,
        uint256 nonce,
        bytes32 existingHash
    );
    error CustodyTxClearMissingVouch();
    error CustodyTxClearUnexpectedVouch();
    error CustodyTxClearNonceOutOfRange(uint256 chainId, uint256 nonce);
    /// @dev `signNonceBurn` for a slot not cleared as `BurnNonce`. Distinct from
    ///      `CustodyTxClearUnexpectedVouch` so the two failures are tellable apart.
    error CustodyTxClearNotBurnAuthorized(uint256 chainId, uint256 nonce);

    /**
     * @notice Record an owner-authorized, one-shot clear for a custody-tx record
     *         stuck in the off-chain executor's blocking state.
     * @dev Signal + idempotency bus only: stores no record state, trusts the
     *      off-chain proof. First-clear-wins, so a mis-keyed clear is irreversible;
     *      `nonce < nonces[chainId]` is the only on-chain sanity check.
     * @param chainId Destination chain whose custody-EOA nonce is cleared.
     * @param nonce Custody-EOA nonce of the stuck record.
     * @param action Recovery the off-chain executor should apply.
     * @param vouchedTxHash Destination tx hash for `MarkSuccessWithHash`; zero otherwise.
     */
    function clearCustodyTx(
        uint256 chainId,
        uint256 nonce,
        ClearAction action,
        bytes32 vouchedTxHash
    ) external onlyOwner {
        // Bound to allocated nonces: a future-slot write would be a permanent typo.
        if (nonce >= nonces[chainId]) {
            revert CustodyTxClearNonceOutOfRange(chainId, nonce);
        }
        if (clearAppliedHash[chainId][nonce] != bytes32(0)) {
            revert CustodyTxAlreadyCleared(
                chainId,
                nonce,
                clearAppliedHash[chainId][nonce]
            );
        }
        if (action == ClearAction.MarkSuccessWithHash) {
            if (vouchedTxHash == bytes32(0)) revert CustodyTxClearMissingVouch();
        } else {
            if (vouchedTxHash != bytes32(0)) {
                revert CustodyTxClearUnexpectedVouch();
            }
        }
        clearAppliedHash[chainId][nonce] = keccak256(
            abi.encode(action, vouchedTxHash)
        );
        emit CustodyTxCleared(chainId, nonce, action, vouchedTxHash);
    }

    /**
     * @notice Sign a value-0 self-transfer at a specific custody-EOA nonce to
     *         advance past a reserved-but-un-mineable nonce.
     * @dev Owner-gated end-to-end: reverts unless the slot was first cleared with
     *      `ClearAction.BurnNonce`, so ROFL-query auth alone cannot burn arbitrary
     *      nonces. Takes the nonce explicitly (fills a gap; no auto-increment).
     * @param chainId Destination chain the burn tx targets.
     * @param nonce Exact stuck custody-EOA nonce to burn.
     * @return signedTx RLP-encoded signed legacy tx, ready to broadcast.
     */
    function signNonceBurn(
        uint256 chainId,
        uint64 nonce
    ) external view onlyROFLQuery returns (bytes memory signedTx) {
        if (
            clearAppliedHash[chainId][nonce] !=
            keccak256(abi.encode(ClearAction.BurnNonce, bytes32(0)))
        ) {
            revert CustodyTxClearNotBurnAuthorized(chainId, nonce);
        }
        // Zero gas price signs an un-mineable tx; owner must setGasPrice first.
        if (gasPrices[chainId] == 0) revert GasPriceNotSet(chainId);
        signedTx = EIP155Signer.sign(
            evmAddress,
            secretKey,
            EIP155Signer.EthTx({
                nonce: nonce,
                gasPrice: gasPrices[chainId],
                gasLimit: gasLimitNativeSweep,
                to: evmAddress,
                value: 0,
                data: "",
                chainId: chainId
            })
        );
    }

    /**
     * @notice Dispatch routed module selectors to their delegated runtime via
     *         `delegatecall`; revert all other unknown selectors.
     * @dev Allowlists are hardcoded against `IBridgeModule` / `ILockModule`
     *      selector constants. Unknown selectors revert `UnknownSelector(sig)`
     *      — there is no fall-through to a module. UUPS / Ownable / resident
     *      `Accounting` selectors are dispatched normally by Solidity before
     *      this fallback runs, so they cannot be shadowed by an allowlist. Both
     *      module groups share one `delegatecall` block; only the resolved
     *      pointer differs.
     */
    fallback() external {
        bytes4 sig = msg.sig;
        address moduleAddr;
        if (
            sig == IBridgeModule.requestBridgeWithdrawal.selector ||
            sig == IBridgeModule.resolveBridgeWithdrawal.selector ||
            sig == IBridgeModule.setRoflBridge.selector ||
            sig == IBridgeModule.generateSweepERC20TransferToBridge.selector ||
            sig == IBridgeModule.reserveBridgeBurn.selector ||
            sig == IBridgeModule.generateBridgeBurnTransfer.selector ||
            sig == IBridgeModule.getBridgeBurnRequest.selector
        ) {
            bytes32 slot = _BRIDGE_MODULE_SLOT;
            assembly {
                moduleAddr := sload(slot)
            }
            if (moduleAddr == address(0)) revert BridgeModuleNotSet();
        } else if (
            sig == ILockModule.createLock.selector ||
            sig == ILockModule.modifyLock.selector ||
            sig == ILockModule.unlockSingleLock.selector ||
            sig == ILockModule.unlockAllExpiredLocks.selector ||
            sig == ILockModule.transferFromLock.selector ||
            sig == ILockModule.withdrawFromLock.selector ||
            sig == ILockModule.getUserLocks.selector ||
            sig == ILockModule.getServiceLocks.selector
        ) {
            bytes32 slot = _LOCK_MODULE_SLOT;
            assembly {
                moduleAddr := sload(slot)
            }
            if (moduleAddr == address(0)) revert LockModuleNotSet();
        } else {
            revert UnknownSelector(sig);
        }

        assembly {
            calldatacopy(0, 0, calldatasize())
            let ok := delegatecall(gas(), moduleAddr, 0, calldatasize(), 0, 0)
            returndatacopy(0, 0, returndatasize())
            switch ok
            case 0 {
                revert(0, returndatasize())
            }
            default {
                return(0, returndatasize())
            }
        }
    }

    /**
     * @notice Returns all registered token IDs.
     * @return Array of all registered token IDs
     */
    function getRegisteredTokens() external view returns (bytes32[] memory) {
        return registeredTokenIds;
    }

    /**
     * @notice Returns the total number of withdrawal requests.
     *
     * @return The length of the withdrawals array
     */
    function withdrawalCount() external view returns (uint256) {
        return withdrawals.length;
    }

    /**
     * @notice Returns the authenticated user's balance for a token. Requires auth token.
     * @param tokenId The token identifier
     * @param token SIWE auth token identifying the caller
     * @return The authenticated user's balance for `tokenId`
     */
    function balanceOf(
        bytes32 tokenId,
        bytes memory token
    ) public view returns (uint256) {
        address user = _authSender(token);
        if (user == address(0)) revert Unauthorized();
        return balances[user][tokenId];
    }

    /**
     * @notice Total credited supply for a bridge-asset tokenId.
     * @dev Public, unauthenticated read — ledger total is per-token aggregate state,
     *      not user-scoped balance. Only `ROSE_TOKEN_ID` accumulates; every other
     *      tokenId returns 0.
     * @param tokenId The token identifier
     * @return The aggregate ledger total credited for `tokenId`
     */
    function ledgerTotalOf(bytes32 tokenId) external view returns (uint256) {
        return _ledgerTotal[tokenId];
    }

    /// @dev No-op for any tokenId other than `ROSE_TOKEN_ID`: `NativeEVM` and
    ///      `ERC20` deposit paths stay out of the bridge ledger by design.
    ///      `_decreaseLedgerTotal` lives in `BridgeModule` (sole caller is
    ///      `requestBridgeWithdrawal`); leaving it out of `Accounting` saves
    ///      bytecode in the size-pressed root runtime.
    function _increaseLedgerTotal(bytes32 tokenId, uint256 amount) internal {
        if (tokenId == ROSE_TOKEN_ID) _ledgerTotal[tokenId] += amount;
    }
}
