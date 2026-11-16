"""Locks: N players each lock funds for one service, all at once. The service
then settles every lock back to the funder, first in nonce order a gap apart
without waiting for replies, then the rest one after another.

Sessions: the same writes at the pace of real traffic. The locks go out at random
session starts, and the service settles each lock a play time after it landed,
while later sessions still start.

Wallets come from BENCH_MNEMONIC. Index BENCH_FUNDER_INDEX, 0 unless set, funds
the players and gets the settled funds back, and the next index is the service.
The players are new each run: they start at index 1000 plus the funder's
transfer nonce, which every funding transfer moves on. Nothing else signs for
these wallets, and a signature is valid at one nonce only, so a nonce step on
one of them is our write landing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
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
from .privana import OPEN_S, Privana, Reply
from .transfers import (
    REFUSED_WAIT_S,
    RESENDS,
    Journal,
    Transfer,
    _resent,
    _said,
    check_settled,
    run_transfers,
)

PLAYER_INDEX = 1000
LOCK_TTL_S = 2 * 3600  # settling does not check expiry, so this only bounds a stuck lock
SETTLE_S = 12.0  # two blocks, so reads on a lagging RPC node see the last landings
# Burst locks queue behind Privana's one signer, so a lost reply proves nothing while
# others still land. The longest gap seen between two landings in one burst is 55 s.
QUIET_S = 90.0
LOCK_NONCES = "createLockNonces"
SETTLE_NONCES = "transferLockedNonces"
PHASES = {
    "funding": "Funding, one after another",
    "locks": "Locks, all at once",
    "settles in order": "Settles, in nonce order without waiting",
    "settles": "Settles, one after another",
}
SESSION_PHASES = {
    **PHASES,
    "locks": "Locks, as sessions start",
    "settles in order": "Settles, as sessions end, in nonce order without waiting",
}
# One fixed schedule of session starts, so two runs at the same rate compare.
SCHEDULE_SEED = 1
MONEY_BACK_S = (60, 180, 300)  # ideal, fine and the limit

ReadNonces = Callable[[list[str]], Awaitable[tuple[int, dict[str, int]]]]
More = Callable[[float], Awaitable["list[Action] | None"]]


@dataclass
class Action(Transfer):
    """A write sent in a burst: a Transfer, plus the wallet whose nonce it uses."""

    wallet: str = ""
    block: int | None = None  # the block of the first read that showed it landed
    first_sent_at: float = 0.0  # `sent_at` of its first try: a resend moves `sent_at`
    ended_at: float | None = None  # a session's lock: when the session ended
    back_at: float | None = None  # a session's lock: when its settle showed as landed


def _done(a: Action, last_read: float, refused_wait_s: float) -> bool:
    return a.landed_at is not None or (
        a.reply is not None
        and a.reply.kind == "refused"
        and last_read >= a.reply_at + refused_wait_s
    )


def _lost(a: Action) -> bool:
    """Whether `a` goes out again: its reply was lost, and it has not landed while
    its wallet's nonce is still its own."""
    return (
        a.landed_at is None
        and a.reply is not None
        and a.reply.kind == "unknown"
        and a.seen_nonce == a.nonce
    )


