// SPDX-License-Identifier: MIT
/* solhint-disable no-console */
pragma solidity ^0.8.20;

import {TokenInfo, EVMKeypair} from "./Types.sol";

import {RLPReader} from "solidity-rlp/contracts/RLPReader.sol";
import {RLPWriter} from "@oasisprotocol/sapphire-contracts/contracts/RLPWriter.sol";

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {EIP155Signer} from "@oasisprotocol/sapphire-contracts/contracts/EIP155Signer.sol";
import {ProvethVerifier, EVMTransactionProof} from "./lib/ProvethVerifier.sol";

import {SliceBytes} from "./lib/SliceBytes.sol";
import {Sapphire} from "@oasisprotocol/sapphire-contracts/contracts/Sapphire.sol";
import {EthereumUtils} from "@oasisprotocol/sapphire-contracts/contracts/EthereumUtils.sol";

import {TokenInfo, EVMKeypair} from "./Types.sol";
import {IShoyuBashi} from "./interfaces/IShoyuBashi.sol";

contract EVMSignerAndVerifier is ProvethVerifier {
    address public evmAddress;
    bytes32 private secretKey;
    IShoyuBashi public shoyuBashi;

    mapping(uint256 chainId => uint64) public nonces;
    mapping(uint256 chainId => uint256) public gasPrices;

    // Gas limit and gas price variables
    uint64 public constant gasLimitNative = 25000;
    uint64 public constant gasLimitERC20 = 100000;

    using RLPReader for RLPReader.RLPItem;
    using RLPReader for RLPReader.Iterator;
    using RLPReader for bytes;
    using SliceBytes for bytes;

    constructor() {
        // Add back in once the evm_increaseTime issue is resolved on sapphire localnet
        // (
        //     bytes memory compressedPublicKey,
        //     bytes memory secretKeyBytes
        // ) = Sapphire.generateSigningKeyPair(
        //         Sapphire.SigningAlg.Secp256k1PrehashedKeccak256,
        //         Sapphire.randomBytes(32, "EVMSiignerAndVerifier")
        //     );
        // evmAddress = EthereumUtils.k256PubkeyToEthereumAddress(
        //     compressedPublicKey
        // );
        // secretKey = bytes32(secretKeyBytes);
        evmAddress = 0x284a3Fe2939a4e4859e6321537d4264533E3D549;
        secretKey = 0x4bab77fcaf2d66bcb2e52cbf64102eea5fbee93005865faf66f616918f6318ea;
        shoyuBashi = IShoyuBashi(0x284a3Fe2939a4e4859e6321537d4264533E3D549); // Replace with actual ShoyuBashi contract address on Sapphire
    }

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
        bytes memory evmTransactionData
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
            evmTransactionData = evmTransactionData.getSlice(
                1,
                evmTransactionData.length
            );

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
            evmTransactionData = evmTransactionData.getSlice(
                1,
                evmTransactionData.length
            );

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
     * @notice Sets the gas price for a specific EVM chain ID.
     *
     * @dev This function allows updating the gas price used in transaction generation.
     *
     * @param chainId The EVM chain ID to set the gas price for.
     * @param gasPrice The gas price in wei to set for the specified chain ID.
     */
    function setGasPrice(uint256 chainId, uint256 gasPrice) public {
        gasPrices[chainId] = gasPrice;
    }

    /**
     * @notice Generates a signed native token transfer transaction for a specific EVM chain.
     *
     * This function creates and signs a native ETH transfer transaction using the Sapphire
     * EIP155Signer to call SIGN_DIGEST precompile, which internally uses the secure signing environment to:
     *   1. Construct a standard EVM transaction with the specified parameters
     *   2. Sign it using the contract's private key (stored securely in Sapphire)
     *   3. Return the RLP-encoded signed transaction ready for broadcast
     *
     * @dev Uses Sapphire's EIP155Signer to call SIGN_DIGEST precompile for secure transaction signing.
     *      The nonce is automatically incremented to ensure transaction ordering.
     *      Gas limit is set to a conservative 25,000 gas for native transfers.
     *
     * @param chainId The target blockchain's chain ID where the transaction will be sent
     * @param userAddress The recipient address who will receive the native tokens
     * @param amount The amount of native tokens to transfer (in wei)
     * @return output The RLP-encoded signed transaction ready for broadcast to the target chain
     */
    function generateNativeTransfer(
        uint256 chainId,
        address userAddress,
        uint256 amount
    ) internal returns (bytes memory output) {
        return
            EIP155Signer.sign(
                evmAddress,
                secretKey,
                EIP155Signer.EthTx({
                    nonce: nonces[chainId]++,
                    gasPrice: gasPrices[chainId],
                    gasLimit: gasLimitNative,
                    to: userAddress,
                    value: amount,
                    data: "",
                    chainId: chainId
                })
            );
    }

    /**
     * @notice Generates a signed ERC20 token transfer transaction for a specific EVM chain.
     *
     * This function creates and signs an ERC20 transfer transaction using the Sapphire
     * EIP155Signer which uses the SIGN_DIGEST precompile. The process involves:
     *   1. Encoding the ERC20 transfer(address,uint256) function call
     *   2. Constructing a standard EVM transaction with the encoded calldata
     *   3. Signing it using the contract's private key (stored securely in Sapphire)
     *   4. Returning the RLP-encoded signed transaction ready for broadcast
     *
     * The calldata encodes: transfer(userAddress, amount) which instructs the
     * ERC20 contract to transfer `amount` tokens from the contract's address
     * (evmAddress) to the specified user address.
     *
     * @dev Uses Sapphire's EIP155Signer to call SIGN_DIGEST precompile for secure transaction signing.
     *      The nonce is automatically incremented to ensure transaction ordering.
     *      Gas limit is set to 100,000 gas to handle most ERC20 transfer scenarios.
     *
     * @param chainId The target blockchain's chain ID where the transaction will be sent
     * @param userAddress The recipient address who will receive the ERC20 tokens
     * @param tokenAddress The ERC20 contract address on the target chain
     * @param amount The amount of ERC20 tokens to transfer (in token's base units)
     * @return output The RLP-encoded signed transaction ready for broadcast to the target chain
     */
    function generateERC20Transfer(
        uint256 chainId,
        address userAddress,
        address tokenAddress,
        uint256 amount
    ) internal returns (bytes memory output) {
        bytes memory data = abi.encodeWithSignature(
            "transfer(address,uint256)",
            userAddress,
            amount
        );
        return
            EIP155Signer.sign(
                evmAddress,
                secretKey,
                EIP155Signer.EthTx({
                    nonce: nonces[chainId]++,
                    gasPrice: gasPrices[chainId],
                    gasLimit: gasLimitERC20,
                    to: tokenAddress,
                    value: 0,
                    data: data,
                    chainId: chainId
                })
            );
    }

    /**
     * @notice Decodes EVM native token metadata to extract the chain ID.
     *
     * This function decodes the metadata stored for native EVM tokens (like ETH, MATIC, BNB).
     * Native tokens are identified solely by their chain ID, as they don't have a specific
     * contract address - they are the blockchain's native currency.
     *
     * Data structure (32 bytes total):
     *   [0..32): chainId (uint256) - The EVM chain where this native token exists
     *
     * The function uses assembly for efficient memory access to extract the chain ID
     * from the packed byte data. Since abi.encodePacked(chainId) creates exactly 32 bytes,
     * we can directly load the chain ID using mload.
     *
     * @dev Uses assembly for gas-efficient decoding of the 32-byte chain ID.
     *      Expects data created by encodeEVMNativeTokenData().
     *
     * @param data The packed metadata bytes containing the chain ID (must be 32 bytes)
     * @return chainId The EVM chain ID where this native token exists
     */
    function decodeEVMNativeTokenData(
        bytes memory data
    ) public pure returns (uint256 chainId) {
        require(data.length == 32, "Invalid data length for EVM native token");
        assembly {
            chainId := mload(add(data, 32))
        }
    }

    /**
     * @notice Encodes EVM native token metadata for storage in the token registry.
     *
     * This function creates the metadata representation for native EVM tokens (like ETH, MATIC, BNB).
     * Native tokens only require a chain ID for identification, as they are the inherent currency
     * of their respective blockchains and don't have contract addresses.
     *
     * The encoded data structure is:
     *   [0..32): chainId (uint256) - The EVM chain where this native token exists
     *
     * This creates a compact 32-byte representation that can be stored in the TokenInfo.data
     * field and later decoded using decodeEVMNativeTokenData().
     *
     * @dev Uses abi.encodePacked for gas-efficient encoding without padding.
     *      The result is exactly 32 bytes and can be decoded with decodeEVMNativeTokenData().
     *
     * @param chainId The EVM chain ID where this native token exists (e.g., 1 for Ethereum mainnet)
     * @return data The packed metadata bytes (32 bytes) ready for storage
     */
    function encodeEVMNativeTokenData(
        uint256 chainId
    ) public pure returns (bytes memory data) {
        return abi.encodePacked(chainId);
    }

    /**
     * @notice Decodes EVM ERC20 token metadata to extract the chain ID and contract address.
     *
     * This function decodes the metadata stored for ERC20 tokens on EVM chains.
     * ERC20 tokens require both a chain ID (to identify which blockchain) and a contract
     * address (to identify the specific token contract).
     *
     * Data structure (52 bytes total):
     *   [0..32):  chainId (uint256) - The EVM chain where this token contract exists
     *   [32..52): tokenAddress (address) - The ERC20 contract address (20 bytes)
     *
     * The function uses assembly for efficient memory access to extract both values.
     * Note: The address is stored in the last 20 bytes of a 32-byte word (right-aligned)
     * when using abi.encodePacked, so we read at offset 52 to get the full 32-byte word
     * containing the address.
     *
     * @dev Uses assembly for gas-efficient decoding. The address extraction reads a full
     *      32-byte word at offset 52, where the address is right-aligned.
     *      Expects data created by encodeEVMErc20TokenData().
     *
     * @param data The packed metadata bytes containing chain ID and token address (must be 52 bytes)
     * @return chainId The EVM chain ID where this token contract exists
     * @return tokenAddress The ERC20 contract address on the specified chain
     */
    function decodeEVMErc20TokenData(
        bytes memory data
    ) public pure returns (uint256 chainId, address tokenAddress) {
        require(data.length == 52, "Invalid data length for EVM ERC20 token");
        assembly {
            chainId := mload(add(data, 32))
            tokenAddress := mload(add(data, 52))
        }
    }

    /**
     * @notice Encodes EVM ERC20 token metadata for storage in the token registry.
     *
     * This function creates the metadata representation for ERC20 tokens on EVM chains.
     * ERC20 tokens require both a chain ID (to specify which blockchain) and a contract
     * address (to identify the specific token contract on that chain).
     *
     * The encoded data structure is:
     *   [0..32):  chainId (uint256) - The EVM chain where this token contract exists
     *   [32..52): tokenAddress (address) - The ERC20 contract address (20 bytes)
     *
     * This creates a compact 52-byte representation that can be stored in the TokenInfo.data
     * field and later decoded using decodeEVMErc20TokenData().
     *
     * The encoding uses abi.encodePacked which concatenates the values without padding,
     * resulting in exactly 52 bytes (32 for chainId + 20 for address).
     *
     * @dev Uses abi.encodePacked for gas-efficient encoding without padding.
     *      The result is exactly 52 bytes and can be decoded with decodeEVMErc20TokenData().
     *      The address will be right-aligned in memory when decoded.
     *
     * @param chainId The EVM chain ID where this token contract exists (e.g., 1 for Ethereum mainnet)
     * @param tokenAddress The ERC20 contract address on the specified chain
     * @return data The packed metadata bytes (52 bytes) ready for storage
     */
    function encodeEVMErc20TokenData(
        uint256 chainId,
        address tokenAddress
    ) public pure returns (bytes memory data) {
        return abi.encodePacked(chainId, tokenAddress);
    }

    function getBlockNumber(
        bytes memory rlpBlockHeader
    ) public pure returns (uint256 blockNumber) {
        RLPReader.RLPItem[] memory blockHeader = rlpBlockHeader
            .toRlpItem()
            .toList();
        blockNumber = (blockHeader[5].toUint());
    }

    function validateEVMTxProof(
        EVMTransactionProof calldata txProof
    ) internal view returns (bytes memory) {
        // Validate the transaction proof using the ProvethVerifier
        return validateTxProof(txProof);
    }

    function verifyBlockHash(
        bytes32 expectedBlockHash,
        uint256 blockNumber,
        uint256 chainId
    ) public view {
        // Call Blockhash oracle with hash, blocknumber and chainId
        bytes32 actualBlockHash = shoyuBashi.getUnanimousHash(
            chainId,
            blockNumber
        );
        // @ahmed needs to call Shoyubashi before submitting the includeEVMDeposit to update the blockhash oracle
        // Call this function https://github.com/rube-de/hashi/blob/b43726b27eedddf15e48e65e0175aba4b2a8096e/packages/evm/contracts/ownable/ShoyuBashi.sol#L28
        require(actualBlockHash == expectedBlockHash, "Invalid block hash");
    }
}
