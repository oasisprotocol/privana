// Unit tests for the rose-token surface in `tasks/bridge.ts`
// (`addRoseTokenIfNeeded`, `assertRoseTokenRegistered`).
// Route registration lives in the ROFL TEE reconciler and is no longer
// driven from a hardhat task — see `src/services/bridge_route_reconciler.py`.

import { expect } from "chai";
import { ethers, upgrades } from "hardhat";
import { Contract } from "ethers";
import { HardhatEthersSigner } from "@nomicfoundation/hardhat-ethers/signers";

import {
  ACCOUNTING_TOKEN_ABI,
  ROSE_TOKEN_ID,
  TOKEN_TYPE_BRIDGE_ASSET,
  addRoseTokenIfNeeded,
  assertRoseTokenRegistered,
} from "../tasks/bridge";
import {
  deployBridgeModule,
  getLinkedAccountingFactoryAndLib,
} from "./util/links";

const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21);

describe("BridgeTasks", () => {
  let owner: HardhatEthersSigner;
  let accountingAsOwner: Contract;

  beforeEach(async () => {
    [owner] = await ethers.getSigners();

    const MockSiweAuthFactory = await ethers.getContractFactory("MockSiweAuth");
    const mockSiweAuth = await MockSiweAuthFactory.deploy("test");
    await mockSiweAuth.waitForDeployment();

    const { factory: AccountingFactory } =
      await getLinkedAccountingFactoryAndLib("MockAccounting");
    const accounting = await upgrades.deployProxy(
      AccountingFactory,
      [MOCK_ROFL_APP_ID, owner.address],
      {
        kind: "uups",
        initializer: "initialize",
        constructorArgs: [await mockSiweAuth.getAddress()],
        unsafeAllow: ["external-library-linking"],
      },
    );
    await accounting.waitForDeployment();

    const bridgeModule = await deployBridgeModule();
    await (
      accounting as unknown as { setBridgeModule: (a: string) => Promise<any> }
    ).setBridgeModule(await bridgeModule.getAddress());

    const proxyAddr = await accounting.getAddress();
    accountingAsOwner = new ethers.Contract(
      proxyAddr,
      ACCOUNTING_TOKEN_ABI,
      owner,
    );
  });

  describe("addRoseTokenIfNeeded", () => {
    it("registers ROSE when tokens(ROSE_TOKEN_ID).tokenType is zero", async () => {
      const before = await accountingAsOwner.tokens(ROSE_TOKEN_ID);
      expect(Number(before.tokenType)).to.equal(0);

      const result = await addRoseTokenIfNeeded(accountingAsOwner);

      expect(result.skipped).to.equal(false);
      expect(result.txHash)
        .to.be.a("string")
        .and.to.match(/^0x[0-9a-f]{64}$/i);

      const after = await accountingAsOwner.tokens(ROSE_TOKEN_ID);
      expect(Number(after.tokenType)).to.equal(TOKEN_TYPE_BRIDGE_ASSET);
    });

    it("skips when ROSE already registered as BridgeAsset", async () => {
      const data = await accountingAsOwner.encodeBridgeAssetTokenData("ROSE");
      await accountingAsOwner.setTokenInfo({
        tokenType: TOKEN_TYPE_BRIDGE_ASSET,
        data,
      });

      const result = await addRoseTokenIfNeeded(accountingAsOwner);

      expect(result.skipped).to.equal(true);
      expect(result.txHash).to.equal(null);
    });
  });

  describe("assertRoseTokenRegistered", () => {
    it("passes after ROSE is registered as BridgeAsset", async () => {
      const data = await accountingAsOwner.encodeBridgeAssetTokenData("ROSE");
      await accountingAsOwner.setTokenInfo({
        tokenType: TOKEN_TYPE_BRIDGE_ASSET,
        data,
      });

      await assertRoseTokenRegistered(accountingAsOwner);
    });

    it("throws when tokens(ROSE_TOKEN_ID).tokenType is not BridgeAsset", async () => {
      await expect(
        assertRoseTokenRegistered(accountingAsOwner),
      ).to.be.rejectedWith(/not registered as BridgeAsset/);
    });
  });
});
