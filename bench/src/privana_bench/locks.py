"""Locks: N players each lock funds for one service, all at once. The service
then settles every lock back to the funder, first all at once, then the rest
one after another.

Wallets come from BENCH_MNEMONIC. Index 0 funds the players and gets the
settled funds back, and index 1 is the service. The players are new each run:
they start at index 1000 plus the funder's transfer nonce, which every funding
transfer moves on. Nothing else signs for these wallets, and a signature is
valid at one nonce only, so a nonce step on one of them is our write landing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from eth_account.signers.local import LocalAccount

from .chain import Chain, host_of
from .config import Config, PreflightError, indexed_wallets, wallets
from .privana import Privana, Reply
from .transfers import REFUSED_WAIT_S, Journal, Transfer, _said, check_settled, run_transfers

PLAYER_INDEX = 1000
LOCK_TTL_S = 2 * 3600  # settling does not check expiry, so this only bounds a stuck lock
SETTLE_S = 12.0  # two blocks, so reads on a lagging RPC node see the last landings
LOCK_NONCES = "createLockNonces"
SETTLE_NONCES = "transferLockedNonces"
PHASES = {
    "funding": "Funding, one after another",
    "locks": "Locks, all at once",
    "settles at once": "Settles, all at once",
    "settles": "Settles, one after another",
}

ReadNonces = Callable[[list[str]], Awaitable[tuple[int, dict[str, int]]]]


@dataclass
class Action(Transfer):
    """A write sent in a burst: a Transfer, plus the wallet whose nonce it uses."""

    wallet: str = ""
    block: int | None = None  # the block of the first read that showed it landed


def _done(a: Action, last_read: float, refused_wait_s: float) -> bool:
    return a.landed_at is not None or (
        a.reply is not None
        and a.reply.kind == "refused"
        and last_read >= a.reply_at + refused_wait_s
    )


async def burst(
    actions: list[Action],
    send: Callable[[Action], Awaitable[Reply]],
    read_nonces: ReadNonces,
    poll_s: float,
    max_wait_s: float,
    refused_wait_s: float,
    journal: Journal | None = None,
) -> None:
    """Send every action at once, each exactly once, and read their wallets'
    nonces until each landed or was refused `refused_wait_s` ago, or until
    `max_wait_s` passed. Anything else stays unresolved: it can still land."""
    start = time.time()
    for a in actions:
        a.due_at = start
        if journal is not None:
            journal.write("send", k=a.k, nonce=a.nonce, wallet=a.wallet, due_at=start)

    async def one(a: Action) -> None:
        a.sent_at = time.time()
        try:
            a.reply = await send(a)
        except Exception as exc:
            a.reply = Reply("unknown", None, type(exc).__name__)
        a.reply_at = time.time()
        a.sent_at += a.reply.retry_s  # the try that went out

    sends = asyncio.gather(*(one(a) for a in actions))
    wallets = sorted({a.wallet for a in actions})
    last_read, shown = 0.0, (0, 0)
    try:
        while True:
            try:
                block, nonces = await read_nonces(wallets)
                last_read = time.time()
                for a in actions:
                    a.seen_nonce = nonces[a.wallet]
                    if a.seen_nonce > a.nonce and a.landed_at is None:
                        a.landed_at, a.block = last_read, block
                counts = (
                    sum(a.landed_at is not None for a in actions),
                    sum(a.reply is not None for a in actions),
                )
                if counts != shown:
                    shown = counts
                    print(
                        f"  {last_read - start:5.0f} s, block {block}: {counts[0]} of "
                        f"{len(actions)} landed, {counts[1]} replied",
                        flush=True,
                    )
            except Exception:
                pass  # a failed read is no news
            if all(_done(a, last_read, refused_wait_s) for a in actions):
                break
            if time.time() >= start + max_wait_s:
                break
            await asyncio.sleep(poll_s)
        await sends  # every reply is back within 3 SDK timeouts
    finally:
        sends.cancel()
        for a in actions:
            a.done_at = time.time()
            if a.landed_at is not None:
                a.outcome = "landed"
                if a.reply and a.reply.kind != "ok":
                    a.note = f"the reply said it failed ({_said(a.reply)}), but it landed"
            elif a.reply and _done(a, last_read, refused_wait_s):
                a.outcome = "refused"
                a.note = _said(a.reply)
            else:
                reply = f", reply {_said(a.reply)}" if a.reply and a.reply.kind != "ok" else ""
                a.note = f"not landed when the watch ended{reply}, so it can still land"
            if journal is not None:
                journal.write("outcome", **asdict(a))


def same_funding(amount: str, token_id: str) -> Callable[[dict[str, Any]], bool]:
    """Whether an earlier journal is a locks run's funding of `amount` of `token_id`.
    Funding at nonce X always pays player PLAYER_INDEX + X, so its write at the
    funder's nonce is the one this run sends first."""
    expected = ("locks", amount, token_id)
    return lambda start: (
        (start.get("benchmark"), start.get("amount"), start.get("token_id")) == expected
    )


