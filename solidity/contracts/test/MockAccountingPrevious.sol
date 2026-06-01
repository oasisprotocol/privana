// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {EVMSignerAndVerifier} from "../EVMSignerAndVerifier.sol";
import {EIP712SignatureVerifier} from "../EIP712SignatureVerifier.sol";
import {TokenInfo, UserInfo} from "../Types.sol";
import {IAccountingSiweAuth} from "../interfaces/IAccountingSiweAuth.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

/**
 * @title MockAccountingPrevious
 * @notice Storage-layout reference for the deployed Accounting proxy immediately
 *         before the AccountingHistoryModule extraction — i.e. the bridge layout
 *         (`_ledgerTotal` / `roflBridgeAddress` / `bridgeBurnRequests` at slots
 *         108-110) with `__gap` reserving slot 111 onward. This mirrors the live
 *         sapphire-testnet impl: `history` and `historyModule` are NOT inline
 *         here; the history-module upgrade adds them by consuming the first two
 *         gap slots. The migration test upgrades this layout to `MockAccounting`
 *         to prove that addition moves no deployed variable.
 */
contract MockAccountingPrevious is EIP712SignatureVerifier, EVMSignerAndVerifier, UUPSUpgradeable {
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

    mapping(bytes32 requestId => EmergencyWithdrawRequest) public emergencyWithdrawRequests;

    // ─── Bridge state (slots 108-110), mirroring AccountingStorage ────────
    mapping(bytes32 tokenId => uint256) internal _ledgerTotal;
    mapping(uint256 chainId => address) public roflBridgeAddress;
    mapping(bytes32 depositId => BridgeBurnRequest) internal bridgeBurnRequests;

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

    struct BridgeBurnRequest {
        uint256 chainId;
        address bridge;
        uint256 amount;
        uint64 nonce;
        bool exists;
    }

    // Test keypair: #4 of "chimney theory present latin find behave ankle clock shadow earn suit reflect"
    address private constant TEST_ADDRESS = 0xe6F321Fb3D912Db48DE460560B8bB99B57AeAcA2;
    bytes32 private constant TEST_SECRET = bytes32(0x9147e5178b1ee427d704dcdb699f1adf9c8a3b58480a6118635a3486ad3a35ce);

    /// @custom:oz-upgrades-unsafe-allow constructor
    /// @custom:oz-upgrades-unsafe-allow state-variable-immutable
    constructor(address siweAuthAddress) {
        _disableInitializers();
        siweAuth = IAccountingSiweAuth(siweAuthAddress);
    }

    function initialize(bytes21 _roflAppID, address _owner) external initializer {
        __EIP712SignatureVerifier_init();
        __EVMSignerAndVerifier_init(_roflAppID, _owner);
        nextLockId = 1;
    }

    function _generateKeypair() internal pure override returns (address, bytes32) {
        return (TEST_ADDRESS, TEST_SECRET);
    }

    function _authorizeUpgrade(address newImplementation) internal override onlyOwner {}

    /// @notice Test helper to seed bridge-slot state (slot 108) so the migration
    ///         test can assert it survives the history-module upgrade.
    function mockSetLedgerTotal(bytes32 tokenId, uint256 amount) external {
        _ledgerTotal[tokenId] = amount;
    }

    uint256[38] private __gap;
}
