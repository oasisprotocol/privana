// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {EIP712SignatureVerifier} from "./EIP712SignatureVerifier.sol";
import {
    HistoryEntry,
    HistoryKind,
    TokenInfo,
    TokenType,
    UserInfo
} from "./Types.sol";
import {IAccountingSigner} from "./interfaces/IAccountingSigner.sol";
import {OwnableUpgradeable} from "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";

abstract contract AccountingStorage is EIP712SignatureVerifier, OwnableUpgradeable {
    /// @dev internal so test helpers and delegated modules can access ledger state.
    mapping(address user => mapping(bytes32 tokenId => uint256 balance))
        internal balances;
    mapping(bytes32 tokenId => TokenInfo tokenInfo) public tokens;
    mapping(bytes32 depositId => bool processed) public processedDeposits;
    mapping(address user => UserInfo) internal userInfo;

    WithdrawalRequest[] public withdrawals;

    uint256 internal nextLockId;

    bytes32[] internal registeredTokenIds;

    mapping(bytes32 requestId => EmergencyWithdrawRequest) public emergencyWithdrawRequests;
    mapping(address user => HistoryEntry[] entries) internal history;
    address public historyModule;
    IAccountingSigner public signer;
    bytes21 public roflAppID;

    struct EmergencyWithdrawRequest {
        address toAddress;
        uint256 blockNumber; // 0 ⇒ slot empty
    }

    struct WithdrawalRequest {
        address userAddress;
        address toAddress;
        uint256 amount;
        uint256 blockNumber;
        bytes32 tokenId;
        bool resolved;
        bytes txIdentifier; // nonce, utxo identifier, or similar
    }

    error EmergencyWithdrawTooSoon();
    error EmergencyWithdrawNotFound();
    error InsufficientBalance();
    error TooManyActiveLocks();
    error InvalidLockId();
    error LockNotExpired();
    error InsufficientLockedAmount();
    error AddressMismatch();
    error InvalidExpiry();
    error InvalidAmount();
    error WithdrawalTooSoon();
    error Unauthorized();
    error InvalidAddress();
    error DepositAlreadyProcessed();
    error InvalidHistoryModule();
    error InvalidSiweAuth();
    error InvalidSigner();
    error NotAuthorizedROFL();
    error RoflSignerNotSet();
    error GasPriceNotSet(uint256 chainId);
    error InvalidGasPrice();
    error UnsupportedChainType();

    event EmergencyWithdrawRequested(bytes32 indexed requestId, bytes32 indexed tokenId);
    event EmergencyWithdrawExecuted(bytes32 indexed requestId);
    event HistoryModuleSet(address indexed module);
    event SignerSet(address indexed signer);
    event GasPriceSet(uint256 indexed chainId, uint256 gasPrice);
    event RoflSignerUpdated(address indexed newSigner);

    event Deposit(
        bytes32 indexed tokenId,
        uint256 amount,
        bytes32 depositId
    );

    event Withdrawal(
        address indexed userAddress,
        bytes32 indexed tokenId,
        uint256 amount,
        uint256 chainId
    );

    event WithdrawalResolved(
        uint256 indexed index,
        address indexed userAddress,
        bytes32 indexed tokenId,
        address toAddress,
        uint256 amount,
        uint256 chainId
    );

    event TokenRegistered(bytes32 indexed tokenId, TokenType tokenType);

    function _signer()
        internal
        view
        returns (IAccountingSigner signerContract)
    {
        signerContract = signer;
        if (address(signerContract) == address(0)) {
            revert InvalidSigner();
        }
    }

    function _appendHistory(
        address user,
        HistoryKind kind,
        bytes memory payload
    ) internal {
        history[user].push(
            HistoryEntry({
                kind: kind,
                timestamp: uint64(block.timestamp),
                payload: payload
            })
        );
    }

    function _appendTransferHistory(
        address fromAddress,
        address toAddress,
        HistoryKind kind,
        bytes memory payload
    ) internal {
        _appendHistory(fromAddress, kind, payload);
        if (toAddress != address(0) && toAddress != fromAddress) {
            _appendHistory(toAddress, kind, payload);
        }
    }

    function _appendUserCounterpartyHistory(
        address user,
        HistoryKind kind,
        bytes32 tokenId,
        uint256 amount,
        address counterparty
    ) internal {
        _appendHistory(
            user,
            kind,
            abi.encodePacked(tokenId, amount, counterparty)
        );
    }

    function _appendTransferHistoryForParticipants(
        address fromAddress,
        address toAddress,
        HistoryKind kind,
        bytes32 tokenId,
        uint256 amount
    ) internal {
        _appendTransferHistory(
            fromAddress,
            toAddress,
            kind,
            abi.encodePacked(tokenId, amount, fromAddress, toAddress)
        );
    }

    uint256[39] private __gap;
}