async def burst(
    actions: list[Action],
    send: Callable[[Action], Awaitable[Reply]],
    read_nonces: ReadNonces,
    poll_s: float,
    max_wait_s: float,
    refused_wait_s: float,
    journal: Journal | None = None,
    quiet_s: float | None = None,
    gap_s: float = 0.0,
    open_first: Callable[[Action], Awaitable[object]] | None = None,
    offsets: Sequence[float] | None = None,
    more: More | None = None,
    label: str = "",
) -> None:
    """Send every action at once and read their wallets' nonces until each
    landed or was refused `refused_wait_s` ago, or until `max_wait_s` passed.
    Anything else stays unresolved: it can still land.

    With `gap_s`: action i goes out `i * gap_s` after the start, in list order,
    never waiting for an earlier reply. With `offsets`: `offsets[i]` after the start.

    With `more`: it is called after each read, with the time of the last read that
    succeeded, and returns the actions to send, each with its `due_at` set. A new one
    is added. One with a reply goes out again, the same signed write. The burst then
    ends when it returns None, not when every action is done.

    With `open_first`: it starts for each action OPEN_S before the action goes
    out, so the first one goes out OPEN_S after the start.

    With `quiet_s`: each time none has landed or been answered for that long,
    every action whose reply was lost goes out again, the same signed write.
    A resend that Privana refuses is the action's reply."""
    lead = OPEN_S if open_first is not None else 0.0
    start = time.time()
    for i, a in enumerate(actions):
        a.due_at = start + lead + (offsets[i] if offsets else i * gap_s)
        if journal is not None:
            journal.write("send", k=a.k, nonce=a.nonce, wallet=a.wallet, due_at=a.due_at)

    async def reply_to(a: Action) -> Reply:
        try:
            return await send(a)
        except Exception as exc:
            return Reply("unknown", None, type(exc).__name__)

    async def one(a: Action, wait_s: float) -> None:
        # Counted from the task's start, so time spent on the journal shortens no gap.
        await asyncio.sleep(wait_s)
        if open_first is not None:
            began = time.monotonic()
            await open_first(a)
            await asyncio.sleep(began + lead - time.monotonic())
        a.sent_at = time.time()
        a.reply = await reply_to(a)
        a.reply_at = time.time()
        a.sent_at += a.reply.retry_s  # the try that went out
        a.first_sent_at = a.first_sent_at or a.sent_at

    async def again(a: Action) -> None:
        a.resends += 1
        if journal is not None:
            journal.write("resend", k=a.k, nonce=a.nonce, at=time.time())
        reply = await reply_to(a)
        if journal is not None:
            journal.write("resend reply", k=a.k, nonce=a.nonce, at=time.time(), reply=asdict(reply))
        # A resend that never reached Privana says nothing about the first try.
        if reply.kind == "refused" and reply.status is not None:
            a.reply, a.reply_at = reply, time.time()

    tries = [asyncio.create_task(one(a, a.due_at - lead - start)) for a in actions]
    wallets = sorted({a.wallet for a in actions})
    last_read, news, shown = 0.0, start, (0, 0)
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
                    sum(t.done() for t in tries),
                )
                if counts != shown:
                    shown, news = counts, last_read
                    print(
                        f"  {last_read - start:5.0f} s, block {block}: {label}{counts[0]} of "
                        f"{len(actions)} landed, {counts[1]} of {len(tries)} replied",
                        flush=True,
                    )
            except Exception:
                pass  # a failed read is no news
            if more is None:
                if all(_done(a, last_read, refused_wait_s) for a in actions):
                    break
            else:
                added = await more(last_read)
                if added is None:
                    break
                now = time.time()  # one clock for all, so the journal shortens no gap
                for a in added:
                    if a.reply is None:
                        actions.append(a)
                        record = {"wallet": a.wallet, "due_at": a.due_at}
                    else:
                        a.resends += 1
                        record = {"at": a.due_at, "reply": asdict(a.reply)}
                        a.reply = None
                    if journal is not None:
                        journal.write(
                            "resend" if a.resends else "send", k=a.k, nonce=a.nonce, **record
                        )
                    tries.append(asyncio.create_task(one(a, a.due_at - lead - now)))
            if time.time() >= start + max_wait_s:
                break
            # Quiet: every try is answered, and no read since showed a landing or a reply.
            # Privana joins a resend to a submission it still holds, for up to 10 minutes,
            # so a lost write keeps going out until the watch ends.
            quiet = quiet_s is not None and last_read >= news + quiet_s
            if quiet and shown[1] == len(tries):
                tries += [asyncio.create_task(again(a)) for a in actions if _lost(a)]
            await asyncio.sleep(poll_s)
        await asyncio.gather(*tries)  # every reply is back within 3 SDK timeouts
    finally:
        for t in tries:
            t.cancel()
        for a in actions:
            a.done_at = time.time()
            if a.landed_at is not None:
                a.outcome = "landed"
                if a.reply and a.reply.kind != "ok":
                    a.note = (
                        f"the reply said it failed ({_said(a.reply)}), but it landed{_resent(a)}"
                    )
            elif a.reply and _done(a, last_read, refused_wait_s):
                a.outcome = "refused"
                a.note = _said(a.reply) + _resent(a)
            else:
                reply = f", reply {_said(a.reply)}" if a.reply and a.reply.kind != "ok" else ""
                a.note = f"not landed when the watch ended{reply}{_resent(a)}, so it can still land"
            if journal is not None:
                journal.write("outcome", **asdict(a))


def session_starts(n: int, per_minute: float) -> list[float]:
    """When each of `n` sessions starts, in seconds after the first: random gaps at
    `per_minute` starts a minute on average."""
    schedule = random.Random(SCHEDULE_SEED)
    starts = [0.0]
    for _ in range(n - 1):
        starts.append(starts[-1] + schedule.expovariate(per_minute / 60))
    return starts


