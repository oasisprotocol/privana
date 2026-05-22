import { HardhatEthersSigner } from '@nomicfoundation/hardhat-ethers/signers';
import { HardhatNetworkHDAccountsConfig } from 'hardhat/types';
import { HttpNetworkConfig } from "hardhat/types/config";
import { config, ethers, network, upgrades } from 'hardhat';
import { JsonRpcProvider } from 'ethers';
import { AccountingHistory, MockAccounting } from "../typechain-types";

export const MOCK_ROFL_APP_ID = "0x" + "00".repeat(21); // bytes21

/**
 * Returns a signer configured with unwrapped provider (i.e. unencrypted transactions) for deploying or upgrading contracts.
 * Uses the first derived account from the configured hardhat phrase.
 * @returns HardhatEthersSigner
 */
export function getDeployer(index?: number): HardhatEthersSigner {
	const accounts = (config.networks.hardhat.accounts as HardhatNetworkHDAccountsConfig);
	const hdNode = ethers.HDNodeWallet.fromPhrase(accounts.mnemonic, undefined, `m/44'/60'/0'/0/${index ?? 0}`);

	// Only create unwrapped provider if chainId is between 0x5afd and 0x5aff (Sapphire networks)
	if (network.config.chainId && network.config.chainId >= 0x5afd && network.config.chainId <= 0x5aff) {
		const uwProvider = new JsonRpcProvider((network.config as HttpNetworkConfig).url);
		uwProvider.pollingInterval = 50;
		return hdNode.connect(uwProvider) as any;
	}

	return hdNode.connect(ethers.provider) as any;
}

/**
 * Encodes the given wallet address as auth token consumable by MockSiweAuth.
 * @param address Address of the account for authenticated calls
 * @returns Hex string of 32-bytes long token abi decodable by MockSiweAuth.
 */
export function mockAuthToken(address: string) {
	return ethers.hexlify(ethers.zeroPadValue(address, 32))
}

/**
 * Helper to deploy the mock accounting contract with an unencrypted transaction and with workarounds for flaky UUPS wrapper errors.
 * @param mockSiweAuthAddress SIWE auth contract to initialize Accounting with
 */
export async function deployMockAccounting(mockSiweAuthAddress: string) {
	const deployer = getDeployer();
	const AccountingFactory = await ethers.getContractFactory('MockAccounting', deployer);
	const AccountingHistoryFactory = await ethers.getContractFactory('AccountingHistory', deployer);
	let accounting: MockAccounting;
	let accountingHistory: AccountingHistory;

	let deploymentSucceeded = false;
	while (!deploymentSucceeded) {
		try {
			// Deploy as UUPS proxy
			accounting = await upgrades.deployProxy(
				AccountingFactory,
				[MOCK_ROFL_APP_ID, deployer.address],
				{ kind: 'uups', initializer: 'initialize', constructorArgs: [mockSiweAuthAddress] }
			) as unknown as MockAccounting;
			await accounting.waitForDeployment();
			accounting = (await ethers.getContractFactory('MockAccounting')).attach(await accounting.getAddress()) as unknown as MockAccounting;
			accountingHistory = await upgrades.deployProxy(
				AccountingHistoryFactory,
				[await accounting.getAddress(), deployer.address],
				{
					kind: 'uups',
					initializer: 'initialize',
					constructorArgs: [mockSiweAuthAddress],
					unsafeAllow: ['constructor', 'state-variable-immutable'],
				}
			) as unknown as AccountingHistory;
			await accountingHistory.waitForDeployment();
			const linkHistoryTx = await accounting.setAccountingHistory(await accountingHistory.getAddress());
			await linkHistoryTx.wait();
			//accounting = accounting.connect((await getSigners())[0]); // Use wrapped signer for sending txes.
			deploymentSucceeded = true;
		} catch (error) {
			console.log('Deployment failed, retrying...', error);
			await new Promise(resolve => setTimeout(resolve, 1000));
		}
	}
	return accounting!;
}
