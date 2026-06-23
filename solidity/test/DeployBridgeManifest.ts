// Unit tests for buildBridgeManifest / reconcileManifest / writeManifest* plus the XERC20_SOURCE <-> README parity check. Pure / filesystem-only, no deploys.

import { expect } from "chai";
import { access, mkdtemp, readFile, rm } from "node:fs/promises";
import { existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  XERC20_SOURCE,
  buildBridgeManifest,
  reconcileManifest,
  writeBridgeManifestAtomic,
  writeManifestAtPath,
  type BridgeManifest,
  type BuildBridgeManifestArgs,
} from "../tasks/deploy-bridge";

const DEPLOYER = "0x1234567890123456789012345678901234567890";
const ROFL_SIGNER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const XROSE = "0xcccccccccccccccccccccccccccccccccccccccc";
const BRIDGE = "0xdddddddddddddddddddddddddddddddddddddddd";
const CREATEX = "0xba5Ed099633D3B313e4D5F7bdc1305d3c28ba5Ed";
const XROSE_RUNTIME_HASH = "0x" + "ab".repeat(32);
const RAW_XROSE_SALT = "0x" + "11".repeat(32);
const GUARDED_XROSE_SALT = "0x" + "22".repeat(32);
const RAW_BRIDGE_SALT = "0x" + "33".repeat(32);
const GUARDED_BRIDGE_SALT = "0x" + "44".repeat(32);

const MINT_LIMIT = 250000000000000000000000n; // parseEther("250000")
const BURN_LIMIT = 250000000000000000000000n;
const MINT_LIMIT_SMALLER = 100000000000000000000000n; // parseEther("100000")

function buildArgs(overrides: Partial<BuildBridgeManifestArgs> = {}): BuildBridgeManifestArgs {
  return {
    network: "base-sepolia",
    chainId: 84532,
    createX: CREATEX,
    xroseAddress: XROSE,
    xroseRawSalt: RAW_XROSE_SALT,
    xroseGuardedSalt: GUARDED_XROSE_SALT,
    xroseRuntimeHash: XROSE_RUNTIME_HASH,
    xroseConstructor: ["XRose", "xROSE", DEPLOYER],
    roflBridgeAddress: BRIDGE,
    roflBridgeRawSalt: RAW_BRIDGE_SALT,
    roflBridgeGuardedSalt: GUARDED_BRIDGE_SALT,
    roflSigner: ROFL_SIGNER,
    owner: DEPLOYER,
    factory: DEPLOYER,
    mintLimit: MINT_LIMIT,
    burnLimit: BURN_LIMIT,
    ...overrides,
  };
}

function buildExpectedManifest(overrides: Partial<BridgeManifest> = {}): BridgeManifest {
  return {
    network: "base-sepolia",
    chainId: 84532,
    createX: CREATEX,
    saltMode: "permissioned-no-cross-chain-redeploy-protection",
    xroseRawSalt: RAW_XROSE_SALT,
    xroseGuardedSalt: GUARDED_XROSE_SALT,
    xrose: XROSE,
    roflBridgeRawSalt: RAW_BRIDGE_SALT,
    roflBridgeGuardedSalt: GUARDED_BRIDGE_SALT,
    roflBridgeSameAddressRequired: false,
    roflBridge: BRIDGE,
    roflSigner: ROFL_SIGNER,
    owner: DEPLOYER,
    factory: DEPLOYER,
    xerc20Source: XERC20_SOURCE,
    xroseConstructor: ["XRose", "xROSE", DEPLOYER],
    xroseRuntimeHash: XROSE_RUNTIME_HASH,
    mintLimit: MINT_LIMIT.toString(),
    burnLimit: BURN_LIMIT.toString(),
    ...overrides,
  };
}

