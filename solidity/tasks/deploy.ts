import { task } from "hardhat/config";
import type { HardhatRuntimeEnvironment } from "hardhat/types";
import { parseRoflAppId } from "./utils/rofl";

type HistoryModuleReader = {
  historyModule(): Promise<string>;
};

type HistoryModuleLinker = HistoryModuleReader & {
  setHistoryModule(module: string): Promise<{ wait(): Promise<unknown> }>;
};

type UpgradeOptions = {
  kind: "uups";
  constructorArgs: string[];
  unsafeAllow: string[];
  redeployImplementation: "always";
  txOverrides: { gasLimit: number };
  call?: { fn: string; args: string[] };
};

function normalizeAddress(address: string): string {
  return address.toLowerCase();
}

function isEmptyCallResult(value: unknown): boolean {
  if (value === "0x") {
    return true;
  }
  if (!value || typeof value !== "object") {
    return false;
  }
  const result = value as { data?: unknown; result?: unknown; value?: unknown };
  return [result.data, result.result, result.value].some(isEmptyCallResult);
}

function isExecutionRevert(error: unknown): boolean {
  // A pre-module implementation has no module-pointer getter selector, so the
  // proxy fallback rejects the call. Depending on the chain that surfaces as an
  // empty CALL result (see isEmptyCallResult) or as a plain revert: Sapphire's
  // confidential EVM returns the latter — a ProviderError "execution reverted"
  // (the old impl's fallback reverts UnknownSelector with non-empty data, which
  // isEmptyCallResult does not match). A valid post-module getter is a trivial
  // storage read that never reverts, so treating any contract-level revert here
  // as "getter absent" is safe; genuine transport failures (NETWORK_ERROR,
  // TIMEOUT) carry other codes/messages and are intentionally not matched.
  const e = error as {
    code?: string;
    shortMessage?: string;
    message?: string;
    info?: { error?: { message?: string } };
    error?: { message?: string };
  };
  if (e?.code === "CALL_EXCEPTION") {
    return true;
  }
  const haystack = [e?.shortMessage, e?.message, e?.info?.error?.message, e?.error?.message]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return haystack.includes("execution reverted") || haystack.includes("unknownselector");
}

function isMissingHistoryModuleGetter(error: unknown): boolean {
  const code = (error as { code?: string })?.code;
  if ((code === "BAD_DATA" || code === "CALL_EXCEPTION") && isEmptyCallResult(error)) {
    return true;
  }
  return isExecutionRevert(error);
}

async function deployHistoryModule(hre: HardhatRuntimeEnvironment) {
  const AccountingHistoryModule = await hre.ethers.getContractFactory("AccountingHistoryModule");
  const history = await AccountingHistoryModule.deploy({ gasLimit: 5000000 });
  await history.waitForDeployment();
  return history;
}

async function validateHistoryModule(hre: HardhatRuntimeEnvironment, historyAddress: string) {
  if (!hre.ethers.isAddress(historyAddress) || historyAddress === hre.ethers.ZeroAddress) {
    throw new Error(`Invalid AccountingHistoryModule address: ${historyAddress}`);
  }

  const code = await hre.ethers.provider.getCode(historyAddress);
  if (code === "0x") {
    throw new Error(`AccountingHistoryModule address has no contract code: ${historyAddress}`);
  }

  const history = await hre.ethers.getContractAt("AccountingHistoryModule", historyAddress);
  const moduleId = await history.MODULE_ID();
  const expectedModuleId = hre.ethers.id("privana.accounting.historyModule.v1");
  if (moduleId !== expectedModuleId) {
    throw new Error(`AccountingHistoryModule has unexpected module id: ${moduleId}`);
  }

  return history;
}

async function readLinkedHistoryModule(
  hre: HardhatRuntimeEnvironment,
  accounting: HistoryModuleReader
): Promise<string> {
  try {
    return await accounting.historyModule();
  } catch (error) {
    if (!isMissingHistoryModuleGetter(error)) {
      throw error;
    }
    // Pre-AccountingHistoryModule implementations do not expose this getter.
    return hre.ethers.ZeroAddress;
  }
}

