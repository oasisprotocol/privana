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
    "https://testnet.privana.finance",
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
  .addParam("txhash", "Transaction hash on source chain")
  .addParam("amount", "Deposit amount in base units (e.g. wei)")
  .addOptionalParam(
    "apiurl",
    "API base URL",
    "https://testnet.privana.finance",
  )
  .addOptionalParam("chainid", "Chain ID for SIWE message", "23295")
  .addOptionalParam("sourcechainid", "Source chain ID where deposit was made", "84532")
  .addOptionalParam("logindex", "Log index for ERC20 deposits", "0")
  .addOptionalParam("chaintype", "Chain type", "evm")
  .addOptionalParam("keyversion", "Key derivation version", "0")
  .setDescription("Check/trigger deposit verification and sweep (requires SIWE authentication)")
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const userAddress = signer.address;
    const apiBaseUrl = normalizeApiBaseUrl(args.apiurl);
    const chainId = parseInt(args.chainid);

    console.log("User address:", userAddress);
    console.log("API URL:", apiBaseUrl);
    console.log("Tx hash:", args.txhash);
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
          tx_hash: args.txhash,
          amount: args.amount,
          log_index: parseInt(args.logindex),
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

    return data;
  });
