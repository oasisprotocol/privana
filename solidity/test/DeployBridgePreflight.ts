import { expect } from "chai";
import { ethers } from "hardhat";
import { createHash } from "node:crypto";

import {
  assertCreateXDeployed,
  assertHelperRequired,
  assertManifestAgreement,
  assertUpstreamLockIntact,
  buildRoflBridgeInitCode,
  buildXrosePreflight,
  createXGuardedSalt,
  createXPermissionedSameAddressSalt,
  parseUpstreamLock,
  type BridgeManifest,
  type PreflightInputs,
} from "../tasks/deploy-bridge";

const DEPLOYER = "0x1234567890123456789012345678901234567890";
const ROFL_SIGNER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const PAUSE_ADMIN = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const PREDICTED_XROSE = "0xcccccccccccccccccccccccccccccccccccccccc";
const LABEL = "XRose:phase0";

const FAKE_BYTECODE = "0x6080604052"; // doesn't have to be real; helpers only hash/concatenate it
const FAKE_DEPLOYED = "0x6080604000";
const FAKE_BRIDGE_BYTECODE = "0x60a0604052";

function buildInputs(overrides: Partial<PreflightInputs> = {}): PreflightInputs {
  return {
    chainId: 84532n,
    deployer: DEPLOYER,
    xroseName: "XRose",
    xroseSymbol: "xROSE",
    label: LABEL,
    xroseArtifact: { bytecode: FAKE_BYTECODE, deployedBytecode: FAKE_DEPLOYED },
    ...overrides,
  };
}

function sha256Hex(data: Buffer | string): string {
  const buf = typeof data === "string" ? Buffer.from(data, "utf8") : data;
  return createHash("sha256").update(buf).digest("hex");
}

