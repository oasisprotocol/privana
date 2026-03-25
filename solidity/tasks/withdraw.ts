import { task } from "hardhat/config";
import * as cbor from "cbor";
import { Interface } from "ethers";
import { fetchJson, isJsonObject, normalizeApiBaseUrl } from "./utils/siwe";

// Build error selectors from the ABI (lazy loaded)
let ERROR_SELECTORS: Record<string, string> | null = null;

function getErrorSelectors(): Record<string, string> {
  if (ERROR_SELECTORS === null) {
    // Dynamic require to avoid loading before artifacts exist
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const AccountingArtifact = require("../artifacts/contracts/Accounting.sol/Accounting.json");
    const iface = new Interface(AccountingArtifact.abi);
    ERROR_SELECTORS = {};
    iface.forEachError((error) => {
      // selector is 0x prefixed, we want just the hex part
      ERROR_SELECTORS![error.selector.slice(2)] = error.name;
    });
  }
  return ERROR_SELECTORS;
}

// Chain ID to block explorer URL mapping
const CHAIN_EXPLORERS: Record<number, string> = {
  84532: "https://sepolia.basescan.org",
};

// Extract chain ID from token ID (first 32 bytes after token type)
function getChainIdFromTokenId(tokenId: string): number | null {
  // Token ID format: keccak256(abi.encode(tokenType, abi.encodePacked(chainId, tokenAddress)))
  // We can't reverse the hash, so we check against known chain IDs
  // For now, default to Base Sepolia
  return 84532;
}

type WithdrawalMatch = {
  index: number;
  resolved: boolean;
};

type WithdrawalQueueEntry = [string, bigint, bigint, string, boolean, string];

type WithdrawalReader = {
  withdrawalCount: () => Promise<bigint>;
  withdrawals: (index: number) => Promise<WithdrawalQueueEntry>;
};

async function findLatestMatchingWithdrawal(
  accounting: WithdrawalReader,
  userAddress: string,
  tokenId: string,
  amount: bigint,
  maxScan = 25,
): Promise<WithdrawalMatch | null> {
  const withdrawalCount = Number(await accounting.withdrawalCount());
  if (withdrawalCount === 0) {
    return null;
  }

  const minIndex = Math.max(0, withdrawalCount - maxScan);
  for (let i = withdrawalCount - 1; i >= minIndex; i--) {
    const withdrawal = await accounting.withdrawals(i);
    const sameUser = String(withdrawal[0]).toLowerCase() === userAddress.toLowerCase();
    const sameToken = String(withdrawal[3]).toLowerCase() === tokenId.toLowerCase();
    const sameAmount = BigInt(withdrawal[1].toString()) === amount;

    if (sameUser && sameToken && sameAmount) {
      return {
        index: i,
        resolved: Boolean(withdrawal[4]),
      };
    }
  }

  return null;
}

function decodeSubmissionResponse(hexString: string): { ok: boolean; error?: string } {
  try {
    const buffer = Buffer.from(hexString, "hex");
    const decoded = cbor.decodeFirstSync(buffer);

    if (decoded.ok !== undefined) {
      return { ok: true };
    }

    if (decoded.fail) {
      const { message } = decoded.fail;
      if (typeof message === "string" && message.startsWith("reverted: ")) {
        const base64Error = message.substring("reverted: ".length);
        const errorBytes = Buffer.from(base64Error, "base64").toString("hex");
        const errorName = getErrorSelectors()[errorBytes] || `Unknown(0x${errorBytes})`;
        return { ok: false, error: `Contract reverted: ${errorName}` };
      }
      return { ok: false, error: message || "Unknown error" };
    }

    // Neither ok nor fail - unexpected format
    return { ok: false, error: `Unexpected response format: ${JSON.stringify(decoded)}` };
  } catch (e) {
    return { ok: false, error: `Failed to decode CBOR response: ${e}` };
  }
}