def same_funding(amount: str, token_id: str) -> Callable[[dict[str, Any]], bool]:
    """Whether an earlier journal is a locks or sessions run's funding of `amount` of
    `token_id`. Funding at nonce X always pays player PLAYER_INDEX + X, so its write
    at the funder's nonce is the one this run sends first."""
    return lambda start: (
        start.get("benchmark") in ("locks", "sessions")
        and (start.get("amount"), start.get("token_id")) == (amount, token_id)
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
    funded: bool = False,
) -> Plan:
    """Public reads only: the players are new, and no earlier write can still land.
    With `funded` they are the last `n` the funder funded, in a run that stopped
    before they locked."""
    funder, service = wallets(cfg.mnemonic, cfg.funder_index)
    if funded and funder_nonce < n:
        raise PreflightError(f"the funder has funded {funder_nonce} players, fewer than {n}")
    first = PLAYER_INDEX + funder_nonce - (n if funded else 0)
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
    if funded:
        # This run sends no funding, so an unresolved one from an earlier run can still land.
        check_settled(results, funder.address, cfg.accounting, funder_nonce)
    else:
        same = same_funding(amount, cfg.token_id)
        check_settled(results, funder.address, cfg.accounting, funder_nonce, same)
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
    if not args.funded and before < amount * args.n:
        print(f"Not funded: the funder holds {before} units, the run needs {amount * args.n}.")
        return 1

    run_dir = results / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{args.command}"
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

    if not args.funded:
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
                min_gap_s=args.transfer_gap,
            )
        finally:
            journal.close()
        if ended != "completed":
            return f"funding {ended}"
    if args.command == "sessions":
        ended = await _sessions(cfg, args, chain, privana, p, amount, meta, run_dir, phases)
        back = money_back(phases["locks"])
        late = back["sessions"] - back[f"within_{MONEY_BACK_S[-1]}_s"] if back else 0
        if ended == "completed" and late:
            stopped = meta.get("stream_stopped")
            return (
                f"slow: {late} of {len(p.players)} sessions did not have their money back "
                f"within {MONEY_BACK_S[-1] // 60} minutes of ending"
                + (f", after the settle stream stopped at {stopped}" if stopped else "")
            )
        return ended

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
            QUIET_S,
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
    print(
        f"Settling: {len(targets)} locks in nonce order, {args.settle_gap} s apart, "
        f"each on a connection opened {OPEN_S:g} s before."
    )
    in_order = [
        Action(i, p.service_nonce + i, 0.0, wallet=p.service.address) for i in range(len(targets))
    ]
    phases["settles in order"] = in_order
    journal = Journal(run_dir / "settles-in-order.jsonl")
    journal.write("start", **{**meta, "sender": p.service.address})

    try:
        await burst(
            in_order,
            lambda a: privana.settle(
                p.service,
                targets[a.k][0],
                p.funder.address,
                targets[a.k][1],
                amount,
                a.nonce,
                own_connection=True,
            ),
            lambda w: chain.nonces(SETTLE_NONCES, w),
            args.poll,
            args.max_wait,
            REFUSED_WAIT_S,
            journal,
            gap_s=args.settle_gap,
            open_first=_open_first(privana, journal),
        )
    finally:
        journal.close()
    if any(a.outcome == "unresolved" for a in in_order):
        return "stopped after the settles in order: one can still land and would take a nonce"

    rest = [targets[a.k] for a in in_order if a.outcome != "landed"]
    # The landed settles took the burst's first nonces, so each of the rest goes out
    # again at its burst nonce, the same signed write.
    start = p.service_nonce + len(in_order) - len(rest)
    missing = len(p.players) - len(locked)
    return await _rest(args, privana, p, amount, meta, run_dir, phases, rest, start, missing)


def _open_first(privana: Privana, journal: Journal) -> Callable[[Action], Awaitable[None]]:
    """Open the connection for a settle before it goes out, and journal whether it opened."""

    async def open_first(a: Action) -> None:
        opened = await privana.open_connection(a.wallet, a.nonce)
        journal.write("open", k=a.k, nonce=a.nonce, opened=opened)
        if not opened:
            print(f"  settle {a.k + 1}: no open connection in time, so it opens a new one")

    return open_first


