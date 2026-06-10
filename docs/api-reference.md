# Accounting Module API Reference

**Base URL:** `/v1/accounting`

This document is the **narrative companion** to the API. It explains *how* the flows fit together — the polling patterns, the auth choices, the status semantics — and points at the live OpenAPI spec for exact field shapes.

## Source of Truth

Exact request/response schemas (types, validation rules, defaults) are generated from the FastAPI app's Pydantic models. Three surfaces expose them:

| Surface | Where | Purpose |
|---|---|---|
| Committed spec | [`docs/openapi.json`](openapi.json) | **Authoritative snapshot.** Pin SDK builds and contract tests against this. Regenerated via `make openapi`; CI fails on drift. |
| Swagger UI | `/docs` (running service) | Interactive exploration, "Try it out" requests, browseable schemas. |
| ReDoc | `/redoc` (running service) | Read-only single-page view of the same spec. |

If this markdown disagrees with `docs/openapi.json`, **trust `docs/openapi.json`**. Contributors who change a Pydantic model must run `make openapi` and commit the regenerated file — the CI lint job runs `make openapi-check` and fails on drift.

## Conventions

- **JSON** for all request and response bodies.
- **Hex strings** include the `0x` prefix and are normalised to lowercase.
- **Amounts** are integers in the token's base units (wei for ETH, smallest unit for ERC-20). Strings are accepted; scientific notation is parsed with `Decimal` to preserve precision.
- **Signatures** are EIP-712 typed-data signatures. User-signed accounting operations are defined in `solidity/contracts/EIP712SignatureVerifier.sol`.
- **Nonces** are per-operation. User-signed operations key nonces by recovered signer; service-signed lock operations key nonces by service. Always fetch the current nonce immediately before signing.
- **Status codes** — most submission endpoints return `200 OK` on synchronous success, `202 Accepted` when work continues in the background (currently only `POST /deposits/check`), `400` for validation errors, `401` for missing/invalid auth, `422` for contract reverts, `429` for rate-limit, `500` for internal failures.

### User-Signed EIP-712 Types

| Type | Fields |
|---|---|
| `Lock` | `serviceAddress address`, `tokenId bytes32`, `amount uint256`, `expiry uint256`, `nonce uint256` |
| `ModifyLock` | `lockId uint256`, `amount uint256`, `newExpiry uint256`, `nonce uint256` |
| `Transfer` | `toAddress address`, `tokenId bytes32`, `amount uint256`, `nonce uint256` |
| `Withdraw` | `tokenId bytes32`, `amount uint256`, `nonce uint256` |

## Authentication

Most write operations are authorized **per-request** by an EIP-712 signature embedded in the body — no session needed.

A subset of endpoints expose **private state** (per-user balances, locks, deposit address, history) and require an authenticated session token instead. Two flows produce one:

### 1. Direct SIWE (first-party Flexvaults origin)

```
GET  /auth/domain                       → which SIWE domain to sign
GET  /auth/nonce?address=<user>         → single-use SIWE nonce
POST /auth/login {siwe_message, sig}    → siwe_token + JWT pair
```

The login response returns:
- `siwe_token` — encrypted Sapphire AuthToken; pass via `X-SIWE-Token` header for direct on-chain confidential reads.
- `jwt_access_token` / `jwt_refresh_token` — pass `Authorization: Bearer <jwt_access_token>` for normal API auth.

Browser requests to `/auth/nonce` and `/auth/login` are **origin-checked** — they must come from a configured Flexvaults SIWE origin. Non-browser clients (no `Origin` header) are accepted.
Backends that only receive a JWT from hosted auth can exchange that JWT for a private-read token with `POST /auth/jwt/siwe-token`.

### 2. OAuth-style cross-domain (third-party apps)

For apps on other origins, use the hosted authorization page with PKCE:

