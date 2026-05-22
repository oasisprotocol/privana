// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {HistoryEntry, HistoryKind} from "./Types.sol";
import {IAccountingSiweAuth} from "./interfaces/IAccountingSiweAuth.sol";
import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {OwnableUpgradeable} from "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

/**
 * @title AccountingHistory
 * @notice Confidential per-user accounting history storage.
 * @dev Accounting is the only writer. Reads are authenticated directly with the
 *      same SIWE token semantics.
 */
contract AccountingHistory is
    Initializable,
    OwnableUpgradeable,
    UUPSUpgradeable
{
    uint256 private constant MAX_HISTORY_PAGE_SIZE = 100;

    /// @custom:oz-upgrades-unsafe-allow state-variable-immutable
    IAccountingSiweAuth public immutable siweAuth;

    address public accounting;
    mapping(address user => HistoryEntry[] entries) private history;

    error InvalidAccounting();
    error InvalidSiweAuth();
    error NotAccounting();
    error Unauthorized();
    error OwnershipCannotBeRenounced();

    modifier onlyAccounting() {
        if (msg.sender != accounting) revert NotAccounting();
        _;
    }

    /// @custom:oz-upgrades-unsafe-allow constructor
    /// @custom:oz-upgrades-unsafe-allow state-variable-immutable
    constructor(address siweAuthAddress) {
        _disableInitializers();
        if (siweAuthAddress == address(0)) revert InvalidSiweAuth();
        siweAuth = IAccountingSiweAuth(siweAuthAddress);
    }

    function initialize(
        address _accounting,
        address _owner
    ) external initializer {
        if (_accounting == address(0) || _owner == address(0)) {
            revert InvalidAccounting();
        }
        __Ownable_init(_owner);
        accounting = _accounting;
    }

    function _authorizeUpgrade(
        address newImplementation
    ) internal override onlyOwner {}

    function renounceOwnership() public pure override {
        revert OwnershipCannotBeRenounced();
    }

    function _authSender(bytes memory token) internal view returns (address) {
        if (token.length != 0) {
            return siweAuth.authSender(token);
        }
        return msg.sender;
    }

    function appendHistory(
        address user,
        HistoryKind kind,
        bytes calldata payload
    ) external onlyAccounting {
        _append(user, kind, payload);
    }

    function _append(
        address user,
        HistoryKind kind,
        bytes calldata payload
    ) internal {
        history[user].push(
            HistoryEntry({
                kind: kind,
                timestamp: uint64(block.timestamp),
                payload: payload
            })
        );
    }

    function appendPairedHistory(
        address fromAddress,
        address toAddress,
        HistoryKind kind,
        bytes calldata payload
    ) external onlyAccounting {
        _append(fromAddress, kind, payload);
        if (toAddress != address(0) && toAddress != fromAddress) {
            _append(toAddress, kind, payload);
        }
    }

    function getHistory(
        int256 offset,
        uint256 limit,
        bytes calldata token
    ) external view returns (HistoryEntry[] memory page, uint256 total) {
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

    uint256[49] private __gap;
}
