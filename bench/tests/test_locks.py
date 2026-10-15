from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from privana_bench import cli, locks
from privana_bench.config import Config, PreflightError, indexed_wallets, wallets
from privana_bench.locks import Action, burst, summarize
from privana_bench.privana import Reply
from privana_bench.transfers import Journal, unsettled

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
        self, service: Any, user: str, to: str, lock_id: int, amount: int, nonce: int
    ) -> Reply:
        self.calls.append("settle")
        # Privana checks the nonce before the signer queues the write, as here.
        if nonce != self.nonce("transferLockedNonces", service.address):
            return Reply("refused", 400, "transferFromLock nonce mismatch")
        await asyncio.sleep(0.01)
        self.locks[user].remove((lock_id, amount))
        self.held[to] = self.held.get(to, 0) + amount
        self.step("transferLockedNonces", service.address)
        return Reply("ok", 200)

    async def close(self) -> None:
        pass


class FakeChain:
    def __init__(self, world: World) -> None:
        self.w = world

    def __getattr__(self, name: str) -> Any:
        return getattr(self.w, "nonces_at" if name == "nonces" else name)


def locks_args(live: bool, n: int = 3) -> argparse.Namespace:
    return argparse.Namespace(
        command="locks", n=n, amount="0.01", poll=0.01, max_wait=90.0, live=live
    )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    w = World()
    monkeypatch.setattr(cli, "RESULTS", tmp_path)
    monkeypatch.setattr(cli, "Chain", lambda *args: FakeChain(w))
    monkeypatch.setattr(cli, "Privana", lambda *args: w)
    monkeypatch.setattr(locks, "SETTLE_S", 0.0)
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
    assert world.held[FUNDER] == 100_000  # every lock settled back to the funder
    assert all(not found for found in world.locks.values())

    (run_dir,) = tmp_path.iterdir()
    report = json.loads((run_dir / "report.json").read_text())
    assert (report["ended"], report["money_check"]) == ("completed", "ok")
    landed = {name: s["landed"] for name, s in report["summary"].items()}
    assert landed == {"funding": 3, "locks": 3, "settles at once": 1, "settles": 2}
    assert report["summary"]["settles at once"]["refused"] == 2
    assert all(a["block"] for a in report["phases"]["locks"])  # where each was first seen
    assert (run_dir / "report.md").read_text().startswith("# Locks, N = 3: PASS")
    for path in run_dir.iterdir():
        text = path.read_text()
        assert "5" * 130 not in text and "junk" not in text
    # The lock journal names no sender, so the earlier-run guard skips it.
    assert (run_dir / "locks.jsonl").exists()
    assert unsettled(tmp_path, FUNDER, ACCOUNTING, 0) == []
    assert unsettled(tmp_path, SERVICE, ACCOUNTING, 0) == []


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
