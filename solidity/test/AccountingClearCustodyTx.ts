import { expect } from "chai";
import { ethers, upgrades } from "hardhat";
import type { MockAccounting } from "../typechain-types";
import { getLinkedAccountingFactory } from "./util/links";

const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21);

// ClearAction enum (Accounting.sol): values match storage encoding.
const ClearAction = {
  Requeue: 0,
  Abandon: 1,
  MarkSuccessWithHash: 2,
  BurnNonce: 3,
} as const;

const CHAIN_ID = 84532n; // Base Sepolia, a representative destination chain.
const NONCE_COUNTER = 5n; // mockSetNonce → nonces 0..4 clearable, 5 out of range.
const VOUCH = ethers.id("vouched-dest-tx-hash"); // a non-zero tx hash.

function appliedHash(action: number, vouchedTxHash: string): string {
  return ethers.keccak256(
    ethers.AbiCoder.defaultAbiCoder().encode(
      ["uint8", "bytes32"],
      [action, vouchedTxHash],
    ),
  );
}

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

describe("clearCustodyTx", () => {
  async function deploy(): Promise<MockAccounting> {
    const [owner] = await ethers.getSigners();
    const accounting = await deployAccountingProxy(owner.address);
    await accounting.mockSetNonce(CHAIN_ID, NONCE_COUNTER);
    return accounting;
  }

  it("rejects a non-owner caller with OwnableUnauthorizedAccount", async () => {
    const [, other] = await ethers.getSigners();
    const accounting = await deploy();

    await expect(
      accounting
        .connect(other)
        .clearCustodyTx(CHAIN_ID, 1n, ClearAction.Requeue, ethers.ZeroHash),
    ).to.be.revertedWithCustomError(accounting, "OwnableUnauthorizedAccount");
  });

  it("reverts CustodyTxClearNonceOutOfRange when nonce == counter", async () => {
    const accounting = await deploy();

    await expect(
      accounting.clearCustodyTx(
        CHAIN_ID,
        NONCE_COUNTER,
        ClearAction.Requeue,
        ethers.ZeroHash,
      ),
    )
      .to.be.revertedWithCustomError(accounting, "CustodyTxClearNonceOutOfRange")
      .withArgs(CHAIN_ID, NONCE_COUNTER);
  });

  it("reverts CustodyTxClearNonceOutOfRange when nonce > counter", async () => {
    const accounting = await deploy();

    await expect(
      accounting.clearCustodyTx(
        CHAIN_ID,
        NONCE_COUNTER + 1n,
        ClearAction.Requeue,
        ethers.ZeroHash,
      ),
    )
      .to.be.revertedWithCustomError(accounting, "CustodyTxClearNonceOutOfRange")
      .withArgs(CHAIN_ID, NONCE_COUNTER + 1n);
  });

  it("succeeds for the highest in-range nonce (counter - 1)", async () => {
    const accounting = await deploy();

    await expect(
      accounting.clearCustodyTx(
        CHAIN_ID,
        NONCE_COUNTER - 1n,
        ClearAction.Requeue,
        ethers.ZeroHash,
      ),
    ).to.emit(accounting, "CustodyTxCleared");
  });

  it("reverts CustodyTxClearMissingVouch for MarkSuccessWithHash with zero vouch", async () => {
    const accounting = await deploy();

    await expect(
      accounting.clearCustodyTx(
        CHAIN_ID,
        1n,
        ClearAction.MarkSuccessWithHash,
        ethers.ZeroHash,
      ),
    ).to.be.revertedWithCustomError(accounting, "CustodyTxClearMissingVouch");
  });

  it("reverts CustodyTxClearUnexpectedVouch for non-MarkSuccess actions with a non-zero vouch", async () => {
    const accounting = await deploy();

    for (const action of [
      ClearAction.Requeue,
      ClearAction.Abandon,
      ClearAction.BurnNonce,
    ]) {
      await expect(
        accounting.clearCustodyTx(CHAIN_ID, 1n, action, VOUCH),
      ).to.be.revertedWithCustomError(
        accounting,
        "CustodyTxClearUnexpectedVouch",
      );
    }
  });

  it("Abandon with zero vouch emits CustodyTxCleared and writes keccak(action, zero)", async () => {
    const accounting = await deploy();

    await expect(
      accounting.clearCustodyTx(
        CHAIN_ID,
        1n,
        ClearAction.Abandon,
        ethers.ZeroHash,
      ),
    )
      .to.emit(accounting, "CustodyTxCleared")
      .withArgs(CHAIN_ID, 1n, ClearAction.Abandon, ethers.ZeroHash);

    expect(await accounting.clearAppliedHash(CHAIN_ID, 1n)).to.equal(
      appliedHash(ClearAction.Abandon, ethers.ZeroHash),
    );
  });

  it("MarkSuccessWithHash with a real vouch writes keccak(action, vouch)", async () => {
    const accounting = await deploy();

    await expect(
      accounting.clearCustodyTx(
        CHAIN_ID,
        2n,
        ClearAction.MarkSuccessWithHash,
        VOUCH,
      ),
    )
      .to.emit(accounting, "CustodyTxCleared")
      .withArgs(CHAIN_ID, 2n, ClearAction.MarkSuccessWithHash, VOUCH);

    expect(await accounting.clearAppliedHash(CHAIN_ID, 2n)).to.equal(
      appliedHash(ClearAction.MarkSuccessWithHash, VOUCH),
    );
  });

  it("first-clear-wins: a second clear reverts CustodyTxAlreadyCleared with the stored hash", async () => {
    const accounting = await deploy();

    await accounting.clearCustodyTx(
      CHAIN_ID,
      2n,
      ClearAction.Requeue,
      ethers.ZeroHash,
    );
    const stored = appliedHash(ClearAction.Requeue, ethers.ZeroHash);
    expect(await accounting.clearAppliedHash(CHAIN_ID, 2n)).to.equal(stored);

    // A second clear — even with a different action — must not overwrite.
    await expect(
      accounting.clearCustodyTx(
        CHAIN_ID,
        2n,
        ClearAction.Abandon,
        ethers.ZeroHash,
      ),
    )
      .to.be.revertedWithCustomError(accounting, "CustodyTxAlreadyCleared")
      .withArgs(CHAIN_ID, 2n, stored);
  });
});

