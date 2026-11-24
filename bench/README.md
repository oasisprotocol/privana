# Privana benchmarks

Throughput benchmarks run as an integrator, through Privana's public API, the Python SDK (`oasis-privana`) and public Sapphire reads.

## Locks

`privana-bench locks --n N`: N new players each lock funds for one service, which settles them back. Wallet index 0 funds the run and index 1 is the service. Players start at index 1000 plus the funder's transfer nonce, so each run gets new ones.

1. **Funding, one after another**: the funder sends each player `--amount`.
2. **Locks, all at once**: every player signs a lock of that amount for the service, and all N go out together.
3. **Settles, in nonce order without waiting**: the service reads its lock ids with a signed query, signs one settle per lock at consecutive nonces and sends them in that order, `--settle-gap` apart (default 0.25 s), without waiting for replies. Privana's signer lands at most 10 writes a block, so a shorter gap lands nothing faster and holds the order less often. Each settle goes out on its own connection, opened 3 s before: a new connection takes a TLS handshake, long enough to swap settles sent this close, and the SDK closes a connection that sat idle for 5 s. The contract takes a service's settles in nonce order only, so a settle that overtakes an earlier one reverts and so does every later one.
4. **Settles, one after another**: each settle that did not land goes out again, the same signed write, once the previous one landed.

PASS means all N locks landed, resent or not, and were settled, and the funder's balance ended where it started.

## Sessions

`privana-bench sessions --n N`: the same writes as a locks run, at the pace of real traffic. Funding and the final settles one after another work as in a locks run.

1. **Locks, as sessions start**: the players lock at random times, `--per-minute` session starts a minute (default 10: 100 users with 3 sessions each in a 30 minute visit). The times follow one fixed schedule, so two runs at the same rate compare.
2. **Settles, as sessions end**: the service settles each lock `--play` seconds after it showed as landed (default 30), while later sessions still start. The settles go out at consecutive nonces without waiting for replies, at least `--settle-gap` apart, each on its own connection.
3. **A settle that does not land** goes out again: once its reply is back or lost and the service's nonce still shows it unused 30 s later, it and every settle behind it go out again in order, the same signed writes, so at most one copy lands. After 3 resends of the same settle the stream stops, and once the last lock is done the service settles everything left one after another.

The report adds how long each lock took to land after its session started, and how long each settle took to land after its session ended: how many within 1, 3 and 5 minutes, the median and the slowest. A run where any session is not settled within 5 minutes of ending fails. That time includes the 3 s the bench takes to open a connection and up to `--poll` seconds twice: until a read shows the session ended, and until one shows the landing. A lock whose reply was lost goes out again as in a locks run, once no lock has landed or been answered for 90 s, which is after the last session started.

If a locks or sessions run stopped after its funding and before its players locked, `--funded` runs it for the last `--n` players the funder funded and funds nothing:

```shell
uv run privana-bench sessions --n 10 --funded --live
```

## Transfers

`privana-bench transfers --n N`: wallet index 0 sends N transfers of `--amount` to index 1, one after another. PASS means all N landed and both balances moved by exactly N × amount.

Privana takes 10 transfers a minute and 100 a day from one caller. Transfers, here and in the funding of a locks run, go out at least `--transfer-gap` apart (default 7 s), and one funder funds at most 100 players a day, resends included.

## How a write is judged

Each write is saved to the run's journal and signed at its wallet's next nonce. A try whose connection never opens sends nothing, so the same signed write goes out again, up to 3 tries. One after another, a write whose reply was lost also goes out again while its nonce still shows unused 30 s after the last try, up to 3 times: the proxy can cut a request before it reaches Privana, and a nonce takes one write, so at most one copy lands. Locks sent all at once queue behind Privana's one signer, so a lost reply proves nothing while others still land. Each time no lock has landed or been answered for 90 s, every lock whose reply was lost and whose nonce still shows unused goes out again, until `--max-wait`: Privana waits up to 10 minutes on a write and joins a resend to that wait, so only a later resend submits again. A 4xx reply to a resend is the lock's reply. In a locks run, settles sent in nonce order without waiting go out once each. No write is re-signed at another nonce. One after another means each write waits for the previous one to land, and the run stops at the first that does not.

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

### Localnet

`.env.localnet` holds the settings for the stack that `compose.localnet.yaml` starts (see [Running Locally](../src/README.md#running-locally)), so a run there needs no mnemonic and no funds:

```sh
uv run privana-bench --env-file .env.localnet locks --n 10 --transfer-gap 0 --live
```

`sessions` runs the same way. The file's `BENCH_FUNDER_INDEX=1` moves the two wallets to indices 1 and 2, the stack's funded accounts. The stack exempts index 1 from the transfer limit, so `--transfer-gap 0` drops the wait.

A new stack starts every nonce at 0 again. If an earlier localnet run left a write unresolved, move its run folder out of `results/` first.

## Commands

```sh
uv run privana-bench preflight                # public reads only
uv run privana-bench transfers --n 10         # dry run: preflight, then stops
uv run privana-bench transfers --n 10 --live  # logs in both wallets and sends
uv run privana-bench locks --n 10             # dry run: preflight and the players' nonces
uv run privana-bench locks --n 10 --live      # logs in the funder and sends
uv run privana-bench sessions --n 10          # dry run, as for locks
uv run privana-bench sessions --n 10 --per-minute 20 --live
```

Preflight checks the chain, the contract's EIP-712 domain, the token and that the sender's nonce on chain matches the API's. `--live` logs in with SIWE, since balances are private. `--help` lists the options.

A locks run with N = 100 sends about 300 writes through Privana's one signer, and other staging users queue behind them. Funding 100 players takes over 11 minutes at the default `--transfer-gap`.

Each run writes `results/<UTC time>-<benchmark>/` with `report.md`, `report.json` and its journals. A locks report shows per phase how many writes landed without a resend, next to how many landed in all. Its table covers every landing, so a phase with resends includes their waits. A journal holds the setup, then a line before each send and one with its outcome: nonces, times and replies, never keys, signatures or login tokens. `results/` stays out of git.

Ctrl-C stops a run and still writes the report. A run refuses to start while an earlier journal holds a write that can still land at the nonce it would use. If that write will never land, move its run folder out of `results/`. A locks run that stopped in funding doesn't block the next locks run with the same `--amount` and `TOKEN_ID`: funding at nonce X always pays player 1000 + X, so that run sends the same write first. Leave the folder in `results/`, so it keeps guarding the nonce until one copy lands.

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
```

The tests use fakes and need no network.
