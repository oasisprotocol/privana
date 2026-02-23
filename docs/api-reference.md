# Accounting Module API Reference

**Base URL:** `/v1/accounting`

Requests and responses are JSON. Hex strings must include the `0x` prefix. Signatures follow the EIP-712 payloads defined in the contracts.

## Authentication (private reads)

Balances and lock details are private. To read them via this API, clients must authenticate using SIWE and include the returned token in the `X-SIWE-Token` header.

High-level flow:
1. `GET /auth/domain` to fetch the SIWE domain bound to the contract.
2. Create a SIWE message using that domain and the Sapphire chain ID (e.g., 23295 for Sapphire Testnet).
3. Sign the message with the user's wallet (`signMessage`).
4. `POST /auth/login` with `{ siwe_message, signature }` to receive a `token`.
5. Include `X-SIWE-Token: <token>` on private read endpoints.

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
  - `X-SIWE-Token` (string, required) – SIWE auth token from `POST /auth/login`.
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
  - `X-SIWE-Token` (string, required) – SIWE auth token from `POST /auth/login`.
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
  - `X-SIWE-Token` (string, required) – SIWE auth token from `POST /auth/login`.

### POST `/balances/batch`
Get balances for multiple tokens for a user.
- **Headers**
  - `X-SIWE-Token` (string, required) – SIWE auth token from `POST /auth/login`.
- **Request body**
  - `user_address` (string, required)
  - `token_ids` (array[string], required) – Bytes32 token identifiers (hex), max 100 items

### GET `/funds/locked/total/{user_address}/{token_id}`
Get total locked balance for a specific token across all locks.
- **Headers**
  - `X-SIWE-Token` (string, required) – SIWE auth token from `POST /auth/login`.

### GET `/auth/domain`
Get the SIWE domain bound to the contract.

### POST `/auth/login`
Perform SIWE login and receive an opaque token for private reads.
- **Request body**
  - `siwe_message` (string, required)
  - `signature` (string, required) – `signMessage` signature for the SIWE message (hex)
- **Response body**
  - `token` (string) – SIWE auth token (hex). Include in `X-SIWE-Token`.

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
