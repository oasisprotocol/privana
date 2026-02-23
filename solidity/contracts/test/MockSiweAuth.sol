// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title MockSiweAuth
 * @notice Minimal SIWE auth mock for unit tests on non-Sapphire chains (e.g., Hardhat).
 * @dev Token format is `abi.encode(address)`.
 */
contract MockSiweAuth {
    string public domain;

    struct SignatureRSV {
        bytes32 r;
        bytes32 s;
        uint256 v;
    }

    constructor(string memory inDomain) {
        domain = inDomain;
    }

    function login(
        string calldata,
        SignatureRSV calldata
    ) external view returns (bytes memory) {
        return abi.encode(msg.sender);
    }

    function authSender(bytes calldata token) external pure returns (address) {
        if (token.length == 0) return address(0);
        return abi.decode(token, (address));
    }
}
