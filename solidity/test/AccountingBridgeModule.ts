import { expect } from "chai";
import { ethers, config, upgrades } from "hardhat";
import { Contract, Wallet, ZeroAddress } from "ethers";
import { HardhatNetworkHDAccountsConfig } from "hardhat/types";
import { MockAccounting, MockBridgeModule } from "../typechain-types";
import {
  deployBridgeModule,
  getCombinedAccountingAt,
  getCombinedSelectors,
  getLinkedAccountingFactory,
  wireHistoryModule,
} from "./util/links";
import { findSlot } from "./util/storage";

const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21);

// Pinned literal: bytes32(uint256(keccak256("flexvaults.accounting.bridgeModule")) - 1).
// Recomputed in test below for guard, but kept here for reference.
const EXPECTED_SLOT = ethers.toBeHex(
  BigInt(
    ethers.keccak256(ethers.toUtf8Bytes("flexvaults.accounting.bridgeModule")),
  ) - 1n,
  32,
);

// ERC-1967 reference slots (OZ's UUPS layout).
const ERC1967_IMPLEMENTATION_SLOT = ethers.toBeHex(
  BigInt(ethers.keccak256(ethers.toUtf8Bytes("eip1967.proxy.implementation"))) -
    1n,
  32,
);
const ERC1967_ADMIN_SLOT = ethers.toBeHex(
  BigInt(ethers.keccak256(ethers.toUtf8Bytes("eip1967.proxy.admin"))) - 1n,
  32,
);

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