@dataclass
class Plan:
    funder: LocalAccount
    service: LocalAccount
    players: list[LocalAccount]
    first: int
    funder_nonce: int
    lock_nonces: dict[str, int]
    service_nonce: int


async def plan(
    cfg: Config,
    chain: Chain,
    privana: Privana,
    funder_nonce: int,
    n: int,
    amount: str,
    results: Path,
) -> Plan:
    """Public reads only: the players are new, and no earlier write can still land."""
    funder, service = wallets(cfg.mnemonic)
    first = PLAYER_INDEX + funder_nonce
    players = indexed_wallets(cfg.mnemonic, first, n)
    _, lock_nonces = await chain.nonces(LOCK_NONCES, [p.address for p in players])
    used = [address for address, nonce in lock_nonces.items() if nonce != 0]
    if used:
        raise PreflightError(f"player {used[0]} has locked before, so the players are not new")
    _, settle_nonces = await chain.nonces(SETTLE_NONCES, [service.address])
    service_nonce = settle_nonces[service.address]
    api_nonce = await privana.transfer_locked_nonce(service.address)
    if service_nonce != api_nonce:
        raise PreflightError(
            f"the service's settle nonce is {service_nonce} on chain but {api_nonce} in the API"
        )
    check_settled(
        results, funder.address, cfg.accounting, funder_nonce, same_funding(amount, cfg.token_id)
    )
    check_settled(results, service.address, cfg.accounting, service_nonce)
    # The service's signed lock read works if a new player shows no locks.
    probe = await chain.service_locks(service, [players[0].address])
    if probe != {players[0].address: []}:
        raise PreflightError(f"the service's lock read for a new player gave {probe}")
    return Plan(funder, service, players, first, funder_nonce, lock_nonces, service_nonce)


async def run(
    cfg: Config,
    args: argparse.Namespace,
    chain: Chain,
    privana: Privana,
    p: Plan,
    amount: int,
    meta: dict[str, Any],
    results: Path,
) -> int:
    """Log in the funder for its balance, then fund, lock and settle."""
    await privana.login(p.funder, time.time())
    before = await privana.balance(p.funder, cfg.token_id)
    if before < amount * args.n:
        print(f"Not funded: the funder holds {before} units, the run needs {amount * args.n}.")
        return 1

    run_dir = results / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-locks"
    phases: dict[str, Sequence[Transfer]] = {name: [] for name in PHASES}
    ended, check = "aborted", "not run"
    try:
        ended = await _phases(cfg, args, chain, privana, p, amount, meta, run_dir, phases)
        await asyncio.sleep(SETTLE_S)
        check = await _check(cfg, privana, p, amount, phases, before)
    finally:
        report = write_report(run_dir, meta, phases, ended, check)
        print("\n" + report + f"\nReport and journals: {run_dir}")
    return 0 if ended == "completed" and check == "ok" else 1