describe("deploy-bridge preflight helpers", () => {
  describe("assertCreateXDeployed", () => {
    it("returns silently when code is non-empty", () => {
      expect(() =>
        assertCreateXDeployed("0x6080604052", "0xba5Ed099633D3B313e4D5F7bdc1305d3c28ba5Ed"),
      ).to.not.throw();
    });

    it("throws naming the address when code is 0x", () => {
      expect(() =>
        assertCreateXDeployed("0x", "0xba5Ed099633D3B313e4D5F7bdc1305d3c28ba5Ed"),
      ).to.throw(/CreateX not deployed.*0xba5Ed099633D3B313e4D5F7bdc1305d3c28ba5Ed/i);
    });

    it("treats empty string as not deployed (defensive)", () => {
      expect(() =>
        assertCreateXDeployed("", "0xba5Ed099633D3B313e4D5F7bdc1305d3c28ba5Ed"),
      ).to.throw(/CreateX not deployed/i);
    });
  });

  describe("assertHelperRequired", () => {
    it("returns when raw and guarded predictions differ", () => {
      expect(() =>
        assertHelperRequired(
          "0x1111111111111111111111111111111111111111",
          "0x2222222222222222222222222222222222222222",
        ),
      ).to.not.throw();
    });

    it("throws when raw and guarded predictions match", () => {
      const same = "0x1111111111111111111111111111111111111111";
      expect(() => assertHelperRequired(same, same)).to.throw(
        /computeCreate3Address\(rawSalt\) == computeCreate3Address\(guardedSalt\)/,
      );
    });
  });

  describe("assertManifestAgreement", () => {
    const fullManifest: BridgeManifest = {
      network: "base-sepolia",
      chainId: 84532,
      createX: "0xba5Ed099633D3B313e4D5F7bdc1305d3c28ba5Ed",
      saltMode: "permissioned-no-cross-chain-redeploy-protection",
      xroseRawSalt: "0x" + "11".repeat(32),
      xroseGuardedSalt: "0x" + "22".repeat(32),
      xrose: PREDICTED_XROSE,
      roflBridgeRawSalt: "0x" + "33".repeat(32),
      roflBridgeGuardedSalt: "0x" + "44".repeat(32),
      roflBridgeSameAddressRequired: false,
      roflBridge: "0xdddddddddddddddddddddddddddddddddddddddd",
      roflSigner: ROFL_SIGNER,
      owner: DEPLOYER,
      factory: DEPLOYER,
      xerc20Source: "defi-wonderland/xERC20@v1.0.0",
      xroseConstructor: ["XRose", "xROSE", DEPLOYER],
      xroseRuntimeHash: "0x" + "ab".repeat(32),
      mintLimit: "250000000000000000000000",
      burnLimit: "250000000000000000000000",
    };

    it("returns silently when manifest is null (first deploy)", () => {
      expect(() => assertManifestAgreement({ xrose: PREDICTED_XROSE }, null)).to.not.throw();
    });

    it("returns silently when every predicted field matches the manifest", () => {
      expect(() =>
        assertManifestAgreement(
          {
            xrose: fullManifest.xrose,
            roflBridge: fullManifest.roflBridge,
            roflSigner: fullManifest.roflSigner,
            xroseConstructor: ["XRose", "xROSE", DEPLOYER],
            xroseRuntimeHash: fullManifest.xroseRuntimeHash,
          },
          fullManifest,
        ),
      ).to.not.throw();
    });

    it("throws naming the field on xrose mismatch", () => {
      expect(() =>
        assertManifestAgreement(
          { xrose: "0xdeaddeaddeaddeaddeaddeaddeaddeaddeaddead" },
          fullManifest,
        ),
      ).to.throw(/manifest mismatch.*field=xrose.*expected.*actual/);
    });

    it("throws naming the field on roflSigner mismatch", () => {
      expect(() =>
        assertManifestAgreement({ roflSigner: DEPLOYER }, fullManifest),
      ).to.throw(/manifest mismatch.*field=roflSigner/);
    });

    it("throws on xroseConstructor array mismatch", () => {
      expect(() =>
        assertManifestAgreement(
          { xroseConstructor: ["XRose", "xROSE", ROFL_SIGNER] },
          fullManifest,
        ),
      ).to.throw(/manifest mismatch.*field=xroseConstructor/);
    });

    it("throws on xroseRuntimeHash mismatch", () => {
      expect(() =>
        assertManifestAgreement(
          { xroseRuntimeHash: "0x" + "00".repeat(32) },
          fullManifest,
        ),
      ).to.throw(/manifest mismatch.*field=xroseRuntimeHash/);
    });
  });

  describe("parseUpstreamLock", () => {
    it("parses tab-separated triples", () => {
      const text =
        "solidity/contracts/XERC20.sol\tsolidity/contracts/bridge/XRose.sol\t7481d0c33761360327f5145e2d116f3d3ddb8b93e848043bc3731c0d9834738d\n" +
        "solidity/interfaces/IXERC20.sol\tsolidity/contracts/bridge/IXERC20.sol\t2c2b7cdc5ef4c51812bc422b24e3f4adf1247c08220d96fc162463b17993adba\n";
      const entries = parseUpstreamLock(text);
      expect(entries).to.have.length(2);
      expect(entries[0]).to.deep.equal({
        upstreamPath: "solidity/contracts/XERC20.sol",
        vendorPath: "solidity/contracts/bridge/XRose.sol",
        sha256: "7481d0c33761360327f5145e2d116f3d3ddb8b93e848043bc3731c0d9834738d",
      });
      expect(entries[1].vendorPath).to.equal("solidity/contracts/bridge/IXERC20.sol");
    });

    it("ignores blank lines", () => {
      const text =
        "\n" +
        "solidity/a\tsolidity/b\t" + "a".repeat(64) + "\n" +
        "\n";
      expect(parseUpstreamLock(text)).to.have.length(1);
    });

    it("throws on malformed lines (wrong column count)", () => {
      expect(() => parseUpstreamLock("only\ttwo-columns\n")).to.throw(/malformed.*\.upstream-lock/);
    });

    it("throws when hash is not 64 hex chars", () => {
      expect(() => parseUpstreamLock("a\tb\tnothex\n")).to.throw(/sha256/);
    });
  });

  describe("assertUpstreamLockIntact", () => {
    const fileA = "contract A {}";
    const fileB = "contract B {}";
    const lock = [
      { vendorPath: "a.sol", sha256: sha256Hex(fileA) },
      { vendorPath: "b.sol", sha256: sha256Hex(fileB) },
    ];

    it("returns when every local file matches its locked sha256", () => {
      const reader = (p: string): Buffer => {
        if (p === "a.sol") return Buffer.from(fileA, "utf8");
        if (p === "b.sol") return Buffer.from(fileB, "utf8");
        throw new Error(`unexpected ${p}`);
      };
      expect(() => assertUpstreamLockIntact(lock, reader)).to.not.throw();
    });

    it("throws naming the first drifted vendor path", () => {
      const reader = (p: string): Buffer => {
        if (p === "a.sol") return Buffer.from(fileA, "utf8");
        if (p === "b.sol") return Buffer.from("edited contract B {}", "utf8");
        throw new Error(`unexpected ${p}`);
      };
      expect(() => assertUpstreamLockIntact(lock, reader)).to.throw(
        /upstream-lock drift.*b\.sol/,
      );
    });
  });

  describe("buildXrosePreflight", () => {
    it("is deterministic for identical inputs", () => {
      const a = buildXrosePreflight(buildInputs());
      const b = buildXrosePreflight(buildInputs());
      expect(a).to.deep.equal(b);
    });

    it("xroseRawSalt uses '<label>:xrose' as the salt label", () => {
      const out = buildXrosePreflight(buildInputs());
      expect(out.xroseRawSalt).to.equal(
        createXPermissionedSameAddressSalt(DEPLOYER, `${LABEL}:xrose`),
      );
    });

    it("xroseGuardedSalt matches createXGuardedSalt(rawSalt, deployer, chainId)", () => {
      const out = buildXrosePreflight(buildInputs());
      expect(out.xroseGuardedSalt).to.equal(
        createXGuardedSalt(out.xroseRawSalt, DEPLOYER, 84532n),
      );
    });

    it("xroseGuardedSalt is chainId-invariant (byte21=0x00 same-address branch)", () => {
      const base = buildXrosePreflight(buildInputs({ chainId: 1n }));
      const onSapphire = buildXrosePreflight(buildInputs({ chainId: 23295n }));
      const onBaseSepolia = buildXrosePreflight(buildInputs({ chainId: 84532n }));
      expect(base.xroseGuardedSalt).to.equal(onSapphire.xroseGuardedSalt);
      expect(onSapphire.xroseGuardedSalt).to.equal(onBaseSepolia.xroseGuardedSalt);
    });

    it("xroseInitCode = bytecode || abi.encode(['string','string','address'], [name, symbol, deployer])", () => {
      const out = buildXrosePreflight(buildInputs());
      const encodedArgs = ethers.AbiCoder.defaultAbiCoder().encode(
        ["string", "string", "address"],
        ["XRose", "xROSE", DEPLOYER],
      );
      expect(out.xroseInitCode).to.equal(FAKE_BYTECODE + encodedArgs.slice(2));
    });

    it("xroseRuntimeHash = keccak256(deployedBytecode)", () => {
      const out = buildXrosePreflight(buildInputs());
      expect(out.xroseRuntimeHash).to.equal(ethers.keccak256(FAKE_DEPLOYED));
    });

    it("roflBridgeRawSalt uses '<label>:bridge' as the salt label (distinct from xrose)", () => {
      const out = buildXrosePreflight(buildInputs());
      expect(out.roflBridgeRawSalt).to.equal(
        createXPermissionedSameAddressSalt(DEPLOYER, `${LABEL}:bridge`),
      );
      expect(out.roflBridgeRawSalt).to.not.equal(out.xroseRawSalt);
    });

    it("roflBridgeGuardedSalt matches createXGuardedSalt for the bridge salt", () => {
      const out = buildXrosePreflight(buildInputs());
      expect(out.roflBridgeGuardedSalt).to.equal(
        createXGuardedSalt(out.roflBridgeRawSalt, DEPLOYER, 84532n),
      );
    });
  });

  describe("buildRoflBridgeInitCode", () => {
    it("appends abi.encode(['address','address','address','address'], [xrose, roflSigner, pauseAdmin, owner]) to bytecode", () => {
      const initCode = buildRoflBridgeInitCode(
        { bytecode: FAKE_BRIDGE_BYTECODE },
        PREDICTED_XROSE,
        ROFL_SIGNER,
        PAUSE_ADMIN,
        DEPLOYER,
      );
      const encodedArgs = ethers.AbiCoder.defaultAbiCoder().encode(
        ["address", "address", "address", "address"],
        [PREDICTED_XROSE, ROFL_SIGNER, PAUSE_ADMIN, DEPLOYER],
      );
      expect(initCode).to.equal(FAKE_BRIDGE_BYTECODE + encodedArgs.slice(2));
    });

    it("is deterministic for identical inputs", () => {
      const a = buildRoflBridgeInitCode(
        { bytecode: FAKE_BRIDGE_BYTECODE },
        PREDICTED_XROSE,
        ROFL_SIGNER,
        PAUSE_ADMIN,
        DEPLOYER,
      );
      const b = buildRoflBridgeInitCode(
        { bytecode: FAKE_BRIDGE_BYTECODE },
        PREDICTED_XROSE,
        ROFL_SIGNER,
        PAUSE_ADMIN,
        DEPLOYER,
      );
      expect(a).to.equal(b);
    });
  });
});
