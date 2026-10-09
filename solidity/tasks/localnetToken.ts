import '@nomicfoundation/hardhat-ethers';
import '@oasisprotocol/sapphire-hardhat';
import { JsonRpcProvider, Signer } from "ethers";
import { task } from "hardhat/config";
import { HardhatRuntimeEnvironment } from "hardhat/types";
import { HttpNetworkConfig } from "hardhat/types/config";

// sapphire-localnet — see CHAIN_CONFIGS[23293] in src/config/chain_config.py.
export const SAPPHIRE_LOCALNET_CHAIN_ID = 23293n;

// Well-known public burner key (Hardhat/Anvil test account #0): safe to commit, must
// never hold value.
//
// A CREATE address is keccak256(rlp([deployer, nonce]))[12:] — a pure function of the
// deployer and its nonce, independent of bytecode and constructor args. Deploying from
// this key at nonce 0 therefore pins LOCALNET_TOKEN_ADDRESS across every localnet reset,
// which is what lets .env.localnet hardcode the token in ACCOUNTING_TOKEN_INFO.
export const LOCALNET_TOKEN_DEPLOYER_KEY =
  "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
export const LOCALNET_TOKEN_DEPLOYER_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266";
export const LOCALNET_TOKEN_ADDRESS = "0x5FbDB2315678afecb367f032d93F642f64180aa3";

// 1M LHONOR, 18 decimals. Minted to the network's first configured account —
// the one the deposit/withdraw Hardhat tasks sign with.
const INITIAL_SUPPLY = 1_000_000n * 10n ** 18n;
// Sapphire debits gasLimit * gasPrice upfront and pads gas estimates hard, so a
// single deployment can reserve ~0.2 ROSE. Localnet accounts are seeded with
// thousands of TEST ROSE; 10 covers the deploy plus faucet mints.
const DEPLOYER_FUNDING = 10n * 10n ** 18n;

/**
 * First account configured for the network: SECRET_KEY when set, otherwise index
 * `initialIndex` of the localnet test mnemonic in hardhat.config.ts. Funds the fixed
 * token deployer and receives the initial supply.
 */
function getFunder(hre: HardhatRuntimeEnvironment, provider: JsonRpcProvider): Signer {
  const secretKey = process.env.SECRET_KEY;
  if (secretKey) {
    return new hre.ethers.Wallet(secretKey, provider);
  }

  const accounts = (hre.network.config as HttpNetworkConfig).accounts;
  if (Array.isArray(accounts)) {
    return new hre.ethers.Wallet(accounts[0] as string, provider);
  }

  const hd = accounts as { mnemonic: string; path: string; initialIndex: number; passphrase: string };
  if (!hd?.mnemonic) {
    throw new Error(
      "No account configured for this network: set SECRET_KEY or configure accounts in hardhat.config.ts"
    );
  }
  return hre.ethers.HDNodeWallet.fromPhrase(
    hd.mnemonic,
    hd.passphrase,
    `${hd.path}/${hd.initialIndex}`
  ).connect(provider);
}

task("deploy-localnet-token")
  .setDescription(
    "Deploy the localnet ERC20 (LHONOR) from a fixed key at nonce 0, so its address is stable across localnet resets"
  )
  .setAction(async (_args, hre) => {
    await hre.run("compile");

    const network = await hre.ethers.provider.getNetwork();
    if (network.chainId !== SAPPHIRE_LOCALNET_CHAIN_ID) {
      throw new Error(
        `deploy-localnet-token is localnet-only: expected chain ${SAPPHIRE_LOCALNET_CHAIN_ID}, ` +
        `connected to ${network.chainId}`
      );
    }

    // Unwrapped provider: the token deployment, its transfers, and the Transfer
    // logs the deposit verifier reads must all be plain-text on Sapphire.
    const provider = new JsonRpcProvider((hre.network.config as HttpNetworkConfig).url);

    const existingCode = await provider.getCode(LOCALNET_TOKEN_ADDRESS);
    if (existingCode !== "0x") {
      console.log(`Localnet ERC20 already deployed at ${LOCALNET_TOKEN_ADDRESS}`);
      return LOCALNET_TOKEN_ADDRESS;
    }

    const deployerNonce = await provider.getTransactionCount(LOCALNET_TOKEN_DEPLOYER_ADDRESS);
    if (deployerNonce !== 0) {
      throw new Error(
        `Fixed token deployer ${LOCALNET_TOKEN_DEPLOYER_ADDRESS} is at nonce ${deployerNonce}, ` +
        `not 0, and nothing is deployed at ${LOCALNET_TOKEN_ADDRESS}. The CREATE address would ` +
        `not match the hardcoded ACCOUNTING_TOKEN_INFO entry in .env.localnet — restart ` +
        `sapphire-localnet to reset chain state instead of deploying to a drifted address.`
      );
    }

    const funder = getFunder(hre, provider);
    const funderAddress = await funder.getAddress();

    const deployerBalance = await provider.getBalance(LOCALNET_TOKEN_DEPLOYER_ADDRESS);
    if (deployerBalance < DEPLOYER_FUNDING) {
      console.log(
        `Funding token deployer ${LOCALNET_TOKEN_DEPLOYER_ADDRESS} with ` +
        `${hre.ethers.formatEther(DEPLOYER_FUNDING - deployerBalance)} TEST ROSE from ${funderAddress}`
      );
      const fundingTx = await funder.sendTransaction({
        to: LOCALNET_TOKEN_DEPLOYER_ADDRESS,
        value: DEPLOYER_FUNDING - deployerBalance,
      });
      await fundingTx.wait();
    }

    const tokenDeployer = new hre.ethers.Wallet(LOCALNET_TOKEN_DEPLOYER_KEY, provider);
    const LocalnetERC20 = await hre.ethers.getContractFactory("LocalnetERC20", tokenDeployer);
    const token = await LocalnetERC20.deploy(funderAddress, INITIAL_SUPPLY);
    await token.waitForDeployment();

    const tokenAddress = await token.getAddress();
    if (tokenAddress.toLowerCase() !== LOCALNET_TOKEN_ADDRESS.toLowerCase()) {
      throw new Error(
        `Localnet ERC20 deployed to ${tokenAddress}, expected the deterministic ` +
        `${LOCALNET_TOKEN_ADDRESS}. Update LOCALNET_TOKEN_ADDRESS here and the matching ` +
        `ACCOUNTING_TOKEN_INFO entry in .env.localnet together.`
      );
    }

    console.log(`Localnet ERC20 (LHONOR) address: ${tokenAddress}`);
    console.log(`Initial supply holder: ${funderAddress} (${hre.ethers.formatEther(INITIAL_SUPPLY)} LHONOR)`);
    console.log("Registered for chain 23293 via ACCOUNTING_TOKEN_INFO in .env.localnet");

    return tokenAddress;
  });
