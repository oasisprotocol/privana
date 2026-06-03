import { expect } from "chai";
import { Contract, Wallet, ZeroAddress } from "ethers";
import { ethers, config, upgrades } from "hardhat";
import { HardhatNetworkHDAccountsConfig } from "hardhat/types";
import type { MockAccounting, MockBridgeModule } from "../typechain-types";
import {
  deployBridgeModule,
  getCombinedAccountingAt,
  getCombinedSelectors,
  getLinkedAccountingFactory,
  wireHistoryModule,
} from "./util/links";
import { findSlot, readStorageLayout } from "./util/storage";

const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21);

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

// Storage scaffolding for step 03/01: BridgeBurnRequest struct + mapping +
// event live in `AccountingStorage`, and both `Accounting` and `BridgeModule`
// must agree on the slot index so `delegatecall` from the proxy lands on the
// same data. The full nonce-sequence test (reserveBridgeBurn) belongs to 03/02.
describe("BridgeBurnNonce — storage scaffolding", () => {
  describe("BridgeBurnRequest layout", () => {
    it("declares bridgeBurnRequests at the same slot in Accounting and BridgeModule", async () => {
      const acctSlot = await findSlot("Accounting", "bridgeBurnRequests");
      const moduleSlot = await findSlot("BridgeModule", "bridgeBurnRequests");
      expect(moduleSlot).to.equal(acctSlot);
    });

    it("AccountingStorage __gap is uint256[35] in both Accounting and BridgeModule", async () => {
      // Three __gap arrays exist in the merged layout (EVMSignerAndVerifier
      // [44], EIP712SignatureVerifier [42], AccountingStorage [35]). The
      // AccountingStorage one is the only [35] (shrunk from [36] when
      // `clearAppliedHash` was added at +13); it must appear in both contracts
      // at the same slot so delegatecall from the proxy stays aligned.
      const acctLayout = await readStorageLayout("Accounting");
      const moduleLayout = await readStorageLayout("BridgeModule");
      const acctGap = (acctLayout.storage as any[]).find(
        (e) => e.label === "__gap" && e.type === "t_array(t_uint256)35_storage",
      );
      expect(
        acctGap,
        "Accounting must declare a __gap of size 35 (AccountingStorage reserve shrunk as bridge + history-module + clearAppliedHash state was added)",
      ).to.not.equal(undefined);
      const moduleGap = (moduleLayout.storage as any[]).find(
        (e) =>
          e.label === "__gap" &&
          e.type === "t_array(t_uint256)35_storage" &&
          e.slot === acctGap.slot &&
          e.offset === acctGap.offset,
      );
      expect(
        moduleGap,
        "BridgeModule must mirror Accounting's [35] __gap at the same slot",
      ).to.not.equal(undefined);
    });

    it("missing BridgeBurnRequest reads as zero across all four slots through the proxy", async () => {
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      const proxyAddr = await accounting.getAddress();

      const slot = await findSlot("Accounting", "bridgeBurnRequests");
      const depositId = ethers.keccak256(
        ethers.toUtf8Bytes("test-deposit-unset"),
      );
      const baseSlot = BigInt(
        ethers.keccak256(
          ethers.AbiCoder.defaultAbiCoder().encode(
            ["bytes32", "uint256"],
            [depositId, slot],
          ),
        ),
      );

      // BridgeBurnRequest packs into 4 slots:
      //   +0 chainId (uint256)
      //   +1 bridge  (address, low 20 bytes)
      //   +2 amount  (uint256)
      //   +3 nonce (uint64) | exists (bool) packed
      // For an unset key all slots are zero, so exists=false implicitly.
      for (let i = 0; i < 4; i++) {
        const raw = await ethers.provider.getStorage(
          proxyAddr,
          ethers.toBeHex(baseSlot + BigInt(i), 32),
        );
        expect(BigInt(raw)).to.equal(
          0n,
          `bridgeBurnRequests[unset].slot[${i}] should be zero`,
        );
      }
    });
  });

  describe("BridgeBurnReserved event ABI", () => {
    it("event is declared on the merged Accounting/BridgeModule ABI with depositId indexed", async () => {
      const factory = await getLinkedAccountingFactory("MockAccounting");
      const frag = factory.interface.fragments.find(
        (f: any) => f.type === "event" && f.name === "BridgeBurnReserved",
      ) as any;
      expect(frag, "BridgeBurnReserved event must be declared").to.not.equal(
        undefined,
      );

      const inputs = frag.inputs;
      expect(inputs.length).to.equal(5);

      const expected = [
        { name: "depositId", type: "bytes32", indexed: true },
        { name: "chainId", type: "uint256", indexed: false },
        { name: "bridge", type: "address", indexed: false },
        { name: "amount", type: "uint256", indexed: false },
        { name: "nonce", type: "uint64", indexed: false },
      ];
      for (let i = 0; i < expected.length; i++) {
        expect(inputs[i].name).to.equal(expected[i].name);
        expect(inputs[i].type).to.equal(expected[i].type);
        expect(inputs[i].indexed).to.equal(expected[i].indexed);
      }
    });
  });
});

