import { expect } from "chai";
import { ethers, config, upgrades } from "hardhat";
import { Contract, Wallet, ZeroAddress } from "ethers";
import {
  MockAccounting,
  MockAccountingBridgeExposure,
  MockSiweAuth,
} from "../typechain-types";
import { HardhatNetworkHDAccountsConfig } from "hardhat/types";
import { attachAccounting } from "./util/links";

// Single MockAccounting handle — all bridge/lock/history selectors live on Accounting.
type CombinedAccounting = MockAccounting;

// EIP-712 type maps. Wire field stays `nonce` (wallet UX); Solidity arg / TS
// variables use `userNonce`.
const types = {
  Withdraw: [
    { name: "tokenId", type: "bytes32" },
    { name: "amount", type: "uint256" },
    { name: "nonce", type: "uint256" },
  ],
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

const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21);
const TO_ADDRESS = "0x000000000000000000000000000000000000bEEF";
const AMOUNT = 100_000n;
const ANY_TOKEN_ID = "0x" + "ab".repeat(32);

// Resolved at test-suite start from the live network. The contract treats
// `block.chainid` as the Sapphire-native release chain id, so on hardhat this
// is 31337, on sapphire-localnet 23293, on sapphire-testnet 23295. We can't
// set hardhat's chainId to a Sapphire value because `@oasisprotocol/sapphire-hardhat`
// auto-activates encrypted RPC on Sapphire chain ids and breaks the in-memory chain.
let SAPPHIRE_CHAIN_ID!: bigint;
const BASE_CHAIN_ID = 84532n;
const SAPPHIRE_RESERVE = 1_000_000_000_000_000n; // 0.001 ROSE
const MAX_SAPPHIRE_RESERVE = 10_000_000_000_000_000n; // matches contract constant
const SAPPHIRE_AMOUNT = 5_000_000_000_000_000n; // 0.005 ROSE > reserve
const ROFL_BRIDGE = "0x000000000000000000000000000000000000c0fe";
const OTHER_ADDRESS = "0x000000000000000000000000000000000000c001";

const INITIAL_BALANCE = 100_000_000_000_000_000n; // 0.1 ROSE — covers all behavior tests
// Pinned literal — matches Accounting.sol ROSE_TOKEN_ID constant.
const ROSE_TOKEN_ID =
  "0xca91975d6c6810eb4077546d4fbdb49fa231f351cddfc915862f7c0dad81a7aa";

const TokenType = { NativeEVM: 0, ERC20: 1, BridgeAsset: 2 } as const;

before(async () => {
  SAPPHIRE_CHAIN_ID = (await ethers.provider.getNetwork()).chainId;
});

type Domain = {
  name: string;
  version: string;
  chainId: number;
  verifyingContract: string;
};

async function deployBare(
  deployerAddress: string,
): Promise<CombinedAccounting> {
  const MockSiweAuthFactory = await ethers.getContractFactory("MockSiweAuth");
  const mockSiweAuth = await MockSiweAuthFactory.deploy("test");
  await mockSiweAuth.waitForDeployment();

  const AccountingFactory =
    await ethers.getContractFactory("MockAccounting");
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

  const proxyAddr = await accounting.getAddress();
  const [defaultSigner] = await ethers.getSigners();
  return (await attachAccounting(
    proxyAddr,
    defaultSigner,
  )) as unknown as CombinedAccounting;
}

async function configureBridgeRoutes(
  accounting: CombinedAccounting,
): Promise<void> {
  const roseData = await accounting.encodeBridgeAssetTokenData("ROSE");
  await accounting.setTokenInfo({
    tokenType: TokenType.BridgeAsset,
    data: roseData,
  });
  await accounting.setRoflBridge(BASE_CHAIN_ID, ROFL_BRIDGE);
}

async function getDomain(accounting: MockAccounting): Promise<Domain> {
  const d = await accounting.eip712Domain();
  return {
    name: d[1],
    version: d[2],
    chainId: Number(d[3]),
    verifyingContract: d[4],
  };
}

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

type BridgePayload = {
  userAddress: string;
  toAddress: string;
  destChainId: bigint;
  routeAddress: string;
  amount: bigint;
  maxGasCost: bigint;
  nonce: bigint;
};

type BridgeOverrides = Partial<BridgePayload>;

async function signBridge(
  accounting: CombinedAccounting,
  userWallet: Wallet,
  domain: Domain,
  overrides: BridgeOverrides = {},
): Promise<{ payload: BridgePayload; sig: string }> {
  // Default to a Base-shaped, validation-passing payload.
  const nonce =
    overrides.nonce ?? (await accounting.withdrawalNonces(userWallet.address));
  const payload: BridgePayload = {
    userAddress: overrides.userAddress ?? userWallet.address,
    toAddress: overrides.toAddress ?? TO_ADDRESS,
    destChainId: overrides.destChainId ?? BASE_CHAIN_ID,
    routeAddress: overrides.routeAddress ?? ROFL_BRIDGE,
    amount: overrides.amount ?? AMOUNT,
    maxGasCost: overrides.maxGasCost ?? 0n,
    nonce,
  };
  const sig = await userWallet.signTypedData(
    domain,
    { BridgeWithdraw: types.BridgeWithdraw },
    payload,
  );
  return { payload, sig };
}

async function submitBridge(
  accounting: CombinedAccounting,
  payload: BridgePayload,
  sig: string,
) {
  return accounting.requestBridgeWithdrawal(
    payload.userAddress,
    payload.toAddress,
    payload.destChainId,
    payload.routeAddress,
    payload.amount,
    payload.maxGasCost,
    payload.nonce,
    sig,
  );
}

function decodeTxId(txId: string): [bigint, bigint, string, bigint] {
  const decoded = ethers.AbiCoder.defaultAbiCoder().decode(
    ["uint256", "uint64", "address", "uint256"],
    txId,
  );
  return [decoded[0], decoded[1], decoded[2], decoded[3]];
}

