import { expect } from "chai";
import { parseRoflAppId } from "../tasks/utils/rofl";

describe("parseRoflAppId", function () {
  // Known test vector from rofl.yaml
  const BECH32_ID = "rofl1qrp55hemyhu2g2n6k3jyquu4x92ttmzz0vpad9q6";
  const HEX_ID = "0x00c34a5f3b25f8a42a7ab4644073953154b5ec427b";

  describe("bech32 format (rofl1...)", function () {
    it("should decode valid bech32 rofl1 address", function () {
      const result = parseRoflAppId(BECH32_ID);
      expect(result).to.equal(HEX_ID);
    });

    it("should produce 21-byte output", function () {
      const result = parseRoflAppId(BECH32_ID);
      // 0x prefix + 42 hex chars = 21 bytes
      expect(result.slice(2).length).to.equal(42);
    });

    it("should reject invalid bech32 checksum", function () {
      const invalid = "rofl1qrp55hemyhu2g2n6k3jyquu4x92ttmzz0vpad9q7"; // wrong last char
      expect(() => parseRoflAppId(invalid)).to.throw("Invalid bech32");
    });

    it("should reject bech32 with wrong prefix", function () {
      // oasis1 address instead of rofl1
      const oasisAddr = "oasis1qrp55hemyhu2g2n6k3jyquu4x92ttmzzql37yq7x";
      expect(() => parseRoflAppId(oasisAddr)).to.throw("Invalid ROFL app ID format");
    });
  });

  describe("hex format (0x...)", function () {
    it("should accept valid 21-byte hex", function () {
      const result = parseRoflAppId(HEX_ID);
      expect(result).to.equal(HEX_ID);
    });

    it("should normalize hex to lowercase", function () {
      const upperHex = "0x00C34A5F3B25F8A42A7AB4644073953154B5EC427B";
      const result = parseRoflAppId(upperHex);
      expect(result).to.equal(HEX_ID);
    });

    it("should reject hex with wrong length (too short)", function () {
      const shortHex = "0x00c34a5f3b25f8a42a7ab464407395";
      expect(() => parseRoflAppId(shortHex)).to.throw("expected 21 bytes");
    });

    it("should reject hex with wrong length (too long)", function () {
      const longHex = "0x00c34a5f3b25f8a42a7ab4644073953154b5ec427b00";
      expect(() => parseRoflAppId(longHex)).to.throw("expected 21 bytes");
    });

    it("should reject hex with invalid characters", function () {
      const invalidHex = "0x00c34a5f3b25f8a42a7ab46440739531xyz5ec427b"; // 42 chars with xyz
      expect(() => parseRoflAppId(invalidHex)).to.throw("non-hex characters");
    });
  });

  describe("invalid formats", function () {
    it("should reject empty string", function () {
      expect(() => parseRoflAppId("")).to.throw("Invalid ROFL app ID format");
    });

    it("should reject random string", function () {
      expect(() => parseRoflAppId("not-a-rofl-id")).to.throw("Invalid ROFL app ID format");
    });

    it("should reject Ethereum address", function () {
      const ethAddr = "0x742d35Cc6634C0532925a3b844Bc9e7595f8123a";
      expect(() => parseRoflAppId(ethAddr)).to.throw("expected 21 bytes");
    });
  });

  describe("round-trip consistency", function () {
    it("should produce same hex from bech32 as direct hex input", function () {
      const fromBech32 = parseRoflAppId(BECH32_ID);
      const fromHex = parseRoflAppId(HEX_ID);
      expect(fromBech32).to.equal(fromHex);
    });
  });
});
