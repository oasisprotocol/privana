// SPDX-License-Identifier: MIT
/* solhint-disable no-console */
pragma solidity ^0.8.20;

contract MockShoyuBashi {
    mapping(uint256 domain => mapping(uint256 id => bytes32 hash))
        public unanimousHashes;

    function getUnanimousHash(
        uint256 domain,
        uint256 id
    ) external view returns (bytes32) {
        return unanimousHashes[domain][id];
    }

    function setUnanimousHash(
        uint256 domain,
        uint256 id,
        bytes32 hash
    ) external {
        unanimousHashes[domain][id] = hash;
    }
}
