import { task } from "hardhat/config";
import { formatUnits } from "ethers";
import {
  fetchBalance,
  fetchJson,
  getSiweToken,
  isJsonObject,
  normalizeApiBaseUrl,
} from "./utils/siwe";
import { withRetry } from "./utils/retry";

export type TokenInfo = {
  tokenId: string;
  tokenType: number;
  tokenTypeName: string;
  chainId: number | null;
  chainName: string | null;
  tokenAddress: string | null;
  name: string | null;
  symbol: string | null;
  decimals: number | null;
};

function parseTokenInfo(token: unknown): TokenInfo {
  if (!isJsonObject(token) || typeof token.token_id !== "string") {
    throw new Error("Unexpected token entry in API response");
  }
  return {
    tokenId: token.token_id,
    tokenType: Number(token.token_type),
    tokenTypeName: String(token.token_type_name),
    chainId: typeof token.chain_id === "number" ? token.chain_id : null,
    chainName: typeof token.chain_name === "string" ? token.chain_name : null,
    tokenAddress:
      typeof token.token_address === "string" ? token.token_address : null,
    name: typeof token.name === "string" ? token.name : null,
    symbol: typeof token.symbol === "string" ? token.symbol : null,
    decimals: typeof token.decimals === "number" ? token.decimals : null,
  };
}

export async function fetchTokens(params: { apiBaseUrl: string }): Promise<TokenInfo[]> {
  const url = `${params.apiBaseUrl}/v1/accounting/tokens`;
  const data = await fetchJson(url);

  if (!isJsonObject(data) || !Array.isArray(data.tokens)) {
    throw new Error("Unexpected token list response from API");
  }

  return data.tokens.map(parseTokenInfo);
}

async function fetchToken(params: {
  apiBaseUrl: string;
  tokenId: string;
}): Promise<TokenInfo> {
  const url = `${params.apiBaseUrl}/v1/accounting/tokens/${params.tokenId}`;
  const data = await fetchJson(url);
  return parseTokenInfo(data);
}

export function formatBalance(balance: bigint, decimals: number | null): string {
  if (decimals === null) {
    return balance.toString();
  }
  return formatUnits(balance, decimals);
}

export function tokenName(token: Pick<TokenInfo, "symbol" | "tokenTypeName" | "chainName">): string {
  const symbol = token.symbol ?? token.tokenTypeName;
  return token.chainName ? `${symbol} (${token.chainName})` : symbol;
}

function printBalanceEntry(
  tokenId: string,
  name: string,
  amount: string,
  rawBalance: bigint,
): void {
  console.log(`Token ID: ${tokenId}`);
  console.log(`Name:     ${name}`);
  console.log(`Amount:   ${amount} (${rawBalance.toString()})`);
}

task("getBalance")
  .addOptionalParam(
    "tokenid",
    "Token ID (32-byte hex). If omitted, reports the balance for every token returned by /v1/accounting/tokens",
  )
  .addOptionalParam(
    "apiurl",
    "API base URL",
    "https://api.testnet.privana.finance",
  )
  .addOptionalParam("chainid", "Chain ID for SIWE message", "23295")
  .setDescription("Get user balance for a token, or all registered tokens (requires SIWE authentication)")
  .setAction(async (args, hre) => {
    const [signer] = await hre.ethers.getSigners();
    const userAddress = signer.address;
    const apiBaseUrl = normalizeApiBaseUrl(args.apiurl);
    const chainId = parseInt(args.chainid);

    console.log("User address:", userAddress);
    console.log("API URL:", apiBaseUrl);

    console.log("\nAuthenticating with SIWE...");
    const siweToken = await withRetry(() =>
      getSiweToken({
        apiBaseUrl,
        signer,
        userAddress,
        chainId,
      }),
    );
    console.log("SIWE authentication successful");

    if (args.tokenid) {
      const token = await withRetry(() =>
        fetchToken({ apiBaseUrl, tokenId: args.tokenid }),
      );

      const balance = await withRetry(() =>
        fetchBalance({
          apiBaseUrl,
          userAddress,
          tokenId: args.tokenid,
          siweToken,
        }),
      );

      console.log("\n=== Balance ===");
      printBalanceEntry(
        token.tokenId,
        tokenName(token),
        formatBalance(balance, token.decimals),
        balance,
      );

      return balance;
    }

    console.log("\nFetching token list...");
    const tokens = await withRetry(() => fetchTokens({ apiBaseUrl }));
    console.log(`Found ${tokens.length} registered token(s)`);

    const results = await Promise.all(
      tokens.map(async (token) => {
        try {
          const balance = await withRetry(() =>
            fetchBalance({
              apiBaseUrl,
              userAddress,
              tokenId: token.tokenId,
              siweToken,
            }),
          );
          return {
            token,
            amount: formatBalance(balance, token.decimals),
            rawBalance: balance,
          };
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          return { token, error: message };
        }
      }),
    );

    const balances: Record<string, string> = {};
    console.log("\n=== Balances ===");
    for (const result of results) {
      const name = tokenName(result.token);
      console.log("");
      if ("error" in result) {
        console.warn(`Token ID: ${result.token.tokenId}`);
        console.warn(`Name:     ${name}`);
        console.warn(`Error:    ${result.error}`);
      } else {
        balances[result.token.tokenId] = result.amount;
        printBalanceEntry(result.token.tokenId, name, result.amount, result.rawBalance);
      }
    }

    return balances;
  });

