import { task } from "hardhat/config";
import {
  fetchBalance,
  fetchJson,
  getSiweToken,
  isJsonObject,
  normalizeApiBaseUrl,
} from "./utils/siwe";

async function tryFetchBalance(
  siweToken: string | null,
  params: { apiBaseUrl: string; userAddress: string; tokenId: string },
  label: string,
): Promise<bigint | null> {
  if (!siweToken) return null;
  try {
    return await fetchBalance({ ...params, siweToken });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.warn(`Failed to fetch balance ${label}: ${message}`);
    return null;
  }
}

task("transfer")
  .addParam("contract", "The address of the Accounting contract")
  .addParam("to", "Recipient address")
  .addParam("tokenid", "Token ID (32-byte hex)")
  .addParam("amount", "Amount in token base units (e.g., 1000000 for 1 USDC)")
  .addOptionalParam(
    "apiurl",
    "API base URL",
    "https://p8000.m1356.opf-testnet-rofl-25.rofl.app",
  )
  .setDescription("Transfer funds from your account to another account")
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const userAddress = hre.ethers.getAddress(signer.address); // Ensure checksum
    const toAddress = hre.ethers.getAddress(args.to); // Ensure checksum
    const apiBaseUrl = normalizeApiBaseUrl(args.apiurl);
    const chainId = Number((await hre.ethers.provider.getNetwork()).chainId);
    const accounting = await hre.ethers.getContractAt(
      "Accounting",
      args.contract,
    );

    console.log("From address:", userAddress);
    console.log("To address:", toAddress);
    console.log("Token ID:", args.tokenid);
    console.log("Amount (base units):", args.amount);

    // SIWE login for private reads (balances/locks).
    let siweToken: string | null = null;
    try {
      siweToken = await getSiweToken({
        apiBaseUrl,
        signer,
        userAddress,
        chainId,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      console.warn(
        `SIWE login failed; skipping private balance reads: ${message}`,
      );
    }

    // Get balance before transfer
    const balanceBefore = await tryFetchBalance(
      siweToken,
      { apiBaseUrl, userAddress, tokenId: args.tokenid },
      "before transfer",
    );
    console.log(
      "Balance before:",
      balanceBefore !== null ? balanceBefore.toString() : "(unavailable)",
    );

    // Get EIP-712 domain from contract
    const domainTuple = await accounting.eip712Domain();
    const domain = {
      name: domainTuple[1],
      version: domainTuple[2],
      chainId: Number(domainTuple[3]),
      verifyingContract: domainTuple[4],
    };

    console.log("\nEIP-712 Domain:", domain);

    // Get the current transfer nonce for the user
    const nonce = await accounting.transferNonces(userAddress);
    console.log("Transfer nonce:", nonce.toString());

    // Define Transfer type
    const types = {
      Transfer: [
        { name: "toAddress", type: "address" },
        { name: "tokenId", type: "bytes32" },
        { name: "amount", type: "uint256" },
        { name: "nonce", type: "uint256" },
      ],
    };

    // Create message
    const message = {
      toAddress: toAddress,
      tokenId: args.tokenid,
      amount: BigInt(args.amount),
      nonce: nonce,
    };

    console.log(
      "\nMessage to sign:",
      JSON.stringify(
        {
          toAddress: message.toAddress,
          tokenId: message.tokenId,
          amount: message.amount.toString(),
          nonce: message.nonce.toString(),
        },
        null,
        2,
      ),
    );

    console.log("\nSigning transfer request...");

    // Sign the typed data
    const signature = await signer.signTypedData(domain, types, message);
    console.log("Signature:", signature);

    // Submit to API
    const apiUrl = `${apiBaseUrl}/v1/accounting/funds/transfer`;
    const payload = {
      to_address: toAddress,
      token_id: args.tokenid,
      amount: args.amount,
      nonce: nonce.toString(),
      signature: signature,
    };

    console.log("\nPayload:", JSON.stringify(payload, null, 2));
    console.log("\nSubmitting transfer request to API...");

    let result: unknown;
    try {
      result = await fetchJson(apiUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      console.error("\nAPI Error:", message);
      throw err;
    }

    if (!isJsonObject(result) || typeof result.submission_id !== "string") {
      throw new Error("Unexpected response from API");
    }
    console.log("\nTransfer submitted successfully!");
    console.log("Submission ID:", result.submission_id);

    // Check balance after (wait a bit for processing)
    console.log("\nWaiting for transaction to process...");
    await new Promise((r) => setTimeout(r, 5000));

    const balanceAfter = await tryFetchBalance(
      siweToken,
      { apiBaseUrl, userAddress, tokenId: args.tokenid },
      "after transfer",
    );
    console.log(
      "Balance after:",
      balanceAfter !== null ? balanceAfter.toString() : "(unavailable)",
    );

    return result;
  });
