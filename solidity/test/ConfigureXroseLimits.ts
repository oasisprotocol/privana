// Unit tests for the 05.05 helpers (`configureXroseLimitsIfNeeded` and
// `assertXroseBridgeConfig`) against a locally-deployed XRose + ROFLBridge
// pair. CreateX is not required — the helpers operate on any xROSE/ROFLBridge
// pair, so we use plain factory deploys (matches `test/ROFLBridge.ts` style).
//
// Spec: docs/plans/rose-bridge-phase0/05-createx-deploy/05-configure-xrose-limits.md

import { expect } from "chai";
import { ethers } from "hardhat";
import { HardhatEthersSigner } from "@nomicfoundation/hardhat-ethers/signers";

import {
  assertXroseBridgeConfig,
  configureXroseLimitsIfNeeded,
} from "../tasks/deploy-bridge";
import { ROFLBridge, XRose } from "../typechain-types";

describe("ConfigureXroseLimits", () => {
  before(async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
  });
  let deployer: HardhatEthersSigner;
  let customRoflSigner: HardhatEthersSigner;
  let customPauseAdmin: HardhatEthersSigner;
  let intruder: HardhatEthersSigner;
  let xrose: XRose;
  let bridge: ROFLBridge;
  let xroseAddr: string;
  let bridgeAddr: string;

  const MINT_LIMIT = ethers.parseEther("250");
  const BURN_LIMIT = ethers.parseEther("250");
  const SMALLER_LIMIT = ethers.parseEther("100");

  beforeEach(async () => {
    [deployer, customRoflSigner, customPauseAdmin, intruder] = await ethers.getSigners();

    xrose = (await (await ethers.getContractFactory("XRose")).deploy(
      "XRose",
      "xROSE",
      deployer.address,
    )) as unknown as XRose;
    bridge = (await (await ethers.getContractFactory("ROFLBridge")).deploy(
      await xrose.getAddress(),
      customRoflSigner.address,
      customPauseAdmin.address,
      deployer.address,
    )) as unknown as ROFLBridge;

    xroseAddr = await xrose.getAddress();
    bridgeAddr = await bridge.getAddress();
  });

  // Builds a fresh ethers.Contract bound to a chosen signer with the full
  // ABI fragment the helpers consume. The task body constructs the same
  // shape (see `deploy-bridge.ts` step 14), so this mirrors production.
  function xroseAs(signer: HardhatEthersSigner): ethers.Contract {
    return new ethers.Contract(
      xroseAddr,
      [
        "function setLimits(address,uint256,uint256)",
        "function mintingMaxLimitOf(address) view returns (uint256)",
        "function burningMaxLimitOf(address) view returns (uint256)",
        "function owner() view returns (address)",
        "function FACTORY() view returns (address)",
        "function setLockbox(address)",
        "function lockbox() view returns (address)",
      ],
      signer,
    );
  }

  function bridgeView(): ethers.Contract {
    return new ethers.Contract(
      bridgeAddr,
      ["function roflSigner() view returns (address)"],
      ethers.provider,
    );
  }

  describe("configureXroseLimitsIfNeeded", () => {
    it("sets limits when none are configured", async () => {
      expect(await xrose.mintingMaxLimitOf(bridgeAddr)).to.equal(0n);
      expect(await xrose.burningMaxLimitOf(bridgeAddr)).to.equal(0n);

      const result = await configureXroseLimitsIfNeeded(
        xroseAs(deployer),
        bridgeAddr,
        MINT_LIMIT,
        BURN_LIMIT,
      );

      expect(result.skipped).to.equal(false);
      expect(result.txHash)
        .to.be.a("string")
        .and.to.match(/^0x[0-9a-f]{64}$/i);

      expect(await xrose.mintingMaxLimitOf(bridgeAddr)).to.equal(MINT_LIMIT);
      expect(await xrose.burningMaxLimitOf(bridgeAddr)).to.equal(BURN_LIMIT);
    });

    it("skips when limits already match (idempotency)", async () => {
      await xrose.setLimits(bridgeAddr, MINT_LIMIT, BURN_LIMIT);

      const result = await configureXroseLimitsIfNeeded(
        xroseAs(deployer),
        bridgeAddr,
        MINT_LIMIT,
        BURN_LIMIT,
      );

      expect(result.skipped).to.equal(true);
      expect(result.txHash).to.equal(null);
    });

    it("re-broadcasts when limits differ from desired", async () => {
      await xrose.setLimits(bridgeAddr, SMALLER_LIMIT, SMALLER_LIMIT);

      const result = await configureXroseLimitsIfNeeded(
        xroseAs(deployer),
        bridgeAddr,
        MINT_LIMIT,
        BURN_LIMIT,
      );

      expect(result.skipped).to.equal(false);
      expect(await xrose.mintingMaxLimitOf(bridgeAddr)).to.equal(MINT_LIMIT);
      expect(await xrose.burningMaxLimitOf(bridgeAddr)).to.equal(BURN_LIMIT);
    });

    it("reverts when caller is not the xROSE owner", async () => {
      await expect(
        configureXroseLimitsIfNeeded(xroseAs(intruder), bridgeAddr, MINT_LIMIT, BURN_LIMIT),
      ).to.be.revertedWithCustomError(xrose, "OwnableUnauthorizedAccount");
    });
  });

  describe("assertXroseBridgeConfig", () => {
    it("passes on a correctly configured bridge", async () => {
      await xrose.setLimits(bridgeAddr, MINT_LIMIT, BURN_LIMIT);

      await assertXroseBridgeConfig(xroseAs(deployer), bridgeView(), bridgeAddr, {
        mintLimit: MINT_LIMIT,
        burnLimit: BURN_LIMIT,
        owner: deployer.address,
        factory: deployer.address,
        roflSigner: customRoflSigner.address,
      });
    });

    it("throws on mintLimit mismatch", async () => {
      await xrose.setLimits(bridgeAddr, SMALLER_LIMIT, BURN_LIMIT);

      await expect(
        assertXroseBridgeConfig(xroseAs(deployer), bridgeView(), bridgeAddr, {
          mintLimit: MINT_LIMIT,
          burnLimit: BURN_LIMIT,
          owner: deployer.address,
          factory: deployer.address,
          roflSigner: customRoflSigner.address,
        }),
      ).to.be.rejectedWith(/mintLimit mismatch/);
    });

    it("throws on burnLimit mismatch", async () => {
      await xrose.setLimits(bridgeAddr, MINT_LIMIT, SMALLER_LIMIT);

      await expect(
        assertXroseBridgeConfig(xroseAs(deployer), bridgeView(), bridgeAddr, {
          mintLimit: MINT_LIMIT,
          burnLimit: BURN_LIMIT,
          owner: deployer.address,
          factory: deployer.address,
          roflSigner: customRoflSigner.address,
        }),
      ).to.be.rejectedWith(/burnLimit mismatch/);
    });

    it("throws on roflSigner mismatch", async () => {
      await xrose.setLimits(bridgeAddr, MINT_LIMIT, BURN_LIMIT);

      await expect(
        assertXroseBridgeConfig(xroseAs(deployer), bridgeView(), bridgeAddr, {
          mintLimit: MINT_LIMIT,
          burnLimit: BURN_LIMIT,
          owner: deployer.address,
          factory: deployer.address,
          roflSigner: intruder.address, // wrong
        }),
      ).to.be.rejectedWith(/roflSigner mismatch/);
    });

    it("throws on owner mismatch", async () => {
      await xrose.setLimits(bridgeAddr, MINT_LIMIT, BURN_LIMIT);

      await expect(
        assertXroseBridgeConfig(xroseAs(deployer), bridgeView(), bridgeAddr, {
          mintLimit: MINT_LIMIT,
          burnLimit: BURN_LIMIT,
          owner: intruder.address, // wrong
          factory: deployer.address,
          roflSigner: customRoflSigner.address,
        }),
      ).to.be.rejectedWith(/owner mismatch/);
    });

    it("throws when xROSE.lockbox is set to a non-zero address", async () => {
      await xrose.setLimits(bridgeAddr, MINT_LIMIT, BURN_LIMIT);
      // FACTORY (deployer) flips the lockbox; the assertion must catch it.
      await xroseAs(deployer).setLockbox(intruder.address);

      await expect(
        assertXroseBridgeConfig(xroseAs(deployer), bridgeView(), bridgeAddr, {
          mintLimit: MINT_LIMIT,
          burnLimit: BURN_LIMIT,
          owner: deployer.address,
          factory: deployer.address,
          roflSigner: customRoflSigner.address,
        }),
      ).to.be.rejectedWith(/lockbox mismatch/);
    });

    it("aggregates multiple mismatches into one error", async () => {
      await xrose.setLimits(bridgeAddr, SMALLER_LIMIT, BURN_LIMIT);

      await expect(
        assertXroseBridgeConfig(xroseAs(deployer), bridgeView(), bridgeAddr, {
          mintLimit: MINT_LIMIT, // wrong (set to SMALLER_LIMIT above)
          burnLimit: BURN_LIMIT,
          owner: intruder.address, // wrong
          factory: deployer.address,
          roflSigner: customRoflSigner.address,
        }),
      ).to.be.rejectedWith(/mintLimit mismatch.*owner mismatch|owner mismatch.*mintLimit mismatch/);
    });
  });
});
