// SPDX-License-Identifier: MIT
/* solhint-disable no-console */
pragma solidity ^0.8.20;

import {ChainType} from "./Types.sol";

import {
    EIP155Signer
} from "@oasisprotocol/sapphire-contracts/contracts/EIP155Signer.sol";

import {
    Sapphire
} from "@oasisprotocol/sapphire-contracts/contracts/Sapphire.sol";
import {
    EthereumUtils
} from "@oasisprotocol/sapphire-contracts/contracts/EthereumUtils.sol";

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {OwnableUpgradeable} from "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";

abstract contract EVMSignerAndVerifier is Initializable, OwnableUpgradeable {
    address public evmAddress;
    bytes32 private secretKey;
    address public gasTankAddress;
    bytes32 private gasTankSecret;

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
    error InvalidAddress();
    error UnsupportedChainType();

    event GasPriceSet(uint256 indexed chainId, uint256 gasPrice);
    event RoflSignerUpdated(address indexed newSigner);

    /**
     * @notice Initializes the EVMSignerAndVerifier contract.
     * @param _owner The address that will own this contract
     */
    function __EVMSignerAndVerifier_init(
        address _owner
    ) internal onlyInitializing {
        if (_owner == address(0)) revert InvalidAddress();
        __Ownable_init(_owner);
        (evmAddress, secretKey) = _generateKeypair();
        (gasTankAddress, gasTankSecret) = _generateKeypair();
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
    function _deriveDepositKeypair(
        address beneficiary,
        ChainType chainType,
        uint256 version
    ) internal view virtual returns (address depositAddr, bytes32 depositSecret) {
        bytes32 seed = keccak256(
            abi.encode(secretKey, beneficiary, chainType, version)
        );

        // v1: only EVM family uses Secp256k1. When a non-EVM ChainType variant is
        // added, route it through an additional branch with its own signing alg.
        if (chainType != ChainType.EVM) revert UnsupportedChainType();
        (bytes memory pk, bytes memory sk) = Sapphire.generateSigningKeyPair(
            Sapphire.SigningAlg.Secp256k1PrehashedKeccak256,
            abi.encodePacked(seed)
        );
        depositAddr = EthereumUtils.k256PubkeyToEthereumAddress(pk);
        assembly ("memory-safe") {
            depositSecret := mload(add(sk, 32))
        }
    }

    function _setGasPrice(uint256 chainId, uint256 gasPrice) internal {
        if (gasPrice == 0) revert InvalidGasPrice();
        gasPrices[chainId] = gasPrice;
        emit GasPriceSet(chainId, gasPrice);
    }

    function _setRoflSignerAddress(address newSigner) internal {
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
    function _generateNativeTransfer(
        uint256 chainId,
        address userAddress,
        uint256 amount,
        uint64 nonce
    ) internal view returns (bytes memory output) {
        if (gasPrices[chainId] == 0) revert GasPriceNotSet(chainId);

        return
            EIP155Signer.sign(
                evmAddress,
                secretKey,
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
    function _generateERC20Transfer(
        uint256 chainId,
        address userAddress,
        address tokenAddress,
        uint256 amount,
        uint64 nonce
    ) internal view returns (bytes memory output) {
        if (gasPrices[chainId] == 0) revert GasPriceNotSet(chainId);

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

    // ─── Sweep & Gas Funding ─────────────────────────────────────────

    /**
     * @notice Sign a native token sweep: depositAddress → evmAddress.
     * @dev Derives deposit keypair internally, signs via EIP155Signer.sign().
     *      ROFL broadcasts the returned signedTx on the source chain.
     *      ROFL supplies amount (typically balance - 21000*gasPrice) from source chain query.
     */
    function _generateSweepNativeTransfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    ) internal view virtual returns (bytes memory signedTx) {
        (address depositAddr, bytes32 depositSecret) = _deriveDepositKeypair(
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
    function _generateSweepERC20Transfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        address tokenAddress,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    ) internal view virtual returns (bytes memory signedTx) {
        (address depositAddr, bytes32 depositSecret) = _deriveDepositKeypair(
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
    function _generateDepositAddressTransfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        address toAddress,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    ) internal view returns (bytes memory signedTx) {
        (address depositAddr, bytes32 depositSecret) = _deriveDepositKeypair(
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
    function _generateDepositAddressERC20Transfer(
        address beneficiary,
        ChainType chainType,
        uint256 version,
        uint256 chainId,
        address toAddress,
        address tokenAddress,
        uint256 amount,
        uint64 sourceChainNonce,
        uint256 gasPrice
    ) internal view returns (bytes memory signedTx) {
        (address depositAddr, bytes32 depositSecret) = _deriveDepositKeypair(
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
    function _generateGasFundingTx(
        address toDepositAddress,
        uint256 chainId,
        uint256 gasAmount,
        uint64 gasTankNonce,
        uint256 gasPrice
    ) internal view virtual returns (bytes memory signedTx) {
        signedTx = EIP155Signer.sign(
            gasTankAddress,
            gasTankSecret,
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

    function getEVMNonceAndIncrement(
        uint256 chainId
    ) internal returns (uint64 nonce) {
        return uint64(nonces[chainId]++);
    }

    /**
     * @dev Reserved storage gap for future upgrades.
     * This allows adding new state variables without shifting storage layout.
     */
    uint256[43] private __gap;
}
