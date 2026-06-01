# Accounting Module - Solidity Contracts

A cross-chain accounting system on Oasis Sapphire. Confidential balance management, deposit/withdrawal orchestration, and EVM transaction signing — gated by a TEE-attested off-chain service (ROFL).

## Overview

The Accounting module consists of these main components:

- **Accounting.sol** — Core accounting contract (UUPS upgradeable). Owns balances, deposits, locks, withdrawals, emergency-withdraw state, history storage, and the public ABI.
- **AccountingSigner.sol** — UUPS signer proxy that owns Sapphire-confidential EVM signing keys and signs sweep, gas-funding, withdrawal, and emergency-withdraw transactions for Accounting.
- **AccountingHistoryModule.sol** — non-upgradeable delegated history reader. Accounting owns the history storage and writes history through shared internal helpers; the module supplies `getHistory` code via `delegatecall`.
- **AccountingStorage.sol** — Shared storage base used by Accounting and the delegated history module.
- **EVMSignerAndVerifier.sol** — Sapphire-confidential EVM keypair/signing base used by AccountingSigner.
- **EIP712SignatureVerifier.sol** — Verifies user-authored EIP-712 signatures for transfer / lock / withdrawal operations.
- **lib/TokenCodec.sol** — Packed EVM token metadata encoding/decoding shared by Accounting and modules.
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
└────────┬────────┘    │  │ Withdraw accounting  │  │   │ • Withdraw dest. │
         │             │  └──────────┬───────────┘  │   └────────┬─────────┘
         │             │             │              │            │
         ▼             │             │ via Accounting│           │
┌─────────────────┐    │             ▼              │            │
│   ROFL TEE      │◀──▶│  ┌──────────────────────┐  │            │
│ (Python svc)    │    │  │ AccountingSigner     │  │            │
├─────────────────┤    │  │ TEE keys / tx signing│  │            │
│ • Verify deps.  │    │  └──────────┬───────────┘  │            │
│ • Sweep engine  │    │             │ onlyROFL     │            │
│ • Withdraw poll │    │             ▼              │            │
└────────┬────────┘    │  ┌──────────────────────┐  │            │
         │             │  │ creditDeposit        │  │            │
         │             │  │ setRoflSignerAddress │  │            │
         │             │  └──────────────────────┘  │            │
         │             └────────────────────────────┘            │
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

## Deployment

The `deploy` task provisions the SIWE auth helper, Accounting proxy/implementation,
AccountingSigner proxy, AccountingHistoryModule, and links the helper addresses with
one `setModules(...)` call.

### Deploy to Sapphire Localnet

```shell
npx hardhat deploy --network sapphire-localnet --roflappid <rofl1…>
```

### Deploy to Sapphire Testnet

```shell
npx hardhat deploy --network sapphire-testnet --roflappid <rofl1…>
```

Use `--owner <admin-address>` to transfer Accounting and its linked
AccountingSigner to a production admin after helper modules are linked.

Outputs: SIWE-auth address, Accounting proxy/implementation address,
AccountingSigner address, AccountingHistoryModule address, EVM signing address, owner.

### Standalone subtasks

```shell
# Deploy AccountingSiweAuth alone (e.g., to roll the auth contract):
npx hardhat deploy-siwe-auth --network sapphire-testnet --roflappid <rofl1…>

# Force-import an existing proxy into hardhat-upgrades' deployment registry:
npx hardhat force-import --network sapphire-testnet --proxy <proxy-address>
```

`force-import` only supports proxies that already expose the split Accounting
layout (`historyModule()` and `signer()`). Pre-split inline-signing proxies
should be redeployed before launch unless an explicit migration is written.

### Upgrade

The Accounting contract uses the UUPS upgradeable proxy pattern. To upgrade:

#### 1. Make contract changes and compile

```shell
cd solidity
bun run build
```

#### 2. Run the upgrade task

