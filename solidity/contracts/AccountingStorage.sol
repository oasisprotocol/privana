// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {EVMSignerAndVerifier} from "./EVMSignerAndVerifier.sol";
import {EIP712SignatureVerifier} from "./EIP712SignatureVerifier.sol";
import {GasPriceNotSet, HistoryEntry, HistoryKind, TokenInfo, TokenType, UnsupportedTokenType, UserInfo} from "./Types.sol";
import {IAccountingHistoryModule} from "./interfaces/IAccountingHistoryModule.sol";

/**
 * @title AccountingStorage
 * @notice Single-source-of-truth storage tail shared by `Accounting` and the
 *         delegated `BridgeModule`. Both contracts inherit this base so the
 *         storage prefix is byte-identical when bridge selectors execute via
 *         `delegatecall` against the Accounting proxy's storage.
 *
 * @dev Slot layout (immediately after `EIP712SignatureVerifier` and
 *      `EVMSignerAndVerifier`, before any UUPS extensions which use
 *      ERC-7201 namespaced storage and contribute zero structured slots):
 *
 *        +0  balances
 *        +1  tokens
 *        +2  processedDeposits
 *        +3  userInfo
 *        +4  withdrawals
 *        +5  nextLockId
 *        +6  registeredTokenIds
 *        +7  emergencyWithdrawRequests
 *        +8  _ledgerTotal
 *        +9  roflBridgeAddress
 *        +10 bridgeBurnRequests
 *        +11 history
 *        +12 historyModule
 *        +13 clearAppliedHash
 *        +14..+48  __gap
 *
 *      Adding a state variable here shifts every downstream slot, so it must
 *      consume a `__gap` slot (as `clearAppliedHash` does) and the structural
 *      prefix-mirror test in `test/AccountingStorageLayout.ts` must still pass
 *      for `Accounting`, `BridgeModule`, and `LockModule` (it walks each child's
 *      slots against this shared base; it is not a checked-in JSON baseline).
 *      `history` storage lives here (not in `Accounting`) so the delegated
 *      `AccountingHistoryModule` reads/appends it at a slot fixed by inheritance.
 */
