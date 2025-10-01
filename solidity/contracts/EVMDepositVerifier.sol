// SPDX-License-Identifier: MIT
/* solhint-disable no-console */
pragma solidity ^0.8.20;

import {TokenInfo, EVMKeypair} from "./Types.sol";

import {RLPReader} from "solidity-rlp/contracts/RLPReader.sol";
import {RLPWriter} from "@oasisprotocol/sapphire-contracts/contracts/RLPWriter.sol";

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {EIP155Signer} from "@oasisprotocol/sapphire-contracts/contracts/EIP155Signer.sol";

import {HashiProverLib} from "@hashi/prover/HashiProverLib.sol";
import {ReceiptProof} from "@hashi/prover/HashiProverStructs.sol";
import {IShoyuBashi} from "@hashi/interfaces/IShoyuBashi.sol";
import {TransactionProof} from "./Types.sol";

contract EVMDepositVerifier {
    using RLPReader for RLPReader.RLPItem;
    using RLPReader for RLPReader.Iterator;
    using RLPReader for bytes;

    /**
     * @notice Decodes an EVM transaction and recovers the sender address.
     *
     * This function supports all major EVM transaction types:
     *   - Legacy (type >= 0xc0, RLP-encoded)
     *   - EIP-2930 (type 1, access list)
     *   - EIP-1559 (type 2, dynamic fee)
     *
     * For each type, the function:
     *   1. Parses the transaction type from the first byte.
     *   2. Decodes the RLP-encoded transaction fields into their components.
     *   3. Reconstructs the unsigned transaction (per EIP-155/EIP-2718 rules) for signature recovery.
     *   4. Computes the correct hash for signature recovery (sometimes with a type byte prefix).
     *   5. Recovers the sender address using ECDSA.recover.
     *
     * ---
     * RLP encoding/decoding:
     * - Legacy transactions are fully RLP-encoded (the first byte is >= 0xc0, indicating an RLP list).
     * - Typed transactions (EIP-2930/EIP-1559) have a type byte (0x01 or 0x02) followed by an RLP-encoded list.
     * - RLPReader is used to decode the list into fields (nonce, gas, to, value, data, etc).
     * - RLPWriter is used to reconstruct the unsigned transaction for signature recovery.
     *
     * ---
     * Signature recovery:
     * - For legacy: unsignedHash = keccak256(RLP(list of fields with chainId, 0, 0))
     * - For EIP-2930: unsignedHash = keccak256(0x01 || RLP(list of fields))
     * - For EIP-1559: unsignedHash = keccak256(0x02 || RLP(list of fields))
     * - The recovery id (v) is derived per EIP rules (27/28 for legacy, 27+parity for typed).
     *
     * @param evmTransactionData The raw transaction bytes (RLP-encoded or typed).
     * @return chainId The chain ID the transaction was signed for.
     * @return hash The hash of the signed transaction (for inclusion in a block).
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
        // Parse the transaction type from the first byte of the calldata.
        uint8 transactionType = uint8(evmTransactionData[0]);

        // --- Legacy Transaction (type >= 0xc0) ---
        // These are fully RLP-encoded transactions (no type byte prefix).
        if (transactionType >= 0xc0) {
            // Decode the RLP list into its fields.
            // The RLP list order is: [nonce, gasPrice, gasLimit, to, value, data, v, r, s]
            RLPReader.RLPItem[] memory ls = evmTransactionData
                .toRlpItem()
                .toList();

            // Extract each field from the RLP-decoded list.
            uint256 nonce = ls[0].toUint();
            uint256 gasPrice = ls[1].toUint();
            uint256 gasLimit = ls[2].toUint();
            to = ls[3].toAddress();
            value = ls[4].toUint();
            txData = ls[5].toBytes();
            v = ls[6].toUint();
            r = ls[7].toUint();
            s = ls[8].toUint();

            // Recover the chainId from v (EIP-155 replay protection).
            chainId = (v - 35) / 2;

            // Reconstruct the unsigned transaction for signature recovery:
            // The unsigned tx is RLP([nonce, gasPrice, gasLimit, to, value, data, chainId, 0, 0])
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

            // Hash the RLP-encoded unsigned transaction for signature recovery.
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

            // The transaction hash (for block inclusion) is the keccak256 of the original calldata.
            hash = keccak256(evmTransactionData);

            // --- EIP-2930 Transaction (type 1) ---
        } else if (transactionType == 1) {
            // The transaction hash is the keccak256 of the original calldata.
            hash = keccak256(evmTransactionData);

            // Remove the type byte (0x01) prefix.
            evmTransactionData = evmTransactionData[1:];

            // RLP-decode the remaining bytes into fields.
            // EIP-2930 order: [chainId, nonce, gasPrice, gasLimit, to, value, data, accessList, signatureYParity, signatureR, signatureS]
            RLPReader.RLPItem[] memory ls = evmTransactionData
                .toRlpItem()
                .toList();

            // Extract fields from the RLP list.
            chainId = ls[0].toUint();
            uint256 nonce = ls[1].toUint();
            uint256 gasPrice = ls[2].toUint();
            uint256 gasLimit = ls[3].toUint();
            to = ls[4].toAddress();
            value = ls[5].toUint();
            txData = ls[6].toBytes();
            v = ls[8].toUint(); // signatureYParity {0,1}
            r = ls[9].toUint();
            s = ls[10].toUint();

            // Reconstruct the unsigned transaction for signature recovery:
            // keccak256(0x01 || RLP([chainId, nonce, gasPrice, gasLimit, to, value, data, accessList]))
            bytes[] memory items = new bytes[](8);
            items[0] = RLPWriter.writeUint(chainId);
            items[1] = RLPWriter.writeUint(nonce);
            items[2] = RLPWriter.writeUint(gasPrice);
            items[3] = RLPWriter.writeUint(gasLimit);
            items[4] = RLPWriter.writeAddress(to);
            items[5] = RLPWriter.writeUint(value);
            items[6] = RLPWriter.writeBytes(txData);
            items[7] = ls[7].toRlpBytes(); // accessList

            // Hash with the type byte prefix (0x01) for signature recovery.
            bytes32 unsignedHash = keccak256(
                bytes.concat(bytes1(0x01), RLPWriter.writeList(items))
            );

            // The recovery id for ECDSA is 27 or 28, derived from v (YParity).
            uint8 recoveryV = 27 + uint8(v);

            // Recover the sender address from the signature.
            from = ECDSA.recover(
                unsignedHash,
                uint8(recoveryV),
                bytes32(r),
                bytes32(s)
            );

            // --- EIP-1559 Transaction (type 2) ---
        } else if (transactionType == 2) {
            // The transaction hash is the keccak256 of the original calldata.
            hash = keccak256(evmTransactionData);

            // Remove the type byte (0x02) prefix.
            evmTransactionData = evmTransactionData[1:];

            // RLP-decode the remaining bytes into fields.
            // EIP-1559 order: [chainId, nonce, maxPriorityFeePerGas, maxFeePerGas, gasLimit, to, value, data, accessList, signatureYParity, signatureR, signatureS]
            RLPReader.RLPItem[] memory ls = evmTransactionData
                .toRlpItem()
                .toList();

            // Extract fields from the RLP list.
            chainId = ls[0].toUint();
            uint256 nonce = ls[1].toUint();
            uint256 maxPriorityFeePerGas = ls[2].toUint();
            uint256 maxFeePerGas = ls[3].toUint();
            uint256 gasLimit = ls[4].toUint();
            to = ls[5].toAddress();
            value = ls[6].toUint();
            txData = ls[7].toBytes();
            v = ls[9].toUint(); // signatureYParity {0,1}
            r = ls[10].toUint();
            s = ls[11].toUint();

            // Reconstruct the unsigned transaction for signature recovery:
            // keccak256(0x02 || RLP([chainId, nonce, maxPriorityFeePerGas, maxFeePerGas, gasLimit, to, value, data, accessList]))
            bytes[] memory items = new bytes[](9);
            items[0] = RLPWriter.writeUint(chainId);
            items[1] = RLPWriter.writeUint(nonce);
            items[2] = RLPWriter.writeUint(maxPriorityFeePerGas);
            items[3] = RLPWriter.writeUint(maxFeePerGas);
            items[4] = RLPWriter.writeUint(gasLimit);
            items[5] = RLPWriter.writeAddress(to);
            items[6] = RLPWriter.writeUint(value);
            items[7] = RLPWriter.writeBytes(txData);
            items[8] = ls[8].toRlpBytes(); // accessList

            // Hash with the type byte prefix (0x02) for signature recovery.
            bytes32 unsignedHash = keccak256(
                bytes.concat(bytes1(0x02), RLPWriter.writeList(items))
            );

            // The recovery id for ECDSA is 27 or 28, derived from v (YParity).
            uint8 recoveryV = 27 + uint8(v);

            // Recover the sender address from the signature.
            from = ECDSA.recover(
                unsignedHash,
                uint8(recoveryV),
                bytes32(r),
                bytes32(s)
            );
        } else {
            revert("Unknown transaction type");
        }
    }

    /**
     * @notice Decodes ERC-20 transfer calldata to extract the recipient and amount.
     *
     * This function expects calldata for the ERC-20 `transfer(address,uint256)` function,
     * which is always 68 bytes long:
     *   - 4 bytes: function selector (keccak256("transfer(address,uint256)")[:4])
     *   - 32 bytes: recipient address (left-padded to 32 bytes)
     *   - 32 bytes: amount (uint256, left-padded to 32 bytes)
     *
     * The function:
     *   1. Checks the calldata length is exactly 68 bytes.
     *   2. Verifies the function selector matches `transfer(address,uint256)`.
     *   3. Extracts the recipient address and amount using assembly for efficiency.
     *   4. Returns the decoded address and amount.
     *
     * ---
     * Calldata layout:
     *   [0..4):   selector
     *   [4..36):  address (left-padded)
     *   [36..68): amount (left-padded)
     *
     * Solidity's ABI encoding pads dynamic types to 32 bytes, so the address is right-aligned
     * in its 32-byte slot. We use assembly to extract and cast it to the correct type.
     *
     * @param txData The calldata for the ERC-20 transfer (should be 68 bytes).
     * @return to The recipient address.
     * @return amount The amount to transfer.
     */
    function decodeTxDataForErc20Transfer(
        bytes memory txData
    ) internal returns (address to, uint256 amount) {
        // 1. Check calldata length: selector (4) + address (32) + amount (32) = 68 bytes
        require(txData.length == 4 + 32 + 32, "Invalid txData length");

        // 2. Compute the ERC-20 transfer selector: bytes4(keccak256("transfer(address,uint256)"))
        bytes4 selector = bytes4(keccak256("transfer(address,uint256)"));

        // 3. Extract the selector from calldata (first 4 bytes)
        bytes4 selectorFromTx;
        assembly {
            // mload reads 32 bytes, so we offset by 32 to get the first 32 bytes of data
            // The selector is in the first 4 bytes of this word
            selectorFromTx := mload(add(txData, 32))
        }

        // 4. Check that the selector matches the expected transfer selector
        require(
            selectorFromTx == selector,
            "txData does not start with transfer selector"
        );

        // 5. Extract the recipient address and amount
        //    - Address is in bytes 4..36 (32 bytes, right-aligned)
        //    - Amount is in bytes 36..68 (32 bytes)
        bytes32 toBytes;
        bytes32 amountBytes;
        assembly {
            // toBytes: load 32 bytes at offset 36 (selector + address)
            toBytes := mload(add(txData, 36))
            // amountBytes: load 32 bytes at offset 68 (selector + address + amount)
            amountBytes := mload(add(txData, 68))
        }
        // Convert the 32-byte word to an address (last 20 bytes)
        to = address(uint160(uint256(toBytes)));
        // Convert the 32-byte word to uint256
        amount = uint256(amountBytes);
    }

    /**
     * @notice Verifies that a transaction was included in a block using Hashi proof verification.
     *
     * This function uses the Hashi protocol to cryptographically verify that a transaction
     * was actually included in a specific block on the source chain. It validates:
     *   1. The block header integrity and finality
     *   2. The transaction's inclusion in the block's transaction trie
     *   3. The transaction receipt's inclusion in the block's receipt trie
     *
     * The verification process involves:
     *   - Merkle proof validation against the block's transaction root
     *   - Receipt proof validation against the block's receipt root
     *   - Optional ancestral block verification for finality requirements
     *
     * @param receiptProof The Hashi proof structure containing all verification data
     * @return bool True if the transaction proof is cryptographically valid
     */
    function verifyTransactionProof(
        bytes32 transactionHash,
        ReceiptProof memory receiptProof
    ) internal view returns (bool) {
        // Use Hashi's HashiProverLib to verify the receipt proof
        // This will:
        // 1. Validate the block header(s) provided in the proof
        // 2. Verify the Merkle proof that the receipt exists at the given transaction index
        // 3. Ensure the receipt trie root in the block header matches the proof
        // 4. Check ancestral block relationships if required for finality

        try HashiProverLib.verifyForeignProof(receiptProof, shoyuBashi) {
            return true;
        } catch {
            return false;
        }
    }
}