async def _rest(
    args: argparse.Namespace,
    privana: Privana,
    p: Plan,
    amount: int,
    meta: dict[str, Any],
    run_dir: Path,
    phases: dict[str, Sequence[Transfer]],
    rest: list[tuple[str, int]],
    start: int,
    missing: int,
) -> str:
    """Settle `rest` one after another from the service's nonce `start`."""
    ended = "completed"
    if rest:
        print(f"Settling the other {len(rest)} one after another.")

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
    if ended == "completed" and missing:
        return f"partial: {missing} of {len(p.players)} locks did not land, see the notes"
    return ended


async def _sessions(
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
    """The players lock at random session starts, and the service settles each lock
    `--play` seconds after it landed, while later sessions still start."""
    token = cfg.token_id
    starts = session_starts(len(p.players), args.per_minute)
    print(f"Sessions: {len(p.players)} start over {starts[-1]:.0f} s.")
    locks = [
        Action(i, p.lock_nonces[w.address], 0.0, wallet=w.address) for i, w in enumerate(p.players)
    ]
    phases["locks"] = locks
    in_order: list[Action] = []
    phases["settles in order"] = in_order
    targets: list[tuple[str, int]] = []  # settle i moves targets[i], the lock of player queued[i]
    queued: list[int] = []
    as_head: Counter[int] = Counter()  # by nonce: resends it started as the first unused one

    async def queue(due: list[Action]) -> None:
        found = await chain.service_locks(p.service, [p.players[a.k].address for a in due])
        for a in due:
            user = p.players[a.k].address
            # A read on a node behind shows no lock yet, so that one waits for the next read.
            if len(found[user]) == 1 and found[user][0][1] == amount:
                targets.append((user, found[user][0][0]))
                queued.append(a.k)

    async def ended_sessions(last_read: float) -> list[Action] | None:
        """The settles to send now: those of the sessions that ended since the last
        read, or the first unused one and every one behind it again. None once the
        stream is over."""
        head = next((a for a in in_order if a.landed_at is None), None)
        if head and head.reply and head.reply.kind != "ok":
            # The head's reply came back, or was lost, without it landing. The contract
            # takes a service's settles in nonce order only, so nothing behind it lands.
            # Once a read this long after the reply still shows the nonce unused, the head
            # and every settle behind it go out again in order, the same signed writes.
            unused = head.seen_nonce == head.nonce
            behind = [a for a in in_order if a.landed_at is None]
            if (
                not unused
                or last_read < head.reply_at + REFUSED_WAIT_S
                or any(a.reply is None for a in behind)  # one of them is still out
            ):
                return []
            said = f"settle {head.k} (nonce {head.nonce}): {_said(head.reply)}"
            if as_head[head.nonce] == RESENDS:
                meta["stream_stopped"] = said
                return None
            as_head[head.nonce] += 1
            meta.setdefault("sent_again", []).append(said)
            for i, a in enumerate(behind):
                a.due_at = time.time() + OPEN_S + i * args.settle_gap
            return behind
        landed = [a for a in locks if a.landed_at is not None]
        due = [a for a in landed if a.k not in queued and time.time() >= a.landed_at + args.play]
        if not due:
            over = locking.done() and head is None and len(queued) == len(landed)
            return None if over else []
        try:
            await queue(due)
        except Exception:
            return []  # a failed read is no news
        added: list[Action] = []
        for i in range(len(in_order), len(targets)):
            earlier = (added or in_order)[-1].due_at if added or in_order else 0.0
            due_at = max(time.time() + OPEN_S, earlier + args.settle_gap)
            added.append(Action(i, p.service_nonce + i, due_at, wallet=p.service.address))
        return added

    _, settle_nonces = await chain.nonces(SETTLE_NONCES, [p.service.address])
    if settle_nonces[p.service.address] != p.service_nonce:
        return (
            "stopped before the sessions: the service's settle nonce moved, so another signer "
            "uses it"
        )
    expiry = int(time.time()) + LOCK_TTL_S
    lock_journal = Journal(run_dir / "locks.jsonl")
    lock_journal.write("start", **meta, expiry=expiry)
    journal = Journal(run_dir / "settles-in-order.jsonl")
    journal.write("start", **{**meta, "sender": p.service.address})

    locking = asyncio.create_task(
        burst(
            locks,
            lambda a: privana.lock(
                p.players[a.k], p.service.address, token, amount, expiry, a.nonce
            ),
            lambda w: chain.nonces(LOCK_NONCES, w),
            args.poll,
            args.max_wait,
            REFUSED_WAIT_S,
            lock_journal,
            QUIET_S,
            offsets=starts,
            label="locks, ",
        )
    )
    try:
        try:
            await burst(
                in_order,
                lambda a: privana.settle(
                    p.service,
                    targets[a.k][0],
                    p.funder.address,
                    targets[a.k][1],
                    amount,
                    a.nonce,
                    own_connection=True,
                ),
                # The stream starts empty, so the read names the service itself.
                lambda w: chain.nonces(SETTLE_NONCES, [p.service.address]),
                args.poll,
                args.max_wait,
                REFUSED_WAIT_S,
                journal,
                open_first=_open_first(privana, journal),
                more=ended_sessions,
                label="settles, ",
            )
            await locking
        finally:
            locking.cancel()
            await asyncio.gather(locking, return_exceptions=True)
            lock_journal.close()
            journal.close()

        landed = [a for a in locks if a.outcome == "landed"]
        if not landed:
            return "stopped after locking: no lock landed"
        left = [a for a in landed if a.k not in queued]
        if left:
            # The stream stopped before these sessions were settled. Each still ends first.
            last = max(a.landed_at or 0.0 for a in left) + args.play
            await asyncio.sleep(max(SETTLE_S, last - time.time()))
            await queue(left)
        if len(queued) != len(landed):
            return "stopped after the sessions: the service's read does not show every landed lock"
        # A settle the stream gave up on can land after it, so a read says how many landed.
        # A node behind shows fewer, so the count never goes below what the stream saw.
        seen = sum(a.landed_at is not None for a in in_order)
        api_nonce = await privana.transfer_locked_nonce(p.service.address)
        done = max(seen, api_nonce - p.service_nonce)
        if not 0 <= done <= len(in_order):
            return (
                "stopped after the sessions: the service's settle nonce moved, so another "
                "signer uses it"
            )
        for a in in_order[:done]:
            if a.landed_at is None:
                a.landed_at, a.outcome = time.time(), "landed"
                a.note = f"landed after the stream stopped{_resent(a)}"
        # Each of the rest goes out at its nonce: for a settle the stream already sent, that
        # is the same signed write again, so at most one copy lands.
        return await _rest(
            args,
            privana,
            p,
            amount,
            meta,
            run_dir,
            phases,
            targets[done:],
            p.service_nonce + done,
            len(p.players) - len(landed),
        )
    finally:
        back = {t.nonce: t.landed_at for t in (*in_order, *phases["settles"]) if t.landed_at}
        for i, k in enumerate(queued):
            locks[k].ended_at = (locks[k].landed_at or 0.0) + args.play
            locks[k].back_at = back.get(p.service_nonce + i)


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
    # A sessions run sends an unresolved ordered settle again one after another.
    resent = {t.nonce for t in phases["settles"] if t.outcome == "landed"}
    if any(
        t.outcome == "unresolved" and not (name == "settles in order" and t.nonce in resent)
        for name, ts in phases.items()
        for t in ts
    ):
        return "skipped: an unresolved write can still land"

    def landed(name: str) -> int:
        return sum(t.outcome == "landed" for t in phases[name])

    expected = amount * (landed("settles in order") + landed("settles") - landed("funding"))
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
    sends = [getattr(t, "first_sent_at", 0.0) or t.sent_at for t in ts if t.sent_at]
    landings = [t.landed_at for t in landed if t.landed_at is not None]
    span = max(landings) - min(sends) if landings else None
    unresent = [t.landed_at for t in landed if t.landed_at is not None and not t.resends]
    unresolved = sum(t.outcome == "unresolved" for t in ts)
    return {
        "sent": len(ts),
        "landed": len(landed),
        "landed_without_resend": len(unresent),
        "last_landing_without_resend_s": _round(max(unresent) - min(sends) if unresent else None),
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


def money_back(locks: Sequence[Transfer]) -> dict[str, Any] | None:
    """How long after a session ended its settle showed as landed, for a sessions run.
    A session whose lock never landed has no end, so it counts as not back."""
    ended = [a for a in locks if isinstance(a, Action) and a.ended_at is not None]
    if not ended:
        return None
    waits = sorted(a.back_at - a.ended_at for a in ended if a.ended_at and a.back_at)
    lock_waits = [a.landed_at - a.due_at for a in locks if a.landed_at]
    return {
        "lock_wait_median_s": _round(statistics.median(lock_waits)),
        "lock_wait_slowest_s": _round(max(lock_waits)),
        "sessions": len(locks),
        "back": len(waits),
        **{f"within_{limit}_s": sum(w <= limit for w in waits) for limit in MONEY_BACK_S},
        "median_s": _round(statistics.median(waits)) if waits else None,
        "slowest_s": _round(waits[-1]) if waits else None,
    }


def to_markdown(
    meta: dict[str, Any], phases: dict[str, Sequence[Transfer]], ended: str, check: str
) -> str:
    def cell(value: Any, unit: str = "") -> str:
        return "n/a" if value is None else f"{value}{unit}"

    sessions = meta.get("benchmark") == "sessions"
    labels = SESSION_PHASES if sessions else PHASES
    summaries = {name: summarize(ts) for name, ts in phases.items() if ts}
    verdict = "PASS" if ended == "completed" and check == "ok" else "FAIL"
    who = "players funded by an earlier run" if meta.get("funded") else "new players"
    lines = [
        f"# {'Sessions' if sessions else 'Locks'}, N = {meta['n']}: {verdict}",
        "",
        f"{meta['started_at']}: {meta['n']} {who} each lock {meta['amount']} "
        f"{meta['symbol']} for the service {meta['service']}, which settles them back to the "
        f"funder {meta['funder']}.",
        "",
        "| Phase | Sent | Landed | Refused | Unresolved | First send to last landing "
        "| Landed per minute | Slowest reply |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, s in summaries.items():
        lines.append(
            f"| {labels[name]} | {s['sent']} | {s['landed']} | {s['refused']} | {s['unresolved']} "
            f"| {cell(s['first_send_to_last_landing_s'], ' s')} | {cell(s['landed_per_minute'])} "
            f"| {cell(s['slowest_reply_s'], ' s')} |"
        )
    lines += [
        "",
        "Landed without a resend: "
        + "; ".join(
            f"{n} {s['landed_without_resend']} in {cell(s['last_landing_without_resend_s'], ' s')}"
            for n, s in summaries.items()
        )
        + ".",
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
    ]
    back = money_back(phases["locks"])
    if back:
        within = ", ".join(
            f"{back[f'within_{limit}_s']} within {limit // 60} min" for limit in MONEY_BACK_S
        )
        lines.append(
            f"Lock landed after its session started: median {back['lock_wait_median_s']} s, "
            f"slowest {back['lock_wait_slowest_s']} s."
        )
        lines.append(
            f"Money back after a session ended: {back['back']} of {back['sessions']} sessions, "
            f"{within}, median {cell(back['median_s'], ' s')}, slowest "
            f"{cell(back['slowest_s'], ' s')}. It counts from {meta['play_s']:g} s after the "
            "lock showed as landed to the read that showed the settle landed."
        )
    lines += [
        f"Sent again with the settles behind it, after it did not land: {said}."
        for said in meta.get("sent_again", [])
    ]
    if meta.get("stream_stopped"):
        lines.append(
            f"The settle stream stopped after {RESENDS} resends at {meta['stream_stopped']}. "
            "The settles left went out one after another."
        )
    lines += [
        "",
        f"Landing times come from nonce reads every {meta['poll_s']} s, not block times.",
        "",
        "Setup: "
        + ", ".join(
            f"{k} {v}"
            for k, v in meta.items()
            if k not in ("n", "started_at", "sent_again", "stream_stopped")
        ),
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
        "money_back": money_back(phases["locks"]),
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
    network: str,
    contract_version: int,
    symbol: str,
    commit: str,
) -> dict[str, Any]:
    sessions = (
        {"per_minute": args.per_minute, "play_s": args.play} if args.command == "sessions" else {}
    )
    return {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "benchmark": args.command,
        "n": args.n,
        "amount": args.amount,
        "funded": args.funded,
        "symbol": symbol,
        "funder": p.funder.address,
        "service": p.service.address,
        "players": f"indices {p.first} to {p.first + len(p.players) - 1}",
        "network": network,
        "api": cfg.api_url,
        "accounting": cfg.accounting,
        "token_id": cfg.token_id,
        "contract_version": contract_version,
        "sdk": f"oasis-privana {version('oasis-privana')}",
        "bench_commit": commit,
        "chain_rpc": host_of(cfg.rpc_url),
        "poll_s": args.poll,
        "max_wait_s": args.max_wait,
        "transfer_gap_s": args.transfer_gap,
        "settle_gap_s": args.settle_gap,
        **sessions,
    }
