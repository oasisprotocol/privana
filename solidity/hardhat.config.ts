import {
  sapphireLocalnet,
  sapphireTestnet,
  sapphireMainnet,
} from '@oasisprotocol/sapphire-hardhat';
import '@nomicfoundation/hardhat-ignition-ethers';
import '@nomicfoundation/hardhat-toolbox';
import { HardhatUserConfig } from 'hardhat/config';
import { HDAccountsUserConfig } from 'hardhat/types';
import 'solidity-coverage';
import { task } from "hardhat/config";

task("deploy").setAction(async (_args, hre) => {
  const Accounting = await hre.ethers.getContractFactory("Accounting");
  const accounting = await Accounting.deploy();
  const accountingAddr = await accounting.waitForDeployment();

  console.log(`Accounting address: ${accountingAddr.target}`);
  return accountingAddr.target;
});

task("addEVMNativeToken").addParam("address", "The address of the Accounting contract").addParam("chainid", "Chain ID").setAction(async (args, hre) => {
  const accountingAddr = args.address;
  const accounting = await hre.ethers.getContractAt("Accounting", accountingAddr);
  const tokenType = 0; // EVM_NATIVE
  const chainId = args.chainid;
  const data = await accounting.encodeEVMNativeTokenData(chainId);
  const tx = await accounting.setTokenInfo({
    tokenType,
    data,
  });
  console.log(`Transaction hash: ${tx.hash}`);
  const receipt = await tx.wait();
  console.log(`Transaction confirmed in block ${receipt?.blockNumber}`);
  const tokenId = await accounting.getTokenId({
    tokenType,
    data,
  });
  console.log(`Token ID: ${tokenId}`);
});

const TEST_HDWALLET = {
  mnemonic: 'test test test test test test test test test test test junk',
  path: "m/44'/60'/0'/0",
  initialIndex: 0,
  count: 20,
  passphrase: '',
} as const satisfies HDAccountsUserConfig;

const accounts = process.env.PRIVATE_KEY
  ? [process.env.PRIVATE_KEY]
  : TEST_HDWALLET;

const config: HardhatUserConfig = {
  networks: {
    sapphire: { ...sapphireMainnet, accounts },
    'sapphire-testnet': { ...sapphireTestnet, accounts },
    'sapphire-localnet': { ...sapphireLocalnet, accounts },
  },
  solidity: {
    version: '0.8.20',
    settings: {
      // XXX: Needs to match https://github.com/oasisprotocol/sapphire-paratime/blob/main/contracts/hardhat.config.ts
      optimizer: {
        enabled: true,
        runs: (1 << 32) - 1,
      },
      viaIR: true,
    },
  },
};

export default config;
