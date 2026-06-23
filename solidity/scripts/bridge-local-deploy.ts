/**
 * Deploy a local two-chain ROSE bridge environment and emit an address manifest.
 *
 * Sapphire side (this hardhat --network, sapphire-localnet): the real
 * `Accounting` UUPS proxy (+ `AccountingSiweAuth`) deployed with the bundled
 * appd's app id, plus owner-only wiring (`setTokenInfo`, `setGasPrice`).
 * Base side (anvil, chainId 84532): real `XRose` + `ROFLBridge` wired to the
 * Sapphire custody EOA (`Accounting.evmAddress()`), with mint/burn limits and a
 * funded custody EOA.
 *
 * The two `onlyROFL` writes (`setRoflBridge`, `setRoflSignerAddress`) are NOT
 * done here — they go through the appd and are driven by the Python e2e
 * (`test/py/test_bridge_local_e2e.py`), which also owns every bridge flow. This
 * script only produces the deployed+owner-wired state and writes the manifest
 * the Python side consumes.
 *
 * Run via the repo-root harness scripts/bridge-local-e2e.sh, or directly from
 * the solidity/ directory:
 *   PRIVATE_KEY= npx hardhat run scripts/bridge-local-deploy.ts --network sapphire-localnet
 */
import { ethers, artifacts, upgrades } from "hardhat";
import { JsonRpcProvider, Wallet, NonceManager } from "ethers";
import { writeFileSync, mkdirSync } from "fs";
import { dirname } from "path";
import { parseRoflAppId } from "../tasks/utils/rofl";

const ROFL_APPD_URL = process.env.ROFL_APPD_URL ?? "http://127.0.0.1:8549";
const BASE_RPC_URL = process.env.BASE_LOCAL_RPC_URL ?? "http://127.0.0.1:8546";
const MANIFEST_PATH =
  process.env.BRIDGE_LOCAL_MANIFEST ?? "deployments/bridge-local.json";

const BASE_CHAIN_ID = 84532n;
const BASE_GAS_PRICE = 1_000_000_000n; // 1 gwei; anvil started with --base-fee 0
const TokenType = { NativeEVM: 0, ERC20: 1, BridgeAsset: 2 } as const;

// anvil default mnemonic account[1] — a well-known dev key, not a credential.
const ANVIL_DEPLOYER_KEY =
  "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d";

