// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.20;

import {SignatureRSV, A13e} from "@oasisprotocol/sapphire-contracts/contracts/auth/A13e.sol";
import {Sapphire} from "@oasisprotocol/sapphire-contracts/contracts/Sapphire.sol";
import {Subcall} from "@oasisprotocol/sapphire-contracts/contracts/Subcall.sol";
import {ISiweAuth} from "../interfaces/ISiweAuth.sol";

/// @title AuthToken structure for SIWE-based authentication
struct AuthToken {
    /// @dev Domain from SIWE message (format: [ scheme "://" ] domain).
    ///      Validated at API layer only; not checked on-chain.
    string domain;
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
 * with the following differences:
 * 1. Login can only be performed off-chain. This mitigates replay attacks. As a
 *    consequence the authTokenEncKey is generated off-chain too and a copy is
 *    stored inside this contract (only whitelisted ROFL is allowed).
 * 2. Domain check is removed to allow cross-domain logins.
 */
contract SiweAuth is A13e, ISiweAuth {
    bytes32 private _authTokenEncKey;
    bytes21 public roflAppId;

    error SiweAuth_Expired();
    error SiweAuth_LoginDisabled();

    constructor(bytes21 inRoflAppId) {
        roflAppId = inRoflAppId;
    }

    /// @notice Set the AuthToken encryption key. Can only be called by the authorized ROFL app.
    /// @dev The ROFL app ID is set at deployment. Only that app can call this function.
    /// @param newKey The 32-byte Deoxys-II encryption key.
    function setAuthTokenEncKey(bytes32 newKey) external {
        Subcall.roflEnsureAuthorizedOrigin(roflAppId);
        _authTokenEncKey = newKey;
    }

    /// @notice Login is disabled - use the REST API instead.
    /// @dev This function is kept for A13e interface compatibility but always reverts.
    ///      The REST service generates and encrypts AuthTokens directly.
    function login(string calldata, SignatureRSV calldata)
        external
        pure
        override
        returns (bytes memory)
    {
        revert SiweAuth_LoginDisabled();
    }

    /// @notice Returns the keccak256 hash of the stored encryption key for debugging.
    /// @dev This allows verifying the key was set correctly without exposing the key itself.
    function getAuthTokenEncKeyHash() external view returns (bytes32) {
        return keccak256(abi.encodePacked(_authTokenEncKey));
    }

    function authMsgSender(bytes memory token)
        internal
        view
        override
        checkRevokedAuthToken(token)
        returns (address)
    {
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
        bytes memory authTokenEncoded = Sapphire.decrypt(
            _authTokenEncKey,
            0,
            token,
            ""
        );
        AuthToken memory b = abi.decode(authTokenEncoded, (AuthToken));

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