describe("requestBridgeWithdrawal: validation", () => {
  let accounting: CombinedAccounting;
  let userWallet: Wallet;
  let domain: Domain;
  let userAddress: string;

  before(async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
  });

  before(async () => {
    const [deployer] = await ethers.getSigners();
    accounting = await deployBare(deployer.address);
    await configureBridgeRoutes(accounting);
    userWallet = getUserWallet();
    domain = await getDomain(accounting);
    userAddress = userWallet.address;
  });

  it("rejects zero userAddress", async () => {
    const { payload, sig } = await signBridge(accounting, userWallet, domain, {
      destChainId: SAPPHIRE_CHAIN_ID,
      routeAddress: ZeroAddress,
      amount: SAPPHIRE_AMOUNT,
      maxGasCost: SAPPHIRE_RESERVE,
      userAddress: ZeroAddress,
    });
    await expect(
      submitBridge(accounting, payload, sig),
    ).to.be.revertedWithCustomError(accounting, "InvalidAddress");
  });

  it("rejects zero toAddress", async () => {
    const { payload, sig } = await signBridge(accounting, userWallet, domain, {
      destChainId: SAPPHIRE_CHAIN_ID,
      routeAddress: ZeroAddress,
      amount: SAPPHIRE_AMOUNT,
      maxGasCost: SAPPHIRE_RESERVE,
      toAddress: ZeroAddress,
    });
    await expect(
      submitBridge(accounting, payload, sig),
    ).to.be.revertedWithCustomError(accounting, "InvalidAddress");
  });

  it("rejects zero amount", async () => {
    const { payload, sig } = await signBridge(accounting, userWallet, domain, {
      destChainId: SAPPHIRE_CHAIN_ID,
      routeAddress: ZeroAddress,
      amount: 0n,
      maxGasCost: SAPPHIRE_RESERVE,
    });
    await expect(
      submitBridge(accounting, payload, sig),
    ).to.be.revertedWithCustomError(accounting, "InvalidAmount");
  });

  it("rejects an unregistered destination chain", async () => {
    const { payload, sig } = await signBridge(accounting, userWallet, domain, {
      destChainId: 1n,
      routeAddress: ZeroAddress,
      amount: SAPPHIRE_AMOUNT,
      maxGasCost: SAPPHIRE_RESERVE,
    });
    await expect(submitBridge(accounting, payload, sig))
      .to.be.revertedWithCustomError(accounting, "RoflBridgeNotSet")
      .withArgs(1n);
  });

  it("Sapphire: rejects non-zero routeAddress", async () => {
    const { payload, sig } = await signBridge(accounting, userWallet, domain, {
      destChainId: SAPPHIRE_CHAIN_ID,
      routeAddress: OTHER_ADDRESS,
      amount: SAPPHIRE_AMOUNT,
      maxGasCost: SAPPHIRE_RESERVE,
    });
    await expect(
      submitBridge(accounting, payload, sig),
    ).to.be.revertedWithCustomError(accounting, "InvalidRouteAddress");
  });

  it("Sapphire: rejects maxGasCost == 0", async () => {
    const { payload, sig } = await signBridge(accounting, userWallet, domain, {
      destChainId: SAPPHIRE_CHAIN_ID,
      routeAddress: ZeroAddress,
      amount: SAPPHIRE_AMOUNT,
      maxGasCost: 0n,
    });
    await expect(
      submitBridge(accounting, payload, sig),
    ).to.be.revertedWithCustomError(accounting, "InvalidMaxGasCost");
  });

  it("Sapphire: rejects amount <= maxGasCost (equal)", async () => {
    const { payload, sig } = await signBridge(accounting, userWallet, domain, {
      destChainId: SAPPHIRE_CHAIN_ID,
      routeAddress: ZeroAddress,
      amount: SAPPHIRE_RESERVE,
      maxGasCost: SAPPHIRE_RESERVE,
    });
    await expect(
      submitBridge(accounting, payload, sig),
    ).to.be.revertedWithCustomError(accounting, "InvalidAmount");
  });

  it("Sapphire: rejects amount <= maxGasCost (smaller)", async () => {
    const { payload, sig } = await signBridge(accounting, userWallet, domain, {
      destChainId: SAPPHIRE_CHAIN_ID,
      routeAddress: ZeroAddress,
      amount: SAPPHIRE_RESERVE - 1n,
      maxGasCost: SAPPHIRE_RESERVE,
    });
    await expect(
      submitBridge(accounting, payload, sig),
    ).to.be.revertedWithCustomError(accounting, "InvalidAmount");
  });

  it("Sapphire: rejects maxGasCost > MAX_SAPPHIRE_RELEASE_RESERVE", async () => {
    // amount=1n is non-zero (passes top-level check); the maxGasCost > MAX
    // check fires before the amount-vs-maxGasCost comparison.
    const { payload, sig } = await signBridge(accounting, userWallet, domain, {
      destChainId: SAPPHIRE_CHAIN_ID,
      routeAddress: ZeroAddress,
      amount: 1n,
      maxGasCost: MAX_SAPPHIRE_RESERVE + 1n,
    });
    await expect(
      submitBridge(accounting, payload, sig),
    ).to.be.revertedWithCustomError(accounting, "InvalidMaxGasCost");
  });

  it("Base: rejects mismatched routeAddress", async () => {
    const { payload, sig } = await signBridge(accounting, userWallet, domain, {
      destChainId: BASE_CHAIN_ID,
      routeAddress: OTHER_ADDRESS,
      amount: AMOUNT,
      maxGasCost: 0n,
    });
    await expect(
      submitBridge(accounting, payload, sig),
    ).to.be.revertedWithCustomError(accounting, "InvalidRouteAddress");
  });

  it("Base: rejects non-zero maxGasCost", async () => {
    const { payload, sig } = await signBridge(accounting, userWallet, domain, {
      destChainId: BASE_CHAIN_ID,
      routeAddress: ROFL_BRIDGE,
      amount: AMOUNT,
      maxGasCost: 1n,
    });
    await expect(
      submitBridge(accounting, payload, sig),
    ).to.be.revertedWithCustomError(accounting, "InvalidMaxGasCost");
  });

  describe("when ROSE_TOKEN_ID is not registered as BridgeAsset", () => {
    let bareAccounting: CombinedAccounting;
    let bareDomain: Domain;

    before(async () => {
      const [deployer] = await ethers.getSigners();
      bareAccounting = await deployBare(deployer.address);
      // Deliberately do NOT setTokenInfo for ROSE.
      bareDomain = await getDomain(bareAccounting);
    });

    it("rejects with UnsupportedTokenType", async () => {
      const { payload, sig } = await signBridge(
        bareAccounting,
        userWallet,
        bareDomain,
        {
          destChainId: SAPPHIRE_CHAIN_ID,
          routeAddress: ZeroAddress,
          amount: SAPPHIRE_AMOUNT,
          maxGasCost: SAPPHIRE_RESERVE,
        },
      );
      await expect(
        submitBridge(bareAccounting, payload, sig),
      ).to.be.revertedWithCustomError(bareAccounting, "UnsupportedTokenType");
    });
  });

  describe("when roflBridgeAddress[84532] is unset", () => {
    let unsetAccounting: CombinedAccounting;
    let unsetDomain: Domain;

    before(async () => {
      const [deployer] = await ethers.getSigners();
      unsetAccounting = await deployBare(deployer.address);
      // Register ROSE so the registration check passes; deliberately do NOT setRoflBridge.
      const roseData = await unsetAccounting.encodeBridgeAssetTokenData("ROSE");
      await unsetAccounting.setTokenInfo({
        tokenType: TokenType.BridgeAsset,
        data: roseData,
      });
      unsetDomain = await getDomain(unsetAccounting);
    });

    it("Base: rejects with RoflBridgeNotSet(84532)", async () => {
      const { payload, sig } = await signBridge(
        unsetAccounting,
        userWallet,
        unsetDomain,
        {
          destChainId: BASE_CHAIN_ID,
          routeAddress: ROFL_BRIDGE,
          amount: AMOUNT,
          maxGasCost: 0n,
        },
      );
      await expect(submitBridge(unsetAccounting, payload, sig))
        .to.be.revertedWithCustomError(unsetAccounting, "RoflBridgeNotSet")
        .withArgs(84532n);
    });
  });
});

