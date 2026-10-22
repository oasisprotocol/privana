"""One wallet sends N transfers in a row to a second wallet.

Each transfer is written to the journal before it is sent, signed at the nonce
Privana shows as current. A transfer whose reply was lost goes out again, the
same signed write, while that nonce stays unused. The next one waits until that
nonce shows as used, and the run stops at the first transfer that does not
land, so no transfer is ever re-signed at another nonce. The sender is a test
wallet nothing else signs for, so a nonce step is our transfer. The balance
check at the end confirms it.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from .config import PreflightError
from .privana import Reply

Outcome = Literal["landed", "refused", "unresolved"]
Send = Callable[[int], Awaitable[Reply]]
ReadNonce = Callable[[], Awaitable[int]]

# A few 4xx replies come after the enclave already sent the transaction, so a
# refusal stands only if a nonce read this long after the reply shows it unused.
REFUSED_WAIT_S = 30.0
# A lost reply can be a request that never reached Privana, so the same write goes
# out again while its nonce shows unused this long after the last try. A nonce
# takes one write, so at most one copy lands.
RESEND_AFTER_S = 30.0
RESENDS = 3


@dataclass
class Transfer:
    k: int
    nonce: int
    due_at: float
    sent_at: float = 0.0
    reply: Reply | None = None
    reply_at: float = 0.0
    landed_at: float | None = None  # the first nonce read that showed this nonce used
    seen_nonce: int | None = None  # the last nonce read
    done_at: float = 0.0  # when the run moved on from it, or stopped
    outcome: Outcome = "unresolved"
    note: str = ""
    resends: int = 0  # sends of the same write after a lost reply


class Journal:
    """Append-only JSON lines, on disk before the caller moves on. It holds what
    was sent and what happened, never a signature or a token."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("x", encoding="utf-8")  # one run per journal

    def write(self, event: str, **fields: Any) -> None:
        self._file.write(json.dumps({"event": event, **fields}) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        self._file.close()


def unsettled(
    results: Path,
    sender: str,
    accounting: str,
    nonce: int,
    same: Callable[[dict[str, Any]], bool] = lambda start: False,
) -> list[tuple[str, int, int]]:
    """Writes in earlier runs' journals that can still land: sent by `sender`
    to `accounting`, without a landed or refused outcome, at a nonce not used
    yet. A write at `nonce` from a run whose start passes `same` is the write
    this run sends first, so it is left out. Returns (journal, k, nonce) for each."""
    found: list[tuple[str, int, int]] = []
    for path in sorted(results.glob("*/*.jsonl")):
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # torn as the run died: a send never went out, an outcome stays missing
        start = records[0] if records else {}
        owner = (start.get("event"), start.get("sender"), start.get("accounting"))
        if owner != ("start", sender, accounting):
            continue
        settled = {
            r["k"] for r in records if r["event"] == "outcome" and r["outcome"] != "unresolved"
        }
        found += [
            (f"{path.parent.name}/{path.name}", r["k"], r["nonce"])
            for r in records
            if r["event"] == "send"
            and r["k"] not in settled
            and r["nonce"] >= nonce
            and not (r["nonce"] == nonce and same(start))
        ]
    return found


def check_settled(
    results: Path,
    sender: str,
    accounting: str,
    nonce: int,
    same: Callable[[dict[str, Any]], bool] = lambda start: False,
) -> None:
    """Refuse to start while an earlier run left a write from `sender` that can
    still land, since this run would reuse its nonce for another write."""
    pending = unsettled(results, sender, accounting, nonce, same)
    if pending:
        journal, k, at = pending[0]
        raise PreflightError(
            f"results/{journal} left write {k} from {sender} at nonce {at} unresolved and "
            "that nonce is not used yet, so it can still land and this run would reuse it. "
            f"Rerun once the nonce passes {at}, or move that run folder out of results/ "
            "if it will never land."
        )


async def _look(t: Transfer, read_nonce: ReadNonce) -> bool:
    """Read the nonce once and record it on `t`. Returns whether the read
    succeeded. A failed read is no news, never a verdict."""
    try:
        seen = await read_nonce()
    except Exception:
        return False
    t.seen_nonce = seen
    if seen > t.nonce and t.landed_at is None:
        t.landed_at = time.time()
    return True


async def _watch(t: Transfer, read_nonce: ReadNonce, deadline: float, poll_s: float) -> None:
    """Poll until `t.nonce` shows as used or the deadline passes. Each read is
    recorded on `t`, so it survives a cancel."""
    while True:
        await _look(t, read_nonce)
        if t.landed_at is not None or time.time() >= deadline:
            return
        await asyncio.sleep(poll_s)


async def _resend(
    t: Transfer, send: Send, watch: asyncio.Task[None], journal: Journal, after_s: float
) -> None:
    """Send `t` again, up to RESENDS times, each time its nonce still shows unused
    `after_s` after the last try. Returns once the watch ends."""
    while t.resends < RESENDS:
        done, _ = await asyncio.wait({watch}, timeout=after_s)
        if done:
            return
        if t.seen_nonce != t.nonce:
            continue  # a node behind proves nothing
        t.resends += 1
        journal.write("resend", k=t.k, nonce=t.nonce, at=time.time())
        try:
            reply = await send(t.nonce)
        except Exception as exc:
            reply = Reply("unknown", None, type(exc).__name__)
        journal.write("resend reply", k=t.k, nonce=t.nonce, at=time.time(), reply=asdict(reply))


def _said(reply: Reply) -> str:
    return " ".join(str(part) for part in (reply.status, reply.detail) if part) or reply.kind


def _resent(t: Transfer) -> str:
    return f", resent {t.resends} time{'s' if t.resends > 1 else ''}" if t.resends else ""


async def run_transfers(
    n: int,
    start_nonce: int,
    send: Send,
    read_nonce: ReadNonce,
    journal: Journal,
    transfers: list[Transfer],
    poll_s: float,
    max_wait_s: float,
    refused_wait_s: float = REFUSED_WAIT_S,
    progress: Callable[[Transfer], None] = lambda t: None,
    resend_after_s: float = RESEND_AFTER_S,
) -> str:
    """Send up to `n` transfers from `start_nonce`, the nonce the run expects,
    appending each to `transfers` as it starts. Returns why the run ended."""
    nonce = await read_nonce()
    if nonce != start_nonce:
        return (
            f"stopped before sending: the API shows nonce {nonce} but the run expected "
            f"{start_nonce}, so its view lags or something else used this wallet"
        )
    for k in range(n):
        t = Transfer(k=k, nonce=nonce, due_at=time.time())
        transfers.append(t)
        journal.write("send", k=k, nonce=nonce, due_at=t.due_at)
        t.sent_at = time.time()
        watch = asyncio.create_task(_watch(t, read_nonce, t.sent_at + max_wait_s, poll_s))
        try:
            try:
                t.reply = await send(nonce)
            except Exception as exc:
                t.reply = Reply("unknown", None, type(exc).__name__)
            t.reply_at = time.time()
            t.sent_at += t.reply.retry_s  # the try that went out
            refused = t.reply.kind == "refused"
            if t.reply.kind == "unknown":
                await _resend(t, send, watch, journal, resend_after_s)
            await asyncio.wait({watch}, timeout=refused_wait_s if refused else None)
            watch.cancel()  # no read of it lands on `t` after this
            confirmed = False
            if refused and t.landed_at is None:
                # A refusal stands only on a read this long after the reply that shows
                # exactly this nonce. A lower one is a node behind and proves nothing.
                await asyncio.sleep(t.reply_at + refused_wait_s - time.time())
                confirmed = await _look(t, read_nonce) and t.seen_nonce == t.nonce
        finally:
            watch.cancel()
            t.done_at = time.time()

        if t.landed_at is not None:
            t.outcome = "landed"
            if t.reply.kind != "ok":
                t.note = f"the reply said it failed ({_said(t.reply)}), but it landed{_resent(t)}"
        elif confirmed:
            t.outcome = "refused"
            t.note = _said(t.reply)
        else:
            seen = (
                "no nonce read succeeded"
                if t.seen_nonce is None
                else f"the last nonce read showed {t.seen_nonce}"
            )
            reply = "" if t.reply.kind == "ok" else f", reply {_said(t.reply)}"
            t.note = (
                f"after {t.done_at - t.sent_at:.0f} s {seen}{reply}{_resent(t)}, "
                "so it can still land"
            )
        journal.write("outcome", **asdict(t))
        progress(t)

        if t.outcome != "landed":
            return f"stopped at transfer {k}: {t.outcome} ({t.note})"
        if t.seen_nonce != nonce + 1:
            return (
                f"stopped at transfer {k}: the nonce moved from {nonce} to {t.seen_nonce}, "
                "so something else used this wallet"
            )
        nonce += 1
    return "completed"
