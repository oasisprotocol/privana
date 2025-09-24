# Accounting Module API Reference

**Base URL:** `/v1/accounting`

Requests and responses are JSON. Hex strings must include the `0x` prefix. Signatures follow the EIP-712 payloads defined in the contracts.

## Endpoints

### POST `/quotes/deposit`
Generate deposit instructions for a user/token pair.
- **Request body**
  - `user_address` (string, required) – EVM address of the user.
  - `token_id` (string, required) – Bytes32 token identifier (hex).
- **Response body**
  - `user_address` (string) – Checksummed address.
  - `token_id` (string) – Normalised bytes32 token identifier.
  - `deposit_address` (string) – Sapphire-controlled destination address.
  - `chain_id` (integer) – Deposit chain ID.
  - `chain_name` (string) – Human-readable chain name.
  - `token_symbol` (string) – Token ticker.
  - `instructions` (string) – Deposit guidance for clients.

### POST `/deposits/native`
Submit a native-token deposit inclusion transaction.
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

### POST `/deposits/erc20`
Submit an ERC-20 deposit inclusion transaction (same payload as native).
- **Request body** – identical to `/deposits/native`.
- **Response body** – identical to `/deposits/native`.

### POST `/funds/lock`
Lock user funds for a service using the user’s EIP-712 signature.
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

### POST `/funds/transfer`
Transfer balances between users with the originator’s EIP-712 signature.
- **Request body**
  - `user_address` (string, required).
  - `to_address` (string, required).
  - `token_id` (string, required).
  - `amount` (integer, required).
  - `expiry` (integer, required).
  - `signature` (string, required) – User EIP-712 `Transfer` signature.
- **Response body** – same structure as `/funds/lock`.

### POST `/funds/transfer-locked`
Consume or release locked funds using the service’s EIP-712 signature.
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

### POST `/withdrawals`
Initiate a withdrawal based on the user’s EIP-712 signature. The service verifies the signature, generates the withdrawal transaction via the contract, and relays it to the chain RPC mapped to the token.
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
  - `token_symbol` (string) – Stubbed symbol value.
  - `chain_id` (string) – Default Sapphire chain ID.
