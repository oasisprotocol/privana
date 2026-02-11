import { task } from "hardhat/config";

task("deploy")
  .addParam("shoyubashi", "The address of the ShoyuBashi oracle")
  .addParam("provethverifier", "The address of the ProvethVerifier contract")
  .setAction(async (args, hre) => {
    const [deployer] = await hre.ethers.getSigners();
    const Accounting = await hre.ethers.getContractFactory("Accounting");

    // Deploy as UUPS proxy
    const proxy = await hre.upgrades.deployProxy(
      Accounting,
      [args.shoyubashi, args.provethverifier, deployer.address],
      {
        kind: 'uups',
        initializer: 'initialize',
        txOverrides: { gasLimit: 15000000 }
      }
    );

    await proxy.waitForDeployment();

    const proxyAddress = await proxy.getAddress();
    const implAddress = await hre.upgrades.erc1967.getImplementationAddress(proxyAddress);

    console.log(`Proxy address: ${proxyAddress}`);
    console.log(`Implementation address: ${implAddress}`);
    console.log(`EVM signing address: ${await proxy.evmAddress()}`);
    console.log(`Owner: ${await proxy.owner()}`);

    return proxyAddress;
  });

task("upgrade")
  .addParam("proxy", "The proxy contract address to upgrade")
  .setAction(async (args, hre) => {
    const AccountingV2 = await hre.ethers.getContractFactory("Accounting");

    const upgraded = await hre.upgrades.upgradeProxy(args.proxy, AccountingV2, {
      kind: 'uups',
      txOverrides: { gasLimit: 15000000 }
    });

    await upgraded.waitForDeployment();

    const newImplAddress = await hre.upgrades.erc1967.getImplementationAddress(args.proxy);
    console.log(`Upgraded! New implementation: ${newImplAddress}`);

    return upgraded;
  });
