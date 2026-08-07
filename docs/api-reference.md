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
- **Hex strings** include the `0x` prefix and are normalized to lowercase.
- **Amounts** are integers in the token's base units (wei for ETH, smallest unit for ERC-20). Strings are accepted; scientific notation is parsed with `Decimal` to preserve precision.
- **Signatures** are EIP-712 typed-data signatures. User-signed accounting operations are defined in `solidity/contracts/EIP712SignatureVerifier.sol`.
- **Nonces** are per-operation. User-signed operations key nonces by recovered signer; service-signed lock operations key nonces by service. Always fetch the current nonce immediately before signing.
- **Status codes** — most submission endpoints return `200 OK` on synchronous success, `202 Accepted` when work continues in the background (currently only `POST /deposits/check`), `400` for semantic/business validation failures, `401` for missing/invalid auth, `422` for request-model validation or contract reverts, `429` for rate-limit, and `500` for unexpected internal failures.

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

### 1. Direct SIWE (first-party Privana origin)

```
GET  /auth/domain                       → which SIWE domain to sign
GET  /auth/nonce?address=<user>         → single-use SIWE nonce
POST /auth/login {siwe_message, sig}    → siwe_token + JWT pair
```

The login response returns:
- `siwe_token` — encrypted Sapphire AuthToken; pass via `X-SIWE-Token` header for direct on-chain confidential reads.
- `jwt_access_token` / `jwt_refresh_token` — pass `Authorization: Bearer <jwt_access_token>` for normal API auth.

Browser requests to `/auth/nonce` and `/auth/login` are **origin-checked** — they must come from a configured Privana SIWE origin. Non-browser clients (no `Origin` header) are accepted.
Backends that only receive a JWT from hosted auth can exchange that JWT for a private-read token with `POST /auth/jwt/siwe-token`.

### 2. OAuth-style cross-domain (third-party apps)

For apps on other origins, use the hosted authorization page with PKCE:

```
GET  /auth/authorize?client_id=…&redirect_uri=…&code_challenge=…&state=…&chain_id=…
                                              → HTML page; user signs SIWE on Privana origin
POST /auth/authorize {siwe_message, sig, …}   → short-lived `code`
POST /auth/token {grant_type=authorization_code, code, code_verifier, …}
                                              → access_token + id_token + refresh_token
```

- `code_challenge_method` must be `S256`.
- `redirect_uri` must exactly match a value registered for the client. `http://localhost` and loopback callbacks are allowed; everything else must be `https`.
- `id_token` is client-scoped (audience = configured client audience or `client_id`) — use it for third-party backend identity verification, not for Privana API calls.

### Endpoint matrix

| Endpoint | Auth | Auth header |
|---|---|---|
| `POST /deposits/address` | required | `Authorization: Bearer …` or `X-SIWE-Token` |
| `POST /deposits/check` | required | same |
| `GET /deposits/status/{id}` | required | same |
| `GET /deposits/pending` | required | same |
| `POST /funds/withdraw-from-lock` | required | same |
| `GET /balances/{token_id}` | required | same |
| `POST /balances/batch` | required | same |
| `GET /funds/locked` | required | same |
| `GET /funds/locked/total/{token_id}` | required | same |
| `GET /funds/expired` | required | same |
| `GET /history` | required | same |
| `POST /onramp/intent` | required | same |
| `POST /onramp/sign-url` | required | same |
| `POST /onramp/session` | required | same |
| `GET /onramp/pending` | required | same |
| `POST /onramp/{transaction_id}` | required | same |
| `POST /onramp/moonpay/webhook` | MoonPay webhook signature | `Moonpay-Signature-V2` |
| `POST /onramp/transak/webhook` | Transak JWT | JWT in body `data` |
| `POST /auth/jwt/siwe-token` | required | `Authorization: Bearer …` |
| `POST /auth/jwt/logout`, `GET /auth/jwt/me` | required | `Authorization: Bearer …` |
| Everything else | none (signature-gated where applicable) | — |

`Authorization` and `X-SIWE-Token` are mutually exclusive — sending both yields `400`.

### History

`GET /history` returns one page of the authenticated user's on-chain activity history. Entries within a page are ordered oldest to newest. Query parameters are `offset` (default `-1`) and `limit` (default `50`, max `100`). Non-negative offsets are 0-indexed page numbers from the oldest entries; negative offsets select pages from the end (`-1` is the latest page, `-2` the previous page).

| Field | Description | History kind type(s) |
| --- | --- | --- |
| `kind` | Entry type. Known values are `deposit`, `withdraw`, `createLock`, `transferFromLockOut`, `transferFromLockIn`, `transferBalanceOut`, `transferBalanceIn`, `modifyLock`, and `unlockLock`; undecoded entries return `unknown`. | all |
| `timestamp` | Entry timestamp. | all |
| `token_id` | Token identifier. | all decoded kinds |
| `amount` | Token amount as a decimal string. For `modifyLock`, this is the additional locked amount and can be `0` for expiry-only changes. For `unlockLock`, this is the amount returned to available balance. | all decoded kinds |
| `chain_id` | Source chain for `token_id`, when known. | all decoded kinds |
| `deposit_id` | Deposit identifier. | `deposit` |
| `counterparty` | Address payload for non-deposit entries: withdrawal destination for `withdraw`, lock service for `createLock`/`modifyLock`/`unlockLock`, the recipient for `transferFromLockOut`/`transferBalanceOut`, and the sender for `transferFromLockIn`/`transferBalanceIn`. | all decoded kinds except `deposit` |

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

