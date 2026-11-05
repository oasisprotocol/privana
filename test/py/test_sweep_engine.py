"""Tests for the sweep state machine.

Mocks AccountingContractService and web3 to test state transitions
without hitting real chains.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from web3 import Web3

import src.services.rpc_identity as rpc_identity
from src.clients.rofl import TransactionRevertedError
from src.config.chain_config import MIN_SWEEP_GAS_PRICE_WEI, SWEEP_GAS_LIMIT_NATIVE
from src.services.sweep_engine import (
    BalanceClaimStatus,
    SweepEngine,
    SweepRecord,
    SweepState,
)

L2_FEE_PATCH = "src.services.sweep_engine.estimate_l1_data_fee"


@pytest.fixture(autouse=True)
def _mock_l2_fee():
    """Default: L1 chain behavior (no L1 data fee)."""
    with patch(L2_FEE_PATCH, new_callable=AsyncMock, return_value=0):
        yield


class _AwaitableValue:
    """Re-awaitable mock for async properties (e.g. w3.eth.gas_price)."""

    def __init__(self, val):
        self._val = val

    def __await__(self):
        if False:
            yield  # makes this a generator
        return self._val


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path)


@pytest.fixture
def mock_accounting():
    svc = AsyncMock()
    svc.generate_sweep_native = AsyncMock(return_value=b"\x01\x02\x03")
    svc.generate_sweep_erc20 = AsyncMock(return_value=b"\x04\x05\x06")
    svc.generate_gas_funding_tx = AsyncMock(return_value=b"\x07\x08\x09")
    svc.credit_deposit = AsyncMock()
    return svc


@pytest.fixture
def engine(state_dir, mock_accounting):
    chain_rpc_urls = {84532: "https://fake-rpc.example.com"}
    return SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls=chain_rpc_urls,
        state_dir=state_dir,
    )


def _plan(engine, token_address=None):
    """Save the record claim_balance() leaves: a scheduled sweep."""
    engine._save_record(
        SweepRecord(
            deposit_address="0x" + "aa" * 20,
            chain_id=84532,
            state=SweepState.PENDING,
            beneficiary="0x" + "bb" * 20,
            chain_type="evm",
            version=0,
            token_id_hex="0x" + "11" * 32,
            token_address=token_address,
            deposit_id_hex="0x" + "22" * 32,
        )
    )


def test_initial_state_is_idle(engine):
    record = engine.get_record_by_deposit_id("0x" + "22" * 32)
    assert record is None  # no record = idle


def test_state_persistence(engine, state_dir):
    """Sweep records should survive engine restart."""
    record = SweepRecord(
        token_id_hex="0x" + "11" * 32,
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.PENDING,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        deposit_id_hex="0x" + "22" * 32,
    )
    engine._save_record(record)

    # Create a new engine instance (simulates restart)
    engine2 = SweepEngine(
        accounting_service=engine._accounting,
        chain_rpc_urls={84532: "https://fake-rpc.example.com"},
        state_dir=state_dir,
    )
    loaded = engine2.get_record_by_deposit_id("0x" + "22" * 32)
    assert loaded is not None
    assert loaded.state == SweepState.PENDING


@pytest.mark.asyncio
async def test_sweep_native_full_cycle(engine, mock_accounting):
    """Native sweep: IDLE → PENDING → SWEPT → credit → IDLE."""
    "0x" + "aa" * 20

    with patch.object(engine, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        # Balance query
        w3.eth.get_balance = AsyncMock(return_value=10**18)
        w3.eth.get_transaction_count = AsyncMock(return_value=0)
        w3.eth.gas_price = _AwaitableValue(1_000_000_000)  # 1 gwei
        w3.eth.get_block = AsyncMock(return_value={"number": 1000, "baseFeePerGas": 1_000_000_000})
        # Broadcast sweep tx
        w3.eth.send_raw_transaction = AsyncMock(return_value=b"\xdd" * 32)
        # Wait for sweep confirmation
        w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 1, "blockNumber": 100})
        mock_get_w3.return_value = w3

        _plan(engine)
        await engine.sweep(b"\x22" * 32)

    # Sweep signing was called
    mock_accounting.generate_sweep_native.assert_called_once()
    # Credit was called after sweep
    mock_accounting.credit_deposit.assert_called_once()
    # State is back to idle (record cleaned up)
    assert engine.get_record_by_deposit_id("0x" + "22" * 32) is None


@pytest.mark.asyncio
async def test_idempotent_credit_handles_already_processed(engine, mock_accounting):
    """_idempotent_credit treats DepositAlreadyProcessed revert as success."""
    mock_accounting.credit_deposit = AsyncMock(
        side_effect=TransactionRevertedError(
            "Transaction reverted: DepositAlreadyProcessed",
            error_name="DepositAlreadyProcessed",
        )
    )

    # Should NOT raise — DepositAlreadyProcessed is treated as success
    await engine._idempotent_credit(
        beneficiary="0x" + "bb" * 20,
        token_id=b"\x11" * 32,
        amount=10**18,
        deposit_id=b"\x22" * 32,
    )


@pytest.mark.asyncio
async def test_idempotent_credit_reraises_other_errors(engine, mock_accounting):
    """_idempotent_credit re-raises non-DepositAlreadyProcessed errors."""
    mock_accounting.credit_deposit = AsyncMock(
        side_effect=TransactionRevertedError(
            "Transaction reverted: InsufficientBalance",
            error_name="InsufficientBalance",
        )
    )

    with pytest.raises(TransactionRevertedError, match="InsufficientBalance"):
        await engine._idempotent_credit(
            beneficiary="0x" + "bb" * 20,
            token_id=b"\x11" * 32,
            amount=10**18,
            deposit_id=b"\x22" * 32,
        )


@pytest.mark.asyncio
async def test_sweep_erc20_full_cycle(engine, mock_accounting):
    """ERC20 sweep: IDLE → PENDING → GAS_FUNDED → SWEPT → credit → IDLE."""
    "0x" + "aa" * 20
    token_addr = "0x" + "cc" * 20

    with (
        patch.object(engine, "_get_web3") as mock_get_w3,
        patch.object(
            engine,
            "_get_erc20_balance",
            new_callable=AsyncMock,
            return_value=1000 * 10**6,
        ),
        patch.object(
            engine,
            "_get_gas_tank_address",
            new_callable=AsyncMock,
            return_value="0x" + "ff" * 20,
        ),
    ):
        w3 = AsyncMock()
        w3.eth.get_balance = AsyncMock(return_value=0)  # no native gas at the address yet
        w3.eth.get_transaction_count = AsyncMock(return_value=0)
        w3.eth.gas_price = _AwaitableValue(1_000_000_000)  # 1 gwei
        w3.eth.get_block = AsyncMock(return_value={"number": 1000, "baseFeePerGas": 1_000_000_000})
        # Gas funding broadcast, then sweep broadcast
        w3.eth.send_raw_transaction = AsyncMock(side_effect=[b"\xaa" * 32, b"\xbb" * 32])
        # Gas funding receipt, then sweep receipt
        w3.eth.get_transaction_receipt = AsyncMock(
            side_effect=[
                {"status": 1, "blockNumber": 100},
                {"status": 1, "blockNumber": 101},
            ]
        )
        mock_get_w3.return_value = w3

        _plan(engine, token_addr)
        await engine.sweep(b"\x22" * 32)

    # Gas funding tx was generated
    mock_accounting.generate_gas_funding_tx.assert_called_once()
    # ERC20 sweep was generated
    mock_accounting.generate_sweep_erc20.assert_called_once()
    # Credit was called
    mock_accounting.credit_deposit.assert_called_once()
    # Record cleaned up (back to idle)
    assert engine.get_record_by_deposit_id("0x" + "22" * 32) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("asset", ["native", "erc20"])
async def test_receipt_after_chain_drop_keeps_the_record_for_recovery(
    engine, mock_accounting, monkeypatch, asset
):
    """The sweep holds one client through its receipt wait. A success receipt that
    arrives after re-verification dropped the chain must not credit the deposit or
    delete the record: recovery re-reads it through an endpoint that passed."""
    url = "https://fake-rpc.example.com"
    answers = {url: 84532}

    async def probe(probed_url: str, _timeout: float) -> int:
        return answers[probed_url]

    monkeypatch.setattr(rpc_identity, "_probe_chain_id", probe)
    settings = SimpleNamespace(
        chain_rpc_urls={84532: url},
        sapphire_rpc_url="",
        sapphire_rpc_headers={},
        sapphire_chain_id=23295,
    )
    await rpc_identity.initialize_verified_chain_rpc_urls(settings)

    w3 = AsyncMock()
    w3.eth.get_balance = AsyncMock(return_value=10**18 if asset == "native" else 0)
    w3.eth.get_transaction_count = AsyncMock(return_value=0)
    w3.eth.gas_price = _AwaitableValue(1_000_000_000)
    w3.eth.get_block = AsyncMock(return_value={"number": 1000, "baseFeePerGas": 1_000_000_000})
    w3.eth.send_raw_transaction = AsyncMock(side_effect=[b"\xaa" * 32, b"\xbb" * 32])
    receipt_waits = iter(["gas funding", "sweep"])

    async def receipt(_tx_hash):
        if next(receipt_waits) == "sweep":
            answers[url] = 1  # the endpoint drifts to another chain mid-wait
            await rpc_identity.reverify_chain_rpc_urls(settings)
        return {"status": 1, "blockNumber": 100}

    w3.eth.get_transaction_receipt = receipt
    monkeypatch.setitem(rpc_identity._clients, url, w3)
    monkeypatch.setattr(engine, "_get_gas_tank_address", AsyncMock(return_value="0x" + "ff" * 20))
    monkeypatch.setattr(engine, "_get_erc20_balance", AsyncMock(return_value=1000 * 10**6))

    _plan(engine, None if asset == "native" else "0x" + "cc" * 20)
    sweep = engine.sweep
    with pytest.raises(ValueError, match="No verified RPC endpoint for chain 84532"):
        await sweep(b"\x22" * 32)

    mock_accounting.credit_deposit.assert_not_called()
    record = engine.get_record_by_deposit_id("0x" + "22" * 32)
    assert record.state == SweepState.GAS_FUNDED
    signed_tx = b"\x01\x02\x03" if asset == "native" else b"\x04\x05\x06"
    assert record.sweep_tx_hash == "0x" + Web3.keccak(signed_tx).hex()
    assert record.sweep_tx == "0x" + signed_tx.hex()


@pytest.mark.asyncio
async def test_wait_for_receipt_timeout(engine):
    """_wait_for_receipt raises TimeoutError when receipt never arrives."""
    w3 = AsyncMock()
    w3.eth.get_transaction_receipt = AsyncMock(return_value=None)

    with pytest.raises(TimeoutError, match="not mined"):
        await engine._wait_for_receipt(w3, b"\xaa" * 32, timeout=0)


@pytest.mark.asyncio
async def test_wait_for_receipt_survives_rpc_errors(engine, monkeypatch):
    """_wait_for_receipt continues polling after transient RPC errors."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())  # the poll delay is real time otherwise
    w3 = AsyncMock()
    # First call: RPC error. Second call: success.
    w3.eth.get_transaction_receipt = AsyncMock(
        side_effect=[ConnectionError("RPC down"), {"status": 1, "blockNumber": 42}]
    )

    receipt = await engine._wait_for_receipt(w3, b"\xaa" * 32, timeout=10)
    assert receipt["status"] == 1