async function resolveHistoryModule(
  hre: HardhatRuntimeEnvironment,
  accounting: HistoryModuleReader,
  requestedHistoryAddress?: string
) {
  let historyAddress = requestedHistoryAddress;

  if (historyAddress) {
    await validateHistoryModule(hre, historyAddress);
  } else {
    historyAddress = await readLinkedHistoryModule(hre, accounting);

    if (historyAddress === hre.ethers.ZeroAddress) {
      const history = await deployHistoryModule(hre);
      historyAddress = await history.getAddress();
      console.log(`AccountingHistoryModule address: ${historyAddress}`);
      await validateHistoryModule(hre, historyAddress);
    } else {
      await validateHistoryModule(hre, historyAddress);
      console.log(`Existing AccountingHistoryModule address: ${historyAddress}`);
    }
  }

  return historyAddress;
}

async function ensureHistoryModule(
  hre: HardhatRuntimeEnvironment,
  accounting: HistoryModuleLinker,
  requestedHistoryAddress?: string
) {
  const historyAddress = await resolveHistoryModule(hre, accounting, requestedHistoryAddress);
  const linkedHistoryAddress = await readLinkedHistoryModule(hre, accounting);
  if (normalizeAddress(linkedHistoryAddress) !== normalizeAddress(historyAddress)) {
    const tx = await accounting.setHistoryModule(historyAddress);
    await tx.wait();
    console.log(`Accounting linked to AccountingHistoryModule: ${historyAddress}`);
  }

  return historyAddress;
}

type ModulePointerLinker = {
  bridgeModule(): Promise<string>;
  lockModule(): Promise<string>;
  setBridgeModule(module: string): Promise<{ wait(): Promise<unknown> }>;
  setLockModule(module: string): Promise<{ wait(): Promise<unknown> }>;
};

async function validateModuleCode(
  hre: HardhatRuntimeEnvironment,
  label: string,
  moduleAddress: string,
) {
  if (!hre.ethers.isAddress(moduleAddress) || moduleAddress === hre.ethers.ZeroAddress) {
    throw new Error(`Invalid ${label} address: ${moduleAddress}`);
  }
  const code = await hre.ethers.provider.getCode(moduleAddress);
  if (code === "0x") {
    throw new Error(`${label} address has no contract code: ${moduleAddress}`);
  }
}

async function readLinkedModulePointer(
  hre: HardhatRuntimeEnvironment,
  read: () => Promise<string>,
): Promise<string> {
  try {
    return await read();
  } catch (error) {
    // The getter-absent shape (BAD_DATA / empty CALL_EXCEPTION) is identical
    // for every module pointer on a pre-module implementation.
    if (!isMissingHistoryModuleGetter(error)) {
      throw error;
    }
    return hre.ethers.ZeroAddress;
  }
}

async function deployBridgeModule(hre: HardhatRuntimeEnvironment): Promise<string> {
  // BridgeModule needs BridgeLib linked because its runtime calls the library externally.
  const BridgeLib = await hre.ethers.getContractFactory("BridgeLib");
  const bridgeLib = await BridgeLib.deploy({ gasLimit: 5000000 });
  await bridgeLib.waitForDeployment();
  const bridgeLibAddress = await bridgeLib.getAddress();
  console.log(`BridgeLib address: ${bridgeLibAddress}`);

  const BridgeModule = await hre.ethers.getContractFactory("BridgeModule", {
    libraries: { BridgeLib: bridgeLibAddress },
  });
  const bridgeModule = await BridgeModule.deploy({ gasLimit: 10000000 });
  await bridgeModule.waitForDeployment();
  return await bridgeModule.getAddress();
}

