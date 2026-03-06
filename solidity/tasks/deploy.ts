import { task } from "hardhat/config";
import { parseRoflAppId } from "./utils/rofl";

task("deploy")
  .addParam("shoyubashi", "The address of the ShoyuBashi oracle")
  .addParam("provethverifier", "The address of the ProvethVerifier contract")
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
      [args.shoyubashi, args.provethverifier, deployer.address],
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

    console.log(`AccountingSiweAuth address: ${siweAuthAddress}`);
    console.log(`Proxy address: ${proxyAddress}`);
    console.log(`Implementation address: ${implAddress}`);
    console.log(`EVM signing address: ${await proxy.evmAddress()}`);
    console.log(`Owner: ${await proxy.owner()}`);

    return proxyAddress;
  });

task("upgrade")
  .addParam("proxy", "The proxy contract address to upgrade")
  .addOptionalParam(
    "siweauth",
    "The AccountingSiweAuth address for the new implementation. If omitted, reuse proxy's current siweAuth",
  )
  .setAction(async (args, hre) => {
    const AccountingV2 = await hre.ethers.getContractFactory("Accounting");
    let siweAuthAddress: string = args.siweauth;

    if (!siweAuthAddress) {
      try {
        const current = await hre.ethers.getContractAt("Accounting", args.proxy);
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

    const upgraded = await hre.upgrades.upgradeProxy(args.proxy, AccountingV2, {
      kind: 'uups',
      constructorArgs: [siweAuthAddress],
      txOverrides: { gasLimit: 15000000 }
    });

    await upgraded.waitForDeployment();

    const newImplAddress = await hre.upgrades.erc1967.getImplementationAddress(args.proxy);
    console.log(`Upgraded! New implementation: ${newImplAddress}`);

    return upgraded;
  });
