// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IAccountingSiweAuth} from "./IAccountingSiweAuth.sol";

interface IAccountingForHistoryModule {
    function siweAuth() external view returns (IAccountingSiweAuth);
}