async def _phases(
    cfg: Config,
    args: argparse.Namespace,
    chain: Chain,
    privana: Privana,
    p: Plan,
    amount: int,
    meta: dict[str, Any],
    run_dir: Path,
    phases: dict[str, Sequence[Transfer]],
) -> str:
    token = cfg.token_id

    print(f"Funding {len(p.players)} players, one after another.")

    async def fund(nonce: int) -> Reply:
        to = p.players[nonce - p.funder_nonce].address
        signature = privana.sign_transfer(p.funder, to, token, amount, nonce)
        return await privana.transfer(p.funder, to, token, amount, nonce, signature)

    funding: list[Transfer] = []
    phases["funding"] = funding
    journal = Journal(run_dir / "funding.jsonl")
    journal.write("start", **{**meta, "sender": p.funder.address})
    try:
        ended = await run_transfers(
            len(p.players),
            p.funder_nonce,
            fund,
            lambda: privana.transfer_nonce(p.funder.address),
            journal,
            funding,
            args.poll,
            args.max_wait,
            progress=_progress,
        )
    finally:
        journal.close()
    if ended != "completed":
        return f"funding {ended}"

    print(f"Locking: {len(p.players)} players at once.")
    locks = [
        Action(i, p.lock_nonces[w.address], 0.0, wallet=w.address) for i, w in enumerate(p.players)
    ]
    phases["locks"] = locks
    expiry = int(time.time()) + LOCK_TTL_S
    journal = Journal(run_dir / "locks.jsonl")
    journal.write("start", **meta, expiry=expiry)
    try:
        await burst(
            locks,
            lambda a: privana.lock(
                p.players[a.k], p.service.address, token, amount, expiry, a.nonce
            ),
            lambda w: chain.nonces(LOCK_NONCES, w),
            args.poll,
            args.max_wait,
            REFUSED_WAIT_S,
            journal,
        )
    finally:
        journal.close()

    locked: list[str] = [p.players[a.k].address for a in locks if a.outcome == "landed"]
    if not locked:
        return "stopped after locking: no lock landed"
    # The lock ids, from the service's signed read.
    await asyncio.sleep(SETTLE_S)
    found = await chain.service_locks(p.service, locked)
    odd = [u for u in locked if len(found[u]) != 1 or found[u][0][1] != amount]
    if odd:
        return (
            f"stopped after locking: player {odd[0]} has locks {found[odd[0]]}, not one of {amount}"
        )
    targets = [(u, found[u][0][0]) for u in locked]

    _, settle_nonces = await chain.nonces(SETTLE_NONCES, [p.service.address])
    if settle_nonces[p.service.address] != p.service_nonce:
        return "stopped after locking: the service's settle nonce moved, so another signer uses it"
    print(f"Settling: {len(targets)} locks at once, at consecutive nonces.")
    at_once = [
        Action(i, p.service_nonce + i, 0.0, wallet=p.service.address) for i in range(len(targets))
    ]
    phases["settles at once"] = at_once
    journal = Journal(run_dir / "settles-at-once.jsonl")
    journal.write("start", **{**meta, "sender": p.service.address})
    try:
        await burst(
            at_once,
            lambda a: privana.settle(
                p.service, targets[a.k][0], p.funder.address, targets[a.k][1], amount, a.nonce
            ),
            lambda w: chain.nonces(SETTLE_NONCES, w),
            args.poll,
            args.max_wait,
            REFUSED_WAIT_S,
            journal,
        )
    finally:
        journal.close()
    if any(a.outcome == "unresolved" for a in at_once):
        return "stopped after the settles at once: one can still land and would take a nonce"

    rest = [targets[a.k] for a in at_once if a.outcome != "landed"]
    ended = "completed"
    if rest:
        print(f"Settling the other {len(rest)} one after another.")
        # The landed settles took the burst's first nonces, so each of the rest goes out
        # again at its burst nonce, the same signed write.
        start = p.service_nonce + len(at_once) - len(rest)

        async def settle(nonce: int) -> Reply:
            user, lock_id = rest[nonce - start]
            return await privana.settle(p.service, user, p.funder.address, lock_id, amount, nonce)

        settles: list[Transfer] = []
        phases["settles"] = settles
        journal = Journal(run_dir / "settles.jsonl")
        journal.write("start", **{**meta, "sender": p.service.address})
        try:
            ended = await run_transfers(
                len(rest),
                start,
                settle,
                lambda: privana.transfer_locked_nonce(p.service.address),
                journal,
                settles,
                args.poll,
                args.max_wait,
                progress=_progress,
            )
        finally:
            journal.close()
    missing = len(p.players) - len(locked)
    if ended == "completed" and missing:
        return f"partial: {missing} of {len(p.players)} locks did not land, see the notes"
    return ended


async def _check(
    cfg: Config,
    privana: Privana,
    p: Plan,
    amount: int,
    phases: dict[str, Sequence[Transfer]],
    before: int,
) -> str:
    """Whether the funder's balance moved as the landed writes say: each funding
    sent `amount` out and each settle brought `amount` back."""
    if any(t.outcome == "unresolved" for ts in phases.values() for t in ts):
        return "skipped: an unresolved write can still land"

    def landed(name: str) -> int:
        return sum(t.outcome == "landed" for t in phases[name])

    expected = amount * (landed("settles at once") + landed("settles") - landed("funding"))
    moved = await privana.balance(p.funder, cfg.token_id) - before
    return (
        "ok"
        if moved == expected
        else f"mismatch: funder balance moved {moved}, expected {expected}"
    )


def _progress(t: Transfer) -> None:
    reply = _label(t.reply) if t.reply else "none"
    failed = t.reply.connect_failures if t.reply else 0
    unopened = f", {failed} connection(s) never opened" if failed else ""
    print(
        f"  {t.k + 1}: {t.outcome}, reply {reply} after {t.reply_at - t.sent_at:.1f} s{unopened}",
        flush=True,
    )