For staging (Sapphire Testnet):
```shell
npx hardhat upgrade --network sapphire-testnet --proxy 0xad3C76e4E621C0cfF7540479Ee9B0A945723A642
```

For production (Sapphire Mainnet):
```shell
npx hardhat upgrade --network sapphire --proxy <accounting-proxy-address>
```

If the task cannot resolve `siweAuth()` from the existing proxy, pass it explicitly:
```shell
npx hardhat upgrade --network sapphire-testnet --proxy <proxy-address> --siweauth <siwe-auth-address>
```

If helper contracts were deployed separately, attach them during upgrade:
```shell
npx hardhat upgrade --network sapphire-testnet --proxy <proxy-address> --history <history-module-address> --signer <signer-address>
```

This signer/module split is a fresh pre-mainnet layout. The upgrade task only supports
Accounting proxies that already expose `historyModule()` and `signer()`.
Pre-split inline-signing proxies are intentionally incompatible
unless an explicit storage/key migration is written.

History entries remain in Accounting proxy storage and are read by the delegated module
after upgrade.

`getHistory` is exposed on Accounting and delegates to `AccountingHistoryModule` at the proxy
address. `Accounting.historyModule()` returns the module code address, not a storage-owning
history contract. Read clients should use the `AccountingHistoryModule` ABI at the Accounting
proxy address; the module address is validated code, not the call target for user history.

#### 3. Update the README

