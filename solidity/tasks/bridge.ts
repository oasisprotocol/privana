// Hardhat tasks for owner-only bridge admin on the Accounting proxy.
// Currently exposes `bridge:addRoseToken` only — route registration moved
// to the ROFL TEE reconciler (`bridge_route_reconciler.py`), where the
// in-flight drain guard can actually read the sweep + custody state dirs
// living inside the TEE.

import { ethers } from "ethers";
import { task } from "hardhat/config";
import type { HardhatRuntimeEnvironment } from "hardhat/types";

// Pinned literal; mirrors Accounting.sol:26 and BridgeModule.sol:29.
export const ROSE_TOKEN_ID =
  "0xca91975d6c6810eb4077546d4fbdb49fa231f351cddfc915862f7c0dad81a7aa";

// TokenType.BridgeAsset enum index — see Types.sol:15.
export const TOKEN_TYPE_BRIDGE_ASSET = 2;

export const ACCOUNTING_TOKEN_ABI = [
  "function setTokenInfo((uint8 tokenType, bytes data))",
  "function tokens(bytes32) view returns (uint8 tokenType, bytes data)",
  "function ROSE_TOKEN_ID() view returns (bytes32)",
  "function encodeBridgeAssetTokenData(string) pure returns (bytes)",
  "function owner() view returns (address)",
];

export async function addRoseTokenIfNeeded(
  accounting: ethers.Contract,
): Promise<{ skipped: boolean; txHash: string | null }> {
  const info = await accounting.tokens(ROSE_TOKEN_ID);
  if (Number(info.tokenType) === TOKEN_TYPE_BRIDGE_ASSET) {
    return { skipped: true, txHash: null };
  }
  const data = await accounting.encodeBridgeAssetTokenData("ROSE");
  const tx = await accounting.setTokenInfo({
    tokenType: TOKEN_TYPE_BRIDGE_ASSET,
    data,
  });
  const receipt = await tx.wait();
  if (!receipt || receipt.status !== 1) {
    throw new Error(`addRoseToken setTokenInfo tx ${tx.hash} did not succeed.`);
  }
  return { skipped: false, txHash: tx.hash };
}

export async function assertRoseTokenRegistered(
  accounting: ethers.Contract,
): Promise<void> {
  const onchain: string = await accounting.ROSE_TOKEN_ID();
  if (onchain.toLowerCase() !== ROSE_TOKEN_ID.toLowerCase()) {
    throw new Error(
      `ROSE_TOKEN_ID mismatch: contract returned ${onchain}, expected pinned ${ROSE_TOKEN_ID}.`,
    );
  }
  const info = await accounting.tokens(onchain);
  if (Number(info.tokenType) !== TOKEN_TYPE_BRIDGE_ASSET) {
    throw new Error(
      `ROSE token not registered as BridgeAsset (got tokenType=${info.tokenType}).`,
    );
  }
}

function resolveAccountingAddress(args: { accounting?: string }): string {
  const fromArg = args.accounting?.trim();
  const fromEnv = process.env.ACCOUNTING_ADDRESS_SAPPHIRE?.trim();
  const addr = fromArg || fromEnv;
  if (!addr) {
    throw new Error(
      "Accounting address not provided: pass --accounting or set ACCOUNTING_ADDRESS_SAPPHIRE.",
    );
  }
  return ethers.getAddress(addr);
}

task(
  "bridge:addRoseToken",
  "Register ROSE as a BridgeAsset on Accounting (sapphire-testnet)",
)
  .addOptionalParam(
    "accounting",
    "Accounting proxy address (default: $ACCOUNTING_ADDRESS_SAPPHIRE)",
  )
  .setAction(async (args, hre: HardhatRuntimeEnvironment) => {
    const accountingAddr = resolveAccountingAddress(args);
    const [signer] = await hre.ethers.getSigners();
    const accounting = new hre.ethers.Contract(
      accountingAddr,
      ACCOUNTING_TOKEN_ABI,
      signer,
    );

    const result = await addRoseTokenIfNeeded(accounting);
    console.log(
      result.skipped
        ? `ROSE already registered as BridgeAsset (idempotent skip).`
        : `ROSE registration broadcast via tx ${result.txHash}.`,
    );

    await assertRoseTokenRegistered(accounting);
    console.log(`ROSE registered at tokenId ${ROSE_TOKEN_ID}.`);
  });

task(
  "bridge:redeployModule",
  "Deploy fresh BridgeLib + BridgeModule and wire on Accounting (owner-only)",
)
  .addOptionalParam(
    "accounting",
    "Accounting proxy address (default: $ACCOUNTING_ADDRESS_SAPPHIRE)",
  )
  .setAction(async (args, hre: HardhatRuntimeEnvironment) => {
    const accountingAddr = resolveAccountingAddress(args);
    const [signer] = await hre.ethers.getSigners();

    const BridgeLib = await hre.ethers.getContractFactory("BridgeLib", signer);
    const bridgeLib = await BridgeLib.deploy({ gasLimit: 5000000 });
    await bridgeLib.waitForDeployment();
    const bridgeLibAddress = await bridgeLib.getAddress();
    console.log(`BridgeLib deployed: ${bridgeLibAddress}`);

    const BridgeModule = await hre.ethers.getContractFactory("BridgeModule", {
      libraries: { BridgeLib: bridgeLibAddress },
      signer,
    });
    const bridgeModule = await BridgeModule.deploy({ gasLimit: 10000000 });
    await bridgeModule.waitForDeployment();
    const bridgeModuleAddress = await bridgeModule.getAddress();
    console.log(`BridgeModule deployed: ${bridgeModuleAddress}`);

    const accounting = await hre.ethers.getContractAt(
      "Accounting",
      accountingAddr,
      signer,
    );
    const setTx = await accounting.setBridgeModule(bridgeModuleAddress);
    const receipt = await setTx.wait();
    if (!receipt || receipt.status !== 1) {
      throw new Error(`setBridgeModule tx ${setTx.hash} did not succeed.`);
    }
    console.log(`setBridgeModule mined: ${setTx.hash}`);
    console.log(`Accounting.bridgeModule() now: ${await accounting.bridgeModule()}`);
  });
