"""privana-bench: a read-only preflight, and the transfers and locks benchmarks, which send
only with --live."""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from importlib.metadata import version
from pathlib import Path
from typing import Any

import httpx
from eth_account.signers.local import LocalAccount
from privana import AccountingApiError, NetworkError, create_domain

from . import locks
from .chain import Chain, ChainError, host_of
from .config import TESTNET_CHAIN_ID, Config, ConfigError, PreflightError, load, wallets
from .privana import SDK_TIMEOUT_S, LoginError, Privana, Reply, safe_detail
from .report import Balances, summarize, write_report
from .transfers import Journal, Transfer, check_settled, run_transfers

BENCH_DIR = Path(__file__).resolve().parents[2]
RESULTS = BENCH_DIR / "results"
# The domain fields the SDK signs: name, version, verifyingContract and salt.
SALTED_DOMAIN_FIELDS = 0x1B
# Two blocks, since a balance query can reach an RPC node behind the one that showed the nonce.
BALANCE_SETTLE_S = 12.0


@dataclass(frozen=True)
class Facts:
    chain_id: int
    version: int
    decimals: int
    symbol: str
    nonce: int


async def preflight(cfg: Config, chain: Chain, privana: Privana, sender: LocalAccount) -> Facts:
    """Public reads only: the network, the contract, the token and the sender's nonce."""
    chain_id = await chain.chain_id()
    if chain_id != TESTNET_CHAIN_ID:
        raise PreflightError(f"SAPPHIRE_RPC_URL is chain {chain_id}, not {TESTNET_CHAIN_ID}")
    contract_version = await chain.version()
    fields, name, domain_version, contract, salt = await chain.eip712_domain()
    sdk = create_domain("testnet", cfg.accounting)
    expected = (SALTED_DOMAIN_FIELDS, sdk.name, sdk.version, sdk.verifying_contract, sdk.salt)
    if (fields, name, domain_version, contract, salt) != expected:
        raise PreflightError(
            "the contract's EIP-712 domain is not the one the SDK signs, "
            "so Privana would refuse every transfer"
        )
    token = await privana.token(cfg.token_id)
    if token is None:
        raise PreflightError(f"the API does not list TOKEN_ID {cfg.token_id}")
    chain_nonce = await chain.transfer_nonce(sender.address)
    api_nonce = await privana.transfer_nonce(sender.address)
    if chain_nonce != api_nonce:
        raise PreflightError(
            f"the sender's nonce is {chain_nonce} on chain but {api_nonce} in the API: either "
            "ACCOUNTING_ADDRESS is not the API's contract, or a transfer is landing (retry)"
        )
    return Facts(chain_id, contract_version, token[0], token[1], chain_nonce)


def to_units(amount: str, decimals: int) -> int:
    try:
        units = Decimal(amount) * 10**decimals
    except InvalidOperation:
        units = Decimal("NaN")
    if not units.is_finite() or units <= 0 or units != units.to_integral_value():
        raise PreflightError(f"--amount {amount} is not a positive amount at {decimals} decimals")
    return int(units)


def _commit() -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=BENCH_DIR, capture_output=True, text=True, check=False
        ).stdout.strip()

    sha = git("rev-parse", "--short", "HEAD") or "unknown"
    return sha + (" with local changes" if git("status", "--porcelain", "--", ".") else "")


def _progress(t: Transfer) -> None:
    reply = t.reply or Reply("unknown")
    status = f" {reply.status}" if reply.status and reply.kind != "ok" else ""
    line = f"transfer {t.k} (nonce {t.nonce}): reply {reply.kind}{status} after "
    line += f"{t.reply_at - t.sent_at:.1f} s, {t.outcome}"
    if t.landed_at is not None:
        line += f" {t.landed_at - t.due_at:.1f} s after due"
    print(line + (f". {t.note}" if t.note else ""), flush=True)


async def run(cfg: Config, args: argparse.Namespace) -> int:
    sender, receiver = wallets(cfg.mnemonic)
    async with httpx.AsyncClient(timeout=20.0) as http:
        chain = Chain(cfg.rpc_url, cfg.accounting, http)
        privana = Privana(cfg.api_url, cfg.accounting, TESTNET_CHAIN_ID)
        try:
            facts = await preflight(cfg, chain, privana, sender)
            if args.command == "locks":
                return await _locks(cfg, args, chain, privana, facts)
            print(
                f"Sapphire Testnet, Accounting {cfg.accounting} VERSION {facts.version}, "
                f"sender {sender.address} at transfer nonce {facts.nonce}, "
                f"receiver {receiver.address}."
            )
            if args.command == "preflight":
                print("Preflight passed. It sent nothing.")
                return 0
            amount = to_units(args.amount, facts.decimals)
            check_settled(RESULTS, sender.address, cfg.accounting, facts.nonce)
            print(f"Transfers: {args.n} of {args.amount} {facts.symbol}, one after another.")
            if not args.live:
                print("Dry run, nothing sent. Add --live to log in both wallets and send.")
                return 0
            return await _live_transfers(cfg, args, privana, sender, receiver, facts, amount)
        finally:
            await privana.close()


