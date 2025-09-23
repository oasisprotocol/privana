# Accounting Module API Reference

**Base URL:** `/v1/accounting`

Requests and responses are JSON. Hex strings must include the `0x` prefix. Signatures follow the EIP-712 payloads defined in the contracts.

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
Submit a deposit inclusion transaction (automatically detects native/ERC20 based on token_id).
- **Request body**
  - `user_address` (string, required) – Depositor address.
  - `token_id` (string, required) – Bytes32 token identifier (hex).
  - `evm_transaction_data` (string, required) – RLP-encoded transaction payload.
  - `rlp_block_header` (string, optional) – RLP-encoded block header.
  - `transaction_index_rlp` (string, optional) – RLP-encoded transaction index.
  - `transaction_proof_stack` (string, optional) – Merkle proof stack.
- **Response body**
  - `submission_id` (string) – ROFL submission identifier.
  - `status` (string) – Submission status, e.g. `submitted`.

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
  - `expiry` (integer, required).
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
Unlock an expired lock without a signature.
- **Request body**
  - `user_address` (string, required).
  - `lock_index` (integer, required).
- **Response body** – same structure as `/funds/lock`.

### POST `/withdraw`
Initiate a withdrawal based on the user's EIP-712 signature. The service verifies the signature, generates the withdrawal transaction via the contract, and relays it to the chain RPC mapped to the token.
- **Request body**
  - `user_address` (string, required).
  - `token_id` (string, required).
  - `amount` (integer, required).
  - `signature` (string, required) – User EIP-712 `Withdraw` signature.
- **Response body**
  - `submission_id` (string) – Hash of the relayed transaction.
  - `status` (string) – Typically `sent` when broadcast succeeds.
  - `detail` (string, optional) – Metadata such as `chain_id` and `token_address`.

### GET `/balances/{user_address}/{token_id}`
Prototype endpoint that returns placeholder balance data.
- **Response body**
  - `user_address` (string) – Checksummed address.
  - `token_id` (string).
  - `balance` (string) – Currently always `"0"`.
  - `token_symbol` (string) – Token symbol.
  - `chain_id` (string) – Default Sapphire chain ID.
