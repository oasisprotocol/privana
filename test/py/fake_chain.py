"""A fake node for sweep engine tests: one chain, as the sweeps of one deposit address see it."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from web3 import Web3
from web3.exceptions import TransactionNotFound

import src.services.sweep_engine as sweep_engine_module

DEPOSIT_ADDRESS = "0x" + "aa" * 20
GAS_TANK_ADDRESS = "0x" + "ff" * 20
START_HEAD = 1000
FIRST_NONCE = 5
SWEEP_FEE = 10**13

# Tests that replace asyncio.sleep still need to yield to the event loop.
REAL_SLEEP = asyncio.sleep


class AwaitableValue:
    """Re-awaitable mock for async properties (e.g. w3.eth.gas_price)."""

    def __init__(self, val):
        self._val = val

    def __await__(self):
        if False:
            yield  # makes this a generator
        return self._val


async def settle():
    """Let every task run until it waits on the chain."""
    for _ in range(50):
        await REAL_SLEEP(0)


def _key(tx_hash) -> str:
    if isinstance(tx_hash, str):
        return "0x" + tx_hash.removeprefix("0x").lower()
    return "0x" + bytes(tx_hash).hex()


class FakeChain:
    """Raw txs read ``kind:nonce:amount``, so mining applies their effect.

    Gas funding txs mine when broadcast. A sweep stays pending until the test mines it, or,
    with ``auto_mine``, mines at the head when broadcast. ``receipts`` is keyed by the
    0x-prefixed tx hash. A sleep parks until ``advance()`` (a mocked sleep would never yield,
    so a poll loop would spin); time.time() reads ``now``.
    """

    def __init__(self, *, auto_mine: bool = False):
        self.auto_mine = auto_mine
        self.head = START_HEAD
        self.now = 0.0
        self.native = 10**18
        self.token = 1000 * 10**6
        self.nonce_blocks = []  # block of each tx the deposit address sent, in nonce order
        self.receipts = {}
        self.broadcasts = []
        self.tank_nonce = 0
        self.lag_after_signing = 0  # latest-nonce reads fall behind once a sweep is signed
        self.latest_nonce_lag = 0
        self.on_balance_read = None  # runs once, right after the next balance read
        self._sleepers = []
        self.w3 = SimpleNamespace(
            eth=SimpleNamespace(
                get_block=self._get_block,
                get_balance=self._get_balance,
                get_transaction_count=self._get_transaction_count,
                get_transaction_receipt=self._get_transaction_receipt,
                send_raw_transaction=self._send_raw_transaction,
                gas_price=AwaitableValue(1_000_000_000),
            )
        )

    @property
    def nonce(self) -> int:
        return FIRST_NONCE + len(self.nonce_blocks)

    @property
    def sweeps(self) -> list[bytes]:
        """The sweep txs sent to the node, once each, in order."""
        return list(dict.fromkeys(raw for raw in self.broadcasts if not raw.startswith(b"fund")))

    @property
    def parked(self) -> int:
        """Tasks waiting in a sleep."""
        return sum(1 for wake in self._sleepers if not wake.done())

    async def advance(self, head: int | None = None, *, now: float | None = None) -> None:
        """Move the head and/or the clock, then wake every parked sleep and let it run."""
        if head is not None:
            self.head = head
        if now is not None:
            self.now = now
        sleepers, self._sleepers = self._sleepers, []
        for wake in sleepers:
            if not wake.done():
                wake.set_result(None)
        await settle()

    async def sleep(self, _delay) -> None:
        wake = asyncio.get_running_loop().create_future()
        self._sleepers.append(wake)
        await wake

    def mine(self, raw: bytes, *, block: int, ok: bool = True) -> None:
        kind, nonce, amount = raw.decode().split(":")
        assert int(nonce) == self.nonce, "mined out of nonce order"
        self.native -= SWEEP_FEE
        if ok and kind == "native":
            self.native -= int(amount)
        elif ok:
            self.token -= int(amount)
        self.nonce_blocks.append(block)
        self.receipts[_key(Web3.keccak(raw))] = {"status": 1 if ok else 0, "blockNumber": block}

    def mine_foreign(self, block: int, native_out: int = 0) -> None:
        """A tx of another sweep (one that timed out earlier) takes the next nonce."""
        self.native -= native_out + SWEEP_FEE
        self.nonce_blocks.append(block)

    async def erc20_balance(self, _w3, _token, _holder) -> int:
        return self.token

    async def _get_block(self, _tag="latest"):
        return {"number": self.head, "baseFeePerGas": 1_000_000_000}

    async def _get_balance(self, _address) -> int:
        balance = self.native
        if self.on_balance_read is not None:
            hook, self.on_balance_read = self.on_balance_read, None
            hook()
        return balance

    async def _get_transaction_count(self, address, block="latest") -> int:
        if address == GAS_TANK_ADDRESS:
            return self.tank_nonce
        if not isinstance(block, int):
            return self.nonce - self.latest_nonce_lag
        return FIRST_NONCE + sum(1 for mined in self.nonce_blocks if mined <= block)

    async def _get_transaction_receipt(self, tx_hash):
        if _key(tx_hash) not in self.receipts:
            raise TransactionNotFound(f"Transaction with hash: {tx_hash!r} not found.")
        return self.receipts[_key(tx_hash)]

    async def _send_raw_transaction(self, raw):
        raw = bytes(raw)
        self.broadcasts.append(raw)
        tx_hash = Web3.keccak(raw)
        kind, _nonce, amount = raw.decode().split(":")
        if kind == "fund":
            self.native += int(amount)
            self.tank_nonce += 1
            self.receipts[_key(tx_hash)] = {"status": 1, "blockNumber": self.head}
        elif self.auto_mine and _key(tx_hash) not in self.receipts:
            self.mine(raw, block=self.head)
        return tx_hash


def attach_chain(engine, accounting, monkeypatch, *, auto_mine: bool = False) -> FakeChain:
    """Serve ``engine`` from a new FakeChain; ``accounting`` signs txs the chain can read."""
    chain = FakeChain(auto_mine=auto_mine)

    async def sign_native(*, amount, nonce, **_):
        chain.latest_nonce_lag = chain.lag_after_signing
        return f"native:{nonce}:{amount}".encode()

    async def sign_erc20(*, amount, nonce, **_):
        chain.latest_nonce_lag = chain.lag_after_signing
        return f"erc20:{nonce}:{amount}".encode()

    async def fund_gas(*, gas_amount, gas_tank_nonce, **_):
        return f"fund:{gas_tank_nonce}:{gas_amount}".encode()

    accounting.generate_sweep_native.side_effect = sign_native
    accounting.generate_sweep_erc20.side_effect = sign_erc20
    accounting.generate_gas_funding_tx.side_effect = fund_gas
    monkeypatch.setattr(engine, "_get_erc20_balance", chain.erc20_balance)
    monkeypatch.setattr(engine, "_get_gas_tank_address", AsyncMock(return_value=GAS_TANK_ADDRESS))
    monkeypatch.setattr(engine, "_get_web3", lambda _chain_id: chain.w3)
    monkeypatch.setattr(sweep_engine_module, "time", SimpleNamespace(time=lambda: chain.now))
    monkeypatch.setattr(asyncio, "sleep", chain.sleep)
    return chain
