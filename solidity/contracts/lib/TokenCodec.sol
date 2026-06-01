// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

library TokenCodec {
    error InvalidNativeTokenDataLength();
    error InvalidERC20TokenDataLength();

    function decodeEVMNativeTokenData(
        bytes memory data
    ) internal pure returns (uint256 chainId) {
        if (data.length != 32) revert InvalidNativeTokenDataLength();
        assembly ("memory-safe") {
            chainId := mload(add(data, 32))
        }
    }

    function encodeEVMNativeTokenData(
        uint256 chainId
    ) internal pure returns (bytes memory data) {
        return abi.encodePacked(chainId);
    }

    function decodeEVMErc20TokenData(
        bytes memory data
    ) internal pure returns (uint256 chainId, address tokenAddress) {
        if (data.length != 52) revert InvalidERC20TokenDataLength();
        assembly ("memory-safe") {
            chainId := mload(add(data, 32))
            tokenAddress := shr(96, mload(add(data, 64)))
        }
    }

    function encodeEVMErc20TokenData(
        uint256 chainId,
        address tokenAddress
    ) internal pure returns (bytes memory data) {
        return abi.encodePacked(chainId, tokenAddress);
    }
}