// ─── Runtime sequence ─────────────────────────────────────────────────────
//
// Mint/reserve interleave + idempotency + per-field reverts. Reuses the
// proxy+module deploy and ROSE configuration helpers from
// `AccountingBridgeModule.ts`. `MockBridgeModule` overrides the production
// `reserveBridgeBurn` to drop `onlyROFL` (the Sapphire `roflEnsureAuthorizedOrigin`
// precompile is unavailable on Hardhat) but reuses `_reserveBridgeBurn`, so the
// test path exercises the same body that production hits.

// Resolved at test-suite start from the live network. The contract treats
// `block.chainid` as the Sapphire-native release chain id; hardhat returns
// 31337, sapphire-localnet 23293, sapphire-testnet 23295. Hardhat cannot use
// a Sapphire chainId because `@oasisprotocol/sapphire-hardhat` auto-activates
// encrypted RPC on those ids and breaks the in-memory chain.
let SAPPHIRE_CHAIN_ID!: bigint;
const BASE_CHAIN_ID = 84532n;
const ROFL_BRIDGE = "0x000000000000000000000000000000000000c0fe";
const OTHER_BRIDGE = "0x000000000000000000000000000000000000d00d";
const TO_ADDRESS = "0x000000000000000000000000000000000000bEEF";
// Pinned literal — matches `ROSE_TOKEN_ID` in Accounting / BridgeModule.
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

// Decode the storage word at slot+3 of a `BridgeBurnRequest` mapping value.
// Layout (verified at `AccountingStorage.sol:95-103` and pinned by the
// `default-zero` test above): nonce (uint64) packed with exists (bool) into
// slot 3. `nonce` is the low 8 bytes, `exists` is byte 8 (the next byte up).
function decodeNonceAndExists(rawSlot: string): {
  nonce: bigint;
  exists: boolean;
} {
  const word = BigInt(rawSlot);
  const nonce = word & ((1n << 64n) - 1n);
  const exists = ((word >> 64n) & 0xffn) !== 0n;
  return { nonce, exists };
}

async function readBridgeBurnNonce(
  proxyAddr: string,
  depositId: string,
): Promise<{ nonce: bigint; exists: boolean }> {
  const slot = await findSlot("Accounting", "bridgeBurnRequests");
  const baseSlot = BigInt(
    ethers.keccak256(
      ethers.AbiCoder.defaultAbiCoder().encode(
        ["bytes32", "uint256"],
        [depositId, slot],
      ),
    ),
  );
  const raw = await ethers.provider.getStorage(
    proxyAddr,
    ethers.toBeHex(baseSlot + 3n, 32),
  );
  return decodeNonceAndExists(raw);
}

