from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import pytest

from privana_bench import cli, locks
from privana_bench.config import Config, PreflightError, indexed_wallets, wallets
from privana_bench.locks import Action, burst, summarize
from privana_bench.privana import NEVER_SENT, Reply
from privana_bench.transfers import RESENDS, Journal, run_transfers, unsettled

MNEMONIC = "test test test test test test test test test test test junk"
ACCOUNTING = "0x910CFfe4e8B27bc367F6E34D6D8e3C31DED68B6f"
TOKEN = "0x" + "ab" * 32
CFG = Config("https://api.example", "https://rpc.example", ACCOUNTING, TOKEN, MNEMONIC)
SALT = (23295).to_bytes(32, "big")
FUNDER, SERVICE = (w.address for w in wallets(MNEMONIC))


@dataclass
class World:
    """Privana and Sapphire in one, where each write lands on its first await."""

    held: dict[str, int] = field(default_factory=lambda: {FUNDER: 100_000})
    nonces: dict[tuple[str, str], int] = field(default_factory=dict)
    locks: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    next_lock: int = 1
    block: int = 100
    calls: list[str] = field(default_factory=list)
    opened: list[int] = field(default_factory=list)  # nonces with a connection opened for them
    own: list[int] = field(default_factory=list)  # nonces of the settles sent on their own one
    network: str = "testnet"

    def nonce(self, getter: str, address: str) -> int:
        return self.nonces.get((getter, address), 0)

    def step(self, getter: str, address: str) -> None:
        self.nonces[(getter, address)] = self.nonce(getter, address) + 1
        self.block += 1

    # The chain
    async def chain_id(self) -> int:
        return 23295

    async def version(self) -> int:
        return 3

    async def eip712_domain(self) -> tuple[int, str, str, str, bytes]:
        return (0x1B, "AccountingModule", "1", ACCOUNTING, SALT)

    async def nonces_at(self, getter: str, addresses: list[str]) -> tuple[int, dict[str, int]]:
        return self.block, {a: self.nonce(getter, a) for a in addresses}

    async def service_locks(
        self, service: Any, users: list[str]
    ) -> dict[str, list[tuple[int, int]]]:
        assert service.address == SERVICE
        return {u: list(self.locks.get(u, [])) for u in users}

    # The API
    async def token(self, token_id: str) -> tuple[int, str] | None:
        return (6, "USDC") if token_id == TOKEN else None

    async def transfer_nonce(self, address: str) -> int:
        return self.nonce("transferNonces", address)

    async def transfer_locked_nonce(self, address: str) -> int:
        return self.nonce("transferLockedNonces", address)

    async def login(self, account: Any, now: float) -> None:
        self.calls.append("login")

    async def balance(self, account: Any, token_id: str) -> int:
        return self.held.get(account.address, 0)

    def sign_transfer(self, account: Any, to: str, token_id: str, amount: int, nonce: int) -> str:
        return "0x" + "5" * 130

    async def transfer(
        self, account: Any, to: str, token_id: str, amount: int, nonce: int, signature: str
    ) -> Reply:
        self.calls.append("transfer")
        assert nonce == self.nonce("transferNonces", account.address)
        self.held[account.address] -= amount
        self.held[to] = self.held.get(to, 0) + amount
        self.step("transferNonces", account.address)
        return Reply("ok", 200)

    async def lock(
        self, account: Any, service: str, token_id: str, amount: int, expiry: int, nonce: int
    ) -> Reply:
        self.calls.append("lock")
        await asyncio.sleep(0.01)
        assert nonce == self.nonce("createLockNonces", account.address)
        self.held[account.address] -= amount
        self.locks.setdefault(account.address, []).append((self.next_lock, amount))
        self.next_lock += 1
        self.step("createLockNonces", account.address)
        return Reply("ok", 200)

    async def settle(
        self,
        service: Any,
        user: str,
        to: str,
        lock_id: int,
        amount: int,
        nonce: int,
        own_connection: bool = False,
    ) -> Reply:
        self.calls.append("settle")
        if own_connection:
            self.own.append(nonce)
        # The contract takes the current nonce only, so any other settle reverts.
        if nonce != self.nonce("transferLockedNonces", service.address):
            return Reply("refused", 400, "transferFromLock nonce mismatch")
        await asyncio.sleep(0.01)
        self.locks[user].remove((lock_id, amount))
        self.held[to] = self.held.get(to, 0) + amount
        self.step("transferLockedNonces", service.address)
        return Reply("ok", 200)

    async def open_connection(self, address: str, nonce: int) -> bool:
        assert address == SERVICE
        self.calls.append("open")
        self.opened.append(nonce)
        return True

    async def close(self) -> None:
        pass


class FakeChain:
    def __init__(self, world: World) -> None:
        self.w = world

    def __getattr__(self, name: str) -> Any:
        return getattr(self.w, "nonces_at" if name == "nonces" else name)


