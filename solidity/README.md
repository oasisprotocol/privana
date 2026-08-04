# Accounting Module - Solidity Contracts

A cross-chain accounting system on Oasis Sapphire. Confidential balance management, deposit/withdrawal orchestration, and EVM transaction signing — gated by a TEE-attested off-chain service (ROFL).

## Overview

The Accounting module consists of these main components:

- **Accounting.sol** — Core accounting contract (UUPS upgradeable). Manages balances, deposits, locks, transfers, withdrawals, and emergency withdraws.
- **EVMSignerAndVerifier.sol** — Sapphire-confidential EVM keypair management; signs sweep, gas-funding, and withdrawal transactions for source chains using the `EIP155Signer` precompile.
- **EIP712SignatureVerifier.sol** — Verifies user-authored EIP-712 signatures for transfer / lock / withdrawal operations.
- **auth/AccountingSiweAuth.sol** — SIWE-based authentication for confidential Sapphire view calls.
- **Types.sol** — Shared structs and enums (`TokenInfo`, `ChainType`, `EVMKeypair`, …).

### Key Features

- **TEE-Attested Deposits**: ROFL verifies source-chain deposits off-chain via RPC; on-chain `creditDeposit` trusts the TEE attestation
- **Per-User Deposit Addresses**: Deterministic, Sapphire-derived address per `(beneficiary, chainType, version)`; funds swept to a single encumbered wallet
- **Confidential Signing**: Withdrawal/sweep transactions signed inside Sapphire via `EIP155Signer` + `SIGN_DIGEST`; private keys never leave the TEE
- **Fund Locking**: Escrow-like functionality for service interactions with time-bounded locks
- **P2P Transfers**: Internal transfers between users without source-chain transactions
- **Emergency Withdraw**: User-driven escape hatch from the deposit address, no ROFL involvement required
- **Universal Token Support**: Native tokens (ETH, MATIC, BNB, …) and ERC20 tokens across any registered EVM chain

## Architecture

```
┌─────────────────┐    ┌────────────────────────────┐   ┌──────────────────┐
│   User Wallet   │    │       Oasis Sapphire       │   │   Source Chain   │
│                 │    │                            │   │ (Base Sepolia,   │
├─────────────────┤    │  ┌──────────────────────┐  │   │  Eth Sepolia,…)  │
│ • SIWE login    │───▶│  │  Accounting (UUPS)   │  │   ├──────────────────┤
│ • EIP-712 sigs  │    │  ├──────────────────────┤  │   │ • Deposit addrs  │
│ • REST API      │    │  │ Balances / Locks     │  │   │ • Sweep dest.    │
└────────┬────────┘    │  │ Tx signing (TEE keys)│  │   │ • Withdraw dest. │
         │             │  └──────────┬───────────┘  │   └────────┬─────────┘
         │             │             │              │            │
         ▼             │             │  onlyROFL    │            │
┌─────────────────┐    │             ▼              │            │
│   ROFL TEE      │◀──▶│  ┌──────────────────────┐  │            │
│ (Python svc)    │    │  │ creditDeposit        │  │            │
├─────────────────┤    │  │ resolveWithdrawal    │  │            │
│ • Verify deps.  │    │  │ setRoflSignerAddress │  │            │
│ • Sweep engine  │    │  └──────────────────────┘  │            │
│ • Withdraw poll │    └────────────────────────────┘            │
└────────┬────────┘                                              │
         │                                                       │
         └───────── RPC reads / broadcasts ──────────────────────┘
```

The ROFL TEE is the only authorized caller of `creditDeposit`. Trust anchor: TEE attestation, enforced by `roflEnsureAuthorizedOrigin(roflAppID)`.

## Deposit Verification

ROFL verifies deposits off-chain by reading the source-chain RPC directly. For each `/deposits/check` call:

1. Fetch the transaction receipt; require `status == 1`.
2. Wait for the per-chain finality depth.
3. Match the deposit:
   - **ERC20**: find a `Transfer(_, deposit_address, amount)` log (matched by `logIndex`).
   - **Native**: match `tx.to == deposit_address` with `tx.value`, falling back to a balance-delta check across the deposit-address balance before/after the tx block (catches internal calls).
