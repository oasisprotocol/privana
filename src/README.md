# Accounting Module — Python Service

Python orchestration layer for the Accounting Module. Runs as a FastAPI app inside a ROFL TEE, verifies cross-chain deposits, manages a sweep state machine for moving funds from per-user deposit addresses to the encumbered wallet, and resolves user withdrawals to broadcastable signed transactions.

## Where to Find What

| Topic | Location |
|------|----------|
| HTTP API endpoints (request/response shapes, auth) | [`docs/api-reference.md`](../docs/api-reference.md) |
| Solidity contracts, deployment, on-chain flows | [`solidity/README.md`](../solidity/README.md) |
| **Service internals — this file** | continue below |

## Module Layout

```
src/
├── main.py                    # FastAPI app + lifespan startup/shutdown
├── api/                       # HTTP route handlers
│   ├── routes.py              #   Deposits, locks, transfers, withdrawals
│   └── auth_routes.py         #   SIWE login, OAuth-style code/token exchange
├── services/                  # Business logic
│   ├── deposit_processor.py   #   Orchestrates verify → sweep → credit
│   ├── deposit_verifier.py    #   RPC-based source-chain verification
│   ├── sweep_engine.py        #   Per-deposit state machine, persistence, recovery
│   ├── withdrawal_processor.py#   Polls Sapphire, resolves, broadcasts
│   ├── accounting_contract.py #   Sapphire client (ROFL or direct-key path)
│   ├── rofl_signer_bootstrap.py # Publish roflSignerAddress at startup
│   ├── l2_fee_estimator.py    #   OP-stack / Arbitrum L1-data-fee estimation
│   └── cache.py               #   Lightweight in-memory caches
├── clients/
│   └── rofl.py                # ROFL appd client (sign tx, fetch keypairs)
├── auth/                      # SIWE + OAuth-style auth machinery
│   ├── siwe_service.py        #   SIWE message verification
│   ├── jwt_service.py         #   JWT issuance/validation
│   ├── auth_token_service.py  #   Encrypted SIWE token via AccountingSiweAuth
│   ├── jwt_keys.py            #   ROFL-derived JWT key manager
│   ├── auth_token_keys.py     #   ROFL-derived AuthToken key manager
│   ├── client_registry.py     #   Allowed OAuth clients
│   ├── code_store.py / token_store.py / pkce.py / rate_limiter.py
│   └── ...
├── config/
│   ├── __init__.py            # Settings dataclass + env loading
│   └── chain_config.py        # Per-chain finality, min-deposit, gas-funding
├── models/                    # Pydantic request/response types
├── crypto/
│   └── deoxysii.py            # Sapphire AuthToken decryption helper
├── abi/                       # Contract ABIs (generated)
├── static/                    # Landing-page assets
└── templates/                 # FastAPI HTML templates (landing, authorize)
```

## Service Flows

### Deposit Flow

Triggered by `POST /deposits/check`. The processor returns within ~2-3 s with `status="pending"` and a `deposit_id`; the actual sweep continues in a background task. Clients poll `GET /deposits/status/{deposit_id}`.

```
Client                       DepositProcessor              SweepEngine                Sapphire
  │                                 │                          │                         │
  │── /deposits/check ─────────────▶│                          │                         │
  │  (chain_id, tx_hash, amount,    │                          │                         │
  │   log_index, version, siwe)     │                          │                         │
  │                                 │                          │                         │
  │                                 │── verify_deposit ───── (source-chain RPC) ─▶       │
  │                                 │   (status=1, finality,   │                         │
  │                                 │    matching Transfer/    │                         │
  │                                 │    tx.value)             │                         │
  │                                 │                          │                         │
  │                                 │── compute_deposit_id ────│                         │
  │                                 │── is_deposit_processed ─────────────────────────▶  │
  │                                 │                          │                         │
  │                                 │── create background ────▶│                         │
  │                                 │   sweep task             │                         │
  │◀── {pending, deposit_id} ───────│                          │                         │
  │                                 │                          │                         │
  │                                 │                          │── sweep_native/erc20    │
  │                                 │                          │   (state machine)       │
  │                                 │                          │── creditDeposit ───────▶│
  │                                 │                          │                         │
  │── /deposits/status/{id} ───────▶│ (returns "credited" once on-chain, else "pending") │
```