abstract contract AccountingStorage is
    EIP712SignatureVerifier,
    EVMSignerAndVerifier
{
    // ─── Shared types ─────────────────────────────────────────────────

    struct WithdrawalRequest {
        address userAddress;
        address toAddress;
        uint256 amount;
        uint256 blockNumber;
        bytes32 tokenId;
        bool resolved;
        bytes txIdentifier; // nonce, utxo identifier, or similar
    }

    struct EmergencyWithdrawRequest {
        address toAddress;
        uint256 blockNumber; // 0 ⇒ slot empty
    }

    // ─── Shared state (slot order is the contract surface) ────────────

    /// @dev internal (not private) so MockAccounting test helper can set balances directly.
    mapping(address user => mapping(bytes32 tokenId => uint256 balance))
        internal balances;
    mapping(bytes32 tokenId => TokenInfo tokenInfo) public tokens;
    mapping(bytes32 depositId => bool processed) public processedDeposits;
    mapping(address user => UserInfo) internal userInfo;

    WithdrawalRequest[] public withdrawals;

    uint256 internal nextLockId;

    /// @dev Array of all registered token IDs for enumeration
    bytes32[] internal registeredTokenIds;

    /// @dev requestId = keccak256(abi.encode(beneficiary, tokenId, version)).
    /// Deterministic key ⇒ one pending slot per (beneficiary, token, version).
    /// Re-requesting overwrites; no explicit cancel needed.
    mapping(bytes32 requestId => EmergencyWithdrawRequest)
        public emergencyWithdrawRequests;

    /// @dev Aggregate credited supply per bridge-asset tokenId. Read via
    ///      `Accounting.ledgerTotalOf`; written by `_increaseLedgerTotal` /
    ///      `_decreaseLedgerTotal`, gated on `ROSE_TOKEN_ID`.
    mapping(bytes32 tokenId => uint256) internal _ledgerTotal;

    /// @notice Per-chain address of the off-chain ROFL bridge route.
    /// @dev Set via setRoflBridge (onlyOwner). Unset (zero) entries cause
    ///      `BridgeLib.validateBridgeWithdrawal` to revert RoflBridgeNotSet for that chain.
    mapping(uint256 chainId => address) public roflBridgeAddress;

    /// @dev Burn-side custody-EOA nonce reservations. `nonce` is allocated from
    ///      the same `nonces[chainId]` pool that funds bridge mints, so a Base
    ///      mint and a Base burn cannot collide on the wire. Internal (not
    ///      `public`) to keep the merged Accounting/BridgeModule ABI compact;
    ///      the typed read accessor is `BridgeModule.getBridgeBurnRequest`,
    ///      reached off-chain via the Accounting fallback delegatecall.
    struct BridgeBurnRequest {
        uint256 chainId;
        address bridge;
        uint256 amount;
        uint64 nonce;
        bool exists;
    }

    mapping(bytes32 depositId => BridgeBurnRequest) internal bridgeBurnRequests;

    /// @dev Per-user operation log. Storage owned by the `Accounting` proxy;
    ///      read/append code lives in `AccountingHistoryModule`, which inherits
    ///      this base so it executes against this exact slot via delegatecall.
    mapping(address user => HistoryEntry[] entries) internal history;

    /// @dev Pointer to the delegated `AccountingHistoryModule`. Declared in the
    ///      shared base (not in `Accounting`) so every contract inheriting it —
    ///      `Accounting`, `BridgeModule`, `LockModule` — reads the pointer at an
    ///      identical slot when running history appends via delegatecall.
    ///      Configured through `Accounting.setHistoryModule`.
    address public historyModule;

    /// @notice First-clear-wins coordination flag for custody-tx clears, keyed
    ///         by (destination chainId, custody-EOA nonce).
    /// @dev `keccak256(abi.encode(action, vouchedTxHash))`; zero ⇒ no clear yet.
    ///      In the shared base so the slot is identical across delegatecall modules;
    ///      irreversible once set (no setter zeroes it).
    mapping(uint256 chainId => mapping(uint256 nonce => bytes32))
        public clearAppliedHash;

    /**
     * @dev Reserved storage gap for future upgrades. Allows adding new state
     *      variables without shifting downstream storage layout.
     */
    uint256[35] private __gap;

    // ─── Shared events ────────────────────────────────────────────────

    event Deposit(bytes32 indexed tokenId, uint256 amount, bytes32 depositId);

    event Withdrawal(
        address indexed userAddress,
        bytes32 indexed tokenId,
        uint256 amount,
        uint256 chainId
    );

    event WithdrawalResolved(
        uint256 indexed index,
        address indexed userAddress,
        bytes32 indexed tokenId,
        address toAddress,
        uint256 amount,
        uint256 chainId
    );

    event TokenRegistered(bytes32 indexed tokenId, TokenType tokenType);

    /// @notice Emitted when an off-chain ROFL bridge route is registered or updated for a chain.
    event RoflBridgeUpdated(uint256 indexed chainId, address bridge);

    /// @notice Emitted when a custody-EOA burn nonce is reserved for a deposit.
    /// @dev `depositId` indexed for log lookup parity with `Deposit` /
    ///      `EmergencyWithdrawRequested`. Other fields kept non-indexed since
    ///      they are recovered from the indexed key via the storage struct.
    event BridgeBurnReserved(
        bytes32 indexed depositId,
        uint256 chainId,
        address bridge,
        uint256 amount,
        uint64 nonce
    );

    event EmergencyWithdrawRequested(
        bytes32 indexed requestId,
        bytes32 indexed tokenId
    );
    event EmergencyWithdrawExecuted(bytes32 indexed requestId);

    /// @notice Emitted when the delegated history module pointer is updated.
    event HistoryModuleSet(address indexed module);

    // ─── Shared errors ────────────────────────────────────────────────

    error InsufficientBalance();
    error WithdrawalTooSoon();
    error AddressMismatch();
    error DepositAlreadyProcessed();
    error EmergencyWithdrawTooSoon();
    error EmergencyWithdrawNotFound();
    /// @dev Raised when a `BridgeAsset` (e.g. ROSE) is used in a lock or generic
    ///      `_scheduleWithdrawal` path. BridgeAssets bypass the lock/withdrawal
    ///      model entirely; the only legal exit is `requestBridgeWithdrawal`.
    error BridgeAssetNotSupported();

    /// @dev Raised when the delegated history module pointer is unset, or a
    ///      delegatecall into it reverts without a reason payload.
    error InvalidHistoryModule();

    /// @dev Raised when a private, token-authenticated read cannot resolve a
    ///      caller identity (empty or invalid SIWE token).
    error Unauthorized();

    /// @dev Ownership renunciation is disabled to prevent bricking the proxy.
    ///      Lives here (not in `Accounting`) so the override appears in
    ///      `BridgeModule`'s ABI with the same shape — the merged Accounting +
    ///      BridgeModule ABI must be conflict-free.
    function renounceOwnership() public pure override {
        revert();
    }

    // ─── Shared internal logic ────────────────────────────────────────

    /// @dev Resolves the configured history module, reverting if unset. Shared
    ///      by `Accounting` (deposit/transfer/withdraw history) and `LockModule`
    ///      (lock history) so the delegatecall target is read from one slot.
    function _historyModule()
        internal
        view
        returns (address historyModuleAddress)
    {
        historyModuleAddress = historyModule;
        if (historyModuleAddress == address(0)) {
            revert InvalidHistoryModule();
        }
    }

    /// @dev Delegatecalls the configured history module against this proxy's
    ///      storage. The target is the owner-set, contract-checked
    ///      `historyModule` pointer (`Accounting.setHistoryModule`); this is a
    ///      reviewed-safe delegatecall, annotated so upgrade validation passes
    ///      for every contract that inherits this shared base.
    /// @custom:oz-upgrades-unsafe-allow delegatecall
    function _delegateHistory(
        bytes memory data
    ) internal returns (bytes memory result) {
        // solhint-disable-next-line avoid-low-level-calls
        (bool ok, bytes memory delegateResult) = _historyModule().delegatecall(
            data
        );
        if (!ok) {
            if (delegateResult.length == 0) {
                revert InvalidHistoryModule();
            }
            assembly {
                revert(add(delegateResult, 0x20), mload(delegateResult))
            }
        }
        return delegateResult;
    }

    function _appendHistory(
        address user,
        HistoryKind kind,
        bytes memory payload
    ) internal {
        _delegateHistory(
            abi.encodeCall(
                IAccountingHistoryModule.appendHistory,
                (user, kind, payload)
            )
        );
    }

    function _appendUserCounterpartyHistory(
        address user,
        HistoryKind kind,
        bytes32 tokenId,
        uint256 amount,
        address counterparty
    ) internal {
        _appendHistory(
            user,
            kind,
            abi.encodePacked(tokenId, amount, counterparty)
        );
    }

    function _appendTransferHistoryForParticipants(
        address fromAddress,
        address toAddress,
        HistoryKind kind,
        bytes32 tokenId,
        uint256 amount
    ) internal {
        bytes memory payload = abi.encodePacked(
            tokenId,
            amount,
            fromAddress,
            toAddress
        );
        _delegateHistory(
            abi.encodeCall(
                IAccountingHistoryModule.appendTransferHistory,
                (fromAddress, toAddress, kind, payload)
            )
        );
    }

    /// @dev Debits-already-done withdrawal enqueue shared by
    ///      `Accounting.requestWithdrawal` and `LockModule.withdrawFromLock`.
    ///      Reserves the destination-EOA nonce and records a `WithdrawalRequest`
    ///      resolvable later via `resolveWithdrawal`.
    function _scheduleWithdrawal(
        address userAddress,
        address toAddress,
        bytes32 tokenId,
        uint256 amount
    ) internal {
        TokenInfo memory tInfo = tokens[tokenId];
        if (tInfo.tokenType == TokenType.BridgeAsset)
            revert BridgeAssetNotSupported();

        bytes memory txIdentifier;
        uint256 chainId;

        if (tInfo.tokenType == TokenType.NativeEVM) {
            chainId = EVMSignerAndVerifier.decodeEVMNativeTokenData(tInfo.data);
            if (gasPrices[chainId] == 0) revert GasPriceNotSet(chainId);

            txIdentifier = abi.encode(getEVMNonceAndIncrement(chainId));
        } else if (tInfo.tokenType == TokenType.ERC20) {
            (chainId, ) = EVMSignerAndVerifier.decodeEVMErc20TokenData(
                tInfo.data
            );
            if (gasPrices[chainId] == 0) revert GasPriceNotSet(chainId);

            txIdentifier = abi.encode(getEVMNonceAndIncrement(chainId));
        } else {
            revert UnsupportedTokenType();
        }

        withdrawals.push(
            WithdrawalRequest({
                userAddress: userAddress,
                toAddress: toAddress,
                amount: amount,
                blockNumber: block.number,
                tokenId: tokenId,
                txIdentifier: txIdentifier,
                resolved: false
            })
        );

        emit Withdrawal(userAddress, tokenId, amount, chainId);
    }
}