4. Confirm on-chain amount ≥ user-claimed amount.

Once verified, ROFL calls `creditDeposit(beneficiary, tokenId, amount, depositId)`. The contract trusts the TEE attestation and credits the balance — no on-chain proof of the source-chain transaction is verified.

### Alternative Solutions

The TEE-RPC path is one of several plausible ways to bridge deposit facts into the Accounting contract. The contract surface is intentionally agnostic — `creditDeposit` only requires *some* trusted oracle. Other approaches considered:

- **Hashi / ShoyuBashi + ProvethVerifier** — user submits a Merkle Patricia Trie proof of the source-chain transaction; the contract validates it against a block hash supplied by a Hashi block-hash oracle adapter. Strong trust model (any single honest adapter is enough), but gas-heavy and adds an oracle dependency.
- **FDC (Flare Data Connector)** — Flare's attestation network signs off-chain attestations of source-chain transactions; the contract verifies the signed attestation. Removes the on-chain MPT cost but adds a fee-bearing attestation round-trip and a Flare-validator-set trust assumption.
- **Direct TEE RPC verification (current)** — TEE reads the source chain itself. Cheapest, fastest, no third-party oracle. Trust anchor is the TEE attestation gating `creditDeposit`.

## Installation

```shell
bun install
```

## Compilation

Compile the contracts and generate TypeScript bindings:

```shell
bun run build
```

This will:
- Compile Solidity contracts using Hardhat
- Generate TypeChain TypeScript bindings
- Create artifacts in `artifacts/` and `typechain-types/`

## Testing

### Local Hardhat Testing

Run tests on a local Hardhat node:

```shell
bun run test
```

### Sapphire Localnet Testing

For confidential computing features (generating wallet, signing), run tests on Sapphire Localnet:

1. Start the Sapphire Localnet container:

```shell
docker run -it --rm -p8544-8548:8544-8548 ghcr.io/oasisprotocol/sapphire-localnet -to "<mnemonic from hardhat.config.ts>"
```

2. Run tests against Sapphire Localnet:

```shell
bun run test -- --network sapphire-localnet
```

### Test Coverage

Generate test coverage reports:

```shell
bun run coverage
```

## Deployment and Upgrades

Define `SECRET_KEY` env variable that has enough gas for deployment or upgrade.

```shell
export SECRET_KEY=0x...
```

### Deploy

The `deploy` task provisions both the SIWE auth helper and the Accounting proxy/implementation in one step.

```shell
# Sapphire Localnet
npx hardhat deploy --network sapphire-localnet --roflappid <rofl1…>

# Sapphire Testnet
npx hardhat deploy --network sapphire-testnet --roflappid <rofl1…>
```

If you want to assign ownership to another account e.g. Safe, also pass the 
`--owner <address>` parameter.

#### Standalone subtasks

```shell
# Deploy AccountingSiweAuth alone (e.g., to roll the auth contract):
npx hardhat deploy-siwe-auth --network sapphire-testnet --roflappid <rofl1…>
```

### Upgrade

The Accounting contract uses the UUPS upgradeable proxy pattern. To upgrade:

*Option 1:* With `SECRET_KEY` containing the EOA contract owner:

```shell
# Sapphire Testnet
npx hardhat upgrade --network sapphire-testnet --address 0x910CFfe4e8B27bc367F6E34D6D8e3C31DED68B6f

# Sapphire Mainnet
npx hardhat upgrade --network sapphire --address <accounting-proxy-address>
```

*Option 2:* With `SECRET_KEY` used to deploy an implementation only. The 
owner is Safe account that will sign and execute upgrade transactions 
separately:

```shell
# Sapphire Testnet
npx hardhat upgrade --network sapphire-testnet --address 0x910CFfe4e8B27bc367F6E34D6D8e3C31DED68B6f --output-safe accounting-upgrade-safe.json

# Sapphire Mainnet
npx hardhat upgrade --network sapphire --address <accounting-proxy-address> --output-safe accounting-upgrade-safe.json
```

