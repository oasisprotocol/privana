import { task } from "hardhat/config";
import {
  fetchJson,
  getSiweToken,
  isJsonObject,
  normalizeApiBaseUrl,
} from "./utils/siwe";
import { fetchTokens, formatBalance, tokenName, TokenInfo } from "./tokens";
import { withRetry } from "./utils/retry";

type HistoryEntry = {
  kind: string;
  timestamp: number;
  tokenId: string | null;
  amount: string | null;
  chainId: number | null;
  counterparty: string | null;
  depositId: string | null;
};

function parseHistoryEntry(entry: unknown): HistoryEntry {
  if (!isJsonObject(entry) || typeof entry.kind !== "string" || typeof entry.timestamp !== "number") {
    throw new Error("Unexpected history entry in API response");
  }
  return {
    kind: entry.kind,
    timestamp: entry.timestamp,
    tokenId: typeof entry.token_id === "string" ? entry.token_id : null,
    amount: typeof entry.amount === "string" ? entry.amount : null,
    chainId: typeof entry.chain_id === "number" ? entry.chain_id : null,
    counterparty: typeof entry.counterparty === "string" ? entry.counterparty : null,
    depositId: typeof entry.deposit_id === "string" ? entry.deposit_id : null,
  };
}

function printHistoryEntry(
  index: number,
  entry: HistoryEntry,
  tokensById: Map<string, TokenInfo>,
): void {
  const lines: string[] = [];
  lines.push(`Kind:         ${entry.kind}`);
  lines.push(`Timestamp:    ${new Date(entry.timestamp * 1000).toISOString()}`);

  const token = entry.tokenId ? tokensById.get(entry.tokenId) : undefined;
  if (entry.tokenId) {
    lines.push(`Token:        ${token ? tokenName(token) : entry.tokenId}`);
  }
  if (entry.amount !== null) {
    const amount = token ? formatBalance(BigInt(entry.amount), token.decimals) : entry.amount;
    lines.push(`Amount:       ${amount}`);
  }
  if (entry.counterparty) {
    lines.push(`Counterparty: ${entry.counterparty}`);
  }
  if (entry.chainId !== null) {
    lines.push(`Chain ID:     ${entry.chainId}`);
  }
  if (entry.depositId) {
    lines.push(`Deposit ID:   ${entry.depositId}`);
  }

  const marker = `${index}. `;
  const indent = " ".repeat(marker.length);
  lines.forEach((line, i) => {
    console.log(`${i === 0 ? marker : indent}${line}`);
  });
}

const MAX_PAGE_SIZE = 100;

async function fetchHistoryPage(params: {
  apiBaseUrl: string;
  siweToken: string;
  offset: number;
  limit: number;
}): Promise<{ history: HistoryEntry[]; total: number }> {
  const url = `${params.apiBaseUrl}/v1/accounting/history?offset=${params.offset}&limit=${params.limit}`;
  const data = await fetchJson(url, {
    headers: { "X-SIWE-Token": params.siweToken },
  });

  if (!isJsonObject(data) || !Array.isArray(data.history) || typeof data.total !== "number") {
    throw new Error("Unexpected history response from API");
  }

  return { history: data.history.map(parseHistoryEntry), total: data.total };
}

task("history")
  .addOptionalParam(
    "offset",
    "0-indexed page number from the oldest entries, or negative from the end (-1 is the latest page). Ignored when --limit is 0",
    "-1",
  )
  .addOptionalParam("limit", "Page size, max 100. Use 0 to fetch the entire history from the beginning", "50")
  .addOptionalParam(
    "apiurl",
    "API base URL",
    "https://api.testnet.privana.finance",
  )
  .addOptionalParam("chainid", "Chain ID for SIWE message", "23295")
  .setDescription("Get a page of the authenticated user's account history (requires SIWE authentication)")
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

    console.log("\nFetching token list...");
    const tokens = await withRetry(() => fetchTokens({ apiBaseUrl }));
    const tokensById = new Map(tokens.map((t) => [t.tokenId, t]));

    const limit = parseInt(args.limit);

    let history: HistoryEntry[];
    let total: number;
    let startIndex: number;

    if (limit === 0) {
      console.log("\nFetching entire history from the beginning...");
      const firstPage = await withRetry(() =>
        fetchHistoryPage({
          apiBaseUrl,
          siweToken,
          offset: 0,
          limit: MAX_PAGE_SIZE,
        }),
      );
      total = firstPage.total;
      const totalPages = Math.max(1, Math.ceil(total / MAX_PAGE_SIZE));

      const remainingPages = await Promise.all(
        Array.from({ length: totalPages - 1 }, (_, i) =>
          withRetry(() =>
            fetchHistoryPage({
              apiBaseUrl,
              siweToken,
              offset: i + 1,
              limit: MAX_PAGE_SIZE,
            }),
          ),
        ),
      );

      history = [
        ...firstPage.history,
        ...remainingPages.flatMap((page) => page.history),
      ];
      startIndex = 0;
    } else {
      console.log("\nFetching history...");
      const result = await withRetry(() =>
        fetchHistoryPage({
          apiBaseUrl,
          siweToken,
          offset: parseInt(args.offset),
          limit,
        }),
      );
      history = result.history;
      total = result.total;

      // offset is a 0-indexed page number (oldest-first), or negative to count pages
      // from the end (-1 is the latest page) — resolve it to an absolute record index
      // so entries can be enumerated by their true position in the full history.
      const requestedOffset = parseInt(args.offset);
      const totalPages = Math.max(1, Math.ceil(total / limit));
      const pageIndex = requestedOffset >= 0 ? requestedOffset : totalPages + requestedOffset;
      startIndex = pageIndex * limit;
    }

    console.log(`\n=== History (${history.length} of ${total} total) ===`);
    history.forEach((entry, i) => {
      console.log("");
      printHistoryEntry(startIndex + i + 1, entry, tokensById);
    });

    return { history, total };
  });
