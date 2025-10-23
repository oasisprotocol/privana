// SPDX-License-Identifier: MIT
/* solhint-disable no-console */
pragma solidity ^0.8.20;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";

import {EVMSignerAndVerifier} from "./EVMSignerAndVerifier.sol";
import {EIP712SignatureVerifier} from "./EIP712SignatureVerifier.sol";

import {TokenInfo, TokenType, UserInfo, FundLock, TransactionProof} from "./Types.sol";

import {EVMSignerAndVerifier} from "./EVMSignerAndVerifier.sol";

import {EVMTransactionProof} from "./lib/ProvethVerifier.sol";

import "hardhat/console.sol";

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
 * - Multi-chain deposit verification using transaction proofs
 * - Fund locking mechanism for service interactions
 * - Peer-to-peer transfers within the accounting system
 * - Automated withdrawal transaction generation
 * - Universal token abstraction supporting various tokens
 */
contract Accounting is EIP712SignatureVerifier, EVMSignerAndVerifier {
    // Accounting for user balances
    mapping(address user => mapping(bytes32 tokenId => uint256 balance))
        public balances;
    mapping(bytes32 tokenId => TokenInfo tokenInfo) public tokens;

    mapping(address user => UserInfo) private userInfo;

    /**
     * @notice Initializes the Accounting contract with EIP712 and EVM verification capabilities.
     * @dev Calls parent constructors to set up EIP-712 domain and EVM signing infrastructure.
     */
    constructor(
        address _shoyubashi
    ) EVMSignerAndVerifier(_shoyubashi) EIP712SignatureVerifier() {}

    /**
     * @notice Processes and verifies an EVM deposit transaction to credit user's account.
     *
     * This function decodes and verifies a transaction from an EVM chain to confirm
     * that a user has deposited tokens. It supports both native tokens (ETH, MATIC, etc.)
     * and ERC20 tokens from any EVM-compatible chain.
     *
     * The verification process:
     * 1. Decodes the raw transaction data to extract all transaction fields
     * 2. Verifies the transaction sender matches the claimed user
     * 3. Validates the transaction target and parameters match the token type
     * 4. For native tokens: verifies the recipient is the deposit address
     * 5. For ERC20 tokens: verifies the contract call and transfer recipient
     * 6. Credits the verified amount to the user's account balance
     *
     * Security features:
     * - Transaction proof verification (TODO: implement with txProof parameter)
     * - Sender address verification against claimed user
     * - Token-specific validation (native vs ERC20)
     * - Chain ID verification to prevent cross-chain replay
     * - Transaction data decoding and validation
     *
     * @dev Currently transaction proof verification is not implemented (TODO).
     *      The function assumes the provided transaction data is included in
     *      the block that will be specified by the txProof.
     *
     * @param userAddress The address of the user making the deposit
     * @param tokenId The identifier of the token being deposited
     * @param txProof The cryptographic proof that the transaction was included in a block
     */
    function includeEVMDeposit(
        address userAddress,
        bytes32 tokenId,
        EVMTransactionProof calldata txProof
    ) public {
        console.log("includeEVMDeposit called");

        bytes memory evmTransactionData = validateTxProof(txProof);

        (
            uint256 chainId,
            bytes32 txHash,
            address from,
            address to,
            uint256 value,
            bytes memory txData,
            uint256 v,
            uint256 r,
            uint256 s
        ) = EVMSignerAndVerifier.decodeEVMTransaction(evmTransactionData);

        console.log("proof validated");

        uint256 blockNumber = EVMSignerAndVerifier.getBlockNumber(
            txProof.rlpBlockHeader
        );

        console.log("block number:", blockNumber);

        EVMSignerAndVerifier.verifyBlockHash(
            keccak256(txProof.rlpBlockHeader),
            blockNumber,
            chainId
        );

        console.log("transaction verified");

        // Verify from matches the userAddress
        require(from == userAddress, "From address mismatch");

        TokenInfo memory tInfo = tokens[tokenId];

        // TODO: Verify transaction hash proof using txProof

        uint256 amount;

        if (tInfo.tokenType == TokenType.NativeEVM) {
            uint256 tChainId = EVMSignerAndVerifier.decodeEVMNativeTokenData(
                tInfo.data
            );

            require(tChainId == chainId, "ChainId mismatch");

            // Verify the to address is the deposit address
            require(
                to == EVMSignerAndVerifier.evmAddress,
                "Not a deposit transaction"
            );

            // Verify from matches the userAddress
            require(from == userAddress, "From address mismatch");

            // Verify the txData is empty
            require(txData.length == 0, "Non-empty tx data");

            amount = value;
        } else if (tInfo.tokenType == TokenType.ERC20) {
            (uint256 tChainId, address tokenAddress) = EVMSignerAndVerifier
                .decodeEVMErc20TokenData(tInfo.data);

            require(tChainId == chainId, "ChainId mismatch");

            // Verify the to address matches the tokenAddress
            require(to == tokenAddress, "Not a deposit transaction");

            (address erc20To, uint256 erc20amount) = EVMSignerAndVerifier
                .decodeTxDataForErc20Transfer(txData);

            require(
                erc20To == EVMSignerAndVerifier.evmAddress,
                "ERC20 to address mismatch"
            );

            amount = erc20amount;
        }

        // Increase token balance by value
        balances[userAddress][tokenId] += amount;

        emit Deposit(userAddress, tokenId, amount);
    }

    /**
     * @notice Locks user funds for exclusive access by a designated service.
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
     * - Expiry timestamp to prevent indefinite locks
     * - Balance verification before locking
     * - Limited number of active locks per user (max 10)
     *
     * @dev The signature must be from the user whose funds are being locked.
     *      Locked funds are stored in the user's activeLocks array.
     *
     * @param userAddress The address of the user whose funds will be locked
     * @param serviceAddress The address of the service that will have access to the locked funds
     * @param tokenId The identifier of the token to lock
     * @param amount The amount of tokens to lock
     * @param expiry The timestamp when the lock expires and funds can be reclaimed
     * @param signature The EIP-712 signature from the user authorizing the lock
     */
    function lockFunds(
        address userAddress,
        address serviceAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 expiry,
        bytes calldata signature
    ) public {
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

        if (locks.length >= 10) {
            revert("Too many active locks");
        }

        require(
            balances[userAddress][tokenId] >= amount,
            "Insufficient balance"
        );
        balances[userAddress][tokenId] -= amount;

        locks.push(
            FundLock({
                serviceId: serviceAddress,
                tokenId: tokenId,
                amount: amount,
                expiry: expiry
            })
        );

        // TODO: Emit event
    }

    /**
     * @notice Unlocks expired fund locks and returns funds to the user's available balance.
     *
     * This function allows users to reclaim funds from expired locks. Once a lock's
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
     *      a service goes down.
     *
     * @param userAddress The address of the user whose lock should be unlocked
     * @param lockIndex The index of the lock in the user's activeLocks array
     */
    function unlockFunds(address userAddress, uint256 lockIndex) public {
        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        // If the expiry has passed or there are no funds in the, undo the above step, otherwise revert
        require(lockIndex < locks.length, "Invalid lock index");
        FundLock memory lock = locks[lockIndex];
        if (lock.amount != 0) {
            require(block.timestamp >= lock.expiry, "Lock not yet expired");
            balances[userAddress][lock.tokenId] += lock.amount;
        }

        // Remove the lock by swapping with the last and popping
        locks[lockIndex] = locks[locks.length - 1];
        locks.pop();

        // TODO: emit event that funds were unlocked
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
     * @param signature The EIP-712 signature from the sender authorizing the transfer
     */
    function transferFunds(
        address userAddress,
        address toAddress,
        bytes32 tokenId,
        uint256 amount,
        bytes calldata signature
    ) public {
        EIP712SignatureVerifier.verifyTransferSignature(
            userAddress,
            toAddress,
            tokenId,
            amount,
            signature
        );

        require(
            balances[userAddress][tokenId] >= amount,
            "Insufficient balance"
        );
        balances[userAddress][tokenId] -= amount;
        balances[toAddress][tokenId] += amount;
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
     *      If the lock amount reaches zero, the lock can be removed by calling unlockFunds();
     *      The lock array may be reordered due to swap-and-pop removal. The service is
     *      a user (managed the same way as regular users) and any user can act as a service.
     *
     * @param userAddress The address of the user who originally locked the funds
     * @param toAddress The address receiving the transferred locked funds
     * @param lockIndex The index of the lock in the user's activeLocks array
     * @param amount The amount of locked tokens to transfer
     * @param signature The EIP-712 signature from the service authorizing the transfer
     */
    function transferLockedFunds(
        address userAddress,
        address toAddress,
        uint256 lockIndex,
        uint256 amount,
        bytes calldata signature
    ) public {
        UserInfo storage uInfo = userInfo[userAddress];
        FundLock[] storage locks = uInfo.activeLocks;

        // If the expiry has passed or there are no funds in the, undo the above step, otherwise revert
        require(lockIndex < locks.length, "Invalid lock index");
        FundLock storage lock = locks[lockIndex];

        EIP712SignatureVerifier.verifyTransferLockedSignature(
            lock.serviceId,
            userAddress,
            toAddress,
            lockIndex,
            amount,
            signature
        );

        require(lock.amount >= amount, "Insufficient locked amount");
        lock.amount -= amount;
        balances[toAddress][lock.tokenId] += amount;

        // Remove the lock by swapping with the last and popping
        if (lock.amount == 0) {
            locks[lockIndex] = locks[locks.length - 1];
            locks.pop();
        }
    }

    /**
     * @notice Withdraws user funds by generating a signed transaction for the origin chain.
     *
     * This function processes user withdrawal requests by:
     * 1. Verifying the user's authorization via EIP-712 signature
     * 2. Debiting the requested amount from the user's account
     * 3. Generating a signed transaction to send funds on the origin chain
     * 4. Returning the signed transaction for broadcast
     *
     * The generated transaction is signed using the contract's private key and can be
     * broadcast to the appropriate blockchain to complete the withdrawal. The transaction
     * type (native transfer vs ERC20 transfer) is determined by the token configuration.
     *
     * Security features:
     * - EIP-712 signature verification to authorize withdrawal
     * - Balance verification before debiting
     * - Token-type specific transaction generation
     *
     * @dev The returned transaction must be broadcast externally to complete withdrawal.
     *      The function debits the user's balance immediately upon signature verification.
     *      Replay protection mechanisms are automatically managed for the target chain.
     *
     * @param userAddress The address of the user requesting the withdrawal
     * @param tokenId The identifier of the token to withdraw
     * @param amount The amount of tokens to withdraw
     * @param signature The EIP-712 signature from the user authorizing the withdrawal
     * @return signedTx The raw signed transaction ready for broadcast
     */
    function withdrawFunds(
        address userAddress,
        bytes32 tokenId,
        uint256 amount,
        bytes calldata signature
    ) public returns (bytes memory signedTx) {
        EIP712SignatureVerifier.verifyWithdrawSignature(
            userAddress,
            tokenId,
            amount,
            signature
        );

        require(
            balances[userAddress][tokenId] >= amount,
            "Insufficient balance"
        );
        balances[userAddress][tokenId] -= amount;

        TokenInfo memory tInfo = tokens[tokenId];

        if (tInfo.tokenType == TokenType.NativeEVM) {
            uint256 chainId = EVMSignerAndVerifier.decodeEVMNativeTokenData(
                tInfo.data
            );
            signedTx = EVMSignerAndVerifier.generateNativeTransfer(
                chainId,
                userAddress,
                amount
            );
        } else if (tInfo.tokenType == TokenType.ERC20) {
            (uint256 chainId, address tokenAddress) = EVMSignerAndVerifier
                .decodeEVMErc20TokenData(tInfo.data);
            signedTx = EVMSignerAndVerifier.generateERC20Transfer(
                chainId,
                userAddress,
                tokenAddress,
                amount
            );
        } else {
            revert("Unsupported token type");
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
    function getTokenId(TokenInfo calldata info) public view returns (bytes32) {
        return keccak256(abi.encode(info.tokenType, info.data));
    }

    /**
     * @notice Registers or updates token information in the system.
     *
     * This function adds a new token to the accounting system or updates
     * an existing token's configuration. The token ID is automatically
     * computed from the provided token information.
     *
     * @param info The complete token information including type, data, and metadata
     */
    function setTokenInfo(TokenInfo calldata info) external {
        bytes32 tokenId = getTokenId(info);
        tokens[tokenId] = info;
    }

    /**
     * @notice Retrieves all active fund locks for a specific user.
     *
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

    error InvalidDeposit();

    event Deposit(
        address indexed userAddress,
        bytes32 indexed tokenId,
        uint256 amount
    );
}
