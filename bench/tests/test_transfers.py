"""The transfers engine against a fake API, on real time scaled down to milliseconds."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from privana_bench.privana import Reply
from privana_bench.transfers import Journal, Transfer, run_transfers, unsettled

START = 7
POLL = 0.01
REPLY = 0.02  # how long the fake API takes to answer
LAND = 0.05  # how long a transfer takes to land
MAX_WAIT = 0.3
REFUSED_WAIT = 0.1
RESEND_AFTER = 0.08

REPLIES = {
    "ok": Reply("ok", 200),
    "unknown": Reply("unknown", 500, "Failed to submit transaction"),
    "refused": Reply("refused", 400, "Transfer nonce mismatch"),
    "ok after a connection that never opened": Reply("ok", 200, connect_failures=1, retry_s=0.2),
}


@dataclass
class Step:
    reply: str  # ok, unknown or refused
    land_after: float | None = LAND  # None: it never lands
    jump: int = 1  # how far the nonce moves when it lands
    raises: bool = False
    reply_after: float = REPLY
    lands_on_try: int = 1  # earlier tries are lost on the way in


@dataclass
class World:
    nonce: int = START
    landings: list[tuple[float, int]] = field(default_factory=list)
    sent: list[int] = field(default_factory=list)
    reads: int = 0
    failing_reads: set[int] = field(default_factory=set)
    watch_reads_fail: bool = False  # only reads from the run's own task succeed
    run_task: asyncio.Task[Any] | None = None

    async def read_nonce(self) -> int:
        await asyncio.sleep(0)
        self.reads += 1
        if self.reads in self.failing_reads:
            raise RuntimeError("read failed")
        if self.watch_reads_fail and asyncio.current_task() is not self.run_task:
            raise RuntimeError("read failed")
        now = time.time()
        for when, nonce in self.landings:
            if now >= when:
                self.nonce = max(self.nonce, nonce)
        self.landings = [(when, nonce) for when, nonce in self.landings if now < when]
        return self.nonce


@dataclass
class Run:
    world: World
    transfers: list[Transfer]
    ended: str
    records: list[dict[str, Any]]
    elapsed: float


async def run(
    tmp_path: Path, steps: list[Step], world: World | None = None, max_wait: float = MAX_WAIT
) -> Run:
    world = world or World()
    world.run_task = asyncio.current_task()
    path = tmp_path / "journal.jsonl"
    journal = Journal(path)

    async def send(nonce: int) -> Reply:
        k = nonce - START
        world.sent.append(nonce)
        tries = world.sent.count(nonce)
        last = json.loads(path.read_text().splitlines()[-1])
        expected = ("send" if tries == 1 else "resend", k, nonce)
        assert (last["event"], last["k"], last["nonce"]) == expected, "journal first"
        step = steps[k]
        if step.land_after is not None and tries == step.lands_on_try:
            world.landings.append((time.time() + step.land_after, nonce + step.jump))
        await asyncio.sleep(step.reply_after)
        if step.raises:
            raise RuntimeError("boom")
        return REPLIES[step.reply]

    transfers: list[Transfer] = []
    started = time.time()
    ended = await run_transfers(
        len(steps),
        START,
        send,
        world.read_nonce,
        journal,
        transfers,
        POLL,
        max_wait,
        REFUSED_WAIT,
        resend_after_s=RESEND_AFTER,
    )
    elapsed = time.time() - started
    journal.close()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    return Run(world, transfers, ended, records, elapsed)


async def test_all_land_in_order(tmp_path: Path) -> None:
    r = await run(tmp_path, [Step("ok")] * 4)
    assert r.ended == "completed"
    assert r.world.sent == [7, 8, 9, 10]
    assert [t.outcome for t in r.transfers] == ["landed"] * 4
    assert [t.seen_nonce for t in r.transfers] == [8, 9, 10, 11]
    for t in r.transfers:
        assert t.landed_at is not None
        assert LAND <= t.landed_at - t.due_at < LAND + 0.1
        assert t.reply_at <= t.done_at and t.landed_at <= t.done_at
    for t, after in zip(r.transfers, r.transfers[1:], strict=False):
        assert t.done_at <= after.due_at
    assert [rec["event"] for rec in r.records] == ["send", "outcome"] * 4


async def test_landing_is_seen_while_the_reply_is_pending(tmp_path: Path) -> None:
    r = await run(tmp_path, [Step("ok", land_after=0.0)])
    t = r.transfers[0]
    assert t.landed_at is not None and t.landed_at < t.reply_at


async def test_failed_reply_that_landed_counts_as_landed(tmp_path: Path) -> None:
    r = await run(tmp_path, [Step("unknown"), Step("ok")])
    assert r.ended == "completed"
    assert r.transfers[0].outcome == "landed"
    assert r.transfers[0].note == (
        "the reply said it failed (500 Failed to submit transaction), but it landed"
    )
    assert r.world.sent == [7, 8]


async def test_unknown_that_never_lands_stops_without_resigning(tmp_path: Path) -> None:
    steps = [Step("ok"), Step("unknown", land_after=None), Step("ok")]
    r = await run(tmp_path, steps, max_wait=0.6)
    assert r.world.sent == [7, 8, 8, 8, 8]
    assert r.transfers[1].outcome == "unresolved"
    assert r.transfers[1].note == (
        "after 1 s the last nonce read showed 8, reply 500 Failed to submit transaction, "
        "resent 3 times, so it can still land"
    )
    assert r.ended.startswith("stopped at transfer 1: unresolved")
    assert r.transfers[1].done_at - r.transfers[1].sent_at >= 0.6
    assert r.records[-1]["outcome"] == "unresolved"


async def test_lost_write_goes_out_again_and_lands(tmp_path: Path) -> None:
    r = await run(tmp_path, [Step("unknown", lands_on_try=2), Step("ok")])
    assert r.ended == "completed"
    assert r.world.sent == [7, 7, 8]
    t = r.transfers[0]
    assert (t.outcome, t.resends) == ("landed", 1)
    assert t.note == (
        "the reply said it failed (500 Failed to submit transaction), but it landed, resent 1 time"
    )
    events = [rec["event"] for rec in r.records]
    assert events == ["send", "resend", "resend reply", "outcome", "send", "outcome"]
    assert (r.records[2]["nonce"], r.records[2]["reply"]["kind"]) == (7, "unknown")


async def test_no_resend_while_a_node_is_behind(tmp_path: Path) -> None:
    run_task = asyncio.current_task()

    async def read_nonce() -> int:
        await asyncio.sleep(0)
        return START if asyncio.current_task() is run_task else START - 1

    sent: list[int] = []

    async def send(nonce: int) -> Reply:
        sent.append(nonce)
        return REPLIES["unknown"]

    journal = Journal(tmp_path / "journal.jsonl")
    transfers: list[Transfer] = []
    await run_transfers(
        1,
        START,
        send,
        read_nonce,
        journal,
        transfers,
        POLL,
        MAX_WAIT,
        REFUSED_WAIT,
        resend_after_s=RESEND_AFTER,
    )
    journal.close()
    assert sent == [START]
    assert (transfers[0].outcome, transfers[0].resends) == ("unresolved", 0)


async def test_ok_reply_that_never_lands_is_unresolved(tmp_path: Path) -> None:
    r = await run(tmp_path, [Step("ok", land_after=None), Step("ok")])
    assert r.world.sent == [7]
    assert r.transfers[0].outcome == "unresolved"
    assert r.transfers[0].note == "after 0 s the last nonce read showed 7, so it can still land"


async def test_no_successful_read_is_said_so(tmp_path: Path) -> None:
    world = World(failing_reads=set(range(2, 10_000)))
    r = await run(tmp_path, [Step("ok", land_after=None)], world)
    assert r.transfers[0].outcome == "unresolved"
    assert r.transfers[0].note == "after 0 s no nonce read succeeded, so it can still land"


async def test_cancel_mid_send_stops_the_watch(tmp_path: Path) -> None:
    world = World()

    async def slow_send(nonce: int) -> Reply:
        await asyncio.sleep(10)
        return REPLIES["ok"]

    journal = Journal(tmp_path / "journal.jsonl")
    transfers: list[Transfer] = []
    task = asyncio.create_task(
        run_transfers(
            1, START, slow_send, world.read_nonce, journal, transfers, POLL, MAX_WAIT, REFUSED_WAIT
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.01)
    assert asyncio.all_tasks() == {asyncio.current_task()}
    # The stall counts in the window. The loop's clock is not time.time(), so allow a little.
    assert transfers[0].done_at - transfers[0].sent_at >= 0.04
    assert transfers[0].outcome == "unresolved"
    journal.close()


async def test_refused_stops_after_a_short_watch(tmp_path: Path) -> None:
    r = await run(tmp_path, [Step("refused", land_after=None), Step("ok")])
    assert r.world.sent == [7]
    assert r.transfers[0].outcome == "refused"
    assert r.transfers[0].landed_at is None
    assert r.ended == "stopped at transfer 0: refused (400 Transfer nonce mismatch)"
    assert REFUSED_WAIT <= r.elapsed < MAX_WAIT


async def test_refusal_without_a_later_read_is_unresolved(tmp_path: Path) -> None:
    # The transfer went out despite the 400, and no read after the reply succeeds.
    world = World(failing_reads=set(range(2, 10_000)))
    r = await run(tmp_path, [Step("refused"), Step("ok")], world)
    assert r.world.sent == [7]
    assert r.transfers[0].outcome == "unresolved"
    assert r.transfers[0].note == (
        "after 0 s no nonce read succeeded, reply 400 Transfer nonce mismatch, so it can still land"
    )
    assert r.ended.startswith("stopped at transfer 0: unresolved")


async def test_refusal_stands_on_the_read_after_the_wait(tmp_path: Path) -> None:
    world = World(watch_reads_fail=True)
    r = await run(tmp_path, [Step("refused", land_after=None)], world)
    assert r.transfers[0].outcome == "refused"
    assert r.transfers[0].seen_nonce == 7


async def test_refusal_waits_its_full_time_after_a_slow_reply(tmp_path: Path) -> None:
    # The watch's deadline passes before the refused wait ends.
    slow = MAX_WAIT - REFUSED_WAIT / 2
    r = await run(tmp_path, [Step("refused", land_after=None, reply_after=slow)])
    t = r.transfers[0]
    assert t.outcome == "refused"
    assert t.done_at - t.reply_at >= REFUSED_WAIT


@pytest.mark.parametrize("decider", [START - 1, None], ids=["node behind", "read failed"])
async def test_refusal_needs_the_read_after_the_wait(tmp_path: Path, decider: int | None) -> None:
    # The watch saw the nonce unused, but only the read after the wait counts.
    run_task = asyncio.current_task()
    run_reads = iter([START, decider])  # the start, then the read that decides

    async def read_nonce() -> int:
        await asyncio.sleep(0)
        seen = next(run_reads) if asyncio.current_task() is run_task else START
        if seen is None:
            raise RuntimeError("read failed")
        return seen

    async def send(nonce: int) -> Reply:
        return REPLIES["refused"]

    journal = Journal(tmp_path / "journal.jsonl")
    transfers: list[Transfer] = []
    ended = await run_transfers(
        1, START, send, read_nonce, journal, transfers, POLL, MAX_WAIT, REFUSED_WAIT
    )
    journal.close()
    assert transfers[0].outcome == "unresolved"
    assert ended.startswith("stopped at transfer 0: unresolved")


@pytest.mark.parametrize("shown", [START - 1, START + 1])
async def test_start_nonce_other_than_preflight_sends_nothing(tmp_path: Path, shown: int) -> None:
    r = await run(tmp_path, [Step("ok")], World(nonce=shown))
    assert r.world.sent == [] and r.transfers == []
    assert r.ended == (
        f"stopped before sending: the API shows nonce {shown} but the run expected {START}, "
        "so its view lags or something else used this wallet"
    )


def test_journal_holds_one_run(tmp_path: Path) -> None:
    Journal(tmp_path / "journal.jsonl").close()
    with pytest.raises(FileExistsError):
        Journal(tmp_path / "journal.jsonl")


async def test_read_after_the_wait_can_find_a_landing(tmp_path: Path) -> None:
    world = World(watch_reads_fail=True)
    r = await run(tmp_path, [Step("refused"), Step("ok", land_after=0.0)], world)
    assert r.transfers[0].outcome == "landed"
    assert r.transfers[0].note.startswith("the reply said it failed (400")


async def test_refused_reply_that_landed_counts_as_landed(tmp_path: Path) -> None:
    # Privana answers 400 to a few enclave replies it cannot parse, after the transaction went out.
    r = await run(tmp_path, [Step("refused"), Step("ok")])
    assert r.ended == "completed"
    assert r.transfers[0].outcome == "landed"
    assert r.transfers[0].note.startswith("the reply said it failed (400")


async def test_nonce_jump_means_another_signer(tmp_path: Path) -> None:
    r = await run(tmp_path, [Step("ok", jump=2), Step("ok")])
    assert r.world.sent == [7]
    assert r.transfers[0].outcome == "landed"
    assert "moved from 7 to 9" in r.ended


async def test_send_that_raises_is_unknown(tmp_path: Path) -> None:
    r = await run(tmp_path, [Step("ok", raises=True)])
    assert r.transfers[0].reply == Reply("unknown", None, "RuntimeError")
    assert r.transfers[0].outcome == "landed"
    assert r.ended == "completed"


async def test_failed_reads_are_no_news(tmp_path: Path) -> None:
    world = World(failing_reads={2, 3, 4})
    r = await run(tmp_path, [Step("ok"), Step("ok")], world)
    assert r.ended == "completed"
    assert [t.outcome for t in r.transfers] == ["landed", "landed"]


async def test_reply_time_counts_from_the_try_that_went_out(tmp_path: Path) -> None:
    r = await run(tmp_path, [Step("ok after a connection that never opened", reply_after=0.2)])
    t = r.transfers[0]
    assert t.outcome == "landed"
    assert t.reply_at - t.sent_at < 0.1


async def test_journal_holds_no_signature(tmp_path: Path) -> None:
    r = await run(tmp_path, [Step("ok")])
    assert set(r.records[0]) == {"event", "k", "nonce", "due_at"}
    assert set(r.records[1]["reply"]) == {"kind", "status", "detail", "connect_failures", "retry_s"}
    assert "signature" not in json.dumps(r.records)


def write_journal(run_dir: Path, sender: str, *events: dict[str, Any]) -> None:
    start = {"event": "start", "sender": sender, "accounting": "0xA"}
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in (start, *events)))


def send(k: int, nonce: int) -> dict[str, Any]:
    return {"event": "send", "k": k, "nonce": nonce}


def outcome(k: int, result: str) -> dict[str, Any]:
    return {"event": "outcome", "k": k, "outcome": result}


def test_unsettled_finds_transfers_that_can_still_land(tmp_path: Path) -> None:
    landed_then_unresolved = (
        send(0, 5),
        outcome(0, "landed"),
        send(1, 6),
        outcome(1, "unresolved"),
    )
    write_journal(tmp_path / "1-transfers", "0xS", *landed_then_unresolved)
    write_journal(tmp_path / "2-transfers", "0xS", send(0, 7))
    with (tmp_path / "2-transfers" / "journal.jsonl").open("a") as journal:
        journal.write('{"event": "outcome", "k": 0, "out')  # torn as the run died
    write_journal(tmp_path / "3-transfers", "0xS", send(0, 8), outcome(0, "refused"))
    write_journal(tmp_path / "4-transfers", "0xOther", send(0, 6))
    assert unsettled(tmp_path, "0xS", "0xA", 5) == [
        ("1-transfers/journal.jsonl", 1, 6),
        ("2-transfers/journal.jsonl", 0, 7),
    ]
    assert unsettled(tmp_path, "0xS", "0xA", 7) == [("2-transfers/journal.jsonl", 0, 7)]
    assert unsettled(tmp_path, "0xS", "0xA", 8) == []
    assert unsettled(tmp_path, "0xS", "0xB", 5) == []
    assert unsettled(tmp_path / "none", "0xS", "0xA", 0) == []
