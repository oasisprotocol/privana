import { expect } from "chai";
import { Contract, Wallet } from "ethers";
import { ethers, config, upgrades } from "hardhat";
import { HardhatNetworkHDAccountsConfig } from "hardhat/types";
import type { MockAccounting } from "../typechain-types";
import { attachAccounting } from "./util/links";
import { findSlot } from "./util/storage";

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

// Storage scaffolding: the BridgeBurnRequest struct + mapping + event are
// resident on `Accounting`. The mapping value packs into four consecutive
// slots; the full nonce-sequence behavior is exercised by the runtime suite.
describe("BridgeBurnNonce — storage scaffolding", () => {
  before(async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
  });

  describe("BridgeBurnRequest layout", () => {
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
    it("event is declared on the Accounting ABI with depositId indexed", async () => {
      const factory = await ethers.getContractFactory("MockAccounting");
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
// Mint/reserve interleave + idempotency + per-field reverts. `MockAccounting`
// overrides the production `reserveBridgeBurn` to drop `onlyROFL` (the Sapphire
// `roflEnsureAuthorizedOrigin` precompile is unavailable on Hardhat) but reuses
// `_reserveBridgeBurn`, so the test path exercises the same body that
// production hits.

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
// Pinned literal — matches `ROSE_TOKEN_ID` in Accounting.
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
}> {
  const accounting = await deployAccountingProxy(ownerAddress);
  const proxyAddr = await accounting.getAddress();
  const [signer] = await ethers.getSigners();
  const combined = await attachAccounting(proxyAddr, signer);
  return { accounting, combined, proxyAddr };
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
// slot 3 packs nonce (uint64, low 8 bytes) with exists (bool, byte 8).
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
  before(async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
  });

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
    const { combined } = await deployWiredCombined(owner.address);
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
      .to.be.revertedWithCustomError(combined, "BridgeBurnMismatch")
      .withArgs(depositA);
  });

  it("mismatched bridge on re-call reverts BridgeBurnMismatch(depositId)", async () => {
    const [owner] = await ethers.getSigners();
    const { combined } = await deployWiredCombined(owner.address);
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
      .to.be.revertedWithCustomError(combined, "BridgeBurnMismatch")
      .withArgs(depositA);
  });

  it("unregistered destination chainId reverts RoflBridgeNotSet(chainId)", async () => {
    const [owner] = await ethers.getSigners();
    const { combined } = await deployWiredCombined(owner.address);
    await configureRose(combined);

    await expect(
      (combined as any).reserveBridgeBurn(
        depositB,
        SAPPHIRE_CHAIN_ID,
        ROFL_BRIDGE,
        amountA,
      ),
    )
      .to.be.revertedWithCustomError(combined, "RoflBridgeNotSet")
      .withArgs(SAPPHIRE_CHAIN_ID);
  });

  it("unset roflBridgeAddress[84532] reverts RoflBridgeNotSet(84532)", async () => {
    const [owner] = await ethers.getSigners();
    // No configureRose — the route is left unset.
    const { combined } = await deployWiredCombined(owner.address);

    await expect(
      (combined as any).reserveBridgeBurn(
        depositB,
        BASE_CHAIN_ID,
        ROFL_BRIDGE,
        amountA,
      ),
    )
      .to.be.revertedWithCustomError(combined, "RoflBridgeNotSet")
      .withArgs(BASE_CHAIN_ID);
  });

  it("bridge != roflBridgeAddress[84532] (first reservation) reverts InvalidRouteAddress", async () => {
    const [owner] = await ethers.getSigners();
    const { combined } = await deployWiredCombined(owner.address);
    await configureRose(combined);

    await expect(
      (combined as any).reserveBridgeBurn(
        depositB,
        BASE_CHAIN_ID,
        OTHER_BRIDGE,
        amountA,
      ),
    ).to.be.revertedWithCustomError(combined, "InvalidRouteAddress");
  });

  it("amount == 0 reverts InvalidAmount", async () => {
    const [owner] = await ethers.getSigners();
    const { combined } = await deployWiredCombined(owner.address);
    await configureRose(combined);

    await expect(
      (combined as any).reserveBridgeBurn(
        depositB,
        BASE_CHAIN_ID,
        ROFL_BRIDGE,
        0n,
      ),
    ).to.be.revertedWithCustomError(combined, "InvalidAmount");
  });

  it("depositId == bytes32(0) reverts InvalidDepositId", async () => {
    const [owner] = await ethers.getSigners();
    const { combined } = await deployWiredCombined(owner.address);
    await configureRose(combined);

    await expect(
      (combined as any).reserveBridgeBurn(
        ethers.ZeroHash,
        BASE_CHAIN_ID,
        ROFL_BRIDGE,
        amountA,
      ),
    ).to.be.revertedWithCustomError(combined, "InvalidDepositId");
  });

  it("no two functions share a 4-byte selector, and reserveBridgeBurn is present once", async () => {
    // Selector uniqueness within one contract is compile-enforced. This
    // re-asserts it over the live ABI, and pins that the reserve selector
    // resolves to the single bytes32/uint256/address/uint256 form.
    const iface = (await ethers.getContractFactory("MockAccounting"))
      .interface;
    const fns = iface.fragments.filter((f: any) => f.type === "function");
    const selectors = fns.map((f: any) => f.selector);
    expect(new Set(selectors).size).to.equal(
      selectors.length,
      "MockAccounting ABI must have no duplicate 4-byte selectors",
    );

    const reserveSelector = ethers
      .id("reserveBridgeBurn(bytes32,uint256,address,uint256)")
      .slice(0, 10);
    const matches = selectors.filter((s) => s === reserveSelector);
    expect(matches.length).to.equal(
      1,
      "reserveBridgeBurn selector must appear exactly once in the Accounting ABI",
    );
  });

  // NOTE: the production `onlyROFL` gate on `reserveBridgeBurn` cannot be
  // exercised on Hardhat. `Accounting.initialize` calls the Sapphire
  // `EthereumUtils.generateKeypair` precompile (absent on Hardhat), so a plain
  // `Accounting` proxy cannot be deployed here, and `MockAccounting`
  // deliberately overrides the function to drop the gate. The gate must
  // therefore be exercised on a Sapphire network; there is no Hardhat-runnable
  // equivalent.
});

