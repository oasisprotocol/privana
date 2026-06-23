import { expect } from "chai";
import { ethers, config, upgrades } from "hardhat";
import { Contract, Wallet, ZeroAddress } from "ethers";
import { HardhatNetworkHDAccountsConfig } from "hardhat/types";
import { MockAccounting } from "../typechain-types";

const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21);

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

// ─── bridge security & roundtrip ─────────────────────────────────────────
//
// The bridge selectors live on `Accounting`. Where a primitive requires the
// Sapphire `EIP155Signer.sign` precompile (the signed-tx production in
// `resolveBridgeWithdrawal` / `generateSweepERC20TransferToBridge`), the
// invariant is pinned indirectly — see the off-chain derivation case.

// Resolved at test-suite start from the live network. The contract treats
// `block.chainid` as the Sapphire-native release chain id; hardhat returns
// 31337, sapphire-localnet 23293, sapphire-testnet 23295. Hardhat cannot use
// a Sapphire chainId because `@oasisprotocol/sapphire-hardhat` auto-activates
// encrypted RPC on those ids and breaks the in-memory chain.
let SAPPHIRE_CHAIN_ID!: bigint;
const BASE_CHAIN_ID = 84532n;
const ROFL_BRIDGE = "0x000000000000000000000000000000000000c0fe";
const TO_ADDRESS = "0x000000000000000000000000000000000000bEEF";
// Pinned literal — matches the `ROSE_TOKEN_ID` constant in Accounting.
const ROSE_TOKEN_ID =
  "0xca91975d6c6810eb4077546d4fbdb49fa231f351cddfc915862f7c0dad81a7aa";
const TokenType = { NativeEVM: 0, ERC20: 1, BridgeAsset: 2 } as const;
const INITIAL_BALANCE = 100_000_000_000_000_000n; // 0.1 ROSE

before(async () => {
  SAPPHIRE_CHAIN_ID = (await ethers.provider.getNetwork()).chainId;
});

type BridgePayload = {
  userAddress: string;
  toAddress: string;
  destChainId: bigint;
  routeAddress: string;
  amount: bigint;
  maxGasCost: bigint;
  nonce: bigint;
};

const bridgeWithdrawTypes = {
  BridgeWithdraw: [
    { name: "userAddress", type: "address" },
    { name: "toAddress", type: "address" },
    { name: "destChainId", type: "uint256" },
    { name: "routeAddress", type: "address" },
    { name: "amount", type: "uint256" },
    { name: "maxGasCost", type: "uint256" },
    { name: "nonce", type: "uint256" },
  ],
};

function getUserWallet(): Wallet {
  const mnemonic = (
    config.networks.hardhat.accounts as HardhatNetworkHDAccountsConfig
  ).mnemonic;
  return ethers.HDNodeWallet.fromPhrase(
    mnemonic,
    undefined,
    "m/44'/60'/0'/0/0",
  ).connect(ethers.provider) as unknown as Wallet;
}

async function getDomain(accounting: Contract | MockAccounting) {
  const d = await (accounting as any).eip712Domain();
  return {
    name: d[1],
    version: d[2],
    chainId: Number(d[3]),
    verifyingContract: d[4],
  };
}

async function configureRose(accounting: MockAccounting): Promise<void> {
  const roseData = await (accounting as any).encodeBridgeAssetTokenData("ROSE");
  await (accounting as any).setTokenInfo({
    tokenType: TokenType.BridgeAsset,
    data: roseData,
  });
  await accounting.setRoflBridge(BASE_CHAIN_ID, ROFL_BRIDGE);
}

async function signAndSubmitBaseRequest(
  accounting: MockAccounting,
  userWallet: Wallet,
  amount: bigint,
): Promise<bigint> {
  const domain = await getDomain(accounting);
  const nonce = await (accounting as any).withdrawalNonces(userWallet.address);
  const payload: BridgePayload = {
    userAddress: userWallet.address,
    toAddress: TO_ADDRESS,
    destChainId: BASE_CHAIN_ID,
    routeAddress: ROFL_BRIDGE,
    amount,
    maxGasCost: 0n,
    nonce,
  };
  const sig = await userWallet.signTypedData(
    domain,
    bridgeWithdrawTypes,
    payload,
  );
  await accounting.requestBridgeWithdrawal(
    payload.userAddress,
    payload.toAddress,
    payload.destChainId,
    payload.routeAddress,
    payload.amount,
    payload.maxGasCost,
    payload.nonce,
    sig,
  );
  return amount;
}