After a successful upgrade, refresh the implementation address in the [Contract Addresses](#contract-addresses) section below.

#### Troubleshooting

If the proxy was deployed outside of hardhat-upgrades (or on a fresh machine), you may need to import it first:

```shell
npx hardhat force-import --network sapphire-testnet --proxy <accounting-proxy-address>
```

The force-import task intentionally rejects pre-split inline-signing proxies.
Use it only after the proxy already exposes `historyModule()` and `signer()`.

The upgrade task uses `redeployImplementation: 'always'` to ensure a fresh implementation is deployed. If you see the same implementation address after an upgrade, verify the contract was actually recompiled with your changes.

## Configuration

### Adding Token Support

After deployment, register tokens that the accounting system should support:

#### Native Token (ETH, MATIC, etc.)

```shell
npx hardhat addEVMNativeToken --network sapphire-testnet --chainid 1 --address <deployed-accounting-address>
```

#### ERC20 Token

```shell
npx hardhat addEVMErc20Token --network sapphire-testnet --chainid 1 --tokenaddress 0x... --address <deployed-accounting-address>
```

Both tasks call `setTokenInfo(...)` (owner-only).

### Setting Gas Prices

Configure gas prices for different chains:

```shell
npx hardhat setGasPrice --network sapphire-testnet --chainid 1 --gasprice 20000000000 --contract <deployed-accounting-address>
```

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

| Task | Purpose |
|------|---------|
| `deploy` | Deploy Accounting + AccountingSigner + AccountingHistoryModule + SIWE auth |
| `deploy-siwe-auth` | Deploy `AccountingSiweAuth` standalone |
| `force-import` | Import an existing proxy into hardhat-upgrades |
| `upgrade` | UUPS upgrade Accounting implementation |
| `addEVMNativeToken` / `addEVMErc20Token` | Register tokens |
| `setGasPrice` | Set per-chain gas price |
| `getBalance` | Read user balance |
| `transferERC20` | Sign + submit an EIP-712 transfer |
| `withdraw` / `watchWithdrawal` | User-side withdrawal flow |
| `directWithdraw` | Withdraw on-chain without ROFL/API |
| `emergencyRequest` / `emergencyExecute` / `emergencyStatus` | Emergency-withdraw flow from deposit address |
| `getDepositAddress` / `checkDeposit` | User-side deposit helpers |
| `accounts` | List configured signer accounts |
| `getAuthKeyHash` / `sign` / `transfer` | Auth/SIWE helpers |

Run `npx hardhat <task> --help` for parameter details.

## Contract Addresses

### Staging (Sapphire Testnet)

| Contract | Address |
|----------|---------|
| AccountingSiweAuth | `0xFc97d47F0bc8f4E50333D34c281705E0666D3fD7` |
| Accounting (Proxy) | `0xad3C76e4E621C0cfF7540479Ee9B0A945723A642` |
| Accounting (Implementation) | `0x12fb6720c445aa2d38009eb64e191e26C30b4CAA` (refresh after each upgrade) |
| AccountingSigner | TBD after deployment |
| AccountingHistoryModule | TBD after deployment |

**ROFL App ID:** `rofl1qrmnjkx47f4tcfvfclnrtj2rad82akeum5jcpe8y`

**Source-chain operator addresses** (owned by AccountingSigner; query through the Accounting proxy after deployment):

| Role | Address | Funding |
|------|---------|---------|
| `evmAddress` (sweep / withdrawal signer) | `0xF0006F3222b033De6DBE4CeB1E5AE99E54Aa398F` | None — does not pay gas directly. |
| `gasTankAddress` (funds deposit addresses for ERC20 sweeps) | `0xDfFE6d45F1D52320d8F05CCd6623b09EcA05CF53` | **Must hold native gas on every source chain** (Base Sepolia at minimum). |

### Production (Sapphire Mainnet)

| Contract | Address |
|----------|---------|
| AccountingSiweAuth | TBD |
| Accounting (Proxy) | TBD |
| Accounting (Implementation) | TBD |
| AccountingSigner | TBD |
| AccountingHistoryModule | TBD |

## Security Considerations

- **Trust anchor for deposits:** ROFL TEE attestation. `creditDeposit` is gated by `roflEnsureAuthorizedOrigin(roflAppID)` — no on-chain transaction proof is verified
- **Confidential signing:** Sapphire's `EIP155Signer` + `SIGN_DIGEST` precompile keeps the contract-held EVM private key inside the secure environment; signed transactions are returned only to authorized callers
- **Signer ownership:** AccountingSigner owns the Sapphire EVM keys. Deployment and contract setters require it to be linked back to the Accounting proxy and owned by the same admin; signer ownership changes are routed through Accounting ownership transfer. Replacing the signer is signing-root/key rotation, not routine module rotation: it changes the EVM signing address, gas tank address, source-chain nonces, gas prices, ROFL query signer, and per-user deposit-address derivation root.
- **EIP-712:** All user-authored balance operations require typed-data signatures, validated by `EIP712SignatureVerifier`
- **Signed view-call auth:** `onlyROFLQuery` matches `msg.sender` against the ROFL-published `roflSignerAddress`. `roflEnsureAuthorizedOrigin` is unavailable inside `eth_call`, so signed-query reads use this alternative gate
- **History module:** Accounting stores history in its own proxy storage and delegates `getHistory` reads to `AccountingHistoryModule`. Only the owner can update the module pointer; setters validate the module marker before linking.
- **1-block delays** on `resolveWithdrawal` and `executeEmergencyWithdraw` mitigate same-block read-then-act simulation attacks

## Development

### Project Structure

```
contracts/
├── Accounting.sol              # Main accounting contract (UUPS proxy)
├── AccountingSigner.sol        # UUPS signer proxy for Sapphire-held EVM keys
├── AccountingHistoryModule.sol # Delegated history read code
├── AccountingStorage.sol       # Shared Accounting/module storage layout
├── EVMSignerAndVerifier.sol    # EVM keypairs + tx signing
├── EIP712SignatureVerifier.sol # User auth via EIP-712
├── Types.sol                   # Shared structs and enums
├── auth/
│   └── AccountingSiweAuth.sol  # SIWE auth for view-call reads
├── interfaces/                 # Contract interfaces
├── lib/                        # Utility libraries (TokenCodec, …)
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
