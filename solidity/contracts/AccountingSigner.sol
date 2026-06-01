// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ChainType, TokenType, UnsupportedTokenType} from "./Types.sol";
import {EVMSignerAndVerifier} from "./EVMSignerAndVerifier.sol";
import {TokenCodec} from "./lib/TokenCodec.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

contract AccountingSigner is EVMSignerAndVerifier, UUPSUpgradeable {
    bytes32 public constant SIGNER_ID =
        keccak256("privana.accounting.signer.v1");

    address public accounting;

    error NotAccounting();
    error OwnershipRenounceDisabled();

    event AccountingSet(address indexed accounting);

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(
        address _owner,
        address _accounting
    ) external virtual initializer {
        __EVMSignerAndVerifier_init(_owner);
        _setAccounting(_accounting);
    }

    function _authorizeUpgrade(
        address
    ) internal view override onlyOwner {}

    /// @dev Ownership renunciation is disabled to prevent bricking upgrades/config.
    function renounceOwnership() public pure override {
        revert OwnershipRenounceDisabled();
    }

    function transferOwnership(address newOwner) public override onlyAccounting {
        if (newOwner == address(0)) revert OwnableInvalidOwner(address(0));
        _transferOwnership(newOwner);
    }

    modifier onlyAccounting() {
        if (msg.sender != accounting) revert NotAccounting();
        _;
    }

    function _setAccounting(address _accounting) internal {
        if (_accounting == address(0)) revert InvalidAddress();
        if (_accounting.code.length == 0) revert InvalidAddress();
        accounting = _accounting;
        emit AccountingSet(_accounting);
    }

    function setGasPrice(
        uint256 chainId,
        uint256 gasPrice
    ) external onlyAccounting {
        _setGasPrice(chainId, gasPrice);
    }

    function setRoflSignerAddress(
        address newSigner
    ) external onlyAccounting {
        _setRoflSignerAddress(newSigner);
    }

    function getDepositAddress(
        address beneficiary,
        ChainType chainType,
        uint256 version
    ) external view onlyAccounting returns (address depositAddr) {
        (depositAddr, ) = _deriveDepositKeypair(
            beneficiary,
            chainType,
            version
        );
    }

    function _reserveWithdrawalNonce(
        uint256 chainId
    ) private returns (uint64 nonce) {
        if (gasPrices[chainId] == 0) revert GasPriceNotSet(chainId);
        return getEVMNonceAndIncrement(chainId);
    }

    function reserveTokenWithdrawalNonce(
        TokenType tokenType,
        bytes calldata tokenData
    ) external onlyAccounting returns (uint256 chainId, uint64 nonce) {
        (chainId, ) = _decodeEVMToken(tokenType, tokenData);
        nonce = _reserveWithdrawalNonce(chainId);
    }

    function generateTokenWithdrawalTransfer(
        TokenType tokenType,
        bytes calldata tokenData,
        address toAddress,
        uint256 amount,
        uint64 nonce
    )
        external
        view
        onlyAccounting
        returns (uint256 chainId, bytes memory signedTx)
    {
        if (tokenType == TokenType.NativeEVM) {
            chainId = TokenCodec.decodeEVMNativeTokenData(tokenData);
            signedTx = _generateNativeTransfer(
                chainId,
                toAddress,
                amount,
                nonce
            );
        } else if (tokenType == TokenType.ERC20) {
            address tokenAddress;
            (chainId, tokenAddress) = TokenCodec.decodeEVMErc20TokenData(
                tokenData
            );
            signedTx = _generateERC20Transfer(
                chainId,
                toAddress,
                tokenAddress,
                amount,
                nonce
            );
        } else {
            revert UnsupportedTokenType();
        }
    }

    function generateDepositAddressTokenTransfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        TokenType tokenType,
        bytes calldata tokenData,
        address toAddress,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    )
        external
        view
        onlyAccounting
        returns (bytes memory signedTx)
    {
        if (tokenType == TokenType.NativeEVM) {
            uint256 chainId = TokenCodec.decodeEVMNativeTokenData(tokenData);
            return
                _generateDepositAddressTransfer(
                    beneficiary,
                    chainType,
                    version,
                    chainId,
                    toAddress,
                    amount,
                    sourceChainNonce,
                    gasPrice
                );
        }

        if (tokenType == TokenType.ERC20) {
            (uint256 chainId, address tokenAddress) = TokenCodec
                .decodeEVMErc20TokenData(tokenData);
            return
                _generateDepositAddressERC20Transfer(
                    beneficiary,
                    chainType,
                    version,
                    chainId,
                    toAddress,
                    tokenAddress,
                    amount,
                    sourceChainNonce,
                    gasPrice
                );
        }

        revert UnsupportedTokenType();
    }

    function generateSweepNativeTransfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    )
        external
        view
        onlyAccounting
        returns (bytes memory signedTx)
    {
        return
            _generateSweepNativeTransfer(
                beneficiary,
                chainType,
                version,
                chainId,
                amount,
                sourceChainNonce,
                gasPrice
            );
    }

    function generateSweepERC20Transfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        address tokenAddress,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    )
        external
        view
        onlyAccounting
        returns (bytes memory signedTx)
    {
        return
            _generateSweepERC20Transfer(
                beneficiary,
                chainType,
                version,
                chainId,
                tokenAddress,
                amount,
                sourceChainNonce,
                gasPrice
            );
    }

    function generateGasFundingTx(
        address toDepositAddress,
        uint256 chainId,
        uint256 gasAmount,
        uint64 gasTankNonce,
        uint256 gasPrice
    )
        external
        view
        onlyAccounting
        returns (bytes memory signedTx)
    {
        return
            _generateGasFundingTx(
                toDepositAddress,
                chainId,
                gasAmount,
                gasTankNonce,
                gasPrice
            );
    }

    function _decodeEVMToken(
        TokenType tokenType,
        bytes memory tokenData
    ) private pure returns (uint256 chainId, address tokenAddress) {
        if (tokenType == TokenType.NativeEVM) {
            return (TokenCodec.decodeEVMNativeTokenData(tokenData), address(0));
        }
        if (tokenType == TokenType.ERC20) {
            return TokenCodec.decodeEVMErc20TokenData(tokenData);
        }
        revert UnsupportedTokenType();
    }
}
