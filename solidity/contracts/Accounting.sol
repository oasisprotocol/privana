// SPDX-License-Identifier: MIT
/* solhint-disable no-console */
pragma solidity ^0.8.20;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";

import {EVMDepositVerifier} from "./EVMDepositVerifier.sol";
import {EIP712SignatureVerifier} from "./EIP712SignatureVerifier.sol";
import {EVMWithdrawalSigner} from "./EVMWithdrawalSigner.sol";

import {TokenInfo, TokenType, UserInfo, FundLock, TransactionProof} from "./Types.sol";

import {EVMUtils} from "./EVMUtils.sol";

contract Accounting is
    EIP712SignatureVerifier,
    EVMWithdrawalSigner,
    EVMDepositVerifier,
    EVMUtils
{
    // Accounting for user balances
    mapping(address user => mapping(bytes32 tokenId => uint256 balance))
        public balances;
    mapping(bytes32 tokenId => TokenInfo tokenInfo) public tokens;

    mapping(address user => UserInfo) private userInfo;

    constructor() EVMWithdrawalSigner() EIP712SignatureVerifier() {}

    function includeEVMNativeDeposit(
        address userAddress,
        bytes32 tokenId,
        bytes calldata evmTransactionData,
        TransactionProof calldata txProof
    ) public {
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
        ) = EVMDepositVerifier.decodeEVMTransaction(evmTransactionData);

        TokenInfo memory tInfo = tokens[tokenId];

        uint256 tChainId = EVMUtils.decodeEVMNativeTokenData(tInfo.data);

        require(tChainId == chainId, "ChainId mismatch");

        // Verify the to address is the deposit address
        require(to == getEVMDepositAddress(), "Not a deposit transaction");

        // Verify from matches the userAddress
        require(from == userAddress, "From address mismatch");

        // Verify the txData is empty
        require(txData.length == 0, "Non-empty tx data");

        // TODO: Verify transaction hash proof using txProof

        // Increase token balance by value
        balances[userAddress][tokenId] += value;

        emit Deposit(userAddress, tokenId, value);
    }

    function includeEVMErc20Deposit(
        address userAddress,
        bytes32 tokenId,
        bytes calldata evmTransactionData,
        TransactionProof calldata txProof
    ) public {
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
        ) = EVMDepositVerifier.decodeEVMTransaction(evmTransactionData);

        // Verify from matches the userAddress
        require(from == userAddress, "From address mismatch");

        TokenInfo memory tInfo = tokens[tokenId];
        (uint256 tChainId, address tokenAddress) = EVMUtils
            .decodeEVMErc20TokenData(tInfo.data);

        require(tChainId == chainId, "ChainId mismatch");

        // Verify the to address matches the tokenAddress
        require(to == tokenAddress, "Not a deposit transaction");

        (address erc20To, uint256 erc20Amount) = EVMDepositVerifier
            .decodeTxDataForErc20Transfer(txData);

        require(erc20To == getEVMDepositAddress(), "ERC20 to address mismatch");

        // TODO: Verify transaction hash proof using txProof

        // Increase token balance by value
        balances[userAddress][tokenId] += erc20Amount;

        emit Deposit(userAddress, tokenId, value);
    }

    function includeEVMDeposit(
        address userAddress,
        bytes32 tokenId,
        bytes calldata evmTransactionData,
        TransactionProof calldata txProof
    ) public {
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
        ) = EVMDepositVerifier.decodeEVMTransaction(evmTransactionData);

        // Verify from matches the userAddress
        require(from == userAddress, "From address mismatch");

        TokenInfo memory tInfo = tokens[tokenId];
        (uint256 tChainId, address tokenAddress) = EVMUtils
            .decodeEVMErc20TokenData(tInfo.data);

        require(tChainId == chainId, "ChainId mismatch");

        // TODO: Verify transaction hash proof using txProof

        uint256 amount;

        if (tInfo.tokenType == TokenType.NativeEVM) {
            // Verify the to address is the deposit address
            require(to == getEVMDepositAddress(), "Not a deposit transaction");

            // Verify from matches the userAddress
            require(from == userAddress, "From address mismatch");

            // Verify the txData is empty
            require(txData.length == 0, "Non-empty tx data");

            amount = value;
        } else if (tInfo.tokenType == TokenType.ERC20) {
            // Verify the to address matches the tokenAddress
            require(to == tokenAddress, "Not a deposit transaction");

            (address erc20To, uint256 erc20amount) = EVMDepositVerifier
                .decodeTxDataForErc20Transfer(txData);

            require(
                erc20To == getEVMDepositAddress(),
                "ERC20 to address mismatch"
            );

            amount = erc20amount;
        }

        // Increase token balance by value
        balances[userAddress][tokenId] += amount;

        emit Deposit(userAddress, tokenId, amount);
    }

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
        FundLock memory lock = locks[lockIndex];

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
            uint256 chainId = EVMUtils.decodeEVMNativeTokenData(tInfo.data);
            signedTx = generateNativeTransfer(chainId, userAddress, amount);
        } else if (tInfo.tokenType == TokenType.ERC20) {
            (uint256 chainId, address tokenAddress) = EVMUtils
                .decodeEVMErc20TokenData(tInfo.data);
            signedTx = generateERC20Transfer(
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

    function getTokenId(TokenInfo calldata info) public view returns (bytes32) {
        return keccak256(abi.encode(info.tokenType, info.data));
    }

    function setTokenInfo(TokenInfo calldata info) external {
        bytes32 tokenId = getTokenId(info);
        tokens[tokenId] = info;
    }

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