```
GET  /auth/authorize?client_id=…&redirect_uri=…&code_challenge=…&state=…&chain_id=…
                                              → HTML page; user signs SIWE on Flexvaults origin
POST /auth/authorize {siwe_message, sig, …}   → short-lived `code`
POST /auth/token {grant_type=authorization_code, code, code_verifier, …}
                                              → access_token + id_token + refresh_token
```

- `code_challenge_method` must be `S256`.
- `redirect_uri` must exactly match a value registered for the client. `http://localhost` and loopback callbacks are allowed; everything else must be `https`.
- `id_token` is client-scoped (audience = configured client audience or `client_id`) — use it for third-party backend identity verification, not for Flexvaults API calls.

### Endpoint matrix

| Endpoint | Auth | Auth header |
|---|---|---|
| `POST /deposits/address` | required | `Authorization: Bearer …` or `X-SIWE-Token` |
| `POST /deposits/check` | required | same |
| `GET /deposits/status/{id}` | required | same |
| `POST /funds/withdraw-from-lock` | required | same |
| `GET /balances/{token_id}` | required | same |
| `POST /balances/batch` | required | same |
| `GET /funds/locked` | required | same |
| `GET /funds/locked/total/{token_id}` | required | same |
| `GET /funds/expired` | required | same |
| `GET /history` | required | same |
| `POST /onramp/intent` | required | same |
| `POST /onramp/sign-url` | required | same |
| `GET /onramp/pending` | required | same |
| `POST /onramp/{transaction_id}` | required | same |
| `POST /onramp/webhook` | MoonPay webhook signature | `Moonpay-Signature-V2` |
| `POST /auth/jwt/siwe-token` | required | `Authorization: Bearer …` |
| `POST /auth/jwt/logout`, `GET /auth/jwt/me` | required | `Authorization: Bearer …` |
| Everything else | none (signature-gated where applicable) | — |

`Authorization` and `X-SIWE-Token` are mutually exclusive — sending both yields `400`.

### History

`GET /history` returns one page of the authenticated user's on-chain activity history. Entries within a page are ordered oldest to newest. Query parameters are `offset` (default `-1`) and `limit` (default `50`, max `100`). Non-negative offsets are 0-indexed page numbers from the oldest entries; negative offsets select pages from the end (`-1` is the latest page, `-2` the previous page).

| Field | Description | History kind type(s) |
| --- | --- | --- |
| `kind` | Entry type. Known values are `deposit`, `withdraw`, `createLock`, `transferFromLock`, and `transferBalance`; undecoded entries return `unknown`. | all |
| `timestamp` | Entry timestamp. | all |
| `token_id` | Token identifier. | `deposit`, `withdraw`, `createLock`, `transferFromLock`, `transferBalance` |
| `amount` | Token amount as a decimal string. | `deposit`, `withdraw`, `createLock`, `transferFromLock`, `transferBalance` |
| `chain_id` | Source chain for `token_id`, when known. | `deposit`, `withdraw`, `createLock`, `transferFromLock`, `transferBalance` |
| `deposit_id` | Deposit identifier. | `deposit` |
| `counterparty` | Address payload for non-deposit entries: withdrawal destination, lock service, or transfer recipient. | `withdraw`, `createLock`, `transferFromLock`, `transferBalance` |

## Deposit Flow

The deposit path is **address-based**: the contract derives a per-user address (one per `chain_type` × `version`), the user funds it directly, and the backend verifies + sweeps + credits.

```
1. POST /deposits/address           → returns the user's per-user deposit address
                                      (auth-gated — only the user can learn it)
2. user sends funds to that address on the source chain
3. POST /deposits/check             → backend verifies the tx, returns 202
                                      with status="pending" and a deposit_id
4. GET /deposits/status/{deposit_id}  → poll until status="credited"
```

Behaviour notes:

