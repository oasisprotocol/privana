// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ChainType, TokenType} from "../Types.sol";

interface IAccountingSigner {
    // solhint-disable-next-line func-name-mixedcase
    function SIGNER_ID() external view returns (bytes32);

    function evmAddress() external view returns (address);

    function gasTankAddress() external view returns (address);

    function gasPrices(uint256 chainId) external view returns (uint256);

    function nonces(uint256 chainId) external view returns (uint64);

    function roflSignerAddress() external view returns (address);

    function accounting() external view returns (address);

    function owner() external view returns (address);

    function gasLimitNativeSweep() external view returns (uint64);

    function gasLimitERC20Sweep() external view returns (uint64);

    function gasLimitNativeWithdraw() external view returns (uint64);

    function gasLimitERC20Withdraw() external view returns (uint64);

    function transferOwnership(address newOwner) external;

    function setGasPrice(uint256 chainId, uint256 gasPrice) external;

    function setRoflSignerAddress(address newSigner) external;

    function getDepositAddress(
        address beneficiary,
        ChainType chainType,
        uint256 version
    ) external view returns (address depositAddr);

    function reserveTokenWithdrawalNonce(
        TokenType tokenType,
        bytes calldata tokenData
    ) external returns (uint256 chainId, uint64 nonce);

    function generateTokenWithdrawalTransfer(
        TokenType tokenType,
        bytes calldata tokenData,
        address toAddress,
        uint256 amount,
        uint64 nonce
    ) external view returns (uint256 chainId, bytes memory signedTx);

    function generateDepositAddressTokenTransfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        TokenType tokenType,
        bytes calldata tokenData,
        address toAddress,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    ) external view returns (bytes memory signedTx);

    function generateSweepNativeTransfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    ) external view returns (bytes memory signedTx);

    function generateSweepERC20Transfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        address tokenAddress,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    ) external view returns (bytes memory signedTx);

    function generateGasFundingTx(
        address toDepositAddress,
        uint256 chainId,
        uint256 gasAmount,
        uint64 gasTankNonce,
        uint256 gasPrice
    ) external view returns (bytes memory signedTx);
}