For **external-wallet deposits** the client does not know the tx hash (no
wallet callback, no webhook). `GET /deposits/pending?chain_id=…` scans
finalized source-chain ERC20 Transfer logs to the caller's deposit address and
returns uncredited candidates carrying exactly the fields `POST
/deposits/check` consumes (`chain_id`, `tx_hash`, `amount`, `log_index`,
`version`). Candidates with a sweep already in flight come back as
`status="processing"` with their `deposit_id` — poll `GET
/deposits/status/{deposit_id}` instead of re-submitting. The scan window
defaults to ~1 hour and is clamped to ~24 hours via `lookback_blocks`
(rounded up to full scan chunks); an optional `token_address` narrows the
scan to one registered token. Results are cached for up to 30 seconds, so a
just-submitted candidate may briefly reappear, possibly still marked
`discovered` — re-submitting it to `/deposits/check` is harmless (the
endpoint is idempotent). `scanned_from_block` reports the oldest block
actually examined: when a scan stops early at its internal candidate caps it
is higher than the requested window bottom, so treat only
`[scanned_from_block, scanned_to_block]` as covered. In the extreme case
where the caps trip inside the newest block, `scanned_from_block` is
`scanned_to_block + 1`: the interval is empty and no block is fully covered. Native transfers
emit no logs and are not discoverable — submit their tx hash to
`/deposits/check` directly.

Behavior notes:

- `POST /deposits/check` is **idempotent**. If the same `(chain_id, tx_hash, log_index)` is submitted twice, the second call short-circuits — returning `200` with `status="credited"` once the credit has landed on Sapphire, or `202` with `status="pending"` while the sweep is still running.
- Sweeps run as background tasks. The processor returns within ~2-3s. Don't block on the response — poll `GET /deposits/status/{deposit_id}`.
- The status endpoint first consults an in-memory record (sweep in progress / failed) and then falls through to an on-chain `isDepositProcessed` check. Records survive restarts via JSON persistence (see `src/README.md`).
- The `min_deposit` field of the address response is a per-chain map — clients should use it to gate the UI.

## On-Ramp Flow (MoonPay or Transak)

The fiat on-ramp reuses the existing deposit path. The configured provider delivers an ERC-20 to the server-derived per-user deposit address; the normal verify → sweep → credit pipeline is the only path that can credit a balance. Provider orders, amounts, widget events, and webhooks are correlation evidence only.