This will generate two Safe Transaction Builder JSON files:

1. the `proposeUpgrade()` transaction with suffix -1
2. the `upgradeToAndCall()` transaction with suffix -2

Both transactions need to be separately signed and submitted in order to upgrade
the Accounting contracts. Combining them inside a single batch is not possible 
due to the simulation attack gated by `UPUPSUpgradeable`.

### Check status

You can view all public info of the contract by running:

```shell
# Sapphire Testnet
npx hardhat show <accounting-proxy-address> --network sapphire-testnet

# Sapphire Mainnet
npx hardhat show <accounting-proxy-address> --network sapphire
```

## Configuration

### Adding Token Support

Tokens (`setTokenInfo`, gated by `onlyROFL`) are registered by `src/services/token_info_bootstrap.py` at every ROFL restart, reading the desired token list from the `ACCOUNTING_TOKEN_INFO` JSON env var — no manual Hardhat task. Each entry is `{"chain_id": <int>}` for a native token, or `{"chain_id": <int>, "token_address": "0x..."}` for an ERC20 token. See `src/README.md` → Token Info Bootstrap.

### Setting Gas Prices

Per-chain gas prices (`setGasPrice`, gated by `onlyROFL`) are kept in sync by `src/services/gas_price_bootstrap.py` at every ROFL restart, reading desired values from the `ACCOUNTING_GAS_PRICE` JSON env var — no manual Hardhat task. See `src/README.md` → Gas Price Bootstrap.

### ROFL Signer Address

`roflSignerAddress` is published on-chain by `src/services/rofl_signer_bootstrap.py` on first ROFL start. It's the address whose signed view calls satisfy the `onlyROFLQuery` modifier — no manual setup required, but the same address must remain stable across ROFL deployments (it's derived from the ROFL-managed query-signer keypair).

## Usage Examples

### Deposit Flow

