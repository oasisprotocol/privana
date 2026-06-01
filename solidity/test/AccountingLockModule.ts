import { expect } from "chai";
import { ethers, config, upgrades } from "hardhat";
import { Contract, Wallet, ZeroAddress } from "ethers";
import { HardhatNetworkHDAccountsConfig } from "hardhat/types";
import { MockAccounting } from "../typechain-types";
import {
  getCombinedAccountingAt,
  getCombinedSelectors,
  getLinkedAccountingFactory,
  wireHistoryModule,
} from "./util/links";

const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21);

// Pinned literal: bytes32(uint256(keccak256("flexvaults.accounting.lockModule")) - 1).
// Recomputed in test below for guard, but kept here for reference.
const EXPECTED_SLOT = ethers.toBeHex(
  BigInt(
    ethers.keccak256(ethers.toUtf8Bytes("flexvaults.accounting.lockModule")),
  ) - 1n,
  32,
);

// Sibling delegated-module pointer. Lock and bridge pointers must occupy
// distinct unstructured slots — otherwise setting one would clobber the other.
const BRIDGE_MODULE_SLOT = ethers.toBeHex(
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

// Non-bridge ERC20 token used by the routing smoke test. createLock rejects
// BridgeAsset tokens (e.g. ROSE), so the smoke path needs a plain ERC20.
const TokenType = { NativeEVM: 0, ERC20: 1, BridgeAsset: 2 } as const;
const TEST_TOKEN = {
  chainId: 84532,
  address: "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
  // keccak256(abi.encodePacked(uint256(84532), address(0x036c...cf7e)))
  tokenId: "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514",
};

const lockTypes = {
  Lock: [
    { name: "serviceAddress", type: "address" },
    { name: "tokenId", type: "bytes32" },
    { name: "amount", type: "uint256" },
    { name: "expiry", type: "uint256" },
    { name: "nonce", type: "uint256" },
  ],
};

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

// Deploys a fresh LockModule. Unlike BridgeModule, the lock primitives use
// `ECDSA.recover` natively (no Sapphire precompile), so the production
// `LockModule` works directly in Hardhat fixtures — no mock variant exists.
async function deployLockModule(): Promise<Contract> {
  const factory = await ethers.getContractFactory("LockModule");
  const module = await factory.deploy();
  await module.waitForDeployment();
  return module as unknown as Contract;
}

describe("Accounting lock module dispatcher", () => {
  describe("setLockModule admin", () => {
    it("reverts when caller is not the owner", async () => {
      const [owner, other] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      const moduleContract = await deployLockModule();
      await expect(
        accounting
          .connect(other)
          .setLockModule(await moduleContract.getAddress()),
      ).to.be.revertedWithCustomError(accounting, "OwnableUnauthorizedAccount");
    });

    it("reverts on address(0)", async () => {
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      await expect(
        accounting.setLockModule(ZeroAddress),
      ).to.be.revertedWithCustomError(accounting, "InvalidAddress");
    });

    it("reverts on EOA (code.length == 0)", async () => {
      const [owner, eoa] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      await expect(
        accounting.setLockModule(eoa.address),
      ).to.be.revertedWithCustomError(accounting, "LockModuleNotContract");
    });

    it("emits LockModuleSet and lockModule() reads back the address", async () => {
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      const moduleContract = await deployLockModule();
      const moduleAddr = await moduleContract.getAddress();

      await expect(accounting.setLockModule(moduleAddr))
        .to.emit(accounting, "LockModuleSet")
        .withArgs(moduleAddr);

      expect(await accounting.lockModule()).to.equal(moduleAddr);
    });

    it("replacing the module routes future calls to the new address", async () => {
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      const first = await deployLockModule();
      const second = await deployLockModule();

      await accounting.setLockModule(await first.getAddress());
      expect(await accounting.lockModule()).to.equal(await first.getAddress());

      await accounting.setLockModule(await second.getAddress());
      expect(await accounting.lockModule()).to.equal(await second.getAddress());
    });
  });

  describe("storage slot derivation", () => {
    it("lock module pointer lives at the documented unstructured slot", async () => {
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      const moduleContract = await deployLockModule();
      const moduleAddr = await moduleContract.getAddress();

      await accounting.setLockModule(moduleAddr);

      const raw = await ethers.provider.getStorage(
        await accounting.getAddress(),
        EXPECTED_SLOT,
      );
      // sstore packs an address into the lower 20 bytes; compare canonicalized.
      expect(ethers.getAddress("0x" + raw.slice(-40))).to.equal(moduleAddr);
    });

    it("lock module slot does not collide with ERC-1967 implementation/admin slots or the bridge module slot", () => {
      expect(EXPECTED_SLOT).to.not.equal(ERC1967_IMPLEMENTATION_SLOT);
      expect(EXPECTED_SLOT).to.not.equal(ERC1967_ADMIN_SLOT);
      expect(EXPECTED_SLOT).to.not.equal(BRIDGE_MODULE_SLOT);
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

    it("no resident Accounting selector shadows a routed lock selector", async () => {
      // If a future Accounting helper is added with a 4-byte selector that
      // happens to equal a routed lock selector, Solidity dispatch in the
      // proxy would shadow the routed call (fallback only fires on
      // unmatched selectors). The fallback's allowlist would then be
      // silently bypassed.
      const accountingFactory =
        await getLinkedAccountingFactory("MockAccounting");
      const lockFactory = await getLinkedAccountingFactory("LockModule");
      // Source of truth: every function declared on ILockModule is routed
      // through the fallback. Hard-coding the list here historically drifted
      // (added selectors never reached the shadow check); deriving from the
      // interface keeps the test in lockstep with the real allowlist target.
      const ifaceArtifact = await (
        await import("hardhat")
      ).artifacts.readArtifact("ILockModule");
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
          `Resident Accounting selector ${sig} shadows a routed lock selector`,
        ).to.equal(false);
      }
      // Also assert that the routed selectors actually exist in LockModule.
      const moduleSelectors = new Set<string>(
        lockFactory.interface.fragments
          .filter((f: any) => f.type === "function")
          .map((f: any) => ethers.id(f.format("sighash")).slice(0, 10)),
      );
      for (const sig of routed) {
        expect(
          moduleSelectors.has(sig),
          `Routed selector ${sig} not present in LockModule`,
        ).to.equal(true);
      }
    });

    it("reverts LockModuleNotSet when a routed selector hits the dispatcher with module unset", async () => {
      const [owner] = await ethers.getSigners();
      const accounting = await deployAccountingProxy(owner.address);
      // Module pointer deliberately left unset. createLock is one of the
      // routed selectors; hand-rolled calldata forces the fallback path.
      const createLockSelector = ethers
        .id("createLock(address,bytes32,uint256,uint256,uint256,bytes)")
        .slice(0, 10);
      const args = ethers.AbiCoder.defaultAbiCoder().encode(
        ["address", "bytes32", "uint256", "uint256", "uint256", "bytes"],
        [
          ethers.Wallet.createRandom().address,
          TEST_TOKEN.tokenId,
          100n,
          ethers.MaxUint256,
          0n,
          "0x",
        ],
      );
      const calldata = createLockSelector + args.slice(2);
      await expect(
        owner.sendTransaction({
          to: await accounting.getAddress(),
          data: calldata,
        }),
      ).to.be.revertedWithCustomError(accounting, "LockModuleNotSet");
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
        "LockModule",
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

// ─── delegated-path routing smoke test ───────────────────────────────────
//
// Exercises the proxy + delegated LockModule path on Hardhat. createLock is
// routed through the fallback via delegatecall; the lock primitives recover
// the EIP-712 signer with `ECDSA.recover` (no Sapphire precompile), so the
// real LockModule body runs end-to-end in-memory.

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

function mockAuthToken(address: string): string {
  return ethers.hexlify(ethers.zeroPadValue(address, 32));
}

async function deployWiredCombined(ownerAddress: string): Promise<{
  accounting: MockAccounting;
  combined: Contract;
  proxyAddr: string;
  moduleContract: Contract;
  moduleAddr: string;
}> {
  const accounting = await deployAccountingProxy(ownerAddress);
  const moduleContract = await deployLockModule();
  const moduleAddr = await moduleContract.getAddress();
  await accounting.setLockModule(moduleAddr);
  await wireHistoryModule(accounting);
  const proxyAddr = await accounting.getAddress();
  const [signer] = await ethers.getSigners();
  const combined = await getCombinedAccountingAt(proxyAddr, signer, [
    "MockAccounting",
    "LockModule",
  ]);
  return { accounting, combined, proxyAddr, moduleContract, moduleAddr };
}

async function getBlockTimestamp(): Promise<number> {
  const block = await ethers.provider.getBlock("latest");
  return block!.timestamp;
}

describe("Accounting lock module: delegated-path routing smoke test", () => {
  it("createLock through the proxy decrements the resident balance and lands a visible lock", async () => {
    const [owner] = await ethers.getSigners();
    const { accounting, combined } = await deployWiredCombined(owner.address);

    // Register a plain ERC20 token (createLock rejects BridgeAsset) and seed
    // the user's resident balance via the MockAccounting helper.
    const tokenData = ethers.concat([
      ethers.zeroPadValue(ethers.toBeHex(TEST_TOKEN.chainId), 32),
      ethers.zeroPadValue(TEST_TOKEN.address, 20),
    ]);
    await (combined as any).setTokenInfo({
      tokenType: TokenType.ERC20,
      data: tokenData,
    });

    const userWallet = getUserWallet();
    const seeded = 1_000_000n;
    const amount = 250_000n;
    await accounting.setBalance(userWallet.address, TEST_TOKEN.tokenId, seeded);

    const expiry = (await getBlockTimestamp()) + 3600;
    const domain = await getDomain(combined);
    const nonce = await (combined as any).createLockNonces(userWallet.address);
    const signature = await userWallet.signTypedData(domain, lockTypes, {
      serviceAddress: owner.address,
      tokenId: TEST_TOKEN.tokenId,
      amount,
      expiry,
      nonce,
    });

    await (combined as any).createLock(
      owner.address,
      TEST_TOKEN.tokenId,
      amount,
      expiry,
      nonce,
      signature,
    );

    // Resident read must observe the LockModule-side balance decrement.
    expect(
      await accounting.getBalance(userWallet.address, TEST_TOKEN.tokenId),
    ).to.equal(seeded - amount);

    // The lock itself is visible through the routed view selector.
    const locks = await (combined as any).getUserLocks(
      mockAuthToken(userWallet.address),
    );
    expect(locks.length).to.equal(1);
    expect(locks[0].serviceId).to.equal(owner.address);
    expect(locks[0].tokenId).to.equal(TEST_TOKEN.tokenId);
    expect(locks[0].amount).to.equal(amount);
    expect(locks[0].expiry).to.equal(BigInt(expiry));
  });
});
