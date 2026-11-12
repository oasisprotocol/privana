import { task } from "hardhat/config";
import { isJsonObject, login } from "./utils/siwe";

type Deposit = Record<string, unknown>;

function printDeposit(deposit: Deposit) {
  console.log("Deposit id:", deposit.deposit_id);
  console.log("  Status:", deposit.status);
  if (deposit.amount) console.log("  Amount:", deposit.amount);
  if (deposit.token_address) console.log("  Token:", deposit.token_address);
}

task("deposits:address")
  .addOptionalParam(
    "apiurl",
    "API base URL",
    "https://api.testnet.privana.finance",
  )
  .addOptionalParam("chainid", "Chain ID for SIWE message", "23295")
  .addOptionalParam("keyversion", "Key derivation version", "0")
  .addOptionalParam("chaintype", "Chain type", "evm")
  .setDescription("Get per-user deposit address (requires SIWE authentication)")
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const post = await login(signer, args.apiurl, args.chainid);

    console.log("\nFetching deposit address...");
    const data = await post("deposits/address", {
      chain_type: args.chaintype,
      version: parseInt(args.keyversion),
    });
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
          console.log(
            `  Chain ${chainId}: native=${limits.native}, erc20=${limits.erc20}`,
          );
        }
      }
    }

    return data.deposit_address;
  });

task("deposits:check")
  .addParam("txhash", "Transaction hash on source chain")
  .addParam("amount", "Deposit amount in base units (e.g. wei)")
  .addOptionalParam(
    "apiurl",
    "API base URL",
    "https://api.testnet.privana.finance",
  )
  .addOptionalParam("chainid", "Chain ID for SIWE message", "23295")
  .addOptionalParam(
    "sourcechainid",
    "Source chain ID where deposit was made",
    "84532",
  )
  .addOptionalParam("logindex", "Log index for ERC20 deposits", "0")
  .addOptionalParam("chaintype", "Chain type", "evm")
  .addOptionalParam("keyversion", "Key derivation version", "0")
  .setDescription(
    "Check/trigger deposit verification and sweep (requires SIWE authentication)",
  )
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const post = await login(signer, args.apiurl, args.chainid);

    console.log("Tx hash:", args.txhash);
    console.log("Source chain:", args.sourcechainid);

    console.log("\nChecking deposit...");
    const deposit = await post("deposits/check", {
      chain_id: parseInt(args.sourcechainid),
      tx_hash: args.txhash,
      amount: args.amount,
      log_index: parseInt(args.logindex),
      version: parseInt(args.keyversion),
    });
    if (!isJsonObject(deposit)) {
      throw new Error("Unexpected response from API");
    }

    console.log("\n=== Deposit Check Result ===");
    printDeposit(deposit);
    return deposit;
  });

task("deposits:check-all")
  .addOptionalParam(
    "apiurl",
    "API base URL",
    "https://api.testnet.privana.finance",
  )
  .addOptionalParam("chainid", "Chain ID for SIWE message", "23295")
  .addOptionalParam("keyversion", "Key derivation version", "0")
  .addOptionalParam(
    "tokenid",
    "Token id (bytes32) to check (default: every configured token)",
  )
  .setDescription(
    "Find and claim every unclaimed deposit, sweeping and crediting each (requires SIWE authentication)",
  )
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const post = await login(signer, args.apiurl, args.chainid);

    console.log("Token id:", args.tokenid ?? "all configured tokens");
    console.log("\nChecking all deposits...");
    const deposits = await post("deposits/check-all", {
      ...(args.tokenid ? { token_id: args.tokenid } : {}),
      version: parseInt(args.keyversion),
    });
    if (!Array.isArray(deposits) || !deposits.every(isJsonObject)) {
      throw new Error("Unexpected response from API");
    }

    console.log("\n=== Deposit Check Result ===");
    if (deposits.length === 0) {
      console.log("No deposit in flight.");
    }
    deposits.forEach(printDeposit);

    return deposits;
  });
