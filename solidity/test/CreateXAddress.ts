import { expect } from "chai";
import { ethers } from "hardhat";
import {
  CREATEX_ADDRESS,
  createXGuardedSalt,
  createXPermissionedSameAddressSalt,
} from "../tasks/deploy-bridge";

describe("CreateXAddress salt helpers", () => {
  const deployer = "0x1234567890123456789012345678901234567890";
  const other = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd";
  const LABEL = "XRose:phase0";

  function withByte21(raw: string, b: number): string {
    const bytes = ethers.getBytes(raw);
    bytes[20] = b;
    return ethers.hexlify(bytes);
  }

  describe("CREATEX_ADDRESS", () => {
    it("matches the canonical factory", () => {
      expect(CREATEX_ADDRESS.toLowerCase()).to.equal(
        "0xba5ed099633d3b313e4d5f7bdc1305d3c28ba5ed",
      );
    });
  });

  describe("createXPermissionedSameAddressSalt", () => {
    it("lays out 20 bytes deployer || 0x00 || 11 bytes of keccak256(label)", () => {
      const salt = createXPermissionedSameAddressSalt(deployer, LABEL);
      const bytes = ethers.getBytes(salt);
      expect(bytes.length).to.equal(32);

      const first20 = ethers.getAddress(ethers.hexlify(bytes.slice(0, 20)));
      expect(first20).to.equal(ethers.getAddress(deployer));

      expect(bytes[20]).to.equal(0x00);

      const labelHash = ethers.getBytes(ethers.keccak256(ethers.toUtf8Bytes(LABEL)));
      expect(ethers.hexlify(bytes.slice(21))).to.equal(
        ethers.hexlify(labelHash.slice(0, 11)),
      );
    });

    it("is deterministic for the same (deployer, label)", () => {
      expect(createXPermissionedSameAddressSalt(deployer, LABEL)).to.equal(
        createXPermissionedSameAddressSalt(deployer, LABEL),
      );
    });

    it("differs across labels", () => {
      expect(createXPermissionedSameAddressSalt(deployer, "a")).to.not.equal(
        createXPermissionedSameAddressSalt(deployer, "b"),
      );
    });

    it("differs across deployers", () => {
      expect(createXPermissionedSameAddressSalt(deployer, LABEL)).to.not.equal(
        createXPermissionedSameAddressSalt(other, LABEL),
      );
    });
  });

  describe("createXGuardedSalt — branch A (byte21 = 0x00)", () => {
    const rawSalt = createXPermissionedSameAddressSalt(deployer, LABEL);

    it("produces the same guarded salt across chainIds 1, 84532, 23295", () => {
      const g1 = createXGuardedSalt(rawSalt, deployer, 1n);
      const g2 = createXGuardedSalt(rawSalt, deployer, 84532n);
      const g3 = createXGuardedSalt(rawSalt, deployer, 23295n);
      expect(g1).to.equal(g2);
      expect(g2).to.equal(g3);
    });

    it("differs from the raw salt (transformation happened)", () => {
      expect(createXGuardedSalt(rawSalt, deployer, 1n)).to.not.equal(rawSalt);
    });

    it("matches keccak256(abi.encode(deployerWord, rawSalt))", () => {
      const expected = ethers.keccak256(
        ethers.AbiCoder.defaultAbiCoder().encode(
          ["bytes32", "bytes32"],
          [ethers.zeroPadValue(deployer, 32), rawSalt],
        ),
      );
      expect(createXGuardedSalt(rawSalt, deployer, 0n)).to.equal(expected);
    });
  });

  describe("createXGuardedSalt — branch B (byte21 = 0x01)", () => {
    const rawSalt = withByte21(createXPermissionedSameAddressSalt(deployer, LABEL), 0x01);

    it("produces distinct guarded salts across chainIds 1, 84532, 23295", () => {
      const g1 = createXGuardedSalt(rawSalt, deployer, 1n);
      const g2 = createXGuardedSalt(rawSalt, deployer, 84532n);
      const g3 = createXGuardedSalt(rawSalt, deployer, 23295n);
      expect(g1).to.not.equal(g2);
      expect(g2).to.not.equal(g3);
      expect(g1).to.not.equal(g3);
    });

    it("differs from the raw salt", () => {
      expect(createXGuardedSalt(rawSalt, deployer, 1n)).to.not.equal(rawSalt);
    });

    it("matches keccak256(abi.encode(deployerWord, chainIdWord, rawSalt))", () => {
      const chainId = 84532n;
      const expected = ethers.keccak256(
        ethers.AbiCoder.defaultAbiCoder().encode(
          ["bytes32", "bytes32", "bytes32"],
          [
            ethers.zeroPadValue(deployer, 32),
            ethers.toBeHex(chainId, 32),
            rawSalt,
          ],
        ),
      );
      expect(createXGuardedSalt(rawSalt, deployer, chainId)).to.equal(expected);
    });
  });

  describe("createXGuardedSalt — rejects unsupported layouts", () => {
    it("throws when byte21 = 0x02", () => {
      const salt = withByte21(createXPermissionedSameAddressSalt(deployer, LABEL), 0x02);
      expect(() => createXGuardedSalt(salt, deployer, 1n)).to.throw(/byte21=0x02/);
    });

    it("throws when byte21 = 0xff", () => {
      const salt = withByte21(createXPermissionedSameAddressSalt(deployer, LABEL), 0xff);
      expect(() => createXGuardedSalt(salt, deployer, 1n)).to.throw(/byte21=0xff/);
    });

    it("throws when first20 != deployer", () => {
      const salt = createXPermissionedSameAddressSalt(other, LABEL);
      expect(() => createXGuardedSalt(salt, deployer, 1n)).to.throw(
        /first20=.* must equal deployer=/,
      );
    });

    it("throws when rawSalt is not bytes32", () => {
      expect(() => createXGuardedSalt("0x1234", deployer, 1n)).to.throw(/bytes32/);
    });
  });
});
