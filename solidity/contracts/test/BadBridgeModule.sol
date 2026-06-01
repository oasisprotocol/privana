// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {AccountingStorage} from "../AccountingStorage.sol";

/**
 * @title BadBridgeModule
 * @notice Test-only wrong-layout fixture. Inserts an extra storage slot via
 *         an intermediate abstract base so the leaf's storage layout no
 *         longer matches `Accounting`'s. The storage-prefix gate in
 *         `AccountingStorageLayout.ts` MUST fail closed against this contract.
 *
 * @dev The injected slot lands AFTER `AccountingStorage.__gap`, which means
 *      `BadBridgeModule`'s storage layout has one entry (`__injected`) that
 *      `Accounting`'s layout does not — exactly the regression we want to
 *      catch in CI without needing a deployed proxy or an OZ upgrade.
 */
abstract contract _BadBridgeBase is AccountingStorage {
    uint256 private __injected;
}

contract BadBridgeModule is _BadBridgeBase {
    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }
}
