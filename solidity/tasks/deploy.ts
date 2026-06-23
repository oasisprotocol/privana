import { task } from "hardhat/config";
import { parseRoflAppId } from "./utils/rofl";

type UpgradeOptions = {
  kind: "uups";
  constructorArgs: string[];
  unsafeAllow: string[];
  redeployImplementation: "always";
  txOverrides: { gasLimit: number };
};

const ACCOUNTING_UNSAFE_ALLOW = ["constructor", "state-variable-immutable"];

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

    // siweAuth is a constructor arg (it backs an immutable, so it is set at
    // implementation-deploy time, not in the initializer).
    const Accounting = await hre.ethers.getContractFactory("Accounting");
    const proxy = await hre.upgrades.deployProxy(
      Accounting,
      [roflAppIdHex, deployer.address],
      {
        kind: "uups",
        initializer: "initialize",
        constructorArgs: [siweAuthAddress],
        unsafeAllow: ACCOUNTING_UNSAFE_ALLOW,
        txOverrides: { gasLimit: 15000000 },
      },
    );

    await proxy.waitForDeployment();

    const proxyAddress = await proxy.getAddress();
    const implAddress =
      await hre.upgrades.erc1967.getImplementationAddress(proxyAddress);

    console.log(`AccountingSiweAuth address: ${siweAuthAddress}`);
    console.log(`Proxy address: ${proxyAddress}`);
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
      unsafeAllow: ACCOUNTING_UNSAFE_ALLOW,
    });

    console.log(`Proxy ${args.proxy} imported successfully`);
  });

task("upgrade")
  .addParam("proxy", "The proxy contract address to upgrade")
  .addOptionalParam(
    "siweauth",
    "The AccountingSiweAuth address for the new implementation. If omitted, reuse proxy's current siweAuth",
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

    const upgradeOptions: UpgradeOptions = {
      kind: "uups",
      constructorArgs: [siweAuthAddress],
      unsafeAllow: ACCOUNTING_UNSAFE_ALLOW,
      redeployImplementation: "always",
      txOverrides: { gasLimit: 15000000 },
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

    if (currentImpl === newImplAddress) {
      console.log(
        `Warning: Implementation address unchanged. Upgrade may have been a no-op.`,
      );
    }

    return upgraded;
  });
