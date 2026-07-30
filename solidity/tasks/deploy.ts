import { readFileSync, writeFileSync } from "fs";
import { join } from "path";

import '@nomicfoundation/hardhat-ethers';
import '@oasisprotocol/sapphire-hardhat';
import '@typechain/hardhat';
import {JsonRpcProvider} from "ethers";
import { task } from "hardhat/config";
import {HardhatEthersSigner} from "@nomicfoundation/hardhat-ethers/signers";
import {HardhatRuntimeEnvironment} from "hardhat/types";
import {HttpNetworkConfig} from "hardhat/types/config";
import { parseRoflAppId } from "./utils/rofl";

// Return unwrapped Sapphire client bound to SECRET_KEY with plain text
// transactions. Used for all contract management that should be public.
async function getUwDeployer(hre: HardhatRuntimeEnvironment): Promise<HardhatEthersSigner> {
  const { network } = hre;
  const uwProvider = new JsonRpcProvider((network.config as HttpNetworkConfig).url);
  return new hre.ethers.Wallet(process.env.SECRET_KEY as string, uwProvider) as any;
}

// Read VERSION from the local Accounting.sol.
function getAvailableAccountingVersion(): bigint {
  const source = readFileSync(join(__dirname, "..", "contracts", "Accounting.sol"), "utf8");
  const match = source.match(/uint64 public constant VERSION = (\d+)/);
  if (!match) {
    throw new Error("Could not find `VERSION` constant in contracts/Accounting.sol");
  }
  return BigInt(match[1]);
}

// Safe's CreateCall library (canonical v1.4.1 deployment). A Safe cannot execute a raw
// contract creation, so deployments are routed through CreateCall.performCreate().
const SAFE_CREATE_CALL_ADDRESS = "0x9b35Af71d77eaf8d7e40252370304687390A1A52";
const SAFE_CREATE_CALL_ABI = [
  "function performCreate(uint256 value, bytes deploymentData) returns (address newContract)",
];

