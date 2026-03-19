# Accounting Module API Reference

**Base URL:** `/v1/accounting`

Requests and responses are JSON. Hex strings must include the `0x` prefix. Signatures follow the EIP-712 payloads defined in the contracts.

## Authentication (private reads)

Balances and lock details are private.

Supported authentication modes:
1. **Direct SIWE on the Flexvaults auth origin**
   - `GET /auth/domain` to fetch the configured SIWE domain.
   - `GET /auth/nonce?address=<user>` to fetch a single-use nonce.
   - Sign a SIWE message on the current wallet chain using that domain and nonce.
   - `POST /auth/login` with `{ siwe_message, signature }`.
   - Use the returned `jwt_access_token` as `Authorization: Bearer <token>` on private read endpoints.
   - The returned `siwe_token` can also be passed via `X-SIWE-Token` for direct token-based private reads.
   - Browser requests to `/auth/nonce` and `/auth/login` must originate from the configured Flexvaults auth origin. Non-browser clients may omit the `Origin` header.
2. **Cross-domain / third-party apps**
   - Redirect or open a popup to `GET /auth/authorize` with `client_id`, exact `redirect_uri`, `code_challenge`, `code_challenge_method=S256`, `state`, and `response_mode`.
   - The Flexvaults auth page performs SIWE on the canonical Flexvaults domain and returns a short-lived authorization code.
   - Exchange that code at `POST /auth/token` with `grant_type=authorization_code`, the code, and the PKCE verifier to receive:
     - `access_token` for Flexvaults API calls
     - `id_token` for third-party backend identity verification
     - `refresh_token` for Flexvaults access-token rotation
   - Registered `redirect_uri` values must use `https`, except `http://localhost` / loopback development callbacks.

## Endpoints

### POST `/quote/deposit`
Generate deposit instructions and transaction data for a user/token/amount combination.
- **Request body**
  - `user_address` (string, required) – EVM address of the user.
  - `token_id` (string, required) – Bytes32 token identifier (hex).
  - `amount` (integer, required) – Amount to deposit in base units (e.g., wei for ETH).
- **Response body**
  - `user_address` (string) – Checksummed address.
  - `token_id` (string) – Normalised bytes32 token identifier.
  - `amount` (integer) – Amount to deposit.
  - `deposit_address` (string) – ROFL-controlled destination address.
  - `transaction` (object) – Transaction data to execute:
    - `to` (string) – Destination address.
    - `value` (string) – Value in wei (hex).
    - `data` (string) – Transaction data (hex).
    - `chain_id` (integer) – Chain ID for the transaction.
  - `instructions` (string) – Deposit guidance for clients.

### POST `/deposits`
Submit an EVM deposit inclusion transaction (automatically detects native/ERC20 based on token_id). Uses the `creditEVMDeposit` contract function.
- **Request body**
  - `user_address` (string, required) – Depositor address.
  - `token_id` (string, required) – Bytes32 token identifier (hex).
  - `evm_transaction_data` (string, required) – RLP-encoded EVM transaction payload.
  - `rlp_block_header` (string, optional) – RLP-encoded block header.
  - `transaction_index_rlp` (string, optional) – RLP-encoded transaction index.
  - `transaction_proof_stack` (string, optional) – Merkle proof stack.
- **Response body**
  - `submission_id` (string) – ROFL submission identifier.
  - `status` (string) – Submission status, e.g. `submitted`.
- **Note:** This endpoint is specifically for EVM-compatible chains. Future endpoints for other blockchains (Solana, Sui, etc.) will be added separately.

### POST `/funds/lock`
Lock user funds for a service using the user's EIP-712 signature.
- **Request body**
  - `user_address` (string, required).
  - `service_address` (string, required).
  - `token_id` (string, required).
  - `amount` (integer, required).
  - `expiry` (integer, required) – Unix timestamp.
  - `signature` (string, required) – User EIP-712 `Lock` signature.
- **Response body**
  - `submission_id` (string).
  - `status` (string).
  - `detail` (string, optional).