- `POST /deposits/check` is **idempotent**. If the same `(chain_id, tx_hash, log_index)` is submitted twice, the second call short-circuits — returning `200` with `status="credited"` once the credit has landed on Sapphire, or `202` with `status="pending"` while the sweep is still running.
- Sweeps run as background tasks. The processor returns within ~2-3s. Don't block on the response — poll `GET /deposits/status/{deposit_id}`.
- The status endpoint first consults an in-memory record (sweep in progress / failed) and then falls through to an on-chain `isDepositProcessed` check. Records survive restarts via JSON persistence (see `src/README.md`).
- The `min_deposit` field of the address response is a per-chain map — clients should use it to gate the UI.

## On-Ramp Flow (MoonPay)

The fiat on-ramp reuses the deposit path: MoonPay delivers tokens straight to the user's per-user deposit address, and the existing verify → sweep → credit pipeline performs the actual credit. The on-ramp endpoints only **correlate** MoonPay purchase state with Privana deposits — the webhook never credits balances.

```
1. POST /onramp/intent             → create a Privana-owned correlation record;
                                     its transaction_id doubles as the MoonPay
                                     externalTransactionId
2. POST /onramp/sign-url           → validate + sign the MoonPay widget URL
3. user completes the purchase in the MoonPay widget
4. POST /onramp/webhook            → MoonPay reports status + on-chain tx hash
                                     (HMAC-verified)
5. GET  /onramp/pending            → completed purchases that still need
                                     deposit verification
6. POST /deposits/check            → normal deposit verification using the
                                     webhook's tx hash; links deposit_id to the
                                     on-ramp record
7. GET  /deposits/status/{id}      → poll until status="credited"; the record
                                     is marked credited server-side and leaves
                                     /onramp/pending
```

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /onramp/intent` | required | Create an intent pinning `user_address`, deposit `wallet_address`, `token_id`, `chain_id`, and MoonPay currency before the widget opens. |
| `POST /onramp/sign-url` | required | Validate an unsigned MoonPay widget URL (allow-listed host, expected `apiKey`, `walletAddress` = caller's deposit address, `externalCustomerId` = caller, `externalTransactionId` = known intent owned by the caller, allow-listed `currencyCode` matching the intent) and return its HMAC signature. |
| `GET /onramp/pending` | required | The caller's completed, uncredited purchases that carry enough data (`token_id`, `chain_id`, `on_chain_tx_hash`) to run `/deposits/check`. |
| `POST /onramp/{transaction_id}` | required | Upsert caller-owned purchase metadata (e.g. a late `moonpay_transaction_id` binding that merges a matching orphan webhook record). Cannot change `status`; locked fields (`token_id`, `chain_id`) must match. |
| `POST /onramp/webhook` | MoonPay HMAC (`Moonpay-Signature-V2`) | Persist MoonPay transaction state updates. |

Behaviour notes:

- **Intent fields win.** `user_address`, `wallet_address`, `token_id`, `chain_id`, and currency fields set at intent creation are never overwritten by webhook or client updates — conflicting incoming values are dropped and logged.
- **Webhook joining.** A webhook without `externalTransactionId` is joined to a known record by `moonpay_transaction_id`, else to exactly one open intent matching the delivery wallet (and currency) created within the last 24 hours. Ambiguous matches stay orphaned rather than guessed.
- **Status is webhook-owned.** Clients cannot set `status`; only verified webhooks mark a purchase `completed`, and a completed record ignores stale non-completed webhook replays while still accepting late metadata.
- **Credit closes server-side.** `POST /deposits/check` stamps the backend `deposit_id` onto the matching on-ramp record (matched by user, chain and source tx hash); `GET /deposits/status/{deposit_id}` marks the record credited once the backend proves credit. Clients do not need to write back to `/onramp/{transaction_id}` to close the loop.
- **Fail-closed configuration.** `POST /onramp/sign-url` and `POST /onramp/webhook` return `503` until the `MOONPAY_*` keys are configured.

## Withdrawal Flow

```
1. GET /withdraw/nonce/{user}       → current nonce, sign EIP-712 Withdraw(token_id, amount, nonce)
2. POST /withdraw                  → {token_id, amount, nonce, signature};
                                      balance debited immediately and withdrawal queued