1. User authenticates with SIWE (`/auth/login`); receives an opaque `siweToken`.
2. User calls `getDepositAddress(chainType, version, siweToken)` (signed view call) → receives a per-user EVM address derived deterministically from a Sapphire-generated master key.
3. User sends funds to that address on the source chain.
4. User POSTs `/deposits/check` with `(chain_id, tx_hash, amount, log_index, version)`.
5. ROFL verifies the deposit (see [Deposit Verification](#deposit-verification)), then runs the **sweep state machine** in the background:
   - `PENDING` → optionally `GAS_FUNDED` (ERC20 only — gas tank funds the deposit address with native gas) → `SWEPT` (sweep tx confirmed) → calls `creditDeposit` → record deleted.
   - State persisted to disk; survives ROFL restart via a recovery loop.

### Transfer Flow

1. User signs an EIP-712 `Transfer` message
2. Anyone can submit the signature to execute the transfer
3. `transferBalance(...)` decrements the sender and increments the recipient atomically within the accounting system

### Withdrawal Flow

1. User signs an EIP-712 `Withdraw` message specifying token, amount, and destination address
2. ROFL submits `requestWithdrawal(...)` on Sapphire — assigns a destination-chain nonce, queues the request, emits `Withdrawal`
3. Once a 1-block delay passes, ROFL calls `resolveWithdrawal(index)` — marks the request resolved, emits `WithdrawalResolved`, and returns a Sapphire-signed RLP transaction
4. ROFL broadcasts the signed transaction on the destination chain

### Emergency Withdraw

User-driven escape hatch from a per-user deposit address, with no ROFL involvement. Useful when ROFL is unavailable or the user wants to reclaim funds before sweeping.

1. User calls `requestEmergencyWithdraw(tokenId, toAddress, version)` — overwrites any prior request for the same `(beneficiary, tokenId, version)` slot
2. After a 1-block delay, user calls `executeEmergencyWithdraw(...)` — returns a signed transaction from the deposit address to `toAddress`. The user broadcasts it on the source chain

## Hardhat Tasks

| Task                                                        | Purpose                                          |
|-------------------------------------------------------------|--------------------------------------------------|
| `deploy`                                                    | Deploy Accounting + SIWE auth                    |
| `deploy-siwe-auth`                                          | Deploy `AccountingSiweAuth` standalone           |
| `force-import`                                              | Import an existing proxy into hardhat-upgrades   |
| `upgrade`                                                   | UUPS upgrade Accounting implementation           |
| `show`                                                      | Print out all Accounting contract available info |
| `transferOwnership`                                         | Change Accounting owner to a new one             |
| `getBalance`                                                | Read user balance                                |
| `transferERC20`                                             | Sign + submit an EIP-712 transfer                |
| `withdraw` / `watchWithdrawal`                              | User-side withdrawal flow                        |
| `directWithdraw`                                            | Withdraw on-chain without ROFL/API               |
| `emergencyRequest` / `emergencyExecute` / `emergencyStatus` | Emergency-withdraw flow from deposit address     |
| `getDepositAddress` / `checkDeposit`                        | User-side deposit helpers                        |
| `accounts`                                                  | List configured signer accounts                  |
| `getAuthKeyHash` / `sign` / `transfer`                      | Auth/SIWE helpers                                |

Run `npx hardhat <task> --help` for parameter details.

## Contract Addresses

### Staging (Sapphire Testnet)

| Contract | Address |
|----------|---------|
| Accounting (Proxy) | `0x910CFfe4e8B27bc367F6E34D6D8e3C31DED68B6f` |

**ROFL App ID:** `rofl1qrmnjkx47f4tcfvfclnrtj2rad82akeum5jcpe8y`

### Production (Sapphire Mainnet)

| Contract | Address |
|----------|---------|
| Accounting (Proxy) | TBD |

**ROFL App ID:** TBD

## Security Considerations

- **Trust anchor for deposits:** ROFL TEE attestation. `creditDeposit` is gated by `roflEnsureAuthorizedOrigin(roflAppID)` — no on-chain transaction proof is verified
- **Confidential signing:** Sapphire's `EIP155Signer` + `SIGN_DIGEST` precompile keeps the contract-held EVM private key inside the secure environment; signed transactions are returned only to authorized callers
- **EIP-712:** All user-authored balance operations require typed-data signatures, validated by `EIP712SignatureVerifier`
- **Signed view-call auth:** `onlyROFLQuery` matches `msg.sender` against the ROFL-published `roflSignerAddress`. `roflEnsureAuthorizedOrigin` is unavailable inside `eth_call`, so signed-query reads use this alternative gate
- **1-block delays** on `resolveWithdrawal` and `executeEmergencyWithdraw` mitigate same-block read-then-act simulation attacks

## Development

### Project Structure

```
contracts/
├── Accounting.sol              # Main accounting contract (UUPS proxy)
├── EVMSignerAndVerifier.sol    # EVM keypairs + tx signing
├── EIP712SignatureVerifier.sol # User auth via EIP-712
├── Types.sol                   # Shared structs and enums
├── auth/
│   └── AccountingSiweAuth.sol  # SIWE auth for view-call reads
├── interfaces/                 # Contract interfaces
├── lib/                        # Utility libraries (SliceBytes, …)
└── test/                       # Mock contracts for non-Sapphire tests

test/
├── Accounting.E2E.ts           # End-to-end integration test
├── EVMSignerAndVerifier.ts     # EVM signing tests
├── AuthTokenDecryption.ts      # SIWE auth-token tests
├── RoflAppId.ts                # ROFL app ID parsing tests
└── utils.ts                    # Test utilities
```

### Key Dependencies

- **Hardhat** - Development environment and testing framework
- **Oasis Sapphire Contracts** - Confidential computing primitives (`EIP155Signer`, `Sapphire`, `SiweAuth`, …)
- **OpenZeppelin** - Security-audited contract libraries (UUPS proxy, access control)
- **Solidity RLP** - RLP encoding/decoding for Ethereum data