task("withdraw")
  .addParam("contract", "The address of the Accounting contract")
  .addParam("tokenid", "Token ID (32-byte hex)")
  .addParam("amount", "Amount in token base units (e.g., 1000000 for 1 USDC)")
  .addOptionalParam(
    "apiurl",
    "API base URL",
    "https://flexvaults-staging.rofl.build",
  )
  .addOptionalParam("scanlimit", "How many latest withdrawals to scan when fallback-confirming on-chain", "250")
  .addOptionalParam("timeout", "Timeout in seconds to wait for resolution", "120")
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const userAddress = signer.address;
    const accounting = await hre.ethers.getContractAt("Accounting", args.contract, signer);

    console.log("User address:", userAddress);
    console.log("Token ID:", args.tokenid);
    console.log("Amount (base units):", args.amount);

    // Get EIP-712 domain from contract
    const domainTuple = await accounting.eip712Domain();
    const domain = {
      name: domainTuple[1],
      version: domainTuple[2],
      chainId: Number(domainTuple[3]),
      verifyingContract: domainTuple[4],
    };

    console.log("\nEIP-712 Domain:", domain);

    // Get the current withdrawal nonce from the API
    const apiBaseUrl = normalizeApiBaseUrl(args.apiurl);
    const nonceUrl = `${apiBaseUrl}/v1/accounting/withdraw/nonce/${userAddress}`;
    const nonceData = await fetchJson(nonceUrl);
    const nonce = BigInt((nonceData as { nonce: number }).nonce);
    console.log("Withdrawal nonce:", nonce.toString());

    // Define Withdraw type
    const types = {
      Withdraw: [
        { name: "userAddress", type: "address" },
        { name: "tokenId", type: "bytes32" },
        { name: "amount", type: "uint256" },
        { name: "nonce", type: "uint256" },
      ],
    };

    // Create message
    const message = {
      userAddress: userAddress,
      tokenId: args.tokenid,
      amount: BigInt(args.amount),
      nonce: nonce,
    };

    console.log("\nSigning withdrawal request...");

    // Sign the typed data
    const signature = await signer.signTypedData(domain, types, message);
    console.log("Signature:", signature);

    // Submit to API
    const apiUrl = `${apiBaseUrl}/v1/accounting/withdraw`;
    const payload = {
      user_address: userAddress,
      token_id: args.tokenid,
      amount: args.amount, // Keep as string to preserve precision for large values
      nonce: nonce.toString(),
      signature: signature,
    };

    console.log("\nSubmitting withdrawal request to API...");

    let result: Record<string, unknown>;
    try {
      const resp = await fetchJson(apiUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!isJsonObject(resp) || typeof resp.status !== "string") {
        throw new Error("Unexpected response from API");
      }
      result = resp;
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error("\nAPI Error:", msg);
      throw err;
    }

    console.log("Withdrawal request submitted successfully, status:", result.status);

    // Get the withdrawal index by checking pending withdrawals
    console.log("\nWaiting for withdrawal to be created on Sapphire...");
    await new Promise((r) => setTimeout(r, 30000)); // Wait for tx to be mined

    // Find our pending withdrawal
    const pendingUrl = `${apiBaseUrl}/v1/accounting/withdraw/pending/${userAddress}`;
    const pendingData = await fetchJson(pendingUrl);
    const pendingWithdrawals =
      isJsonObject(pendingData) && Array.isArray(pendingData.pending_withdrawals)
        ? (pendingData.pending_withdrawals as Record<string, unknown>[])
        : [];
    let withdrawalIndex: number | null = null;

    if (pendingWithdrawals.length === 0) {
      console.log("No pending withdrawals returned by API. Confirming on-chain state...");
      const nonceAfter = await accounting.withdrawalNonces(userAddress);

      if (nonceAfter <= nonce) {
        console.log("Withdrawal nonce did not advance yet; request is not confirmed on-chain.");
        return { ...result, confirmedOnchain: false };
      }

      const match = await findLatestMatchingWithdrawal(
        accounting,
        userAddress,
        args.tokenid,
        BigInt(args.amount),
        parseInt(args.scanlimit),
      );

      if (!match) {
        console.log(
          `Withdrawal nonce advanced, but matching withdrawal was not found in the last ${args.scanlimit} on-chain entries.`,
        );
        console.log(`Retry with a higher --scanlimit if the withdrawal queue is very large.`);
        return { ...result, confirmedOnchain: true };
      }

      withdrawalIndex = match.index;
      console.log(
        `Confirmed on-chain withdrawal #${withdrawalIndex} via nonce advance + withdrawal queue lookup.`,
      );

      if (match.resolved) {
        console.log(`Withdrawal #${withdrawalIndex} is already resolved on Sapphire.`);
        const chainId = getChainIdFromTokenId(args.tokenid);
        const explorerUrl = chainId ? CHAIN_EXPLORERS[chainId] : null;
        if (explorerUrl) {
          const depositAddress = await accounting.evmAddress();
          console.log(`\nView transactions: ${explorerUrl}/address/${depositAddress}#tokentxns`);
        }
        return { ...result, withdrawalIndex, resolved: true };
      }

      console.log(`Withdrawal #${withdrawalIndex} is pending. Watching for resolution...`);
    } else {
      // Get the most recent pending withdrawal for this user
      const withdrawal = pendingWithdrawals[pendingWithdrawals.length - 1];
      withdrawalIndex = Number(withdrawal.index);
      console.log(`Found withdrawal #${withdrawalIndex}`);
    }

    if (withdrawalIndex === null) {
      throw new Error("Failed to determine withdrawal index for status tracking");
    }

    // Poll until resolved
    const timeoutSeconds = parseInt(args.timeout);
    const startTime = Date.now();
    console.log(`\nWaiting for withdrawal to be resolved and broadcast (timeout: ${timeoutSeconds}s)...`);

    while (Date.now() - startTime < timeoutSeconds * 1000) {
      const withdrawalInfo = await accounting.withdrawals(withdrawalIndex);
      const resolved = withdrawalInfo[4];

      if (resolved) {
        const chainId = getChainIdFromTokenId(args.tokenid);
        const explorerUrl = chainId ? CHAIN_EXPLORERS[chainId] : null;
        const depositAddress = await accounting.evmAddress();

        console.log("\n=== Withdrawal Complete ===");
        console.log(`Withdrawal #${withdrawalIndex} has been resolved on Sapphire`);
        console.log("The backend has broadcast the transaction to the destination chain.");
        if (explorerUrl) {
          console.log(`\nView transactions: ${explorerUrl}/address/${depositAddress}#tokentxns`);
        }
        return { ...result, withdrawalIndex, resolved: true };
      }

      process.stdout.write(".");
      await new Promise((r) => setTimeout(r, 5000));
    }

    console.log("\n\nTimeout waiting for resolution.");
    console.log(`Withdrawal #${withdrawalIndex} is still pending.`);
    console.log("The backend will continue processing it in the background.");

    return { ...result, withdrawalIndex, resolved: false };
  });

