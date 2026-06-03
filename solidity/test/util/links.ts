import { ethers } from "hardhat";
import type { BaseContract, Contract, ContractFactory, Signer } from "ethers";

/// Deploys a fresh BridgeLib instance and returns the deployed contract.
/// BridgeLib is a stateless pure-function library; every Accounting (or
/// Accounting-derived) contract factory needs `libraries: { BridgeLib }`
/// set so hardhat-ethers can link the placeholder during bytecode deployment.
export async function deployBridgeLib(): Promise<BaseContract> {
  const Factory = await ethers.getContractFactory("BridgeLib");
  const lib = await Factory.deploy();
  await lib.waitForDeployment();
  return lib;
}

/// Returns a contract factory for an Accounting-like contract (Accounting,
/// MockAccounting, MockAccountingBridgeExposure). After the BridgeModule
/// extraction, Accounting itself no longer references BridgeLib, so this
/// helper links BridgeLib only if the factory's bytecode actually needs it.
export async function getLinkedAccountingFactory(
  name: string,
  signer?: Signer,
): Promise<ContractFactory> {
  const artifact = await import("hardhat").then((h) =>
    h.artifacts.readArtifact(name),
  );
  const needsBridgeLib =
    artifact.linkReferences &&
    Object.values(artifact.linkReferences).some((libs: any) =>
      Object.keys(libs).includes("BridgeLib"),
    );
  if (!needsBridgeLib) {
    return ethers.getContractFactory(name, signer ? { signer } : undefined);
  }
  const lib = await deployBridgeLib();
  return ethers.getContractFactory(name, {
    libraries: { BridgeLib: await lib.getAddress() },
    ...(signer ? { signer } : {}),
  });
}

/// Returns the contract factory plus a deployed BridgeLib instance. The
/// library reference is used as the `revertedWithCustomError` target for
/// errors that live in the library (e.g. `InvalidRouteAddress`,
/// `RoflBridgeNotSet`); the chai matcher
/// looks up the error fragment by name on the contract instance and does
/// not care that this BridgeLib copy is not the one delegate-called by
/// the module under test.
export async function getLinkedAccountingFactoryAndLib(
  name: string,
  signer?: Signer,
): Promise<{ factory: ContractFactory; bridgeLib: BaseContract }> {
  const bridgeLib = await deployBridgeLib();
  const factory = await getLinkedAccountingFactory(name, signer);
  return { factory, bridgeLib };
}

/// Deploys a fresh BridgeModule (or test variant) with BridgeLib linked.
/// `name` defaults to `MockBridgeModule` for Hardhat tests.
export async function deployBridgeModule(
  name: string = "MockBridgeModule",
  signer?: Signer,
): Promise<BaseContract> {
  const lib = await deployBridgeLib();
  const factory = await ethers.getContractFactory(name, {
    libraries: { BridgeLib: await lib.getAddress() },
    ...(signer ? { signer } : {}),
  });
  const moduleContract = await factory.deploy();
  await moduleContract.waitForDeployment();
  return moduleContract;
}

/// Deploys a fresh AccountingHistoryModule and links it to the given Accounting
/// proxy via setHistoryModule. Every fixture that credits a deposit, transfer,
/// or withdrawal must wire history: those paths append a history entry through
/// the delegated module and revert with InvalidHistoryModule() when the pointer
/// is unset (the production deploy task wires it identically). The module needs
/// no BridgeLib link.
export async function wireHistoryModule(
  accounting: { setHistoryModule(addr: string): Promise<{ wait(): Promise<unknown> }> },
  signer?: Signer,
): Promise<string> {
  const factory = await ethers.getContractFactory(
    "AccountingHistoryModule",
    signer ? { signer } : undefined,
  );
  const module = await factory.deploy();
  await module.waitForDeployment();
  const addr = await module.getAddress();
  await (await accounting.setHistoryModule(addr)).wait();
  return addr;
}