```
1. POST /onramp/intent             → mint a provider-bound signed Privana intent
2a. MoonPay: POST /onramp/sign-url → validate and sign the widget URL
2b. Transak: POST /onramp/session  → create a five-minute, single-use opaque URL
3. user completes the provider purchase to the locked deposit address
4. provider webhook (optional)     → backend verifies and logs an observability signal
5. GET /onramp/pending             → bounded provider read; optional exact signed-intent
                                     values recover known purchases after a provider switch
6. POST /deposits/check            → verify the matching on-chain transfer, sweep, credit
7. GET /deposits/status/{id}       → poll until status="credited"
```

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /onramp/intent` | required | Mint an intent for the deployment-selected `ONRAMP_PROVIDER`. It pins provider, caller, freshly derived deposit wallet, token, chain, and canonical provider asset. Transak accepts only its one configured, registered asset. |
| `POST /onramp/sign-url` | required | MoonPay-only launch. Validate the allow-listed widget URL and return its HMAC signature. Returns `503` when MoonPay is not selected. |
| `POST /onramp/session` | required | Transak-only launch. Revalidate the unexpired caller-owned intent and registered asset, require one trusted proxy-owned client IP, and return an opaque URL with `expires_at`. Returns `503` when Transak is not selected or fully configured. The browser must open/embed the URL without `noreferrer` or a `no-referrer` policy so Transak receives the approved `Referer`. |
| `GET /onramp/pending` | required | Bootstrap the selected provider by caller/wallet and optionally perform up to 10 exact lookups through each signed intent's encoded provider. Return only completed, strictly admitted records with an on-chain transaction hash. |
| `POST /onramp/{transaction_id}` | required | MoonPay compatibility endpoint. Validate caller-owned signed intent metadata and echo the merged client payload without persistence. |
| `POST /onramp/moonpay/webhook` | MoonPay HMAC (`Moonpay-Signature-V2`) | Verify and log a MoonPay update; never credit or persist provider state. |
| `POST /onramp/transak/webhook` | Transak body JWT | Verify with a cached current or bounded previous Partner Access Token and log a redacted summary; never credit or persist provider state. |

Behavior notes:

- **The deployment chooses the provider.** `ONRAMP_PROVIDER` must be configured explicitly; the shipped environment files select `moonpay` until the controlled Transak rollout. Clients cannot select a provider. Launch endpoints enforce the choice; exact authenticated recovery can still dispatch a known signed intent to its encoded provider.
- **Signed intents bind provider orders to Privana deposits; they do not authorize credit.** The v1 wire format is canonical minified JSON authenticated with HMAC-SHA256. It binds provider, user, deposit wallet, `token_id`, chain, canonical asset, nonce, issue time, and expiry. Generated identifiers are at most 448 characters; the 512-character validation cap is defensive, not a provider limit. New launches reject expired or mismatched intents. Authenticated pending recovery may decode expired intents because it still cannot authorize credit.
- **Recovery is stateless and bounded.** There is no local provider-order database. MoonPay reads by caller or exact external ID. Transak reads exact `partnerOrderId` first through one maximum-31-day window anchored to the signed intent, then bootstraps by the derived wallet using Transak's bounded default dates. Both paths retain BUY/COMPLETED filters, deterministic order, and capped pagination. Only the exact known `partnerOrderId`-not-found response is treated as empty; other upstream errors remain visible.
- **Transak admission is strict.** Every returned order must carry a valid signed `partnerOrderId`, caller-owned deposit wallet, configured token/chain/asset/network, BUY product, `COMPLETED` status, and a 32-byte transaction hash. Provider fiat/crypto amounts are display-only; use the matching transfer log for the `/deposits/check` claim.
- **Partner tokens stay in memory.** Transak token refresh is single-flight and driven by returned `expiresAt`; an authenticated API request gets one compare-and-swap refresh/retry only after an explicit `401`. Webhook verification never mints a token in response to an unauthenticated request. A fresh process without a cached verification token returns `503` for the webhook while polling remains available.
- **Transak session URLs require the approved browser referrer.** Preserve `Referer` when opening or embedding the opaque URL; never use `noreferrer` or `Referrer-Policy: no-referrer`.
- **There is one intent signing-key ring per environment.** At startup, the backend derives a 256-bit HMAC key from ROFL appd using `ONRAMP_INTENT_SIGNING_KEY_ID`. Verification also accepts keys derived from comma-separated `ONRAMP_INTENT_PREVIOUS_SIGNING_KEY_IDS`. Rotate by selecting a new current ID and retaining old IDs for 365 days after their last mint.
- **Configuration fails closed.** Startup fails if any configured intent key cannot be derived from ROFL. Invalid Transak configuration disables only its endpoints with `503`. The provider flip additionally requires registered token configuration and a proxy-owned original-client-IP header whose browser-supplied value is overwritten.
- **Sensitive launch material is not logged.** Application access logs redact exact-recovery query values and signed compatibility-route IDs. Any upstream proxy/access-log layer must apply the same policy. Widget URLs, provider tokens, webhook JWTs, and full provider bodies are never logged.
- **Only the deposit path credits.** `/deposits/check` and `/deposits/status/{deposit_id}` retain the existing on-chain verification, idempotency, sweep, and ROFL-gated credit behavior.

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
| `POST /auth/authorize` | Verify SIWE on the Privana origin and mint a short-lived authorization code. Browser-origin-checked. Rate-limited. |
| `POST /auth/token` | Exchange an authorization code + PKCE verifier for `access_token`, `id_token`, `refresh_token`. |

## Status Code Semantics

| Code | Meaning |
|---|---|
| `200 OK` | Synchronous success — the response body has the result. |
| `202 Accepted` | Work continues in the background. Currently only `POST /deposits/check` when a sweep is in flight. Poll the corresponding status endpoint. |
| `400 Bad Request` | Semantic validation or business rule violation after the request model has parsed (for example malformed signed intent metadata or a modify-lock no-op). |
| `401 Unauthorized` | Missing or invalid auth — bad SIWE token, expired JWT, both auth headers sent at once, or a provider webhook signature/JWT that fails verification. |
| `403 Forbidden` | On-ramp record belongs to a different user or deposit address. |
| `404 Not Found` | Status check for an unknown deposit. |
| `413 Payload Too Large` | A provider webhook body exceeds the 1 MiB cap. |
| `422 Unprocessable Entity` | FastAPI/Pydantic request-model validation, or a contract revert after a parsed request reaches the chain. Contract-revert responses carry the revert reason when available. |
| `429 Too Many Requests` | An auth/on-ramp rate limit or Transak upstream rate limit tripped. Honour `Retry-After` when present. |
| `502 Bad Gateway` | A configured provider API is unavailable, malformed, or rejects an authenticated server request. |
| `500 Internal Server Error` | Unexpected failure in the service layer. Errors are logged with stack traces — file an issue with the request id. |
| `503 Service Unavailable` | A required integration is unavailable or fail-closed — intent keys, provider configuration, registered Transak asset, cached Transak webhook verification token, or the source-chain RPC used by `GET /deposits/pending`. |

## Schemas

For exact request/response shapes — including field types, constraints, and enum values — read [`docs/openapi.json`](openapi.json) (committed snapshot) or browse `/docs` against a running service. The Pydantic source models live in `src/models/accounting.py` and `src/models/authorize.py`.
