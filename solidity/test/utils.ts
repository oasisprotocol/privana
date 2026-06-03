import { HardhatEthersSigner } from '@nomicfoundation/hardhat-ethers/signers';
import { HardhatNetworkHDAccountsConfig } from 'hardhat/types';
import { HttpNetworkConfig } from "hardhat/types/config";
import { config, ethers, network, upgrades } from 'hardhat';
import { JsonRpcProvider } from 'ethers';
import { LockModule, MockAccounting } from "../typechain-types";
import { getCombinedAccountingAt } from "./util/links";

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
 * Combined-ABI handle bound to the Accounting proxy: the union of
 * `MockAccounting` (resident selectors) and `LockModule` (lock selectors routed
 * through the proxy fallback via delegatecall). Cast call sites to this type so
 * `accounting.createLock(...)` etc. keep their static typing now that the lock
 * subsystem lives in the delegated module.
 */
export type CombinedMockAccounting = MockAccounting & LockModule;

/**
 * Helper to deploy the mock accounting contract with an unencrypted transaction and with workarounds for flaky UUPS wrapper errors.
 * Wires the delegated history and lock modules, then returns a combined-ABI
 * handle so both resident and lock selectors are callable at the proxy address.
 * @param mockSiweAuthAddress SIWE auth contract to initialize Accounting with
 */
export async function deployMockAccounting(mockSiweAuthAddress: string): Promise<CombinedMockAccounting> {
	const deployer = getDeployer();
	const AccountingFactory = await ethers.getContractFactory('MockAccounting', deployer);
	const AccountingHistoryModuleFactory = await ethers.getContractFactory('AccountingHistoryModule', deployer);
	let accounting: MockAccounting;

	let deploymentSucceeded = false;
	while (!deploymentSucceeded) {
		try {
			// Deploy as UUPS proxy
			accounting = await upgrades.deployProxy(
				AccountingFactory,
				[MOCK_ROFL_APP_ID, deployer.address],
				{
					kind: 'uups',
					initializer: 'initialize',
					constructorArgs: [mockSiweAuthAddress],
					unsafeAllow: ['constructor', 'state-variable-immutable', 'delegatecall'],
				}
			) as unknown as MockAccounting;
			await accounting.waitForDeployment();
			accounting = (await ethers.getContractFactory('MockAccounting')).attach(await accounting.getAddress()) as unknown as MockAccounting;
			const historyModule = await AccountingHistoryModuleFactory.deploy();
			await historyModule.waitForDeployment();
			const linkHistoryTx = await accounting.setHistoryModule(await historyModule.getAddress());
			await linkHistoryTx.wait();
			// Wire the delegated lock module so createLock / modifyLock / ... route
			// through the proxy fallback to LockModule via delegatecall.
			const LockModuleFactory = await ethers.getContractFactory('LockModule', deployer);
			const lockModule = await LockModuleFactory.deploy();
			await lockModule.waitForDeployment();
			const linkLockTx = await accounting.setLockModule(await lockModule.getAddress());
			await linkLockTx.wait();
			deploymentSucceeded = true;
		} catch (error) {
			console.log('Deployment failed, retrying...', error);
			await new Promise(resolve => setTimeout(resolve, 1000));
		}
	}
	const proxyAddr = await accounting!.getAddress();
	return (await getCombinedAccountingAt(proxyAddr, deployer, ['MockAccounting', 'LockModule'])) as unknown as CombinedMockAccounting;
}