### POST `/funds/modify-lock`
Modify an existing lock by adding funds and/or extending the expiry.
- **Request body**
  - `user_address` (string, required) – Owner of the lock.
  - `lock_id` (integer, required) – ID of the lock to modify.
  - `amount` (integer, required) – Amount to add in base units (use 0 to only extend expiry).
  - `new_expiry` (integer, required) – New expiry timestamp (must be >= current expiry).
  - `signature` (string, required) – User EIP-712 `ModifyLock` signature.
- **Response body**
  - `submission_id` (string).
  - `status` (string).
  - `detail` (string, optional).
- **Note:** At least one of `amount > 0` or `new_expiry > current_expiry` must be true; otherwise the call is rejected as a no-op.

### GET `/funds/locked/{user_address}`
Get locked funds for a user, optionally filtered by `service_address`.
- **Headers**
  - `Authorization: Bearer <access_token>` (preferred) – JWT access token from `POST /auth/login` or `POST /auth/token`.
  - `X-SIWE-Token` (direct SIWE token flow) – Encrypted SIWE token from `POST /auth/login`.
- **Query parameters**
  - `service_address` (string, optional) – Filter locks by service.
- **Note**
  - `service_address` is a response filter on user-authenticated reads. It does not grant service-only access. Service backends should use contract-level `getServiceLocks(...)` with Sapphire authenticated view calls.

### POST `/funds/transfer`
Transfer balances between users with the originator's EIP-712 signature.
- **Request body**
  - `user_address` (string, required).
  - `to_address` (string, required).
  - `token_id` (string, required).
  - `amount` (integer, required).
  - `signature` (string, required) – User EIP-712 `Transfer` signature.
- **Response body** – same structure as `/funds/lock`.

### POST `/funds/transfer-locked`
Consume or release locked funds using the service's EIP-712 signature.
- **Request body**
  - `user_address` (string, required) – Owner of the lock.
  - `lock_id` (integer, required).
  - `to_address` (string, required).
  - `amount` (integer, required).
  - `signature` (string, required) – Service EIP-712 `TransferLocked` signature.
- **Response body** – same structure as `/funds/lock`.

### POST `/funds/unlock`
Unlock a single expired lock without a signature.
- **Request body**
  - `user_address` (string, required).
  - `lock_id` (integer, required).
- **Response body** – same structure as `/funds/lock`.

### POST `/funds/unlock-all-expired`
Unlock all expired locks for a user in a single transaction.
- **Request body**
  - `user_address` (string, required).
- **Response body**
  - `submission_id` (string) – ROFL submission identifier.
  - `status` (string) – Submission status.
  - `detail` (string, optional).

### GET `/funds/expired/{user_address}`
Get all expired locks for a user.
- **Headers**
  - `Authorization: Bearer <access_token>` (preferred) – JWT access token from `POST /auth/login` or `POST /auth/token`.
  - `X-SIWE-Token` (direct SIWE token flow) – Encrypted SIWE token from `POST /auth/login`.
- **Response body**
  - `user_address` (string) – Checksummed user address.
  - `expired_locks` (array) – List of expired lock records:
    - `lock_id` (integer)
    - `user_address` (string)
    - `service_address` (string)
    - `token_id` (string)
    - `amount` (integer)
    - `expiry` (integer)
    - `is_expired` (boolean, always `true`)

### GET `/withdraw/nonce/{user_address}`
Get the current withdrawal nonce for a user. Use this nonce in `POST /withdraw` and include it in the EIP-712 `Withdraw` signature.
- **Path parameters**
  - `user_address` (string, required) – User's EVM address.
- **Response body**
  - `user_address` (string) – Checksummed user address.
  - `nonce` (integer) – Current withdrawal nonce.

### POST `/withdraw`
Request a withdrawal based on the user's EIP-712 signature. This schedules the withdrawal for resolution in a later block (simulation attack protection). The user's balance is debited immediately and a nonce is reserved for the withdrawal transaction.
- **Request body**
  - `user_address` (string, required).
  - `token_id` (string, required).
  - `amount` (integer, required).
  - `nonce` (integer, required) – Current withdrawal nonce for the user (use `GET /withdraw/nonce/{user_address}`).
  - `signature` (string, required) – User EIP-712 `Withdraw` signature.
