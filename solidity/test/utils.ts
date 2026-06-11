import { HardhatEthersSigner } from '@nomicfoundation/hardhat-ethers/signers';
import { HardhatNetworkHDAccountsConfig } from 'hardhat/types';
import { HttpNetworkConfig } from "hardhat/types/config";
import { artifacts, config, ethers, network, upgrades } from 'hardhat';
import { JsonRpcProvider } from 'ethers';
import { MockAccounting } from "../typechain-types";

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

const SAPPHIRE_LEGACY_CODE_SIZE_LIMIT = 24576;
let sapphireLargeCodeSupported: boolean | undefined;

async function probeLargeCodeDeploy(): Promise<boolean> {
	const deployer = getDeployer();
	const factory = await ethers.getContractFactory('MockAccounting', deployer);
	// Any nonzero address works here: Accounting's constructor rejects address(0).
	const siweAuthAddress = '0x0000000000000000000000000000000000000001';
	for (let attempt = 0; attempt < 3; attempt++) {
		try {
			const probe = await factory.deploy(siweAuthAddress);
			await probe.waitForDeployment();
			return true;
		} catch (error) {
			console.log(`Large-code probe deploy failed (attempt ${attempt + 1}/3)`, error);
			if (attempt < 2) {
				await new Promise((resolve) => setTimeout(resolve, 1000));
			}
		}
	}
	// Distinguish "runtime rejects large code" from "environment is broken": a
	// small control contract must deploy fine. If it cannot, fail loudly instead
	// of skipping suites for the wrong reason.
	const controlFactory = await ethers.getContractFactory('MockSiweAuth', deployer);
	const control = await controlFactory.deploy('large-code-probe');
	await control.waitForDeployment();
	return false;
}

/**
 * Skips the calling suite when the connected Sapphire runtime cannot deploy
 * MockAccounting. Sapphire runtimes below 1.3.0-testnet enforce the EVM's
 * 24,576-byte code size limit, which MockAccounting exceeds. Deploy-dependent
 * suites self-skip on such runtimes (for example an outdated sapphire-localnet
 * image) and fail loudly when deploys break for any other reason.
 * @param ctx Mocha context of the calling before/beforeEach hook
 */
export async function skipUnlessMockAccountingDeployable(ctx: Mocha.Context): Promise<void> {
	const chainId = (await ethers.provider.getNetwork()).chainId;
	if (!(0x5afdn <= chainId && chainId <= 0x5affn)) {
		return;
	}
	const artifact = await artifacts.readArtifact('MockAccounting');
	const deployedSize = (artifact.deployedBytecode.length - 2) / 2;
	if (deployedSize <= SAPPHIRE_LEGACY_CODE_SIZE_LIMIT) {
		return;
	}
	if (sapphireLargeCodeSupported === undefined) {
		sapphireLargeCodeSupported = await probeLargeCodeDeploy();
	}
	if (!sapphireLargeCodeSupported) {
		console.warn(
			`Skipping suite: MockAccounting (${deployedSize} bytes) exceeds this Sapphire runtime's ` +
			`${SAPPHIRE_LEGACY_CODE_SIZE_LIMIT}-byte code size limit (needs a Sapphire runtime >= ` +
			`1.3.0-testnet; for sapphire-localnet, pull the latest image).`
		);
		ctx.skip();
	}
}

/**
 * Advances chain time until `block.timestamp` exceeds `targetTimestamp`.
 * Sapphire nodes do not support evm_increaseTime/evm_mine, so there we wait
 * for real blocks to pass the target instead; callers must keep expiries
 * within a few seconds of the current chain time.
 * @param targetTimestamp Unix timestamp (seconds) to advance past
 */
export async function advanceTimePast(targetTimestamp: number): Promise<void> {
	const chainId = (await ethers.provider.getNetwork()).chainId;
	if (0x5afdn <= chainId && chainId <= 0x5affn) {
		const deadline = Date.now() + 120_000;
		let latest = (await ethers.provider.getBlock('latest'))!;
		while (latest.timestamp <= targetTimestamp) {
			if (Date.now() > deadline) {
				throw new Error(
					`Chain time did not pass ${targetTimestamp} within 120s (latest: ${latest.timestamp})`
				);
			}
			await new Promise((resolve) => setTimeout(resolve, 1000));
			latest = (await ethers.provider.getBlock('latest'))!;
		}
		return;
	}
	const latest = (await ethers.provider.getBlock('latest'))!;
	await ethers.provider.send('evm_increaseTime', [Math.max(targetTimestamp - latest.timestamp + 1, 1)]);
	await ethers.provider.send('evm_mine', []);
}

/**
 * Waits until the ERC1967 implementation behind `proxyAddress` differs from
 * `previousImplementation`. On Sapphire, upgradeProxy neither awaits nor
 * surfaces a reverted upgrade transaction
 * (https://github.com/oasisprotocol/sapphire-paratime/issues/688), so callers
 * must not read through the proxy until the implementation actually switches.
 */
export async function waitForImplementationChange(
	proxyAddress: string,
	previousImplementation: string
): Promise<void> {
	let current = await upgrades.erc1967.getImplementationAddress(proxyAddress);
	for (let i = 0; i < 100 && current === previousImplementation; i++) {
		await new Promise((resolve) => setTimeout(resolve, 100));
		current = await upgrades.erc1967.getImplementationAddress(proxyAddress);
	}
	if (current === previousImplementation) {
		throw new Error(`Proxy ${proxyAddress} implementation did not change within 10s`);
	}
}

/**
 * Helper to deploy the mock accounting contract with an unencrypted transaction and with workarounds for flaky UUPS wrapper errors.
 * @param mockSiweAuthAddress SIWE auth contract to initialize Accounting with
 */
export async function deployMockAccounting(mockSiweAuthAddress: string) {
	const deployer = getDeployer();
	const AccountingFactory = await ethers.getContractFactory('MockAccounting', deployer);
	let accounting: MockAccounting;

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
			//accounting = accounting.connect((await getSigners())[0]); // Use wrapped signer for sending txes.
			deploymentSucceeded = true;
		} catch (error) {
			console.log('Deployment failed, retrying...', error);
			await new Promise(resolve => setTimeout(resolve, 1000));
		}
	}
	return accounting!;
}
