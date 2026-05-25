// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {EVMSignerAndVerifier} from "../EVMSignerAndVerifier.sol";
import {EIP712SignatureVerifier} from "../EIP712SignatureVerifier.sol";
import {HistoryEntry, HistoryKind, TokenInfo, UserInfo} from "../Types.sol";
import {IAccountingSiweAuth} from "../interfaces/IAccountingSiweAuth.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

/**
 * @title MockAccountingPrevious
 * @notice Storage-layout reference for the pre-AccountingHistoryModule Accounting proxy.
 */
contract MockAccountingPrevious is
    EIP712SignatureVerifier,
    EVMSignerAndVerifier,
    UUPSUpgradeable
{
    /// @custom:oz-upgrades-unsafe-allow state-variable-immutable
    IAccountingSiweAuth public immutable siweAuth;
    mapping(address user => mapping(bytes32 tokenId => uint256 balance))
        internal balances;
    mapping(bytes32 tokenId => TokenInfo tokenInfo) public tokens;
    mapping(bytes32 depositId => bool processed) public processedDeposits;
    mapping(address user => UserInfo) private userInfo;

    WithdrawalRequest[] public withdrawals;

    uint256 private nextLockId;

    bytes32[] private registeredTokenIds;

    mapping(bytes32 requestId => EmergencyWithdrawRequest)
        public emergencyWithdrawRequests;
    mapping(address user => HistoryEntry[] entries) private history;

    struct EmergencyWithdrawRequest {
        address toAddress;
        uint256 blockNumber;
    }

    struct WithdrawalRequest {
        address userAddress;
        address toAddress;
        uint256 amount;
        uint256 blockNumber;
        bytes32 tokenId;
        bool resolved;
        bytes txIdentifier;
    }

    // Test keypair: #4 of "chimney theory present latin find behave ankle clock shadow earn suit reflect"
    address private constant TEST_ADDRESS =
        0xe6F321Fb3D912Db48DE460560B8bB99B57AeAcA2;
    bytes32 private constant TEST_SECRET =
        bytes32(
            0x9147e5178b1ee427d704dcdb699f1adf9c8a3b58480a6118635a3486ad3a35ce
        );

    /// @custom:oz-upgrades-unsafe-allow constructor
    /// @custom:oz-upgrades-unsafe-allow state-variable-immutable
    constructor(address siweAuthAddress) {
        _disableInitializers();
        siweAuth = IAccountingSiweAuth(siweAuthAddress);
    }

    function initialize(
        bytes21 _roflAppID,
        address _owner
    ) external initializer {
        __EIP712SignatureVerifier_init();
        __EVMSignerAndVerifier_init(_roflAppID, _owner);
        nextLockId = 1;
    }

    function _generateKeypair()
        internal
        pure
        override
        returns (address, bytes32)
    {
        return (TEST_ADDRESS, TEST_SECRET);
    }

    function _authorizeUpgrade(
        address newImplementation
    ) internal override onlyOwner {}

    function mockAppendHistory(
        address user,
        HistoryKind kind,
        bytes calldata payload
    ) external {
        history[user].push(
            HistoryEntry({
                kind: kind,
                timestamp: uint64(block.timestamp),
                payload: payload
            })
        );
    }

    uint256[40] private __gap;
}
