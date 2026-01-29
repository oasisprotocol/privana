import { task } from "hardhat/config";
import * as cbor from "cbor";
import { Interface } from "ethers";
import AccountingArtifact from "../artifacts/contracts/Accounting.sol/Accounting.json";

// Build error selectors from the ABI
function buildErrorSelectors(): Record<string, string> {
  const iface = new Interface(AccountingArtifact.abi);
  const selectors: Record<string, string> = {};
  iface.forEachError((error) => {
    // selector is 0x prefixed, we want just the hex part
    selectors[error.selector.slice(2)] = error.name;
  });
  return selectors;
}

const ERROR_SELECTORS = buildErrorSelectors();

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
        const errorName = ERROR_SELECTORS[errorBytes] || `Unknown(0x${errorBytes})`;
        return { ok: false, error: `Contract reverted: ${errorName}` };
      }
      return { ok: false, error: message || "Unknown error" };
    }

    return { ok: true };
  } catch {
    // If we can't decode, assume it's ok (old format or success)
    return { ok: true };
  }
}

task("withdraw")
  .addParam("contract", "The address of the Accounting contract")
  .addParam("tokenid", "Token ID (32-byte hex)")
  .addParam("amount", "Amount in token base units (e.g., 1000000 for 1 USDC)")
  .addOptionalParam(
    "apiurl",
    "API base URL",
    "https://p8000.m1356.opf-testnet-rofl-25.rofl.app",
  )
  .addOptionalParam("timeout", "Timeout in seconds to wait for resolution", "120")
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const userAddress = signer.address;
    const accounting = await hre.ethers.getContractAt(
      "Accounting",
      args.contract,
    );

    console.log("User address:", userAddress);
    console.log("Token ID:", args.tokenid);
    console.log("Amount (base units):", args.amount);

    // Get balance before withdrawal for verification
    const balanceUrl = `${args.apiurl}/v1/accounting/balances/${userAddress}/${args.tokenid}`;
    const balanceBefore = await fetch(balanceUrl)
      .then((r) => r.json())
      .then((d) => BigInt(d.balance || "0"))
      .catch(() => BigInt(0));
    console.log("Balance before:", balanceBefore.toString());

    // Get EIP-712 domain from contract
    const domainTuple = await accounting.eip712Domain();
    const domain = {
      name: domainTuple[1],
      version: domainTuple[2],
      chainId: Number(domainTuple[3]),
      verifyingContract: domainTuple[4],
    };

    console.log("\nEIP-712 Domain:", domain);

    // Get the current withdrawal nonce for the user
    const nonce = await accounting.withdrawalNonces(userAddress);
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
    const apiUrl = `${args.apiurl}/v1/accounting/withdraw`;
    const payload = {
      user_address: userAddress,
      token_id: args.tokenid,
      amount: args.amount, // Keep as string to preserve precision for large values
      nonce: nonce.toString(),
      signature: signature,
    };

    console.log("\nSubmitting withdrawal request to API...");

    const response = await fetch(apiUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });

    const responseText = await response.text();

    if (!response.ok) {
      console.error("\nAPI Error:", response.status, response.statusText);
      console.error("Response:", responseText);
      throw new Error(`API request failed: ${response.status}`);
    }

    const result = JSON.parse(responseText);
    const submissionId = result.submission_id;

    // Decode the CBOR response to check for errors
    const decoded = decodeSubmissionResponse(submissionId);
    if (!decoded.ok) {
      console.error("\nWithdrawal failed:", decoded.error);
      throw new Error(decoded.error);
    }

    console.log("Withdrawal request submitted successfully");

    // Get the withdrawal index by checking pending withdrawals
    console.log("\nWaiting for withdrawal to be created on Sapphire...");
    await new Promise((r) => setTimeout(r, 30000)); // Wait for tx to be mined

    // Find our pending withdrawal
    const pendingUrl = `${args.apiurl}/v1/accounting/withdraw/pending/${userAddress}`;
    const pendingResponse = await fetch(pendingUrl);
    const pendingData = await pendingResponse.json();

    if (!pendingData.pending_withdrawals || pendingData.pending_withdrawals.length === 0) {
      // No pending withdrawals - check if it was already processed by comparing balances
      const balanceAfter = await fetch(balanceUrl)
        .then((r) => r.json())
        .then((d) => BigInt(d.balance || "0"))
        .catch(() => BigInt(0));

      const expectedBalance = balanceBefore - BigInt(args.amount);
      if (balanceAfter === expectedBalance) {
        console.log("\n=== Withdrawal Complete ===");
        console.log("Balance before:", balanceBefore.toString());
        console.log("Balance after:", balanceAfter.toString());
        console.log("Withdrawal was processed quickly and has been broadcast to the destination chain.");
        console.log("\nCheck the destination chain explorer for your funds.");
        return { ...result, resolved: true };
      } else if (balanceAfter < balanceBefore) {
        console.log("\n=== Withdrawal Likely Complete ===");
        console.log("Balance before:", balanceBefore.toString());
        console.log("Balance after:", balanceAfter.toString());
        console.log("Balance decreased - withdrawal appears to have been processed.");
        return { ...result, resolved: true };
      } else {
        console.log("No pending withdrawals found and balance unchanged.");
        console.log("The withdrawal may have failed or is still being processed.");
        return result;
      }
    }

    // Get the most recent pending withdrawal for this user
    const withdrawal = pendingData.pending_withdrawals[pendingData.pending_withdrawals.length - 1];
    const withdrawalIndex = withdrawal.index;
    console.log(`Found withdrawal #${withdrawalIndex}`);

    // Poll until resolved
    const timeoutSeconds = parseInt(args.timeout);
    const startTime = Date.now();
    console.log(`\nWaiting for withdrawal to be resolved and broadcast (timeout: ${timeoutSeconds}s)...`);

    while (Date.now() - startTime < timeoutSeconds * 1000) {
      const withdrawalInfo = await accounting.withdrawals(withdrawalIndex);
      const resolved = withdrawalInfo[4];

      if (resolved) {
        console.log("\n=== Withdrawal Complete ===");
        console.log(`Withdrawal #${withdrawalIndex} has been resolved on Sapphire`);
        console.log("The backend has broadcast the transaction to the destination chain.");
        console.log("\nCheck the destination chain explorer for your funds.");
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

task("setGasPrice")
  .addParam("contract", "Accounting contract address")
  .addParam("chainid", "Chain ID")
  .addParam("gasprice", "Gas price in wei")
  .setAction(async (args, hre) => {
    const accounting = await hre.ethers.getContractAt(
      "Accounting",
      args.contract,
    );
    console.log(
      "Setting gas price for chain",
      args.chainid,
      "to",
      args.gasprice,
    );
    const tx = await accounting.setGasPrice(args.chainid, args.gasprice);
    console.log("Transaction:", tx.hash);
    await tx.wait();
    console.log("Done!");
  });