@pytest.mark.asyncio
async def test_sweep_native_includes_l1_data_fee(engine, mock_accounting):
    """On L2 chains, gas funding should include the L1 data fee."""
    "0x" + "aa" * 20
    l1_fee = 50_000_000_000_000  # 0.00005 ETH

    with (
        patch.object(engine, "_get_web3") as mock_get_w3,
        patch(L2_FEE_PATCH, new_callable=AsyncMock, return_value=l1_fee) as mock_fee,
        patch.object(
            engine,
            "_get_gas_tank_address",
            new_callable=AsyncMock,
            return_value="0x" + "ff" * 20,
        ),
    ):
        w3 = AsyncMock()
        w3.eth.get_balance = AsyncMock(return_value=10**18)
        w3.eth.get_transaction_count = AsyncMock(return_value=0)
        w3.eth.gas_price = _AwaitableValue(1_000_000_000)
        w3.eth.get_block = AsyncMock(return_value={"number": 1000, "baseFeePerGas": 1_000_000_000})
        w3.eth.send_raw_transaction = AsyncMock(return_value=b"\xdd" * 32)
        w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 1, "blockNumber": 100})
        mock_get_w3.return_value = w3

        _plan(engine)
        await engine.sweep(b"\x22" * 32)

    mock_fee.assert_called_once_with(w3, 84532, is_erc20=False)
    # gas_price = max(1.25 * 1 gwei base fee, 1 gwei rpc, floor) = 1.25 gwei;
    # gas_amount = native sweep gas * 1.25 gwei * 2 + l1_fee, under the 84532 cap
    call_kwargs = mock_accounting.generate_gas_funding_tx.call_args
    assert call_kwargs.kwargs["gas_amount"] == SWEEP_GAS_LIMIT_NATIVE * 1_250_000_000 * 2 + l1_fee


