# Local two-chain bridge testing

End-to-end test of the ROSE ⇄ xROSE bridge across two **real local chains**,
driven from **Python** through the production off-chain services — no mocks. The
`onlyROFL` writes go through the localnet rofl-appd, the `onlyROFLQuery` **signed
queries** go through the sapphirepy-wrapped reader, and the resolved mint *and*
the generated burn are actually broadcast to Base and asserted on-chain.

| Side | Chain | What runs there |
|------|-------|-----------------|
| Sapphire (home) | `sapphire-localnet` (docker, chainId 23293) | Real `Accounting` proxy + the bundled **rofl-appd** (real `roflEnsureAuthorizedOrigin`, `EIP155Signer`, key derivation) |
| Base (destination) | `anvil` (chainId 84532) | Real `XRose` (xERC20) + `ROFLBridge` |

The split: **hardhat deploys + owner-wires** both sides and writes an address
manifest; **Python drives every ROFL interaction** — the `onlyROFL` wiring and
all bridge flows — via the real `AccountingContractService`,
`bootstrap_rofl_signer_address`, and `RoflAppdClient`. This is what lets the test
exercise the production signing path end to end.

## Why Python (the signed-query path)

The inbound xROSE **burn** and the deposit-address **sweep** are produced by
`onlyROFLQuery` views (`generateBridgeBurnTransfer`,
`generateSweepERC20TransferToBridge`) that require a Sapphire **signed query** to
authenticate `msg.sender`. `@oasisprotocol/sapphire-paratime` (TS) only
*encrypts* eth_calls — it cannot sign them — so a TS reader can drive the
Sapphire-side reserve+credit but not those signing reads.

Python's `sapphirepy.sapphire.wrap` *does* sign queries (the production reader
already relies on it), so this test closes that gap: it calls
`generateBridgeBurnTransfer` over a signed query and **broadcasts the resulting
custody-signed burn to anvil**, asserting xROSE `totalSupply` and the bridge's
balance drop.

### The one production change

`src/clients/rofl.py` constructs `AsyncRoflClient()`, which defaults to the appd
unix socket (`/run/rofl-appd.sock`) — the in-TEE path. On a dev box the localnet
appd is reachable over **TCP** (`:8549`), and on macOS a bind-mounted socket
can't cross the Docker VM boundary. So the client now honors `ROFL_APPD_URL`:

```python
self._client = AsyncRoflClient(os.getenv("ROFL_APPD_URL", ""))
```

Empty (the default) keeps today's socket behavior; setting it points the appd
client at the TCP endpoint. Backward-compatible — no other call site changes.

## Prerequisites

- Docker (with Rosetta on Apple Silicon — the image is `linux/amd64`)
- [foundry] (`anvil`) on `PATH`; `uv` and `bun` installed

## Run

1. From the **repo root**, start sapphire-localnet with the appd exposed. Mount
   the repo root so the localnet finds `rofl.yaml` (which triggers the appd), and
   pass the `hardhat.config.ts` mnemonic via `-to` so the deployer is funded:

   ```bash
   docker run -d --rm --name sapphire-localnet-bridge --platform linux/x86_64 \
     -p 8544-8549:8544-8549 -v "$PWD":/rofls \
     ghcr.io/oasisprotocol/sapphire-localnet \
     -to "chimney theory present latin find behave ankle clock shadow earn suit reflect"
   ```

   Drop `--platform` on non-arm64. Wait ~60–90 s; the logs should show
   `TDX ROFL detected ...`. Sanity check:

   ```bash
   curl http://localhost:8549/rofl/v1/app/id   # -> "rofl1qqn9xndja7e2pnxhttktmecvwzz0yqwxsquqyxdf"
   ```

2. Run it (boots anvil, deploys via hardhat, then drives the Python e2e):

   ```bash
   make bridge-local-test
   # or: bash scripts/bridge-local-e2e.sh
   ```

> The runner clears `PRIVATE_KEY` so hardhat deploys from the funded
> `TEST_HDWALLET` mnemonic, and exports `ROFL_APPD_URL` / `BASE_LOCAL_RPC_URL` /
> `BRIDGE_LOCAL_MANIFEST` for both the deploy script and pytest.

## What it covers

- **Bridge OUT (full cross-chain)** — `credit_deposit` (appd) → user-signed
  `BridgeWithdraw` (eth_account EIP-712) → `request_bridge_withdrawal` →
  `resolve_bridge_withdrawal` (signed query) → broadcast the signed `mint` to
  anvil → assert xROSE minted, `withdrawalId == keccak256(abi.encode(proxy,
  sapphireChainId, index))`, ledger conserved.
- **Bridge IN (full, incl. the burn)** — seed the bridge's xROSE →
  `reserve_bridge_burn` (appd) → `generate_bridge_burn_transfer`
  (**onlyROFLQuery signed query**) → broadcast the custody-signed `burn` to anvil
  → assert `totalSupply` / bridge balance drop and `burnedDepositIds[depositId]`
  → `credit_deposit` (appd) → assert the ROSE ledger credited.
- **Replay** — a duplicate `credit_deposit` reverts (real appd CBOR `fail` →
  `TransactionRevertedError`) and does not move the ledger.

The test self-skips unless sapphire-localnet (`:8545`), the appd (`:8549`), anvil
Base (`:8546`, chainId 84532) and the deploy manifest are all present, so
`make test` stays green without the infra. It is also re-runnable against an
already-deployed contract (depositIds are salted per run).

### Remaining boundary: the deposit-address sweep

The inbound **sweep** (`generateSweepERC20TransferToBridge`) is the *same*
signed-query transport the burn now proves, applied to a tx signed by a
per-user deposit keypair. The e2e seeds the bridge's xROSE directly (via the
deployer mint limit) rather than driving the full deposit-address sweep, which
additionally needs SIWE-token deposit-address derivation and gas-tank funding.
Closing that is incremental — the signing-read capability itself is already
exercised by the burn.

## Env overrides

| Var | Default | Meaning |
|-----|---------|---------|
| `SAPPHIRE_LOCALNET_URL` | `http://127.0.0.1:8545` | sapphire-localnet RPC |
| `ROFL_APPD_URL` | `http://127.0.0.1:8549` | localnet appd TCP endpoint |
| `BASE_LOCAL_RPC_URL` | `http://127.0.0.1:8546` | local Base (anvil) RPC |
| `BASE_LOCAL_PORT` | `8546` | port the runner starts anvil on |
| `BRIDGE_LOCAL_MANIFEST` | `solidity/deployments/bridge-local.json` | deploy manifest path (shared by deploy + test) |

[foundry]: https://book.getfoundry.sh/
