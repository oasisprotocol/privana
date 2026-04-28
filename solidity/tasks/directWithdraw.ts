/**
 * Direct withdrawal: bypass ROFL/API entirely.
 *
 * Calls requestWithdrawal + resolveWithdrawal directly on Sapphire,
 * then broadcasts the signed tx on the source chain. Works even when
 * ROFL is down — the contract handles nonce management and signing.
 */
import { task } from "hardhat/config";
import { JsonRpcProvider } from "ethers";

const SOURCE_CHAIN_RPC: Record<number, () => string> = {
  84532: () => process.env.BASE_SEPOLIA_RPC_URL || "https://sepolia.base.org",
  11155111: () => process.env.ETH_SEPOLIA_RPC_URL || "https://eth-sepolia.public.blastapi.io",
};

const CHAIN_EXPLORERS: Record<number, string> = {
  84532: "https://sepolia.basescan.org",
  11155111: "https://sepolia.etherscan.io",
};

function getSourceRpcUrl(chainId: number): string {
  const factory = SOURCE_CHAIN_RPC[chainId];
  if (!factory) throw new Error(`No RPC URL for chain ${chainId}. Pass --sourcerpc.`);
  return factory();
}

task("directWithdraw", "Withdraw directly on-chain without ROFL/API")
  .addParam("contract", "Accounting contract address")
  .addParam("tokenid", "Token ID (bytes32 hex)")
  .addParam("amount", "Amount in token base units")
  .addOptionalParam("sourcerpc", "Source chain RPC URL (auto-detected for known chains)")
  .addOptionalParam("timeout", "Seconds to wait for source chain confirmation", "120")
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const userAddress = signer.address;
    const accounting = await hre.ethers.getContractAt("Accounting", args.contract, signer);
    const amount = BigInt(args.amount);

    console.log("User:", userAddress);
    console.log("Token ID:", args.tokenid);
    console.log("Amount:", args.amount);

    // 1. Read nonce directly from contract (no API needed)
    const nonce = await accounting.withdrawalNonces(userAddress);
    console.log("Withdrawal nonce:", nonce.toString());

    // 2. Get EIP-712 domain
    const domainTuple = await accounting.eip712Domain();
    const domain = {
      name: domainTuple[1],
      version: domainTuple[2],
      chainId: Number(domainTuple[3]),
      verifyingContract: domainTuple[4],
    };

    // 3. Sign EIP-712 withdrawal
    const types = {
      Withdraw: [
        { name: "userAddress", type: "address" },
        { name: "tokenId", type: "bytes32" },
        { name: "amount", type: "uint256" },
        { name: "nonce", type: "uint256" },
      ],
    };
    const message = {
      userAddress,
      tokenId: args.tokenid,
      amount,
      nonce,
    };

    console.log("\nSigning EIP-712 withdrawal...");
    const signature = await signer.signTypedData(domain, types, message);

    // 4. Get withdrawal count before to determine our index
    const countBefore = await accounting.withdrawalCount();

    // 5. Submit requestWithdrawal directly on Sapphire
    console.log("Submitting requestWithdrawal on Sapphire...");
    const requestTx = await accounting.requestWithdrawal(
      userAddress,
      args.tokenid,
      amount,
      nonce,
      signature,
    );
    const requestReceipt = await requestTx.wait();
    console.log("Request tx:", requestReceipt?.hash);

    // The new withdrawal index is countBefore (0-based array push)
    const withdrawalIndex = Number(countBefore);
    console.log("Withdrawal index:", withdrawalIndex);

    // 6. Wait 1 block (resolveWithdrawal requires block.number > request block)
    console.log("Waiting for next block...");
    await new Promise<void>((resolve) => {
      const listener = () => {
        hre.ethers.provider.off("block", listener);
        resolve();
      };
      hre.ethers.provider.on("block", listener);
    });

    // 7. Resolve: get signed source-chain tx via staticCall
    console.log("Resolving withdrawal...");
    const signedTx: string = await accounting.resolveWithdrawal.staticCall(withdrawalIndex);

    // 8. Mark resolved on Sapphire
    const resolveTx = await accounting.resolveWithdrawal(withdrawalIndex);
    await resolveTx.wait();
    console.log("Resolved on Sapphire:", resolveTx.hash);

    // 9. Determine source chain from token info
    const tokenInfo = await accounting.tokens(args.tokenid);
    const tokenType = Number(tokenInfo[0]); // 0 = NativeEVM, 1 = ERC20
    const tokenData = tokenInfo[1] as string;

    let chainId: number;
    if (tokenType === 0) {
      // NativeEVM: data is abi.encodePacked(chainId) = 32 bytes
      chainId = Number(BigInt(tokenData));
    } else {
      // ERC20: data is abi.encodePacked(chainId, tokenAddress) = 52 bytes
      // chainId is first 32 bytes
      chainId = Number(BigInt("0x" + tokenData.slice(2, 66)));
    }

    console.log("\nSource chain ID:", chainId);

    // 10. Broadcast on source chain
    const rpcUrl = args.sourcerpc || getSourceRpcUrl(chainId);
    const sourceProvider = new JsonRpcProvider(rpcUrl);

    console.log("Broadcasting on source chain...");
    try {
      const broadcastResult = await sourceProvider.broadcastTransaction(signedTx);
      console.log("Source tx:", broadcastResult.hash);

      console.log("Waiting for confirmation...");
      const sourceReceipt = await broadcastResult.wait(1, parseInt(args.timeout) * 1000);

      const explorer = CHAIN_EXPLORERS[chainId];
      console.log("\n=== Direct Withdrawal Complete ===");
      console.log("Source chain tx:", sourceReceipt?.hash);
      console.log("Block:", sourceReceipt?.blockNumber);
      if (explorer) {
        console.log(`Explorer: ${explorer}/tx/${sourceReceipt?.hash}`);
      }
    } catch (err) {
      console.error("\nFailed to broadcast on source chain:", err);
      console.error("\nThe withdrawal IS resolved on Sapphire (balance deducted).");
      console.error("Raw signed tx for manual broadcast:");
      console.error(signedTx);
    }
  });