async function createSafeJson(to: string, data: string, name: string, description: string, chainId: string): Promise<string> {
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
  .addOptionalParam("owner", "Address to own the deployed Accounting proxy (e.g. a Safe); defaults to the deployer")
  .setAction(async (args, hre) => {
    await hre.run("compile");

    const deployer = await getUwDeployer(hre);
    const ownerAddress: string = args.owner ?? deployer.address;

    if (!hre.ethers.isAddress(ownerAddress)) {
      throw new Error(`Invalid owner address: ${ownerAddress}`);
    }

    // Parse ROFL app ID (supports hex and bech32 formats)
    const roflAppIdHex = parseRoflAppId(args.roflappid);

    // Deploy AccountingSiweAuth if not provided.
    let siweAuthAddress: string = args.siweauth;
    if (!siweAuthAddress) {
      siweAuthAddress = await hre.run('deploy-siwe-auth', { roflappid: args.roflappid });
    }
    if (!hre.ethers.isAddress(siweAuthAddress)) {
      throw new Error(`Invalid siweAuthAddress address: ${siweAuthAddress}`);
    }

    // Deploy Accounting as UUPS proxy (siweAuth passed as constructor arg for immutable).
    // `ownerAddress` (e.g. a Safe) is set directly via initialize(), independent of who
    // sends this deployment transaction — no Safe transaction is needed to hand over
    // ownership.
    const Accounting = await hre.ethers.getContractFactory("Accounting", deployer);
    const proxy = await hre.upgrades.deployProxy(
      Accounting,
      [roflAppIdHex, ownerAddress],
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

    console.log(`Accounting contract address: ${proxyAddress}`);
    console.log(`Accounting implementation address: ${implAddress}`);
    console.log(`AccountingSiweAuth address: ${siweAuthAddress}`);
    console.log(`EVM signing address: ${await proxy.evmAddress()}`);
    console.log(`Owner: ${await proxy.owner()}`);

    try {
      await hre.run("verify:sourcify", { address: implAddress, contract: "Accounting" });
    } catch (err) {
      console.log(
        `Warning: Sourcify verification of implementation ${implAddress} failed or is unsupported on this network: ${(err as Error).message}`
      );
    }
    try {
      await hre.run("verify:sourcify", { address: proxyAddress, proxy: true });
    } catch (err) {
      console.log(
        `Warning: Sourcify verification of proxy ${proxyAddress} failed or is unsupported on this network: ${(err as Error).message}`
      );
    }

    return proxyAddress;
  });

task("deploy-siwe-auth")
  .addParam("roflappid", "The ROFL app ID (hex 0x... or bech32 rofl1...)")
  .addOptionalParam("outputSafe", "Instead of submitting the transaction write it to file as Safe Transaction Builder JSON.")
  .setDescription("Deploy a new AccountingSiweAuth contract")
  .setAction(async (args, hre) => {
    await hre.run("compile");

    const roflAppIdHex = parseRoflAppId(args.roflappid);

    const AccountingSiweAuth = await hre.ethers.getContractFactory("AccountingSiweAuth", await getUwDeployer(hre));

    if (args.outputSafe) {
      const deployTx = await AccountingSiweAuth.getDeployTransaction(roflAppIdHex);
      const createCall = new hre.ethers.Interface(SAFE_CREATE_CALL_ABI);
      const data = createCall.encodeFunctionData("performCreate", [0, deployTx.data]);
      const json = await createSafeJson(
        SAFE_CREATE_CALL_ADDRESS,
        data,
        "Deploy AccountingSiweAuth",
        `Deploy AccountingSiweAuth with ROFL app ID ${args.roflappid}`,
        (await hre.ethers.provider.getNetwork()).chainId.toString()
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

    try {
      await hre.run("verify:sourcify", { address: siweAuthAddress, contract: "AccountingSiweAuth" });
    } catch (err) {
      console.log(
        `Warning: Sourcify verification of implementation ${siweAuthAddress} failed or is unsupported on this network: ${(err as Error).message}`
      );
    }

    return siweAuthAddress;
  });

task("force-import")
  .addParam("proxy", "The proxy contract address to import")
  .setDescription("Force import an existing proxy into OpenZeppelin's deployment state")
  .setAction(async (args, hre) => {
    await hre.run("compile");

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
  .addParam("address", "The UUPSUpgradeable proxy contract address for Accounting")
  .addOptionalParam("siweauth", "New AccountingSiweAuth address; keep existing one if omitted")
  .addOptionalParam("outputSafe", "Instead of submitting the transaction write it to file as Safe Transaction Builder JSON.")
  .setDescription("Upgrade the Accounting contract to a new implementation")
  .setAction(async (args, hre) => {
    await hre.run("compile");

    const Accounting = await hre.ethers.getContractFactory("Accounting", await getUwDeployer(hre));
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
    let currentVersion = 0n;
    try {
      currentVersion = await current.VERSION();
    } catch {}
    console.log(`Current version: ${currentVersion}, available version: ${availableVersion}`);

    if (currentVersion >= availableVersion) {
      console.log(`Skipping upgrade: deployed version ${currentVersion} is not lower than available version ${availableVersion}.`);
      return;
    }

    // Check the current implementation in .openzeppelin folder with the proposed one.
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
      // await upgraded.waitForDeployment(); doesn't work for unwrapped providers.
      // Extract the upgrade tx and wait for it directly.
      const upgradeTx = (upgraded as unknown as { deployTransaction?: { wait: () => Promise<unknown> } }).deployTransaction;
      await upgradeTx!.wait();

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
      await hre.run("verify:sourcify", { address: newImplAddress, contract: "Accounting" });
    } catch (err) {
      if (args.outputSafe) {
        // Verification is critical for a Safe artifact: signers rely on it to confirm the
        // bytecode they're approving actually matches this source before executing on-chain.
        throw new Error(
          `Sourcify verification of new implementation ${newImplAddress} failed, refusing to produce a Safe transaction for an unverified upgrade: ${(err as Error).message}`
        );
      }
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
        `Upgrade Accounting contract ${args.address} to implementation ${newImplAddress}`,
        (await hre.ethers.provider.getNetwork()).chainId.toString()
      );
      writeFileSync(args.outputSafe, json);
      console.log(`Safe Transaction Builder JSON written to ${args.outputSafe}`);
    }
  });

task("transferOwnership")
  .addParam("address", "The Accounting proxy contract address")
  .addParam("newowner", "The address of the new owner")
  .addOptionalParam("outputSafe", "Instead of submitting the transaction write it to file as Safe Transaction Builder JSON.")
  .setDescription("Transfer ownership of the Accounting contract to a new address")
  .setAction(async (args, hre) => {
    await hre.run("compile");

    if (!hre.ethers.isAddress(args.newowner)) {
      throw new Error(`Invalid new owner address: ${args.newowner}`);
    }

    const accounting = await hre.ethers.getContractAt("Accounting", args.address, await getUwDeployer(hre));
    const currentOwner = await accounting.owner();
    console.log(`Current owner: ${currentOwner}`);
    console.log(`New owner:     ${args.newowner}`);

    if (currentOwner.toLowerCase() === (args.newowner as string).toLowerCase()) {
      console.log("Skipping: new owner is already the current owner.");
      return;
    }

    if (args.outputSafe) {
      const data = accounting.interface.encodeFunctionData("transferOwnership", [args.newowner]);
      const json = await createSafeJson(
        args.address,
        data,
        "Transfer Accounting Ownership",
        `Transfer ownership of Accounting contract ${args.address} to ${args.newowner}`,
        (await hre.ethers.provider.getNetwork()).chainId.toString()
      );
      writeFileSync(args.outputSafe, json);
      console.log(`Safe Transaction Builder JSON written to ${args.outputSafe}`);
      return;
    }

    const tx = await accounting.transferOwnership(args.newowner);
    console.log(`Transaction hash: ${tx.hash}`);
    await tx.wait();

    console.log(`Ownership transferred. New owner: ${await accounting.owner()}`);
  });
