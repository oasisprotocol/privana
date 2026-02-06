import { task } from "hardhat/config";

task("setGasPrice")
  .addParam("contract", "Accounting contract address")
  .addParam("chainid", "Chain ID")
  .addParam("gasprice", "Gas price in wei")
  .setAction(async (args, hre) => {
    const accounting = await hre.ethers.getContractAt(
      "Accounting",
      args.contract,
    );
    console.log(
      "Setting gas price for chain",
      args.chainid,
      "to",
      args.gasprice,
    );
    const tx = await accounting.setGasPrice(args.chainid, args.gasprice);
    console.log("Transaction:", tx.hash);
    await tx.wait();
    console.log("Done!");
  });