async function deployLockModule(hre: HardhatRuntimeEnvironment): Promise<string> {
  // Unlike BridgeModule, LockModule links no library.
  const LockModule = await hre.ethers.getContractFactory("LockModule");
  const lockModule = await LockModule.deploy({ gasLimit: 10000000 });
  await lockModule.waitForDeployment();
  return await lockModule.getAddress();
}

async function ensureBridgeModule(
  hre: HardhatRuntimeEnvironment,
  accounting: ModulePointerLinker,
  requestedAddress?: string,
): Promise<string> {
  let bridgeModuleAddress = requestedAddress;
  if (bridgeModuleAddress) {
    await validateModuleCode(hre, "BridgeModule", bridgeModuleAddress);
  } else {
    bridgeModuleAddress = await readLinkedModulePointer(hre, () => accounting.bridgeModule());
    if (bridgeModuleAddress === hre.ethers.ZeroAddress) {
      bridgeModuleAddress = await deployBridgeModule(hre);
      console.log(`BridgeModule address: ${bridgeModuleAddress}`);
    } else {
      await validateModuleCode(hre, "BridgeModule", bridgeModuleAddress);
      console.log(`Existing BridgeModule address: ${bridgeModuleAddress}`);
    }
  }
  const linked = await readLinkedModulePointer(hre, () => accounting.bridgeModule());
  if (normalizeAddress(linked) !== normalizeAddress(bridgeModuleAddress)) {
    const tx = await accounting.setBridgeModule(bridgeModuleAddress);
    await tx.wait();
    console.log(`Accounting linked to BridgeModule: ${bridgeModuleAddress}`);
  }
  return bridgeModuleAddress;
}

async function ensureLockModule(
  hre: HardhatRuntimeEnvironment,
  accounting: ModulePointerLinker,
  requestedAddress?: string,
): Promise<string> {
  let lockModuleAddress = requestedAddress;
  if (lockModuleAddress) {
    await validateModuleCode(hre, "LockModule", lockModuleAddress);
  } else {
    lockModuleAddress = await readLinkedModulePointer(hre, () => accounting.lockModule());
    if (lockModuleAddress === hre.ethers.ZeroAddress) {
      lockModuleAddress = await deployLockModule(hre);
      console.log(`LockModule address: ${lockModuleAddress}`);
    } else {
      await validateModuleCode(hre, "LockModule", lockModuleAddress);
      console.log(`Existing LockModule address: ${lockModuleAddress}`);
    }
  }
  const linked = await readLinkedModulePointer(hre, () => accounting.lockModule());
  if (normalizeAddress(linked) !== normalizeAddress(lockModuleAddress)) {
    const tx = await accounting.setLockModule(lockModuleAddress);
    await tx.wait();
    console.log(`Accounting linked to LockModule: ${lockModuleAddress}`);
  }
  return lockModuleAddress;
}

task("deploy-proveth-verifier")
  .setAction(async (_, hre) => {
    const ProvethVerifier = await hre.ethers.getContractFactory("ProvethVerifier");
    const provethVerifier = await ProvethVerifier.deploy({ gasLimit: 5000000 });
    await provethVerifier.waitForDeployment();
    const address = await provethVerifier.getAddress();
    console.log(`ProvethVerifier deployed at: ${address}`);
    return address;
  });