@pytest.mark.asyncio
async def test_sweep_erc20_includes_l1_data_fee(engine, mock_accounting):
    """On L2 chains, ERC20 gas funding should include the L1 data fee."""
    "0x" + "aa" * 20
    token_addr = "0x" + "cc" * 20
    l1_fee = 80_000_000_000_000  # ERC20 txs have more calldata

    with (
        patch.object(engine, "_get_web3") as mock_get_w3,
        patch(L2_FEE_PATCH, new_callable=AsyncMock, return_value=l1_fee) as mock_fee,
        patch.object(
            engine,
            "_get_erc20_balance",
            new_callable=AsyncMock,
            return_value=1000 * 10**6,
        ),
        patch.object(
            engine,
            "_get_gas_tank_address",
            new_callable=AsyncMock,
            return_value="0x" + "ff" * 20,
        ),
    ):
        w3 = AsyncMock()
        w3.eth.get_balance = AsyncMock(return_value=0)  # no native gas at the address yet
        w3.eth.get_transaction_count = AsyncMock(return_value=0)
        w3.eth.gas_price = _AwaitableValue(1_000_000_000)
        w3.eth.get_block = AsyncMock(return_value={"number": 1000, "baseFeePerGas": 1_000_000_000})
        w3.eth.send_raw_transaction = AsyncMock(side_effect=[b"\xaa" * 32, b"\xbb" * 32])
        w3.eth.get_transaction_receipt = AsyncMock(
            side_effect=[
                {"status": 1, "blockNumber": 100},
                {"status": 1, "blockNumber": 101},
            ]
        )
        mock_get_w3.return_value = w3

        _plan(engine, token_addr)
        await engine.sweep(b"\x22" * 32)

    mock_fee.assert_called_once_with(w3, 84532, is_erc20=True)
    # Derived amount (65k gas * 1.25 gwei * 2 + l1_fee = 242.5e12) exceeds the
    # 84532 cap, so funding is clamped to GAS_FUNDING_AMOUNT_WEI.
    call_kwargs = mock_accounting.generate_gas_funding_tx.call_args
    assert call_kwargs.kwargs["gas_amount"] == 200_000_000_000_000


