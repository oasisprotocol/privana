import { ethers, upgrades } from "hardhat";

async function main() {
  console.log("Validating upgrade safety for MockAccounting...");

  const MockAccounting = await ethers.getContractFactory("MockAccounting");
  await upgrades.validateImplementation(MockAccounting, {
    kind: "uups",
    // delegatecall: required by the bridge-module fallback dispatcher.
    // Scope is constrained to the unstructured `_BRIDGE_MODULE_SLOT`
    // pointer; selector allowlist + UnknownSelector revert keep the surface
    // minimal.
    unsafeAllow: ["constructor", "state-variable-immutable", "delegatecall"],
    constructorArgs: [
      ethers.getAddress("0x0000000000000000000000000000000000000001"),
    ],
  } as any);

  console.log("Validating upgrade safety for Accounting...");

  const Accounting = await ethers.getContractFactory("Accounting");
  await upgrades.validateImplementation(Accounting, {
    kind: "uups",
    unsafeAllow: ["constructor", "state-variable-immutable", "delegatecall"],
    constructorArgs: [
      ethers.getAddress("0x0000000000000000000000000000000000000001"),
    ],
  } as any);

  console.log("Validating upgrade safety for BridgeModule...");

  // BridgeModule is not a proxy implementation (no `_authorizeUpgrade`,
  // never deployed behind its own proxy). Validate without `kind: 'uups'`
  // so the OZ plugin only checks layout/initializer rules. The shared
  // storage prefix is enforced separately by AccountingStorageLayout.ts.
  const BridgeModule = await ethers.getContractFactory("BridgeModule", {
    libraries: {
      BridgeLib: ethers.getAddress(
        "0x0000000000000000000000000000000000000001",
      ),
    },
  });
  await upgrades.validateImplementation(BridgeModule, {
    // BridgeModule is never deployed as a proxy — it's a fixed delegated
    // module. The shared storage layout it inherits has its initializer
    // called by Accounting at proxy deploy time; the module itself disables
    // initializers in its constructor and must not have one of its own.
    unsafeAllow: [
      "constructor",
      "state-variable-immutable",
      "external-library-linking",
      "missing-initializer",
      // delegatecall: inherits the history-append helper (_delegateHistory)
      // from the shared AccountingStorage base, scoped to the historyModule
      // pointer. BridgeModule never calls it, but the validator flags presence
      // in the inheritance tree.
      "delegatecall",
    ],
  } as any);

  console.log("Validating upgrade safety for LockModule...");

  // LockModule is a fixed delegated module (never its own proxy), same as
  // BridgeModule but with no library linking — it uses no BridgeLib.
  const LockModule = await ethers.getContractFactory("LockModule");
  await upgrades.validateImplementation(LockModule, {
    unsafeAllow: [
      "constructor",
      "state-variable-immutable",
      "missing-initializer",
      // delegatecall: lock history appends go through the inherited
      // _delegateHistory helper (shared AccountingStorage base), scoped to the
      // historyModule pointer.
      "delegatecall",
    ],
  } as any);

  console.log("Storage layout validation passed for all contracts");
}

main().catch((error) => {
  console.error("Storage layout validation failed:", error.message);
  process.exit(1);
});
