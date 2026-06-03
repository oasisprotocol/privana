import { expect } from "chai";
import { Contract, ZeroAddress } from "ethers";
import { ethers, upgrades } from "hardhat";
import type { MockAccounting, MockBridgeModule } from "../typechain-types";
import {
  deployBridgeModule,
  getCombinedAccountingAt,
  getLinkedAccountingFactory,
} from "./util/links";

const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21);
const BASE_CHAIN_ID = 84532n;
const ROFL_BRIDGE = "0x000000000000000000000000000000000000c0fe";
const ROSE_TOKEN_ID =
  "0xca91975d6c6810eb4077546d4fbdb49fa231f351cddfc915862f7c0dad81a7aa";
const TokenType = { NativeEVM: 0, ERC20: 1, BridgeAsset: 2 } as const;

async function deployAccountingProxy(
  deployerAddress: string,
): Promise<MockAccounting> {
  const MockSiweAuthFactory = await ethers.getContractFactory("MockSiweAuth");
  const mockSiweAuth = await MockSiweAuthFactory.deploy("test");
  await mockSiweAuth.waitForDeployment();

  const AccountingFactory = await getLinkedAccountingFactory("MockAccounting");
  const accounting = (await upgrades.deployProxy(
    AccountingFactory,
    [MOCK_ROFL_APP_ID, deployerAddress],
    {
      kind: "uups",
      initializer: "initialize",
      constructorArgs: [await mockSiweAuth.getAddress()],
      unsafeAllow: ["external-library-linking"],
    },
  )) as unknown as MockAccounting;
  await accounting.waitForDeployment();
  return accounting;
}

async function deployWiredCombined(ownerAddress: string): Promise<{
  accounting: MockAccounting;
  combined: Contract;
  proxyAddr: string;
  moduleContract: MockBridgeModule;
  moduleAddr: string;
}> {
  const accounting = await deployAccountingProxy(ownerAddress);
  const moduleContract =
    (await deployBridgeModule()) as unknown as MockBridgeModule;
  const moduleAddr = await moduleContract.getAddress();
  await accounting.setBridgeModule(moduleAddr);
  const proxyAddr = await accounting.getAddress();
  const [signer] = await ethers.getSigners();
  const combined = await getCombinedAccountingAt(proxyAddr, signer);
  return { accounting, combined, proxyAddr, moduleContract, moduleAddr };
}

async function configureRose(combined: Contract): Promise<void> {
  const roseData = await (combined as any).encodeBridgeAssetTokenData("ROSE");
  await (combined as any).setTokenInfo({
    tokenType: TokenType.BridgeAsset,
    data: roseData,
  });
  await (combined as any).setRoflBridge(BASE_CHAIN_ID, ROFL_BRIDGE);
}

describe("BridgeModule.getBridgeBurnRequest", () => {
  const depositId = ethers.id("get-bridge-burn-request");
  const amount = 1_234n;

  it("returns the recorded reservation through the Accounting proxy fallback", async () => {
    const [owner] = await ethers.getSigners();
    const { accounting, combined } = await deployWiredCombined(owner.address);
    await configureRose(combined);

    const noncesBefore = await accounting.nonces(BASE_CHAIN_ID);
    await (combined as any).reserveBridgeBurn(
      depositId,
      BASE_CHAIN_ID,
      ROFL_BRIDGE,
      amount,
    );

    const [chainId, bridge, recordedAmount, nonce, exists] = await (
      combined as any
    ).getBridgeBurnRequest(depositId);

    expect(chainId).to.equal(BASE_CHAIN_ID);
    expect(bridge).to.equal(ethers.getAddress(ROFL_BRIDGE));
    expect(recordedAmount).to.equal(amount);
    expect(nonce).to.equal(noncesBefore);
    expect(exists).to.equal(true);
  });

  it("returns identical data when called on the BridgeModule directly", async () => {
    // BridgeModule is a delegated runtime; called directly it reads its OWN
    // (empty) bridgeBurnRequests slot, so the fields are zero. This test pins
    // that behaviour so the function's "via the proxy" semantics are explicit:
    // off-chain callers must always reach it through the Accounting proxy.
    const [owner] = await ethers.getSigners();
    const moduleContract =
      (await deployBridgeModule()) as unknown as MockBridgeModule;
    const [chainId, bridge, recordedAmount, nonce, exists] =
      await moduleContract.getBridgeBurnRequest(depositId);
    expect(chainId).to.equal(0n);
    expect(bridge).to.equal(ZeroAddress);
    expect(recordedAmount).to.equal(0n);
    expect(nonce).to.equal(0n);
    expect(exists).to.equal(false);
  });

  it("returns (0, address(0), 0, 0, false) for an unknown depositId via the proxy", async () => {
    const [owner] = await ethers.getSigners();
    const { combined } = await deployWiredCombined(owner.address);
    await configureRose(combined);

    const unknown = ethers.id("never-reserved");
    const [chainId, bridge, recordedAmount, nonce, exists] = await (
      combined as any
    ).getBridgeBurnRequest(unknown);

    expect(chainId).to.equal(0n);
    expect(bridge).to.equal(ZeroAddress);
    expect(recordedAmount).to.equal(0n);
    expect(nonce).to.equal(0n);
    expect(exists).to.equal(false);
  });

  it("reverts BridgeModuleNotSet when bridgeModule slot is unset", async () => {
    // Selector must be in the fallback allowlist for this to even reach the
    // module-slot check. If the selector were missing the call would revert
    // UnknownSelector(getBridgeBurnRequest.selector) instead.
    const [owner] = await ethers.getSigners();
    const accounting = await deployAccountingProxy(owner.address);
    const proxyAddr = await accounting.getAddress();
    const [signer] = await ethers.getSigners();
    const combined = await getCombinedAccountingAt(proxyAddr, signer);

    await expect(
      (combined as any).getBridgeBurnRequest(depositId),
    ).to.be.revertedWithCustomError(accounting, "BridgeModuleNotSet");
  });
});
