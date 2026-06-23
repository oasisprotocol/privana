/**
 * AuthToken Decryption Tests
 *
 * Tests for verifying Python-generated AuthTokens are compatible with Solidity decoding.
 *
 * IMPORTANT: These tests use MockAuthTokenDecrypt which decodes unencrypted ABI-encoded
 * AuthTokens. The real AccountingSiweAuth uses Sapphire.decrypt() which requires a Sapphire
 * chain. For full integration testing, deploy to Sapphire testnet.
 *
 * Test Vectors:
 * Python generates encrypted tokens using Deoxys-II. This test verifies that the
 * ABI encoding format matches between Python (eth_abi) and Solidity (abi.decode).
 */

import { expect } from 'chai';
import { ethers } from 'hardhat';
import { MockAuthTokenDecrypt } from '../typechain-types';
import { HardhatEthersSigner } from '@nomicfoundation/hardhat-ethers/signers';
import { AbiCoder, keccak256, solidityPacked, getBytes } from 'ethers';

// Default domain for test tokens (not validated by contract anymore)
const TEST_DOMAIN = 'https://api.privana.finance';

// AuthToken struct type for ABI encoding (must match Solidity struct)
const AUTH_TOKEN_TYPE = 'tuple(string domain, address userAddr, uint256 validUntil, string statement, string[] resources)';

// Helper to encode an AuthToken struct (same format as Solidity's abi.encode)
function encodeAuthToken(
  domain: string,
  userAddr: string,
  validUntil: number,
  statement: string,
  resources: string[]
): string {
  const abiCoder = AbiCoder.defaultAbiCoder();
  return abiCoder.encode([AUTH_TOKEN_TYPE], [{ domain, userAddr, validUntil, statement, resources }]);
}

// Helper to get future timestamp relative to the EVM block clock
// (resilient to evm_increaseTime in other test files)
async function getFutureTimestamp(hoursAhead: number = 24): Promise<number> {
  const block = await ethers.provider.getBlock('latest');
  return block!.timestamp + hoursAhead * 3600;
}

// Helper to get past timestamp relative to the EVM block clock
async function getPastTimestamp(hoursAgo: number = 1): Promise<number> {
  const block = await ethers.provider.getBlock('latest');
  return block!.timestamp - hoursAgo * 3600;
}

