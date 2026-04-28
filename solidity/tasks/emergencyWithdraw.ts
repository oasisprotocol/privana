import { task } from "hardhat/config";
import { Contract, JsonRpcProvider, Wallet } from "ethers";

// Mirror of the Solidity TokenType enum in contracts/Types.sol — typechain exposes
// enum values as uint8 at the TS boundary, so we use ordinals.
const TokenType = { NativeEVM: 0, ERC20: 1 } as const;

/**
 * Known source-chain RPC URLs. Env vars take precedence.
 * Extend this map when adding new source chains.
 */
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

// ─── Request ───────────────────────────────────────────────────────────

task("emergencyRequest", "Request emergency withdrawal (executable after 1 block)")
  .addParam("contract", "Accounting contract address")
  .addParam("tokenid", "Token ID (bytes32 hex)")
  .addParam("toaddress", "Destination address for recovered funds")
  .addOptionalParam("keyversion", "Key derivation version", "0")
  .setDescription("chainId/chainType are derived from tokenId on-chain — no longer params")
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const accounting = await hre.ethers.getContractAt("Accounting", args.contract, signer);

    console.log("Beneficiary:", signer.address);
    console.log("Token ID:", args.tokenid);
    console.log("To:", args.toaddress);

    const tx = await accounting.requestEmergencyWithdraw(
      args.tokenid,
      args.toaddress,
      parseInt(args.keyversion),
    );
    const receipt = await tx.wait();

    // requestId is deterministic from (beneficiary, tokenId, version)
    const requestId: string = await accounting.emergencyWithdrawKey(
      signer.address,
      args.tokenid,
      parseInt(args.keyversion),
    );

    console.log("\n=== Emergency Withdrawal Requested ===");
    console.log("Request ID:", requestId);
    console.log("Executable after: next block");
    console.log("Tx:", receipt?.hash);
  });

// ─── Execute ───────────────────────────────────────────────────────────

