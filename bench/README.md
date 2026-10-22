# Privana benchmarks

Throughput benchmarks run as an integrator, through Privana's public API, the Python SDK (`oasis-privana`) and public Sapphire reads.

## Locks

`privana-bench locks --n N`: N new players each lock funds for one service, which settles them back. Wallet index 0 funds the run and index 1 is the service. Players start at index 1000 plus the funder's transfer nonce, so each run gets new ones.

1. **Funding, one after another**: the funder sends each player `--amount`.
2. **Locks, all at once**: every player signs a lock of that amount for the service, and all N go out together.
3. **Settles, all at once**: the service reads its lock ids with a signed query, signs one settle per lock at consecutive nonces and sends them together. Privana accepts only the current nonce, so one lands and the rest are refused.
4. **Settles, one after another**: each refused settle goes out again, the same signed write, once the previous one landed.

PASS means all N locks landed and were settled, and the funder's balance ended where it started.

## Transfers

`privana-bench transfers --n N`: wallet index 0 sends N transfers of `--amount` to index 1, one after another. PASS means all N landed and both balances moved by exactly N × amount.

## How a write is judged

Each write is saved to the run's journal and signed at its wallet's next nonce. A try whose connection never opens sends nothing, so the same signed write goes out again, up to 3 tries. One after another, a write whose reply was lost also goes out again while its nonce still shows unused 30 s after the last try, up to 3 times: the proxy can cut a request before it reaches Privana, and a nonce takes one write, so at most one copy lands. Writes sent all at once go out once each. No write is re-signed at another nonce. One after another means each write waits for the previous one to land, and the run stops at the first that does not.

Each write ends as one of:

- **landed**: its wallet's nonce moved past it, whatever the reply said. A signature is valid at one nonce and nothing else signs for these wallets, so only this write can move it.
- **refused**: a 4xx reply, an SDK rejection or no connection on any try, and a nonce read 30 s after the reply still shows it unused. A 409, submitted without a seen outcome, does not count, nor does a 400 that Privana raises after sending when the enclave's reply does not parse.
- **unresolved**: neither, such as no landing within `--max-wait`. It can still land, so the run stops before reusing its nonce and skips the balance check.

## Setup

Needs [uv](https://docs.astral.sh/uv/).

```sh
cd privana/bench
uv sync
# Writes .env with a fresh mnemonic, never printed. It refuses to replace an existing
# .env, which holds the funded mnemonic.
(set -C; umask 077; grep -v '^BENCH_MNEMONIC=' .env.example > .env) &&
  uv run python -c 'from eth_account import Account; Account.enable_unaudited_hdwallet_features(); print("BENCH_MNEMONIC=" + Account.create_with_mnemonic()[1])' >> .env
uv run privana-bench preflight
```

Preflight prints the two wallets. Give index 0 a Privana balance of N × `--amount` in `TOKEN_ID` (Base Sepolia USDC by default), so 1 USDC for `locks --n 100`. Nothing else needs funds.

Use the mnemonic only for benchmarks and run one benchmark at a time, since a nonce step counts as our write only while nothing else signs for these wallets.

## Commands

```sh
uv run privana-bench preflight                # public reads only
uv run privana-bench transfers --n 10         # dry run: preflight, then stops
uv run privana-bench transfers --n 10 --live  # logs in both wallets and sends
uv run privana-bench locks --n 10             # dry run: preflight and the players' nonces
uv run privana-bench locks --n 10 --live      # logs in the funder and sends
```

Preflight checks the chain, the contract's EIP-712 domain, the token and that the sender's nonce on chain matches the API's. `--live` logs in with SIWE, since balances are private. `--help` lists the options.

A locks run with N = 100 sends about 300 writes through Privana's one signer over nearly an hour, and other staging users queue behind them.

Each run writes `results/<UTC time>-<benchmark>/` with `report.md`, `report.json` and its journals. A journal holds the setup, then a line before each send and one with its outcome: nonces, times and replies, never keys, signatures or login tokens. `results/` stays out of git.

Ctrl-C stops a run and still writes the report. A run refuses to start while an earlier journal holds a write that can still land at the nonce it would use. If that write will never land, move its run folder out of `results/`. A locks run that stopped in funding doesn't block the next locks run with the same `--amount` and `TOKEN_ID`: funding at nonce X always pays player 1000 + X, so that run sends the same write first. Leave the folder in `results/`, so it keeps guarding the nonce until one copy lands.

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
```

The tests use fakes and need no network.