task("deploy")
  .addParam("roflappid", "The ROFL app ID (hex 0x... or bech32 rofl1...)")
  .setAction(async (args, hre) => {
    const [deployer] = await hre.ethers.getSigners();

    // Parse ROFL app ID (supports hex and bech32 formats)
    const roflAppIdHex = parseRoflAppId(args.roflappid);

    // Deploy AccountingSiweAuth
    const AccountingSiweAuth =
      await hre.ethers.getContractFactory("AccountingSiweAuth");
    const siweAuth = await AccountingSiweAuth.deploy(roflAppIdHex, {
      gasLimit: 10000000,
    });
    await siweAuth.waitForDeployment();
    const siweAuthAddress = await siweAuth.getAddress();

    // Deploy Accounting as UUPS proxy (siweAuth passed as constructor arg for immutable)
    const Accounting = await hre.ethers.getContractFactory("Accounting");
    const proxy = await hre.upgrades.deployProxy(
      Accounting,
      [roflAppIdHex, deployer.address],
      {
        kind: "uups",
        initializer: "initialize",
        constructorArgs: [siweAuthAddress],
        unsafeAllow: ["constructor", "state-variable-immutable", "delegatecall"],
        txOverrides: { gasLimit: 15000000 },
      },
    );

    await proxy.waitForDeployment();

    const proxyAddress = await proxy.getAddress();
    const implAddress =
      await hre.upgrades.erc1967.getImplementationAddress(proxyAddress);

    // Deploy + link the delegated runtime modules. On a fresh proxy each
    // helper deploys a new module and wires the proxy's pointer; the same
    // helpers re-wire the pointers on `upgrade`.
    const bridgeModuleAddress = await ensureBridgeModule(hre, proxy);
    const historyAddress = await ensureHistoryModule(hre, proxy);
    const lockModuleAddress = await ensureLockModule(hre, proxy);

    console.log(`AccountingSiweAuth address: ${siweAuthAddress}`);
    console.log(`Proxy address: ${proxyAddress}`);
    console.log(`AccountingHistoryModule address: ${historyAddress}`);
    console.log(`Implementation address: ${implAddress}`);
    console.log(`BridgeModule address: ${bridgeModuleAddress}`);
    console.log(`LockModule address: ${lockModuleAddress}`);
    console.log(`EVM signing address: ${await proxy.evmAddress()}`);
    console.log(`Owner: ${await proxy.owner()}`);

    return proxyAddress;
  });

task("deploy-siwe-auth")
  .addParam("roflappid", "The ROFL app ID (hex 0x... or bech32 rofl1...)")
  .setDescription("Deploy a new AccountingSiweAuth contract")
  .setAction(async (args, hre) => {
    const roflAppIdHex = parseRoflAppId(args.roflappid);

    const AccountingSiweAuth =
      await hre.ethers.getContractFactory("AccountingSiweAuth");
    const siweAuth = await AccountingSiweAuth.deploy(roflAppIdHex, {
      gasLimit: 10000000,
    });
    await siweAuth.waitForDeployment();
    const siweAuthAddress = await siweAuth.getAddress();

    console.log(`AccountingSiweAuth deployed at: ${siweAuthAddress}`);
    console.log(`ROFL app ID: ${args.roflappid}`);

    return siweAuthAddress;
  });

task("force-import")
  .addParam("proxy", "The proxy contract address to import")
  .setDescription(
    "Force import an existing proxy into OpenZeppelin's deployment state",
  )
  .setAction(async (args, hre) => {
    const Accounting = await hre.ethers.getContractFactory("Accounting");

    // Get current siweAuth from proxy
    const current = await hre.ethers.getContractAt("Accounting", args.proxy);
    const siweAuthAddress = await current.siweAuth();
    console.log(`Current siweAuth: ${siweAuthAddress}`);

    await hre.upgrades.forceImport(args.proxy, Accounting, {
      kind: "uups",
      constructorArgs: [siweAuthAddress],
      unsafeAllow: ['constructor', 'state-variable-immutable', 'delegatecall'],
    });

    console.log(`Proxy ${args.proxy} imported successfully`);
  });

