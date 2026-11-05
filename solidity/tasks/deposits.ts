import { task } from "hardhat/config";
import {
  authenticate,
  fetchJson,
  isJsonObject,
  normalizeApiBaseUrl,
} from "./utils/siwe";

task("getDepositAddress")
  .addOptionalParam(
    "apiurl",
    "API base URL",
    "https://api.testnet.privana.finance",
  )
  .addOptionalParam("chainid", "Chain ID for SIWE message", "23295")
  .addOptionalParam("chaintype", "Chain type", "evm")
  .addOptionalParam("keyversion", "Key derivation version", "0")
  .setDescription("Get per-user deposit address (requires SIWE authentication)")
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const userAddress = signer.address;
    const apiBaseUrl = normalizeApiBaseUrl(args.apiurl);
    const chainId = parseInt(args.chainid);

    console.log("User address:", userAddress);
    console.log("API URL:", apiBaseUrl);

    console.log("\nAuthenticating with SIWE...");
    const { jwtAccessToken } = await authenticate({
      apiBaseUrl,
      signer,
      userAddress,
      chainId,
    });
    console.log("Authentication successful");

    console.log("\nFetching deposit address...");
    const data = await fetchJson(
      `${apiBaseUrl}/v1/accounting/deposits/address`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${jwtAccessToken}`,
        },
        body: JSON.stringify({
          chain_type: args.chaintype,
          version: parseInt(args.keyversion),
        }),
      },
    );

    if (!isJsonObject(data)) {
      throw new Error("Unexpected response from API");
    }

    console.log("\n=== Deposit Address ===");
    console.log("Address:", data.deposit_address);
    console.log("Chain type:", data.chain_type);
    console.log("Version:", data.version);
    if (isJsonObject(data.min_deposit)) {
      console.log("\nMinimum deposits:");
      for (const [chainId, limits] of Object.entries(data.min_deposit)) {
        if (isJsonObject(limits)) {
          console.log(`  Chain ${chainId}: native=${limits.native}, erc20=${limits.erc20}`);
        }
      }
    }

    return data.deposit_address;
  });

task("checkDeposit")
  .addParam(
    "token",
    "Asset to claim: an ERC20 contract, or the zero address for the native asset",
  )
  .addOptionalParam(
    "apiurl",
    "API base URL",
    "https://api.testnet.privana.finance",
  )
  .addOptionalParam("chainid", "Chain ID for SIWE message", "23295")
  .addOptionalParam("sourcechainid", "Source chain ID where deposit was made", "84532")
  .addOptionalParam("chaintype", "Chain type", "evm")
  .addOptionalParam("keyversion", "Key derivation version", "0")
  .addFlag(
    "nowait",
    "Return once the sweep is started instead of waiting until it is credited",
  )
  .setDescription(
    "Claim a deposit: sweep and credit everything finalized at your deposit address (requires SIWE authentication)",
  )
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const userAddress = signer.address;
    const apiBaseUrl = normalizeApiBaseUrl(args.apiurl);
    const chainId = parseInt(args.chainid);

    console.log("User address:", userAddress);
    console.log("API URL:", apiBaseUrl);
    console.log("Token:", args.token);
    console.log("Source chain:", args.sourcechainid);

    console.log("\nAuthenticating with SIWE...");
    const { jwtAccessToken } = await authenticate({
      apiBaseUrl,
      signer,
      userAddress,
      chainId,
    });
    console.log("Authentication successful");

    console.log("\nChecking deposit...");
    const data = await fetchJson(
      `${apiBaseUrl}/v1/accounting/deposits/check`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${jwtAccessToken}`,
        },
        body: JSON.stringify({
          chain_type: args.chaintype,
          chain_id: parseInt(args.sourcechainid),
          token_address: args.token,
          version: parseInt(args.keyversion),
        }),
      },
    );

    if (!isJsonObject(data)) {
      throw new Error("Unexpected response from API");
    }

    console.log("\n=== Deposit Check Result ===");
    console.log("Status:", data.status);
    if (data.deposit_id) console.log("Deposit id:", data.deposit_id);
    if (data.amount) console.log("Amount:", data.amount);
    if (data.token_address) console.log("Token:", data.token_address);
    if (data.detail) console.log("Detail:", data.detail);

    // The sweep and credit run in the background: poll until the deposit is credited.
    let result: Record<string, unknown> = data;
    if (
      !args.nowait &&
      result.status === "pending" &&
      typeof result.deposit_id === "string"
    ) {
      console.log("\nWaiting for the deposit to be credited...");
      for (
        let attempt = 0;
        attempt < 60 && result.status === "pending";
        attempt++
      ) {
        await new Promise((resolve) => setTimeout(resolve, 5000));
        const status = await fetchJson(
          `${apiBaseUrl}/v1/accounting/deposits/status/${result.deposit_id}`,
          { headers: { Authorization: `Bearer ${jwtAccessToken}` } },
        );
        if (!isJsonObject(status)) {
          throw new Error("Unexpected response from API");
        }
        result = status;
        console.log("Status:", result.status);
      }
      if (result.status === "credited") {
        console.log("Deposit credited.");
      } else if (result.status === "error") {
        console.log("Sweep failed:", result.detail);
      } else {
        console.log("Still pending; check again later with the same command.");
      }
    }

    return result;
  });
