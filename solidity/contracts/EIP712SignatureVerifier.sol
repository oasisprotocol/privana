// SPDX-License-Identifier: MIT
/* solhint-disable no-console */
pragma solidity ^0.8.20;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";

contract EIP712SignatureVerifier is EIP712 {
    constructor() EIP712("AccountingModule", "1") {}

    mapping(bytes signature => bool used) public usedSignatures;

    error InvalidSignature();
    error UsedSignature();

    bytes32 private constant WITHDRAW_TYPEHASH =
        keccak256(
            "Withdraw(address userAddress,bytes32 tokenId,uint256 amount)"
        );

    bytes32 private constant LOCK_TYPEHASH =
        keccak256(
            "Lock(address userAddress,address serviceAddress,bytes32 tokenId,uint256 amount,uint256 expiry)"
        );

    bytes32 private constant TRANSFER_TYPEHASH =
        keccak256(
            "Transfer(address userAddress,address toAddress,bytes32 tokenId,uint256 amount)"
        );

    bytes32 private constant TRANSFER_LOCKED_TYPEHASH =
        keccak256(
            "TransferLocked(address userAddress,address toAddress,uint256 lockIndex,uint256 amount)"
        );

    function verifyWithdrawSignature(
        address userAddress,
        bytes32 tokenId,
        uint256 amount,
        bytes calldata signature
    ) public {
        bytes32 structHash = keccak256(
            abi.encode(WITHDRAW_TYPEHASH, userAddress, tokenId, amount)
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(digest, signature);
        if (signer != userAddress) {
            revert InvalidSignature();
        }

        if (usedSignatures[signature]) {
            revert UsedSignature();
        }

        usedSignatures[signature] = true;
    }

    function verifyLockSignature(
        address userAddress,
        address serviceAddress,
        bytes32 tokenId,
        uint256 amount,
        uint256 expiry,
        bytes calldata signature
    ) public {
        bytes32 structHash = keccak256(
            abi.encode(
                LOCK_TYPEHASH,
                userAddress,
                serviceAddress,
                tokenId,
                amount,
                expiry
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(digest, signature);
        if (signer != userAddress) {
            revert InvalidSignature();
        }

        if (usedSignatures[signature]) {
            revert UsedSignature();
        }

        usedSignatures[signature] = true;
    }

    function verifyTransferSignature(
        address userAddress,
        address toAddress,
        bytes32 tokenId,
        uint256 amount,
        bytes calldata signature
    ) public {
        bytes32 structHash = keccak256(
            abi.encode(
                TRANSFER_TYPEHASH,
                userAddress,
                toAddress,
                tokenId,
                amount
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(digest, signature);
        if (signer != userAddress) {
            revert InvalidSignature();
        }

        if (usedSignatures[signature]) {
            revert UsedSignature();
        }

        usedSignatures[signature] = true;
    }

    function verifyTransferLockedSignature(
        address serviceAddress,
        address userAddress,
        address toAddress,
        uint256 lockIndex,
        uint256 amount,
        bytes calldata signature
    ) public {
        bytes32 structHash = keccak256(
            abi.encode(
                TRANSFER_LOCKED_TYPEHASH,
                userAddress,
                toAddress,
                lockIndex,
                amount
            )
        );
        bytes32 digest = _hashTypedDataV4(structHash);
        address signer = ECDSA.recover(digest, signature);
        if (signer != serviceAddress) {
            revert InvalidSignature();
        }

        if (usedSignatures[signature]) {
            revert UsedSignature();
        }

        usedSignatures[signature] = true;
    }
}
