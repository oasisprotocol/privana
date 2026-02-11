import { config as dotenvConfig } from 'dotenv';
import {
  sapphireLocalnet,
  sapphireTestnet,
  sapphireMainnet,
} from '@oasisprotocol/sapphire-hardhat';
import '@nomicfoundation/hardhat-ignition-ethers';
import '@nomicfoundation/hardhat-toolbox';
import '@openzeppelin/hardhat-upgrades';
import { HardhatUserConfig } from 'hardhat/config';
import { HDAccountsUserConfig } from 'hardhat/types';
import 'solidity-coverage';
import './tasks';

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
      accounts: TEST_HDWALLET
    }
  },
  sourcify: {
    enabled: true
  },
  solidity: {
    version: '0.8.24',
    settings: {
      optimizer: {
        enabled: true,
        runs: 200,
      },
      viaIR: true,
    },
  },
};

export default config;
