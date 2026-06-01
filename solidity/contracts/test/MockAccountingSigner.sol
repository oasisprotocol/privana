// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {AccountingSigner} from "../AccountingSigner.sol";
import {ChainType} from "../Types.sol";

contract MockAccountingSigner is AccountingSigner {
    // Test keypair: #4 of "chimney theory present latin find behave ankle clock shadow earn suit reflect"
    address private constant TEST_ADDRESS = 0xe6F321Fb3D912Db48DE460560B8bB99B57AeAcA2;
    bytes32 private constant TEST_SECRET = bytes32(0x9147e5178b1ee427d704dcdb699f1adf9c8a3b58480a6118635a3486ad3a35ce);

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() AccountingSigner() {}

    function initialize(
        address _owner,
        address _accounting
    ) external override initializer {
        __EVMSignerAndVerifier_init(_owner);
        _setAccounting(_accounting);
    }

    function _generateKeypair() internal pure override returns (address, bytes32) {
        return (TEST_ADDRESS, TEST_SECRET);
    }

    function _generateSweepNativeTransfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    ) internal pure override returns (bytes memory signedTx) {
        return
            abi.encode(
                "native-sweep",
                beneficiary,
                chainType,
                version,
                chainId,
                amount,
                sourceChainNonce,
                gasPrice
            );
    }

    function _generateSweepERC20Transfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        address tokenAddress,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    ) internal pure override returns (bytes memory signedTx) {
        return
            abi.encode(
                "erc20-sweep",
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

    function _generateGasFundingTx(
        address toDepositAddress,
        uint256 chainId,
        uint256 gasAmount,
        uint64 gasTankNonce,
        uint256 gasPrice
    ) internal pure override returns (bytes memory signedTx) {
        return
            abi.encode(
                "gas-funding",
                toDepositAddress,
                chainId,
                gasAmount,
                gasTankNonce,
                gasPrice
            );
    }

}
