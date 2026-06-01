import { expect } from "chai";
import { ethers } from "hardhat";
import { HardhatEthersSigner } from "@nomicfoundation/hardhat-ethers/signers";
import { ROFLBridge, XRose } from "../typechain-types";

describe("ROFLBridge", () => {
  before(async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
  });
  let xRose: XRose;
  let bridge: ROFLBridge;
  let deployer: HardhatEthersSigner;
  let rofl: HardhatEthersSigner;
  let admin: HardhatEthersSigner;
  let user: HardhatEthersSigner;
  let other: HardhatEthersSigner;

  const MINT_LIMIT = ethers.parseEther("100");
  const BURN_LIMIT = ethers.parseEther("100");
  const AMOUNT = ethers.parseEther("10");
  const WITHDRAWAL_ID = ethers.keccak256(ethers.toUtf8Bytes("withdrawal-1"));
  const DEPOSIT_ID = ethers.keccak256(ethers.toUtf8Bytes("deposit-1"));

  beforeEach(async () => {
    [deployer, rofl, admin, user, other] = await ethers.getSigners();

    const XRoseFactory = await ethers.getContractFactory("XRose");
    xRose = (await XRoseFactory.deploy("XRose", "xROSE", deployer.address)) as unknown as XRose;

    const BridgeFactory = await ethers.getContractFactory("ROFLBridge");
    bridge = (await BridgeFactory.deploy(
      await xRose.getAddress(),
      rofl.address,
      admin.address,
      deployer.address,
    )) as unknown as ROFLBridge;

    await xRose.setLimits(await bridge.getAddress(), MINT_LIMIT, BURN_LIMIT);
  });

  it("constructor wires xrose/roflSigner/pauseAdmin/owner", async () => {
    expect(await bridge.xrose()).to.equal(await xRose.getAddress());
    expect(await bridge.roflSigner()).to.equal(rofl.address);
    expect(await bridge.pauseAdmin()).to.equal(admin.address);
    expect(await bridge.owner()).to.equal(deployer.address);
  });

  it("constructor reverts ZeroAddress when xrose is zero", async () => {
    const BridgeFactory = await ethers.getContractFactory("ROFLBridge");
    await expect(
      BridgeFactory.deploy(ethers.ZeroAddress, rofl.address, admin.address, deployer.address),
    ).to.be.revertedWithCustomError(BridgeFactory, "ZeroAddress");
  });

  it("constructor reverts ZeroAddress when roflSigner is zero", async () => {
    const BridgeFactory = await ethers.getContractFactory("ROFLBridge");
    await expect(
      BridgeFactory.deploy(
        await xRose.getAddress(),
        ethers.ZeroAddress,
        admin.address,
        deployer.address,
      ),
    ).to.be.revertedWithCustomError(BridgeFactory, "ZeroAddress");
  });

  it("constructor reverts ZeroAddress when pauseAdmin is zero", async () => {
    const BridgeFactory = await ethers.getContractFactory("ROFLBridge");
    await expect(
      BridgeFactory.deploy(
        await xRose.getAddress(),
        rofl.address,
        ethers.ZeroAddress,
        deployer.address,
      ),
    ).to.be.revertedWithCustomError(BridgeFactory, "ZeroAddress");
  });

  it("constructor reverts when owner is zero (Ownable)", async () => {
    const BridgeFactory = await ethers.getContractFactory("ROFLBridge");
    await expect(
      BridgeFactory.deploy(
        await xRose.getAddress(),
        rofl.address,
        admin.address,
        ethers.ZeroAddress,
      ),
    ).to.be.revertedWithCustomError(BridgeFactory, "OwnableInvalidOwner");
  });

  describe("rotation", () => {
    it("setRoflSigner rotates the authority and emits RoflSignerUpdated", async () => {
      await expect(bridge.connect(deployer).setRoflSigner(other.address))
        .to.emit(bridge, "RoflSignerUpdated")
        .withArgs(rofl.address, other.address);
      expect(await bridge.roflSigner()).to.equal(other.address);

      // Old key is no longer authorized; new key is.
      await expect(
        bridge.connect(rofl).mint(user.address, AMOUNT, WITHDRAWAL_ID),
      ).to.be.revertedWithCustomError(bridge, "Unauthorized");
      await expect(bridge.connect(other).mint(user.address, AMOUNT, WITHDRAWAL_ID))
        .to.emit(bridge, "Minted")
        .withArgs(WITHDRAWAL_ID, user.address, AMOUNT);
    });

    it("setRoflSigner is owner-only", async () => {
      await expect(
        bridge.connect(other).setRoflSigner(other.address),
      ).to.be.revertedWithCustomError(bridge, "OwnableUnauthorizedAccount");
    });

    it("setRoflSigner rejects the zero address", async () => {
      await expect(
        bridge.connect(deployer).setRoflSigner(ethers.ZeroAddress),
      ).to.be.revertedWithCustomError(bridge, "ZeroAddress");
    });

    it("setPauseAdmin rotates the authority and emits PauseAdminUpdated", async () => {
      await expect(bridge.connect(deployer).setPauseAdmin(other.address))
        .to.emit(bridge, "PauseAdminUpdated")
        .withArgs(admin.address, other.address);
      expect(await bridge.pauseAdmin()).to.equal(other.address);

      // Old admin loses pause authority; new admin gains it.
      await expect(bridge.connect(admin).pause()).to.be.revertedWithCustomError(
        bridge,
        "Unauthorized",
      );
      await expect(bridge.connect(other).pause()).to.emit(bridge, "Paused");
    });

    it("setPauseAdmin is owner-only", async () => {
      await expect(
        bridge.connect(other).setPauseAdmin(other.address),
      ).to.be.revertedWithCustomError(bridge, "OwnableUnauthorizedAccount");
    });

    it("setPauseAdmin rejects the zero address", async () => {
      await expect(
        bridge.connect(deployer).setPauseAdmin(ethers.ZeroAddress),
      ).to.be.revertedWithCustomError(bridge, "ZeroAddress");
    });
  });

  it("paused() defaults to false", async () => {
    expect(await bridge.paused()).to.equal(false);
  });

  it("mint reverts with Unauthorized for non-ROFL caller", async () => {
    await expect(
      bridge.connect(other).mint(user.address, AMOUNT, WITHDRAWAL_ID),
    ).to.be.revertedWithCustomError(bridge, "Unauthorized");
  });

  it("burn reverts with Unauthorized for non-ROFL caller", async () => {
    await expect(
      bridge.connect(other).burn(AMOUNT, DEPOSIT_ID),
    ).to.be.revertedWithCustomError(bridge, "Unauthorized");
  });

  it("mint mints xROSE, marks id, emits Minted exactly once", async () => {
    await expect(bridge.connect(rofl).mint(user.address, AMOUNT, WITHDRAWAL_ID))
      .to.emit(bridge, "Minted")
      .withArgs(WITHDRAWAL_ID, user.address, AMOUNT);

    expect(await xRose.balanceOf(user.address)).to.equal(AMOUNT);
    expect(await bridge.mintedWithdrawalIds(WITHDRAWAL_ID)).to.equal(true);
  });

  it("mint with amount=0 reverts ZeroAmount and does not consume the id", async () => {
    await expect(
      bridge.connect(rofl).mint(user.address, 0n, WITHDRAWAL_ID),
    ).to.be.revertedWithCustomError(bridge, "ZeroAmount");
    expect(await bridge.mintedWithdrawalIds(WITHDRAWAL_ID)).to.equal(false);
  });

  it("mint with bytes32(0) withdrawalId reverts ZeroId and does not consume the slot", async () => {
    await expect(
      bridge.connect(rofl).mint(user.address, AMOUNT, ethers.ZeroHash),
    ).to.be.revertedWithCustomError(bridge, "ZeroId");
    expect(await bridge.mintedWithdrawalIds(ethers.ZeroHash)).to.equal(false);
  });

  it("burn burns bridge's own xROSE, marks id, emits Burned exactly once", async () => {
    const bridgeAddr = await bridge.getAddress();
    await bridge.connect(rofl).mint(bridgeAddr, AMOUNT, WITHDRAWAL_ID);
    expect(await xRose.balanceOf(bridgeAddr)).to.equal(AMOUNT);

    const burnAmount = ethers.parseEther("4");
    await expect(bridge.connect(rofl).burn(burnAmount, DEPOSIT_ID))
      .to.emit(bridge, "Burned")
      .withArgs(DEPOSIT_ID, burnAmount);

    expect(await xRose.balanceOf(bridgeAddr)).to.equal(AMOUNT - burnAmount);
    expect(await bridge.burnedDepositIds(DEPOSIT_ID)).to.equal(true);
  });

  it("burn with amount=0 reverts ZeroAmount and does not consume the id", async () => {
    await expect(
      bridge.connect(rofl).burn(0n, DEPOSIT_ID),
    ).to.be.revertedWithCustomError(bridge, "ZeroAmount");
    expect(await bridge.burnedDepositIds(DEPOSIT_ID)).to.equal(false);
  });

  it("burn with bytes32(0) depositId reverts ZeroId and does not consume the slot", async () => {
    const bridgeAddr = await bridge.getAddress();
    await bridge.connect(rofl).mint(bridgeAddr, AMOUNT, WITHDRAWAL_ID);
    await expect(
      bridge.connect(rofl).burn(AMOUNT, ethers.ZeroHash),
    ).to.be.revertedWithCustomError(bridge, "ZeroId");
    expect(await bridge.burnedDepositIds(ethers.ZeroHash)).to.equal(false);
  });

  it("burn with insufficient sweep balance reverts InsufficientSweep", async () => {
    const bridgeAddr = await bridge.getAddress();
    expect(await xRose.balanceOf(bridgeAddr)).to.equal(0n);
    await expect(bridge.connect(rofl).burn(AMOUNT, DEPOSIT_ID))
      .to.be.revertedWithCustomError(bridge, "InsufficientSweep")
      .withArgs(0n, AMOUNT);
  });

  it("duplicate withdrawalId reverts with AlreadyProcessed", async () => {
    await bridge.connect(rofl).mint(user.address, AMOUNT, WITHDRAWAL_ID);
    await expect(
      bridge.connect(rofl).mint(user.address, AMOUNT, WITHDRAWAL_ID),
    ).to.be.revertedWithCustomError(bridge, "AlreadyProcessed");
  });

  it("duplicate depositId reverts with AlreadyProcessed", async () => {
    const bridgeAddr = await bridge.getAddress();
    await bridge.connect(rofl).mint(bridgeAddr, AMOUNT, WITHDRAWAL_ID);
    const burnAmount = ethers.parseEther("1");
    await bridge.connect(rofl).burn(burnAmount, DEPOSIT_ID);
    await expect(
      bridge.connect(rofl).burn(burnAmount, DEPOSIT_ID),
    ).to.be.revertedWithCustomError(bridge, "AlreadyProcessed");
  });

  it("pause is admin-only", async () => {
    await expect(bridge.connect(other).pause()).to.be.revertedWithCustomError(
      bridge,
      "Unauthorized",
    );
  });

  it("pause blocks mint", async () => {
    await bridge.connect(admin).pause();
    expect(await bridge.paused()).to.equal(true);
    await expect(
      bridge.connect(rofl).mint(user.address, AMOUNT, WITHDRAWAL_ID),
    ).to.be.revertedWithCustomError(bridge, "EnforcedPause");
  });

  it("pause blocks burn", async () => {
    const bridgeAddr = await bridge.getAddress();
    await bridge.connect(rofl).mint(bridgeAddr, AMOUNT, WITHDRAWAL_ID);
    await bridge.connect(admin).pause();
    await expect(
      bridge.connect(rofl).burn(AMOUNT, DEPOSIT_ID),
    ).to.be.revertedWithCustomError(bridge, "EnforcedPause");
  });

  it("pause when already paused reverts EnforcedPause", async () => {
    await bridge.connect(admin).pause();
    await expect(bridge.connect(admin).pause()).to.be.revertedWithCustomError(
      bridge,
      "EnforcedPause",
    );
  });

  it("unpause when not paused reverts ExpectedPause", async () => {
    await expect(bridge.connect(admin).unpause()).to.be.revertedWithCustomError(
      bridge,
      "ExpectedPause",
    );
  });

  it("pause→unpause→pause emits one event per real transition", async () => {
    await expect(bridge.connect(admin).pause()).to.emit(bridge, "Paused").withArgs(admin.address);
    await expect(bridge.connect(admin).unpause()).to.emit(bridge, "Unpaused").withArgs(admin.address);
    await expect(bridge.connect(admin).pause()).to.emit(bridge, "Paused").withArgs(admin.address);
    expect(await bridge.paused()).to.equal(true);
  });

  it("unpause re-enables mint", async () => {
    await bridge.connect(admin).pause();
    await bridge.connect(admin).unpause();
    expect(await bridge.paused()).to.equal(false);
    await expect(bridge.connect(rofl).mint(user.address, AMOUNT, WITHDRAWAL_ID))
      .to.emit(bridge, "Minted")
      .withArgs(WITHDRAWAL_ID, user.address, AMOUNT);
  });

  it("unpause is admin-only", async () => {
    await bridge.connect(admin).pause();
    await expect(bridge.connect(other).unpause()).to.be.revertedWithCustomError(
      bridge,
      "Unauthorized",
    );
  });
});
