// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {EVMSignerAndVerifier} from "./EVMSignerAndVerifier.sol";
import {EIP712SignatureVerifier} from "./EIP712SignatureVerifier.sol";
import {TokenInfo, TokenType, UserInfo, FundLock} from "./Types.sol";
import {EVMTransactionProof, EVMReceiptProof} from "./interfaces/IProvethVerifier.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

/**
 * @title Accounting
 * @notice Cross-chain accounting module for managing user balances and fund operations.
 *
 * This contract provides a unified accounting system that supports deposits from multiple
 * chains, fund locking for services, transfers between users, and withdrawals back
 * to origin chains. It combines transaction verification with EIP-712 signature
 * verification to ensure secure and authorized operations.
 *
 * Key features:
 * - Multi-chain deposit verification using ShoyuBashi oracle and transaction proofs
 * - Fund locking mechanism for service interactions
 * - Peer-to-peer transfers within the accounting system
 * - Automated withdrawal transaction generation
 * - Universal token abstraction supporting various tokens
 */
contract Accounting is EIP712SignatureVerifier, EVMSignerAndVerifier, UUPSUpgradeable {
    mapping(address user => mapping(bytes32 tokenId => uint256 balance))
        public balances;
    mapping(bytes32 tokenId => TokenInfo tokenInfo) public tokens;
    mapping(bytes32 depositKey => bool processed) public processedDeposits;
    mapping(address user => UserInfo) private userInfo;

    WithdrawalRequest[] public withdrawals;

    uint256 private nextLockId;

    event Deposit(
        address indexed userAddress,
        bytes32 indexed tokenId,
        uint256 amount
    );

    event LockCreated(
        address indexed userAddress,
        address indexed serviceAddress,
        bytes32 indexed tokenId,
        uint256 amount,
        uint256 expiry,
        uint256 lockId
    );

    event LockUnlocked(
        address indexed userAddress,
        bytes32 indexed tokenId,
        uint256 amount,
        uint256 lockId
    );

    event LockModified(
        address indexed userAddress,
        address indexed serviceAddress,
        bytes32 indexed tokenId,
        uint256 amountAdded,
        uint256 newExpiry,
        uint256 lockId
    );

    event BalanceTransferred(
        address indexed fromAddress,
        address indexed toAddress,
        bytes32 indexed tokenId,
        uint256 amount
    );

    event LockedFundsTransferred(
        address indexed userAddress,
        address indexed serviceAddress,
        address indexed toAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 lockId
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
        uint256 amount,
        uint256 chainId
    );

    event TokenRegistered(bytes32 indexed tokenId, TokenType tokenType);

    error InvalidDeposit();
    error InsufficientBalance();
    error TooManyActiveLocks();
    error InvalidLockId();
    error LockNotExpired();
    error InsufficientLockedAmount();
    error UnsupportedTokenType();
    error ChainIdMismatch();
    error AddressMismatch();
    error InvalidTransactionData();
    error InvalidExpiry();
    error InvalidAmount();
    error WithdrawalTooSoon();
    error DepositAlreadyProcessed();
    error ReceiptIndexMismatch();

    struct WithdrawalRequest {
        address userAddress;
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
     * @notice Internal initializer for the Accounting contract.
     * @dev Sets up EIP-712 domain and EVM signing infrastructure.
     * @param _shoyubashi Address of the ShoyuBashi oracle for cross-chain block hash verification
     * @param _provethVerifier Address of the ProvethVerifier contract for proof validation
     * @param _owner Address that will own this contract
     */
    function __Accounting_init(address _shoyubashi, address _provethVerifier, address _owner) internal onlyInitializing {
        __EIP712SignatureVerifier_init();
        __EVMSignerAndVerifier_init(_shoyubashi, _provethVerifier, _owner);
        __UUPSUpgradeable_init();
        nextLockId = 1;
    }

    /**
     * @notice Initializes the Accounting contract with EIP712 and EVM verification capabilities.
     * @dev Replaces the constructor for upgradeable contracts.
     *      The specified owner becomes the contract owner.
     * @param _shoyubashi Address of the ShoyuBashi oracle for cross-chain block hash verification
     * @param _provethVerifier Address of the ProvethVerifier contract for proof validation
     * @param _owner Address that will own this contract
     */
    function initialize(address _shoyubashi, address _provethVerifier, address _owner) external virtual initializer {
        __Accounting_init(_shoyubashi, _provethVerifier, _owner);
    }

    /**
     * @notice Authorizes an upgrade to a new implementation.
     * @dev Required by UUPSUpgradeable. Only the contract owner can upgrade.
     * @param newImplementation Address of the new implementation contract
     */
    function _authorizeUpgrade(address newImplementation) internal override onlyOwner {}

    /// @dev Ownership renunciation is disabled to prevent bricking the proxy.
    function renounceOwnership() public pure override {
        revert();
    }

    /**
     * @notice Credits user's account after verifying an EVM deposit transaction.
     *
     * This function decodes and verifies a transaction from an EVM chain to confirm
     * that a user has deposited tokens. It supports both native tokens (ETH, MATIC, etc.)
     * and ERC20 tokens from any EVM-compatible chain.
     *
     * The verification process:
     * 1. Validates the transaction proof using ProvethVerifier
     * 2. Decodes the raw transaction data to extract all transaction fields
     * 3. Verifies block hash through ShoyuBashi oracle for cross-chain security
     * 4. Verifies the transaction sender matches the claimed user
     * 5. Validates the transaction target and parameters match the token type
     * 6. For native tokens: verifies the recipient is the deposit address
     * 7. For ERC20 tokens: verifies the contract call and transfer recipient
     * 8. Credits the verified amount to the user's account balance
     *
     * Security features:
     * - Transaction proof verification via Merkle Patricia Trie
     * - ShoyuBashi oracle block hash verification (multi-oracle consensus)
     * - Sender address verification against claimed user
     * - Token-specific validation (native vs ERC20)
     * - Chain ID verification to prevent cross-chain replay attacks
     * - Transaction data decoding and validation
     *
     * @param userAddress The address of the user making the deposit
     * @param tokenId The identifier of the token being deposited
     * @param txProof The cryptographic proof that the transaction was included in a block
     */
    function creditEVMDeposit(
        address userAddress,
        bytes32 tokenId,
        EVMTransactionProof calldata txProof,
        EVMReceiptProof calldata receiptProof
    ) public {
        if (
            keccak256(txProof.transactionIndexRlp) !=
            keccak256(receiptProof.receiptIndexRlp)
        ) revert ReceiptIndexMismatch();

        bytes memory evmTransactionData = provethVerifier.validateTxProof(txProof);

        (
            uint256 chainId,
            bytes32 txHash,
            address from,
            address to,
            uint256 value,
            bytes memory txData,
            ,
            ,

        ) = EVMSignerAndVerifier.decodeEVMTransaction(evmTransactionData);

        bytes32 depositKey = keccak256(abi.encodePacked(chainId, txHash));
        if (processedDeposits[depositKey]) revert DepositAlreadyProcessed();

        bytes memory rawReceipt = provethVerifier.validateReceiptProof(
            txProof.rlpBlockHeader,
            receiptProof
        );

        (uint256 status, ) = EVMSignerAndVerifier.decodeEVMTxReceipt(
            rawReceipt
        );

        if (status != 1) revert InvalidDeposit();

        uint256 blockNumber = EVMSignerAndVerifier.getBlockNumber(
            txProof.rlpBlockHeader
        );

        EVMSignerAndVerifier.verifyBlockHash(
            keccak256(txProof.rlpBlockHeader),
            blockNumber,
            chainId
        );

        if (from != userAddress) revert AddressMismatch();

        TokenInfo memory tInfo = tokens[tokenId];

        uint256 amount;

        if (tInfo.tokenType == TokenType.NativeEVM) {
            uint256 tChainId = EVMSignerAndVerifier.decodeEVMNativeTokenData(
                tInfo.data
            );

            if (tChainId != chainId) revert ChainIdMismatch();

            if (to != EVMSignerAndVerifier.evmAddress) revert InvalidDeposit();

            if (txData.length != 0) revert InvalidTransactionData();

            amount = value;
        } else if (tInfo.tokenType == TokenType.ERC20) {
            (uint256 tChainId, address tokenAddress) = EVMSignerAndVerifier
                .decodeEVMErc20TokenData(tInfo.data);

            if (tChainId != chainId) revert ChainIdMismatch();

            if (to != tokenAddress) revert InvalidDeposit();

            (address erc20To, uint256 erc20amount) = EVMSignerAndVerifier
                .decodeTxDataForErc20Transfer(txData);

            if (erc20To != EVMSignerAndVerifier.evmAddress)
                revert AddressMismatch();

            amount = erc20amount;
        } else {
            revert UnsupportedTokenType();
        }

        if (amount == 0) revert InvalidAmount();

        processedDeposits[depositKey] = true;

        balances[userAddress][tokenId] += amount;

        emit Deposit(userAddress, tokenId, amount);
    }

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
     * @param userAddress The address of the user whose funds will be locked
     * @param serviceAddress The address of the service that will have access to the locked funds
     * @param tokenId The identifier of the token to lock
     * @param amount The amount of tokens to lock
     * @param expiry The timestamp when the lock expires and funds can be reclaimed (must be in future)
     * @param signature The EIP-712 signature from the user authorizing the lock
     */
    function createLock(
        address userAddress,
        address serviceAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 expiry,
        bytes calldata signature
    ) public {
        if (expiry <= block.timestamp) revert InvalidExpiry();
        if (amount == 0) revert InvalidAmount();

        EIP712SignatureVerifier.verifyLockSignature(
            userAddress,
            serviceAddress,
            tokenId,
            amount,
            expiry,
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

        emit LockCreated(
            userAddress,
            serviceAddress,
            tokenId,
            amount,
            expiry,
            lockId
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

    function modifyLock(
        address userAddress,
        uint256 lockId,
        uint256 amount,
        uint256 newExpiry,
        bytes calldata signature
    ) public {
        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        uint256 lockIndex = _findLockIndex(locks, lockId);
        FundLock storage lock = locks[lockIndex];

        if (newExpiry < lock.expiry) revert InvalidExpiry();

        if (amount == 0 && newExpiry == lock.expiry) revert InvalidAmount();

        EIP712SignatureVerifier.verifyModifyLockSignature(
            userAddress,
            lockId,
            amount,
            newExpiry,
            signature
        );

        if (amount > 0) {
            if (balances[userAddress][lock.tokenId] < amount)
                revert InsufficientBalance();

            balances[userAddress][lock.tokenId] -= amount;
            lock.amount += amount;
        }

        lock.expiry = newExpiry;

        emit LockModified(
            userAddress,
            lock.serviceId,
            lock.tokenId,
            amount,
            newExpiry,
            lockId
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
        }

        locks[lockIndex] = locks[locks.length - 1];
        locks.pop();

        emit LockUnlocked(userAddress, lock.tokenId, lock.amount, lockId);
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

                locks[i] = locks[locks.length - 1];
                locks.pop();

                emit LockUnlocked(userAddress, lock.tokenId, lock.amount, lock.lockId);
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
     * @param signature The EIP-712 signature from the service authorizing the transfer
     */
    function transferFromLock(
        address userAddress,
        address toAddress,
        uint256 lockId,
        uint256 amount,
        bytes calldata signature
    ) public {
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
            signature
        );

        if (lock.amount < amount) revert InsufficientLockedAmount();

        lock.amount -= amount;
        balances[toAddress][lock.tokenId] += amount;

        emit LockedFundsTransferred(
            userAddress,
            lock.serviceId,
            toAddress,
            lock.tokenId,
            amount,
            lockId
        );

        if (lock.amount == 0) {
            locks[lockIndex] = locks[locks.length - 1];
            locks.pop();
        }
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
     * @dev The signature must be from the userAddress (sender).
     *      This is an internal transfer that doesn't generate blockchain transactions.
     *
     * @param userAddress The address of the user sending the funds
     * @param toAddress The address of the user receiving the funds
     * @param tokenId The identifier of the token being transferred
     * @param amount The amount of tokens to transfer
     * @param nonce The nonce for replay protection (must match user's current transfer nonce)
     * @param signature The EIP-712 signature from the sender authorizing the transfer
     */
    function transferBalance(
        address userAddress,
        address toAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) public {
        if (amount == 0) revert InvalidAmount();

        EIP712SignatureVerifier.verifyTransferSignature(
            userAddress,
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

        emit BalanceTransferred(userAddress, toAddress, tokenId, amount);
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
     * @param userAddress The address of the user requesting the withdrawal
     * @param tokenId The identifier of the token to withdraw
     * @param amount The amount of tokens to withdraw
     * @param nonce The user's current withdrawal nonce for replay protection
     * @param signature The EIP-712 signature from the user authorizing the withdrawal
     */
    function requestWithdrawal(
        address userAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 nonce,
        bytes calldata signature
    ) public {
        EIP712SignatureVerifier.verifyWithdrawSignature(
            userAddress,
            tokenId,
            amount,
            nonce,
            signature
        );

        if (balances[userAddress][tokenId] < amount)
            revert InsufficientBalance();

        balances[userAddress][tokenId] -= amount;

        TokenInfo memory tInfo = tokens[tokenId];

        bytes memory txIdentifier;

        if (tInfo.tokenType == TokenType.NativeEVM) {
            uint256 chainId = EVMSignerAndVerifier.decodeEVMNativeTokenData(
                tInfo.data
            );

            txIdentifier = abi.encode(getEVMNonceAndIncrement(chainId));

            emit Withdrawal(userAddress, tokenId, amount, chainId);
        } else if (tInfo.tokenType == TokenType.ERC20) {
            (uint256 chainId, ) = EVMSignerAndVerifier.decodeEVMErc20TokenData(
                tInfo.data
            );

            txIdentifier = abi.encode(getEVMNonceAndIncrement(chainId));

            emit Withdrawal(userAddress, tokenId, amount, chainId);
        } else {
            revert UnsupportedTokenType();
        }

        withdrawals.push(
            WithdrawalRequest({
                userAddress: userAddress,
                amount: amount,
                blockNumber: block.number,
                tokenId: tokenId,
                txIdentifier: txIdentifier,
                resolved: false
            })
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
        bytes32 tokenId = withdrawalRequest.tokenId;
        uint256 amount = withdrawalRequest.amount;
        TokenInfo memory tInfo = tokens[tokenId];
        uint256 chainId;

        if (tInfo.tokenType == TokenType.NativeEVM) {
            chainId = EVMSignerAndVerifier.decodeEVMNativeTokenData(tInfo.data);
            uint64 nonce = abi.decode(withdrawalRequest.txIdentifier, (uint64));
            signedTx = EVMSignerAndVerifier.generateNativeTransfer(
                chainId,
                userAddress,
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
                userAddress,
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
            emit WithdrawalResolved(index, userAddress, tokenId, amount, chainId);
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
     * @dev Only callable by the contract owner to prevent unauthorized token configuration.
     * @param info The complete token information including type, data, and metadata
     */
    function setTokenInfo(TokenInfo calldata info) external onlyOwner {
        bytes32 tokenId = getTokenId(info);
        tokens[tokenId] = info;

        emit TokenRegistered(tokenId, info.tokenType);
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
     * @notice Retrieves all active fund locks for a specific user.
     *
     * @dev Returns a memory copy of the locks array.
     *      The returned array may be reordered if locks are removed concurrently.
     *
     * @param user The address of the user whose locks to retrieve
     * @return An array of all active fund locks for the user
     */
    function getUserLocks(
        address user
    ) external view returns (FundLock[] memory) {
        UserInfo storage uInfo = userInfo[user];
        return uInfo.activeLocks;
    }

    /**
     * @notice Retrieves all expired fund locks for a specific user.
     *
     * This function filters the user's active locks to return only those that
     * have expired (current timestamp >= lock expiry). This is useful for UIs
     * to display which locks can be unlocked.
     *
     * @dev Returns arrays of expired locks and their corresponding indices.
     *      The indices correspond to positions in the user's activeLocks array.
     *
     * @param user The address of the user whose expired locks to retrieve
     * @return expiredLocks Array of expired FundLock structs
     * @return lockIndices Array of indices corresponding to each expired lock
     */
    function getExpiredLocks(
        address user
    )
        external
        view
        returns (FundLock[] memory expiredLocks, uint256[] memory lockIndices)
    {
        UserInfo storage uInfo = userInfo[user];
        FundLock[] storage allLocks = uInfo.activeLocks;

        uint256 expiredCount = 0;
        for (uint256 i = 0; i < allLocks.length; i++) {
            if (block.timestamp >= allLocks[i].expiry) {
                expiredCount++;
            }
        }

        expiredLocks = new FundLock[](expiredCount);
        lockIndices = new uint256[](expiredCount);

        uint256 currentIndex = 0;
        for (uint256 i = 0; i < allLocks.length; i++) {
            if (block.timestamp >= allLocks[i].expiry) {
                expiredLocks[currentIndex] = allLocks[i];
                lockIndices[currentIndex] = i;
                currentIndex++;
            }
        }

        return (expiredLocks, lockIndices);
    }

    /**
     * @notice Retrieves balances for multiple tokens for a specific user.
     *
     * This function allows batch querying of token balances to reduce the number
     * of RPC calls needed by frontends and services.
     *
     * @dev Returns balances in the same order as the input tokenIds array.
     *
     * @param user The address of the user whose balances to retrieve
     * @param tokenIds Array of token identifiers to query
     * @return Array of balances corresponding to each token ID
     */
    function getBalances(
        address user,
        bytes32[] calldata tokenIds
    ) external view returns (uint256[] memory) {
        uint256[] memory userBalances = new uint256[](tokenIds.length);

        for (uint256 i = 0; i < tokenIds.length; i++) {
            userBalances[i] = balances[user][tokenIds[i]];
        }

        return userBalances;
    }

    /**
     * @notice Calculates the total locked balance for a specific token across all locks.
     *
     * This function sums up all locked amounts for a given token ID across all
     * of a user's active locks. This is useful for displaying the user's total
     * unavailable balance.
     *
     * @dev Iterates through all active locks and sums matching token amounts.
     *
     * @param user The address of the user
     * @param tokenId The token identifier to calculate total locked amount for
     * @return The total amount of the token that is currently locked
     */
    function getTotalLockedBalance(
        address user,
        bytes32 tokenId
    ) external view returns (uint256) {
        UserInfo storage uInfo = userInfo[user];
        FundLock[] storage locks = uInfo.activeLocks;

        uint256 totalLocked = 0;
        for (uint256 i = 0; i < locks.length; i++) {
            if (locks[i].tokenId == tokenId) {
                totalLocked += locks[i].amount;
            }
        }

        return totalLocked;
    }

    /**
     * @dev Reserved storage gap for future upgrades.
     * This allows adding new state variables without shifting storage layout.
     */
    uint256[44] private __gap;
}
