// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.20;

import {AuthToken} from "../auth/AccountingSiweAuth.sol";

/**
 * @title MockAuthTokenDecrypt
 * @notice Test helper for verifying AuthToken ABI encoding compatibility between Python and Solidity.
 * @dev This contract allows testing the ABI decoding of AuthTokens without requiring Sapphire's
 *      encryption. Used to verify that Python's eth_abi encoding matches Solidity's abi.decode.
 *
 *      The real AccountingSiweAuth uses Sapphire.decrypt() which is only available on Sapphire.
 *      This mock skips decryption and decodes tokens directly, testing only the struct format.
 *
 * TODO: Remove this mock when Sapphire localnet e2e tests are available.
 */
contract MockAuthTokenDecrypt {
    uint256 private constant DEFAULT_VALIDITY = 24 hours;

    error SiweAuth_Expired();
    error SiweAuth_InvalidTokenFormat();

    constructor() {}

    /**
     * @notice Decode an ABI-encoded AuthToken and return the user address.
     * @dev This simulates what authMsgSender() does after decryption on Sapphire.
     * @param encodedToken ABI-encoded AuthToken (NOT encrypted, for testing only)
     * @return userAddr The user address from the token
     */
    function decodeAuthToken(bytes calldata encodedToken) external view returns (address userAddr) {
        AuthToken memory token = _decodeAndValidate(encodedToken);
        return token.userAddr;
    }

    /**
     * @notice Decode and return all fields of an AuthToken for testing.
     * @param encodedToken ABI-encoded AuthToken
     */
    function decodeAuthTokenFull(
        bytes calldata encodedToken
    )
        external
        view
        returns (
            string memory tokenDomain,
            address userAddr,
            uint256 validUntil,
            string memory statement,
            string[] memory resources
        )
    {
        AuthToken memory token = _decodeAndValidate(encodedToken);
        return (token.domain, token.userAddr, token.validUntil, token.statement, token.resources);
    }

    /**
     * @notice Try to decode a token and return success/failure.
     * @dev Useful for testing invalid token formats without reverting.
     */
    function tryDecodeAuthToken(bytes calldata encodedToken) external view returns (bool success, address userAddr) {
        try this.decodeAuthToken(encodedToken) returns (address addr) {
            return (true, addr);
        } catch {
            return (false, address(0));
        }
    }

    /**
     * @notice Decode and validate an AuthToken.
     * @dev Performs expiry validation, matching AccountingSiweAuth behavior.
     */
    function _decodeAndValidate(bytes calldata encodedToken) internal view returns (AuthToken memory) {
        if (encodedToken.length == 0) {
            revert SiweAuth_InvalidTokenFormat();
        }

        // Decode the AuthToken struct
        AuthToken memory token = abi.decode(encodedToken, (AuthToken));

        // Validate token hasn't expired
        if (token.validUntil < block.timestamp) {
            revert SiweAuth_Expired();
        }

        return token;
    }

    /**
     * @notice Encode an AuthToken to bytes for test vector generation.
     * @dev Generates ABI-encoded AuthToken that Python should produce identically.
     */
    function encodeAuthToken(
        string calldata tokenDomain,
        address userAddr,
        uint256 validUntil,
        string calldata statement,
        string[] calldata resources
    ) external pure returns (bytes memory) {
        AuthToken memory token = AuthToken({
            domain: tokenDomain,
            userAddr: userAddr,
            validUntil: validUntil,
            statement: statement,
            resources: resources
        });
        return abi.encode(token);
    }
}
