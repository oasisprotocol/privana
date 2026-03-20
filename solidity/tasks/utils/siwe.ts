export type JsonObject = Record<string, unknown>;

export function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function normalizeApiBaseUrl(apiUrl: string): string {
  return apiUrl.replace(/\/+$/, "");
}

export async function fetchJson(
  url: string,
  init?: RequestInit,
): Promise<unknown> {
  const response = await fetch(url, init);
  const bodyText = await response.text();

  let body: unknown = bodyText;
  if (bodyText) {
    try {
      body = JSON.parse(bodyText);
    } catch {
      // Leave body as plain text.
    }
  }

  if (!response.ok) {
    const detail =
      isJsonObject(body) && typeof body.detail === "string"
        ? body.detail
        : bodyText;
    throw new Error(
      `HTTP ${response.status} ${response.statusText}${detail ? `: ${detail}` : ""}`,
    );
  }

  return body;
}

// Must match the Sapphire on-chain SiweParser expectations (strict format).
// See: solidity/contracts/auth/AccountingSiweAuth.sol and @oasisprotocol/sapphire-contracts SiweParser.
export function buildSiweMessage(params: {
  domain: string;
  address: string;
  chainId: number;
  uri: string;
  statement: string;
  nonce: string;
  issuedAt: string;
  expirationTime?: string;
}): string {
  const domain = params.domain.trim();
  const statement = params.statement.trim().replace(/\s+/g, " ");
  const nonce = params.nonce.trim();

  if (!domain) {
    throw new Error("SIWE domain is empty");
  }
  if (nonce.length < 8) {
    throw new Error("SIWE nonce must be at least 8 characters");
  }

  return (
    `${domain} wants you to sign in with your Ethereum account:\n` +
    `${params.address}\n` +
    `\n` +
    `${statement}\n` +
    `\n` +
    `URI: ${params.uri}\n` +
    `Version: 1\n` +
    `Chain ID: ${params.chainId}\n` +
    `Nonce: ${nonce}\n` +
    `Issued At: ${params.issuedAt}` +
    (params.expirationTime ? `\nExpiration Time: ${params.expirationTime}` : "")
  );
}

export async function getSiweToken(params: {
  apiBaseUrl: string;
  signer: { signMessage: (message: string) => Promise<string> };
  userAddress: string;
  chainId: number;
}): Promise<string> {
  // Fetch domain from API
  const domainResp = await fetchJson(
    `${params.apiBaseUrl}/v1/accounting/auth/domain`,
  );
  const domain =
    isJsonObject(domainResp) && typeof domainResp.domain === "string"
      ? domainResp.domain
      : null;
  if (!domain || !domain.trim()) {
    throw new Error("API returned an invalid SIWE domain");
  }

  // Fetch nonce from API (server-generated for replay protection)
  const nonceResp = await fetchJson(
    `${params.apiBaseUrl}/v1/accounting/auth/nonce?address=${params.userAddress}`,
  );
  const nonce =
    isJsonObject(nonceResp) && typeof nonceResp.nonce === "string"
      ? nonceResp.nonce
      : null;
  if (!nonce || nonce.length < 8) {
    throw new Error("API returned an invalid SIWE nonce");
  }

  const issuedAt = new Date().toISOString();
  const expirationTime = new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString();
  const siweMessage = buildSiweMessage({
    domain,
    address: params.userAddress,
    chainId: params.chainId,
    uri: params.apiBaseUrl,
    statement: "Sign in to view private Accounting balances.",
    nonce,
    issuedAt,
    expirationTime,
  });

  const signature = await params.signer.signMessage(siweMessage);

  const loginResp = await fetchJson(
    `${params.apiBaseUrl}/v1/accounting/auth/login`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ siwe_message: siweMessage, signature }),
    },
  );

  const token =
    isJsonObject(loginResp) && typeof loginResp.siwe_token === "string"
      ? loginResp.siwe_token
      : null;
  if (!token || !token.startsWith("0x")) {
    throw new Error("API returned an invalid SIWE token");
  }
  return token;
}

export async function fetchBalance(params: {
  apiBaseUrl: string;
  userAddress: string;
  tokenId: string;
  siweToken: string;
}): Promise<bigint> {
  const url = `${params.apiBaseUrl}/v1/accounting/balances/${params.userAddress}/${params.tokenId}`;
  const data = await fetchJson(url, {
    headers: { "X-SIWE-Token": params.siweToken },
  });

  if (!isJsonObject(data) || typeof data.balance !== "string") {
    throw new Error("Unexpected balance response from API");
  }

  return BigInt(data.balance);
}