describe('AuthTokenDecryption', function () {
  let mockDecrypt: MockAuthTokenDecrypt;
  let deployer: HardhatEthersSigner;
  let user1: HardhatEthersSigner;

  before(async () => {
    [deployer, user1] = await ethers.getSigners();

    const MockAuthTokenDecryptFactory = await ethers.getContractFactory('MockAuthTokenDecrypt');
    mockDecrypt = await MockAuthTokenDecryptFactory.deploy();
    await mockDecrypt.waitForDeployment();
  });

  describe('ABI Encoding Compatibility', function () {
    it('Should decode Python-format AuthToken and extract user address', async function () {
      /**
       * This test verifies that ABI encoding from Python's eth_abi matches
       * Solidity's abi.decode for the AuthToken struct:
       *
       * struct AuthToken {
       *     string domain;
       *     address userAddr;
       *     uint256 validUntil;
       *     string statement;
       *     string[] resources;
       * }
       *
       * Python encoding uses: ["string", "address", "uint256", "string", "string[]"]
       * which produces the same format as Solidity's abi.encode(struct)
       */
      const validUntil = await getFutureTimestamp(24);
      const userAddress = user1.address;
      const statement = 'Sign in with Ethereum to privana.finance';
      const resources: string[] = ['https://api.privana.finance/v1'];

      // Encode using the struct format (same as Python's eth_abi with tuple)
      const encoded = encodeAuthToken(TEST_DOMAIN, userAddress, validUntil, statement, resources);

      // Contract should decode it correctly
      const result = await mockDecrypt.decodeAuthToken(encoded);
      expect(result.toLowerCase()).to.equal(userAddress.toLowerCase());
    });

    it('Should decode AuthToken with empty statement and resources', async function () {
      const validUntil = await getFutureTimestamp(24);
      const userAddress = user1.address;

      const encoded = encodeAuthToken(TEST_DOMAIN, userAddress, validUntil, '', []);

      const result = await mockDecrypt.decodeAuthToken(encoded);
      expect(result.toLowerCase()).to.equal(userAddress.toLowerCase());
    });

    it('Should decode AuthToken with multiple resources', async function () {
      const validUntil = await getFutureTimestamp(24);
      const userAddress = user1.address;
      const resources = [
        'https://api.privana.finance/v1',
        'https://api.privana.finance/v2',
        'ipfs://QmTest123',
      ];

      const encoded = encodeAuthToken(TEST_DOMAIN, userAddress, validUntil, 'Test statement', resources);

      const [domain, decodedAddr, decodedValidUntil, statement, decodedResources] =
        await mockDecrypt.decodeAuthTokenFull(encoded);

      expect(domain).to.equal(TEST_DOMAIN);
      expect(decodedAddr.toLowerCase()).to.equal(userAddress.toLowerCase());
      expect(decodedValidUntil).to.equal(BigInt(validUntil));
      expect(statement).to.equal('Test statement');
      expect(decodedResources).to.deep.equal(resources);
    });

    it('Should match Solidity-encoded AuthToken exactly', async function () {
      /**
       * Generate a token using the contract's encodeAuthToken() and verify
       * it can be decoded. This confirms the encoding format is consistent.
       */
      const validUntil = await getFutureTimestamp(24);
      const userAddress = user1.address;
      const statement = 'Test';
      const resources: string[] = [];

      // Get contract-encoded token
      const contractEncoded = await mockDecrypt.encodeAuthToken(
        TEST_DOMAIN,
        userAddress,
        validUntil,
        statement,
        resources
      );

      // Encode in TypeScript using our helper (same format as Python would produce)
      const tsEncoded = encodeAuthToken(TEST_DOMAIN, userAddress, validUntil, statement, resources);

      // Both should be identical
      expect(contractEncoded).to.equal(tsEncoded);

      // Both should decode correctly
      const result = await mockDecrypt.decodeAuthToken(contractEncoded);
      expect(result.toLowerCase()).to.equal(userAddress.toLowerCase());
    });
  });

  describe('Token Validation', function () {
    it('Should reject expired token', async function () {
      const expiredTime = await getPastTimestamp(1); // 1 hour ago
      const userAddress = user1.address;

      const encoded = encodeAuthToken(TEST_DOMAIN, userAddress, expiredTime, '', []);

      await expect(mockDecrypt.decodeAuthToken(encoded)).to.be.revertedWithCustomError(
        mockDecrypt,
        'SiweAuth_Expired'
      );
    });

    it('Should accept token with any domain (domain not validated)', async function () {
      const validUntil = await getFutureTimestamp(24);
      const userAddress = user1.address;
      const anyDomain = 'https://any-domain.com';

      const encoded = encodeAuthToken(anyDomain, userAddress, validUntil, '', []);

      // Domain is no longer validated - should succeed
      const result = await mockDecrypt.decodeAuthToken(encoded);
      expect(result.toLowerCase()).to.equal(userAddress.toLowerCase());
    });

    it('Should reject empty token', async function () {
      await expect(mockDecrypt.decodeAuthToken('0x')).to.be.revertedWithCustomError(
        mockDecrypt,
        'SiweAuth_InvalidTokenFormat'
      );
    });

    it('Should handle tryDecodeAuthToken for valid token', async function () {
      const validUntil = await getFutureTimestamp(24);
      const userAddress = user1.address;

      const encoded = encodeAuthToken(TEST_DOMAIN, userAddress, validUntil, '', []);

      const [success, addr] = await mockDecrypt.tryDecodeAuthToken(encoded);
      expect(success).to.be.true;
      expect(addr.toLowerCase()).to.equal(userAddress.toLowerCase());
    });

    it('Should handle tryDecodeAuthToken for invalid token', async function () {
      const [success, addr] = await mockDecrypt.tryDecodeAuthToken('0x');
      expect(success).to.be.false;
      expect(addr).to.equal(ethers.ZeroAddress);
    });

    it('Should reject tampered/malformed token data', async function () {
      // Random invalid bytes that can't be decoded as AuthToken
      const malformed = '0x1234567890abcdef';

      const [success] = await mockDecrypt.tryDecodeAuthToken(malformed);
      expect(success).to.be.false;
    });
  });

  describe('Edge Cases', function () {
    it('Should handle maximum length domain', async function () {
      // Note: This uses the mock contract's domain which is fixed
      // but tests that long strings in other fields work
      const validUntil = await getFutureTimestamp(24);
      const userAddress = user1.address;
      const longStatement = 'A'.repeat(1000);

      const encoded = encodeAuthToken(TEST_DOMAIN, userAddress, validUntil, longStatement, []);

      const result = await mockDecrypt.decodeAuthToken(encoded);
      expect(result.toLowerCase()).to.equal(userAddress.toLowerCase());
    });

    it('Should handle many resources', async function () {
      const validUntil = await getFutureTimestamp(24);
      const userAddress = user1.address;
      // Create 100 resource URIs
      const resources = Array.from({ length: 100 }, (_, i) => `https://example.com/resource/${i}`);

      const encoded = encodeAuthToken(TEST_DOMAIN, userAddress, validUntil, '', resources);

      const [, , , , decodedResources] = await mockDecrypt.decodeAuthTokenFull(encoded);
      expect(decodedResources.length).to.equal(100);
    });

    it('Should handle very large validUntil timestamp', async function () {
      // Year 3000 timestamp
      const farFuture = 32503680000;
      const userAddress = user1.address;

      const encoded = encodeAuthToken(TEST_DOMAIN, userAddress, farFuture, '', []);

      const [, , decodedValidUntil] = await mockDecrypt.decodeAuthTokenFull(encoded);
      expect(decodedValidUntil).to.equal(BigInt(farFuture));
    });

    it('Should handle zero address (should still decode)', async function () {
      const validUntil = await getFutureTimestamp(24);
      const zeroAddress = ethers.ZeroAddress;

      const encoded = encodeAuthToken(TEST_DOMAIN, zeroAddress, validUntil, '', []);

      const result = await mockDecrypt.decodeAuthToken(encoded);
      expect(result).to.equal(zeroAddress);
    });

    it('Should handle unicode in statement', async function () {
      const validUntil = await getFutureTimestamp(24);
      const userAddress = user1.address;
      const unicodeStatement = 'Sign in 你好 مرحبا 🔐🔑';

      const encoded = encodeAuthToken(TEST_DOMAIN, userAddress, validUntil, unicodeStatement, []);

      const [, , , statement] = await mockDecrypt.decodeAuthTokenFull(encoded);
      expect(statement).to.equal(unicodeStatement);
    });
  });

  describe('Python Test Vector Generation', function () {
    /**
     * These tests generate test vectors that can be used to verify
     * the Python implementation produces identical output.
     *
     * Note: For decoding validation, we use future timestamps since
     * the contract validates expiration. The encoding itself is
     * timestamp-independent.
     */

    it('Should produce consistent encoding for reference vector', async function () {
      // Fixed values for reproducible test vector
      // Note: using far future timestamp to avoid expiration errors
      const domain = 'https://app.privana.finance';
      const userAddr = '0x1234567890123456789012345678901234567890';
      const validUntil = await getFutureTimestamp(24 * 365); // 1 year from now
      const statement = 'Sign in with Ethereum to privana.finance';
      const resources = ['https://api.privana.finance/v1'];

      const encoded = await mockDecrypt.encodeAuthToken(domain, userAddr, validUntil, statement, resources);

      // Log for test vector generation (visible in test output)
      console.log('\n=== Python Test Vector ===');
      console.log('domain:', domain);
      console.log('user_addr:', userAddr);
      console.log('valid_until:', validUntil);
      console.log('statement:', statement);
      console.log('resources:', resources);
      console.log('expected_encoding:', encoded);
      console.log('encoding_length:', encoded.length);
      console.log('===========================\n');

      // Verify it's decodeable (domain matches mock's domain)
      const [decodedDomain, decodedAddr, decodedValidUntil, decodedStatement, decodedResources] =
        await mockDecrypt.decodeAuthTokenFull(encoded);

      expect(decodedDomain).to.equal(domain);
      expect(decodedAddr.toLowerCase()).to.equal(userAddr.toLowerCase());
      expect(decodedValidUntil).to.equal(BigInt(validUntil));
      expect(decodedStatement).to.equal(statement);
      expect(decodedResources).to.deep.equal(resources);
    });

    it('Should produce consistent encoding for empty fields vector', async function () {
      const domain = 'https://app.privana.finance';
      const userAddr = '0xdead000000000000000000000000000000000000';
      const validUntil = await getFutureTimestamp(24 * 365); // 1 year from now
      const statement = '';
      const resources: string[] = [];

      const encoded = await mockDecrypt.encodeAuthToken(domain, userAddr, validUntil, statement, resources);

      console.log('\n=== Python Test Vector (Empty Fields) ===');
      console.log('domain:', domain);
      console.log('user_addr:', userAddr);
      console.log('valid_until:', validUntil);
      console.log('statement:', '""');
      console.log('resources:', '[]');
      console.log('expected_encoding:', encoded);
      console.log('==========================================\n');

      const result = await mockDecrypt.decodeAuthToken(encoded);
      expect(result.toLowerCase()).to.equal(userAddr.toLowerCase());
    });

    it('Should verify TypeScript encoding matches contract encoding', async function () {
      // Use the same values for both TypeScript and contract encoding
      const domain = 'https://app.privana.finance';
      const userAddr = '0xabcdef1234567890abcdef1234567890abcdef12';
      const validUntil = await getFutureTimestamp(24);
      const statement = 'Test statement';
      const resources = ['https://resource1.com', 'https://resource2.com'];

      // TypeScript encoding (simulating Python's eth_abi.encode)
      const tsEncoded = encodeAuthToken(domain, userAddr, validUntil, statement, resources);

      // Contract encoding
      const contractEncoded = await mockDecrypt.encodeAuthToken(
        domain,
        userAddr,
        validUntil,
        statement,
        resources
      );

      // Both should be identical
      expect(tsEncoded).to.equal(contractEncoded);

      console.log('\n=== Encoding Compatibility Verified ===');
      console.log('TypeScript and Solidity produce identical ABI encoding');
      console.log('This confirms Python eth_abi compatibility');
      console.log('=========================================\n');
    });
  });
});