@pytest.mark.asyncio
async def test_safe_gas_price_edge_signals(engine):
    """Explicit-null baseFeePerGas is normalized; no signal at all raises."""
    floor = MIN_SWEEP_GAS_PRICE_WEI[8453]
    w3 = AsyncMock()

    # Explicit null baseFeePerGas must not TypeError on None * 5 // 4.
    w3.eth.get_block = AsyncMock(return_value={"baseFeePerGas": None})
    w3.eth.gas_price = _AwaitableValue(1)
    assert await engine._get_safe_gas_price(w3, 8453) == floor

    # Base fee and RPC both zero: raise rather than broadcast a guessed price
    # that may never mine and wedge the shared gas-tank lock.
    w3.eth.get_block = AsyncMock(return_value={})
    w3.eth.gas_price = _AwaitableValue(0)
    with pytest.raises(ValueError, match="no gas price signal"):
        await engine._get_safe_gas_price(w3, 8453)


def test_persist_error_skips_record_with_broadcast_sweep_tx(engine):
    """A broadcast sweep tx must never be flagged as terminally errored."""
    deposit_id_hex = "0x" + "dd" * 32
    record = SweepRecord(
        token_id_hex="0x" + "11" * 32,
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.GAS_FUNDED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        deposit_id_hex=deposit_id_hex,
        sweep_tx_hash="0x" + "cc" * 32,
    )
    engine._save_record(record)

    engine.persist_error(deposit_id_hex, "receipt polling timed out")

    stored = engine.get_record_by_deposit_id(deposit_id_hex)
    assert stored is not None
    assert stored.error is None
    assert stored.sweep_tx_hash == "0x" + "cc" * 32


