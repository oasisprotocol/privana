import { config as dotenvConfig } from 'dotenv';
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

dotenvConfig();

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

task("addEVMErc20Token").addParam("address", "The address of the Accounting contract").addParam("chainid", "Chain ID").addParam("tokenaddress", "ERC20 token address").setAction(async (args, hre) => {
  const accountingAddr = args.address;
  const accounting = await hre.ethers.getContractAt("Accounting", accountingAddr);
  const tokenType = 1;
  const chainId = args.chainid;
  const tokenAddress = args.tokenaddress;
  const data = await accounting.encodeEVMErc20TokenData(chainId, tokenAddress);
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

task("sign")
  .addParam("contract", "The address of the Accounting contract")
  .addParam("type", "Signature type: lock, transfer, transferlocked, or withdraw")
  .addParam("user", "User address")
  .addParam("amount", "Amount (in wei)")
  .addOptionalParam("tokenid", "Token ID (32-byte hex, required for lock/transfer/withdraw)")
  .addOptionalParam("to", "Recipient address (required for transfer/transferlocked)")
  .addOptionalParam("service", "Service address (required for lock)")
  .addOptionalParam("expiry", "Lock expiry timestamp (required for lock)")
  .addOptionalParam("lockindex", "Lock index (required for transferlocked)")
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const accounting = await hre.ethers.getContractAt("Accounting", args.contract);

    const domainTuple = await accounting.eip712Domain();
    const domain = {
      name: domainTuple[1],
      version: domainTuple[2],
      chainId: Number(domainTuple[3]),
      verifyingContract: domainTuple[4],
    };

    const signatureType = args.type.toLowerCase();
    let types: any;
    let message: any;

    switch (signatureType) {
      case "lock":
        if (!args.tokenid || !args.service || !args.expiry) {
          throw new Error("Lock requires: tokenid, service, and expiry");
        }
        types = {
          Lock: [
            { name: "userAddress", type: "address" },
            { name: "serviceAddress", type: "address" },
            { name: "tokenId", type: "bytes32" },
            { name: "amount", type: "uint256" },
            { name: "expiry", type: "uint256" },
          ]
        };
        message = {
          userAddress: args.user,
          serviceAddress: args.service,
          tokenId: args.tokenid,
          amount: args.amount,
          expiry: args.expiry,
        };
        break;

      case "transfer":
        if (!args.tokenid || !args.to) {
          throw new Error("Transfer requires: tokenid and to");
        }
        types = {
          Transfer: [
            { name: "userAddress", type: "address" },
            { name: "toAddress", type: "address" },
            { name: "tokenId", type: "bytes32" },
            { name: "amount", type: "uint256" },
          ]
        };
        message = {
          userAddress: args.user,
          toAddress: args.to,
          tokenId: args.tokenid,
          amount: args.amount,
        };
        break;

      case "transferlocked":
        if (!args.to || args.lockindex === undefined) {
          throw new Error("TransferLocked requires: to and lockindex");
        }
        types = {
          TransferLocked: [
            { name: "userAddress", type: "address" },
            { name: "toAddress", type: "address" },
            { name: "lockIndex", type: "uint256" },
            { name: "amount", type: "uint256" },
          ]
        };
        message = {
          userAddress: args.user,
          toAddress: args.to,
          lockIndex: args.lockindex,
          amount: args.amount,
        };
        break;

      case "withdraw":
        if (!args.tokenid) {
          throw new Error("Withdraw requires: tokenid");
        }
        types = {
          Withdraw: [
            { name: "userAddress", type: "address" },
            { name: "tokenId", type: "bytes32" },
            { name: "amount", type: "uint256" },
          ]
        };
        message = {
          userAddress: args.user,
          tokenId: args.tokenid,
          amount: args.amount,
        };
        break;

      default:
        throw new Error(`Unknown signature type: ${signatureType}. Valid types: lock, transfer, transferlocked, withdraw`);
    }

    const signature = await signer.signTypedData(domain, types, message);
    console.log(`Signature: ${signature}`);
    return signature;
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
  sourcify: {
    enabled: true
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
