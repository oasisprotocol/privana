// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ISiweAuth {
    function authSender(bytes calldata token) external view returns (address);
    function setAuthTokenEncKey(bytes32 newKey) external;
    function roflAppId() external view returns (bytes21);
}