describe("requestBridgeWithdrawal: signature", () => {
  let accounting: CombinedAccounting;
  let userWallet: Wallet;
  let domain: Domain;

  before(async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
  });

  // Each test redeploys to keep withdrawalNonces predictable.
  beforeEach(async () => {
    const [deployer] = await ethers.getSigners();
    accounting = await deployBare(deployer.address);
    await configureBridgeRoutes(accounting);
    userWallet = getUserWallet();
    domain = await getDomain(accounting);
    // Seed enough ROSE balance + ledger so successful submissions don't underflow.
    await accounting.mockCreditDeposit(
      userWallet.address,
      ROSE_TOKEN_ID,
      INITIAL_BALANCE,
      ethers.id("seed-sig"),
    );
  });

  it("rejects a Withdraw-typed signature replayed as BridgeWithdraw", async () => {
    const nonce = await accounting.withdrawalNonces(userWallet.address);
    const sig = await userWallet.signTypedData(
      domain,
      { Withdraw: types.Withdraw },
      {
        tokenId: ANY_TOKEN_ID,
        amount: AMOUNT,
        nonce,
      },
    );
    // Submit as a Base-shaped BridgeWithdraw — validation passes, nonce passes,
    // sig digest mismatches because typehash differs.
    await expect(
      accounting.requestBridgeWithdrawal(
        userWallet.address,
        TO_ADDRESS,
        BASE_CHAIN_ID,
        ROFL_BRIDGE,
        AMOUNT,
        0n,
        nonce,
        sig,
      ),
    ).to.be.revertedWithCustomError(accounting, "InvalidSignature");
  });

  it("rejects a BridgeWithdraw signature replayed as Withdraw", async () => {
    const { payload, sig } = await signBridge(accounting, userWallet, domain);
    // requestWithdrawal derives the signer from the signature (no explicit
    // userAddress argument, unlike requestBridgeWithdrawal). A BridgeWithdraw
    // signature therefore recovers an unrelated address rather than failing
    // verification; that address holds no balance, so the replayed withdrawal
    // is rejected at the debit step.
    await expect(
      accounting.requestWithdrawal(
        ANY_TOKEN_ID,
        payload.amount,
        payload.nonce,
        sig,
      ),
    ).to.be.revertedWithCustomError(accounting, "InsufficientBalance");
  });

  it("rejects mismatched toAddress", async () => {
    const { payload, sig } = await signBridge(accounting, userWallet, domain);
    // Submit with a different toAddress than was signed.
    await expect(
      accounting.requestBridgeWithdrawal(
        payload.userAddress,
        OTHER_ADDRESS,
        payload.destChainId,
        payload.routeAddress,
        payload.amount,
        payload.maxGasCost,
        payload.nonce,
        sig,
      ),
    ).to.be.revertedWithCustomError(accounting, "InvalidSignature");
  });

  it("rejects mismatched amount", async () => {
    const { payload, sig } = await signBridge(accounting, userWallet, domain);
    await expect(
      accounting.requestBridgeWithdrawal(
        payload.userAddress,
        payload.toAddress,
        payload.destChainId,
        payload.routeAddress,
        payload.amount * 2n,
        payload.maxGasCost,
        payload.nonce,
        sig,
      ),
    ).to.be.revertedWithCustomError(accounting, "InvalidSignature");
  });

  it("rejects stale nonce after a successful bridge withdrawal", async () => {
    // Burn nonce 0 with a successful submission.
    const fresh = await signBridge(accounting, userWallet, domain);
    await submitBridge(accounting, fresh.payload, fresh.sig);

    // Re-sign at the now-stale nonce 0 and resubmit.
    const stale = await signBridge(accounting, userWallet, domain, {
      nonce: 0n,
    });
    await expect(
      submitBridge(accounting, stale.payload, stale.sig),
    ).to.be.revertedWithCustomError(accounting, "InvalidNonce");
  });

  it("shares withdrawalNonces[user] with the normal Withdraw typehash", async () => {
    // BridgeWithdraw advances withdrawalNonces[user] from 0 to 1.
    const before = await accounting.withdrawalNonces(userWallet.address);
    const { payload, sig } = await signBridge(accounting, userWallet, domain, {
      nonce: before,
    });
    await submitBridge(accounting, payload, sig);
    expect(await accounting.withdrawalNonces(userWallet.address)).to.equal(
      before + 1n,
    );

    // A Withdraw signed at the now-stale nonce must revert InvalidNonce —
    // proving Withdraw reads the same counter the BridgeWithdraw just bumped.
    const staleSig = await userWallet.signTypedData(
      domain,
      { Withdraw: types.Withdraw },
      {
        tokenId: ANY_TOKEN_ID,
        amount: AMOUNT,
        nonce: before,
      },
    );
    await expect(
      accounting.requestWithdrawal(
        ANY_TOKEN_ID,
        AMOUNT,
        before,
        staleSig,
      ),
    ).to.be.revertedWithCustomError(accounting, "InvalidNonce");
  });
});

