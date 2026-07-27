// SPDX-License-Identifier: MIT
/* solhint-disable no-console */
pragma solidity ^0.8.20;

import {ChainType, TokenInfo, EVMKeypair} from "./Types.sol";

import {RLPReader} from "solidity-rlp/contracts/RLPReader.sol";
import {RLPWriter} from "@oasisprotocol/sapphire-contracts/contracts/RLPWriter.sol";

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {EIP155Signer} from "@oasisprotocol/sapphire-contracts/contracts/EIP155Signer.sol";
import {Subcall} from "@oasisprotocol/sapphire-contracts/contracts/Subcall.sol";

import {SliceBytes} from "./lib/SliceBytes.sol";
import {Sapphire} from "@oasisprotocol/sapphire-contracts/contracts/Sapphire.sol";
import {ROFLableUpgradeable} from "@oasisprotocol/sapphire-contracts/contracts/ROFLableUpgradeable.sol";
import {EthereumUtils} from "@oasisprotocol/sapphire-contracts/contracts/EthereumUtils.sol";

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {OwnableUpgradeable} from "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

abstract contract EVMSignerVerifier is OwnableUpgradeable, ROFLableUpgradeable, UUPSUpgradeable {
    /// @notice Contract version, bumped on each upgrade for tracking/verification.
    string public constant VERSION = "1.0.0";

    address public evmAddress;
    bytes32 private _secretKey;
    address public gasTankAddress;
    bytes32 private _gasTankSecret;
    address public accounting;

    mapping(uint256 chainId => uint64) public nonces;
    mapping(uint256 chainId => uint256) public gasPrices;

    /// @notice Address of the ROFL-derived secp256k1 key used to authenticate signed queries.
    /// @dev Published by the service at startup via setRoflSignerAddress. Enables msg.sender-based
    ///      auth on view functions (roflEnsureAuthorizedOrigin is tx-only and doesn't work in eth_call).
    address public roflSignerAddress;

    // Sweep gas limits: deposit address → evmAddress (always an EOA)
    uint64 public constant gasLimitNativeSweep = 21000;
    uint64 public constant gasLimitERC20Sweep = 65000;
    // Withdrawal gas limits: evmAddress → user-chosen address (may be a contract)
    uint64 public constant gasLimitNativeWithdraw = 50000;
    uint64 public constant gasLimitERC20Withdraw = 100000;

    error GasPriceNotSet(uint256 chainId);
    error InvalidGasPrice();
    error InvalidNativeTokenDataLength();
    error InvalidERC20TokenDataLength();
    error InvalidAddress();
    error UnsupportedChainType();
    error NotAuthorized();
    error RoflSignerNotSet();
    error UpgradeCallDataNotAllowed();

    event GasPriceSet(uint256 indexed chainId, uint256 gasPrice);
    event RoflSignerUpdated(address indexed newSigner);
    event AccountingUpdated(address indexed newAccounting);

    /// @notice Gate for functions called by the whitelisted Accounting contract.
    modifier onlyAccounting() {
        if (msg.sender != accounting) revert NotAuthorized();
        _;
    }

    /// @notice Gate for functions called as signed view queries by ROFL.
    /// @dev Cannot use roflEnsureAuthorizedOrigin in view context — no tx origin inside eth_call.
    ///      Relies on sapphirepy signed queries setting msg.sender to the ROFL-derived key.
    modifier onlyROFLQuery() {
        if (roflSignerAddress == address(0)) revert RoflSignerNotSet();
        if (msg.sender != roflSignerAddress) revert NotAuthorized();
        _;
    }

    modifier onlyAccountingOrROFLQuery() {
        if (roflSignerAddress == address(0)) revert RoflSignerNotSet();
        if (msg.sender != roflSignerAddress && msg.sender != accounting) revert NotAuthorized();
        _;
    }

    using RLPReader for RLPReader.RLPItem;
    using RLPReader for RLPReader.Iterator;
    using RLPReader for bytes;
    using SliceBytes for bytes;

    constructor() {
        _disableInitializers();
    }

    /**
     * @notice Initializes the EVMSignerAndVerifier contract.
     * @param inAccounting Accounting contract whitelisted to sign transaction
     * @param inRoflAppId The ROFL app identifier (stable across redeployments)
     * @param inOwner The address of the contract owner managing upgrades
     */
    function initialize(address inAccounting, bytes21 inRoflAppId, address inOwner) external virtual initializer {
        __EVMSignerAndVerifier_init(inAccounting, inRoflAppId, inOwner);
    }

    /**
     * @notice Initializes the EVMSignerAndVerifier contract.
     * @param inAccounting Accounting contract whitelisted to sign transaction
     * @param inRoflAppId The ROFL app identifier (stable across redeployments)
     * @param inOwner The address of the contract owner managing upgrades
     */
    function __EVMSignerAndVerifier_init(address inAccounting, bytes21 inRoflAppId, address inOwner) internal onlyInitializing {
        __ROFLable_init(inRoflAppId);
        __Ownable_init(inOwner);

        (evmAddress, _secretKey) = _generateKeypair();
        (gasTankAddress, _gasTankSecret) = _generateKeypair();
        accounting = inAccounting;
    }

    /**
     * @notice Authorizes an upgrade to a new implementation.
     * @dev Required by UUPSUpgradeable. Only the contract owner can upgrade.
     * @param newImplementation Address of the new implementation contract
     */
    function _authorizeUpgrade(address newImplementation) internal override onlyOwner {}

    /**
     * @dev Overridden to prevent the simulation attack by contract owner.
     * @param newImplementation Address of the new implementation contract
     * @param data Must be empty so no upgrade migration hook can extract sensitive data in simulated call; passing call data reverts.
     */
    function upgradeToAndCall(
        address newImplementation,
        bytes memory data
    ) public payable override onlyProxy {
        if (data.length != 0) revert UpgradeCallDataNotAllowed();
        super.upgradeToAndCall(newImplementation, data);
    }

    /// @notice Updates the Accounting contract authorized to call the signer/verifier functions.
    /// @dev Owner-gated. The wiring is circular at deploy time (Accounting needs the verifier
    ///      address and vice versa), so this setter lets the owner point the verifier at the
    ///      real Accounting proxy after both are deployed. Whoever is set as `accounting` can
    ///      request signed transfers from the pooled hot wallet, so restrict this to a trusted owner.
    /// @param newAccounting The address of the Accounting contract.
    function setAccounting(address newAccounting) external onlyOwner {
        if (newAccounting == address(0)) revert InvalidAddress();
        accounting = newAccounting;
        emit AccountingUpdated(newAccounting);
    }

    /**
     * @notice Generates a new keypair for signing EVM transactions.
     * @dev This function is virtual to allow mocking in tests.
     *      In production, this calls the Sapphire EthereumUtils precompile.
     */
    function _generateKeypair() internal virtual returns (address, bytes32) {
        return EthereumUtils.generateKeypair();
    }

    /**
     * @notice Derives a deterministic deposit keypair for a beneficiary.
     * @dev Uses secretKey as master seed with domain separation via (beneficiary, chainType, version).
     *      Sapphire.generateSigningKeyPair is deterministic — same seed always produces the same keypair.
     *      chainId is deliberately omitted: one address across all chains within a ChainType family.
     *      Virtual to allow mocking in tests (Sapphire precompiles unavailable in Hardhat).
     * @param beneficiary The user's Sapphire address
     * @param chainType The chain family (see ChainType enum)
     * @param version Key derivation index
     * @return depositAddr The derived deposit address
     * @return depositSecret The derived deposit private key
     */
    function deriveDepositKeypair(
        address beneficiary,
        ChainType chainType,
        uint256 version
    ) public view onlyAccountingOrROFLQuery returns (address depositAddr, bytes32 depositSecret) {
        bytes32 seed = keccak256(
            abi.encode(_secretKey, beneficiary, chainType, version)
        );

        // v1: only EVM family uses Secp256k1. When a non-EVM ChainType variant is
        // added, route it through an additional branch with its own signing alg.
        if (chainType != ChainType.EVM) revert UnsupportedChainType();
        (bytes memory pk, bytes memory sk) = Sapphire.generateSigningKeyPair(
            Sapphire.SigningAlg.Secp256k1PrehashedKeccak256,
            abi.encodePacked(seed)
        );
        depositAddr = EthereumUtils.k256PubkeyToEthereumAddress(pk);
        assembly {
            depositSecret := mload(add(sk, 32))
        }
    }

    /**
     * @notice Sets the gas price for a specific EVM chain ID.
     *
     * @dev This function allows updating the gas price used in transaction generation.
     *      Gated by onlyROFL so only an authenticated ROFL transaction can update it.
     *      Gas price must be greater than 0 to prevent transaction failures.
     *
     * @param chainId The EVM chain ID to set the gas price for.
     * @param gasPrice The gas price in wei to set for the specified chain ID.
     */
    function setGasPrice(uint256 chainId, uint256 gasPrice) public onlyROFL {
        if (gasPrice == 0) revert InvalidGasPrice();
        gasPrices[chainId] = gasPrice;
        emit GasPriceSet(chainId, gasPrice);
    }

    /// @notice Publish the ROFL-derived signer address on-chain.
    /// @dev Called by the service at startup. Gated by onlyROFL so only an authenticated ROFL
    ///      transaction can update it. The address becomes the msg.sender that onlyROFLQuery
    ///      functions will check against for signed view queries.
    function setRoflSignerAddress(address newSigner) external onlyROFL {
        if (newSigner == address(0)) revert InvalidAddress();
        roflSignerAddress = newSigner;
        emit RoflSignerUpdated(newSigner);
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
     *      Gas price must be set for the chain via setGasPrice() before calling this function.
     *
     * @param chainId The target blockchain's chain ID where the transaction will be sent
     * @param userAddress The recipient address who will receive the native tokens
     * @param amount The amount of native tokens to transfer (in wei)
     * @param nonce The sender's transaction nonce on the target chain
     * @return output The RLP-encoded signed transaction ready for broadcast to the target chain
     */
    function generateNativeTransfer(
        uint256 chainId,
        address userAddress,
        uint256 amount,
        uint64 nonce
    ) public onlyAccounting view returns (bytes memory output) {
        if (gasPrices[chainId] == 0) revert GasPriceNotSet(chainId);

        return
            EIP155Signer.sign(
                evmAddress,
                _secretKey,
                EIP155Signer.EthTx({
                    nonce: nonce,
                    gasPrice: gasPrices[chainId],
                    gasLimit: gasLimitNativeWithdraw,
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
     *      Gas price must be set for the chain via setGasPrice() before calling this function.
     *
     * @param chainId The target blockchain's chain ID where the transaction will be sent
     * @param userAddress The recipient address who will receive the ERC20 tokens
     * @param tokenAddress The ERC20 contract address on the target chain
     * @param amount The amount of ERC20 tokens to transfer (in token's base units)
     * @param nonce The sender's transaction nonce on the target chain
     * @return output The RLP-encoded signed transaction ready for broadcast to the target chain
     */
    function generateERC20Transfer(
        uint256 chainId,
        address userAddress,
        address tokenAddress,
        uint256 amount,
        uint64 nonce
    ) public onlyAccounting view returns (bytes memory output) {
        if (gasPrices[chainId] == 0) revert GasPriceNotSet(chainId);

        bytes memory data = abi.encodeWithSignature(
            "transfer(address,uint256)",
            userAddress,
            amount
        );
        return
            EIP155Signer.sign(
                evmAddress,
                _secretKey,
                EIP155Signer.EthTx({
                    nonce: nonce,
                    gasPrice: gasPrices[chainId],
                    gasLimit: gasLimitERC20Withdraw,
                    to: tokenAddress,
                    value: 0,
                    data: data,
                    chainId: chainId
                })
            );
    }

    // ─── Sweep & Gas Funding (onlyROFL) ───────────────────────────────

    /**
     * @notice Sign a native token sweep: depositAddress → evmAddress.
     * @dev Derives deposit keypair internally, signs via EIP155Signer.sign().
     *      ROFL broadcasts the returned signedTx on the source chain.
     *      ROFL supplies amount (typically balance - 21000*gasPrice) from source chain query.
     */
    function generateSweepNativeTransfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    ) external view onlyROFLQuery returns (bytes memory signedTx) {
        (address depositAddr, bytes32 depositSecret) = deriveDepositKeypair(
            beneficiary, chainType, version
        );
        signedTx = EIP155Signer.sign(
            depositAddr,
            depositSecret,
            EIP155Signer.EthTx({
                nonce: sourceChainNonce,
                gasPrice: gasPrice,
                gasLimit: gasLimitNativeSweep,
                to: evmAddress,
                value: amount,
                data: "",
                chainId: chainId
            })
        );
    }

    /**
     * @notice Sign an ERC20 sweep: token.transfer(evmAddress, amount) from deposit address.
     * @dev Same derivation + signing pattern. ROFL supplies amount from source chain query.
     */
    function generateSweepERC20Transfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        address tokenAddress,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    ) external view onlyROFLQuery returns (bytes memory signedTx) {
        (address depositAddr, bytes32 depositSecret) = deriveDepositKeypair(
            beneficiary, chainType, version
        );
        bytes memory data = abi.encodeWithSignature(
            "transfer(address,uint256)",
            evmAddress,
            amount
        );
        signedTx = EIP155Signer.sign(
            depositAddr,
            depositSecret,
            EIP155Signer.EthTx({
                nonce: sourceChainNonce,
                gasPrice: gasPrice,
                gasLimit: gasLimitERC20Sweep,
                to: tokenAddress,
                value: 0,
                data: data,
                chainId: chainId
            })
        );
    }

    /**
     * @notice Sign a native transfer from a deposit address to an arbitrary destination.
     * @dev Used by emergency withdraw — caller supplies all source-chain state.
     */
    function generateDepositAddressTransfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        address toAddress,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    ) external onlyAccounting view returns (bytes memory signedTx) {
        (address depositAddr, bytes32 depositSecret) = deriveDepositKeypair(
            beneficiary, chainType, version
        );
        signedTx = EIP155Signer.sign(
            depositAddr,
            depositSecret,
            EIP155Signer.EthTx({
                nonce: sourceChainNonce,
                gasPrice: gasPrice,
                gasLimit: gasLimitNativeWithdraw,
                to: toAddress,
                value: amount,
                data: "",
                chainId: chainId
            })
        );
    }

    /**
     * @notice Sign an ERC20 transfer from a deposit address to an arbitrary destination.
     * @dev Used by emergency withdraw for ERC20 tokens — caller supplies all source-chain state.
     *      Signs token.transfer(toAddress, amount) from the derived deposit keypair.
     */
    function generateDepositAddressERC20Transfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        address toAddress,
        address tokenAddress,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    ) external onlyAccounting view returns (bytes memory signedTx) {
        (address depositAddr, bytes32 depositSecret) = deriveDepositKeypair(
            beneficiary, chainType, version
        );
        bytes memory data = abi.encodeWithSignature(
            "transfer(address,uint256)",
            toAddress,
            amount
        );
        signedTx = EIP155Signer.sign(
            depositAddr,
            depositSecret,
            EIP155Signer.EthTx({
                nonce: sourceChainNonce,
                gasPrice: gasPrice,
                gasLimit: gasLimitERC20Withdraw,
                to: tokenAddress,
                value: 0,
                data: data,
                chainId: chainId
            })
        );
    }

    /**
     * @notice Sign a gas funding tx: gasTankAddress → depositAddress (native tokens for ERC20 sweep gas).
     * @dev Uses gasTankSecret internally. ROFL supplies nonce/gasPrice from source chain.
     */
    function generateGasFundingTx(
        address toDepositAddress,
        uint256 chainId,
        uint256 gasAmount,
        uint64 gasTankNonce,
        uint256 gasPrice
    ) external view onlyROFLQuery returns (bytes memory signedTx) {
        signedTx = EIP155Signer.sign(
            gasTankAddress,
            _gasTankSecret,
            EIP155Signer.EthTx({
                nonce: gasTankNonce,
                gasPrice: gasPrice,
                gasLimit: gasLimitNativeSweep,
                to: toDepositAddress,
                value: gasAmount,
                data: "",
                chainId: chainId
            })
        );
    }

    // ─── Token Data Encoding/Decoding ─────────────────────────────────

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
        if (data.length != 32) revert InvalidNativeTokenDataLength();
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
        if (data.length != 52) revert InvalidERC20TokenDataLength();
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

    function getEVMNonceAndIncrement(
        uint256 chainId
    ) external onlyAccounting returns (uint64 nonce) {
        return uint64(nonces[chainId]++);
    }

    /**
     * @dev Reserved storage gap for future upgrades.
     * This allows adding new state variables without shifting storage layout.
     */
    uint256[40] private __gap;
}
