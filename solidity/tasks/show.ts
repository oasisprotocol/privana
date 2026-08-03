import { task } from "hardhat/config";
import { formatRoflAppId } from "./utils/rofl";

const TOKEN_TYPE_NAMES = ["NativeEVM", "ERC20"];

// Wraps a single contract read so a revert (e.g. a function that doesn't exist on
// whatever's actually deployed) only blanks out that one field instead of crashing
// the whole task.
async function tryCall<T>(fn: () => Promise<T>): Promise<T | undefined> {
  try {
    return await fn();
  } catch {
    return undefined;
  }
}

task("show")
  .addPositionalParam("address", "The Accounting proxy contract address")
  .setDescription("Print details about a deployed Accounting contract: implementation, owner, siweAuth, tokens, etc.")
  .setAction(async (args, hre) => {
    await hre.run("compile");

    const accounting = await hre.ethers.getContractAt("Accounting", args.address);

    const [implAddress, version, owner, siweAuthAddress, withdrawalCount, tokenIds, proposedUpgradeImpl, proposedUpgradeImplHash, proposedUpgradeMinBlockNumber] =
      await Promise.all([
        tryCall(() => hre.upgrades.erc1967.getImplementationAddress(args.address)),
        tryCall(() => accounting.VERSION()),
        tryCall(() => accounting.owner()),
        tryCall(() => accounting.siweAuth()),
        tryCall(() => accounting.withdrawalCount()),
        tryCall(() => accounting.getRegisteredTokens()),
        tryCall(() => accounting.proposedUpgradeImplementation()),
        tryCall(() => accounting.proposedUpgradeImplementationHash()),
        tryCall(() => accounting.proposedUpgradeMinBlockNumber()),
      ]);

    const hasProposedUpgrade = proposedUpgradeImpl !== undefined && proposedUpgradeImpl !== hre.ethers.ZeroAddress;

    console.log("=== Accounting Contract Info ===");
    console.log("Proxy address:       ", args.address);
    console.log("Implementation:      ", implAddress);
    console.log("VERSION:             ", version?.toString());
    console.log("Owner:               ", owner);
    console.log("SiweAuth address:    ", siweAuthAddress);
    console.log("Withdrawal count:    ", withdrawalCount?.toString());
    console.log("Proposed upgrade:    ", hasProposedUpgrade ? "yes" : "none");
    if (hasProposedUpgrade) {
      console.log("  New implementation:     ", proposedUpgradeImpl);
      console.log("  New implementation hash:", proposedUpgradeImplHash);
      console.log("  Min block number:       ", proposedUpgradeMinBlockNumber?.toString());
    }

    console.log(`\n=== Registered Tokens (${tokenIds?.length ?? 0}) ===`);
    for (const tokenId of tokenIds ?? []) {
      const tokenInfo = await tryCall(() => accounting.tokens(tokenId));
      const typeIndex = tokenInfo !== undefined ? Number(tokenInfo.tokenType) : undefined;
      const typeName = typeIndex !== undefined ? TOKEN_TYPE_NAMES[typeIndex] ?? `Unknown(${typeIndex})` : undefined;

      console.log(`\n  Token ID: ${tokenId}`);
      console.log(`  Type: ${typeName}`);

      if (typeIndex === 0) {
        const chainId = await tryCall(() => accounting.decodeEVMNativeTokenData(tokenInfo!.data));
        console.log(`  Chain ID: ${chainId}`);
      } else if (typeIndex === 1) {
        const decoded = await tryCall(() => accounting.decodeEVMErc20TokenData(tokenInfo!.data));
        console.log(`  Chain ID: ${decoded?.[0]}`);
        console.log(`  Token address: ${decoded?.[1]}`);
      } else {
        console.log(`  Raw data: ${tokenInfo?.data}`);
      }
    }

    let siweRoflAppId: string | undefined;
    let siweRoflAppIdBech32: string | undefined;
    let siweAuthTokenEncKeyHash: string | undefined;
    if (siweAuthAddress !== undefined) {
      const siweAuth = await hre.ethers.getContractAt("AccountingSiweAuth", siweAuthAddress);

      [siweRoflAppId, siweAuthTokenEncKeyHash] = await Promise.all([
        tryCall(() => siweAuth.roflAppId()),
        tryCall(() => siweAuth.getAuthTokenEncKeyHash()),
      ]);
      siweRoflAppIdBech32 = siweRoflAppId !== undefined ? await tryCall(async () => formatRoflAppId(siweRoflAppId!)) : undefined;

      console.log("\n=== AccountingSiweAuth Info ===");
      console.log("Address:             ", siweAuthAddress);
      console.log("ROFL app ID:         ", siweRoflAppIdBech32 ?? siweRoflAppId);
      console.log("Auth key hash:       ", siweAuthTokenEncKeyHash);
    }

    const [evmSigningAddress, gasTankAddress, roflAppID, roflSignerAddress] = await Promise.all([
      tryCall(() => accounting.evmAddress()),
      tryCall(() => accounting.gasTankAddress()),
      tryCall(() => accounting.roflAppID()),
      tryCall(() => accounting.roflSignerAddress()),
    ]);
    const roflAppIDBech32 = roflAppID !== undefined ? await tryCall(async () => formatRoflAppId(roflAppID)) : undefined;

    console.log("\n=== EVMSignerAndVerifier Info ===");
    console.log("EVM signing address: ", evmSigningAddress);
    console.log("Gas tank address:    ", gasTankAddress);
    console.log("ROFL app ID:         ", roflAppIDBech32 ?? roflAppID);
    console.log("ROFL signer address: ", roflSignerAddress);

    return {
      proxyAddress: args.address,
      implAddress,
      version: version?.toString(),
      owner,
      siweAuthAddress,
      siweRoflAppId,
      siweRoflAppIdBech32,
      siweAuthTokenEncKeyHash,
      evmSigningAddress,
      gasTankAddress,
      roflAppID,
      roflAppIDBech32,
      roflSignerAddress,
      withdrawalCount: withdrawalCount?.toString(),
      tokenIds,
      proposedUpgradeImpl: hasProposedUpgrade ? proposedUpgradeImpl : undefined,
      proposedUpgradeImplHash: hasProposedUpgrade ? proposedUpgradeImplHash : undefined,
      proposedUpgradeMinBlockNumber: hasProposedUpgrade ? proposedUpgradeMinBlockNumber?.toString() : undefined,
    };
  });