describe("requestBridgeWithdrawal: behavior", () => {
  let accounting: CombinedAccounting;
  let userWallet: Wallet;
  let domain: Domain;
  let userAddress: string;

  beforeEach(async () => {
    const [deployer] = await ethers.getSigners();
    accounting = await deployBare(deployer.address);
    await configureBridgeRoutes(accounting);
    userWallet = getUserWallet();
    domain = await getDomain(accounting);
    userAddress = userWallet.address;
    await accounting.mockCreditDeposit(
      userAddress,
      ROSE_TOKEN_ID,
      INITIAL_BALANCE,
      ethers.id("seed-behavior"),
    );
  });

  it("Base: debits balance and ledger and emits Withdrawal", async () => {
    const balBefore = await accounting.getBalance(userAddress, ROSE_TOKEN_ID);
    const ledgerBefore = await accounting.ledgerTotalOf(ROSE_TOKEN_ID);

    const { payload, sig } = await signBridge(accounting, userWallet, domain);
    await expect(submitBridge(accounting, payload, sig))
      .to.emit(accounting, "Withdrawal")
      .withArgs(userAddress, ROSE_TOKEN_ID, AMOUNT, BASE_CHAIN_ID);

    expect(await accounting.getBalance(userAddress, ROSE_TOKEN_ID)).to.equal(
      balBefore - AMOUNT,
    );
    expect(await accounting.ledgerTotalOf(ROSE_TOKEN_ID)).to.equal(
      ledgerBefore - AMOUNT,
    );
  });

  it("Base: appends WithdrawalRequest with correct txIdentifier shape", async () => {
    const prevDestNonce = await accounting.nonces(BASE_CHAIN_ID);
    const { payload, sig } = await signBridge(accounting, userWallet, domain);
    await submitBridge(accounting, payload, sig);

    const idx = 0n; // first entry
    const req = await accounting.withdrawals(idx);
    expect(req.userAddress).to.equal(userAddress);
    expect(req.toAddress).to.equal(TO_ADDRESS);
    expect(req.amount).to.equal(AMOUNT);
    expect(req.tokenId).to.equal(ROSE_TOKEN_ID);
    expect(req.resolved).to.equal(false);

    const [destChainId, destTxNonce, routeAddress, maxGasCost] = decodeTxId(
      req.txIdentifier,
    );
    expect(destChainId).to.equal(BASE_CHAIN_ID);
    expect(destTxNonce).to.equal(prevDestNonce);
    expect(routeAddress.toLowerCase()).to.equal(ROFL_BRIDGE.toLowerCase());
    expect(maxGasCost).to.equal(0n);
  });

  it("Sapphire: stores txIdentifier with maxGasCost reserve", async () => {
    const prevDestNonce = await accounting.nonces(SAPPHIRE_CHAIN_ID);
    const { payload, sig } = await signBridge(accounting, userWallet, domain, {
      destChainId: SAPPHIRE_CHAIN_ID,
      routeAddress: ZeroAddress,
      amount: SAPPHIRE_AMOUNT,
      maxGasCost: SAPPHIRE_RESERVE,
    });
    await expect(submitBridge(accounting, payload, sig))
      .to.emit(accounting, "Withdrawal")
      .withArgs(userAddress, ROSE_TOKEN_ID, SAPPHIRE_AMOUNT, SAPPHIRE_CHAIN_ID);

    const req = await accounting.withdrawals(0n);
    const [destChainId, destTxNonce, routeAddress, maxGasCost] = decodeTxId(
      req.txIdentifier,
    );
    expect(destChainId).to.equal(SAPPHIRE_CHAIN_ID);
    expect(destTxNonce).to.equal(prevDestNonce);
    expect(routeAddress).to.equal(ZeroAddress);
    expect(maxGasCost).to.equal(SAPPHIRE_RESERVE);
  });

  it("reserves destination EOA nonce via getEVMNonceAndIncrement", async () => {
    const before = await accounting.nonces(BASE_CHAIN_ID);
    const { payload, sig } = await signBridge(accounting, userWallet, domain);
    await submitBridge(accounting, payload, sig);
    const after = await accounting.nonces(BASE_CHAIN_ID);
    expect(after).to.equal(before + 1n);

    const req = await accounting.withdrawals(0n);
    const [, destTxNonce] = decodeTxId(req.txIdentifier);
    expect(destTxNonce).to.equal(before);
  });

  it("userNonce and destTxNonce are independent counters", async () => {
    // First: bridge to Base. Advances nonces[84532] 0→1, withdrawalNonces[user] 0→1.
    const baseSig = await signBridge(accounting, userWallet, domain);
    await submitBridge(accounting, baseSig.payload, baseSig.sig);

    // Second: bridge to Sapphire. nonces[23295] is still 0 (independent per-chain).
    // userNonce must now be 1 (consumed slot is shared with Withdraw).
    const sapphireSig = await signBridge(accounting, userWallet, domain, {
      destChainId: SAPPHIRE_CHAIN_ID,
      routeAddress: ZeroAddress,
      amount: SAPPHIRE_AMOUNT,
      maxGasCost: SAPPHIRE_RESERVE,
      // signBridge defaults to current withdrawalNonces, which is 1 here.
    });
    expect(sapphireSig.payload.nonce).to.equal(1n);
    await submitBridge(accounting, sapphireSig.payload, sapphireSig.sig);

    const req = await accounting.withdrawals(1n);
    const [, destTxNonce] = decodeTxId(req.txIdentifier);
    expect(destTxNonce).to.equal(0n);
    expect(await accounting.nonces(SAPPHIRE_CHAIN_ID)).to.equal(1n);
    expect(await accounting.withdrawalNonces(userAddress)).to.equal(2n);
  });

  it("reverts InsufficientBalance when balance < amount", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
    // Sign over a Base payload with amount > seeded balance.
    const tooMuch = INITIAL_BALANCE + 1n;
    const { payload, sig } = await signBridge(accounting, userWallet, domain, {
      amount: tooMuch,
    });
    await expect(
      submitBridge(accounting, payload, sig),
    ).to.be.revertedWithCustomError(accounting, "InsufficientBalance");

    // Balance + ledger untouched after revert.
    expect(await accounting.getBalance(userAddress, ROSE_TOKEN_ID)).to.equal(
      INITIAL_BALANCE,
    );
    expect(await accounting.ledgerTotalOf(ROSE_TOKEN_ID)).to.equal(
      INITIAL_BALANCE,
    );
  });

  it("reverts InvalidNonce on replay (verifier consumed userNonce)", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
    const { payload, sig } = await signBridge(accounting, userWallet, domain);
    await submitBridge(accounting, payload, sig);
    // Same payload, same sig — verifier has already incremented withdrawalNonces.
    await expect(
      submitBridge(accounting, payload, sig),
    ).to.be.revertedWithCustomError(accounting, "InvalidNonce");
  });

  it("runs validation before signature verification (bad-sig + bad-chain)", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
    // Sign a valid Sapphire payload, then submit with destChainId=1 and a corrupted sig.
    // If sig-check ran first, ECDSA recovery on the corrupted sig surfaces InvalidSignature.
    // Validation runs first, so the bad chain fires RoflBridgeNotSet instead.
    const { sig } = await signBridge(accounting, userWallet, domain, {
      destChainId: SAPPHIRE_CHAIN_ID,
      routeAddress: ZeroAddress,
      amount: SAPPHIRE_AMOUNT,
      maxGasCost: SAPPHIRE_RESERVE,
    });
    // Flip a byte in the signature.
    const corrupted = sig.slice(0, -2) + (sig.endsWith("00") ? "ff" : "00");
    await expect(
      accounting.requestBridgeWithdrawal(
        userAddress,
        TO_ADDRESS,
        1n,
        ZeroAddress,
        SAPPHIRE_AMOUNT,
        SAPPHIRE_RESERVE,
        await accounting.withdrawalNonces(userAddress),
        corrupted,
      ),
    )
      .to.be.revertedWithCustomError(accounting, "RoflBridgeNotSet")
      .withArgs(1n);
  });
});

