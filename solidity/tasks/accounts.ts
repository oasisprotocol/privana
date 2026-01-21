import { task } from "hardhat/config";
import { HDAccountsUserConfig } from "hardhat/types";

task("accounts")
  .addOptionalParam("idx", "Account index")
  .setAction(async (args, hre) => {
    const accounts = hre.config.networks.hardhat.accounts as HDAccountsUserConfig;
    const wallet1 = hre.ethers.Wallet.fromPhrase(accounts.mnemonic);

    console.log("private key:", wallet1.privateKey);
    console.log("address:", wallet1.address);
  });