- **Response body**
  - `submission_id` (string) – ROFL submission identifier.
  - `status` (string) – Submission status, e.g. `submitted`.
  - `detail` (string, optional) – Metadata such as `chain_id` and `token_address`.
- **Note:** Withdrawals are automatically resolved by the backend after the required block delay. Frontend clients only need to call this endpoint once. Use `/withdraw/pending/{user_address}` to check withdrawal status.

### GET `/withdraw/pending/{user_address}`
Get all pending (unresolved) withdrawal requests for a user. Use this to display withdrawal status in the UI.
- **Path parameters**
  - `user_address` (string, required) – User's EVM address.
- **Response body**
  - `user_address` (string) – Checksummed user address.
  - `pending_withdrawals` (array) – List of pending withdrawal requests:
    - `index` (integer) – Withdrawal request index.
    - `user_address` (string) – Address of the user who requested the withdrawal.
    - `amount` (string) – Amount requested in base units.
    - `block_number` (integer) – Block number when the withdrawal was requested.
    - `token_id` (string) – Token identifier.
    - `resolved` (boolean) – Always `false` for pending withdrawals.
    - `tx_identifier` (string) – Transaction identifier (nonce) reserved for this withdrawal.

### GET `/withdraw/{index}`
Get information about a specific withdrawal request.
- **Path parameters**
  - `index` (integer, required) – Index of the withdrawal request.
- **Response body**
  - `index` (integer) – Withdrawal request index.
  - `user_address` (string) – Address of the user who requested the withdrawal.
  - `amount` (string) – Amount requested in base units.
  - `block_number` (integer) – Block number when the withdrawal was requested.
  - `token_id` (string) – Token identifier.
  - `resolved` (boolean) – Whether the withdrawal has been resolved.
  - `tx_identifier` (string) – Transaction identifier (nonce) reserved for this withdrawal.

### GET `/balances/{user_address}/{token_id}`
Get the user's balance for a specific token.
- **Headers**
  - `Authorization: Bearer <access_token>` (preferred) – JWT access token from `POST /auth/login` or `POST /auth/token`.
  - `X-SIWE-Token` (direct SIWE token flow) – Encrypted SIWE token from `POST /auth/login`.

### POST `/balances/batch`
Get balances for multiple tokens for a user.
- **Headers**
  - `Authorization: Bearer <access_token>` (preferred) – JWT access token from `POST /auth/login` or `POST /auth/token`.
  - `X-SIWE-Token` (direct SIWE token flow) – Encrypted SIWE token from `POST /auth/login`.
- **Request body**
  - `user_address` (string, required)
  - `token_ids` (array[string], required) – Bytes32 token identifiers (hex), max 100 items

### GET `/funds/locked/total/{user_address}/{token_id}`
Get total locked balance for a specific token across all locks.
- **Headers**
  - `Authorization: Bearer <access_token>` (preferred) – JWT access token from `POST /auth/login` or `POST /auth/token`.
  - `X-SIWE-Token` (direct SIWE token flow) – Encrypted SIWE token from `POST /auth/login`.

### GET `/auth/domain`
Get the configured SIWE domain that clients must use in the SIWE message.

### GET `/auth/nonce`
Get a single-use nonce for SIWE login.
- **Query parameters**
  - `address` (string, required) – User's EVM address.
- **Response body**
  - `address` (string) – Checksummed Ethereum address associated with the nonce.
  - `nonce` (string)
  - `expires_in` (integer) – Nonce TTL in seconds.
- **Browser restriction**
  - Browser requests must originate from the configured Flexvaults auth origin.

### POST `/auth/login`
Perform SIWE login, mint an encrypted Sapphire auth token, and issue JWT credentials.
- **Request body**
  - `siwe_message` (string, required)
  - `signature` (string, required) – `signMessage` signature for the SIWE message (hex)
- **Browser restriction**
  - Browser requests must originate from the configured Flexvaults auth origin.