describe("non-bridge withdrawal paths reject BridgeAsset", () => {
  let accounting: CombinedAccounting;

  before(async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
  });

  beforeEach(async () => {
    const [deployer] = await ethers.getSigners();
    accounting = await deployBare(deployer.address);
    await configureBridgeRoutes(accounting); // registers ROSE as BridgeAsset
  });

  it("requestWithdrawal reverts BridgeAssetNotSupported for ROSE_TOKEN_ID", async () => {
    // Gate runs before sig verify — `0x` signature never reaches the verifier.
    await expect(
      accounting.requestWithdrawal(ROSE_TOKEN_ID, 1n, 0n, "0x"),
    ).to.be.revertedWithCustomError(accounting, "BridgeAssetNotSupported");
  });

  it("executeEmergencyWithdraw reverts BridgeAssetNotSupported for ROSE_TOKEN_ID", async () => {
    // Gate runs before the request-slot lookup — no slot needs to exist.
    await expect(
      accounting.executeEmergencyWithdraw(
        ZeroAddress,
        ROSE_TOKEN_ID,
        0,
        0n,
        1n,
        1n,
      ),
    ).to.be.revertedWithCustomError(accounting, "BridgeAssetNotSupported");
  });
});

describe("resolveBridgeWithdrawal", () => {
  // Hardhat default accounts[0] — must match MockAccounting TEST_ADDRESS.
  const HH_ACCOUNT0 = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266";

  const BASE_GAS_PRICE = 1_000_000_000n; // 1 gwei
  const BASE_MINT_GAS_LIMIT = 200_000n; // matches BridgeLib.GAS_LIMIT_BRIDGE_MINT
  const SAPPHIRE_GAS_LIMIT = 25_000n; // matches BridgeLib.GAS_LIMIT_NATIVE_RELEASE
  const SAPPHIRE_GAS_PRICE = 10n; // 25000 * 10 = 250000 ≪ SAPPHIRE_RESERVE
  const MINT_SELECTOR = ethers.id("mint(address,uint256,bytes32)").slice(0, 10);

  // Handle exposing MockAccountingBridgeExposure mock helpers
  // (mockPushBridgeWithdrawal, etc.) alongside the resident bridge selectors
  // (resolveBridgeWithdrawal, setRoflBridge, requestBridgeWithdrawal).
  let accounting: MockAccountingBridgeExposure;
  let userWallet: Wallet;
  let domain: Domain;
  let userAddress: string;

  // Helper: enqueue a Sapphire bridge withdrawal and return its index +
  // resolver-relevant fields.
  async function enqueueSapphire(): Promise<{
    index: bigint;
    toAddress: string;
    amount: bigint;
    maxGasCost: bigint;
  }> {
    const { payload, sig } = await signBridge(
      accounting as unknown as CombinedAccounting,
      userWallet,
      domain,
      {
        destChainId: SAPPHIRE_CHAIN_ID,
        routeAddress: ZeroAddress,
        amount: SAPPHIRE_AMOUNT,
        maxGasCost: SAPPHIRE_RESERVE,
      },
    );
    const index = await accounting.withdrawalCount();
    await submitBridge(
      accounting as unknown as CombinedAccounting,
      payload,
      sig,
    );
    return {
      index,
      toAddress: payload.toAddress,
      amount: payload.amount,
      maxGasCost: payload.maxGasCost,
    };
  }

  // Helper: enqueue a Base bridge withdrawal and return its index + relevant fields.
  async function enqueueBase(): Promise<{
    index: bigint;
    toAddress: string;
    amount: bigint;
    routeAddress: string;
  }> {
    const { payload, sig } = await signBridge(
      accounting as unknown as CombinedAccounting,
      userWallet,
      domain,
      {
        destChainId: BASE_CHAIN_ID,
        routeAddress: ROFL_BRIDGE,
        amount: AMOUNT,
        maxGasCost: 0n,
      },
    );
    const index = await accounting.withdrawalCount();
    await submitBridge(
      accounting as unknown as CombinedAccounting,
      payload,
      sig,
    );
    return {
      index,
      toAddress: payload.toAddress,
      amount: payload.amount,
      routeAddress: payload.routeAddress,
    };
  }

  beforeEach(async () => {
    const [deployer] = await ethers.getSigners();
    const MockSiweAuthFactory = await ethers.getContractFactory("MockSiweAuth");
    const mockSiweAuth = await MockSiweAuthFactory.deploy("test");
    await mockSiweAuth.waitForDeployment();
    const Factory = await ethers.getContractFactory(
      "MockAccountingBridgeExposure",
    );
    const proxyExposure = (await upgrades.deployProxy(
      Factory,
      [MOCK_ROFL_APP_ID, deployer.address],
      {
        kind: "uups",
        initializer: "initialize",
        constructorArgs: [await mockSiweAuth.getAddress()],
        unsafeAllow: ["missing-initializer"],
      },
    )) as unknown as MockAccountingBridgeExposure;
    await proxyExposure.waitForDeployment();

    const proxyAddr = await proxyExposure.getAddress();
    accounting = (await attachAccounting(
      proxyAddr,
      deployer,
      "MockAccountingBridgeExposure",
    )) as unknown as MockAccountingBridgeExposure;

    await configureBridgeRoutes(accounting as unknown as CombinedAccounting);
    await accounting.setGasPrice(BASE_CHAIN_ID, BASE_GAS_PRICE);
    await accounting.setGasPrice(SAPPHIRE_CHAIN_ID, SAPPHIRE_GAS_PRICE);

    userWallet = getUserWallet();
    domain = await getDomain(accounting as unknown as MockAccounting);
    userAddress = userWallet.address;
    // mockCreditDeposit bumps both `balances` and `_ledgerTotal` so the
    // `_decreaseLedgerTotal` inside `requestBridgeWithdrawal` does not underflow.
    await accounting.mockCreditDeposit(
      userAddress,
      ROSE_TOKEN_ID,
      INITIAL_BALANCE,
      ethers.id("seed-resolve"),
    );
  });

  it("resolveWithdrawal reverts on a queued ROSE request (BridgeAsset has no NativeEVM/ERC20 branch)", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
    const { index } = await enqueueBase();
    await expect(
      accounting.resolveWithdrawal(index),
    ).to.be.revertedWithCustomError(accounting, "UnsupportedTokenType");
  });

  it("resolveBridgeWithdrawal reverts on a non-ROSE row", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
    // mockPushBridgeWithdrawal sets an unconfigured destChainId; RoflBridgeNotSet
    // fires first, proving the function is reachable.
    const idx = await accounting.mockPushBridgeWithdrawal.staticCall(
      userAddress,
      TO_ADDRESS,
      AMOUNT,
      999n,
      0n,
      ZeroAddress,
      0n,
    );
    await accounting.mockPushBridgeWithdrawal(
      userAddress,
      TO_ADDRESS,
      AMOUNT,
      999n,
      0n,
      ZeroAddress,
      0n,
    );
    await expect(accounting.resolveBridgeWithdrawal(idx))
      .to.be.revertedWithCustomError(accounting, "RoflBridgeNotSet")
      .withArgs(999n);
  });

  it("Sapphire branch: GAS_LIMIT_NATIVE_RELEASE * gasPrices[Sapphire] > maxGasCost reverts GasBudgetExceeded", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
    const { index, maxGasCost } = await enqueueSapphire();
    // Bump gasPrices[Sapphire] after the user signed, so that
    // 25000 * gasPrices[Sapphire] just exceeds the signed maxGasCost.
    const overflowGasPrice = maxGasCost / 25_000n + 1n;
    await accounting.setGasPrice(SAPPHIRE_CHAIN_ID, overflowGasPrice);
    await expect(
      accounting.resolveBridgeWithdrawal(index),
    ).to.be.revertedWithCustomError(accounting, "GasBudgetExceeded");
  });

  it("Base branch: gasPrices[84532] unset reverts GasPriceNotSet", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
    const { index } = await enqueueBase();
    // setGasPrice(_, 0) reverts, so deploy a fresh proxy with gasPrices[84532] left unset.
    const [deployer] = await ethers.getSigners();
    const MockSiweAuthFactory = await ethers.getContractFactory("MockSiweAuth");
    const mockSiweAuth = await MockSiweAuthFactory.deploy("test");
    await mockSiweAuth.waitForDeployment();
    const Factory = await ethers.getContractFactory(
      "MockAccountingBridgeExposure",
    );
    const proxyExposure = (await upgrades.deployProxy(
      Factory,
      [MOCK_ROFL_APP_ID, deployer.address],
      {
        kind: "uups",
        initializer: "initialize",
        constructorArgs: [await mockSiweAuth.getAddress()],
        unsafeAllow: ["missing-initializer"],
      },
    )) as unknown as MockAccountingBridgeExposure;
    await proxyExposure.waitForDeployment();

    const acct = (await attachAccounting(
      await proxyExposure.getAddress(),
      deployer,
      "MockAccountingBridgeExposure",
    )) as unknown as MockAccountingBridgeExposure;

    await configureBridgeRoutes(acct as unknown as CombinedAccounting);
    // No setGasPrice call — gasPrices[84532] is zero.
    await acct.mockCreditDeposit(
      userAddress,
      ROSE_TOKEN_ID,
      INITIAL_BALANCE,
      ethers.id("seed-resolve-base-unset"),
    );
    const freshDomain = await getDomain(acct as unknown as MockAccounting);
    const { payload, sig } = await signBridge(
      acct as unknown as CombinedAccounting,
      userWallet,
      freshDomain,
      {
        destChainId: BASE_CHAIN_ID,
        routeAddress: ROFL_BRIDGE,
        amount: AMOUNT,
        maxGasCost: 0n,
      },
    );
    const idx = await acct.withdrawalCount();
    await submitBridge(acct as unknown as CombinedAccounting, payload, sig);
    void index; // unused — kept for symmetry with the helper above
    await expect(acct.resolveBridgeWithdrawal(idx))
      .to.be.revertedWithCustomError(acct, "GasPriceNotSet")
      .withArgs(BASE_CHAIN_ID);
  });

  it("Sapphire branch: gasPrices[Sapphire] unset reverts GasPriceNotSet", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
    // Deploy a fresh proxy without the SAPPHIRE_CHAIN_ID gas price set; mirrors
    // the Base "unset reverts GasPriceNotSet" test above.
    const [deployer] = await ethers.getSigners();
    const MockSiweAuthFactory = await ethers.getContractFactory("MockSiweAuth");
    const mockSiweAuth = await MockSiweAuthFactory.deploy("test");
    await mockSiweAuth.waitForDeployment();
    const Factory = await ethers.getContractFactory(
      "MockAccountingBridgeExposure",
    );
    const proxyExposure = (await upgrades.deployProxy(
      Factory,
      [MOCK_ROFL_APP_ID, deployer.address],
      {
        kind: "uups",
        initializer: "initialize",
        constructorArgs: [await mockSiweAuth.getAddress()],
        unsafeAllow: ["missing-initializer"],
      },
    )) as unknown as MockAccountingBridgeExposure;
    await proxyExposure.waitForDeployment();

    const acct = (await attachAccounting(
      await proxyExposure.getAddress(),
      deployer,
      "MockAccountingBridgeExposure",
    )) as unknown as MockAccountingBridgeExposure;

    await configureBridgeRoutes(acct as unknown as CombinedAccounting);
    // No setGasPrice for SAPPHIRE_CHAIN_ID — leave it zero.
    await acct.mockCreditDeposit(
      userAddress,
      ROSE_TOKEN_ID,
      INITIAL_BALANCE,
      ethers.id("seed-resolve-sapphire-unset"),
    );
    const freshDomain = await getDomain(acct as unknown as MockAccounting);
    const { payload, sig } = await signBridge(
      acct as unknown as CombinedAccounting,
      userWallet,
      freshDomain,
      {
        destChainId: SAPPHIRE_CHAIN_ID,
        routeAddress: ZeroAddress,
        amount: SAPPHIRE_AMOUNT,
        maxGasCost: SAPPHIRE_RESERVE,
      },
    );
    const idx = await acct.withdrawalCount();
    await submitBridge(acct as unknown as CombinedAccounting, payload, sig);
    await expect(acct.resolveBridgeWithdrawal(idx))
      .to.be.revertedWithCustomError(acct, "GasPriceNotSet")
      .withArgs(SAPPHIRE_CHAIN_ID);
  });

  it("Backstop: unconfigured destChainId reverts RoflBridgeNotSet", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
    const idx = await accounting.mockPushBridgeWithdrawal.staticCall(
      userAddress,
      TO_ADDRESS,
      AMOUNT,
      1n,
      0n,
      ZeroAddress,
      0n,
    );
    await accounting.mockPushBridgeWithdrawal(
      userAddress,
      TO_ADDRESS,
      AMOUNT,
      1n,
      0n,
      ZeroAddress,
      0n,
    );
    await expect(accounting.resolveBridgeWithdrawal(idx))
      .to.be.revertedWithCustomError(accounting, "RoflBridgeNotSet")
      .withArgs(1n);
  });

  it("Sapphire happy-path reaches sign and reverts DER_Split_Error on Hardhat", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
    const { index } = await enqueueSapphire();
    // All validation passes; EIP155Signer.sign hits the missing Sapphire precompile
    // and DER-decodes an empty response, reverting DER_Split_Error.
    await expect(
      accounting.resolveBridgeWithdrawal(index),
    ).to.be.revertedWithCustomError(accounting, "DER_Split_Error");
  });

  it("Base happy-path reaches sign and reverts DER_Split_Error on Hardhat", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
    const { index } = await enqueueBase();
    await expect(
      accounting.resolveBridgeWithdrawal(index),
    ).to.be.revertedWithCustomError(accounting, "DER_Split_Error");
  });

  // ───────────────── Sapphire-localnet only ───────────────────
  // These rely on EIP155Signer.sign actually returning RLP-encoded signed bytes,
  // which requires the Sapphire precompile. Skip on Hardhat.

  it("produces signed Sapphire native release with correct fields", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name === "hardhat" || network.name === "unknown") this.skip();
    const { index, toAddress, amount, maxGasCost } = await enqueueSapphire();

    const signedTx: string =
      await accounting.resolveBridgeWithdrawal.staticCall(index);
    const parsed = ethers.Transaction.from(signedTx);

    expect(parsed.chainId).to.equal(SAPPHIRE_CHAIN_ID);
    expect(parsed.to?.toLowerCase()).to.equal(toAddress.toLowerCase());
    expect(parsed.value).to.equal(amount - maxGasCost);
    expect(parsed.gasLimit).to.equal(SAPPHIRE_GAS_LIMIT);
    expect(parsed.gasPrice).to.equal(SAPPHIRE_GAS_PRICE);
    expect(parsed.data).to.equal("0x");

    const recovered = parsed.from!.toLowerCase();
    expect(recovered).to.equal((await accounting.evmAddress()).toLowerCase());
    expect(recovered).to.equal(HH_ACCOUNT0.toLowerCase());
  });

  it("produces signed Base mint with deterministic withdrawalId", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name === "hardhat" || network.name === "unknown") this.skip();
    const { index, toAddress, amount, routeAddress } = await enqueueBase();

    const signedTx: string =
      await accounting.resolveBridgeWithdrawal.staticCall(index);
    const parsed = ethers.Transaction.from(signedTx);
    const accountingAddr = await accounting.getAddress();
    const expectedId = ethers.keccak256(
      ethers.AbiCoder.defaultAbiCoder().encode(
        ["address", "uint256", "uint256"],
        [accountingAddr, network.chainId, index],
      ),
    );

    expect(parsed.chainId).to.equal(BASE_CHAIN_ID);
    expect(parsed.to?.toLowerCase()).to.equal(routeAddress.toLowerCase());
    expect(parsed.value).to.equal(0n);
    expect(parsed.gasLimit).to.equal(BASE_MINT_GAS_LIMIT);
    expect(parsed.gasPrice).to.equal(BASE_GAS_PRICE);

    expect(parsed.data.slice(0, 10)).to.equal(MINT_SELECTOR);
    const iface = new ethers.Interface([
      "function mint(address,uint256,bytes32)",
    ]);
    const [decodedTo, decodedAmount, decodedId] = iface.decodeFunctionData(
      "mint",
      parsed.data,
    );
    expect(decodedTo).to.equal(toAddress);
    expect(decodedAmount).to.equal(amount);
    expect(decodedId).to.equal(expectedId);
  });

  it("Base resolve uses stored routeAddress (immune to setRoflBridge updates)", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name === "hardhat" || network.name === "unknown") this.skip();
    const { index, routeAddress } = await enqueueBase();
    // Mutate the configured bridge AFTER the request was queued.
    await accounting.setRoflBridge(BASE_CHAIN_ID, OTHER_ADDRESS);
    const signedTx: string =
      await accounting.resolveBridgeWithdrawal.staticCall(index);
    const parsed = ethers.Transaction.from(signedTx);
    expect(parsed.to?.toLowerCase()).to.equal(routeAddress.toLowerCase());
    expect(parsed.to?.toLowerCase()).to.not.equal(OTHER_ADDRESS.toLowerCase());
  });

  it("Sapphire resolve uses gasPrices[Sapphire]", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name === "hardhat" || network.name === "unknown") this.skip();
    // Re-pin gasPrices[Sapphire] to a value distinct from the beforeEach setup
    // so the test pins behavior rather than relying on the setup value.
    const pinnedGasPrice = 13n;
    await accounting.setGasPrice(SAPPHIRE_CHAIN_ID, pinnedGasPrice);
    const { index, amount, maxGasCost } = await enqueueSapphire();
    const signedTx: string =
      await accounting.resolveBridgeWithdrawal.staticCall(index);
    const parsed = ethers.Transaction.from(signedTx);
    expect(parsed.gasPrice).to.equal(pinnedGasPrice);
    expect(parsed.gasLimit).to.equal(SAPPHIRE_GAS_LIMIT);
    expect(parsed.value).to.equal(amount - maxGasCost);
  });

  it("idempotent: WithdrawalResolved emits only on first call; signed bytes equal", async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name === "hardhat" || network.name === "unknown") this.skip();
    const { index } = await enqueueBase();

    const tx1 = await accounting.resolveBridgeWithdrawal(index);
    const r1 = await tx1.wait();
    const ev1 = r1!.logs.filter(
      (l) =>
        l.topics[0] ===
        accounting.interface.getEvent("WithdrawalResolved")!.topicHash,
    );
    expect(ev1.length).to.equal(1);

    const signed1 =
      await accounting.resolveBridgeWithdrawal.staticCall(index);
    const tx2 = await accounting.resolveBridgeWithdrawal(index);
    const r2 = await tx2.wait();
    const ev2 = r2!.logs.filter(
      (l) =>
        l.topics[0] ===
        accounting.interface.getEvent("WithdrawalResolved")!.topicHash,
    );
    expect(ev2.length).to.equal(0);

    const signed2 =
      await accounting.resolveBridgeWithdrawal.staticCall(index);
    expect(signed2).to.equal(signed1);
  });
});