async function main(): Promise<void> {
  const network = await ethers.provider.getNetwork();
  if (network.name === "hardhat" || network.name === "unknown") {
    throw new Error(
      "Refusing to deploy on the in-process hardhat network — needs real " +
        "Sapphire precompiles. Run with --network sapphire-localnet.",
    );
  }
  const sapphireChainId = network.chainId;

  // The appd's app id is what makes onlyROFL submissions pass
  // roflEnsureAuthorizedOrigin; read it live rather than hardcoding.
  const appIdResp = await fetch(`${ROFL_APPD_URL}/rofl/v1/app/id`);
  if (!appIdResp.ok) {
    throw new Error(
      `rofl-appd not reachable at ${ROFL_APPD_URL} (HTTP ${appIdResp.status})`,
    );
  }
  const appId = (await appIdResp.text()).trim();
  const roflAppIdHex = parseRoflAppId(appId);
  console.log(`appd app id: ${appId}`);

  // ── Sapphire side: AccountingSiweAuth + Accounting UUPS proxy ──
  const [deployer] = await ethers.getSigners();
  const SiweAuth = await ethers.getContractFactory("AccountingSiweAuth");
  const siweAuth = await SiweAuth.deploy(roflAppIdHex, { gasLimit: 10_000_000 });
  await siweAuth.waitForDeployment();
  const siweAuthAddr = await siweAuth.getAddress();

  const Accounting = await ethers.getContractFactory("Accounting");
  const proxy = await upgrades.deployProxy(
    Accounting,
    [roflAppIdHex, deployer.address],
    {
      kind: "uups",
      initializer: "initialize",
      constructorArgs: [siweAuthAddr],
      unsafeAllow: ["constructor", "state-variable-immutable"],
      txOverrides: { gasLimit: 15_000_000 },
    },
  );
  await proxy.waitForDeployment();
  const accounting = proxy as unknown as {
    getAddress(): Promise<string>;
    evmAddress(): Promise<string>;
    encodeBridgeAssetTokenData(s: string): Promise<string>;
    setTokenInfo(info: { tokenType: number; data: string }): Promise<any>;
    setGasPrice(chainId: bigint, price: bigint): Promise<any>;
    ROSE_TOKEN_ID(): Promise<string>;
  };
  const accountingAddr = await accounting.getAddress();
  const custodyEOA = await accounting.evmAddress();
  const roseTokenId = await accounting.ROSE_TOKEN_ID();
  console.log(`Accounting proxy: ${accountingAddr}`);
  console.log(`custody EOA (evmAddress): ${custodyEOA}`);

  // ── Base side: XRose + ROFLBridge wired to the custody EOA ──
  const base = new JsonRpcProvider(BASE_RPC_URL);
  const baseWallet = new Wallet(ANVIL_DEPLOYER_KEY, base);
  const baseAddr = baseWallet.address;
  const baseDeployer = new NonceManager(baseWallet); // sequential; avoids nonce races
  const xroseArt = await artifacts.readArtifact("XRose");
  const bridgeArt = await artifacts.readArtifact("ROFLBridge");

  const XRoseFactory = new ethers.ContractFactory(
    xroseArt.abi,
    xroseArt.bytecode,
    baseDeployer,
  );
  const xrose = await XRoseFactory.deploy("XRose", "xROSE", baseAddr);
  await xrose.waitForDeployment();
  const xroseAddr = await xrose.getAddress();

  const BridgeFactory = new ethers.ContractFactory(
    bridgeArt.abi,
    bridgeArt.bytecode,
    baseDeployer,
  );
  const bridge = await BridgeFactory.deploy(
    xroseAddr,
    custodyEOA, // roflSigner — must equal Accounting.evmAddress()
    baseAddr, // pauseAdmin
    baseAddr, // owner
  );
  await bridge.waitForDeployment();
  const bridgeAddr = await bridge.getAddress();
  console.log(`Base XRose: ${xroseAddr}`);
  console.log(`Base ROFLBridge: ${bridgeAddr}`);

  // Grant the bridge mint/burn limits; grant the deployer a mint limit the
  // Python e2e uses to seed the bridge's xROSE balance for the inbound burn.
  const LIMIT = ethers.parseEther("1000000");
  const xroseAsDeployer = xrose as unknown as {
    setLimits(a: string, m: bigint, b: bigint): Promise<any>;
  };
  await (await xroseAsDeployer.setLimits(bridgeAddr, LIMIT, LIMIT)).wait();
  await (await xroseAsDeployer.setLimits(baseAddr, LIMIT, 0n)).wait();

  // Fund the custody EOA on Base so it can pay gas for mint/burn broadcasts.
  await base.send("anvil_setBalance", [custodyEOA, "0x3635c9adc5dea00000"]); // 1000 ETH

  // ── Owner-only wiring (plain txs; the onlyROFL writes are done in Python) ──
  const roseData = await accounting.encodeBridgeAssetTokenData("ROSE");
  await (
    await accounting.setTokenInfo({
      tokenType: TokenType.BridgeAsset,
      data: roseData,
    })
  ).wait();
  await (await accounting.setGasPrice(BASE_CHAIN_ID, BASE_GAS_PRICE)).wait();

  const manifest = {
    accountingProxy: accountingAddr,
    siweAuth: siweAuthAddr,
    custodyEOA,
    xrose: xroseAddr,
    roflBridge: bridgeAddr,
    baseDeployer: baseAddr,
    sapphireChainId: Number(sapphireChainId),
    baseChainId: Number(BASE_CHAIN_ID),
    baseGasPrice: BASE_GAS_PRICE.toString(),
    roseTokenId,
    appId,
  };
  mkdirSync(dirname(MANIFEST_PATH), { recursive: true });
  writeFileSync(MANIFEST_PATH, JSON.stringify(manifest, null, 2) + "\n");
  console.log(`manifest written: ${MANIFEST_PATH}`);
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