def locks_args(live: bool, n: int = 3) -> argparse.Namespace:
    return argparse.Namespace(
        command="locks",
        n=n,
        amount="0.01",
        poll=0.01,
        max_wait=90.0,
        transfer_gap=0.0,
        settle_gap=0.0,
        live=live,
        funded=False,
    )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    w = World()
    monkeypatch.setattr(cli, "RESULTS", tmp_path)
    monkeypatch.setattr(cli, "Chain", lambda *args: FakeChain(w))
    monkeypatch.setattr(cli, "Privana", lambda *args: w)
    monkeypatch.setattr(locks, "SETTLE_S", 0.0)
    monkeypatch.setattr(locks, "OPEN_S", 0.0)
    monkeypatch.setattr(locks, "REFUSED_WAIT_S", 0.05)
    return w


async def test_dry_run_neither_logs_in_nor_sends(world: World, tmp_path: Path) -> None:
    assert await cli.run(CFG, locks_args(live=False)) == 0
    assert world.calls == []
    assert list(tmp_path.iterdir()) == []


async def test_players_that_locked_before_stop_the_run(world: World) -> None:
    (player,) = indexed_wallets(MNEMONIC, locks.PLAYER_INDEX, 1)
    world.nonces[("createLockNonces", player.address)] = 1
    with pytest.raises(PreflightError, match="has locked before"):
        await cli.run(CFG, locks_args(live=True, n=1))
    assert world.calls == []


async def test_live_run_end_to_end(world: World, tmp_path: Path) -> None:
    assert await cli.run(CFG, locks_args(live=True)) == 0
    assert world.calls[:4] == ["login", "transfer", "transfer", "transfer"]
    # Each settle of the ordered burst goes out on a connection opened for it. The two
    # that the fake refuses go out again one after another, on the service's shared one.
    assert world.calls[4:] == ["lock"] * 3 + ["open"] * 3 + ["settle"] * 5
    assert world.opened == world.own == [0, 1, 2]
    assert world.held[FUNDER] == 100_000  # every lock settled back to the funder
    assert all(not found for found in world.locks.values())

    (run_dir,) = tmp_path.iterdir()
    report = json.loads((run_dir / "report.json").read_text())
    assert (report["ended"], report["money_check"]) == ("completed", "ok")
    landed = {name: s["landed"] for name, s in report["summary"].items()}
    assert landed == {"funding": 3, "locks": 3, "settles in order": 1, "settles": 2}
    assert report["summary"]["settles in order"]["refused"] == 2
    assert all(a["block"] for a in report["phases"]["locks"])  # where each was first seen
    assert (run_dir / "report.md").read_text().startswith("# Locks, N = 3: PASS")
    for path in run_dir.iterdir():
        text = path.read_text()
        assert "5" * 130 not in text and "junk" not in text
    journal = (run_dir / "settles-in-order.jsonl").read_text().splitlines()
    opens = [r for r in map(json.loads, journal) if r["event"] == "open"]
    assert [(r["nonce"], r["opened"]) for r in opens] == [(0, True), (1, True), (2, True)]
    # The lock journal names no sender, so the earlier-run guard skips it.
    assert (run_dir / "locks.jsonl").exists()
    assert unsettled(tmp_path, FUNDER, ACCOUNTING, 0) == []
    assert unsettled(tmp_path, SERVICE, ACCOUNTING, 0) == []


def test_settles_at_the_default_gap_all_land_in_the_burst(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "load", lambda path: CFG)
    argv = ["locks", "--n", "3", "--poll", "0.01", "--max-wait", "90", "--transfer-gap", "0"]
    assert cli.main([*argv, "--live"]) == 0
    (run_dir,) = tmp_path.iterdir()
    report = json.loads((run_dir / "report.json").read_text())
    assert report["meta"]["settle_gap_s"] == 0.25
    landed = {name: s["landed"] for name, s in report["summary"].items()}
    assert landed == {"funding": 3, "locks": 3, "settles in order": 3}