task("emergencyExecute", "Execute emergency withdrawal after 1-block delay")
  .addParam("contract", "Accounting contract address")
  .addParam("beneficiary", "Beneficiary address (msg.sender of emergencyRequest)")
  .addParam("tokenid", "Token ID (bytes32 hex)")
  .addParam("depositaddress", "Deposit address on source chain")
  .addOptionalParam("keyversion", "Key derivation version", "0")
  .addOptionalParam("sourcerpc", "Source chain RPC URL (auto-detected for known chains)")
  .addOptionalParam("amount", "Amount in wei (default: max balance minus gas)")
  .addOptionalParam("gasprice", "Gas price in wei (default: auto from source chain)")
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const accounting = await hre.ethers.getContractAt("Accounting", args.contract, signer);
    const beneficiary = args.beneficiary as string;
    const tokenId = args.tokenid as string;
    const version = parseInt(args.keyversion);

    // 1. Read request from contract (key = keccak(beneficiary, tokenId, version))
    const requestId: string = await accounting.emergencyWithdrawKey(beneficiary, tokenId, version);
    const req = await accounting.emergencyWithdrawRequests(requestId);
    const toAddress = req[0] as string;
    const requestBlock = Number(req[1]);

    if (requestBlock === 0) throw new Error("Request not found");

    // 2. Determine token type + chainId (both derived from the registered token metadata,
    //    so the signed tx is always on the chain the tokenId pins).
    //    tokenData layout: abi.encodePacked(chainId[, tokenAddress]) — chainId in first 32 bytes,
    //    address (ERC20 only) in the last 20 bytes. Guard against malformed registrations that
    //    slipped past the on-chain length check so we don't silently broadcast to a random chain.
    const tokenInfo = await accounting.tokens(tokenId);
    const tokenType = Number(tokenInfo[0]);
    const tokenData = tokenInfo[1] as string;
    const isERC20 = tokenType === TokenType.ERC20;
    const expectedBytes = isERC20 ? 52 : 32;
    const expectedHex = 2 + expectedBytes * 2;
    if (tokenData.length < expectedHex) {
      throw new Error(
        `Malformed tokenData for ${tokenId}: expected ${expectedBytes} bytes, got ${(tokenData.length - 2) / 2}`,
      );
    }
    const chainId = Number(BigInt("0x" + tokenData.slice(2, 66)));

    let tokenAddress: string | undefined;
    if (isERC20) {
      tokenAddress = "0x" + tokenData.slice(66);
    }

    console.log("=== Request " + requestId + " ===");
    console.log("Beneficiary:", beneficiary);
    console.log("Chain ID:", chainId);
    console.log("Token:", isERC20 ? `ERC20 (${tokenAddress})` : "Native");
    console.log("To:", toAddress);
    console.log("Request block:", requestBlock);

    // 3. Query source chain
    const rpcUrl = args.sourcerpc || getSourceRpcUrl(chainId);
    const sourceProvider = new JsonRpcProvider(rpcUrl);
    const depositAddress = args.depositaddress;

    let [nonce, ethBalance, feeData] = await Promise.all([
      sourceProvider.getTransactionCount(depositAddress),
      sourceProvider.getBalance(depositAddress),
      sourceProvider.getFeeData(),
    ]);

    const gasPrice = args.gasprice
      ? BigInt(args.gasprice)
      : feeData.gasPrice ?? 1_000_000_000n;

    let withdrawAmount: bigint;
    const GAS_LIMIT_NATIVE = 21_000n;
    const GAS_LIMIT_ERC20 = 65_000n;
    const DUST_BUFFER = 500_000n; // covers L1 data fee on L2 chains (Base, OP, etc.)

    if (isERC20) {
      // Query ERC20 balance on source chain
      const erc20 = new Contract(
        tokenAddress!,
        ["function balanceOf(address) view returns (uint256)"],
        sourceProvider,
      );
      const tokenBalance: bigint = await erc20.balanceOf(depositAddress);
      const gasCost = GAS_LIMIT_ERC20 * gasPrice;

      console.log("\n=== Source Chain State ===");
      console.log("Deposit address:", depositAddress);
      console.log("Token balance:", tokenBalance.toString());
      console.log("ETH balance:", hre.ethers.formatEther(ethBalance), "ETH");
      console.log("Nonce:", nonce);
      console.log("Gas price:", gasPrice.toString(), "wei");
      console.log("Gas cost:", hre.ethers.formatEther(gasCost), "ETH");

      if (ethBalance < gasCost) {
        // Auto-fund deposit address with ETH for gas
        const fundAmount = gasCost * 3n; // 3x buffer for safety
        console.log(
          `\nDeposit address needs ETH for gas. Sending ${hre.ethers.formatEther(fundAmount)} ETH...`,
        );

        // Get signer's private key to create source chain wallet
        const networkConfig = hre.config.networks[hre.network.name] as { accounts?: string[] };
        const accounts = networkConfig.accounts;
        if (!accounts || !Array.isArray(accounts) || accounts.length === 0) {
          throw new Error("No private key found in hardhat config for gas funding");
        }
        const sourceWallet = new Wallet(accounts[0] as string, sourceProvider);

        const fundTx = await sourceWallet.sendTransaction({
          to: depositAddress,
          value: fundAmount,
        });
        console.log("Gas funding tx:", fundTx.hash);
        await fundTx.wait();
        console.log("Gas funding confirmed");

        // Re-query nonce (funding tx may have changed it if same address)
        nonce = await sourceProvider.getTransactionCount(depositAddress);
      }

      withdrawAmount = args.amount ? BigInt(args.amount) : tokenBalance;

      if (withdrawAmount <= 0n) {
        throw new Error(`No token balance to withdraw at ${depositAddress}`);
      }

      console.log("Withdraw amount:", withdrawAmount.toString(), "tokens");
    } else {
      // Native: deduct gas from withdrawal amount
      const gasCost = GAS_LIMIT_NATIVE * gasPrice;
      withdrawAmount = args.amount
        ? BigInt(args.amount)
        : ethBalance - gasCost - DUST_BUFFER;

      console.log("\n=== Source Chain State ===");
      console.log("Deposit address:", depositAddress);
      console.log("Balance:", hre.ethers.formatEther(ethBalance), "ETH");
      console.log("Nonce:", nonce);
      console.log("Gas price:", gasPrice.toString(), "wei");
      console.log("Withdraw amount:", hre.ethers.formatEther(withdrawAmount), "ETH");

      if (withdrawAmount <= 0n) {
        throw new Error(
          `Nothing to withdraw. Balance=${hre.ethers.formatEther(ethBalance)}, ` +
            `gasCost=${hre.ethers.formatEther(gasCost)}`,
        );
      }
    }

    // 4. Get signed source-chain tx via staticCall (state-changing fn returns bytes)
    console.log("\nGetting signed tx from contract...");
    const signedTx: string = await accounting.executeEmergencyWithdraw.staticCall(
      beneficiary,
      tokenId,
      version,
      nonce,
      withdrawAmount,
      gasPrice,
    );

    // 5. Emit execution event on Sapphire (safe to re-run: nonce guards double-spend)
    console.log("Emitting execute event on Sapphire...");
    const sapphireTx = await accounting.executeEmergencyWithdraw(
      beneficiary,
      tokenId,
      version,
      nonce,
      withdrawAmount,
      gasPrice,
    );
    await sapphireTx.wait();
    console.log("Sapphire tx:", sapphireTx.hash);

    // 6. Broadcast on source chain
    console.log("\nBroadcasting on source chain...");
    try {
      const broadcastResult = await sourceProvider.broadcastTransaction(signedTx);
      console.log("Source tx:", broadcastResult.hash);

      console.log("Waiting for confirmation...");
      const sourceReceipt = await broadcastResult.wait();

      const explorer = CHAIN_EXPLORERS[chainId];
      console.log("\n=== Emergency Withdrawal Complete ===");
      console.log("Source chain tx:", sourceReceipt?.hash);
      console.log("Block:", sourceReceipt?.blockNumber);
      if (isERC20) {
        console.log("Sent:", withdrawAmount.toString(), "tokens to", toAddress);
      } else {
        console.log("Sent:", hre.ethers.formatEther(withdrawAmount), "ETH to", toAddress);
      }
      if (explorer) {
        console.log(`Explorer: ${explorer}/tx/${sourceReceipt?.hash}`);
      }
    } catch (err) {
      console.error("\nFailed to broadcast on source chain:", err);
      console.error("\nRaw signed tx for manual broadcast:");
      console.error(signedTx);
    }
  });

