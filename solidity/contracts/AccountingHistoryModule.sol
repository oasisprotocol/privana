// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {HistoryEntry} from "./Types.sol";
import {AccountingStorage} from "./AccountingStorage.sol";
import {IAccountingForHistoryModule} from "./interfaces/IAccountingForHistoryModule.sol";
import {IAccountingHistoryModule} from "./interfaces/IAccountingHistoryModule.sol";

/**
 * @title AccountingHistoryModule
 * @notice Delegated history code for Accounting-owned history storage.
 * @dev This contract is never called as storage owner. Accounting delegatecalls
 *      into it so the history mapping remains in the Accounting proxy layout.
 */
contract AccountingHistoryModule is AccountingStorage, IAccountingHistoryModule {
    /// @dev Used by deploy/backend validation to reject linking the wrong code.
    bytes32 public constant MODULE_ID =
        keccak256("privana.accounting.historyModule.v1");
    uint256 private constant MAX_HISTORY_PAGE_SIZE = 100;
    address private immutable SELF = address(this);

    error NotDelegated();
    modifier onlyDelegateCall() {
        if (address(this) == SELF) revert NotDelegated();
        _;
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
        HistoryEntry[] storage all = history[user];
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