/// Returns a contract handle bound to the Accounting proxy address with the
/// union of the named contracts' ABIs. Bridge selectors route through the
/// proxy fallback to the configured BridgeModule via delegatecall; non-bridge
/// selectors hit Accounting's resident bodies.
///
/// Default usage: `getCombinedAccountingAt(proxyAddr, signer)` merges
/// `MockAccounting` + `MockBridgeModule`. Pass a longer `names` array when
/// the proxy is fronting a more specialized fixture (e.g.
/// `[..., 'MockAccountingBridgeExposure']`) so the combined handle also
/// exposes mock-only helpers.
export async function getCombinedAccountingAt(
  proxyAddr: string,
  signer?: Signer,
  names: ReadonlyArray<string> = ["MockAccounting", "MockBridgeModule"],
): Promise<Contract> {
  const fragmentLists = await Promise.all(
    names.map(
      async (n) => (await getLinkedAccountingFactory(n)).interface.fragments,
    ),
  );
  const merged = mergeFragments(fragmentLists.flat());
  return new ethers.Contract(proxyAddr, merged, signer ?? ethers.provider);
}

// Merges ethers Fragments by canonical key (selector for functions/errors,
// topic+indexed-flags for events) using prefer-first semantics: when both
// inputs declare the same key, keep the first one seen.
//
// Why prefer-first instead of throw-on-conflict: `renounceOwnership` is
// declared once in `AccountingStorage.sol` and inherited by both
// `Accounting` and `BridgeModule`, so both factories surface the SAME
// fragment from the shared base. Without dedup the union would carry it
// twice with identical canonical keys; throwing on every such case would
// fire reflexively on every shared-base fragment. The dedup collapses the
// inherited duplicates without erasing genuine conflicts (which the
// throw-on-conflict Python merger catches independently — see
// `src/abi/accounting.py::_merge_abis`). Selector-shadowing of routed
// bridge selectors and 4-byte hash collisions are policed by separate
// tests in `AccountingBridgeModule.ts`.
function mergeFragments(fragments: ReadonlyArray<any>): any[] {
  const out = new Map<string, any>();
  for (const frag of fragments) {
    const key = canonicalKey(frag);
    if (key === null) {
      // constructor / fallback / receive — keep first occurrence, no dedup needed
      continue;
    }
    if (!out.has(key)) {
      out.set(key, frag);
    }
  }
  return Array.from(out.values());
}

/// Returns the merged 4-byte function selectors for the named contracts'
/// combined ABI, paired with their human-readable signatures. Walks the
/// MERGED fragment set (output of `mergeFragments`) so identical inherited
/// fragments (`renounceOwnership` etc.) are collapsed before grouping —
/// callers grouping by selector won't false-positive on shared-base
/// duplicates. Useful for selector-uniqueness gates that mirror the Python
/// regression in `test/py/test_accounting_abi.py`.
export async function getCombinedSelectors(
  names: ReadonlyArray<string>,
): Promise<Array<{ selector: string; signature: string }>> {
  const fragmentLists = await Promise.all(
    names.map(
      async (n) => (await getLinkedAccountingFactory(n)).interface.fragments,
    ),
  );
  const merged = mergeFragments(fragmentLists.flat());
  const out: Array<{ selector: string; signature: string }> = [];
  for (const f of merged) {
    if (f.type !== "function") continue;
    const sig = f.format("sighash") as string;
    out.push({ selector: ethers.id(sig).slice(0, 10), signature: sig });
  }
  return out;
}

function canonicalKey(frag: any): string | null {
  if (frag.type === "function" || frag.type === "error") {
    const inputs = (frag.inputs ?? []).map((i: any) => i.type).join(",");
    return `${frag.type}:${frag.name}(${inputs})`;
  }
  if (frag.type === "event") {
    const inputs = (frag.inputs ?? [])
      .map((i: any) => `${i.type}${i.indexed ? "!" : ""}`)
      .join(",");
    return `event:${frag.name}(${inputs})`;
  }
  return null;
}
