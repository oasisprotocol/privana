// SPDX-License-Identifier: MIT
/* solhint-disable no-console */
pragma solidity ^0.8.20;

import {TokenInfo, EVMKeypair} from "./Types.sol";

import {RLPReader} from "solidity-rlp/contracts/RLPReader.sol";
import {RLPWriter} from "@oasisprotocol/sapphire-contracts/contracts/RLPWriter.sol";

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {EIP155Signer} from "@oasisprotocol/sapphire-contracts/contracts/EIP155Signer.sol";

import {TransactionProof} from "./Types.sol";

import "hardhat/console.sol";

contract EVMDepositVerifier {
    using RLPReader for RLPReader.RLPItem;
    using RLPReader for RLPReader.Iterator;
    using RLPReader for bytes;

    /**
     * @notice Decodes an EVM transaction and recovers the sender address.
     * @dev Handles RLP-encoded transactions (the current Ethereum transaction format).
     *      The function extracts all fields, reconstructs the unsigned transaction for EIP-155,
     *      and recovers the sender address using the signature (v, r, s).
     *      The recovery id for ECDSA is always 27 or 28, derived from the EIP-155 v value.
     * @param evmTransactionData The raw RLP-encoded transaction bytes.
     * @return chainId The chain ID the transaction was signed for.
     * @return hash The hash of the signed transaction.
     * @return from The recovered sender address.
     * @return to The recipient address.
     * @return value The amount of ETH sent.
     * @return txData The calldata for the transaction.
     * @return v The signature v value (raw, as in the transaction).
     * @return r The signature r value.
     * @return s The signature s value.
     */
    function decodeEVMTransaction(
        bytes calldata evmTransactionData
    )
        internal
        returns (
            uint256 chainId,
            bytes32 hash,
            address from,
            address to,
            uint256 value,
            bytes memory txData,
            uint256 v,
            uint256 r,
            uint256 s
        )
    {
        // Parse the transaction type from the first byte.
        uint8 transactionType = uint8(evmTransactionData[0]);

        // Handle RLP-encoded transactions (transactionType >= 0xc0).
        if (transactionType >= 0xc0) {
            // Decode the RLP list into its fields.
            RLPReader.RLPItem[] memory ls = evmTransactionData
                .toRlpItem()
                .toList();

            // Extract transaction fields from the RLP list.
            // Field order: [nonce, gasPrice, gasLimit, to, value, data, v, r, s]
            uint256 nonce = ls[0].toUint();
            uint256 gasPrice = ls[1].toUint();
            uint256 gasLimit = ls[2].toUint();
            to = ls[3].toAddress();
            value = ls[4].toUint();
            txData = ls[5].toBytes();
            v = ls[6].toUint();
            r = ls[7].toUint();
            s = ls[8].toUint();

            // EIP-155: v = {0,1} + chainId * 2 + 35
            // Recover the chainId from v. We use the rounding here to get around the parity issue.
            chainId = (v - 35) / 2;

            // Reconstruct the RLP-encoded unsigned transaction for signature recovery:
            // [nonce, gasPrice, gasLimit, to, value, data, chainId, 0, 0]
            bytes[] memory items = new bytes[](9);
            items[0] = RLPWriter.writeUint(nonce);
            items[1] = RLPWriter.writeUint(gasPrice);
            items[2] = RLPWriter.writeUint(gasLimit);
            items[3] = RLPWriter.writeAddress(to);
            items[4] = RLPWriter.writeUint(value);
            items[5] = RLPWriter.writeBytes(txData);
            items[6] = RLPWriter.writeUint(chainId);
            items[7] = RLPWriter.writeUint(0);
            items[8] = RLPWriter.writeUint(0);

            bytes32 unsignedHash = keccak256(RLPWriter.writeList(items));

            // The recovery id for ECDSA is always 27 or 28, derived from v.
            uint8 recoveryV = uint8(((v - 1) % 2) + 27);

            // Recover the sender address from the signature.
            from = ECDSA.recover(
                unsignedHash,
                uint8(recoveryV),
                bytes32(r),
                bytes32(s)
            );

            hash = keccak256(evmTransactionData);
        } else if (transactionType == 1) {
            // Typed transaction (EIP-2930)

            console.log("Typed transaction (EIP-2930)");

            hash = keccak256(evmTransactionData);

            // Strip first byte (0x01)
            evmTransactionData = evmTransactionData[1:];

            // 0x01 || rlp([chainId, nonce, gasPrice, gasLimit, to, value, data, accessList, signatureYParity, signatureR, signatureS])
            // Decode the RLP list into its fields.
            RLPReader.RLPItem[] memory ls = evmTransactionData
                .toRlpItem()
                .toList();

            console.log("Got here");

            // Extract transaction fields from the RLP list.
            // Field order: [nonce, gasPrice, gasLimit, to, value, data, v, r, s]
            chainId = ls[0].toUint();
            uint256 nonce = ls[1].toUint();
            uint256 gasPrice = ls[2].toUint();
            uint256 gasLimit = ls[3].toUint();
            to = ls[4].toAddress();
            value = ls[5].toUint();
            txData = ls[6].toBytes();
            v = ls[8].toUint();
            r = ls[9].toUint();
            s = ls[10].toUint();

            console.log("Got here 2");
            console.log(chainId, nonce, gasPrice);
            console.log(gasLimit, to, value);
            console.log(v, r, s);

            console.log("Got here 3");
            // Reconstruct the RLP-encoded unsigned transaction for signature recovery:
            // [nonce, gasPrice, gasLimit, to, value, data, chainId, 0, 0]
            bytes[] memory items = new bytes[](9);
            items[0] = RLPWriter.writeUint(chainId);
            items[1] = RLPWriter.writeUint(nonce);
            items[2] = RLPWriter.writeUint(gasPrice);
            items[3] = RLPWriter.writeUint(gasLimit);
            items[4] = RLPWriter.writeAddress(to);
            items[5] = RLPWriter.writeUint(value);
            items[6] = RLPWriter.writeBytes(txData);
            items[7] = ls[7].toBytes();

            console.log("Got here 4");
            // The signatureYParity, signatureR, signatureS elements of this
            // transaction represent a secp256k1 signature over
            // keccak256(0x01 || rlp([chainId, nonce, gasPrice, gasLimit, to, value, data, accessList])).
            bytes32 unsignedHash = keccak256(RLPWriter.writeList(items));

            // The recovery id for ECDSA is always 27 or 28, derived from v.
            uint8 recoveryV = 27 + uint8(v);

            console.log("Got here 5");

            // Recover the sender address from the signature.
            from = ECDSA.recover(
                unsignedHash,
                uint8(recoveryV),
                bytes32(r),
                bytes32(s)
            );

            console.log("Got here 6");

            console.log("Recovered from:", from);
        } else if (transactionType == 2) {
            revert("Typed transactions not yet supported");
        } else {
            revert("Unknown transaction type");
        }
    }

    function decodeTxDataForErc20Transfer(
        bytes memory txData
    ) internal returns (address to, uint256 amount) {
        require(txData.length == 4 + 32 + 32, "Invalid txData length");
        // ERC-20 transfer function selector is the first 4 bytes of keccak256("transfer(address,uint256)")
        bytes4 selector = bytes4(keccak256("transfer(address,uint256)"));

        bytes4 selectorFromTx;
        assembly {
            selectorFromTx := mload(add(txData, 32))
        }

        require(
            selectorFromTx == selector,
            "txData does not start with transfer selector"
        );

        // Decode the 'to' address (next 32 bytes, right-padded)
        bytes32 toBytes;
        bytes32 amountBytes;
        assembly {
            toBytes := mload(add(txData, 36)) // 32 bytes after selector (4 + 32)
            amountBytes := mload(add(txData, 68)) // 32 bytes after selector + address (4 + 32 + 32)
        }
        to = address(uint160(uint256(toBytes)));
        amount = uint256(amountBytes);
    }

    function verifyEVMNativeDeposit(
        bytes memory evmTransactionData
    ) internal returns (bool) {
        return true;
    }

    function verifyEVMErc20Deposit(
        bytes memory evmTransactionData
    ) internal returns (bool) {
        return true;
    }
}