def test_persist_error_marks_record_without_sweep_tx(engine):
    deposit_id_hex = "0x" + "ee" * 32
    record = SweepRecord(
        token_id_hex="0x" + "11" * 32,
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.PENDING,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        deposit_id_hex=deposit_id_hex,
    )
    engine._save_record(record)

    engine.persist_error(deposit_id_hex, "gas funding failed")

    stored = engine.get_record_by_deposit_id(deposit_id_hex)
    assert stored is not None
    assert stored.error == "gas funding failed"


def _same_address_record(deposit_id_hex: str, token_id_hex: str) -> SweepRecord:
    return SweepRecord(
        token_id_hex=token_id_hex,
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.PENDING,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        deposit_id_hex=deposit_id_hex,
    )


ID_A, TOKEN_A = "0x" + "1a" * 32, "0x" + "a1" * 32
ID_B, TOKEN_B = "0x" + "2b" * 32, "0x" + "b2" * 32


def test_same_address_records_of_different_tokens_coexist(engine):
    """Records are keyed by (token, address): another token's sweep never shadows ours."""
    engine._save_record(_same_address_record(ID_A, TOKEN_A))
    engine._save_record(_same_address_record(ID_B, TOKEN_B))

    assert engine.get_record(TOKEN_A, "0x" + "aa" * 20).deposit_id_hex == ID_A
    assert engine.get_record_by_deposit_id(ID_B).deposit_id_hex == ID_B


def test_cleanup_record_leaves_same_address_sibling(engine):
    engine._save_record(_same_address_record(ID_A, TOKEN_A))
    engine._save_record(_same_address_record(ID_B, TOKEN_B))

    engine.cleanup_record(ID_A)

    assert engine.get_record_by_deposit_id(ID_A) is None
    assert engine.get_record_by_deposit_id(ID_B) is not None


def test_persist_error_targets_only_its_deposit(engine):
    """An error on one deposit must not land on a same-address sibling."""
    engine._save_record(_same_address_record(ID_A, TOKEN_A))
    engine._save_record(_same_address_record(ID_B, TOKEN_B))

    engine.persist_error(ID_A, "gas funding failed")

    assert engine.get_record_by_deposit_id(ID_A).error == "gas funding failed"
    assert engine.get_record_by_deposit_id(ID_B).error is None


def test_malformed_deposit_ids_are_not_found(engine):
    # Malformed ids on the read path are a client error, not ours: not-found.
    assert engine.get_record_by_deposit_id("0x../etc/passwd") is None


