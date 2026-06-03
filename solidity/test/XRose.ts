import { expect } from "chai";
import { ethers } from "hardhat";
import { time } from "@nomicfoundation/hardhat-network-helpers";
import { HardhatEthersSigner } from "@nomicfoundation/hardhat-ethers/signers";
import { XRose } from "../typechain-types";

describe("XRose", () => {
  before(async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
  });
  let xRose: XRose;
  let deployer: HardhatEthersSigner;
  let bridge: HardhatEthersSigner;
  let other: HardhatEthersSigner;
  let user: HardhatEthersSigner;

  const MINT_LIMIT = ethers.parseEther("100");
  const BURN_LIMIT = ethers.parseEther("100");
  const DAY = 24 * 60 * 60;

  beforeEach(async () => {
    [deployer, bridge, other, user] = await ethers.getSigners();
    const Factory = await ethers.getContractFactory("XRose");
    xRose = (await Factory.deploy("XRose", "xROSE", deployer.address)) as unknown as XRose;
  });

  it("18 decimals", async () => {
    expect(await xRose.decimals()).to.equal(18);
  });

  it("owner() == deployer", async () => {
    expect(await xRose.owner()).to.equal(deployer.address);
  });

  it("FACTORY() == deployer", async () => {
    expect(await xRose.FACTORY()).to.equal(deployer.address);
  });

  it("lockbox unset", async () => {
    expect(await xRose.lockbox()).to.equal(ethers.ZeroAddress);
  });

  it("setLimits is owner-only", async () => {
    await expect(
      xRose.connect(other).setLimits(bridge.address, MINT_LIMIT, BURN_LIMIT),
    )
      .to.be.revertedWithCustomError(xRose, "OwnableUnauthorizedAccount")
      .withArgs(other.address);
  });

  it("setLimits records mint/burn max", async () => {
    await xRose.setLimits(bridge.address, MINT_LIMIT, BURN_LIMIT);
    expect(await xRose.mintingMaxLimitOf(bridge.address)).to.equal(MINT_LIMIT);
    expect(await xRose.burningMaxLimitOf(bridge.address)).to.equal(BURN_LIMIT);
  });

  it("limits replenish over 24 hours", async () => {
    await xRose.setLimits(bridge.address, MINT_LIMIT, BURN_LIMIT);
    const half = MINT_LIMIT / 2n;
    await xRose.connect(bridge).mint(user.address, half);
    expect(await xRose.mintingCurrentLimitOf(bridge.address)).to.be.lessThan(MINT_LIMIT);

    await time.increase(DAY);
    expect(await xRose.mintingCurrentLimitOf(bridge.address)).to.equal(MINT_LIMIT);
  });

  it("unauthorized bridge cannot mint", async () => {
    await expect(
      xRose.connect(bridge).mint(user.address, 1n),
    ).to.be.revertedWithCustomError(xRose, "IXERC20_NotHighEnoughLimits");
  });

  it("unauthorized bridge cannot burn", async () => {
    await expect(
      xRose.connect(bridge).burn(bridge.address, 1n),
    ).to.be.revertedWithCustomError(xRose, "IXERC20_NotHighEnoughLimits");
  });

  it("authorized bridge can burn its own xROSE balance", async () => {
    await xRose.setLimits(bridge.address, MINT_LIMIT, BURN_LIMIT);
    const amount = ethers.parseEther("10");
    await xRose.connect(bridge).mint(bridge.address, amount);
    expect(await xRose.balanceOf(bridge.address)).to.equal(amount);

    const burnAmount = ethers.parseEther("4");
    await xRose.connect(bridge).burn(bridge.address, burnAmount);
    expect(await xRose.balanceOf(bridge.address)).to.equal(amount - burnAmount);
  });
});
