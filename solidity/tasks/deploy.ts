import { task } from "hardhat/config";
import { readFileSync, writeFileSync } from "fs";
import { join } from "path";
import { parseRoflAppId } from "./utils/rofl";

// Read VERSION from the local Accounting.sol.
function getAvailableAccountingVersion(): string {
  const source = readFileSync(join(__dirname, "..", "contracts", "Accounting.sol"), "utf8");
  const match = source.match(/string public constant VERSION = "([^"]+)"/);
  if (!match) {
    throw new Error("Could not find `VERSION` constant in contracts/Accounting.sol");
  }
  return match[1];
}

// Compare semver strings, e.g. "1.2.0" vs "1.10.0".
function compareVersions(a: string, b: string): number {
  const partsA = a.split(".").map(Number);
  const partsB = b.split(".").map(Number);
  for (let i = 0; i < Math.max(partsA.length, partsB.length); i++) {
    const diff = (partsA[i] || 0) - (partsB[i] || 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

// Safe's CreateCall library (canonical v1.4.1 deployment). A Safe cannot execute a raw
// contract creation, so deployments are routed through CreateCall.performCreate().
const SAFE_CREATE_CALL_ADDRESS = "0x9b35Af71d77eaf8d7e40252370304687390A1A52";
const SAFE_CREATE_CALL_ABI = [
  "function performCreate(uint256 value, bytes deploymentData) returns (address newContract)",
];

async function createSafeJson(to: string, data: string, name: string, description: string): Promise<string> {
  const chainId = (await hre.ethers.provider.getNetwork()).chainId.toString();

  const safeTransaction = {
    version: "1.0",
    chainId,
    createdAt: Date.now(),
    meta: {
      name,
      description,
      txBuilderVersion: "1.16.5",
    },
    transactions: [
      {
        to,
        value: "0",
        data,
      },
    ],
  };

  return JSON.stringify(safeTransaction, null, 2);
}

task("deploy")
  .addParam("roflappid", "The ROFL app ID (hex 0x... or bech32 rofl1...)")
  .addOptionalParam("siweauth", "The address of already deployed AccountingSiweAuth")
  .addOptionalParam("outputSafe", "Instead of submitting the transaction write it to file as Safe Transaction Builder JSON.")
  .setAction(async (args, hre) => {
    await hre.run("compile");

    const [deployer] = await hre.ethers.getSigners();

    // Parse ROFL app ID (supports hex and bech32 formats)
    const roflAppIdHex = parseRoflAppId(args.roflappid);

    // In Safe scenario, AccountingSiweAuth already needs to be deployed.
    if (args.outputSafe && !args.siweauth) {
      throw new Error(`Safe method requires the existing address of AccountingSiweAuth. Provide it with --siweauth parameter.`);
    }

    // Deploy AccountingSiweAuth if not provided.
    let siweAuthAddress: string = args.siweauth;
    if (!siweAuthAddress) {
      siweAuthAddress = await hre.run('deploy-siwe-auth', { roflappid: args.roflappid });
    }

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
    const implAddress = await hre.upgrades.erc1967.getImplementationAddress(proxyAddress);

    for (const address of [implAddress, proxyAddress]) {
      try {
        await hre.run("verify:sourcify", { address });
      } catch (err) {
        console.log(
          `Warning: Sourcify verification of ${address} failed or is unsupported on this network: ${(err as Error).message}`
        );
      }
    }

    console.log(`AccountingSiweAuth address: ${siweAuthAddress}`);
    console.log(`Accounting contract address: ${proxyAddress}`);
    console.log(`Accounting implementation address: ${implAddress}`);
    console.log(`EVM signing address: ${await proxy.evmAddress()}`);
    console.log(`Owner: ${await proxy.owner()}`);

    return proxyAddress;
  });

task("deploy-siwe-auth")
  .addParam("roflappid", "The ROFL app ID (hex 0x... or bech32 rofl1...)")
  .addOptionalParam("outputSafe", "Instead of submitting the transaction write it to file as Safe Transaction Builder JSON.")
  .setDescription("Deploy a new AccountingSiweAuth contract")
  .setAction(async (args, hre) => {
    await hre.run("compile");

    const roflAppIdHex = parseRoflAppId(args.roflappid);

    const AccountingSiweAuth = await hre.ethers.getContractFactory("AccountingSiweAuth");

    if (args.outputSafe) {
      const deployTx = await AccountingSiweAuth.getDeployTransaction(roflAppIdHex);
      const createCall = new hre.ethers.Interface(SAFE_CREATE_CALL_ABI);
      const data = createCall.encodeFunctionData("performCreate", [0, deployTx.data]);
      const json = await createSafeJson(
        SAFE_CREATE_CALL_ADDRESS,
        data,
        "Deploy AccountingSiweAuth",
        `Deploy AccountingSiweAuth with ROFL app ID ${args.roflappid}`
      );
      writeFileSync(args.outputSafe, json);
      console.log(`Safe Transaction Builder JSON written to ${args.outputSafe}`);
      console.log(`The deployed contract address will be emitted in the ContractCreation event of the Safe transaction.`);
      return;
    }

    const siweAuth = await AccountingSiweAuth.deploy(roflAppIdHex, {
      gasLimit: 10000000
    });
    await siweAuth.waitForDeployment();
    const siweAuthAddress = await siweAuth.getAddress();

    console.log(`AccountingSiweAuth deployed at: ${siweAuthAddress}`);
    console.log(`ROFL app ID: ${args.roflappid}`);

    return siweAuthAddress;
  });

task("upgrade")
  .addParam("address", "The UUPSUpgradeable proxy contract address for Accounting")
  .addOptionalParam("siweauth", "New AccountingSiweAuth address; keep existing one if omitted")
  .addOptionalParam("outputSafe", "Instead of submitting the transaction write it to file as Safe Transaction Builder JSON.")
  .setDescription("Upgrade the Accounting contract to a new implementation")
  .setAction(async (args, hre) => {
    await hre.run("compile");

    const Accounting = await hre.ethers.getContractFactory("Accounting");
    const current = await hre.ethers.getContractAt("Accounting", args.address);
    let siweAuthAddress: string = args.siweauth;

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

    // Get deployed implementation for comparison.
    const currentImpl = await hre.upgrades.erc1967.getImplementationAddress(args.address);
    console.log(`Current implementation: ${currentImpl}`);

    // Only upgrade if the deployed implementation's VERSION is lower than the one being deployed.
    const availableVersion = getAvailableAccountingVersion();
    let currentVersion = "0.0.0";
    try {
      currentVersion = await current.VERSION();
    } catch {}
    console.log(`Current version: ${currentVersion}, available version: ${availableVersion}`);

    if (compareVersions(currentVersion, availableVersion) >= 0) {
      console.log(`Skipping upgrade: deployed version ${currentVersion} is not lower than available version ${availableVersion}.`);
      return;
    }

    await hre.upgrades.forceImport(args.address, Accounting, {
      kind: 'uups',
      constructorArgs: [siweAuthAddress],
    });
    await hre.upgrades.validateUpgrade(args.address, Accounting, {
      kind: 'uups',
      constructorArgs: [siweAuthAddress],
    });

    let newImplAddress: string
    if (!args.outputSafe) {
      const upgraded = await hre.upgrades.upgradeProxy(args.address, Accounting, {
        kind: 'uups',
        constructorArgs: [siweAuthAddress],
        redeployImplementation: 'always',
        txOverrides: { gasLimit: 15000000 }
      });
      await upgraded.waitForDeployment();

      newImplAddress = await hre.upgrades.erc1967.getImplementationAddress(args.address);
      console.log(`Upgraded! New implementation: ${newImplAddress}`);

      if (currentImpl === newImplAddress) {
        console.log(`Warning: Implementation address unchanged. Upgrade may have been a no-op.`);
      }
    } else {
      newImplAddress = await hre.upgrades.prepareUpgrade(args.address, Accounting, {
        kind: 'uups',
        constructorArgs: [siweAuthAddress],
        redeployImplementation: 'always',
        txOverrides: { gasLimit: 15000000 }
      }) as string;

      console.log(`Deployed new proposed implementation: ${newImplAddress}`);
    }

    try {
      await hre.run("verify:sourcify", { address: newImplAddress });
    } catch (err) {
      console.log(
        `Warning: Sourcify verification failed or is unsupported on this network: ${(err as Error).message}`
      );
    }

    if (args.outputSafe) {
      const data = Accounting.interface.encodeFunctionData("upgradeToAndCall", [newImplAddress, "0x"]);
      const json = await createSafeJson(
        args.address,
        data,
        "Upgrade Accounting",
        `Upgrade Accounting contract ${args.address} to implementation ${newImplAddress}`
      );
      writeFileSync(args.outputSafe, json);
      console.log(`Safe Transaction Builder JSON written to ${args.outputSafe}`);
    }
  });