3. GET /withdraw/pending/{user}     → poll; resolution + broadcast happens automatically
                                      in the WithdrawalProcessor (~12s loop)
```

- The user's balance is debited at request time. Resolution is delayed by one block (simulation-attack protection) and runs in a background poll loop on the service side — frontends do **not** need to call `resolveWithdrawal` themselves.
- Withdrawals are processed **sequentially per destination chain** to preserve nonce ordering. A failure on one chain does not block others.
- `GET /withdraw/{index}` returns information about a specific withdrawal (resolved or not) — useful for status pages and audit views.

## Lock Flow

Locks are escrow primitives: a user locks funds for a service, and the service can spend within the locked amount until expiry; after expiry, anyone can release the funds back to the user.

| Endpoint | Signer | Purpose |
|---|---|---|
| `GET /funds/lock/nonce/{user}` | — | Fetch nonce for `Lock` EIP-712 |
| `POST /funds/lock` | user | Create a new lock (`{service_address, token_id, amount, expiry, nonce, signature}`) |
| `GET /funds/modify-lock/nonce/{user}` | — | Fetch nonce for `ModifyLock` EIP-712 |
| `POST /funds/modify-lock` | user | Add funds and/or extend expiry (`{lock_id, amount, new_expiry, nonce, signature}`). Pure no-ops are rejected. |
| `GET /funds/transfer-locked/nonce/{service}` | — | Fetch nonce for `TransferFromLock` EIP-712 |
| `POST /funds/transfer-locked` | service | Service consumes part or all of the lock to a destination user (`{user_address, lock_id, to_address, amount, service_address, nonce, signature}`) |
| `POST /funds/withdraw-from-lock` | user (auth) | User withdraws locked funds directly to an external destination (`{to_address, lock_id, amount, nonce, signature}` — the user is resolved from the auth header, not the body) |
| `POST /funds/unlock` | — | Unlock a single expired lock (`{user_address, lock_id}`, no signature; reverts if not yet expired) |
| `POST /funds/unlock-all-expired` | — | Unlock every expired lock for a user in one tx |

Reads:
- `GET /funds/locked` (auth) — active locks; optional `?service_address=…` is a **response-side filter only**, not an authorization gate. Service backends should hit the contract's `getServiceLocks(...)` directly with their own SIWE token.
- `GET /funds/locked/total/{token_id}` (auth) — total locked across all the user's locks for a token.
- `GET /funds/expired` (auth) — every expired lock for the authenticated user.

## Transfer Flow

Direct user-to-user balance transfer inside the accounting module — no on-chain settlement.

```
GET  /funds/transfer/nonce/{user}     → current Transfer nonce
POST /funds/transfer                  → {to_address, token_id, amount, nonce, signature}
```

The signature is an EIP-712 `Transfer` from the source user.

## Private Read Endpoints

All require a session token (`Authorization: Bearer …` or `X-SIWE-Token`). The user is resolved from the token; never trust a user address in the body for reads.

| Endpoint | Returns |
|---|---|
| `GET /balances/{token_id}` | One token balance for the caller. |
| `POST /balances/batch` | Up to 100 token balances in one round trip. Body: `{token_ids: string[]}`. |
| `GET /funds/locked` | Active locks (optional `?service_address` filter). |
| `GET /funds/locked/total/{token_id}` | Sum of locked amounts for one token. |
| `GET /funds/expired` | Locks past their expiry. |
| `GET /history` | On-chain activity history for the caller, defaulting to the latest page. |

A `ContractLogicError` from Sapphire on any of these is mapped to `401 Invalid or expired SIWE token` — clients should reauth and retry.

## Public Read Endpoints

| Endpoint | Returns |
|---|---|
| `GET /tokens` | List of every registered token. |
| `GET /tokens/{token_id}` | One token's metadata: type, chain, address, symbol, decimals. |
| `GET /withdraw/{index}` | Public withdrawal record. |
| `GET /withdraw/pending/{user}` | Pending withdrawals (public — knowing pending state isn't sensitive). |
| `GET /withdraw/nonce/{user}` | Current withdrawal nonce. |
| `GET /funds/transfer/nonce/{user}` | Current transfer nonce. |
| `GET /funds/lock/nonce/{user}` | Current createLock nonce. |
| `GET /funds/modify-lock/nonce/{user}` | Current modifyLock nonce. |
| `GET /funds/transfer-locked/nonce/{service}` | Current transferFromLock nonce. |

## Auth Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /auth/domain` | Configured SIWE domain. |
| `GET /auth/nonce?address=…` | Single-use SIWE nonce. Browser-origin-checked. Rate-limited. |
| `POST /auth/login` | SIWE login → `siwe_token` + JWT pair. Browser-origin-checked. Rate-limited. |
| `POST /auth/jwt/refresh` | Rotate the refresh token; returns a fresh access/refresh pair. |
| `POST /auth/jwt/siwe-token` (Bearer) | Exchange a JWT access token for an encrypted SIWE token for on-chain private reads. |
| `POST /auth/jwt/logout` (Bearer) | Revoke one or all refresh tokens for the current user. |
| `GET /auth/jwt/jwks.json` | JWKS document for verifying issued JWTs. |
| `GET /auth/jwt/me` (Bearer) | Returns the authenticated address. Useful for client-side identity checks. |
| `GET /auth/authorize` | HTML auth page for cross-domain sign-in. Query params: `client_id`, `redirect_uri`, `code_challenge`, `code_challenge_method=S256`, `chain_id`, `state`, `response_mode`. |
| `POST /auth/authorize` | Verify SIWE on the Flexvaults origin and mint a short-lived authorization code. Browser-origin-checked. Rate-limited. |
| `POST /auth/token` | Exchange an authorization code + PKCE verifier for `access_token`, `id_token`, `refresh_token`. |

