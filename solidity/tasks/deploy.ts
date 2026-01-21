import { task } from "hardhat/config";

task("deploy")
  .addParam("shoyubashi", "The address of the ShoyuBashi oracle")
  .setAction(async (args, hre) => {
    const Accounting = await hre.ethers.getContractFactory("Accounting");
    const accounting = await Accounting.deploy(args.shoyubashi, {
      gasLimit: 10000000
    });
    const accountingAddr = await accounting.waitForDeployment();

    console.log(`Accounting address: ${accountingAddr.target}`);
    return accountingAddr.target;
  });
