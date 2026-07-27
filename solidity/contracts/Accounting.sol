// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ROFLableUpgradeable} from "@oasisprotocol/sapphire-contracts/contracts/ROFLableUpgradeable.sol";

import {EVMSignerVerifier} from "./EVMSignerVerifier.sol";
import {EIP712Verifier} from "./EIP712Verifier.sol";
import {
    ChainType,
    FundLock,
    HistoryEntry,
    HistoryKind,
    TokenInfo,
    TokenType,
    UnsupportedTokenType,
    UserInfo
} from "./Types.sol";
import {ISiweAuth} from "./interfaces/ISiweAuth.sol";
import {OwnableUpgradeable} from "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

/**
 * @title Accounting
 * @notice Cross-chain accounting module for managing user balances and fund operations.
 *
 * Deposits verified off-chain by ROFL TEE, credited via onlyROFL. Per-user deposit
 * addresses derived on-chain from contract's secretKey. Fund locking, P2P transfers,
 * and automated withdrawals via EIP-712 signatures.
 */
contract Accounting is OwnableUpgradeable, ROFLableUpgradeable, UUPSUpgradeable {
    /// @notice Contract version, bumped on each upgrade for tracking/verification.
    string public constant VERSION = "1.0.0";

    /// @dev Maximum entries returned by `getHistory` in a single call.
    uint256 private constant MAX_HISTORY_PAGE_SIZE = 100;

    ISiweAuth public siweAuth;
    EIP712Verifier public eip712Verifier;
    EVMSignerVerifier public evmSignerVerifier;

    /// @dev internal (not private) so MockAccounting test helper can set balances directly.
    mapping(address user => mapping(bytes32 tokenId => uint256 balance))
        internal balances;
    mapping(bytes32 tokenId => TokenInfo tokenInfo) public tokens;
    mapping(bytes32 depositId => bool processed) public processedDeposits;
    mapping(address user => UserInfo) private userInfo;

    WithdrawalRequest[] public withdrawals;

    uint256 private nextLockId;

    /// @dev Array of all registered token IDs for enumeration
    bytes32[] private registeredTokenIds;

    /// @dev requestId = keccak256(abi.encode(beneficiary, tokenId, version)).
    /// Deterministic key ⇒ one pending slot per (beneficiary, token, version).
    /// Re-requesting overwrites; no explicit cancel needed.
    mapping(bytes32 requestId => EmergencyWithdrawRequest) public emergencyWithdrawRequests;
    mapping(address user => HistoryEntry[] entries) private history;

    struct EmergencyWithdrawRequest {
        address toAddress;
        uint256 blockNumber; // 0 ⇒ slot empty
    }

    error EmergencyWithdrawTooSoon();
    error EmergencyWithdrawNotFound();

    event EmergencyWithdrawRequested(bytes32 indexed requestId, bytes32 indexed tokenId);
    event EmergencyWithdrawExecuted(bytes32 indexed requestId);

    event Deposit(
        bytes32 indexed tokenId,
        uint256 amount,
        bytes32 depositId
    );

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

    error InsufficientBalance();
    error TooManyActiveLocks();
    error InvalidLockId();
    error LockNotExpired();
    error InsufficientLockedAmount();
    error AddressMismatch();
    error InvalidExpiry();
    error InvalidAmount();
    error WithdrawalTooSoon();
    error Unauthorized();
    error DepositAlreadyProcessed();
    error InvalidSiweAuth();
    error InvalidEip712Verifier();
    error InvalidEvmSignerVerifier();
    error UpgradeCallDataNotAllowed();

    struct WithdrawalRequest {
        address userAddress;
        address toAddress;
        uint256 amount;
        uint256 blockNumber;
        bytes32 tokenId;
        bool resolved;
        bytes txIdentifier; // nonce, utxo identifier, or similar
    }

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    /**
     * @notice Initializes the Accounting contract.
     * @dev Replaces the constructor for upgradeable contracts.
     * @param roflAppId The ROFL app identifier (stable across redeployments)
     * @param owner Address that will own this contract
     */
    function initialize(address siweAuthAddr, address eip712VerifierAddr, address evmSignerVerifierAddr, bytes21 roflAppId, address owner) external virtual initializer {
        __ROFLable_init(roflAppId);
        __Ownable_init(owner);

        if (siweAuthAddr == address(0)) revert InvalidSiweAuth();
        siweAuth = ISiweAuth(siweAuthAddr);

        if (eip712VerifierAddr == address(0)) revert InvalidEip712Verifier();
        eip712Verifier = EIP712Verifier(eip712VerifierAddr);

        if (evmSignerVerifierAddr == address(0)) revert InvalidEvmSignerVerifier();
        evmSignerVerifier = EVMSignerVerifier(evmSignerVerifierAddr);

        nextLockId = 1;
    }

    /**
     * @notice Authorizes an upgrade to a new implementation.
     * @dev Required by UUPSUpgradeable. Only the contract owner can upgrade.
     * @param newImplementation Address of the new implementation contract
     */
    function _authorizeUpgrade(address newImplementation) internal override onlyOwner {}

    /**
     * @dev Overridden to prevent the simulation attack by contract owner.
     * @param newImplementation Address of the new implementation contract
     * @param data Must be empty so no upgrade migration hook can extract sensitive data in simulated call; passing call data reverts.
     */
    function upgradeToAndCall(
        address newImplementation,
        bytes memory data
    ) public payable override onlyProxy {
        if (data.length != 0) revert UpgradeCallDataNotAllowed();
        super.upgradeToAndCall(newImplementation, data);
    }

    /// @dev Ownership renunciation is disabled to prevent bricking the proxy.
    function renounceOwnership() public pure override {
        revert();
    }

    function _authSender(bytes memory token) internal view returns (address) {
        if (token.length != 0) {
            return siweAuth.authSender(token);
        }
        return msg.sender;
    }

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
        (depositAddr, ) = evmSignerVerifier.deriveDepositKeypair(beneficiary, chainType, version);
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
        if (beneficiary == address(0)) revert AddressMismatch();
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
        bytes32 requestId = emergencyWithdrawKey(beneficiary, tokenId, version);
        EmergencyWithdrawRequest memory req = emergencyWithdrawRequests[requestId];
        if (req.blockNumber == 0) revert EmergencyWithdrawNotFound();
        if (block.number - req.blockNumber < 1) revert EmergencyWithdrawTooSoon();

        TokenInfo memory tInfo = tokens[tokenId];

        // Dispatch on tokenType; the else-revert is the single exhaustiveness guard.
        // When a non-EVM TokenType is added, add a branch here with its ChainType.
        if (tInfo.tokenType == TokenType.NativeEVM) {
            uint256 chainId = evmSignerVerifier.decodeEVMNativeTokenData(tInfo.data);
            signedTx = evmSignerVerifier.generateDepositAddressTransfer(
                beneficiary,
                ChainType.EVM,
                version,
                chainId,
                req.toAddress,
                amount,
                sourceChainNonce,
                gasPrice
            );
        } else if (tInfo.tokenType == TokenType.ERC20) {
            (uint256 chainId, address tokenAddress) = evmSignerVerifier
                .decodeEVMErc20TokenData(tInfo.data);
            signedTx = evmSignerVerifier.generateDepositAddressERC20Transfer(
                beneficiary,
                ChainType.EVM,
                version,
                chainId,
                req.toAddress,
                tokenAddress,
                amount,
                sourceChainNonce,
                gasPrice
            );
        } else {
            revert UnsupportedTokenType();
        }

        emit EmergencyWithdrawExecuted(requestId);
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
    ) public {
        if (expiry <= block.timestamp) revert InvalidExpiry();
        if (amount == 0) revert InvalidAmount();

        address userAddress = eip712Verifier.verifyLockSignature(
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

    function _scheduleWithdrawal(
        address userAddress,
        address toAddress,
        bytes32 tokenId,
        uint256 amount
    ) internal {
        TokenInfo memory tInfo = tokens[tokenId];

        bytes memory txIdentifier;
        uint256 chainId;

        if (tInfo.tokenType == TokenType.NativeEVM) {
            chainId = evmSignerVerifier.decodeEVMNativeTokenData(tInfo.data);
            if (evmSignerVerifier.gasPrices(chainId) == 0) revert EVMSignerVerifier.GasPriceNotSet(chainId);

            txIdentifier = abi.encode(evmSignerVerifier.getEVMNonceAndIncrement(chainId));
        } else if (tInfo.tokenType == TokenType.ERC20) {
            (chainId, ) = evmSignerVerifier.decodeEVMErc20TokenData(
                tInfo.data
            );
            if (evmSignerVerifier.gasPrices(chainId) == 0) revert EVMSignerVerifier.GasPriceNotSet(chainId);

            txIdentifier = abi.encode(evmSignerVerifier.getEVMNonceAndIncrement(chainId));
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
    ) public {
        address userAddress = eip712Verifier.verifyModifyLockSignature(
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
    function unlockSingleLock(address userAddress, uint256 lockId) public {
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
    ) public {
        if (amount == 0) revert InvalidAmount();

        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        uint256 lockIndex = _findLockIndex(locks, lockId);
        FundLock storage lock = locks[lockIndex];

        eip712Verifier.verifyTransferLockedSignature(
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
    ) public {
        if (amount == 0) revert InvalidAmount();
        if (toAddress == address(0)) revert AddressMismatch();

        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        uint256 lockIndex = _findLockIndex(locks, lockId);
        FundLock storage lock = locks[lockIndex];

        eip712Verifier.verifyWithdrawFromLockSignature(
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

        address userAddress = eip712Verifier.verifyTransferSignature(
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

        address userAddress = eip712Verifier.verifyWithdrawSignature(
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

        address userAddress = withdrawalRequest.userAddress;
        address toAddress = withdrawalRequest.toAddress;
        bytes32 tokenId = withdrawalRequest.tokenId;
        uint256 amount = withdrawalRequest.amount;
        TokenInfo memory tInfo = tokens[tokenId];
        uint256 chainId;

        if (tInfo.tokenType == TokenType.NativeEVM) {
            chainId = evmSignerVerifier.decodeEVMNativeTokenData(tInfo.data);
            uint64 nonce = abi.decode(withdrawalRequest.txIdentifier, (uint64));
            signedTx = evmSignerVerifier.generateNativeTransfer(
                chainId,
                toAddress,
                amount,
                nonce
            );
        } else if (tInfo.tokenType == TokenType.ERC20) {
            address tokenAddress;
            (chainId, tokenAddress) = evmSignerVerifier
                .decodeEVMErc20TokenData(tInfo.data);
            uint64 nonce = abi.decode(withdrawalRequest.txIdentifier, (uint64));
            signedTx = evmSignerVerifier.generateERC20Transfer(
                chainId,
                toAddress,
                tokenAddress,
                amount,
                nonce
            );
        } else {
            revert UnsupportedTokenType();
        }

        // Only mark resolved and emit event if not already resolved
        if (!withdrawalRequest.resolved) {
            withdrawalRequest.resolved = true;
            emit WithdrawalResolved(
                index,
                userAddress,
                tokenId,
                toAddress,
                amount,
                chainId
            );
        }

        return signedTx;
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
     * @dev Gated by onlyROFL so only an authenticated ROFL transaction can update it.
     * @param info The complete token information including type, data, and metadata
     */
    function setTokenInfo(TokenInfo calldata info) external onlyROFL {
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
     * @notice Returns active locks for the authenticated user. Requires auth token for private reads.
     * @param token SIWE auth token identifying the caller
     * @return Array of active fund locks owned by the authenticated user
     */
    function getUserLocks(
        bytes memory token
    ) public view returns (FundLock[] memory) {
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
        bytes memory token
    ) public view returns (FundLock[] memory) {
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
     * @notice Returns authenticated user history entries within a single page, oldest first.
     * @param offset Non-negative number of page starting at 0, or negative number for the page starting from the end (-1 is last page)
     * @param limit Requested page size, capped at 100 entries
     * @param token SIWE auth token identifying the caller
     * @return page Page of history entries (oldest first within the page)
     * @return total Total history entries for the authenticated user
     */
    function getHistory(
        int256 offset,
        uint256 limit,
        bytes calldata token
    ) external view returns (HistoryEntry[] memory page, uint256 total) {
        address user = _authSender(token);
        if (user == address(0)) revert Unauthorized();
        HistoryEntry[] storage all = history[user];
        total = all.length;

        uint256 pageSize = limit > MAX_HISTORY_PAGE_SIZE ? MAX_HISTORY_PAGE_SIZE : limit;
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
        for (uint256 i = 0; i < pageSize; i++) {
            page[i] = all[start + i];
        }
    }

    /**
     * @dev Reserved storage gap for future upgrades.
     * This allows adding new state variables without shifting storage layout.
     */
    uint256[40] private __gap;
}