@pytest.mark.asyncio
async def test_claim_schedules_the_balance_without_amount_or_nonce(engine):
    w3 = AsyncMock()

    args = dict(
        deposit_address="0x" + "aa" * 20,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        chain_id=84532,
        token_address="0x" + "cc" * 20,
        token_id=b"\x11" * 32,
    )
    with (
        patch.object(engine, "_get_web3", return_value=w3),
        patch.object(engine, "_get_erc20_balance", AsyncMock(return_value=9_000_000)),
    ):
        claim = await engine.claim_balance(**args)
        again = await engine.claim_balance(**args)

    assert (claim.status, claim.amount) == (BalanceClaimStatus.STARTED, 9_000_000)
    assert (claim.record.amount, claim.record.sweep_tx) == (None, None)  # decided at signing
    assert (again.status, again.record.deposit_id_hex) == (
        BalanceClaimStatus.IN_FLIGHT,
        claim.record.deposit_id_hex,
    )


@pytest.mark.asyncio
async def test_claim_reschedules_a_record_that_failed_before_broadcast(engine):
    w3 = AsyncMock()
    w3.eth.get_block = AsyncMock(return_value={"number": 1000})
    w3.eth.get_balance = AsyncMock(return_value=10**18)
    args = dict(
        deposit_address="0x" + "aa" * 20,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        chain_id=84532,
        token_address=None,
        token_id=b"\x11" * 32,
    )
    with patch.object(engine, "_get_web3", return_value=w3):
        failed = (await engine.claim_balance(**args)).record
        engine.persist_error(failed.deposit_id_hex, "gas funding failed")
        retry = await engine.claim_balance(**args)

        assert retry.status is BalanceClaimStatus.STARTED
        assert retry.record.deposit_id_hex != failed.deposit_id_hex
        assert engine.get_record_by_deposit_id(failed.deposit_id_hex) is None

        # Once a sweep tx may exist the record is kept for recovery.
        retry.record.error, retry.record.sweep_tx_hash = "timed out", "0x" + "dd" * 32
        engine._save_record(retry.record)
        kept = await engine.claim_balance(**args)

    assert (kept.status, kept.record.deposit_id_hex) == (
        BalanceClaimStatus.IN_FLIGHT,
        retry.record.deposit_id_hex,
    )


@pytest.mark.asyncio
async def test_sweep_receipt_is_awaited_until_its_block_is_final(engine):
    """A sweep is credited only once its block is finality_depth deep (15 on this chain)."""
    w3 = AsyncMock()
    w3.eth.get_block = AsyncMock(side_effect=[{"number": 100}, {"number": 110}, {"number": 115}])
    w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 1, "blockNumber": 100})

    with patch("src.services.sweep_engine.asyncio.sleep", AsyncMock()):
        receipt = await engine._wait_for_final_receipt(w3, "0x" + "dd" * 32, 84532)

    assert receipt["blockNumber"] == 100
    assert w3.eth.get_block.await_count == 3


@pytest.mark.asyncio
async def test_claims_made_together_for_one_asset_schedule_a_single_sweep(engine):
    w3 = AsyncMock()
    w3.eth.get_block = AsyncMock(return_value={"number": 1000})
    w3.eth.get_balance = AsyncMock(return_value=10**18)
    args = dict(
        deposit_address="0x" + "aa" * 20,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        chain_id=84532,
        token_address=None,
        token_id=b"\x11" * 32,
    )
    with patch.object(engine, "_get_web3", return_value=w3):
        claims = await asyncio.gather(engine.claim_balance(**args), engine.claim_balance(**args))

    assert sorted(claim.status for claim in claims) == [
        BalanceClaimStatus.IN_FLIGHT,
        BalanceClaimStatus.STARTED,
    ]
    assert len({claim.record.deposit_id_hex for claim in claims}) == 1


@pytest.mark.asyncio
async def test_sweep_waiting_for_the_lock_skips_a_record_settled_meanwhile(engine, mock_accounting):
    """#299: a pass that queued behind a live sweep re-reads the record under the lock,
    finds it gone, and does not sweep again."""
    _plan(engine)
    lock = engine._get_address_lock("0x" + "aa" * 20, 84532)
    await lock.acquire()  # the live sweep
    stale_pass = asyncio.create_task(engine.sweep(b"\x22" * 32))
    await asyncio.sleep(0)
    engine.cleanup_record("0x" + "22" * 32)  # the live sweep settled and cleaned up
    lock.release()
    await stale_pass

    mock_accounting.generate_sweep_native.assert_not_called()
