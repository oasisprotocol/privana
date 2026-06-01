// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IAccountingSiweAuth} from "./IAccountingSiweAuth.sol";

/// @notice Minimal view onto the Accounting proxy used by the delegated
///         `LockModule` to reach the proxy's resident `siweAuth` immutable.
/// @dev Under `delegatecall`, a module reads its own immutables, not the
///      proxy's. The lock view getters resolve the real `siweAuth` by calling
///      `address(this)` (the proxy) through this interface, mirroring
///      `IAccountingForHistoryModule`.
interface IAccountingForLockModule {
    function siweAuth() external view returns (IAccountingSiweAuth);
}