// ─── Status ────────────────────────────────────────────────────────────
// Note: there is no separate "cancel" task. To cancel a pending request,
// call emergencyRequest again with any new params — the deterministic key
// means the slot overwrites in place.

task("emergencyStatus", "Check emergency withdrawal request status")
  .addParam("contract", "Accounting contract address")
  .addParam("beneficiary", "Beneficiary address")
  .addParam("tokenid", "Token ID (bytes32 hex)")
  .addOptionalParam("keyversion", "Key derivation version", "0")
  .setAction(async (args, hre) => {
    const accounting = await hre.ethers.getContractAt("Accounting", args.contract);
    const beneficiary = args.beneficiary as string;
    const tokenId = args.tokenid as string;
    const version = parseInt(args.keyversion);

    const requestId: string = await accounting.emergencyWithdrawKey(beneficiary, tokenId, version);
    const req = await accounting.emergencyWithdrawRequests(requestId);
    const toAddress = req[0] as string;
    const requestBlock = Number(req[1]);

    if (requestBlock === 0) {
      console.log("No pending request for", beneficiary, "tokenId", tokenId, "version", version);
      return;
    }

    // chainId is pinned by tokenId — derive from tokens[tokenId].data (first 32 bytes).
    // Guard against malformed/unregistered token data so we don't print a nonsense chainId.
    const tokenInfo = await accounting.tokens(tokenId);
    const tokenData = tokenInfo[1] as string;
    if (tokenData.length < 66) {
      console.log("Token not registered or has malformed data:", tokenData);
      return;
    }
    const chainId = Number(BigInt("0x" + tokenData.slice(2, 66)));

    const currentBlock = await hre.ethers.provider.getBlockNumber();
    const status = currentBlock > requestBlock ? "ready to execute" : "pending (wait 1 block)";

    console.log("=== Emergency Withdrawal " + requestId + " ===");
    console.log("Status:", status);
    console.log("Beneficiary:", beneficiary);
    console.log("Chain ID:", chainId);
    console.log("Token ID:", tokenId);
    console.log("To:", toAddress);
    console.log("Version:", version);
    console.log("Request block:", requestBlock);
    console.log("Current block:", currentBlock);
  });