def _label(reply: Reply) -> str:
    if reply.kind == "ok":
        return "ok"
    if reply.status:
        return str(reply.status)
    return "SDK rejected" if reply.detail.startswith("SDK rejected") else reply.detail or reply.kind


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def summarize(ts: Sequence[Transfer]) -> dict[str, Any]:
    landed = [t for t in ts if t.landed_at is not None]
    sends = [t.sent_at for t in ts if t.sent_at]
    landings = [t.landed_at for t in landed if t.landed_at is not None]
    span = max(landings) - min(sends) if landings else None
    unresolved = sum(t.outcome == "unresolved" for t in ts)
    return {
        "sent": len(ts),
        "landed": len(landed),
        "refused": sum(t.outcome == "refused" for t in ts),
        "unresolved": unresolved,
        "replies": dict(Counter(_label(t.reply) for t in ts if t.reply)),
        "failed_replies_that_landed": sum(1 for t in landed if t.reply and t.reply.kind != "ok"),
        "resends": sum(t.resends for t in ts),
        "connections_that_never_opened": sum(t.reply.connect_failures for t in ts if t.reply),
        "first_send_to_last_landing_s": _round(span),
        # An unresolved write stalls the phase, so a rate over the landed ones misleads.
        "landed_per_minute": _round(len(landed) / span * 60) if span and not unresolved else None,
        "slowest_reply_s": _round(
            max((t.reply_at - t.sent_at for t in ts if t.reply), default=None)
        ),
    }


def to_markdown(
    meta: dict[str, Any], phases: dict[str, Sequence[Transfer]], ended: str, check: str
) -> str:
    def cell(value: Any, unit: str = "") -> str:
        return "n/a" if value is None else f"{value}{unit}"

    summaries = {name: summarize(ts) for name, ts in phases.items() if ts}
    verdict = "PASS" if ended == "completed" and check == "ok" else "FAIL"
    lines = [
        f"# Locks, N = {meta['n']}: {verdict}",
        "",
        f"{meta['started_at']}: {meta['n']} new players each lock {meta['amount']} "
        f"{meta['symbol']} for the service {meta['service']}, which settles them back to the "
        f"funder {meta['funder']}.",
        "",
        "| Phase | Sent | Landed | Refused | Unresolved | First send to last landing "
        "| Landed per minute | Slowest reply |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, s in summaries.items():
        lines.append(
            f"| {PHASES[name]} | {s['sent']} | {s['landed']} | {s['refused']} | {s['unresolved']} "
            f"| {cell(s['first_send_to_last_landing_s'], ' s')} | {cell(s['landed_per_minute'])} "
            f"| {cell(s['slowest_reply_s'], ' s')} |"
        )
    lines += [
        "",
        "Replies: " + "; ".join(f"{n} {s['replies']}" for n, s in summaries.items()) + ".",
        "Failed replies that landed: "
        + "; ".join(f"{n} {s['failed_replies_that_landed']}" for n, s in summaries.items())
        + ".",
        "Resends after a lost reply: "
        + "; ".join(f"{n} {s['resends']}" for n, s in summaries.items())
        + ".",
        "Connections that never opened, so nothing went out on that try: "
        + "; ".join(f"{n} {s['connections_that_never_opened']}" for n, s in summaries.items())
        + ".",
        f"Money check: {check}. Ended: {ended}.",
        "",
        f"Landing times come from nonce reads every {meta['poll_s']} s, not block times.",
        "",
        "Setup: " + ", ".join(f"{k} {v}" for k, v in meta.items() if k not in ("n", "started_at")),
    ]
    notes = [
        f"- {name} {t.k} (nonce {t.nonce}): {t.note}"
        for name, ts in phases.items()
        for t in ts
        if t.note
    ]
    if notes:
        lines += ["", "Notes:", *notes]
    return "\n".join(lines) + "\n"


def write_report(
    run_dir: Path,
    meta: dict[str, Any],
    phases: dict[str, Sequence[Transfer]],
    ended: str,
    check: str,
) -> str:
    run_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "meta": meta,
        "ended": ended,
        "money_check": check,
        "summary": {name: summarize(ts) for name, ts in phases.items() if ts},
        "phases": {name: [asdict(t) for t in ts] for name, ts in phases.items()},
    }
    (run_dir / "report.json").write_text(json.dumps(data, indent=2) + "\n")
    markdown = to_markdown(meta, phases, ended, check)
    (run_dir / "report.md").write_text(markdown)
    return markdown


def describe(
    cfg: Config,
    p: Plan,
    args: argparse.Namespace,
    contract_version: int,
    symbol: str,
    commit: str,
) -> dict[str, Any]:
    return {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "benchmark": "locks",
        "n": args.n,
        "amount": args.amount,
        "symbol": symbol,
        "funder": p.funder.address,
        "service": p.service.address,
        "players": f"indices {p.first} to {p.first + len(p.players) - 1}",
        "network": "Sapphire Testnet",
        "api": cfg.api_url,
        "accounting": cfg.accounting,
        "token_id": cfg.token_id,
        "contract_version": contract_version,
        "sdk": f"oasis-privana {version('oasis-privana')}",
        "bench_commit": commit,
        "chain_rpc": host_of(cfg.rpc_url),
        "poll_s": args.poll,
        "max_wait_s": args.max_wait,
    }
