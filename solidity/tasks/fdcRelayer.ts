import { task } from "hardhat/config";

task("setFDCRelayer")
  .addParam("address", "The address of the Accounting contract")
  .addParam("relayer", "The address to authorize as FDC relayer")
  .setAction(async (args, hre) => {
    const accounting = await hre.ethers.getContractAt("Accounting", args.address);
    const tx = await accounting.setFDCRelayer(args.relayer);
    console.log(`Transaction hash: ${tx.hash}`);
    const receipt = await tx.wait();
    console.log(`Transaction confirmed in block ${receipt?.blockNumber}`);
    console.log(`FDC relayer set to: ${args.relayer}`);
  });
