import { expect } from "chai";
import { ZeroAddress } from "ethers";
import { ethers, upgrades } from "hardhat";
import type { MockAccounting } from "../typechain-types";

const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21);
const BASE_CHAIN_ID = 84532n;
const ROFL_BRIDGE = "0x000000000000000000000000000000000000c0fe";
const TokenType = { NativeEVM: 0, ERC20: 1, BridgeAsset: 2 } as const;

async function deployAccountingProxy(
  deployerAddress: string,
): Promise<MockAccounting> {
  const MockSiweAuthFactory = await ethers.getContractFactory("MockSiweAuth");
  const mockSiweAuth = await MockSiweAuthFactory.deploy("test");
  await mockSiweAuth.waitForDeployment();

  const AccountingFactory = await ethers.getContractFactory("MockAccounting");
  const accounting = (await upgrades.deployProxy(
    AccountingFactory,
    [MOCK_ROFL_APP_ID, deployerAddress],
    {
      kind: "uups",
      initializer: "initialize",
      constructorArgs: [await mockSiweAuth.getAddress()],
      unsafeAllow: [],
    },
  )) as unknown as MockAccounting;
  await accounting.waitForDeployment();
  return accounting;
}

async function configureRose(accounting: MockAccounting): Promise<void> {
  const roseData = await (accounting as any).encodeBridgeAssetTokenData("ROSE");
  await (accounting as any).setTokenInfo({
    tokenType: TokenType.BridgeAsset,
    data: roseData,
  });
  await accounting.setRoflBridge(BASE_CHAIN_ID, ROFL_BRIDGE);
}

describe("Accounting.getBridgeBurnRequest", () => {
  const depositId = ethers.id("get-bridge-burn-request");
  const amount = 1_234n;

  it("returns the recorded reservation", async () => {
    const [owner] = await ethers.getSigners();
    const accounting = await deployAccountingProxy(owner.address);
    await configureRose(accounting);

    const noncesBefore = await accounting.nonces(BASE_CHAIN_ID);
    await accounting.reserveBridgeBurn(
      depositId,
      BASE_CHAIN_ID,
      ROFL_BRIDGE,
      amount,
    );

    const [chainId, bridge, recordedAmount, nonce, exists] =
      await accounting.getBridgeBurnRequest(depositId);

    expect(chainId).to.equal(BASE_CHAIN_ID);
    expect(bridge).to.equal(ethers.getAddress(ROFL_BRIDGE));
    expect(recordedAmount).to.equal(amount);
    expect(nonce).to.equal(noncesBefore);
    expect(exists).to.equal(true);
  });

  it("returns (0, address(0), 0, 0, false) for an unknown depositId", async () => {
    const [owner] = await ethers.getSigners();
    const accounting = await deployAccountingProxy(owner.address);
    await configureRose(accounting);

    const unknown = ethers.id("never-reserved");
    const [chainId, bridge, recordedAmount, nonce, exists] =
      await accounting.getBridgeBurnRequest(unknown);

    expect(chainId).to.equal(0n);
    expect(bridge).to.equal(ZeroAddress);
    expect(recordedAmount).to.equal(0n);
    expect(nonce).to.equal(0n);
    expect(exists).to.equal(false);
  });
});
