// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {HistoryEntry, HistoryKind} from "./Types.sol";
import {IAccountingForHistoryModule} from "./interfaces/IAccountingForHistoryModule.sol";
import {IAccountingHistoryModule} from "./interfaces/IAccountingHistoryModule.sol";

/**
 * @title AccountingHistoryModule
 * @notice Delegated history code for Accounting-owned history storage.
 * @dev This contract is never called as storage owner. Accounting delegatecalls
 *      into it so the `history` mapping stays in the Accounting proxy layout.
 *      Standalone (no AccountingStorage inheritance) to keep the module lean;
 *      it reaches `history` through a pinned absolute slot instead.
 */
contract AccountingHistoryModule is IAccountingHistoryModule {
    /// @dev Used by deploy/backend validation to reject linking the wrong code.
    bytes32 public constant MODULE_ID =
        keccak256("privana.accounting.historyModule.v1");
    uint256 private constant MAX_HISTORY_PAGE_SIZE = 100;
    /// @dev Must equal `Accounting.history`'s storage slot. Slot 111 (not 108
    ///      as on the upstream non-bridge layout) because `AccountingStorage`
    ///      carries three extra bridge slots (`_ledgerTotal`,
    ///      `roflBridgeAddress`, `bridgeBurnRequests`) ahead of `history`.
    ///      Pinned by `AccountingStorageLayout.ts`.
    uint256 public constant HISTORY_SLOT = 111;

    address private immutable SELF = address(this);

    struct HistoryStorage {
        mapping(address user => HistoryEntry[] entries) history;
    }

    error NotDelegated();
    error Unauthorized();

    modifier onlyDelegateCall() {
        if (address(this) == SELF) revert NotDelegated();
        _;
    }

    function _historyStorage()
        internal
        pure
        returns (HistoryStorage storage historyStorage)
    {
        uint256 slot = HISTORY_SLOT;
        assembly {
            historyStorage.slot := slot
        }
    }

    function _authSender(bytes memory token) internal view returns (address) {
        if (token.length != 0) {
            return
                IAccountingForHistoryModule(address(this))
                    .siweAuth()
                    .authSender(token);
        }
        return msg.sender;
    }

    function appendHistory(
        address user,
        HistoryKind kind,
        bytes calldata payload
    ) external onlyDelegateCall {
        _append(user, kind, payload);
    }

    function _append(
        address user,
        HistoryKind kind,
        bytes calldata payload
    ) internal {
        _historyStorage().history[user].push(
            HistoryEntry({
                kind: kind,
                timestamp: uint64(block.timestamp),
                payload: payload
            })
        );
    }

    function appendTransferHistory(
        address fromAddress,
        address toAddress,
        HistoryKind kind,
        bytes calldata payload
    ) external onlyDelegateCall {
        _append(fromAddress, kind, payload);
        if (toAddress != address(0) && toAddress != fromAddress) {
            _append(toAddress, kind, payload);
        }
    }

    function getHistory(
        int256 offset,
        uint256 limit,
        bytes calldata token
    )
        external
        view
        onlyDelegateCall
        returns (HistoryEntry[] memory page, uint256 total)
    {
        address user = _authSender(token);
        if (user == address(0)) revert Unauthorized();
        return _getHistory(user, offset, limit);
    }

    function _getHistory(
        address user,
        int256 offset,
        uint256 limit
    ) internal view returns (HistoryEntry[] memory page, uint256 total) {
        HistoryEntry[] storage all = _historyStorage().history[user];
        total = all.length;

        uint256 pageSize = limit > MAX_HISTORY_PAGE_SIZE
            ? MAX_HISTORY_PAGE_SIZE
            : limit;
        if (total == 0 || pageSize == 0) {
            return (new HistoryEntry[](0), total);
        }

        uint256 pageCount = (total + pageSize - 1) / pageSize;
        uint256 start;
        if (offset < 0) {
            // Avoid negating type(int256).min.
            uint256 pageFromEnd = uint256(-(offset + 1)) + 1;
            if (pageFromEnd > pageCount) {
                return (new HistoryEntry[](0), total);
            }
            start = (pageCount - pageFromEnd) * pageSize;
        } else {
            if (uint256(offset) >= pageCount) {
                return (new HistoryEntry[](0), total);
            }
            start = uint256(offset) * pageSize;
        }

        if (total - start < pageSize) {
            pageSize = total - start;
        }

        page = new HistoryEntry[](pageSize);
        for (uint256 i = 0; i < pageSize; ) {
            page[i] = all[start + i];
            unchecked {
                ++i;
            }
        }
    }
}