task("upgrade")
  .addParam("proxy", "The proxy contract address to upgrade")
  .addOptionalParam(
    "siweauth",
    "The AccountingSiweAuth address for the new implementation. If omitted, reuse proxy's current siweAuth",
  )
  .addOptionalParam(
    "history",
    "Existing AccountingHistoryModule to attach. If omitted and none is set, deploy one.",
  )
  .addOptionalParam(
    "bridge",
    "Existing BridgeModule to attach. If omitted, reuse the proxy's linked module or deploy one.",
  )
  .addOptionalParam(
    "lock",
    "Existing LockModule to attach. If omitted, reuse the proxy's linked module or deploy one.",
  )
  .setDescription("Upgrade the Accounting proxy to a new implementation")
  .setAction(async (args, hre) => {
    const Accounting = await hre.ethers.getContractFactory("Accounting");
    let siweAuthAddress: string = args.siweauth;
    const current = await hre.ethers.getContractAt("Accounting", args.proxy);

    if (!siweAuthAddress) {
      try {
        siweAuthAddress = await current.siweAuth();
        console.log(`Resolved siweAuth from proxy: ${siweAuthAddress}`);
      } catch {
        throw new Error(
          "Could not resolve current siweAuth from proxy. Pass --siweauth <address> for this upgrade.",
        );
      }
    }

    if (!hre.ethers.isAddress(siweAuthAddress)) {
      throw new Error(`Invalid siweAuth address: ${siweAuthAddress}`);
    }

    // Get current implementation for comparison
    const currentImpl = await hre.upgrades.erc1967.getImplementationAddress(
      args.proxy,
    );
    console.log(`Current implementation: ${currentImpl}`);

    const historyAddress = await resolveHistoryModule(hre, current, args.history);
    const upgradeOptions: UpgradeOptions = {
      kind: "uups",
      constructorArgs: [siweAuthAddress],
      unsafeAllow: ["constructor", "state-variable-immutable", "delegatecall"],
      redeployImplementation: "always",
      txOverrides: { gasLimit: 15000000 },
      call: {
        fn: "setHistoryModule",
        args: [historyAddress],
      },
    };

    // Always redeploy implementation to avoid caching issues
    const upgraded = await hre.upgrades.upgradeProxy(
      args.proxy,
      Accounting,
      upgradeOptions,
    );

    await upgraded.waitForDeployment();

    const newImplAddress = await hre.upgrades.erc1967.getImplementationAddress(
      args.proxy,
    );
    console.log(`Upgraded! New implementation: ${newImplAddress}`);
    const linkedHistoryAddress = await upgraded.historyModule();
    if (normalizeAddress(linkedHistoryAddress) !== normalizeAddress(historyAddress)) {
      throw new Error(
        `AccountingHistoryModule link mismatch after upgrade: ${linkedHistoryAddress}, expected ${historyAddress}`
      );
    }
    await validateHistoryModule(hre, historyAddress);
    console.log(`AccountingHistoryModule address: ${historyAddress}`);

    // CRITICAL: the lock subsystem now lives entirely in LockModule and the
    // bridge selectors in BridgeModule, both reached via the proxy fallback.
    // An upgrade that leaves either pointer unset makes every lock/bridge
    // selector revert (LockModuleNotSet / BridgeModuleNotSet), stranding
    // escrowed funds. Re-wire (or re-confirm) both, then assert neither is
    // zero before returning so a missed link fails the upgrade loudly.
    const bridgeAddress = await ensureBridgeModule(hre, upgraded, args.bridge);
    const lockAddress = await ensureLockModule(hre, upgraded, args.lock);

    const linkedBridge = await upgraded.bridgeModule();
    if (
      linkedBridge === hre.ethers.ZeroAddress ||
      normalizeAddress(linkedBridge) !== normalizeAddress(bridgeAddress)
    ) {
      throw new Error(
        `BridgeModule link mismatch after upgrade: ${linkedBridge}, expected ${bridgeAddress}`,
      );
    }
    const linkedLock = await upgraded.lockModule();
    if (
      linkedLock === hre.ethers.ZeroAddress ||
      normalizeAddress(linkedLock) !== normalizeAddress(lockAddress)
    ) {
      throw new Error(
        `LockModule link mismatch after upgrade: ${linkedLock}, expected ${lockAddress}`,
      );
    }
    console.log(`BridgeModule address: ${bridgeAddress}`);
    console.log(`LockModule address: ${lockAddress}`);

    if (currentImpl === newImplAddress) {
      console.log(
        `Warning: Implementation address unchanged. Upgrade may have been a no-op.`,
      );
    }

    return upgraded;
  });