Key files: `services/deposit_processor.py`, `services/deposit_verifier.py`, `services/sweep_engine.py`.

### Sweep State Machine

```
            sweep_native              gas-fund            sweep tx mined
              starts                  succeeds            (receipt.status=1)
   (no record) ──────▶ PENDING ────────▶ GAS_FUNDED ────────▶ SWEPT
                          │                                       │
                          │                                       │
                          ├──▶ (native: skip GAS_FUNDED if ───────┤
                          │     deposit-addr balance covers       │
                          │     gas + amount)                     │
                          │                                       ▼
                          │                                   creditDeposit
                          │                                       │
                          │                                       ▼
                          │                                  record deleted
                          │
                          └─ on failure: stay in current state, error stored,
                             recovery loop retries
```

Concurrency:
- One `asyncio.Lock` per `(deposit_address, chain_id)` — concurrent claims for the same address queue.
- One global lock around gas-tank nonce reads (prevents concurrent sweeps from reusing the same nonce on the gas tank).

Persistence: one JSON file per active sweep at `/data/sweep-engine/sweep_<deposit_id>.json` (matches `SweepRecord` dataclass; keyed by the unique deposit_id so same-address deposits never clobber each other). On startup, `resume_incomplete_sweeps()` migrates any legacy `sweep_<address>_<chain_id>.json` files to the deposit_id key and re-drives any `PENDING` / `GAS_FUNDED` records.

Recovery semantics:
- `SWEPT` records → retry `creditDeposit` (idempotent via `DepositAlreadyProcessed` revert).
- `GAS_FUNDED` with a mined sweep tx → promote to `SWEPT`, then credit.
- `PENDING` / `GAS_FUNDED` without a sweep tx → re-run sweep from scratch (no nonce-collision risk because no tx was broadcast).
- `GAS_FUNDED` with an unmined / dropped / reverted sweep tx → logged for manual inspection (the gas-tank nonce may be encumbered).

A periodic recovery loop (`SWEEP_RECOVERY_INTERVAL = 60s`) retries any `SWEPT` records whose credit failed.

Key file: `services/sweep_engine.py`.

### Withdrawal Flow

Two halves: user-driven request (HTTP), then ROFL-driven resolution (background poll loop).

```
Client                       FastAPI                    Sapphire             Destination Chain
  │                            │                          │                         │
  │── /withdraw (EIP-712) ─────▶│                          │                         │
  │                            │── requestWithdrawal ────▶│                         │
  │                            │   (assigns nonce, emits  │                         │
  │                            │    Withdrawal event)     │                         │
  │◀── tx_hash ────────────────│                          │                         │
                               
WithdrawalProcessor (background, ~12s poll)
  │                            │
  │── poll pending withdrawals (per chain, sequential by index)
  │── wait 1-block delay       │
  │── resolveWithdrawal (tx) ─▶│ (marks resolved, emits WithdrawalResolved)
  │── resolveWithdrawal.call() │ → returns Sapphire-signed RLP tx
  │── eth_sendRawTransaction ─────────────────────────────────────────▶│
  │                            │                          │            (broadcast)
```

`WithdrawalProcessor` also runs a periodic catch-up (`_catch_up_missing_broadcasts`) that compares destination-chain on-chain nonces against per-chain high-water marks, re-broadcasting any signed transactions that never landed.

Withdrawals are processed **sequentially per chain** to preserve nonce ordering. A failure on one chain does not block others.

Key file: `services/withdrawal_processor.py`.

### ROFL Signer Bootstrap

At startup (`main.py:99`), after the AuthToken encryption key has been synced, `bootstrap_rofl_signer_address` runs:

1. Derive the ROFL query-signer keypair via `RoflAppdClient.get_keypair(ROFL_QUERY_SIGNER_KEY)`.
2. Read `roflSignerAddress` from the Accounting contract.
3. If they differ, submit `setRoflSignerAddress(...)` (gated by `onlyROFL`).

