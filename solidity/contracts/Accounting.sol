// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {EVMSignerAndVerifier} from "./EVMSignerAndVerifier.sol";
import {EIP712SignatureVerifier} from "./EIP712SignatureVerifier.sol";
import {BridgeLib, RoflBridgeNotSet, InvalidRouteAddress} from "./BridgeLib.sol";
import {ChainType, FundLock, GasPriceNotSet, HistoryEntry, HistoryKind, InvalidAddress, InvalidAmount, TokenInfo, TokenType, UnsupportedTokenType, UserInfo} from "./Types.sol";
import {IAccountingSiweAuth} from "./interfaces/IAccountingSiweAuth.sol";
import {EIP155Signer} from "@oasisprotocol/sapphire-contracts/contracts/EIP155Signer.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

/**
 * @title Accounting
 * @notice Cross-chain accounting module for managing user balances and fund operations.
 *
 * Deposits verified off-chain by ROFL TEE, credited via onlyROFL. Per-user deposit
 * addresses derived on-chain from contract's secretKey. Fund locking, P2P transfers,
 * automated withdrawals, the ROSE bridge, and per-user history are all provided here.
 * `BridgeLib` is an internal library holding the bridge validation and signing
 * helpers.
 */
contract Accounting is
    EIP712SignatureVerifier,
    EVMSignerAndVerifier,
    UUPSUpgradeable
{
    /// @notice Chain-agnostic tokenId for bridge-asset ROSE.
    /// @dev Equals getTokenId(TokenInfo(TokenType.BridgeAsset, encodeBridgeAssetTokenData("ROSE"))).
    ///      Pinned literal so off-chain consumers (ROFL TEE) can read a stable value
    ///      without recomputing the hash.
    bytes32 public constant ROSE_TOKEN_ID =
        0xca91975d6c6810eb4077546d4fbdb49fa231f351cddfc915862f7c0dad81a7aa;

    /// @dev Upper bound on entries returned by a single `getHistory` page.
    uint256 private constant MAX_HISTORY_PAGE_SIZE = 100;

    /// @custom:oz-upgrades-unsafe-allow state-variable-immutable
    IAccountingSiweAuth public immutable siweAuth;

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

    /// @dev Burn-side custody-EOA nonce reservations. `nonce` is allocated from
    ///      the same `nonces[chainId]` pool that funds bridge mints, so a Base
    ///      mint and a Base burn cannot collide on the wire. The mapping is
    ///      `internal` for ABI compactness; the typed read accessor is
    ///      `getBridgeBurnRequest`.
    struct BridgeBurnRequest {
        uint256 chainId;
        address bridge;
        uint256 amount;
        uint64 nonce;
        bool exists;
    }

    // ─── State ────────────────────────────────────────────────────────

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
    ///      `ledgerTotalOf`; written by `_increaseLedgerTotal` and the ROSE debit
    ///      in `requestBridgeWithdrawal`, both gated on `ROSE_TOKEN_ID`.
    mapping(bytes32 tokenId => uint256) internal _ledgerTotal;

    /// @notice Per-chain address of the off-chain ROFL bridge route.
    /// @dev Set via setRoflBridge (onlyROFL). Unset (zero) entries cause
    ///      `BridgeLib.validateBridgeWithdrawal` to revert RoflBridgeNotSet for that chain.
    mapping(uint256 chainId => address) public roflBridgeAddress;

    mapping(bytes32 depositId => BridgeBurnRequest) internal bridgeBurnRequests;

    /// @dev Per-user operation log, appended by the internal `_appendHistory`
    ///      helpers and read by `getHistory`.
    mapping(address user => HistoryEntry[] entries) internal history;

    /// @notice First-clear-wins coordination flag for custody-tx clears, keyed
    ///         by (destination chainId, custody-EOA nonce).
    /// @dev `keccak256(abi.encode(action, vouchedTxHash))`; zero ⇒ no clear yet.
    ///      Irreversible once set (no setter zeroes it).
    mapping(uint256 chainId => mapping(uint256 nonce => bytes32))
        public clearAppliedHash;

    /**
     * @dev Reserved storage gap for future upgrades. Allows adding new state
     *      variables without shifting downstream storage layout.
     */
    uint256[50] private __gap;

    // ─── Events ───────────────────────────────────────────────────────

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

    // ─── Errors ───────────────────────────────────────────────────────

    error InvalidSiweAuth();
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

    /// @dev Raised when a private, token-authenticated read cannot resolve a
    ///      caller identity (empty or invalid SIWE token).
    error Unauthorized();

    // Bridge-specific errors
    /// @dev Reservation rejected: depositId is the zero hash.
    error InvalidDepositId();
    /// @dev Re-call with the same depositId but at least one mismatched field.
    ///      `depositId` is included so log filters can identify the offending key.
    error BridgeBurnMismatch(bytes32 depositId);
    /// @dev Burn signing requested for a depositId with no reservation.
    error BridgeBurnNotFound(bytes32 depositId);

    // Lock-specific errors
    error TooManyActiveLocks();
    error InvalidLockId();
    error LockNotExpired();
    error InsufficientLockedAmount();
    error InvalidExpiry();

    /**
     * @notice Sets the immutable SIWE auth contract and locks the implementation.
     * @dev Disables initializers so the logic contract cannot be initialized directly.
     * @param siweAuthAddress Address of the SIWE auth contract; must be non-zero.
     * @custom:oz-upgrades-unsafe-allow constructor
     * @custom:oz-upgrades-unsafe-allow state-variable-immutable
     */
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

    /**
     * @dev Ownership renunciation is disabled to prevent bricking the proxy.
     */
    function renounceOwnership() public pure override {
        revert();
    }

    /**
     * @dev Resolves the caller identity: the SIWE-authenticated address when a
     *      token is supplied, otherwise `msg.sender`.
     * @param token SIWE auth token; empty to fall back to `msg.sender`.
     * @return The resolved caller address.
     */
    function _authSender(bytes memory token) internal view returns (address) {
        if (token.length != 0) {
            return siweAuth.authSender(token);
        }
        return msg.sender;
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

    /**
     * @dev Body of `creditDeposit` factored out so test mocks can exercise the
     *      exact production logic without re-implementing it.
     * @param beneficiary The address to credit
     * @param tokenId The token identifier
     * @param amount The deposit amount (verified off-chain by TEE)
     * @param depositId Unique deposit identifier: keccak256(chainId, txHash, tokenId, depositIndex)
     */
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

        _appendUserCounterpartyHistory(
            userAddress,
            HistoryKind.TransferBalanceOut,
            tokenId,
            amount,
            toAddress
        );
        if (toAddress != address(0) && toAddress != userAddress) {
            _appendUserCounterpartyHistory(
                toAddress,
                HistoryKind.TransferBalanceIn,
                tokenId,
                amount,
                userAddress
            );
        }
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

    /**
     * @dev Idempotent finalize: flips `resolved` and emits `WithdrawalResolved` only on
     *      first call. Shared by `resolveWithdrawal` and `resolveBridgeWithdrawal`; the
     *      divergent block-delay gate lives in the callers (only `resolveWithdrawal`
     *      enforces `WithdrawalTooSoon`).
     * @param req Withdrawal request slot to finalize.
     * @param index Index of `req` in the `withdrawals` array (emitted in the event).
     * @param chainId Destination chain id (emitted in the event).
     */
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

    // ─── ROSE bridge ──────────────────────────────────────────────────

    /**
     * @notice Register or update the off-chain ROFL bridge route for a destination chain.
     * @dev ROFL-only. Reverts `InvalidAddress` if `bridge` is zero — a route can be
     *      repointed but not cleared. `virtual` so `MockAccounting` can drop
     *      `onlyROFL` on Hardhat (Sapphire `roflEnsureAuthorizedOrigin` precompile is
     *      unavailable there); the mock override re-uses `_setRoflBridge` so test and
     *      production bodies cannot drift.
     * @param chainId  Destination chain id (e.g. 84532 for Base Sepolia).
     * @param bridge   ROFLBridge contract used to mint to `chainId`; must be non-zero.
     */
    function setRoflBridge(
        uint256 chainId,
        address bridge
    ) external virtual onlyROFL {
        _setRoflBridge(chainId, bridge);
    }

    /**
     * @dev Body of `setRoflBridge`. Split out so `MockAccounting` can bypass
     *      `onlyROFL` (Sapphire precompile) on Hardhat — same shape as
     *      `_reserveBridgeBurn`.
     * @param chainId Destination chain id (e.g. 84532 for Base Sepolia).
     * @param bridge ROFLBridge contract used to mint to `chainId`; must be non-zero.
     */
    function _setRoflBridge(uint256 chainId, address bridge) internal {
        if (bridge == address(0)) revert InvalidAddress();
        roflBridgeAddress[chainId] = bridge;
        emit RoflBridgeUpdated(chainId, bridge);
    }

    /**
     * @notice Reserve a Base custody-EOA tx nonce for a queued xROSE burn.
     * @dev ROFL-only. Idempotent on identical re-call (no state change, no second
     *      event). The reserved nonce is allocated from the same `nonces[chainId]`
     *      pool used by `requestBridgeWithdrawal` so a Base mint and a Base burn
     *      cannot collide on the wire. Off-chain consumers read the reservation via
     *      `getBridgeBurnRequest` (canonical state) and `BridgeBurnReserved` events
     *      as the audit trail; the storage mapping itself is `internal` for ABI
     *      compactness. `virtual` so `MockAccounting` can drop `onlyROFL` on Hardhat
     *      (Sapphire `roflEnsureAuthorizedOrigin` precompile is unavailable there);
     *      the mock override re-uses `_reserveBridgeBurn` so test and production
     *      bodies cannot drift.
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
    ) external virtual onlyROFL {
        _reserveBridgeBurn(depositId, chainId, bridge, amount);
    }

    /**
     * @dev Body of `reserveBridgeBurn`. Split out so `MockAccounting` can
     *      bypass `onlyROFL` (Sapphire precompile) on Hardhat — same shape as
     *      `mockCreditDeposit -> _creditDeposit`.
     * @param depositId Caller-supplied identifier for the burn (one slot per id).
     * @param chainId Destination chain id; must have a registered ROFL bridge route via `setRoflBridge`.
     * @param bridge Must equal `roflBridgeAddress[chainId]` at reservation time.
     * @param amount Burn amount; must be > 0.
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
    ) external view onlyROFLQuery returns (bytes memory signedTx) {
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
    ) external {
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

        // ROSE-only bridge debit; mirrors _increaseLedgerTotal on credit.
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
    ) external returns (bytes memory signedTx) {
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

    /**
     * @notice Sign an xROSE burn tx for a previously reserved Base custody-EOA nonce.
     * @dev ROFL-only (signed query). Reads `BridgeBurnRequest` from storage and uses
     *      every field — chain, target bridge, amount, nonce — from the reservation;
     *      the caller supplies only `depositId`. Closes a stale-amount injection by
     *      a compromised orchestrator. Reverts `BridgeBurnNotFound(depositId)` if
     *      `bridgeBurnRequests[depositId].exists == false`. Reverts
     *      `GasPriceNotSet(r.chainId)` if `gasPrices[r.chainId] == 0`. Signer is the
     *      shared Accounting custody EOA (`evmAddress`).
     * @param depositId  Reservation identifier set by `reserveBridgeBurn`.
     * @return signedTx  RLP-encoded signed tx ready for broadcast on the reserved destination chain.
     */
    function generateBridgeBurnTransfer(
        bytes32 depositId
    ) external view onlyROFLQuery returns (bytes memory signedTx) {
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
        )
    {
        BridgeBurnRequest memory r = bridgeBurnRequests[depositId];
        return (r.chainId, r.bridge, r.amount, r.nonce, r.exists);
    }

    // ─── Locks ────────────────────────────────────────────────────────

    /**
     * @notice Creates a lock on user funds for exclusive access by a designated service.
     *
     * This function allows users to lock a portion of their funds for use by a specific
     * service. Locked funds are removed from the user's available balance but remain
     * owned by the user until the lock expires or the service transfers them.
     *
     * The locking mechanism enables:
     * - Escrow-like functionality for service interactions
     * - Temporary delegation of fund access to trusted services
     * - Time-bounded locks that automatically expire
     * - Multiple concurrent locks per user (up to 10)
     *
     * Security features:
     * - EIP-712 signature verification to authorize the lock
     * - Expiry timestamp validation to ensure locks are created with future expiry
     * - Balance verification before locking
     * - Limited number of active locks per user (max 10)
     *
     * @dev The signature must be from the user whose funds are being locked.
     *      Locked funds are stored in the user's activeLocks array.
     *      The expiry must be a timestamp in the future (> block.timestamp).
     *
     * @param serviceAddress The address of the service that will have access to the locked funds
     * @param tokenId The identifier of the token to lock
     * @param amount The amount of tokens to lock
     * @param expiry The timestamp when the lock expires and funds can be reclaimed (must be in future)
     * @param nonce The nonce for replay protection (must match user's current createLockNonces)
     * @param signature The EIP-712 signature from the user authorizing the lock
     */
    function createLock(
        address serviceAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 expiry,
        uint256 nonce,
        bytes calldata signature
    ) external {
        if (expiry <= block.timestamp) revert InvalidExpiry();
        if (amount == 0) revert InvalidAmount();
        if (tokens[tokenId].tokenType == TokenType.BridgeAsset)
            revert BridgeAssetNotSupported();

        address userAddress = EIP712SignatureVerifier.verifyLockSignature(
            serviceAddress,
            tokenId,
            amount,
            expiry,
            nonce,
            signature
        );

        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        if (locks.length >= 10) revert TooManyActiveLocks();

        if (balances[userAddress][tokenId] < amount)
            revert InsufficientBalance();

        balances[userAddress][tokenId] -= amount;

        uint256 lockId = nextLockId++;
        locks.push(
            FundLock({
                lockId: lockId,
                serviceId: serviceAddress,
                tokenId: tokenId,
                amount: amount,
                expiry: expiry
            })
        );

        _appendUserCounterpartyHistory(
            userAddress,
            HistoryKind.CreateLock,
            tokenId,
            amount,
            serviceAddress
        );
    }

    /**
     * @dev Linear search for the array index of `lockId`; reverts `InvalidLockId`
     *      if no matching lock exists.
     * @param locks The user's active locks array to search.
     * @param lockId The lock identifier to locate.
     * @return The index of the matching lock within `locks`.
     */
    function _findLockIndex(
        FundLock[] storage locks,
        uint256 lockId
    ) internal view returns (uint256) {
        for (uint256 i = 0; i < locks.length; i++) {
            if (locks[i].lockId == lockId) {
                return i;
            }
        }
        revert InvalidLockId();
    }

    /**
     * @notice Modifies an existing lock by increasing the locked amount and/or extending expiry.
     * @dev Expiry can only be extended (newExpiry >= current expiry). Amount increases are
     *      drawn from the user's available balance. Authorized via EIP-712 user signature.
     * @param lockId The unique identifier of the lock to modify
     * @param amount Additional amount to add to the lock (0 to only extend expiry)
     * @param newExpiry The new expiry timestamp for the lock
     * @param nonce The nonce for replay protection (must match user's current modifyLockNonces)
     * @param signature The EIP-712 signature from the user authorizing the modification
     */
    function modifyLock(
        uint256 lockId,
        uint256 amount,
        uint256 newExpiry,
        uint256 nonce,
        bytes calldata signature
    ) external {
        address userAddress = EIP712SignatureVerifier.verifyModifyLockSignature(
            lockId,
            amount,
            newExpiry,
            nonce,
            signature
        );

        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        uint256 lockIndex = _findLockIndex(locks, lockId);
        FundLock storage lock = locks[lockIndex];

        if (tokens[lock.tokenId].tokenType == TokenType.BridgeAsset)
            revert BridgeAssetNotSupported();

        if (newExpiry < lock.expiry) revert InvalidExpiry();

        if (amount == 0 && newExpiry == lock.expiry) revert InvalidAmount();

        if (amount > 0) {
            if (balances[userAddress][lock.tokenId] < amount)
                revert InsufficientBalance();

            balances[userAddress][lock.tokenId] -= amount;
            lock.amount += amount;
        }

        lock.expiry = newExpiry;
        _appendUserCounterpartyHistory(
            userAddress,
            HistoryKind.ModifyLock,
            lock.tokenId,
            amount,
            lock.serviceId
        );
    }

    /**
     * @notice Unlocks a single expired fund lock and returns funds to the user's available balance.
     *
     * This function allows users to reclaim funds from an expired lock. Once a lock's
     * expiry timestamp has passed, the original user can call this function to
     * unlock the funds and restore them to their available balance.
     *
     * The unlocking process:
     * 1. Validates the lock index exists
     * 2. Checks that the lock has expired (block.timestamp >= expiry)
     * 3. Returns any remaining locked amount to the user's balance
     * 4. Removes the lock from the user's active locks array
     *
     * Security features:
     * - Time-based expiry validation to prevent premature unlocking
     * - Index bounds checking to prevent invalid access
     * - Efficient lock removal using swap-and-pop pattern
     *
     * @dev Uses swap-and-pop to remove locks efficiently from the array.
     *      The lock order may change after removal due to the swap operation.
     *      Anyone can call this function for any user if the lock has expired.
     *      The purpose of this function is to allow users to reclaim funds if
     *      a service goes down or becomes unresponsive.
     *
     * @param userAddress The address of the user whose lock should be unlocked
     * @param lockId The unique identifier of the lock to unlock
     */
    function unlockSingleLock(
        address userAddress,
        uint256 lockId
    ) external {
        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        uint256 lockIndex = _findLockIndex(locks, lockId);
        FundLock memory lock = locks[lockIndex];

        if (lock.amount != 0) {
            if (block.timestamp < lock.expiry) revert LockNotExpired();
            balances[userAddress][lock.tokenId] += lock.amount;
            _appendUserCounterpartyHistory(
                userAddress,
                HistoryKind.UnlockLock,
                lock.tokenId,
                lock.amount,
                lock.serviceId
            );
        }

        locks[lockIndex] = locks[locks.length - 1];
        locks.pop();
    }

    /**
     * @notice Unlocks all expired fund locks for a user and returns funds to available balance.
     *
     * This function iterates through all of a user's active locks and unlocks any that
     * have expired. This is a convenience function to avoid calling unlockSingleLock
     * multiple times when a user has several expired locks.
     *
     * @dev Iterates backwards to handle swap-and-pop without skipping elements.
     *      Anyone can call this function for any user.
     *
     * @param userAddress The address of the user whose expired locks should be unlocked
     * @return unlockedCount The number of locks that were successfully unlocked
     */
    function unlockAllExpiredLocks(
        address userAddress
    ) external returns (uint256 unlockedCount) {
        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        unlockedCount = 0;
        uint256 i = locks.length;

        while (i > 0) {
            i--;
            FundLock memory lock = locks[i];

            if (block.timestamp >= lock.expiry && lock.amount > 0) {
                balances[userAddress][lock.tokenId] += lock.amount;
                _appendUserCounterpartyHistory(
                    userAddress,
                    HistoryKind.UnlockLock,
                    lock.tokenId,
                    lock.amount,
                    lock.serviceId
                );

                locks[i] = locks[locks.length - 1];
                locks.pop();
                unlockedCount++;
            }
        }

        return unlockedCount;
    }

    /**
     * @notice Transfers locked funds under service authorization.
     *
     * This function allows a service to transfer funds that were previously locked
     * to them by a user. Unlike regular transfers, this requires authorization from
     * the service (not the original user) since the funds are under the service's
     * temporary control.
     *
     * The transfer process:
     * 1. Validates the lock index and retrieves the lock details
     * 2. Verifies the service's EIP-712 signature authorizing the transfer
     * 3. Checks that the lock has sufficient funds for the transfer
     * 4. Reduces the locked amount and credits the recipient
     * 5. Removes the lock if all funds are transferred
     *
     * Security features:
     * - Service signature verification (not user signature)
     * - Lock amount validation before transfer
     * - Automatic lock cleanup when empty
     * - Signature replay protection via EIP712SignatureVerifier
     *
     * @dev The signature must be from the service address associated with the lock.
     *      If the lock amount reaches zero, the lock is automatically removed.
     *      The lock array may be reordered due to swap-and-pop removal. The service is
     *      a user (managed the same way as regular users) and any user can act as a service.
     *
     * @param userAddress The address of the user who originally locked the funds
     * @param toAddress The address receiving the transferred locked funds
     * @param lockId The unique identifier of the lock to transfer from
     * @param amount The amount of locked tokens to transfer
     * @param nonce The nonce for replay protection (must match service's current transferLockedNonces)
     * @param signature The EIP-712 signature from the service authorizing the transfer
     */
    function transferFromLock(
        address userAddress,
        address toAddress,
        uint256 lockId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) external {
        if (amount == 0) revert InvalidAmount();

        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        uint256 lockIndex = _findLockIndex(locks, lockId);
        FundLock storage lock = locks[lockIndex];

        EIP712SignatureVerifier.verifyTransferLockedSignature(
            lock.serviceId,
            userAddress,
            toAddress,
            lockId,
            amount,
            nonce,
            signature
        );

        if (lock.amount < amount) revert InsufficientLockedAmount();

        lock.amount -= amount;
        balances[toAddress][lock.tokenId] += amount;

        _appendUserCounterpartyHistory(
            userAddress,
            HistoryKind.TransferFromLockOut,
            lock.tokenId,
            amount,
            toAddress
        );
        if (toAddress != address(0) && toAddress != userAddress) {
            _appendUserCounterpartyHistory(
                toAddress,
                HistoryKind.TransferFromLockIn,
                lock.tokenId,
                amount,
                userAddress
            );
        }

        if (lock.amount == 0) {
            locks[lockIndex] = locks[locks.length - 1];
            locks.pop();
        }
    }

    /**
     * @notice Withdraws locked funds to an external on-chain address via scheduled withdrawal.
     * @dev Service-authorized counterpart to `transferFromLock`: instead of crediting an
     *      internal balance, this schedules a cross-chain withdrawal signed by ROFL.
     *      Signature must come from the lock's serviceId. If `lock.amount` hits zero the
     *      slot is removed via swap-and-pop.
     * @param userAddress The user who originally created the lock
     * @param toAddress The external destination address on the token's source chain
     * @param lockId The unique identifier of the lock to withdraw from
     * @param amount The amount of locked tokens to withdraw
     * @param nonce The nonce for replay protection (must match service's current withdrawFromLock nonce)
     * @param signature The EIP-712 signature from the service authorizing the withdrawal
     */
    function withdrawFromLock(
        address userAddress,
        address toAddress,
        uint256 lockId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) external {
        if (amount == 0) revert InvalidAmount();
        if (toAddress == address(0)) revert AddressMismatch();

        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        uint256 lockIndex = _findLockIndex(locks, lockId);
        FundLock storage lock = locks[lockIndex];

        if (tokens[lock.tokenId].tokenType == TokenType.BridgeAsset)
            revert BridgeAssetNotSupported();

        EIP712SignatureVerifier.verifyWithdrawFromLockSignature(
            lock.serviceId,
            userAddress,
            toAddress,
            lockId,
            amount,
            nonce,
            signature
        );

        if (lock.amount < amount) revert InsufficientLockedAmount();

        lock.amount -= amount;

        bytes32 tokenId = lock.tokenId;

        if (lock.amount == 0) {
            locks[lockIndex] = locks[locks.length - 1];
            locks.pop();
        }

        _scheduleWithdrawal(userAddress, toAddress, tokenId, amount);
        _appendUserCounterpartyHistory(
            userAddress,
            HistoryKind.Withdraw,
            tokenId,
            amount,
            toAddress
        );
    }

    /**
     * @notice Returns active locks for the authenticated user. Requires auth token for private reads.
     * @param token SIWE auth token identifying the caller
     * @return Array of active fund locks owned by the authenticated user
     */
    function getUserLocks(
        bytes calldata token
    ) external view returns (FundLock[] memory) {
        address user = _authSender(token);
        if (user == address(0)) revert Unauthorized();
        return userInfo[user].activeLocks;
    }

    /**
     * @notice Returns locks for `user` scoped to authenticated service identity.
     * @param user The lock owner whose locks should be filtered
     * @param token SIWE auth token identifying the caller as the service
     * @return Array of `user`'s locks where serviceId matches the authenticated caller
     */
    function getServiceLocks(
        address user,
        bytes calldata token
    ) external view returns (FundLock[] memory) {
        address service = _authSender(token);
        if (service == address(0)) revert Unauthorized();

        UserInfo storage uInfo = userInfo[user];
        FundLock[] storage allLocks = uInfo.activeLocks;

        uint256 matchCount = 0;
        for (uint256 i = 0; i < allLocks.length; i++) {
            if (allLocks[i].serviceId == service) {
                matchCount++;
            }
        }

        FundLock[] memory serviceLocks = new FundLock[](matchCount);
        uint256 currentIndex = 0;
        for (uint256 i = 0; i < allLocks.length; i++) {
            if (allLocks[i].serviceId == service) {
                serviceLocks[currentIndex] = allLocks[i];
                currentIndex++;
            }
        }

        return serviceLocks;
    }

    // ─── History ──────────────────────────────────────────────────────

    /**
     * @notice Returns a page of the authenticated user's operation history.
     * @param offset Page index; negative counts pages from the most recent.
     * @param limit Page size, capped at `MAX_HISTORY_PAGE_SIZE`.
     * @param token SIWE auth token identifying the caller.
     * @return page The history entries for the requested page.
     * @return total The total number of history entries for the user.
     */
    function getHistory(
        int256 offset,
        uint256 limit,
        bytes calldata token
    ) external view returns (HistoryEntry[] memory page, uint256 total) {
        address user = _authSender(token);
        if (user == address(0)) revert Unauthorized();
        return _getHistory(user, offset, limit);
    }

    /**
     * @dev Append one history entry for `user`. Internal-only: there is no
     *      external append surface, so history cannot be forged by an outside
     *      caller.
     * @param user The account whose history is appended.
     * @param kind The history entry kind.
     * @param payload The ABI-packed entry payload.
     */
    function _appendHistory(
        address user,
        HistoryKind kind,
        bytes memory payload
    ) internal {
        history[user].push(
            HistoryEntry({
                kind: kind,
                timestamp: uint64(block.timestamp),
                payload: payload
            })
        );
    }

    /**
     * @dev Appends a single history entry for `user` packing the token, amount,
     *      and counterparty.
     * @param user The account whose history is appended.
     * @param kind The history entry kind.
     * @param tokenId The token involved in the operation.
     * @param amount The operation amount.
     * @param counterparty The other party to the operation.
     */
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

    /**
     * @dev Paginates `history[user]`. Page size is capped at
     *      `MAX_HISTORY_PAGE_SIZE`; a negative `offset` counts pages from the
     *      most recent. Out-of-range pages return an empty `page`.
     * @param user The account whose history is read.
     * @param offset Page index; negative counts pages from the most recent.
     * @param limit Page size, capped at `MAX_HISTORY_PAGE_SIZE`.
     * @return page The history entries for the requested page.
     * @return total The total number of history entries for `user`.
     */
    function _getHistory(
        address user,
        int256 offset,
        uint256 limit
    ) internal view returns (HistoryEntry[] memory page, uint256 total) {
        HistoryEntry[] storage all = history[user];
        total = all.length;

        uint256 pageSize = limit > MAX_HISTORY_PAGE_SIZE
            ? MAX_HISTORY_PAGE_SIZE
            : limit;
        if (total == 0 || pageSize == 0) {
            return (new HistoryEntry[](0), total);
        }

        uint256 pageCount = (total + pageSize - 1) / pageSize;
        uint256 start;
        if (offset < 0) {
            // Avoid negating type(int256).min.
            uint256 pageFromEnd = uint256(-(offset + 1)) + 1;
            if (pageFromEnd > pageCount) {
                return (new HistoryEntry[](0), total);
            }
            start = (pageCount - pageFromEnd) * pageSize;
        } else {
            if (uint256(offset) >= pageCount) {
                return (new HistoryEntry[](0), total);
            }
            start = uint256(offset) * pageSize;
        }

        if (total - start < pageSize) {
            pageSize = total - start;
        }

        page = new HistoryEntry[](pageSize);
        for (uint256 i = 0; i < pageSize; ) {
            page[i] = all[start + i];
            unchecked {
                ++i;
            }
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

    // ─── Shared internal logic ────────────────────────────────────────

    /**
     * @dev Debits-already-done withdrawal enqueue shared by `requestWithdrawal`
     *      and `withdrawFromLock`. Reserves the destination-EOA nonce and records
     *      a `WithdrawalRequest` resolvable later via `resolveWithdrawal`.
     * @param userAddress The user the withdrawal is recorded against.
     * @param toAddress The destination address on the token's source chain.
     * @param tokenId The token being withdrawn.
     * @param amount The withdrawal amount (already debited by the caller).
     */
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

    /**
     * @dev No-op for any tokenId other than `ROSE_TOKEN_ID`: `NativeEVM` and
     *      `ERC20` deposit paths stay out of the bridge ledger by design.
     * @param tokenId The token whose ledger total may increase.
     * @param amount The amount to add when `tokenId` is `ROSE_TOKEN_ID`.
     */
    function _increaseLedgerTotal(bytes32 tokenId, uint256 amount) internal {
        if (tokenId == ROSE_TOKEN_ID) _ledgerTotal[tokenId] += amount;
    }

    // ─── Views ────────────────────────────────────────────────────────

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
}