describe("BridgeBurnNonce — runtime sequence", () => {
  const depositA = ethers.id("burn-A");
  const depositB = ethers.id("burn-B");
  const amountA = 1_000n;
  const amountB = 2_000n;

  it("mint / reserve / mint sequence consumes nonces n, n+1, n+2 from the shared allocator", async () => {
    const [owner] = await ethers.getSigners();
    const { accounting, combined, proxyAddr } = await deployWiredCombined(
      owner.address,
    );
    await configureRose(combined);

    const userWallet = getUserWallet();
    await accounting.mockCreditDeposit(
      userWallet.address,
      ROSE_TOKEN_ID,
      INITIAL_BALANCE,
      ethers.id("burn-nonce-seed"),
    );

    const baseline = await accounting.nonces(BASE_CHAIN_ID);

    // Step 1: Base mint reserves nonce `n`.
    await signAndSubmitBaseRequest(combined, userWallet, amountA);
    expect(await accounting.nonces(BASE_CHAIN_ID)).to.equal(baseline + 1n);

    // Step 2: reserveBridgeBurn(depositA) reserves `n + 1`.
    await expect(
      (combined as any).reserveBridgeBurn(
        depositA,
        BASE_CHAIN_ID,
        ROFL_BRIDGE,
        amountA,
      ),
    )
      .to.emit(combined, "BridgeBurnReserved")
      .withArgs(
        depositA,
        BASE_CHAIN_ID,
        ethers.getAddress(ROFL_BRIDGE),
        amountA,
        baseline + 1n,
      );
    expect(await accounting.nonces(BASE_CHAIN_ID)).to.equal(baseline + 2n);

    // Stored nonce equals the freshly allocated one.
    const stored = await readBridgeBurnNonce(proxyAddr, depositA);
    expect(stored.exists).to.equal(true);
    expect(stored.nonce).to.equal(baseline + 1n);

    // Step 3: another Base mint reserves `n + 2`.
    await signAndSubmitBaseRequest(combined, userWallet, amountB);
    expect(await accounting.nonces(BASE_CHAIN_ID)).to.equal(baseline + 3n);

    // Sapphire nonce slot is untouched — confirms the allocator is per-chain
    // and the reserve path didn't accidentally touch a different chain.
    expect(await accounting.nonces(SAPPHIRE_CHAIN_ID)).to.equal(0n);
  });

  it("idempotent re-call with identical fields: no nonce bump, no second event", async () => {
    const [owner] = await ethers.getSigners();
    const { accounting, combined } = await deployWiredCombined(owner.address);
    await configureRose(combined);

    await (combined as any).reserveBridgeBurn(
      depositA,
      BASE_CHAIN_ID,
      ROFL_BRIDGE,
      amountA,
    );
    const noncesAfterFirst = await accounting.nonces(BASE_CHAIN_ID);

    await expect(
      (combined as any).reserveBridgeBurn(
        depositA,
        BASE_CHAIN_ID,
        ROFL_BRIDGE,
        amountA,
      ),
    ).to.not.emit(combined, "BridgeBurnReserved");
    expect(await accounting.nonces(BASE_CHAIN_ID)).to.equal(noncesAfterFirst);
  });

  it("mismatched amount on re-call reverts BridgeBurnMismatch(depositId)", async () => {
    const [owner] = await ethers.getSigners();
    const { combined, moduleContract } = await deployWiredCombined(
      owner.address,
    );
    await configureRose(combined);
    await (combined as any).reserveBridgeBurn(
      depositA,
      BASE_CHAIN_ID,
      ROFL_BRIDGE,
      amountA,
    );

    await expect(
      (combined as any).reserveBridgeBurn(
        depositA,
        BASE_CHAIN_ID,
        ROFL_BRIDGE,
        amountA + 1n,
      ),
    )
      .to.be.revertedWithCustomError(moduleContract, "BridgeBurnMismatch")
      .withArgs(depositA);
  });

  it("mismatched bridge on re-call reverts BridgeBurnMismatch(depositId)", async () => {
    const [owner] = await ethers.getSigners();
    const { combined, moduleContract } = await deployWiredCombined(
      owner.address,
    );
    await configureRose(combined);
    await (combined as any).reserveBridgeBurn(
      depositA,
      BASE_CHAIN_ID,
      ROFL_BRIDGE,
      amountA,
    );

    // Repoint the route. The first-call gate (`bridge == roflBridgeAddress[84532]`)
    // now passes for OTHER_BRIDGE, so the only thing standing between the
    // call and a silent overwrite is the all-fields-equal idempotency check.
    // Catches a regression where the check is collapsed to compare only
    // against the live `roflBridgeAddress[84532]` instead of the stored bridge.
    await (combined as any).setRoflBridge(BASE_CHAIN_ID, OTHER_BRIDGE);

    await expect(
      (combined as any).reserveBridgeBurn(
        depositA,
        BASE_CHAIN_ID,
        OTHER_BRIDGE,
        amountA,
      ),
    )
      .to.be.revertedWithCustomError(moduleContract, "BridgeBurnMismatch")
      .withArgs(depositA);
  });

  it("unregistered destination chainId reverts RoflBridgeNotSet(chainId)", async () => {
    const [owner] = await ethers.getSigners();
    const { combined, moduleContract } = await deployWiredCombined(
      owner.address,
    );
    await configureRose(combined);

    await expect(
      (combined as any).reserveBridgeBurn(
        depositB,
        SAPPHIRE_CHAIN_ID,
        ROFL_BRIDGE,
        amountA,
      ),
    )
      .to.be.revertedWithCustomError(moduleContract, "RoflBridgeNotSet")
      .withArgs(SAPPHIRE_CHAIN_ID);
  });

  it("unset roflBridgeAddress[84532] reverts RoflBridgeNotSet(84532)", async () => {
    const [owner] = await ethers.getSigners();
    // No configureRose — the route is left unset.
    const { combined, moduleContract } = await deployWiredCombined(
      owner.address,
    );

    await expect(
      (combined as any).reserveBridgeBurn(
        depositB,
        BASE_CHAIN_ID,
        ROFL_BRIDGE,
        amountA,
      ),
    )
      .to.be.revertedWithCustomError(moduleContract, "RoflBridgeNotSet")
      .withArgs(BASE_CHAIN_ID);
  });

  it("bridge != roflBridgeAddress[84532] (first reservation) reverts InvalidRouteAddress", async () => {
    const [owner] = await ethers.getSigners();
    const { combined, moduleContract } = await deployWiredCombined(
      owner.address,
    );
    await configureRose(combined);

    await expect(
      (combined as any).reserveBridgeBurn(
        depositB,
        BASE_CHAIN_ID,
        OTHER_BRIDGE,
        amountA,
      ),
    ).to.be.revertedWithCustomError(moduleContract, "InvalidRouteAddress");
  });

  it("amount == 0 reverts InvalidAmount", async () => {
    const [owner] = await ethers.getSigners();
    const { combined, moduleContract } = await deployWiredCombined(
      owner.address,
    );
    await configureRose(combined);

    await expect(
      (combined as any).reserveBridgeBurn(
        depositB,
        BASE_CHAIN_ID,
        ROFL_BRIDGE,
        0n,
      ),
    ).to.be.revertedWithCustomError(moduleContract, "InvalidAmount");
  });

  it("depositId == bytes32(0) reverts InvalidDepositId", async () => {
    const [owner] = await ethers.getSigners();
    const { combined, moduleContract } = await deployWiredCombined(
      owner.address,
    );
    await configureRose(combined);

    await expect(
      (combined as any).reserveBridgeBurn(
        ethers.ZeroHash,
        BASE_CHAIN_ID,
        ROFL_BRIDGE,
        amountA,
      ),
    ).to.be.revertedWithCustomError(moduleContract, "InvalidDepositId");
  });

  it("BridgeModuleNotSet reaches reserveBridgeBurn through fallback (selector wired)", async () => {
    // Deploy proxy with NO bridge module set; hand-roll calldata for
    // reserveBridgeBurn. Expecting `BridgeModuleNotSet` proves the selector
    // entered the routing branch — i.e. the fallback allowlist edit landed.
    // Mirrors the existing `setRoflBridge` test in
    // `AccountingBridgeModule.ts:199-218`.
    const [owner] = await ethers.getSigners();
    const accounting = await deployAccountingProxy(owner.address);

    const reserveSelector = ethers
      .id("reserveBridgeBurn(bytes32,uint256,address,uint256)")
      .slice(0, 10);
    const args = ethers.AbiCoder.defaultAbiCoder().encode(
      ["bytes32", "uint256", "address", "uint256"],
      [depositA, BASE_CHAIN_ID, ROFL_BRIDGE, amountA],
    );
    const calldata = reserveSelector + args.slice(2);

    await expect(
      owner.sendTransaction({
        to: await accounting.getAddress(),
        data: calldata,
      }),
    ).to.be.revertedWithCustomError(accounting, "BridgeModuleNotSet");
  });

  it("merged-ABI: reserveBridgeBurn selector is unique and present in BridgeModule", async () => {
    const selector = ethers
      .id("reserveBridgeBurn(bytes32,uint256,address,uint256)")
      .slice(0, 10);
    const merged = await getCombinedSelectors([
      "MockAccounting",
      "MockBridgeModule",
    ]);
    const matches = merged.filter((s) => s.selector === selector);
    expect(matches.length).to.equal(
      1,
      "reserveBridgeBurn selector must appear exactly once in the merged ABI",
    );
    expect(matches[0].signature).to.equal(
      "reserveBridgeBurn(bytes32,uint256,address,uint256)",
    );
  });

  it("production reserveBridgeBurn carries onlyROFL — real BridgeModule reverts on Hardhat", async () => {
    // Wires the *real* `BridgeModule` (not the mock that drops onlyROFL) as
    // the proxy's module pointer and sends calldata. The real path hits
    // `Subcall.roflEnsureAuthorizedOrigin`, which has no precompile on
    // Hardhat and reverts. Generic `.to.be.reverted` keeps the assertion
    // robust to whatever shape the precompile-absent revert takes.
    const [owner] = await ethers.getSigners();
    const accounting = await deployAccountingProxy(owner.address);
    const realModule = await deployBridgeModule("BridgeModule");
    await accounting.setBridgeModule(await realModule.getAddress());
    const combined = await getCombinedAccountingAt(
      await accounting.getAddress(),
      owner,
    );

    await expect(
      (combined as any).reserveBridgeBurn(
        depositA,
        BASE_CHAIN_ID,
        ROFL_BRIDGE,
        amountA,
      ),
    ).to.be.reverted;
  });
});