describe("Accounting bridge module dispatcher", () => {
  describe("setBridgeModule admin", () => {
    it("reverts when caller is not the owner", async () => {
      const [owner, other] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      const moduleContract = await deployBridgeModule();
      await expect(
        accounting
          .connect(other)
          .setBridgeModule(await moduleContract.getAddress()),
      ).to.be.revertedWithCustomError(accounting, "OwnableUnauthorizedAccount");
    });

    it("reverts on address(0)", async () => {
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      await expect(
        accounting.setBridgeModule(ZeroAddress),
      ).to.be.revertedWithCustomError(accounting, "InvalidAddress");
    });

    it("reverts on EOA (code.length == 0)", async () => {
      const [owner, eoa] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      await expect(
        accounting.setBridgeModule(eoa.address),
      ).to.be.revertedWithCustomError(accounting, "BridgeModuleNotContract");
    });

    it("emits BridgeModuleSet and bridgeModule() reads back the address", async () => {
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      const moduleContract = await deployBridgeModule();
      const moduleAddr = await moduleContract.getAddress();

      await expect(accounting.setBridgeModule(moduleAddr))
        .to.emit(accounting, "BridgeModuleSet")
        .withArgs(moduleAddr);

      expect(await accounting.bridgeModule()).to.equal(moduleAddr);
    });

    it("replacing the module routes future calls to the new address", async () => {
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      const first = await deployBridgeModule();
      const second = await deployBridgeModule();

      await accounting.setBridgeModule(await first.getAddress());
      expect(await accounting.bridgeModule()).to.equal(
        await first.getAddress(),
      );

      await accounting.setBridgeModule(await second.getAddress());
      expect(await accounting.bridgeModule()).to.equal(
        await second.getAddress(),
      );
    });
  });

  describe("storage slot derivation", () => {
    it("bridge module pointer lives at the documented unstructured slot", async () => {
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      const moduleContract = await deployBridgeModule();
      const moduleAddr = await moduleContract.getAddress();

      await accounting.setBridgeModule(moduleAddr);

      const raw = await ethers.provider.getStorage(
        await accounting.getAddress(),
        EXPECTED_SLOT,
      );
      // sstore packs an address into the lower 20 bytes; compare canonicalized.
      expect(ethers.getAddress("0x" + raw.slice(-40))).to.equal(moduleAddr);
    });

    it("bridge module slot does not collide with ERC-1967 implementation/admin slots", () => {
      expect(EXPECTED_SLOT).to.not.equal(ERC1967_IMPLEMENTATION_SLOT);
      expect(EXPECTED_SLOT).to.not.equal(ERC1967_ADMIN_SLOT);
    });
  });

  describe("fallback dispatcher", () => {
    it("reverts UnknownSelector(sig) on an unmatched selector", async () => {
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      const proxyAddr = await accounting.getAddress();

      // Hand-rolled selector for a function the contract does not expose.
      const unknownSig = ethers.id("definitelyNotAFunction()").slice(0, 10);
      await expect(owner.sendTransaction({ to: proxyAddr, data: unknownSig }))
        .to.be.revertedWithCustomError(accounting, "UnknownSelector")
        .withArgs(unknownSig);
    });

    it("no resident Accounting selector shadows a routed bridge selector", async () => {
      // If a future Accounting helper is added with a 4-byte selector that
      // happens to equal a routed bridge selector, Solidity dispatch in the
      // proxy would shadow the routed call (fallback only fires on
      // unmatched selectors). The fallback's allowlist would then be
      // silently bypassed.
      const accountingFactory =
        await getLinkedAccountingFactory("MockAccounting");
      const bridgeFactory =
        await getLinkedAccountingFactory("MockBridgeModule");
      // Source of truth: every function declared on IBridgeModule is routed
      // through the fallback. Hard-coding the list here historically drifted
      // (added selectors never reached the shadow check); deriving from the
      // interface keeps the test in lockstep with the real allowlist target.
      const ifaceArtifact = await (
        await import("hardhat")
      ).artifacts.readArtifact("IBridgeModule");
      const ifaceInterface = new ethers.Interface(ifaceArtifact.abi);
      const routed = new Set<string>(
        ifaceInterface.fragments
          .filter((f: any) => f.type === "function")
          .map((f: any) => ethers.id(f.format("sighash")).slice(0, 10)),
      );
      const residentSelectors = accountingFactory.interface.fragments
        .filter((f: any) => f.type === "function")
        .map((f: any) => f.format("sighash"))
        .map((s: string) => ethers.id(s).slice(0, 10));
      for (const sig of residentSelectors) {
        expect(
          routed.has(sig),
          `Resident Accounting selector ${sig} shadows a routed bridge selector`,
        ).to.equal(false);
      }
      // Also assert that the routed selectors actually exist in BridgeModule.
      const moduleSelectors = new Set<string>(
        bridgeFactory.interface.fragments
          .filter((f: any) => f.type === "function")
          .map((f: any) => ethers.id(f.format("sighash")).slice(0, 10)),
      );
      for (const sig of routed) {
        expect(
          moduleSelectors.has(sig),
          `Routed selector ${sig} not present in BridgeModule`,
        ).to.equal(true);
      }
    });

    it("reverts BridgeModuleNotSet when a routed selector hits the dispatcher with module unset", async () => {
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      // Module pointer deliberately left unset. setRoflBridge is one of the
      // routed selectors; hand-rolled calldata forces the fallback path.
      const setRoflBridgeSelector = ethers
        .id("setRoflBridge(uint256,address)")
        .slice(0, 10);
      const args = ethers.AbiCoder.defaultAbiCoder().encode(
        ["uint256", "address"],
        [84532n, ethers.Wallet.createRandom().address],
      );
      const calldata = setRoflBridgeSelector + args.slice(2);
      await expect(
        owner.sendTransaction({
          to: await accounting.getAddress(),
          data: calldata,
        }),
      ).to.be.revertedWithCustomError(accounting, "BridgeModuleNotSet");
    });

    it("merged-ABI selector uniqueness — no resident + routed selector hash-collision", async () => {
      // Mirrors the Python `test_no_duplicate_function_selectors` regression
      // on the TS side. Runs on the MERGED fragment set (output of
      // `mergeFragments`) so identical inherited fragments (e.g.
      // `renounceOwnership` declared in `AccountingStorage` and surfaced by
      // both factories) are collapsed before grouping. The gate fires only
      // on genuine 4-byte hash collisions between distinct signatures.
      const selectors = await getCombinedSelectors([
        "MockAccounting",
        "MockBridgeModule",
      ]);
      const grouped = new Map<string, string[]>();
      for (const s of selectors) {
        const list = grouped.get(s.selector) ?? [];
        list.push(s.signature);
        grouped.set(s.selector, list);
      }
      for (const [selector, signatures] of grouped) {
        const distinct = new Set(signatures);
        if (distinct.size > 1) {
          expect.fail(
            `selector collision at ${selector}: ` +
              `${[...distinct].join(" / ")} all hash to the same 4 bytes`,
          );
        }
      }
    });
  });
});

