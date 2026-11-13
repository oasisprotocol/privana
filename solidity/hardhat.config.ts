import { config as dotenvConfig } from 'dotenv';
import { join } from 'path';

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

import './tasks';

// Fixate .openzeppelin location to this folder.
if (!process.env.MANIFEST_DEFAULT_DIR) {
  process.env.MANIFEST_DEFAULT_DIR = join(__dirname, '.openzeppelin');
}

import '@openzeppelin/hardhat-upgrades'; // NB: Must be imported after hardhat packages to preserve network configuration!

dotenvConfig();

const TEST_HDWALLET = {
  mnemonic: 'chimney theory present latin find behave ankle clock shadow earn suit reflect',
  path: "m/44'/60'/0'/0",
  initialIndex: 0,
  count: 20,
  passphrase: '',
} as const satisfies HDAccountsUserConfig;

const SECRET_KEY = process.env.SECRET_KEY;

const accounts = SECRET_KEY ? [SECRET_KEY] : TEST_HDWALLET;

const config: HardhatUserConfig = {
  networks: {
    sapphire: { ...sapphireMainnet, accounts },
    'sapphire-testnet': { ...sapphireTestnet, accounts },
    'sapphire-localnet': { ...sapphireLocalnet, accounts },
    'base-sepolia': {
      url: process.env.BASE_SEPOLIA_RPC_URL || 'https://sepolia.base.org',
      chainId: 84532,
      accounts,
    },
    hardhat: {
      accounts: TEST_HDWALLET,
      // Accounting may exceed the EIP-170 24576-byte cap; Sapphire allows 64 KiB
      // (see scripts/check-bytecode-size.ts), so lift the cap on the in-process
      // test network too.
      allowUnlimitedContractSize: true,
    }
  },
  sourcify: {
    enabled: true
  },
  solidity: {
    version: '0.8.24',
    settings: {
      evmVersion: 'paris',
      optimizer: {
        enabled: true,
        // Keep bytecode size within the Sapphire 64 KiB budget enforced by
        // scripts/check-bytecode-size.ts; large "runs" values bloat size significantly.
        runs: 20,
      },
      viaIR: true,
    },
  },
};

export default config;