// ─── Sign sequence ────────────────────────────────────────────────────────
//
// `generateBridgeBurnTransfer(bytes32 depositId)` signs a Base-Sepolia burn
// tx whose every field — destination, calldata, chain, nonce — is derived
// from the stored `BridgeBurnRequest`. On Hardhat `EIP155Signer.sign`
// reverts with `DER_Split_Error` because the Sapphire precompile is absent
// (the same DER_Split_Error pin used for the sweep selector), so the
// "successful sign" path is pinned by reaching that revert rather than parsing bytes. All
// pre-sign reverts are checkable directly.

describe("BridgeBurnNonce — sign sequence", () => {
  before(async function () {
    const network = await ethers.provider.getNetwork();
    if (network.name !== "hardhat" && network.name !== "unknown") this.skip();
  });

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
    // Same DER_Split_Error pin used for the sweep selector.
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
    const { accounting, combined } = await deployWiredCombined(owner.address);
    await configureRose(combined);
    await accounting.mockSetRoflSignerAddress(owner.address);

    await expect(
      (combined as any).generateBridgeBurnTransfer.staticCall(depositA),
    )
      .to.be.revertedWithCustomError(combined, "BridgeBurnNotFound")
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
    // any storage read in the body. Same gate covered for the sweep selector.
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
    // Same gate covered for the sweep selector.
    const [owner, other] = await ethers.getSigners();
    const { accounting, combined, proxyAddr } = await deployWiredCombined(
      owner.address,
    );
    await configureRose(combined);
    await reserveDepositA(combined, accounting);
    await accounting.mockSetRoflSignerAddress(owner.address);

    const otherCombined = await attachAccounting(proxyAddr, other);
    await expect(
      (otherCombined as any).generateBridgeBurnTransfer.staticCall(depositA),
    ).to.be.revertedWithCustomError(otherCombined, "NotAuthorizedROFL");
  });

  it("no duplicate selectors, and generateBridgeBurnTransfer takes exactly one bytes32 arg", async () => {
    // The on-chain proof that no overload accepting external
    // amount/bridge/chain/nonce exists: exactly one fragment, one bytes32
    // input. Selector uniqueness across the Accounting surface is
    // compile-enforced; re-asserted here over the live ABI.
    const iface = (await ethers.getContractFactory("MockAccounting"))
      .interface;
    const fns = iface.fragments.filter((f: any) => f.type === "function");
    const selectors = fns.map((f: any) => f.selector);
    expect(new Set(selectors).size).to.equal(
      selectors.length,
      "MockAccounting ABI must have no duplicate 4-byte selectors",
    );

    const matches = fns.filter(
      (f: any) => f.name === "generateBridgeBurnTransfer",
    );
    expect(matches.length).to.equal(
      1,
      "generateBridgeBurnTransfer must have exactly one fragment in the Accounting ABI",
    );
    expect(matches[0].inputs.length).to.equal(1);
    expect(matches[0].inputs[0].type).to.equal("bytes32");
  });
});
