// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {encryptCallData} from "@oasisprotocol/sapphire-contracts/contracts/CalldataEncryption.sol";
import {EIP155Signer} from "@oasisprotocol/sapphire-contracts/contracts/EIP155Signer.sol";

import {TokenInfo, EVMKeypair} from "./Types.sol";

contract EVMWithdrawalSigner {
    mapping(uint256 chainId => uint64) public nonces;
    mapping(uint256 chainId => uint256) public gasPrice;
    // Gas limit and gas price variables
    uint64 public gasLimitNative = 21000;
    uint64 public gasLimitERC20 = 100000;
    // Keypairs are derived from the Sapphire KMS TODO: ask Bernhard how to do that correctly

    function deriveEVMKeypair() internal view returns (EVMKeypair memory kp) {
        // Derive the EVM keypair from the Sapphire KMS
        kp = EVMKeypair({
            addr: 0xD93F26962F74892A9f9F704CeB441C35C3A07405,
            secret: 0x958eb779a4aa0546fe2d1ed08bed7fb490c3b46525d5a3e6b31374ee7f9da0b9
        });
    }

    function getEVMDepositAddress() public view returns (address) {
        EVMKeypair memory kp = deriveEVMKeypair();
        return kp.addr;
    }

    function generateNativeTransfer(
        uint256 chainId,
        address userAddress,
        uint256 amount
    ) internal returns (bytes memory output) {
        EVMKeypair memory kp = deriveEVMKeypair();

        return
            EIP155Signer.sign(
                kp.addr,
                kp.secret,
                EIP155Signer.EthTx({
                    nonce: nonces[chainId]++,
                    gasPrice: gasPrice[chainId],
                    gasLimit: gasLimitNative,
                    to: userAddress,
                    value: amount,
                    data: "",
                    chainId: chainId
                })
            );
    }

    function generateERC20Transfer(
        uint256 chainId,
        address userAddress,
        address tokenAddress,
        uint256 amount
    ) internal returns (bytes memory output) {
        EVMKeypair memory kp = deriveEVMKeypair();

        bytes memory data = abi.encodeWithSignature(
            "transfer(address,uint256)",
            userAddress,
            amount
        );
        return
            EIP155Signer.sign(
                kp.addr,
                kp.secret,
                EIP155Signer.EthTx({
                    nonce: nonces[chainId]++,
                    gasPrice: gasPrice[chainId],
                    gasLimit: gasLimitERC20,
                    to: tokenAddress,
                    value: 0,
                    data: data,
                    chainId: chainId
                })
            );
    }
}
