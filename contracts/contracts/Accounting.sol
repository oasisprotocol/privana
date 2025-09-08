// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {encryptCallData} from "@oasisprotocol/sapphire-contracts/contracts/CalldataEncryption.sol";
import {EIP155Signer} from "@oasisprotocol/sapphire-contracts/contracts/EIP155Signer.sol";

struct EthereumKeypair {
    address addr;
    bytes32 secret;
}

contract Accounting {
    EthereumKeypair private kp;
    mapping(uint256 chainId => uint64 nonce) public nonces;

    constructor(EthereumKeypair memory keypair) payable {
        kp = keypair;
        if (msg.value > 0) {
            payable(kp.addr).transfer(msg.value);
        }
    }

    function transferNativeTokens(
        address to,
        uint256 amount,
        uint256 chainId
    ) external returns (bytes memory output) {
        return
            EIP155Signer.sign(
                kp.addr,
                kp.secret,
                EIP155Signer.EthTx({
                    nonce: nonces[chainId]++,
                    gasPrice: 100_000_000_000,
                    gasLimit: 21000,
                    to: to,
                    value: amount,
                    data: "",
                    chainId: chainId
                })
            );
    }

    function transferERC20Tokens(
        address tokenAddr,
        address to,
        uint256 amount,
        uint256 chainId
    ) external returns (bytes memory output) {
        bytes memory data = abi.encodeWithSignature(
            "transfer(address,uint256)",
            to,
            amount
        );
        return
            EIP155Signer.sign(
                kp.addr,
                kp.secret,
                EIP155Signer.EthTx({
                    nonce: nonces[chainId]++,
                    gasPrice: 100_000_000_000,
                    gasLimit: 100000,
                    to: tokenAddr,
                    value: 0,
                    data: data,
                    chainId: chainId
                })
            );
    }
}
