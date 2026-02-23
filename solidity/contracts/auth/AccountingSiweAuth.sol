// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.20;

import {Strings} from "@openzeppelin/contracts/utils/Strings.sol";

import {SignatureRSV, A13e} from "@oasisprotocol/sapphire-contracts/contracts/auth/A13e.sol";
import {
    ParsedSiweMessage,
    SiweParser
} from "@oasisprotocol/sapphire-contracts/contracts/SiweParser.sol";
import {Sapphire} from "@oasisprotocol/sapphire-contracts/contracts/Sapphire.sol";

/// @title AuthToken structure for SIWE-based authentication
struct AuthToken {
    string domain; // [ scheme "://" ] domain.
    address userAddr;
    uint256 validUntil; // in Unix timestamp.
    string statement; // Human-readable statement from the SIWE message.
    // Informational only for now: carried through from SIWE message but not enforced by auth checks.
    string[] resources;
}

/**
 * @title AccountingSiweAuth
 * @notice SIWE authentication helper for Sapphire view-call authentication.
 *
 * This contract is a minimal fork of sapphire-contracts' SiweAuth
 * with one key difference: it avoids calling Sapphire precompiles in the constructor
 * on non-Sapphire test networks (e.g., Hardhat), so unit tests can deploy contracts.
 *
 * The authentication logic (login/authMsgSender) is only supported on Sapphire chains.
 */
contract AccountingSiweAuth is A13e {
    string internal _domain;
    bytes32 private _authTokenEncKey;
    uint256 private constant DEFAULT_VALIDITY = 24 hours;

    error SiweAuth_UnsupportedChain();
    error SiweAuth_ChainIdMismatch();
    error SiweAuth_DomainMismatch();
    error SiweAuth_AddressMismatch();
    error SiweAuth_NotBeforeInFuture();
    error SiweAuth_Expired();

    constructor(string memory inDomain) {
        _domain = inDomain;

        if (_isSapphireChainId(block.chainid)) {
            _authTokenEncKey = bytes32(Sapphire.randomBytes(32, ""));
        } else {
            // Deterministic key for non-Sapphire local test networks (e.g., Hardhat).
            // Authentication is not expected to be used off-Sapphire.
            _authTokenEncKey = bytes32(uint256(1));
        }
    }

    function _isSapphireChainId(uint256 chainId) private pure returns (bool) {
        return chainId == 0x5afe || chainId == 0x5aff || chainId == 0x5afd;
    }

    function _requireSapphire() private view {
        if (!_isSapphireChainId(block.chainid)) {
            revert SiweAuth_UnsupportedChain();
        }
    }

    function login(string calldata siweMsg, SignatureRSV calldata sig)
        external
        view
        override
        returns (bytes memory)
    {
        _requireSapphire();

        AuthToken memory b;

        // Derive the user's address from the signature.
        bytes memory eip191msg = abi.encodePacked(
            "\x19Ethereum Signed Message:\n",
            Strings.toString(bytes(siweMsg).length),
            siweMsg
        );
        address addr = ecrecover(
            keccak256(eip191msg),
            uint8(sig.v),
            sig.r,
            sig.s
        );
        b.userAddr = addr;

        ParsedSiweMessage memory p = SiweParser.parseSiweMsg(bytes(siweMsg));

        if (p.chainId != block.chainid) {
            revert SiweAuth_ChainIdMismatch();
        }

        if (keccak256(p.schemeDomain) != keccak256(bytes(_domain))) {
            revert SiweAuth_DomainMismatch();
        }
        b.domain = string(p.schemeDomain);

        if (p.addr != addr) {
            revert SiweAuth_AddressMismatch();
        }

        if (
            p.notBefore.length != 0 &&
            block.timestamp <= SiweParser.timestampFromIso(p.notBefore)
        ) {
            revert SiweAuth_NotBeforeInFuture();
        }

        if (p.expirationTime.length != 0) {
            b.validUntil = SiweParser.timestampFromIso(p.expirationTime);
        } else {
            b.validUntil = block.timestamp + DEFAULT_VALIDITY;
        }
        if (block.timestamp >= b.validUntil) {
            revert SiweAuth_Expired();
        }

        b.statement = string(p.statement);

        b.resources = new string[](p.resources.length);
        for (uint256 i = 0; i < p.resources.length; i++) {
            b.resources[i] = string(p.resources[i]);
        }

        return Sapphire.encrypt(_authTokenEncKey, 0, abi.encode(b), "");
    }

    function domain() public view returns (string memory) {
        return _domain;
    }

    function authMsgSender(bytes memory token)
        internal
        view
        override
        checkRevokedAuthToken(token)
        returns (address)
    {
        _requireSapphire();

        if (token.length == 0) {
            return address(0);
        }

        AuthToken memory b = decodeAndValidateToken(token);
        return b.userAddr;
    }

    function decodeAndValidateToken(bytes memory token)
        internal
        view
        virtual
        returns (AuthToken memory)
    {
        _requireSapphire();

        bytes memory authTokenEncoded = Sapphire.decrypt(
            _authTokenEncKey,
            0,
            token,
            ""
        );
        AuthToken memory b = abi.decode(authTokenEncoded, (AuthToken));

        if (keccak256(bytes(b.domain)) != keccak256(bytes(_domain))) {
            revert SiweAuth_DomainMismatch();
        }

        if (b.validUntil < block.timestamp) {
            revert SiweAuth_Expired();
        }

        return b;
    }

    /// @notice Expose authenticated sender for external callers (e.g., wrapper contracts).
    function authSender(bytes calldata token) external view returns (address) {
        return authMsgSender(token);
    }
}