## Status Code Semantics

| Code | Meaning |
|---|---|
| `200 OK` | Synchronous success — the response body has the result. |
| `202 Accepted` | Work continues in the background. Currently only `POST /deposits/check` when a sweep is in flight. Poll the corresponding status endpoint. |
| `400 Bad Request` | Pydantic validation, malformed hex/address, business rule violation (e.g. modify-lock no-op). |
| `401 Unauthorized` | Missing or invalid auth — bad SIWE token, expired JWT, both auth headers sent at once, or a MoonPay webhook signature that fails verification. |
| `403 Forbidden` | On-ramp record belongs to a different user or deposit address. |
| `404 Not Found` | Status check for an unknown deposit. |
| `409 Conflict` | Orphan on-ramp record without a recorded owner or wallet cannot be claimed. |
| `413 Payload Too Large` | MoonPay webhook body exceeds the 1 MiB cap. |
| `422 Unprocessable Entity` | Contract revert (transaction submitted but the chain rejected it). The response `detail` carries the revert reason when available. |
| `429 Too Many Requests` | Auth rate-limiter tripped. Honour the `Retry-After` header. |
| `500 Internal Server Error` | Unexpected failure in the service layer. Errors are logged with stack traces — file an issue with the request id. |
| `503 Service Unavailable` | MoonPay on-ramp is not configured — URL-signing or webhook keys are missing. |

## Schemas

For exact request/response shapes — including field types, constraints, and enum values — read [`docs/openapi.json`](openapi.json) (committed snapshot) or browse `/docs` against a running service. The Pydantic source models live in `src/models/accounting.py` and `src/models/authorize.py`.