async def test_the_funding_is_paced_and_the_serial_settles_are_not(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    paced = locks_args(live=True)
    paced.transfer_gap = 0.01
    gaps: list[float] = []

    async def recorded(*args: Any, **kwargs: Any) -> str:
        gaps.append(kwargs.get("min_gap_s", 0.0))
        return await run_transfers(*args, **kwargs)

    monkeypatch.setattr(locks, "run_transfers", recorded)
    assert await cli.run(CFG, paced) == 0
    assert gaps == [0.01, 0.0]  # the funding, then the settles one after another


@pytest.mark.parametrize(
    ("benchmark", "amount", "blocks"),
    [("locks", "0.01", False), ("locks", "0.02", True), ("transfers", "0.01", True)],
)
async def test_an_earlier_write_at_the_funder_nonce_blocks_only_another_write(
    world: World, tmp_path: Path, benchmark: str, amount: str, blocks: bool
) -> None:
    # An earlier run left funding nonce 0 unresolved. A locks run of the same amount
    # sends that same write first.
    start = {"event": "start", "benchmark": benchmark, "amount": "0.01", "token_id": TOKEN}
    start |= {"sender": FUNDER, "accounting": ACCOUNTING}
    (tmp_path / "earlier").mkdir()
    (tmp_path / "earlier" / "funding.jsonl").write_text(
        json.dumps(start) + "\n" + json.dumps({"event": "send", "k": 0, "nonce": 0}) + "\n"
    )
    args = locks_args(live=False)
    args.amount = amount
    if blocks:
        with pytest.raises(PreflightError, match="left write 0 .* at nonce 0 unresolved"):
            await cli.run(CFG, args)
    else:
        assert await cli.run(CFG, args) == 0


async def test_a_lock_that_did_not_land_makes_the_run_partial(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = indexed_wallets(MNEMONIC, locks.PLAYER_INDEX, 3)[1].address
    lock = world.lock

    async def lock_unless_refused(account: Any, *args: Any) -> Reply:
        if account.address == refused:
            return Reply("refused", 422, "Transaction reverted: InsufficientBalance")
        return await lock(account, *args)

    monkeypatch.setattr(world, "lock", lock_unless_refused)
    assert await cli.run(CFG, locks_args(live=True)) == 1
    (run_dir,) = tmp_path.iterdir()
    report = json.loads((run_dir / "report.json").read_text())
    assert report["ended"] == "partial: 1 of 3 locks did not land, see the notes"
    assert report["money_check"] == "ok"  # the refused player keeps its funding
    assert world.calls.count("settle") == 3  # both landed locks settled
    assert (run_dir / "report.md").read_text().startswith("# Locks, N = 3: FAIL")


async def test_no_lock_landed_stops_before_settling(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*args: Any) -> Reply:
        return Reply("refused", 422, "Transaction reverted: InsufficientBalance")

    monkeypatch.setattr(world, "lock", refuse)
    assert await cli.run(CFG, locks_args(live=True)) == 1
    assert "settle" not in world.calls
    (run_dir,) = tmp_path.iterdir()
    report = json.loads((run_dir / "report.json").read_text())
    assert report["ended"] == "stopped after locking: no lock landed"
    assert report["money_check"] == "ok"  # the funding stays with the players


async def test_burst_outcomes(tmp_path: Path) -> None:
    nonces = {"0xA": 0, "0xB": 0, "0xC": 0, "0xD": 0}
    replies = {
        "0xA": Reply("ok", 200),
        "0xB": Reply("unknown", 500, "Failed to submit transaction"),
        "0xC": Reply("refused", 422, "Transaction reverted: InsufficientBalance"),
        "0xD": Reply("unknown", None, "network: ReadTimeout"),
    }
    lands = {"0xA", "0xB"}

    async def send(a: Action) -> Reply:
        if a.wallet in lands:
            nonces[a.wallet] += 1
        return replies[a.wallet]

    async def read(wallets: list[str]) -> tuple[int, dict[str, int]]:
        return 7, {w: nonces[w] for w in wallets}

    actions = [Action(k, 0, 0.0, wallet=w) for k, w in enumerate(nonces)]
    journal = Journal(tmp_path / "j.jsonl")
    await burst(
        actions, send, read, poll_s=0.01, max_wait_s=0.3, refused_wait_s=0.05, journal=journal
    )
    journal.close()
    assert [a.outcome for a in actions] == ["landed", "landed", "refused", "unresolved"]
    assert actions[0].block == 7 and actions[0].note == ""
    assert "but it landed" in actions[1].note
    assert "can still land" in actions[3].note
    events = [json.loads(line)["event"] for line in (tmp_path / "j.jsonl").read_text().splitlines()]
    assert events == ["send"] * 4 + ["outcome"] * 4


async def test_a_burst_with_a_gap_sends_in_order_without_waiting(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    nonce = 0
    sent: list[tuple[int, float]] = []
    journaled: list[int] = []

    async def send(a: Action) -> Reply:
        nonlocal nonce
        sent.append((a.k, time.time()))
        journaled.append(len(path.read_text().splitlines()))
        if a.nonce == nonce:  # one wallet's writes land in nonce order only
            nonce += 1
        await asyncio.sleep(0.5)  # every reply comes back after the last send
        return Reply("ok", 200)

    async def read(wallets: list[str]) -> tuple[int, dict[str, int]]:
        return 7, {w: nonce for w in wallets}

    actions = [Action(k, k, 0.0, wallet="0xS") for k in range(3)]
    journal = Journal(path)
    await burst(
        actions,
        send,
        read,
        poll_s=0.01,
        max_wait_s=2.0,
        refused_wait_s=0.02,
        journal=journal,
        gap_s=0.05,
    )
    journal.close()
    assert [k for k, _ in sent] == [0, 1, 2]
    assert [a.outcome for a in actions] == ["landed"] * 3
    assert [a.due_at - actions[0].due_at for a in actions] == pytest.approx([0.0, 0.05, 0.1])
    # The loop's clock is not time.time(), so allow a little.
    assert all(a.sent_at >= a.due_at - 0.01 for a in actions)
    assert sent[-1][1] < actions[0].reply_at
    assert journaled == [3, 3, 3]  # every send line is on disk before the first send
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["due_at"] for r in records[:3]] == [a.due_at for a in actions]


async def test_a_burst_opens_each_action_first_and_keeps_the_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(locks, "OPEN_S", 0.2)
    nonce = 0
    began: dict[int, float] = {}
    sent: dict[int, float] = {}

    async def open_first(a: Action) -> None:
        began[a.k] = time.time()
        await asyncio.sleep(0.15 if a.k == 1 else 0.01)  # one opens slower than the others

    async def send(a: Action) -> Reply:
        nonlocal nonce
        sent[a.k] = time.time()
        nonce += 1
        return Reply("ok", 200)

    async def read(wallets: list[str]) -> tuple[int, dict[str, int]]:
        return 7, {w: nonce for w in wallets}

    actions = [Action(k, k, 0.0, wallet="0xS") for k in range(3)]
    await burst(actions, send, read, 0.01, 2.0, 0.02, gap_s=0.05, open_first=open_first)
    assert [a.outcome for a in actions] == ["landed"] * 3
    # Each goes out OPEN_S after its opening began, however long the opening took.
    assert all(sent[k] - began[k] == pytest.approx(0.2, abs=0.03) for k in range(3))
    assert [sent[k + 1] - sent[k] for k in range(2)] == pytest.approx([0.05, 0.05], abs=0.03)


async def test_a_lost_write_goes_out_again_once_the_burst_is_quiet(tmp_path: Path) -> None:
    nonces = {"0xA": 0, "0xB": 0}
    sent: list[tuple[str, float]] = []

    async def send(a: Action) -> Reply:
        sent.append((a.wallet, time.time()))
        if a.wallet == "0xA":
            await asyncio.sleep(0.2)  # still queued when 0xB's reply is cut
        elif len(sent) == 2:
            return Reply("unknown", None, "network: RemoteProtocolError")
        nonces[a.wallet] += 1
        return Reply("ok", 200)

    async def read(wallets: list[str]) -> tuple[int, dict[str, int]]:
        return 7, {w: nonces[w] for w in wallets}

    actions = [Action(k, 0, 0.0, wallet=w) for k, w in enumerate(nonces)]
    journal = Journal(tmp_path / "j.jsonl")
    await burst(
        actions,
        send,
        read,
        poll_s=0.01,
        max_wait_s=5.0,
        refused_wait_s=0.02,
        journal=journal,
        quiet_s=0.1,
    )
    journal.close()
    assert [a.outcome for a in actions] == ["landed", "landed"]
    assert [a.resends for a in actions] == [0, 1]
    assert actions[1].note.endswith("but it landed, resent 1 time")
    # Not while 0xA was still landing: only 0.1 s after the last landing or reply.
    assert [w for w, _ in sent] == ["0xA", "0xB", "0xB"]
    assert actions[0].landed_at is not None and sent[2][1] >= actions[0].landed_at + 0.1
    events = [json.loads(line)["event"] for line in (tmp_path / "j.jsonl").read_text().splitlines()]
    assert events == ["send", "send", "resend", "resend reply", "outcome", "outcome"]


async def test_a_write_that_stays_lost_goes_out_again_until_the_watch_ends() -> None:
    sent: list[str] = []
    reads = 0

    async def send(a: Action) -> Reply:
        sent.append(a.wallet)
        return Reply("unknown", 502)

    async def read(wallets: list[str]) -> tuple[int, dict[str, int]]:
        nonlocal reads
        reads += 1
        # 0xC landed, and only the second read shows it: later ones come from a node behind.
        return 7, {w: int(w == "0xC" and reads == 2) for w in wallets}

    # 0xB waits for nonce 1 while its wallet is at 0, so a resend could not land.
    actions = [
        Action(0, 0, 0.0, wallet="0xA"),
        Action(1, 1, 0.0, wallet="0xB"),
        Action(2, 0, 0.0, wallet="0xC"),
    ]
    await burst(actions, send, read, poll_s=0.01, max_wait_s=0.4, refused_wait_s=0.02, quiet_s=0.05)
    assert [a.outcome for a in actions] == ["unresolved", "unresolved", "landed"]
    resends = actions[0].resends
    assert resends >= 2 and sent.count("0xA") == 1 + resends
    assert f"resent {resends} times, so it can still land" in actions[0].note
    assert (sent.count("0xB"), sent.count("0xC")) == (1, 1)


async def test_a_refused_resend_is_the_reply() -> None:
    replies = [Reply("unknown", 502), Reply("refused", 422, "Transaction reverted: Expired")]

    async def send(a: Action) -> Reply:
        return replies.pop(0)

    async def read(wallets: list[str]) -> tuple[int, dict[str, int]]:
        return 7, {w: 0 for w in wallets}

    actions = [Action(0, 0, 0.0, wallet="0xA")]
    await burst(actions, send, read, poll_s=0.01, max_wait_s=2.0, refused_wait_s=0.02, quiet_s=0.05)
    assert (actions[0].outcome, actions[0].resends) == ("refused", 1)
    assert actions[0].note == "422 Transaction reverted: Expired, resent 1 time"


async def test_a_resend_that_never_connected_is_no_reply() -> None:
    replies = [Reply("unknown", 502)]

    async def send(a: Action) -> Reply:
        return replies.pop(0) if replies else Reply("refused", None, NEVER_SENT)

    async def read(wallets: list[str]) -> tuple[int, dict[str, int]]:
        return 7, {w: 0 for w in wallets}

    actions = [Action(0, 0, 0.0, wallet="0xA")]
    await burst(actions, send, read, poll_s=0.01, max_wait_s=0.3, refused_wait_s=0.02, quiet_s=0.05)
    assert actions[0].outcome == "unresolved" and actions[0].resends >= 1
    assert "reply 502, resent" in actions[0].note


async def test_a_lost_lock_is_recovered_in_a_live_run(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locks, "QUIET_S", 0.1)
    lost = indexed_wallets(MNEMONIC, locks.PLAYER_INDEX, 3)[1].address
    lock, tries = world.lock, 0

    async def lock_after_a_lost_try(account: Any, *args: Any) -> Reply:
        nonlocal tries
        if account.address == lost:
            tries += 1
            if tries == 1:
                return Reply("unknown", None, "network: RemoteProtocolError")
        return await lock(account, *args)

    monkeypatch.setattr(world, "lock", lock_after_a_lost_try)
    assert await cli.run(CFG, locks_args(live=True)) == 0
    (run_dir,) = tmp_path.iterdir()
    report = json.loads((run_dir / "report.json").read_text())
    assert (report["ended"], report["money_check"]) == ("completed", "ok")
    summary = report["summary"]["locks"]
    assert (summary["landed"], summary["landed_without_resend"], summary["resends"]) == (3, 2, 1)
    assert "Landed without a resend: funding 3 in " in (run_dir / "report.md").read_text()


async def test_reply_time_counts_from_the_try_that_went_out() -> None:
    async def send(a: Action) -> Reply:
        await asyncio.sleep(0.2)
        return Reply("ok", 200, connect_failures=1, retry_s=0.2)

    async def read(wallets: list[str]) -> tuple[int, dict[str, int]]:
        return 7, {w: 1 for w in wallets}

    actions = [Action(0, 0, 0.0, wallet="0xA")]
    await burst(actions, send, read, poll_s=0.01, max_wait_s=1.0, refused_wait_s=0.05)
    assert actions[0].outcome == "landed"
    assert actions[0].reply_at - actions[0].sent_at < 0.1


def test_no_rate_while_a_write_is_unresolved() -> None:
    landed = Action(0, 0, 0.0, wallet="0xA", sent_at=1.0, landed_at=7.0, outcome="landed")
    stuck = Action(1, 0, 0.0, wallet="0xB", sent_at=1.0, outcome="unresolved")
    assert summarize([landed])["landed_per_minute"] == 10.0
    assert summarize([landed, stuck])["landed_per_minute"] is None


def test_report_counts_connections_that_never_opened() -> None:
    reply = Reply("ok", 200, connect_failures=2)
    landed = Action(0, 0, 0.0, wallet="0xA", sent_at=1.0, landed_at=7.0, outcome="landed")
    landed.reply = reply
    assert summarize([landed])["connections_that_never_opened"] == 2


async def test_failed_reads_leave_a_refusal_unresolved() -> None:
    async def send(a: Action) -> Reply:
        return Reply("refused", 400, "createLock nonce mismatch")

    async def read(wallets: list[str]) -> tuple[int, dict[str, int]]:
        raise OSError("rpc down")

    actions = [Action(0, 0, 0.0, wallet="0xA")]
    await burst(actions, send, cast(Any, read), poll_s=0.01, max_wait_s=0.1, refused_wait_s=0.01)
    assert actions[0].outcome == "unresolved"


def sessions_args(n: int = 4) -> argparse.Namespace:
    args = locks_args(live=True, n=n)
    args.max_wait = 400.0
    args.command, args.per_minute, args.play, args.settle_gap = "sessions", 600.0, 0.2, 0.03
    return args


async def test_sessions_settle_each_lock_while_later_ones_still_start(
    world: World, tmp_path: Path
) -> None:
    assert await cli.run(CFG, sessions_args()) == 0
    # The first settle goes out before the last lock: the two streams overlap.
    writes = [c for c in world.calls if c in ("lock", "settle")]
    assert writes.index("settle") < len(writes) - 1 - writes[::-1].index("lock")
    assert world.opened == world.own == [0, 1, 2, 3]
    assert world.held[FUNDER] == 100_000

    (run_dir,) = tmp_path.iterdir()
    report = json.loads((run_dir / "report.json").read_text())
    assert (report["ended"], report["money_check"]) == ("completed", "ok")
    landed = {name: s["landed"] for name, s in report["summary"].items()}
    assert landed == {"funding": 4, "locks": 4, "settles in order": 4}
    back = report["money_back"]
    assert (back["sessions"], back["back"], back["within_60_s"]) == (4, 4, 4)
    starts = [a["due_at"] for a in report["phases"]["locks"]]
    assert starts == sorted(starts) and len(set(starts)) == 4  # one schedule, not all at once
    # Each settle goes out a play time after its lock landed, in the order they landed.
    landings = sorted(a["landed_at"] for a in report["phases"]["locks"])
    settles = report["phases"]["settles in order"]
    assert all(s["due_at"] >= at + 0.2 for s, at in zip(settles, landings, strict=True))
    # Every settle is in the journal before it goes out, so a later run sees one left open.
    journal = (run_dir / "settles-in-order.jsonl").read_text().splitlines()
    sends = [r["nonce"] for r in map(json.loads, journal) if r["event"] == "send"]
    assert sends == [0, 1, 2, 3]
    text = (run_dir / "report.md").read_text()
    assert text.startswith("# Sessions, N = 4: PASS")
    assert "| Locks, as sessions start | 4 | 4 |" in text
    assert "Money back after a session ended: 4 of 4 sessions, 4 within 1 min" in text
    assert 0 <= back["lock_wait_median_s"] <= back["lock_wait_slowest_s"] < 5
    assert f"Lock landed after its session started: median {back['lock_wait_median_s']} s" in text
    assert (report["meta"]["benchmark"], report["meta"]["per_minute"]) == ("sessions", 600.0)
    assert unsettled(tmp_path, SERVICE, ACCOUNTING, 0) == []
    # A later run sends a sessions run's funding again, as it does a locks run's.
    assert locks.same_funding("0.01", TOKEN)(report["meta"])


async def test_sessions_that_end_together_are_settled_a_gap_apart(
    world: World, tmp_path: Path
) -> None:
    args = sessions_args()
    args.per_minute, args.settle_gap = 600_000.0, 0.05
    assert await cli.run(CFG, args) == 0
    (run_dir,) = tmp_path.iterdir()
    report = json.loads((run_dir / "report.json").read_text())
    dues = [a["due_at"] for a in report["phases"]["settles in order"]]
    assert len(dues) == 4
    assert all(later - earlier >= 0.05 - 1e-6 for earlier, later in pairwise(dues))


async def test_sessions_whose_money_is_back_late_fail(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locks, "MONEY_BACK_S", (60, 180, 0))
    assert await cli.run(CFG, sessions_args()) == 1
    assert world.held[FUNDER] == 100_000  # every lock was settled back, only late

    (run_dir,) = tmp_path.iterdir()
    report = json.loads((run_dir / "report.json").read_text())
    assert report["ended"] == (
        "slow: 4 of 4 sessions did not have their money back within 0 minutes of ending"
    )
    assert (run_dir / "report.md").read_text().startswith("# Sessions, N = 4: FAIL")


def failing_settle(world: World, tries: int, status: int | None, reason: str) -> Any:
    """`world.settle`, but the settle at nonce 1 fails on its first `tries` tries."""
    settle, tried = world.settle, 0

    async def fail_first(service: Any, user: str, *args: Any, **kwargs: Any) -> Reply:
        nonlocal tried
        if args[3] == 1:
            tried += 1
            if tried <= tries:
                return Reply("unknown", status, reason)
        return await settle(service, user, *args, **kwargs)

    return fail_first


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (409, "transferFromLock 0xabc: submission outcome is unknown: appd replied 400: no"),
        (None, "network: RemoteProtocolError"),
    ],
)
async def test_a_settle_that_does_not_land_goes_out_again(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int | None, reason: str
) -> None:
    monkeypatch.setattr(world, "settle", failing_settle(world, 1, status, reason))
    assert await asyncio.wait_for(cli.run(CFG, sessions_args()), 20) == 0
    assert world.held[FUNDER] == 100_000

    (run_dir,) = tmp_path.iterdir()
    assert run_dir.name.endswith("-sessions")
    report = json.loads((run_dir / "report.json").read_text())
    assert (report["ended"], report["money_check"]) == ("completed", "ok")
    ordered = report["phases"]["settles in order"]
    # The stream itself lands all four, so nothing is left to send one after another.
    assert [a["outcome"] for a in ordered] == ["landed"] * 4
    assert (ordered[0]["resends"], ordered[1]["resends"]) == (0, 1)
    assert ordered[0]["first_sent_at"] == ordered[0]["sent_at"]
    assert ordered[1]["first_sent_at"] < ordered[1]["sent_at"]
    assert report["phases"]["settles"] == []
    assert report["money_back"]["back"] == 4
    # The reply that made it go out again is kept, in the journal and in the report.
    journal = map(json.loads, (run_dir / "settles-in-order.jsonl").read_text().splitlines())
    resends = [r for r in journal if r["event"] == "resend"]
    assert (resends[0]["nonce"], resends[0]["reply"]["detail"]) == (1, reason)
    text = (run_dir / "report.md").read_text()
    assert (
        "Sent again with the settles behind it, after it did not land: settle 1 (nonce 1)" in text
    )
    assert reason in text