// ─── Sign sequence ────────────────────────────────────────────────────────
//
// `generateBridgeBurnTransfer(bytes32 depositId)` signs a Base-Sepolia burn
// tx whose every field — destination, calldata, chain, nonce — is derived
// from the stored `BridgeBurnRequest`. On Hardhat `EIP155Signer.sign`
// reverts with `DER_Split_Error` because the Sapphire precompile is absent
// (mirrors `AccountingBridgeModule.ts:660-674`), so the "successful sign"
// path is pinned by reaching that revert rather than parsing bytes. All
// pre-sign reverts are checkable directly.

describe("BridgeBurnNonce — sign sequence", () => {
  const depositA = ethers.id("burn-sign-A");
  const BASE_GAS_PRICE = 1_000_000_000n; // 1 gwei

  async function reserveDepositA(
    combined: Contract,
    accounting: MockAccounting,
  ): Promise<void> {
    await (combined as any).reserveBridgeBurn(
      depositA,
      BASE_CHAIN_ID,
      ROFL_BRIDGE,
      1_000n,
    );
    await accounting.setGasPrice(BASE_CHAIN_ID, BASE_GAS_PRICE);
  }

  it("reaches EIP155Signer.sign and reverts DER_Split_Error on Hardhat", async () => {
    // Full setup → reservation present, gas price set, ROFL signer wired.
    // The body must traverse the BridgeBurnNotFound and GasPriceNotSet gates
    // and reach the sign call, which reverts on Hardhat (no precompile).
    // Mirrors AccountingBridgeModule.ts:660-674 for the sweep selector.
    const [owner] = await ethers.getSigners();
    const { accounting, combined } = await deployWiredCombined(owner.address);
    await configureRose(combined);
    await reserveDepositA(combined, accounting);
    await accounting.mockSetRoflSignerAddress(owner.address);

    await expect(
      (combined as any).generateBridgeBurnTransfer.staticCall(depositA),
    ).to.be.revertedWithCustomError(combined, "DER_Split_Error");
  });

  it("missing reservation reverts BridgeBurnNotFound(depositId)", async () => {
    // No reserveBridgeBurn — bridgeBurnRequests[depositA].exists is false.
    // BridgeBurnNotFound fires before the gas-price gate, so this stays a
    // pure pre-sign revert even with no gas price set.
    const [owner] = await ethers.getSigners();
    const { accounting, combined, moduleContract } = await deployWiredCombined(
      owner.address,
    );
    await configureRose(combined);
    await accounting.mockSetRoflSignerAddress(owner.address);

    await expect(
      (combined as any).generateBridgeBurnTransfer.staticCall(depositA),
    )
      .to.be.revertedWithCustomError(moduleContract, "BridgeBurnNotFound")
      .withArgs(depositA);
  });

  it("gasPrices[84532] == 0 reverts GasPriceNotSet(84532)", async () => {
    // Reservation present but gas price unset — the ordering BridgeBurnNotFound
    // → GasPriceNotSet → sign is pinned by this test together with the one above.
    const [owner] = await ethers.getSigners();
    const { accounting, combined } = await deployWiredCombined(owner.address);
    await configureRose(combined);
    await (combined as any).reserveBridgeBurn(
      depositA,
      BASE_CHAIN_ID,
      ROFL_BRIDGE,
      1_000n,
    );
    await accounting.mockSetRoflSignerAddress(owner.address);

    await expect(
      (combined as any).generateBridgeBurnTransfer.staticCall(depositA),
    )
      .to.be.revertedWithCustomError(combined, "GasPriceNotSet")
      .withArgs(BASE_CHAIN_ID);
  });

  it("onlyROFLQuery: rejects unset roflSignerAddress with RoflSignerNotSet", async () => {
    // No mockSetRoflSignerAddress — the outermost gate must fire before
    // any storage read in the body. Mirrors
    // AccountingBridgeModule.ts:567-588 for the sweep selector.
    const [owner] = await ethers.getSigners();
    const { accounting, combined } = await deployWiredCombined(owner.address);
    await configureRose(combined);
    await reserveDepositA(combined, accounting);

    await expect(
      (combined as any).generateBridgeBurnTransfer.staticCall(depositA),
    ).to.be.revertedWithCustomError(combined, "RoflSignerNotSet");
  });

  it("onlyROFLQuery: rejects wrong sender with NotAuthorizedROFL", async () => {
    // ROFL signer is owner; call from `other` must trip the modifier.
    // Mirrors AccountingBridgeModule.ts:590-613 for the sweep selector.
    const [owner, other] = await ethers.getSigners();
    const { accounting, combined, proxyAddr } = await deployWiredCombined(
      owner.address,
    );
    await configureRose(combined);
    await reserveDepositA(combined, accounting);
    await accounting.mockSetRoflSignerAddress(owner.address);

    const otherCombined = await getCombinedAccountingAt(proxyAddr, other);
    await expect(
      (otherCombined as any).generateBridgeBurnTransfer.staticCall(depositA),
    ).to.be.revertedWithCustomError(otherCombined, "NotAuthorizedROFL");
  });

  it("BridgeModuleNotSet reaches generateBridgeBurnTransfer through fallback (selector wired)", async () => {
    // Deploy proxy with NO bridge module; expecting `BridgeModuleNotSet`
    // proves the selector entered the routing branch — i.e. the
    // fallback-allowlist edit landed for this step.
    const [owner] = await ethers.getSigners();
    const accounting = await deployAccountingProxy(owner.address);

    const selector = ethers
      .id("generateBridgeBurnTransfer(bytes32)")
      .slice(0, 10);
    const args = ethers.AbiCoder.defaultAbiCoder().encode(
      ["bytes32"],
      [depositA],
    );
    const calldata = selector + args.slice(2);

    await expect(
      owner.sendTransaction({
        to: await accounting.getAddress(),
        data: calldata,
      }),
    ).to.be.revertedWithCustomError(accounting, "BridgeModuleNotSet");
  });

  it("merged-ABI: generateBridgeBurnTransfer selector unique with exactly one bytes32 arg", async () => {
    // The on-chain proof that no overload accepting external
    // amount/bridge/chain/nonce exists: exactly one fragment, one bytes32 input.
    const selector = ethers
      .id("generateBridgeBurnTransfer(bytes32)")
      .slice(0, 10);
    const merged = await getCombinedSelectors([
      "MockAccounting",
      "MockBridgeModule",
    ]);
    const matches = merged.filter((s) => s.selector === selector);
    expect(matches.length).to.equal(
      1,
      "generateBridgeBurnTransfer selector must appear exactly once in the merged ABI",
    );
    expect(matches[0].signature).to.equal(
      "generateBridgeBurnTransfer(bytes32)",
    );
  });
});
