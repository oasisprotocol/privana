// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {TokenInfo, EVMKeypair} from "./Types.sol";

contract EVMUtils {
    // Decode EVM native token metadata
    function decodeEVMNativeTokenData(
        bytes memory data
    ) public pure returns (uint256 chainId) {
        require(data.length == 32, "Invalid data length for EVM native token");
        assembly {
            chainId := mload(add(data, 32))
        }
    }

    function encodeEVMNativeTokenData(
        uint256 chainId
    ) public pure returns (bytes memory data) {
        data = new bytes(32);
        assembly {
            mstore(add(data, 32), chainId)
        }
    }

    // Decode EVM ERC20 token metadata
    function decodeEVMErc20TokenData(
        bytes memory data
    ) public pure returns (uint256 chainId, address tokenAddress) {
        require(data.length == 52, "Invalid data length for EVM ERC20 token");
        assembly {
            chainId := mload(add(data, 32))
            tokenAddress := mload(add(data, 52))
        }
    }

    function encodeEVMErc20TokenData(
        uint256 chainId,
        address tokenAddress
    ) public pure returns (bytes memory data) {
        data = new bytes(52);
        assembly {
            mstore(add(data, 32), chainId)
            mstore(add(data, 52), tokenAddress)
        }
    }
}