async def test_settles_go_out_again_in_order_a_gap_apart_once_none_is_still_out(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settle = world.settle
    tried: Counter[int] = Counter()
    out: set[int] = set()
    overlapped: list[int] = []

    async def slow(service: Any, user: str, *args: Any, **kwargs: Any) -> Reply:
        nonce = args[3]
        overlapped.extend([nonce] * (nonce in out))
        out.add(nonce)
        tried[nonce] += 1
        try:
            if nonce == 1 and tried[1] == 1:
                await asyncio.sleep(0.1)
                return Reply("unknown", 409, "no")
            if nonce == 2 and tried[2] == 1:
                await asyncio.sleep(0.4)  # still out when the settle before it is sent again
            return await settle(service, user, *args, **kwargs)
        finally:
            out.discard(nonce)

    monkeypatch.setattr(world, "settle", slow)
    args = sessions_args()
    args.per_minute = 60_000.0  # the four sessions end together
    assert await asyncio.wait_for(cli.run(CFG, args), 20) == 0
    assert overlapped == []

    (run_dir,) = tmp_path.iterdir()
    journal = map(json.loads, (run_dir / "settles-in-order.jsonl").read_text().splitlines())
    resends = [r for r in journal if r["event"] == "resend"]
    assert [r["nonce"] for r in resends] == [1, 2, 3]
    assert all(b["at"] - a["at"] >= 0.03 - 1e-6 for a, b in pairwise(resends))


async def test_a_read_on_a_node_behind_does_not_send_a_landed_settle_again(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = world.transfer_locked_nonce

    async def behind(address: str) -> int:
        return max(0, await read(address) - 1)

    monkeypatch.setattr(world, "transfer_locked_nonce", behind)
    assert await cli.run(CFG, sessions_args()) == 0
    (run_dir,) = tmp_path.iterdir()
    assert json.loads((run_dir / "report.json").read_text())["phases"]["settles"] == []


def test_a_resend_does_not_shorten_the_time_from_the_first_send() -> None:
    resent = Action(0, 0, 0.0, sent_at=100.0, first_sent_at=10.0, landed_at=110.0, resends=1)
    assert summarize([resent])["first_send_to_last_landing_s"] == 100.0


async def test_a_settle_that_never_lands_in_the_stream_stops_it(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reason = "transferFromLock 0xabc: submission outcome is unknown: appd replied 400: no"
    monkeypatch.setattr(world, "settle", failing_settle(world, 1 + RESENDS, 409, reason))
    # The stream stops on that settle, well before --max-wait.
    assert await asyncio.wait_for(cli.run(CFG, sessions_args()), 20) == 0
    assert world.held[FUNDER] == 100_000  # every lock was still settled back

    (run_dir,) = tmp_path.iterdir()
    report = json.loads((run_dir / "report.json").read_text())
    assert (report["ended"], report["money_check"]) == ("completed", "ok")
    ordered = report["phases"]["settles in order"]
    assert [a["outcome"] for a in ordered][:2] == ["landed", "unresolved"]
    assert (ordered[1]["resends"], ordered[1]["reply"]["detail"]) == (RESENDS, reason)
    # Nothing behind the unused nonce lands, so the rest go out one after another,
    # the settle at nonce 1 as the same write again.
    assert all(a["outcome"] != "landed" for a in ordered[1:])
    serial = report["phases"]["settles"]
    assert [t["nonce"] for t in serial] == [1, 2, 3]
    assert all(t["outcome"] == "landed" for t in serial)
    back = report["money_back"]
    assert (back["back"], back["within_60_s"]) == (4, 4)
    # No session's money is back before the session ended.
    assert all(a["back_at"] >= a["ended_at"] for a in report["phases"]["locks"])
    text = (run_dir / "report.md").read_text()
    stopped = f"The settle stream stopped after {RESENDS} resends at settle 1 (nonce 1)"
    assert stopped in text


async def test_a_settle_that_lands_after_the_stream_stopped_is_not_sent_again(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settle, read = world.settle, world.transfer_locked_nonce
    lost: list[Any] = []  # the stream's tries of the settle at nonce 1, each reply lost
    late = False

    async def lose(service: Any, user: str, *args: Any, **kwargs: Any) -> Reply:
        if args[3] == 1 and not late:
            lost.append((service, user, *args))
            return Reply("unknown", None, "network: RemoteProtocolError")
        return await settle(service, user, *args, **kwargs)

    async def read_after_it_landed(address: str) -> int:
        nonlocal late
        if len(lost) == 1 + RESENDS and not late:
            late = True
            await settle(*lost[-1])  # the last try lands once the stream has stopped
        return await read(address)

    monkeypatch.setattr(world, "settle", lose)
    monkeypatch.setattr(world, "transfer_locked_nonce", read_after_it_landed)
    assert await asyncio.wait_for(cli.run(CFG, sessions_args()), 20) == 0
    assert world.held[FUNDER] == 100_000

    (run_dir,) = tmp_path.iterdir()
    report = json.loads((run_dir / "report.json").read_text())
    assert (report["ended"], report["money_check"]) == ("completed", "ok")
    ordered = report["phases"]["settles in order"]
    assert [a["outcome"] for a in ordered][:2] == ["landed", "landed"]
    assert ordered[1]["note"].startswith("landed after the stream stopped")
    # Only the two behind it go out one after another.
    assert [t["nonce"] for t in report["phases"]["settles"]] == [2, 3]
    assert report["money_back"]["back"] == 4


async def test_sessions_that_outlast_the_watch_stop_before_anything_is_sent(world: World) -> None:
    args = sessions_args(n=100)
    args.per_minute = 3.0
    with pytest.raises(PreflightError, match="ends the watch too early"):
        await cli.run(CFG, args)
    assert world.calls == []


async def test_a_burst_takes_offsets_and_later_actions() -> None:
    nonce = 0
    sent: list[int] = []

    async def send(a: Action) -> Reply:
        nonlocal nonce
        sent.append(a.k)
        nonce += 1
        return Reply("ok", 200)

    async def read(wallets: list[str]) -> tuple[int, dict[str, int]]:
        return 7, {"0xS": nonce}

    actions = [Action(k, k, 0.0, wallet="0xS") for k in range(2)]
    late = [Action(2, 2, 0.0, wallet="0xS")]

    async def more(last_read: float) -> list[Action] | None:
        if len(actions) == 3:
            return None if actions[2].landed_at else []
        if actions[1].landed_at is None:
            return []
        late[0].due_at = time.time() + 0.05
        return late

    await burst(actions, send, read, 0.01, 2.0, 0.02, offsets=[0.1, 0.0], more=more)
    assert sent == [1, 0, 2]
    assert actions[0].due_at - actions[1].due_at == pytest.approx(0.1)
    assert [a.outcome for a in actions] == ["landed"] * 3
    assert actions[2].sent_at >= actions[2].due_at - 0.01


async def test_players_funded_by_an_earlier_run_are_not_funded_again(
    world: World, tmp_path: Path
) -> None:
    # An earlier run funded six players, the last four have not locked, and the funder
    # holds nothing more.
    world.nonces[("transferNonces", FUNDER)] = 6
    world.held[FUNDER] = 0
    for player in indexed_wallets(MNEMONIC, locks.PLAYER_INDEX + 2, 4):
        world.held[player.address] = 10_000
    args = sessions_args()
    args.funded = True
    assert await cli.run(CFG, args) == 0
    assert "transfer" not in world.calls
    assert world.held[FUNDER] == 40_000

    (run_dir,) = tmp_path.iterdir()
    report = json.loads((run_dir / "report.json").read_text())
    assert (report["ended"], report["money_check"]) == ("completed", "ok")
    first = locks.PLAYER_INDEX + 2
    assert report["meta"]["players"] == f"indices {first} to {first + 3}"
    assert set(report["summary"]) == {"locks", "settles in order"}
    assert not (run_dir / "funding.jsonl").exists()
    assert "4 players funded by an earlier run each lock" in (run_dir / "report.md").read_text()


async def test_funded_needs_that_many_funded_players(world: World) -> None:
    world.nonces[("transferNonces", FUNDER)] = 3
    args = sessions_args()
    args.funded = True
    with pytest.raises(PreflightError, match="has funded 3 players, fewer than 4"):
        await cli.run(CFG, args)
    assert world.calls == []


async def test_funded_stops_while_an_earlier_funding_can_still_land(
    world: World, tmp_path: Path
) -> None:
    # The earlier run left its funding at nonce 4 unresolved. A funded run does not send
    # that write, so it landing mid-run would move the funder's balance.
    world.nonces[("transferNonces", FUNDER)] = 4
    start = {"event": "start", "benchmark": "sessions", "amount": "0.01", "token_id": TOKEN}
    start |= {"sender": FUNDER, "accounting": ACCOUNTING}
    (tmp_path / "earlier").mkdir()
    (tmp_path / "earlier" / "funding.jsonl").write_text(
        json.dumps(start) + "\n" + json.dumps({"event": "send", "k": 4, "nonce": 4}) + "\n"
    )
    args = sessions_args()
    args.funded = True
    with pytest.raises(PreflightError, match="left write 4 .* at nonce 4 unresolved"):
        await cli.run(CFG, args)
    assert world.calls == []


async def test_resends_behind_another_settle_do_not_use_up_a_settles_own(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settle = world.settle
    tried: Counter[int] = Counter()
    failed_itself = False

    async def fail(service: Any, user: str, *args: Any, **kwargs: Any) -> Reply:
        nonlocal failed_itself
        nonce = args[3]
        tried[nonce] += 1
        # Nonce 1 lands on its last resend. Nonce 2 went out behind it each time, and
        # then fails once as the first unused one.
        itself = nonce == 2 and tried[1] > RESENDS and not failed_itself
        if (nonce == 1 and tried[1] <= RESENDS) or itself:
            failed_itself = failed_itself or itself
            return Reply("unknown", 409, "no")
        return await settle(service, user, *args, **kwargs)

    monkeypatch.setattr(world, "settle", fail)
    args = sessions_args()
    args.per_minute = 60_000.0  # all four sessions end together, so every settle is behind
    assert await asyncio.wait_for(cli.run(CFG, args), 20) == 0

    (run_dir,) = tmp_path.iterdir()
    report = json.loads((run_dir / "report.json").read_text())
    assert (report["ended"], report["money_check"]) == ("completed", "ok")
    ordered = report["phases"]["settles in order"]
    assert [a["outcome"] for a in ordered] == ["landed"] * 4
    assert ordered[2]["resends"] == RESENDS + 1
    # The stream sent nonce 2 again instead of stopping on it.
    assert "stream_stopped" not in report["meta"]
    assert report["phases"]["settles"] == []
    assert "sent_again" not in (run_dir / "report.md").read_text()
