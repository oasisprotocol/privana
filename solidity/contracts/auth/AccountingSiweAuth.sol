// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.20;

import {SignatureRSV, A13e} from "@oasisprotocol/sapphire-contracts/contracts/auth/A13e.sol";
import {Sapphire} from "@oasisprotocol/sapphire-contracts/contracts/Sapphire.sol";
import {Subcall} from "@oasisprotocol/sapphire-contracts/contracts/Subcall.sol";

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
 * with one key difference: it avoids calling Sapphire precompiles in the constructor
 * on non-Sapphire test networks (e.g., Hardhat), so unit tests can deploy contracts.
 *
 * The authentication logic (login/authMsgSender) is only supported on Sapphire chains.
 */
contract AccountingSiweAuth is A13e {
    bytes32 private _authTokenEncKey;
    bytes21 private _roflAppId;

    error SiweAuth_UnsupportedChain();
    error SiweAuth_Expired();
    error SiweAuth_NotAuthorizedRofl();
    error SiweAuth_LoginDisabled();

    constructor(bytes21 inRoflAppId) {
        _roflAppId = inRoflAppId;

        // TODO: Remove non-Sapphire fallback when Sapphire localnet e2e tests are available.
        // This allows deployment on Hardhat/local networks for unit testing.
        if (!_isSapphireChainId(block.chainid)) {
            // Deterministic key for non-Sapphire local test networks (e.g., Hardhat).
            // Authentication is not expected to be used off-Sapphire.
            // On Sapphire, the ROFL service will set the key via setAuthTokenEncKey().
            _authTokenEncKey = bytes32(uint256(1));
        }
    }

    // TODO: Remove when Sapphire localnet e2e tests are available.
    function _isSapphireChainId(uint256 chainId) private pure returns (bool) {
        return chainId == 0x5afe || chainId == 0x5aff || chainId == 0x5afd;
    }

    function _requireSapphire() private view {
        if (!_isSapphireChainId(block.chainid)) {
            revert SiweAuth_UnsupportedChain();
        }
    }

    /// @notice Set the AuthToken encryption key. Can only be called by the authorized ROFL app.
    /// @dev The ROFL app ID is set at deployment. Only that app can call this function.
    /// @param newKey The 32-byte Deoxys-II encryption key.
    function setAuthTokenEncKey(bytes32 newKey) external {
        if (Subcall.getRoflAppId() != _roflAppId) {
            revert SiweAuth_NotAuthorizedRofl();
        }
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

    function roflAppId() public view returns (bytes21) {
        return _roflAppId;
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