- **Response body**
  - `siwe_token` (string) – Encrypted SIWE token (hex) for direct `X-SIWE-Token` private reads.
  - `jwt_access_token` (string) – JWT access token for `Authorization: Bearer`.
  - `jwt_refresh_token` (string) – Refresh token for `POST /auth/jwt/refresh`.
  - `address` (string) – Authenticated Ethereum address.
  - `jwt_expires_in` (integer) – Access-token TTL in seconds.
  - `jwt_refresh_expires_in` (integer) – Refresh-token TTL in seconds.

### POST `/auth/jwt/refresh`
Rotate a refresh token and obtain a fresh access token pair.
- **Request body**
  - `refresh_token` (string, required)
- **Response body**
  - `token` (string) – New JWT access token.
  - `refresh_token` (string) – New refresh token.
  - `expires_in` (integer)
  - `refresh_expires_in` (integer)

### POST `/auth/jwt/logout`
Revoke one refresh token or all refresh tokens for the current JWT-authenticated user.
- **Headers**
  - `Authorization: Bearer <access_token>` (required)
- **Request body**
  - `refresh_token` (string, optional) – Specific refresh token to revoke.
  - `revoke_all` (boolean, optional) – Revoke all refresh tokens belonging to the caller.

### GET `/auth/jwt/jwks.json`
Return the public JWKS document used to verify JWTs issued by this service.

### GET `/auth/jwt/me`
Return the authenticated Ethereum address for the provided access token.
- **Headers**
  - `Authorization: Bearer <access_token>` (required)

### GET `/auth/authorize`
Serve the Flexvaults authorization page used for cross-domain sign-in.
- **Query parameters**
  - `client_id` (string, required)
  - `redirect_uri` (string, required) – Must exactly match a registered URI for the client.
  - `code_challenge` (string, required)
  - `code_challenge_method` (string, required) – Only `S256` is supported.
  - `state` (string, required)
  - `response_mode` (string, optional) – `web_message` or `redirect`. Defaults to `web_message`.

### POST `/auth/authorize`
Verify SIWE on the Flexvaults auth origin and mint a short-lived authorization code.
- **Request body**
  - `siwe_message` (string, required)
  - `signature` (string, required)
  - `client_id` (string, required)
  - `redirect_uri` (string, required) – Must exactly match the registered URI.
  - `code_challenge` (string, required)
  - `code_challenge_method` (string, required) – Only `S256` is supported.
- **Browser restriction**
  - Browser requests must originate from the configured Flexvaults auth origin.
- **Response body**
  - `code` (string) – Single-use authorization code.

### POST `/auth/token`
Exchange an authorization code and PKCE verifier for JWT credentials.
- **Request body**
  - `grant_type` (string, required) – Must be `authorization_code`.
  - `code` (string, required)
  - `code_verifier` (string, required)
  - `client_id` (string, required)
  - `redirect_uri` (string, required) – Must exactly match the authorization request.
- **Response body**
  - `access_token` (string) – JWT access token for Flexvaults API calls only.
  - `id_token` (string) – Client-scoped identity token for third-party backend verification. The audience comes from the configured auth client audience and defaults to `client_id`.
  - `refresh_token` (string) – Refresh token for rotating the Flexvaults API access token.
  - `token_type` (string) – `Bearer`
  - `expires_in` (integer)
  - `refresh_expires_in` (integer)
  - `address` (string)

### GET `/tokens/{token_id}`
Get information about a registered token.
- **Path parameters**
  - `token_id` (string, required) – Token identifier (bytes32 hex).
- **Response body**
  - `token_id` (string) – Token identifier.
  - `token_type` (integer) – Token type (0 = NativeEVM, 1 = ERC20).
  - `token_type_name` (string) – Human-readable token type.
  - `data` (string) – Raw token data (hex).
  - `chain_id` (integer, optional) – Chain ID for the token.
  - `chain_name` (string, optional) – Human-readable chain name.
  - `token_address` (string, optional) – ERC20 contract address (only for ERC20 tokens).