describe("DeployBridgeManifest", () => {
  describe("buildBridgeManifest", () => {
    it("builds a fully-populated manifest with every field", () => {
      const manifest = buildBridgeManifest(buildArgs());
      expect(manifest).to.deep.equal(buildExpectedManifest());
      expect(manifest.saltMode).to.equal("permissioned-no-cross-chain-redeploy-protection");
      expect(manifest.roflBridgeSameAddressRequired).to.equal(false);
      expect(manifest.xerc20Source).to.equal(XERC20_SOURCE);
    });

    it("converts a bigint chainId to number", () => {
      const manifest = buildBridgeManifest(buildArgs({ chainId: 84532n }));
      expect(manifest.chainId).to.equal(84532);
      expect(typeof manifest.chainId).to.equal("number");
    });

    it("stringifies bigint mintLimit / burnLimit as decimal", () => {
      const manifest = buildBridgeManifest(buildArgs());
      expect(manifest.mintLimit).to.equal("250000000000000000000000");
      expect(manifest.burnLimit).to.equal("250000000000000000000000");
    });
  });

  describe("reconcileManifest", () => {
    it("returns { action: \"write\" } when existing is null", () => {
      const proposed = buildExpectedManifest();
      const result = reconcileManifest(null, proposed);
      expect(result.action).to.equal("write");
      expect(result.manifest).to.deep.equal(proposed);
      expect(result.changedFields).to.deep.equal([]);
    });

    it("returns { action: \"noop\" } when manifests match exactly", () => {
      const existing = buildExpectedManifest();
      const proposed = buildExpectedManifest();
      const result = reconcileManifest(existing, proposed);
      expect(result.action).to.equal("noop");
      expect(result.manifest).to.deep.equal(existing);
      expect(result.changedFields).to.deep.equal([]);
    });

    it("returns { action: \"update\" } when only mintLimit differs", () => {
      const existing = buildExpectedManifest();
      const proposed = buildExpectedManifest({ mintLimit: MINT_LIMIT_SMALLER.toString() });
      const result = reconcileManifest(existing, proposed);
      expect(result.action).to.equal("update");
      expect(result.changedFields).to.deep.equal(["mintLimit"]);
      expect(result.manifest.mintLimit).to.equal(MINT_LIMIT_SMALLER.toString());
    });

    it("returns { action: \"update\" } when only burnLimit differs", () => {
      const existing = buildExpectedManifest();
      const proposed = buildExpectedManifest({ burnLimit: MINT_LIMIT_SMALLER.toString() });
      const result = reconcileManifest(existing, proposed);
      expect(result.action).to.equal("update");
      expect(result.changedFields).to.deep.equal(["burnLimit"]);
    });

    it("returns { action: \"update\" } when both limits differ", () => {
      const existing = buildExpectedManifest();
      const proposed = buildExpectedManifest({
        mintLimit: MINT_LIMIT_SMALLER.toString(),
        burnLimit: MINT_LIMIT_SMALLER.toString(),
      });
      const result = reconcileManifest(existing, proposed);
      expect(result.action).to.equal("update");
      expect(result.changedFields).to.have.members(["mintLimit", "burnLimit"]);
    });

    it("throws naming the field on xrose mismatch", () => {
      const existing = buildExpectedManifest();
      const proposed = buildExpectedManifest({
        xrose: "0xdeaddeaddeaddeaddeaddeaddeaddeaddeaddead",
      });
      expect(() => reconcileManifest(existing, proposed)).to.throw(
        /manifest mismatch.*xrose.*existing.*proposed/s,
      );
    });
  });

  describe("writeManifestAtPath", () => {
    let workdir: string;

    beforeEach(async () => {
      workdir = await mkdtemp(join(tmpdir(), "bridge-manifest-"));
    });

    afterEach(async () => {
      await rm(workdir, { recursive: true, force: true });
    });

    it("writes JSON to the target path and returns the path", async () => {
      const target = join(workdir, "deployments", "bridge-base-sepolia.json");
      const manifest = buildExpectedManifest();

      const written = await writeManifestAtPath(target, manifest);

      expect(written).to.equal(target);
      expect(existsSync(target)).to.equal(true);
      const parsed = JSON.parse(await readFile(target, "utf8"));
      expect(parsed).to.deep.equal(manifest);
    });

    it("creates parent directories if they do not exist", async () => {
      const target = join(workdir, "a", "b", "c", "bridge.json");
      expect(existsSync(join(workdir, "a"))).to.equal(false);

      await writeManifestAtPath(target, buildExpectedManifest());

      expect(existsSync(target)).to.equal(true);
    });

    it("leaves no .tmp file behind after a successful write (atomic)", async () => {
      const target = join(workdir, "bridge-base-sepolia.json");
      await writeManifestAtPath(target, buildExpectedManifest());

      const tmp = `${target}.tmp`;
      let tmpExists = true;
      try {
        await access(tmp);
      } catch {
        tmpExists = false;
      }
      expect(tmpExists).to.equal(false);
    });

    it("overwrites an existing file on update", async () => {
      const target = join(workdir, "bridge-base-sepolia.json");
      await writeManifestAtPath(target, buildExpectedManifest());

      const updated = buildExpectedManifest({ mintLimit: MINT_LIMIT_SMALLER.toString() });
      await writeManifestAtPath(target, updated);

      const parsed = JSON.parse(await readFile(target, "utf8"));
      expect(parsed.mintLimit).to.equal(MINT_LIMIT_SMALLER.toString());
    });
  });

  describe("writeBridgeManifestAtomic", () => {
    // Writes to the real solidity/deployments/ path; uses a sandbox network name + afterEach cleanup so the suite never touches a real deployment record.
    const SANDBOX_NETWORK = "test-bridge-manifest-sandbox";
    const sandboxPath = join(
      __dirname,
      "..",
      "deployments",
      `bridge-${SANDBOX_NETWORK}.json`,
    );

    afterEach(async () => {
      await rm(sandboxPath, { force: true });
    });

    it("writes to solidity/deployments/bridge-<network>.json", async () => {
      const manifest = buildExpectedManifest({ network: SANDBOX_NETWORK });
      const written = await writeBridgeManifestAtomic(SANDBOX_NETWORK, manifest);
      expect(written).to.equal(sandboxPath);
      const parsed = JSON.parse(await readFile(sandboxPath, "utf8"));
      expect(parsed).to.deep.equal(manifest);
    });

    it("supports re-writing the file (idempotent on identical content)", async () => {
      const manifest = buildExpectedManifest({ network: SANDBOX_NETWORK });
      await writeBridgeManifestAtomic(SANDBOX_NETWORK, manifest);
      await writeBridgeManifestAtomic(SANDBOX_NETWORK, manifest);
      const parsed = JSON.parse(await readFile(sandboxPath, "utf8"));
      expect(parsed).to.deep.equal(manifest);
    });
  });

  describe("XERC20_SOURCE ↔ README parity", () => {
    it("matches the commit hash pinned in solidity/contracts/bridge/README.md", () => {
      const readmePath = join(__dirname, "..", "contracts", "bridge", "README.md");
      const readme = readFileSync(readmePath, "utf8");

      // The README's Pin table has a row like:
      // | Commit     | [`<40-hex>`](https://github.com/.../tree/<40-hex>) |
      const match = readme.match(/\|\s*Commit\s*\|\s*\[`([0-9a-f]{40})`\]/i);
      expect(match, "could not find commit hash in README.md Pin table").to.not.equal(null);

      const readmeCommit = match![1];
      const expectedSource = `defi-wonderland/xERC20@${readmeCommit}`;
      expect(XERC20_SOURCE).to.equal(
        expectedSource,
        "XERC20_SOURCE constant drifted from README pin — re-vendor or update both together",
      );
    });
  });
});
