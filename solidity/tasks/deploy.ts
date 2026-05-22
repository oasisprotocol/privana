import { task } from "hardhat/config";
import { parseRoflAppId } from "./utils/rofl";

function normalizeAddress(address: string): string {
  return address.toLowerCase();
}

function isMissingAccountingHistoryGetter(error: any): boolean {
  const result = error?.value ?? error?.data ?? error?.result;
  return (
    error?.code === "BAD_DATA" ||
    (error?.code === "CALL_EXCEPTION" && result === "0x")
  );
}

async function deployAccountingHistory(
  hre: any,
  accountingAddress: string,
  ownerAddress: string,
  siweAuthAddress: string
) {
  const AccountingHistory =
    await hre.ethers.getContractFactory("AccountingHistory");
  const history = await hre.upgrades.deployProxy(
    AccountingHistory,
    [accountingAddress, ownerAddress],
    {
      kind: "uups",
      initializer: "initialize",
      constructorArgs: [siweAuthAddress],
      unsafeAllow: ["constructor", "state-variable-immutable"],
      txOverrides: { gasLimit: 15000000 },
    }
  );
  await history.waitForDeployment();
  return history;
}

async function validateAccountingHistory(
  hre: any,
  historyAddress: string,
  accountingAddress: string,
  siweAuthAddress: string
) {
  if (
    !hre.ethers.isAddress(historyAddress) ||
    historyAddress === hre.ethers.ZeroAddress
  ) {
    throw new Error(`Invalid AccountingHistory address: ${historyAddress}`);
  }

  const code = await hre.ethers.provider.getCode(historyAddress);
  if (code === "0x") {
    throw new Error(
      `AccountingHistory address has no contract code: ${historyAddress}`
    );
  }

  const history = await hre.ethers.getContractAt(
    "AccountingHistory",
    historyAddress
  );
  const boundAccounting = await history.accounting();
  if (
    normalizeAddress(boundAccounting) !== normalizeAddress(accountingAddress)
  ) {
    throw new Error(
      `AccountingHistory is bound to ${boundAccounting}, expected ${accountingAddress}`
    );
  }

  const boundSiweAuth = await history.siweAuth();
  if (normalizeAddress(boundSiweAuth) !== normalizeAddress(siweAuthAddress)) {
    throw new Error(
      `AccountingHistory uses SIWE auth ${boundSiweAuth}, expected ${siweAuthAddress}`
    );
  }

  return history;
}

async function readLinkedAccountingHistory(
  hre: any,
  accounting: any
): Promise<string> {
  try {
    return await accounting.accountingHistory();
  } catch (error) {
    if (!isMissingAccountingHistoryGetter(error)) {
      throw error;
    }
    // Pre-AccountingHistory implementations do not expose this getter.
    return hre.ethers.ZeroAddress;
  }
}

async function resolveAccountingHistory(
  hre: any,
  accounting: any,
  accountingAddress: string,
  ownerAddress: string,
  siweAuthAddress: string,
  requestedHistoryAddress?: string
) {
  let historyAddress = requestedHistoryAddress;

  if (historyAddress) {
    await validateAccountingHistory(
      hre,
      historyAddress,
      accountingAddress,
      siweAuthAddress
    );
  } else {
    historyAddress = await readLinkedAccountingHistory(hre, accounting);

    if (historyAddress === hre.ethers.ZeroAddress) {
      const history = await deployAccountingHistory(
        hre,
        accountingAddress,
        ownerAddress,
        siweAuthAddress
      );
      historyAddress = await history.getAddress();
      const historyImpl =
        await hre.upgrades.erc1967.getImplementationAddress(historyAddress);
      console.log(`AccountingHistory proxy address: ${historyAddress}`);
      console.log(`AccountingHistory implementation address: ${historyImpl}`);
      await validateAccountingHistory(
        hre,
        historyAddress,
        accountingAddress,
        siweAuthAddress
      );
    } else {
      await validateAccountingHistory(
        hre,
        historyAddress,
        accountingAddress,
        siweAuthAddress
      );
      console.log(
        `Existing AccountingHistory proxy address: ${historyAddress}`
      );
    }
  }

  return historyAddress;
}

async function ensureAccountingHistory(
  hre: any,
  accounting: any,
  accountingAddress: string,
  ownerAddress: string,
  siweAuthAddress: string,
  requestedHistoryAddress?: string
) {
  const historyAddress = await resolveAccountingHistory(
    hre,
    accounting,
    accountingAddress,
    ownerAddress,
    siweAuthAddress,
    requestedHistoryAddress
  );
  await validateAccountingHistory(
    hre,
    historyAddress,
    accountingAddress,
    siweAuthAddress
  );

  const linkedHistoryAddress = await readLinkedAccountingHistory(
    hre,
    accounting
  );
  if (
    normalizeAddress(linkedHistoryAddress) !== normalizeAddress(historyAddress)
  ) {
    if (linkedHistoryAddress !== hre.ethers.ZeroAddress) {
      throw new Error(
        `Accounting is already linked to AccountingHistory ${linkedHistoryAddress}, expected ${historyAddress}`
      );
    }
    const tx = await accounting.setAccountingHistory(historyAddress);
    await tx.wait();
    console.log(`Accounting linked to AccountingHistory: ${historyAddress}`);
  }

  return historyAddress;
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
    const AccountingSiweAuth = await hre.ethers.getContractFactory("AccountingSiweAuth");
    const siweAuth = await AccountingSiweAuth.deploy(roflAppIdHex, {
      gasLimit: 10000000
    });
    await siweAuth.waitForDeployment();
    const siweAuthAddress = await siweAuth.getAddress();

    // Deploy Accounting as UUPS proxy (siweAuth passed as constructor arg for immutable)
    const Accounting = await hre.ethers.getContractFactory("Accounting");
    const proxy = await hre.upgrades.deployProxy(
      Accounting,
      [roflAppIdHex, deployer.address],
      {
        kind: 'uups',
        initializer: 'initialize',
        constructorArgs: [siweAuthAddress],
        txOverrides: { gasLimit: 15000000 }
      }
    );

    await proxy.waitForDeployment();

    const proxyAddress = await proxy.getAddress();
    const historyAddress = await ensureAccountingHistory(
      hre,
      proxy,
      proxyAddress,
      deployer.address,
      siweAuthAddress
    );
    const implAddress =
      await hre.upgrades.erc1967.getImplementationAddress(proxyAddress);

    console.log(`AccountingSiweAuth address: ${siweAuthAddress}`);
    console.log(`Proxy address: ${proxyAddress}`);
    console.log(`AccountingHistory address: ${historyAddress}`);
    console.log(`Implementation address: ${implAddress}`);
    console.log(`EVM signing address: ${await proxy.evmAddress()}`);
    console.log(`Owner: ${await proxy.owner()}`);

    return proxyAddress;
  });