describe("Accounting bridge: security & roundtrip", () => {
  describe("bridge state writes", () => {
    it("requestBridgeWithdrawal debits the ledger and enqueues the withdrawal", async () => {
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      await configureRose(accounting);

      const userWallet = getUserWallet();
      await accounting.mockCreditDeposit(
        userWallet.address,
        ROSE_TOKEN_ID,
        INITIAL_BALANCE,
        ethers.id("roundtrip-request"),
      );

      const beforeLedger = await accounting.ledgerTotalOf(ROSE_TOKEN_ID);
      const beforeCount = await accounting.withdrawalCount();
      const amount = 100_000n;

      await signAndSubmitBaseRequest(accounting, userWallet, amount);

      expect(await accounting.ledgerTotalOf(ROSE_TOKEN_ID)).to.equal(
        beforeLedger - amount,
      );
      expect(await accounting.withdrawalCount()).to.equal(beforeCount + 1n);
      const queued = await accounting.withdrawals(beforeCount);
      expect(queued.userAddress).to.equal(userWallet.address);
      expect(queued.toAddress).to.equal(TO_ADDRESS);
      expect(queued.amount).to.equal(amount);
      expect(queued.tokenId).to.equal(ROSE_TOKEN_ID);
      expect(queued.resolved).to.equal(false);
    });

    it("creditDeposit feeds the bridge ledger read consumed by requestBridgeWithdrawal", async () => {
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      await configureRose(accounting);

      const userWallet = getUserWallet();
      const seeded = INITIAL_BALANCE;
      await accounting.mockCreditDeposit(
        userWallet.address,
        ROSE_TOKEN_ID,
        seeded,
        ethers.id("roundtrip-credit"),
      );

      // `requestBridgeWithdrawal` reads `_ledgerTotal` via
      // `BridgeLib.validateBridgeWithdrawal`, then decrements it. A successful
      // request for the full seeded amount (driving the ledger to zero) proves
      // the read sees the deposit-side write.
      await signAndSubmitBaseRequest(accounting, userWallet, seeded);
      expect(await accounting.ledgerTotalOf(ROSE_TOKEN_ID)).to.equal(0n);
    });

    it("setRoflBridge: zero address reverts InvalidAddress (write-time fail-closed)", async function () {
      const network = await ethers.provider.getNetwork();
      if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      await expect(
        accounting.setRoflBridge(BASE_CHAIN_ID, ZeroAddress),
      ).to.be.revertedWithCustomError(accounting, "InvalidAddress");
    });
  });

  describe("requestBridgeWithdrawal nonce + ledger invariants", () => {
    it("custody signer unchanged; destChain nonce +1; other-chain nonces unchanged; ledger decremented exactly", async () => {
      // The body allocates a *destination-chain* nonce via
      // `getEVMNonceAndIncrement(destChainId)` (the resolver uses it to build a
      // deterministic txIdentifier), then decrements `_ledgerTotal[ROSE]`. It
      // does NOT touch the custody EOA or unrelated `nonces[*]` entries.
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      await configureRose(accounting);

      const userWallet = getUserWallet();
      await accounting.mockCreditDeposit(
        userWallet.address,
        ROSE_TOKEN_ID,
        INITIAL_BALANCE,
        ethers.id("nested-invariants-seed"),
      );

      const beforeEvm = await accounting.evmAddress();
      const beforeBaseNonce = await accounting.nonces(BASE_CHAIN_ID);
      const beforeSapphireNonce = await accounting.nonces(SAPPHIRE_CHAIN_ID);
      const beforeLedger = await accounting.ledgerTotalOf(ROSE_TOKEN_ID);

      const amount = 100_000n;
      await signAndSubmitBaseRequest(accounting, userWallet, amount);

      expect(await accounting.evmAddress()).to.equal(beforeEvm);
      // destChain nonce: incremented by exactly one allocation.
      expect(await accounting.nonces(BASE_CHAIN_ID)).to.equal(
        beforeBaseNonce + 1n,
      );
      // unrelated-chain nonces must remain untouched.
      expect(await accounting.nonces(SAPPHIRE_CHAIN_ID)).to.equal(
        beforeSapphireNonce,
      );
      expect(await accounting.ledgerTotalOf(ROSE_TOKEN_ID)).to.equal(
        beforeLedger - amount,
      );
    });
  });

  describe("ROFL gate enforcement (generateSweepERC20TransferToBridge)", () => {
    // EIP155Signer.sign needs the Sapphire SIGN_DIGEST precompile; on Hardhat
    // it DER-decodes an empty response and reverts DER_Split_Error. Full
    // signed-tx decode is Sapphire-conditional and lives elsewhere.

    const TOKEN_ADDRESS = "0x000000000000000000000000000000000000abcd";
    const BENEFICIARY = "0x0000000000000000000000000000000000001234";
    const AMOUNT = 1_000_000n;
    const NONCE = 0n;
    const GAS_PRICE = 1_000_000_000n;
    const VERSION = 1n;
    const CHAIN_TYPE_EVM = 0;

    it("rejects unset roflSignerAddress with RoflSignerNotSet", async () => {
      // No mockSetRoflSignerAddress here — must hit the outermost gate.
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      await configureRose(accounting);

      await expect(
        accounting.generateSweepERC20TransferToBridge.staticCall(
          BENEFICIARY,
          CHAIN_TYPE_EVM,
          VERSION,
          BASE_CHAIN_ID,
          TOKEN_ADDRESS,
          AMOUNT,
          NONCE,
          GAS_PRICE,
        ),
      ).to.be.revertedWithCustomError(accounting, "RoflSignerNotSet");
    });

    it("rejects wrong sender with NotAuthorizedROFL", async () => {
      const [owner, other] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      await configureRose(accounting);
      await accounting.mockSetRoflSignerAddress(owner.address);

      await expect(
        (accounting.connect(other) as MockAccounting)
          .generateSweepERC20TransferToBridge.staticCall(
            BENEFICIARY,
            CHAIN_TYPE_EVM,
            VERSION,
            BASE_CHAIN_ID,
            TOKEN_ADDRESS,
            AMOUNT,
            NONCE,
            GAS_PRICE,
          ),
      ).to.be.revertedWithCustomError(accounting, "NotAuthorizedROFL");
    });

    it("rejects unregistered chainId with RoflBridgeNotSet", async function () {
      const network = await ethers.provider.getNetwork();
      if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      await configureRose(accounting);
      await accounting.mockSetRoflSignerAddress(owner.address);

      const unregisteredChain = 1n;
      await expect(
        accounting.generateSweepERC20TransferToBridge.staticCall(
          BENEFICIARY,
          CHAIN_TYPE_EVM,
          VERSION,
          unregisteredChain,
          TOKEN_ADDRESS,
          AMOUNT,
          NONCE,
          GAS_PRICE,
        ),
      )
        .to.be.revertedWithCustomError(accounting, "RoflBridgeNotSet")
        .withArgs(unregisteredChain);
    });

    it("rejects unset roflBridgeAddress[84532] with RoflBridgeNotSet", async function () {
      const network = await ethers.provider.getNetwork();
      if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
      // ROSE token registered, ROFL signer wired, but the Base route is unset
      // (skip configureRose's setRoflBridge step).
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      const roseData = await (accounting as any).encodeBridgeAssetTokenData(
        "ROSE",
      );
      await (accounting as any).setTokenInfo({
        tokenType: TokenType.BridgeAsset,
        data: roseData,
      });
      await accounting.mockSetRoflSignerAddress(owner.address);

      await expect(
        accounting.generateSweepERC20TransferToBridge.staticCall(
          BENEFICIARY,
          CHAIN_TYPE_EVM,
          VERSION,
          BASE_CHAIN_ID,
          TOKEN_ADDRESS,
          AMOUNT,
          NONCE,
          GAS_PRICE,
        ),
      )
        .to.be.revertedWithCustomError(accounting, "RoflBridgeNotSet")
        .withArgs(BASE_CHAIN_ID);
    });

    it("reaches sign and reverts DER_Split_Error on Hardhat", async function () {
      // Proves the resident body reaches EIP155Signer.sign for this selector.
      const network = await ethers.provider.getNetwork();
      if (network.name !== "hardhat" && network.name !== "unknown") this.skip();

      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      await configureRose(accounting);
      await accounting.mockSetRoflSignerAddress(owner.address);

      await expect(
        accounting.generateSweepERC20TransferToBridge.staticCall(
          BENEFICIARY,
          CHAIN_TYPE_EVM,
          VERSION,
          BASE_CHAIN_ID,
          TOKEN_ADDRESS,
          AMOUNT,
          NONCE,
          GAS_PRICE,
        ),
      ).to.be.revertedWithCustomError(accounting, "DER_Split_Error");
    });
  });
});
