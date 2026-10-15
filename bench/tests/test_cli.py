from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from privana_bench import cli
from privana_bench.cli import preflight, to_units
from privana_bench.config import Config, PreflightError, wallets
from privana_bench.privana import Reply

MNEMONIC = "test test test test test test test test test test test junk"
ACCOUNTING = "0x910CFfe4e8B27bc367F6E34D6D8e3C31DED68B6f"
TOKEN = "0x" + "ab" * 32
CFG = Config("https://api.example", "https://rpc.example", ACCOUNTING, TOKEN, MNEMONIC)
SALT = (23295).to_bytes(32, "big")


@dataclass
class FakeChain:
    chain: int = 23295
    domain: tuple[int, str, str, str, bytes] = (0x1B, "AccountingModule", "1", ACCOUNTING, SALT)
    nonce: int = 3

    async def chain_id(self) -> int:
        return self.chain

    async def version(self) -> int:
        return 2

    async def eip712_domain(self) -> tuple[int, str, str, str, bytes]:
        return self.domain

    async def transfer_nonce(self, address: str) -> int:
        return self.nonce


@dataclass
class FakeApi:
    tokens: dict[str, tuple[int, str]] = field(default_factory=lambda: {TOKEN: (6, "USDC")})
    nonce: int = 3

    async def token(self, token_id: str) -> tuple[int, str] | None:
        return self.tokens.get(token_id)

    async def transfer_nonce(self, address: str) -> int:
        return self.nonce


async def check(chain: FakeChain, api: FakeApi) -> Any:
    sender, _ = wallets(MNEMONIC)
    return await preflight(CFG, cast(Any, chain), cast(Any, api), sender)


async def test_preflight_passes() -> None:
    facts = await check(FakeChain(), FakeApi())
    assert (facts.chain_id, facts.version, facts.decimals, facts.symbol, facts.nonce) == (
        23295,
        2,
        6,
        "USDC",
        3,
    )


@pytest.mark.parametrize(
    ("chain", "api", "error"),
    [
        (FakeChain(chain=23294), FakeApi(), "not 23295"),
        (
            FakeChain(domain=(0x0F, "AccountingModule", "1", ACCOUNTING, b"\x00" * 32)),
            FakeApi(),
            "EIP-712 domain",
        ),
        (
            FakeChain(domain=(0x1B, "AccountingModule", "1", "0x" + "11" * 20, SALT)),
            FakeApi(),
            "EIP-712 domain",
        ),
        (FakeChain(), FakeApi(tokens={}), "does not list TOKEN_ID"),
        (FakeChain(), FakeApi(nonce=4), "3 on chain but 4 in the API"),
    ],
)
async def test_preflight_refuses(chain: FakeChain, api: FakeApi, error: str) -> None:
    with pytest.raises(PreflightError, match=error):
        await check(chain, api)


def test_to_units() -> None:
    assert to_units("0.01", 6) == 10_000
    assert to_units("1", 18) == 10**18
    for bad in ("0.0000001", "0", "-1", "abc", "NaN", "Infinity"):
        with pytest.raises(PreflightError):
            to_units(bad, 6)


@dataclass
class LiveApi(FakeApi):
    """A fake API on which every transfer lands at once."""

    held: dict[str, int] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def login(self, account: Any, now: float) -> None:
        self.calls.append("login")

    async def balance(self, account: Any, token_id: str) -> int:
        return self.held.get(account.address, 0)

    def sign_transfer(self, account: Any, to: str, token_id: str, amount: int, nonce: int) -> str:
        self.calls.append("sign")
        return "0x" + "5" * 130

    async def transfer(
        self, account: Any, to: str, token_id: str, amount: int, nonce: int, signature: str
    ) -> Reply:
        self.calls.append("transfer")
        assert nonce == self.nonce
        self.nonce += 1
        self.held[account.address] -= amount
        self.held[to] = self.held.get(to, 0) + amount
        return Reply("ok", 200)

    async def close(self) -> None:
        pass


def transfers_args(live: bool) -> argparse.Namespace:
    return argparse.Namespace(
        command="transfers", n=3, amount="0.01", poll=0.01, max_wait=90.0, live=live
    )


@pytest.fixture
def results(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(cli, "RESULTS", tmp_path)
    monkeypatch.setattr(cli, "BALANCE_SETTLE_S", 0.0)
    monkeypatch.setattr(cli, "Chain", lambda *args: FakeChain())
    return tmp_path


async def test_dry_run_neither_logs_in_nor_sends(
    results: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = LiveApi()
    monkeypatch.setattr(cli, "Privana", lambda *args: api)
    assert await cli.run(CFG, transfers_args(live=False)) == 0
    assert api.calls == []
    assert list(results.iterdir()) == []


async def test_unfunded_sender_sends_nothing(
    results: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = LiveApi()
    monkeypatch.setattr(cli, "Privana", lambda *args: api)
    assert await cli.run(CFG, transfers_args(live=True)) == 1
    assert api.calls == ["login", "login"]
    assert list(results.iterdir()) == []


async def test_live_run_end_to_end(results: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sender, receiver = wallets(MNEMONIC)
    api = LiveApi(held={sender.address: 100_000})
    monkeypatch.setattr(cli, "Privana", lambda *args: api)
    assert await cli.run(CFG, transfers_args(live=True)) == 0
    assert api.calls == ["login", "login", *["sign", "transfer"] * 3]
    assert api.held == {sender.address: 70_000, receiver.address: 30_000}
    (run_dir,) = results.iterdir()
    journal = (run_dir / "journal.jsonl").read_text()
    start = json.loads(journal.splitlines()[0])
    assert (start["event"], start["benchmark"], start["token_id"]) == ("start", "transfers", TOKEN)
    assert (run_dir / "report.md").read_text().startswith("# Transfers, N = 3: PASS")
    for text in (journal, (run_dir / "report.json").read_text()):
        assert "5" * 130 not in text and "junk" not in text


@pytest.mark.parametrize("live", [True, False])
async def test_unresolved_earlier_transfer_blocks_the_run(
    results: Path, monkeypatch: pytest.MonkeyPatch, live: bool
) -> None:
    sender, _ = wallets(MNEMONIC)
    api = LiveApi(held={sender.address: 100_000})
    monkeypatch.setattr(cli, "Privana", lambda *args: api)
    earlier = results / "20261014T120000Z-transfers"
    earlier.mkdir()
    start = {"event": "start", "sender": sender.address, "accounting": ACCOUNTING}
    send = {"event": "send", "k": 0, "nonce": 3, "due_at": 0.0}
    (earlier / "journal.jsonl").write_text(json.dumps(start) + "\n" + json.dumps(send) + "\n")
    with pytest.raises(
        PreflightError, match="journal.jsonl left write 0 from .* at nonce 3 unresolved"
    ):
        await cli.run(CFG, transfers_args(live=live))
    assert api.calls == []


@pytest.mark.parametrize(
    "argv",
    [["transfers", "--max-wait", "30"], ["transfers", "--n", "0"], ["transfers", "--poll", "0"]],
)
def test_bad_options_stop_before_anything_runs(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    assert exit_info.value.code == 2