describe("signNonceBurn", () => {
  async function deploy(): Promise<MockAccounting> {
    const [owner] = await ethers.getSigners();
    const accounting = await deployAccountingProxy(owner.address);
    await accounting.mockSetNonce(CHAIN_ID, NONCE_COUNTER);
    return accounting;
  }

  it("reverts RoflSignerNotSet when the ROFL signer is unset", async () => {
    const accounting = await deploy();

    await expect(
      accounting.signNonceBurn.staticCall(CHAIN_ID, 1n),
    ).to.be.revertedWithCustomError(accounting, "RoflSignerNotSet");
  });

  it("reverts NotAuthorizedROFL when the caller is not the ROFL signer", async () => {
    const [owner, other] = await ethers.getSigners();
    const accounting = await deploy();
    await accounting.mockSetRoflSignerAddress(owner.address);

    await expect(
      accounting.connect(other).signNonceBurn.staticCall(CHAIN_ID, 1n),
    ).to.be.revertedWithCustomError(accounting, "NotAuthorizedROFL");
  });

  it("reverts CustodyTxClearNotBurnAuthorized when the slot is uncleared", async () => {
    const [owner] = await ethers.getSigners();
    const accounting = await deploy();
    await accounting.mockSetRoflSignerAddress(owner.address);

    await expect(accounting.signNonceBurn.staticCall(CHAIN_ID, 1n))
      .to.be.revertedWithCustomError(
        accounting,
        "CustodyTxClearNotBurnAuthorized",
      )
      .withArgs(CHAIN_ID, 1n);
  });

  it("reverts CustodyTxClearNotBurnAuthorized when the slot was cleared with a non-BurnNonce action", async () => {
    const [owner] = await ethers.getSigners();
    const accounting = await deploy();
    await accounting.clearCustodyTx(
      CHAIN_ID,
      1n,
      ClearAction.Abandon,
      ethers.ZeroHash,
    );
    await accounting.mockSetRoflSignerAddress(owner.address);

    await expect(accounting.signNonceBurn.staticCall(CHAIN_ID, 1n))
      .to.be.revertedWithCustomError(
        accounting,
        "CustodyTxClearNotBurnAuthorized",
      )
      .withArgs(CHAIN_ID, 1n);
  });

  it("reaches EIP155Signer.sign and reverts DER_Split_Error on Hardhat once the slot is BurnNonce-cleared", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();

    const [owner] = await ethers.getSigners();
    const accounting = await deploy();
    await accounting.clearCustodyTx(
      CHAIN_ID,
      3n,
      ClearAction.BurnNonce,
      ethers.ZeroHash,
    );
    await accounting.mockSetRoflSignerAddress(owner.address);
    // A non-zero gas price is required to pass the GasPriceNotSet guard and
    // reach the signer; otherwise the body never reaches the precompile.
    await accounting.setGasPrice(CHAIN_ID, 1_000_000_000n);

    // The auth gate (onlyROFLQuery) and the BurnNonce-clear gate both pass, so
    // the body reaches the Sapphire SIGN_DIGEST precompile, absent on Hardhat.
    await expect(
      accounting.signNonceBurn.staticCall(CHAIN_ID, 3n),
    ).to.be.revertedWithCustomError(accounting, "DER_Split_Error");
  });

  it("reverts GasPriceNotSet when the slot is BurnNonce-cleared but no gas price is set", async () => {
    const [owner] = await ethers.getSigners();
    const accounting = await deploy();
    await accounting.clearCustodyTx(
      CHAIN_ID,
      3n,
      ClearAction.BurnNonce,
      ethers.ZeroHash,
    );
    await accounting.mockSetRoflSignerAddress(owner.address);

    // Auth and the BurnNonce-clear gate pass, but gasPrices[CHAIN_ID] == 0, so
    // the guard reverts before the precompile — no network gate needed.
    await expect(accounting.signNonceBurn.staticCall(CHAIN_ID, 3n))
      .to.be.revertedWithCustomError(accounting, "GasPriceNotSet")
      .withArgs(CHAIN_ID);
  });
});
