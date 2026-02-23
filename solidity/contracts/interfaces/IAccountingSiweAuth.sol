// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IAccountingSiweAuth {
    function authSender(bytes calldata token) external view returns (address);
}