task("transferERC20")
  .addOptionalParam("token", "ERC20 token address", "0x12084e1a0fe92b5ab803a81a0ae54d91040f89ca")
  .addOptionalParam("recipient", "Recipient address", "0x284a3Fe2939a4e4859e6321537d4264533E3D549")
  .addOptionalParam("amount", "Amount in token units", "1")
  .addOptionalParam("decimals", "Token decimals", "18")
  .setAction(async (args, hre) => {
    const tokenAddress = args.token;
    const recipient = args.recipient;
    const amount = hre.ethers.parseUnits(args.amount, parseInt(args.decimals));

    const [signer] = await hre.ethers.getSigners();
    console.log("Sender address:", signer.address);

    const feeData = await hre.ethers.provider.getFeeData();
    console.log("Fee data:", {
      maxFeePerGas: feeData.maxFeePerGas?.toString(),
      maxPriorityFeePerGas: feeData.maxPriorityFeePerGas?.toString(),
      gasPrice: feeData.gasPrice?.toString(),
    });

    const erc20 = new hre.ethers.Contract(
      tokenAddress,
      [
        "function transfer(address to, uint256 amount) returns (bool)",
        "function balanceOf(address) view returns (uint256)",
      ],
      signer
    );

    const balance = await erc20.balanceOf(signer.address);
    console.log("Token balance:", hre.ethers.formatUnits(balance, parseInt(args.decimals)));

    if (balance < amount) {
      throw new Error("Insufficient token balance!");
    }

    console.log("\nSending EIP-1559 (type 2) transaction...");
    console.log(`Token: ${tokenAddress}`);
    console.log(`Recipient: ${recipient}`);
    console.log(`Amount: ${args.amount} (${amount.toString()} wei)`);

    const tx = await erc20.transfer(recipient, amount, {
      maxFeePerGas: feeData.maxFeePerGas,
      maxPriorityFeePerGas: feeData.maxPriorityFeePerGas,
      type: 2,
    });

    console.log("Transaction hash:", tx.hash);
    console.log("Waiting for confirmation...");

    const receipt = await tx.wait();

    console.log("\n=== Transaction Confirmed ===");
    console.log("Block number:", receipt?.blockNumber);
    console.log("Transaction index:", receipt?.index);
    console.log("Gas used:", receipt?.gasUsed.toString());
    console.log("Type:", receipt?.type, "(should be 2 for EIP-1559)");

    const rawReceipt = await hre.ethers.provider.send("eth_getTransactionReceipt", [tx.hash]);
    console.log("\nRaw receipt status:", rawReceipt.status);
    console.log("Receipt type field:", rawReceipt.type);

    console.log("\n=== Transaction details ===");
    console.log("Block number:", receipt?.blockNumber);
    console.log("Transaction index:", receipt?.index);
    console.log("User address:", signer.address);
    console.log("Transaction hash:", tx.hash);

    return tx.hash;
  });

task("getAuthKeyHash")
  .addParam("address", "The address of the SIWE Auth contract")
  .setDescription("Get the hash of the stored encryption key from the SIWE Auth contract")
  .setAction(async (args, hre) => {
    const siweAuthContract = new hre.ethers.Contract(
      args.address,
      ["function getAuthTokenEncKeyHash() external view returns (bytes32)"],
      hre.ethers.provider
    );

    const keyHash = await siweAuthContract.getAuthTokenEncKeyHash();
    console.log("Encryption key hash on contract:", keyHash);
    return keyHash;
  });