task("watchWithdrawal")
  .addParam("contract", "The address of the Accounting contract")
  .addParam("index", "Withdrawal index to watch")
  .addOptionalParam("timeout", "Timeout in seconds", "300")
  .setDescription("Watch a withdrawal until it is resolved and broadcast")
  .setAction(async (args, hre) => {
    const accounting = await hre.ethers.getContractAt(
      "Accounting",
      args.contract,
    );
    const index = parseInt(args.index);
    const timeoutSeconds = parseInt(args.timeout);

    console.log("Watching withdrawal at index:", index);

    // Get withdrawal details
    const withdrawal = await accounting.withdrawals(index);
    console.log("\nWithdrawal details:");
    console.log("  User:", withdrawal[0]);
    console.log("  Amount:", withdrawal[1].toString());
    console.log("  Block:", withdrawal[2].toString());
    console.log("  Token ID:", withdrawal[3]);
    console.log("  Resolved:", withdrawal[4]);

    if (withdrawal[4]) {
      console.log("\nWithdrawal already resolved!");
      return { resolved: true };
    }

    // Poll until resolved
    const startTime = Date.now();
    console.log(`\nWaiting for resolver to process (timeout: ${timeoutSeconds}s)...`);

    while (Date.now() - startTime < timeoutSeconds * 1000) {
      const current = await accounting.withdrawals(index);
      if (current[4]) {
        console.log("\n\n=== Withdrawal Resolved ===");
        console.log("The resolver has processed this withdrawal.");
        console.log("The broadcaster will now send it to the destination chain.");
        return { resolved: true };
      }
      process.stdout.write(".");
      await new Promise((r) => setTimeout(r, 180000)); // 3 minutes
    }

    console.log("\n\nTimeout - withdrawal not yet resolved.");
    console.log("The resolver service may still process it in the background.");
    return { resolved: false };
  });