// ─── delegated-path security & roundtrip ─────────────────────────────────
//
// Tests below exercise the proxy + delegated BridgeModule path on Hardhat.
// Where a primitive requires the Sapphire `EIP155Signer.sign` precompile
// (i.e. the actual signed-tx production in `resolveBridgeWithdrawal`), the
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
// Pinned literal — matches the `ROSE_TOKEN_ID` constant in Accounting / BridgeModule.
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
  await wireHistoryModule(accounting);
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

async function signAndSubmitBaseRequest(
  combined: Contract,
  userWallet: Wallet,
  amount: bigint,
): Promise<bigint> {
  const domain = await getDomain(combined);
  const nonce = await (combined as any).withdrawalNonces(userWallet.address);
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
  await (combined as any).requestBridgeWithdrawal(
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

describe("Accounting bridge module: delegated-path security & roundtrip", () => {
  describe("secret + signer isolation", () => {
    it("MockBridgeModule has no public secretKey getter", async () => {
      const factory = await getLinkedAccountingFactory("MockBridgeModule");
      const fns = factory.interface.fragments.filter(
        (f: any) => f.type === "function",
      );
      const secretKeyGetter = fns.find((f: any) => f.name === "secretKey");
      expect(
        secretKeyGetter,
        "BridgeModule must not expose secretKey via the ABI",
      ).to.equal(undefined);
    });

    it("secretKey is proxy-resident; module-address storage is empty", async () => {
      const [owner] = await ethers.getSigners();
      const { proxyAddr, moduleAddr, moduleContract } =
        await deployWiredCombined(owner.address);

      // Resolve the slot via the storage layout helper — never hard-code.
      // `secretKey` is declared on EVMSignerAndVerifier and inherited by
      // both Accounting and BridgeModule, so the slot index matches.
      const slot = await findSlot("Accounting", "secretKey");
      const slotHex = ethers.toBeHex(slot, 32);

      const proxySecret = await ethers.provider.getStorage(proxyAddr, slotHex);
      const moduleSecret = await ethers.provider.getStorage(
        moduleAddr,
        slotHex,
      );

      expect(proxySecret).to.not.equal(
        ethers.ZeroHash,
        "proxy `secretKey` slot must be initialized",
      );
      expect(moduleSecret).to.equal(
        ethers.ZeroHash,
        "module-address `secretKey` slot must remain empty",
      );

      // Direct access to the module address must not leak proxy identity.
      expect(await moduleContract.evmAddress()).to.equal(ZeroAddress);
    });

    it("delegated-path signer recovers to evmAddress() (off-chain derivation pin)", async () => {
      // Pins the delegatecall invariant without invoking the Sapphire
      // `EIP155Signer.sign` precompile (unavailable on Hardhat). Off-chain-
      // derive the address from the proxy-resident secret; assert it equals
      // the published `evmAddress()`. Any signature BridgeModule produces
      // inside delegatecall reads from the same slot via shared storage, so
      // recovery is equivalent.
      const [owner] = await ethers.getSigners();
      const { accounting, proxyAddr } = await deployWiredCombined(
        owner.address,
      );

      const slot = await findSlot("Accounting", "secretKey");
      const secret = await ethers.provider.getStorage(
        proxyAddr,
        ethers.toBeHex(slot, 32),
      );

      const derivedAddr = new ethers.Wallet(secret).address;
      expect(derivedAddr.toLowerCase()).to.equal(
        (await accounting.evmAddress()).toLowerCase(),
      );
    });
  });

  describe("storage roundtrip", () => {
    it("module → accounting: requestBridgeWithdrawal write is visible to non-bridge reads", async () => {
      const [owner] = await ethers.getSigners();
      const { combined } = await deployWiredCombined(owner.address);
      await configureRose(combined);

      const userWallet = getUserWallet();
      await (combined as any).mockCreditDeposit(
        userWallet.address,
        ROSE_TOKEN_ID,
        INITIAL_BALANCE,
        ethers.id("roundtrip-mod-to-acct"),
      );

      const beforeLedger = await (combined as any).ledgerTotalOf(ROSE_TOKEN_ID);
      const beforeCount = await (combined as any).withdrawalCount();
      const amount = 100_000n;

      await signAndSubmitBaseRequest(combined, userWallet, amount);

      // Accounting-side reads must observe BridgeModule-side state changes.
      expect(await (combined as any).ledgerTotalOf(ROSE_TOKEN_ID)).to.equal(
        beforeLedger - amount,
      );
      expect(await (combined as any).withdrawalCount()).to.equal(
        beforeCount + 1n,
      );
      const queued = await (combined as any).withdrawals(beforeCount);
      expect(queued.userAddress).to.equal(userWallet.address);
      expect(queued.toAddress).to.equal(TO_ADDRESS);
      expect(queued.amount).to.equal(amount);
      expect(queued.tokenId).to.equal(ROSE_TOKEN_ID);
      expect(queued.resolved).to.equal(false);
    });

    it("accounting → module: creditDeposit is visible to BridgeModule's ledger read", async () => {
      const [owner] = await ethers.getSigners();
      const { combined } = await deployWiredCombined(owner.address);
      await configureRose(combined);

      const userWallet = getUserWallet();
      // Accounting-resident write path bumps `_ledgerTotal` via _creditDeposit.
      const seeded = INITIAL_BALANCE;
      await (combined as any).mockCreditDeposit(
        userWallet.address,
        ROSE_TOKEN_ID,
        seeded,
        ethers.id("roundtrip-acct-to-mod"),
      );

      // BridgeModule's `requestBridgeWithdrawal` reads `_ledgerTotal` via
      // `BridgeLib.validateBridgeWithdrawal`. If the read returned a stale
      // (zero) value, the underflow guard in `_decreaseLedgerTotal` would
      // revert. A successful request for `seeded` proves the module read
      // matches the Accounting-side write.
      await signAndSubmitBaseRequest(combined, userWallet, seeded);
      expect(await (combined as any).ledgerTotalOf(ROSE_TOKEN_ID)).to.equal(0n);
    });

    it("setRoflBridge: zero address reverts InvalidAddress (write-time fail-closed)", async () => {
      const [owner] = await ethers.getSigners();
      const { combined } = await deployWiredCombined(owner.address);
      await expect(
        (combined as any).setRoflBridge(BASE_CHAIN_ID, ZeroAddress),
      ).to.be.revertedWithCustomError(combined, "InvalidAddress");
    });
  });

  describe("nested call invariants", () => {
    it("requestBridgeWithdrawal: signer + module pointer unchanged; destChain nonce increments by 1; other-chain nonces unchanged; ledger decremented exactly", async () => {
      // Pins the delegatecall side-effects of `requestBridgeWithdrawal`.
      // The body allocates a *destination-chain* nonce via
      // `getEVMNonceAndIncrement(destChainId)` (used by the resolver to
      // build a deterministic txIdentifier), then decrements
      // `_ledgerTotal[ROSE_TOKEN_ID]`. It does NOT touch the custody EOA,
      // the bridge-module pointer, or unrelated `nonces[*]` entries —
      // that's the structural invariant this test pins.
      const [owner] = await ethers.getSigners();
      const { accounting, combined, proxyAddr, moduleAddr } =
        await deployWiredCombined(owner.address);
      await configureRose(combined);

      const userWallet = getUserWallet();
      await (combined as any).mockCreditDeposit(
        userWallet.address,
        ROSE_TOKEN_ID,
        INITIAL_BALANCE,
        ethers.id("nested-invariants-seed"),
      );

      // `nonces` is the public mapping on EVMSignerAndVerifier
      // (no `getEVMNonce` shim — verified via codex review).
      const beforeEvm = await accounting.evmAddress();
      const beforeBaseNonce = await accounting.nonces(BASE_CHAIN_ID);
      const beforeSapphireNonce = await accounting.nonces(SAPPHIRE_CHAIN_ID);
      const beforeModulePtr = await ethers.provider.getStorage(
        proxyAddr,
        EXPECTED_SLOT,
      );
      const beforeLedger = await (combined as any).ledgerTotalOf(ROSE_TOKEN_ID);

      const amount = 100_000n;
      await signAndSubmitBaseRequest(combined, userWallet, amount);

      expect(await accounting.evmAddress()).to.equal(beforeEvm);
      // destChain nonce: incremented by exactly one allocation.
      expect(await accounting.nonces(BASE_CHAIN_ID)).to.equal(
        beforeBaseNonce + 1n,
      );
      // unrelated-chain nonces must remain untouched.
      expect(await accounting.nonces(SAPPHIRE_CHAIN_ID)).to.equal(
        beforeSapphireNonce,
      );
      // Module pointer slot — raw and via the resident getter.
      expect(
        await ethers.provider.getStorage(proxyAddr, EXPECTED_SLOT),
      ).to.equal(beforeModulePtr);
      expect(await accounting.bridgeModule()).to.equal(moduleAddr);
      expect(await (combined as any).ledgerTotalOf(ROSE_TOKEN_ID)).to.equal(
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
      const { combined } = await deployWiredCombined(owner.address);
      await configureRose(combined);

      await expect(
        (combined as any).generateSweepERC20TransferToBridge.staticCall(
          BENEFICIARY,
          CHAIN_TYPE_EVM,
          VERSION,
          BASE_CHAIN_ID,
          TOKEN_ADDRESS,
          AMOUNT,
          NONCE,
          GAS_PRICE,
        ),
      ).to.be.revertedWithCustomError(combined, "RoflSignerNotSet");
    });

    it("rejects wrong sender with NotAuthorizedROFL", async () => {
      const [owner, other] = await ethers.getSigners();
      const { accounting, combined, proxyAddr } = await deployWiredCombined(
        owner.address,
      );
      await configureRose(combined);
      await accounting.mockSetRoflSignerAddress(owner.address);

      const otherCombined = await getCombinedAccountingAt(proxyAddr, other);
      await expect(
        (otherCombined as any).generateSweepERC20TransferToBridge.staticCall(
          BENEFICIARY,
          CHAIN_TYPE_EVM,
          VERSION,
          BASE_CHAIN_ID,
          TOKEN_ADDRESS,
          AMOUNT,
          NONCE,
          GAS_PRICE,
        ),
      ).to.be.revertedWithCustomError(otherCombined, "NotAuthorizedROFL");
    });

    it("rejects unregistered chainId with RoflBridgeNotSet", async () => {
      const [owner] = await ethers.getSigners();
      const { accounting, combined } = await deployWiredCombined(owner.address);
      await configureRose(combined);
      await accounting.mockSetRoflSignerAddress(owner.address);

      const unregisteredChain = 1n;
      await expect(
        (combined as any).generateSweepERC20TransferToBridge.staticCall(
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
        .to.be.revertedWithCustomError(combined, "RoflBridgeNotSet")
        .withArgs(unregisteredChain);
    });

    it("rejects unset roflBridgeAddress[84532] with RoflBridgeNotSet", async () => {
      // ROSE token registered, ROFL signer wired, but the Base route is unset
      // (skip configureRose's setRoflBridge step).
      const [owner] = await ethers.getSigners();
      const { accounting, combined } = await deployWiredCombined(owner.address);
      const roseData = await (combined as any).encodeBridgeAssetTokenData(
        "ROSE",
      );
      await (combined as any).setTokenInfo({
        tokenType: TokenType.BridgeAsset,
        data: roseData,
      });
      await accounting.mockSetRoflSignerAddress(owner.address);

      await expect(
        (combined as any).generateSweepERC20TransferToBridge.staticCall(
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
        .to.be.revertedWithCustomError(combined, "RoflBridgeNotSet")
        .withArgs(BASE_CHAIN_ID);
    });

    it("reaches sign and reverts DER_Split_Error on Hardhat", async function () {
      // Proves dispatcher routing reaches the BridgeModule body — only that
      // body invokes EIP155Signer.sign for this selector.
      const network = await ethers.provider.getNetwork();
      if (network.name !== "hardhat" && network.name !== "unknown") this.skip();

      const [owner] = await ethers.getSigners();
      const { accounting, combined } = await deployWiredCombined(owner.address);
      await configureRose(combined);
      await accounting.mockSetRoflSignerAddress(owner.address);

      await expect(
        (combined as any).generateSweepERC20TransferToBridge.staticCall(
          BENEFICIARY,
          CHAIN_TYPE_EVM,
          VERSION,
          BASE_CHAIN_ID,
          TOKEN_ADDRESS,
          AMOUNT,
          NONCE,
          GAS_PRICE,
        ),
      ).to.be.revertedWithCustomError(combined, "DER_Split_Error");
    });
  });
});
