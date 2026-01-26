# Accounting Module API Reference

**Base URL:** `/v1/accounting`

Requests and responses are JSON. Hex strings must include the `0x` prefix. Signatures follow the EIP-712 payloads defined in the contracts.

## Endpoints

### POST `/deposits/quote`
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
  - `lock_index` (integer, required) – Index of the lock to modify.
  - `amount` (integer, required) – Amount to add in base units (use 0 to only extend expiry).
  - `new_expiry` (integer, required) – New expiry timestamp (must be >= current expiry).
  - `signature` (string, required) – User EIP-712 `ModifyLock` signature.
- **Response body**
  - `submission_id` (string).
  - `status` (string).
  - `detail` (string, optional).
- **Note:** At least one of `amount > 0` or `new_expiry > current_expiry` must be true; otherwise the call is rejected as a no-op.

### GET `/funds/locked/{user_address}`
Get locked funds for a user, optionally filtered by service address.
- **Query parameters**
  - `service_address` (string, optional) – Filter locks by service address.
- **Response body**
  - `user_address` (string) – Checksummed user address.
  - `service_address` (string, optional) – Service address filter if provided.
  - `locks` (array) – List of lock information:
    - `lock_index` (integer) – Index of the lock.
    - `user_address` (string) – Owner of the lock.
    - `service_address` (string) – Service that locked the funds.
    - `token_id` (string) – Token identifier.
    - `amount` (integer) – Locked amount.
    - `expiry` (integer) – Unix timestamp when lock expires.
    - `is_expired` (boolean) – Whether the lock has expired.
  - `total_locked` (integer) – Total amount locked across all locks.

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
  - `lock_index` (integer, required).
  - `to_address` (string, required).
  - `amount` (integer, required).
  - `signature` (string, required) – Service EIP-712 `TransferLocked` signature.
- **Response body** – same structure as `/funds/lock`.

### POST `/funds/unlock`
Unlock a single expired lock without a signature.
- **Request body**
  - `user_address` (string, required).
  - `lock_index` (integer, required).
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
- **Response body**
  - `user_address` (string) – Checksummed user address.
  - `expired_locks` (array) – List of expired lock information (same structure as locks in `/funds/locked`).

### POST `/withdraw`
Request a withdrawal based on the user's EIP-712 signature. This schedules the withdrawal for resolution in a later block (simulation attack protection). The user's balance is debited immediately and a nonce is reserved for the withdrawal transaction.
- **Request body**
  - `user_address` (string, required).
  - `token_id` (string, required).
  - `amount` (integer, required).
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
Get the user's balance for a specific token from the contract.
- **Response body**
  - `user_address` (string) – Checksummed address.
  - `token_id` (string) – Token identifier.
  - `balance` (string) – User's balance in base units (wei for ETH).
  - `token_symbol` (string) – Token symbol.
  - `chain_id` (string) – Chain ID where the token originates.

### POST `/balances/batch`
Get balances for multiple tokens for a user in a single call.
- **Request body**
  - `user_address` (string, required).
  - `token_ids` (array of strings, required) – List of token identifiers.
- **Response body**
  - `user_address` (string) – Checksummed address.
  - `balances` (array) – List of token balance information:
    - `token_id` (string) – Token identifier.
    - `balance` (string) – Balance in base units.
    - `token_symbol` (string) – Token symbol.
    - `chain_id` (string) – Chain ID where the token originates.

### GET `/funds/locked/total/{user_address}/{token_id}`
Get total locked balance for a specific token across all locks.
- **Path parameters**
  - `user_address` (string, required) – User's EVM address.
  - `token_id` (string, required) – Token identifier.
- **Response body**
  - `user_address` (string) – Checksummed address.
  - `token_id` (string) – Token identifier.
  - `total_locked` (string) – Total locked amount in base units.

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
