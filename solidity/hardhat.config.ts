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
import './tasks';

import '@openzeppelin/hardhat-upgrades'; // NB: Must be imported after hardhat packages to preserve network configuration!

dotenvConfig();

const TEST_HDWALLET = {
  mnemonic: 'chimney theory present latin find behave ankle clock shadow earn suit reflect',
  path: "m/44'/60'/0'/0",
  initialIndex: 0,
  count: 20,
  passphrase: '',
} as const satisfies HDAccountsUserConfig;

const PRIVATE_KEY = process.env.PRIVATE_KEY;

const accounts = PRIVATE_KEY ? [PRIVATE_KEY] : TEST_HDWALLET;

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
        // Keep bytecode size below EIP-170 limits; large "runs" can bloat size significantly.
        runs: 20,
      },
      viaIR: true,
    },
  },
};

export default config;