async def _live_transfers(
    cfg: Config,
    args: argparse.Namespace,
    privana: Privana,
    sender: LocalAccount,
    receiver: LocalAccount,
    facts: Facts,
    amount: int,
) -> int:
    for account in (sender, receiver):
        await privana.login(account, time.time())
    sender_before = await privana.balance(sender, cfg.token_id)
    receiver_before = await privana.balance(receiver, cfg.token_id)
    if sender_before < amount * args.n:
        print(
            f"Not funded: the sender holds {sender_before} units, the run needs {amount * args.n}."
        )
        return 1

    started = datetime.now(UTC)
    run_dir = RESULTS / f"{started:%Y%m%dT%H%M%SZ}-transfers"
    meta: dict[str, Any] = {
        "started_at": started.isoformat(timespec="seconds"),
        "n": args.n,
        "amount": args.amount,
        "symbol": facts.symbol,
        "sender": sender.address,
        "receiver": receiver.address,
        "benchmark": "transfers",
        "network": f"Sapphire Testnet ({facts.chain_id})",
        "api": cfg.api_url,
        "accounting": cfg.accounting,
        "token_id": cfg.token_id,
        "contract_version": facts.version,
        "sdk": f"oasis-privana {version('oasis-privana')}",
        "bench_commit": _commit(),
        "chain_rpc": host_of(cfg.rpc_url),
        "wallets": 2,
        "amount_units": amount,
        "pattern": "serial, each sent after the previous reply and its nonce showing used",
        "poll_s": args.poll,
        "max_wait_s": args.max_wait,
    }

    async def send(nonce: int) -> Reply:
        signature = privana.sign_transfer(sender, receiver.address, cfg.token_id, amount, nonce)
        return await privana.transfer(
            sender, receiver.address, cfg.token_id, amount, nonce, signature
        )

    async def read_nonce() -> int:
        # The API's view, which its own nonce check uses, as an integrator sees it.
        return await privana.transfer_nonce(sender.address)

    journal = Journal(run_dir / "journal.jsonl")
    journal.write("start", **meta)
    transfers: list[Transfer] = []
    ended = "aborted"
    balances: Balances | None = None
    try:
        ended = await run_transfers(
            args.n,
            facts.nonce,
            send,
            read_nonce,
            journal,
            transfers,
            args.poll,
            args.max_wait,
            progress=_progress,
        )
        await asyncio.sleep(BALANCE_SETTLE_S)
        balances = Balances(
            sender_before,
            await privana.balance(sender, cfg.token_id),
            receiver_before,
            await privana.balance(receiver, cfg.token_id),
        )
    finally:
        journal.close()
        summary = summarize(transfers, ended, amount, balances)
        print("\n" + write_report(run_dir, meta, summary, transfers))
        print(f"Report and journal: {run_dir}")
    return 0 if summary["verdict"] == "PASS" else 1


async def _locks(
    cfg: Config, args: argparse.Namespace, chain: Chain, privana: Privana, facts: Facts
) -> int:
    plan = await locks.plan(cfg, chain, privana, facts.nonce, args.n, RESULTS)
    print(
        f"Sapphire Testnet, Accounting {cfg.accounting} VERSION {facts.version}. "
        f"Funder {plan.funder.address} at transfer nonce {plan.funder_nonce}, service "
        f"{plan.service.address} at settle nonce {plan.service_nonce}, {args.n} new players "
        f"{plan.players[0].address} to {plan.players[-1].address}."
    )
    amount = to_units(args.amount, facts.decimals)
    print(
        f"Locks: the funder sends each player {args.amount} {facts.symbol}, one after another. "
        "The players lock it for the service, all at once. The service settles the locks "
        "back to the funder, all at once and then the rest one after another."
    )
    if not args.live:
        print("Dry run, nothing sent. Add --live to log in the funder and send.")
        return 0
    meta = locks.describe(cfg, plan, args, facts.version, facts.symbol, _commit())
    return await locks.run(cfg, args, chain, privana, plan, amount, meta, RESULTS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="privana-bench", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight", help="public reads only, sends nothing")
    tr = commands.add_parser("transfers", help="one wallet sends N transfers in a row")
    tr.add_argument(
        "--n", type=int, default=10, help="number of transfers, up to 1000 (default 10)"
    )
    tr.add_argument("--amount", default="0.01", help="token amount per transfer (default 0.01)")
    tr.add_argument(
        "--poll", type=float, default=1.0, help="seconds between nonce reads (default 1)"
    )
    tr.add_argument(
        "--max-wait",
        type=float,
        default=300.0,
        help="seconds before a transfer counts as unresolved, at least 90 (default 300)",
    )
    tr.add_argument("--live", action="store_true", help="log in and send; otherwise a dry run")
    lk = commands.add_parser("locks", help="N players lock for one service, which settles them")
    lk.add_argument("--n", type=int, default=10, help="number of players, up to 100 (default 10)")
    lk.add_argument("--amount", default="0.01", help="token amount per player (default 0.01)")
    lk.add_argument(
        "--poll", type=float, default=2.0, help="seconds between nonce reads (default 2)"
    )
    lk.add_argument(
        "--max-wait",
        type=float,
        default=1800.0,
        help="seconds before a write counts as unresolved, at least 90 (default 1800)",
    )
    lk.add_argument("--live", action="store_true", help="log in and send; otherwise a dry run")
    args = parser.parse_args(argv)
    most = {"transfers": 1000, "locks": 100}.get(args.command)
    if most and not (1 <= args.n <= most and args.poll > 0 and args.max_wait >= SDK_TIMEOUT_S):
        parser.error(
            f"--n must be 1 to {most}, --poll positive and --max-wait at least "
            f"{SDK_TIMEOUT_S:.0f}, the SDK reply timeout"
        )
    try:
        return asyncio.run(run(load(), args))
    except KeyboardInterrupt:
        print("Interrupted. A write in flight can still land, see the run's journals.")
        return 130
    except (ConfigError, PreflightError, LoginError, ChainError) as exc:
        print(f"{type(exc).__name__}: {exc}")
    except AccountingApiError as exc:
        print(f"API {exc.status_code}: {safe_detail(exc.detail)}")
    except (httpx.HTTPError, NetworkError) as exc:
        # No message: an httpx error names the URL, and an RPC URL can hold a key.
        print(f"{type(exc).__name__}: a network call failed, check the URLs in .env")
    return 2