task("deploy-siwe-auth")
  .addParam("roflappid", "The ROFL app ID (hex 0x... or bech32 rofl1...)")
  .setDescription("Deploy a new AccountingSiweAuth contract")
  .setAction(async (args, hre) => {
    const roflAppIdHex = parseRoflAppId(args.roflappid);

    const AccountingSiweAuth = await hre.ethers.getContractFactory("AccountingSiweAuth");
    const siweAuth = await AccountingSiweAuth.deploy(roflAppIdHex, {
      gasLimit: 10000000
    });
    await siweAuth.waitForDeployment();
    const siweAuthAddress = await siweAuth.getAddress();

    console.log(`AccountingSiweAuth deployed at: ${siweAuthAddress}`);
    console.log(`ROFL app ID: ${args.roflappid}`);

    return siweAuthAddress;
  });

task("force-import")
  .addParam("proxy", "The proxy contract address to import")
  .setDescription("Force import an existing proxy into OpenZeppelin's deployment state")
  .setAction(async (args, hre) => {
    const Accounting = await hre.ethers.getContractFactory("Accounting");

    // Get current siweAuth from proxy
    const current = await hre.ethers.getContractAt("Accounting", args.proxy);
    const siweAuthAddress = await current.siweAuth();
    console.log(`Current siweAuth: ${siweAuthAddress}`);

    await hre.upgrades.forceImport(args.proxy, Accounting, {
      kind: 'uups',
      constructorArgs: [siweAuthAddress],
    });

    console.log(`Proxy ${args.proxy} imported successfully`);
  });

task("upgrade")
  .addParam("proxy", "The proxy contract address to upgrade")
  .addOptionalParam(
    "siweauth",
    "The AccountingSiweAuth address for the new implementation. If omitted, reuse proxy's current siweAuth"
  )
  .addOptionalParam(
    "history",
    "Existing AccountingHistory proxy to attach. If omitted and none is set, deploy one."
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
          "Could not resolve current siweAuth from proxy. Pass --siweauth <address> for this upgrade."
        );
      }
    }

    if (!hre.ethers.isAddress(siweAuthAddress)) {
      throw new Error(`Invalid siweAuth address: ${siweAuthAddress}`);
    }

    // Get current implementation for comparison
    const currentImpl = await hre.upgrades.erc1967.getImplementationAddress(
      args.proxy
    );
    console.log(`Current implementation: ${currentImpl}`);

    const ownerAddress = await current.owner();
    const currentHistoryAddress = await readLinkedAccountingHistory(
      hre,
      current
    );
    const historyAddress = await resolveAccountingHistory(
      hre,
      current,
      args.proxy,
      ownerAddress,
      siweAuthAddress,
      args.history
    );
    const upgradeOptions: any = {
      kind: 'uups',
      constructorArgs: [siweAuthAddress],
      redeployImplementation: 'always',
      txOverrides: { gasLimit: 15000000 },
    };
    if (
      normalizeAddress(currentHistoryAddress) !==
      normalizeAddress(historyAddress)
    ) {
      if (currentHistoryAddress !== hre.ethers.ZeroAddress) {
        throw new Error(
          `Accounting is already linked to AccountingHistory ${currentHistoryAddress}, expected ${historyAddress}`
        );
      }
      upgradeOptions.call = {
        fn: "setAccountingHistory",
        args: [historyAddress],
      };
    }

    // Always redeploy implementation to avoid caching issues
    const upgraded = await hre.upgrades.upgradeProxy(
      args.proxy,
      Accounting,
      upgradeOptions
    );

    await upgraded.waitForDeployment();

    const newImplAddress = await hre.upgrades.erc1967.getImplementationAddress(
      args.proxy
    );
    console.log(`Upgraded! New implementation: ${newImplAddress}`);
    const linkedHistoryAddress = await upgraded.accountingHistory();
    if (
      normalizeAddress(linkedHistoryAddress) !==
      normalizeAddress(historyAddress)
    ) {
      throw new Error(
        `AccountingHistory link mismatch after upgrade: ${linkedHistoryAddress}, expected ${historyAddress}`
      );
    }
    await validateAccountingHistory(
      hre,
      historyAddress,
      args.proxy,
      siweAuthAddress
    );
    console.log(`AccountingHistory address: ${historyAddress}`);

    if (currentImpl === newImplAddress) {
      console.log(
        `Warning: Implementation address unchanged. Upgrade may have been a no-op.`
      );
    }

    return upgraded;
  });