Idempotent: subsequent starts no-op when the address is already in sync. The published address is what `onlyROFLQuery` view-call functions check `msg.sender` against (see `solidity/README.md` → Security Considerations).

Key file: `services/rofl_signer_bootstrap.py`.

## State & Persistence

| What | Where | Lifecycle |
|------|-------|-----------|
| Sweep records | `/data/sweep-engine/sweep_<deposit_id>.json` (one file per active sweep) | Created on `PENDING`, atomically replaced on each transition, deleted after credit. Survives ROFL restarts. |
| JWT signing key | Derived in-memory from ROFL TEE seed at startup | Re-derived on each start; deterministic per ROFL app. |
| AuthToken encryption key | Derived in-memory from ROFL TEE seed; **also synced to `AccountingSiweAuth` on Sapphire** | At first start, `auth_token_keys.sync_key_to_contract()` writes it on-chain so view-call SIWE token decryption works inside the contract. |
| Withdrawal high-water marks | In-memory only (`WithdrawalProcessor._chain_high_water_mark`) | Rebuilt on restart via the catch-up pass. |

## Configuration

Per-chain settings live in **`src/config/chain_config.py`** as a `ChainConfig` dataclass:

```python
ChainConfig(
    chain_id=84532,
    finality_depth=15,                            # block confirmations required
    min_deposit_native_wei=1_000_000_000_000_000, # 0.001 ETH
    min_deposit_erc20_wei=1_000_000,              # 1 USDC (6 decimals)
    gas_funding_amount_wei=200_000_000_000_000,   # ~65k gas * 3 gwei
    l2_type=L2Type.OP_STACK,                      # for L1-data-fee estimation
)
```

Adding a new chain is a single `CHAIN_CONFIGS` entry — no parallel dicts to keep in sync.

Environment variables: see `.env.example`. Notable ones:

- `SAPPHIRE_RPC_URL`, `ALCHEMY_API_KEY` — chain RPC access
- `ACCOUNTING_CONTRACT_ADDRESS` — the deployed proxy
- `SIWE_DOMAINS` — comma-separated allowed SIWE domains
- `SAPPHIRE_PRIVATE_KEY` (local dev only) — bypasses ROFL appd; uses a direct EOA for Sapphire txs
- `DISABLE_ROFL_KEYS` (local dev only) — skip AuthToken/JWT key sync at startup
- `SWEEP_STATE_DIR` (default `/data/sweep-engine`) — sweep state directory

## Running Locally

```shell
# All-in-one local stack (no ROFL needed):
docker compose -f compose.local.yaml up --build
```

`compose.local.yaml` reads from `.env` and uses `Dockerfile.local` (uv pre-installed). When running with `SAPPHIRE_PRIVATE_KEY` set, the ROFL appd code paths are bypassed — the FDC relayer / signer is the EOA derived from that key.

## Testing

```shell
# Pre-requisite: compiled Solidity ABI artifacts
make solidity-build

# Full Python suite
uv run pytest test/py/ -v

# A single test
uv run pytest test/py/test_sweep_engine.py::test_sweep_native_happy_path -v
```

Mocking: `services/sweep_engine.py` defines `DepositAccountingProtocol` (a runtime-checkable `Protocol`) — tests can pass any object that conforms to its 10 methods in place of `AccountingContractService`.

Notable test files:

| File | Covers |
|------|--------|
| `test_deposit_verifier.py` | RPC-side verification (status, finality, log matching, balance-delta fallback) |
| `test_deposit_processor.py` | Orchestration, idempotency, background-task lifecycle |
| `test_sweep_engine.py` | State machine, persistence, gas-tank concurrency |
| `test_sweep_recovery.py` | Restart recovery for each `SweepState` |
| `test_withdrawals.py` | Withdrawal poll/resolve/broadcast happy paths and catch-up |
| `test_rofl_signer_bootstrap.py` | Idempotent on-chain signer publication |
| `test_accounting_contract_service.py` | Sapphire client (ROFL vs direct-key paths) |
