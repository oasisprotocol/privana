"""Tests for the sweep state machine.

Mocks AccountingContractService and web3 to test state transitions
without hitting real chains.
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest

from src.clients.rofl import TransactionRevertedError
from src.services.accounting_contract import DepositLockAuthorizationValidationError
from src.services.sweep_engine import SweepEngine, SweepRecord, SweepState

L2_FEE_PATCH = "src.services.sweep_engine.estimate_l1_data_fee"
SWEEP_LOGGER = "src.services.sweep_engine"

LOCK_AUTHORIZATION = {
    "service_address": "0x" + "cc" * 20,
    "token_id": "0x" + "11" * 32,
    "max_amount": "1000000",
    "min_amount": "0",
    "lock_duration": "3600",
    "authorization_deadline": "9999999999",
    "intent_id": "0x" + "33" * 32,
    "signature": "0x" + "ab" * 65,
}


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
    svc.credit_deposit_and_create_lock = AsyncMock()
    svc.has_deposit_lock_authorization_executed = AsyncMock(return_value=False)
    return svc


@pytest.fixture
def engine(state_dir, mock_accounting):
    chain_rpc_urls = {84532: "https://fake-rpc.example.com"}
    return SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls=chain_rpc_urls,
        state_dir=state_dir,
    )


def test_initial_state_is_idle(engine):
    record = engine.get_sweep_record("0x" + "aa" * 20, 84532)
    assert record is None  # no record = idle


def test_state_persistence(engine, state_dir):
    """Sweep records should survive engine restart."""
    lock_authorization = {
        "service_address": "0x" + "cc" * 20,
        "token_id": "0x" + "11" * 32,
        "max_amount": "1000000",
        "min_amount": "0",
        "lock_duration": "3600",
        "authorization_deadline": "9999999999",
        "intent_id": "0x" + "33" * 32,
        "signature": "0x" + "ab" * 65,
    }
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.PENDING,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        lock_authorization=lock_authorization,
    )
    engine._save_record(record)

    # Create a new engine instance (simulates restart)
    engine2 = SweepEngine(
        accounting_service=engine._accounting,
        chain_rpc_urls={84532: "https://fake-rpc.example.com"},
        state_dir=state_dir,
    )
    loaded = engine2.get_sweep_record("0x" + "aa" * 20, 84532)
    assert loaded is not None
    assert loaded.state == SweepState.PENDING
    assert loaded.lock_authorization == lock_authorization


def test_attach_lock_authorization_updates_in_flight_record(engine):
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.GAS_FUNDED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
    )
    engine._save_record(record)

    lock_authorization = {
        "service_address": "0x" + "cc" * 20,
        "token_id": "0x" + "11" * 32,
        "max_amount": "1000000",
        "min_amount": "0",
        "lock_duration": "3600",
        "authorization_deadline": "9999999999",
        "intent_id": "0x" + "33" * 32,
        "signature": "0x" + "ab" * 65,
    }

    assert engine.attach_lock_authorization("0x" + "22" * 32, lock_authorization) is True
    loaded = engine.get_record_by_deposit_id("0x" + "22" * 32)
    assert loaded is not None
    assert loaded.lock_authorization == lock_authorization


def test_attach_lock_authorization_rejects_different_intent(engine):
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.GAS_FUNDED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        lock_authorization={"intent_id": "0x" + "33" * 32},
    )
    engine._save_record(record)

    with pytest.raises(ValueError, match="different lock_authorization"):
        engine.attach_lock_authorization(
            "0x" + "22" * 32,
            {"intent_id": "0x" + "44" * 32},
        )


def test_attach_lock_authorization_reopens_lock_failed_same_intent(engine):
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.LOCK_FAILED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
        error="Transaction reverted: InvalidExpiry",
        lock_authorization={"intent_id": "0x" + "33" * 32},
    )
    engine._save_record(record)

    assert engine.attach_lock_authorization(
        "0x" + "22" * 32,
        {
            "intent_id": "0x" + "33" * 32,
            "signature": "0x" + "ab" * 65,
        },
    )

    loaded = engine.get_record_by_deposit_id("0x" + "22" * 32)
    assert loaded is not None
    assert loaded.state == SweepState.SWEPT
    assert loaded.error is None
    assert loaded.lock_authorization == {
        "intent_id": "0x" + "33" * 32,
        "signature": "0x" + "ab" * 65,
    }


@pytest.mark.asyncio
async def test_sweep_native_full_cycle(engine, mock_accounting):
    """Native sweep: IDLE → PENDING → SWEPT → credit → IDLE."""
    deposit_addr = "0x" + "aa" * 20

    with patch.object(engine, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        # Balance query
        w3.eth.get_balance = AsyncMock(return_value=10**18)
        w3.eth.get_transaction_count = AsyncMock(return_value=0)
        w3.eth.gas_price = _AwaitableValue(1_000_000_000)  # 1 gwei
        w3.eth.get_block = AsyncMock(return_value={"baseFeePerGas": 1_000_000_000})
        # Broadcast sweep tx
        w3.eth.send_raw_transaction = AsyncMock(return_value=b"\xdd" * 32)
        # Wait for sweep confirmation
        w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 1, "blockNumber": 100})
        mock_get_w3.return_value = w3

        await engine.sweep_native(
            deposit_address=deposit_addr,
            beneficiary="0x" + "bb" * 20,
            chain_type="evm",
            version=0,
            chain_id=84532,
            token_id=b"\x11" * 32,
            amount=10**18,
            deposit_id=b"\x22" * 32,
        )

    # Sweep signing was called
    mock_accounting.generate_sweep_native.assert_called_once()
    # Credit was called after sweep
    mock_accounting.credit_deposit.assert_called_once()
    # State is back to idle (record cleaned up)
    assert engine.get_sweep_record(deposit_addr, 84532) is None


@pytest.mark.asyncio
async def test_sweep_native_zero_balance_already_processed(engine, mock_accounting):
    """When balance=0 and deposit already processed (race condition), return success."""
    deposit_addr = "0x" + "aa" * 20
    mock_accounting.is_deposit_processed = AsyncMock(return_value=True)

    with patch.object(engine, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        w3.eth.get_balance = AsyncMock(return_value=0)
        mock_get_w3.return_value = w3

        await engine.sweep_native(
            deposit_address=deposit_addr,
            beneficiary="0x" + "bb" * 20,
            chain_type="evm",
            version=0,
            chain_id=84532,
            token_id=b"\x11" * 32,
            amount=10**18,
            deposit_id=b"\x22" * 32,
        )

    mock_accounting.generate_sweep_native.assert_not_called()
    mock_accounting.credit_deposit.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_native_zero_balance_not_processed_rejects(engine, mock_accounting):
    """When balance=0 but deposit NOT yet processed, reject — nothing to sweep."""
    deposit_addr = "0x" + "aa" * 20
    mock_accounting.is_deposit_processed = AsyncMock(return_value=False)

    with patch.object(engine, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        w3.eth.get_balance = AsyncMock(return_value=0)
        mock_get_w3.return_value = w3

        with pytest.raises(ValueError, match="nothing to sweep"):
            await engine.sweep_native(
                deposit_address=deposit_addr,
                beneficiary="0x" + "bb" * 20,
                chain_type="evm",
                version=0,
                chain_id=84532,
                token_id=b"\x11" * 32,
                amount=10**18,
                deposit_id=b"\x22" * 32,
            )

    mock_accounting.credit_deposit.assert_not_called()


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
async def test_idempotent_credit_uses_lock_authorization(engine, mock_accounting):
    """When lock authorization is present, credit+lock is submitted atomically."""
    lock_authorization = {
        "service_address": "0x" + "cc" * 20,
        "token_id": "0x" + "11" * 32,
        "max_amount": "1000000",
        "min_amount": "0",
        "lock_duration": "3600",
        "authorization_deadline": "9999999999",
        "intent_id": "0x" + "33" * 32,
        "signature": "0x" + "ab" * 65,
    }

    await engine._idempotent_credit(
        beneficiary="0x" + "bb" * 20,
        token_id=b"\x11" * 32,
        amount=10**18,
        deposit_id=b"\x22" * 32,
        lock_authorization=lock_authorization,
    )

    mock_accounting.credit_deposit_and_create_lock.assert_awaited_once()
    mock_accounting.credit_deposit.assert_not_called()


@pytest.mark.asyncio
async def test_idempotent_credit_with_lock_reraises_lock_authorization_revert(
    engine, mock_accounting
):
    """Rejected lock authorization must not downgrade to unlocked credit."""
    mock_accounting.credit_deposit_and_create_lock = AsyncMock(
        side_effect=TransactionRevertedError(
            "Transaction reverted: InvalidSignature",
            error_name="InvalidSignature",
        )
    )

    with pytest.raises(TransactionRevertedError, match="InvalidSignature"):
        await engine._idempotent_credit(
            beneficiary="0x" + "bb" * 20,
            token_id=b"\x11" * 32,
            amount=10**18,
            deposit_id=b"\x22" * 32,
            lock_authorization={"signature": "bad"},
        )

    mock_accounting.credit_deposit.assert_not_called()


@pytest.mark.asyncio
async def test_idempotent_credit_with_lock_handles_already_processed(engine, mock_accounting):
    mock_accounting.has_deposit_lock_authorization_executed = AsyncMock(return_value=True)
    mock_accounting.credit_deposit_and_create_lock = AsyncMock(
        side_effect=TransactionRevertedError(
            "Transaction reverted: DepositAlreadyProcessed",
            error_name="DepositAlreadyProcessed",
        )
    )

    await engine._idempotent_credit(
        beneficiary="0x" + "bb" * 20,
        token_id=b"\x11" * 32,
        amount=10**18,
        deposit_id=b"\x22" * 32,
        lock_authorization={"intent_id": "0x" + "33" * 32, "signature": "already-used"},
    )

    mock_accounting.credit_deposit.assert_not_called()
    mock_accounting.has_deposit_lock_authorization_executed.assert_awaited_once_with(
        "0x" + "bb" * 20,
        "0x" + "33" * 32,
    )


@pytest.mark.asyncio
async def test_idempotent_credit_with_lock_rejects_already_processed_without_lock(
    engine, mock_accounting
):
    mock_accounting.has_deposit_lock_authorization_executed = AsyncMock(return_value=False)
    mock_accounting.credit_deposit_and_create_lock = AsyncMock(
        side_effect=TransactionRevertedError(
            "Transaction reverted: DepositAlreadyProcessed",
            error_name="DepositAlreadyProcessed",
        )
    )

    with pytest.raises(
        DepositLockAuthorizationValidationError,
        match="processed without requested lock_authorization",
    ):
        await engine._idempotent_credit(
            beneficiary="0x" + "bb" * 20,
            token_id=b"\x11" * 32,
            amount=10**18,
            deposit_id=b"\x22" * 32,
            lock_authorization={"intent_id": "0x" + "33" * 32, "signature": "already-used"},
        )

    mock_accounting.credit_deposit.assert_not_called()
    mock_accounting.has_deposit_lock_authorization_executed.assert_awaited_once_with(
        "0x" + "bb" * 20,
        "0x" + "33" * 32,
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
    deposit_addr = "0x" + "aa" * 20
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
        w3.eth.get_transaction_count = AsyncMock(return_value=0)
        w3.eth.gas_price = _AwaitableValue(1_000_000_000)  # 1 gwei
        w3.eth.get_block = AsyncMock(return_value={"baseFeePerGas": 1_000_000_000})
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

        await engine.sweep_erc20(
            deposit_address=deposit_addr,
            beneficiary="0x" + "bb" * 20,
            chain_type="evm",
            version=0,
            chain_id=84532,
            token_address=token_addr,
            token_id=b"\x11" * 32,
            amount=1000 * 10**6,
            deposit_id=b"\x22" * 32,
        )

    # Gas funding tx was generated
    mock_accounting.generate_gas_funding_tx.assert_called_once()
    # ERC20 sweep was generated
    mock_accounting.generate_sweep_erc20.assert_called_once()
    # Credit was called
    mock_accounting.credit_deposit.assert_called_once()
    # Gas funding tx hash is tracked (prevents claiming as deposit)
    gas_tx_hash_hex = "0x" + "aa" * 32
    assert gas_tx_hash_hex.lower() in engine._gas_funding_tx_hashes
    # Record cleaned up (back to idle)
    assert engine.get_sweep_record(deposit_addr, 84532) is None


@pytest.mark.asyncio
async def test_sweep_erc20_zero_balance_already_processed(engine, mock_accounting):
    """When ERC20 balance=0 and deposit already processed (race), return success."""
    deposit_addr = "0x" + "aa" * 20
    token_addr = "0x" + "cc" * 20
    mock_accounting.is_deposit_processed = AsyncMock(return_value=True)

    with (
        patch.object(engine, "_get_web3") as mock_get_w3,
        patch.object(
            engine,
            "_get_erc20_balance",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        w3 = AsyncMock()
        mock_get_w3.return_value = w3

        await engine.sweep_erc20(
            deposit_address=deposit_addr,
            beneficiary="0x" + "bb" * 20,
            chain_type="evm",
            version=0,
            chain_id=84532,
            token_address=token_addr,
            token_id=b"\x11" * 32,
            amount=1000 * 10**6,
            deposit_id=b"\x22" * 32,
        )

    mock_accounting.generate_gas_funding_tx.assert_not_called()
    mock_accounting.generate_sweep_erc20.assert_not_called()
    mock_accounting.credit_deposit.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_erc20_zero_balance_not_processed_rejects(engine, mock_accounting):
    """When ERC20 balance=0 but deposit NOT yet processed, reject."""
    deposit_addr = "0x" + "aa" * 20
    token_addr = "0x" + "cc" * 20
    mock_accounting.is_deposit_processed = AsyncMock(return_value=False)

    with (
        patch.object(engine, "_get_web3") as mock_get_w3,
        patch.object(
            engine,
            "_get_erc20_balance",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        w3 = AsyncMock()
        mock_get_w3.return_value = w3

        with pytest.raises(ValueError, match="nothing to sweep"):
            await engine.sweep_erc20(
                deposit_address=deposit_addr,
                beneficiary="0x" + "bb" * 20,
                chain_type="evm",
                version=0,
                chain_id=84532,
                token_address=token_addr,
                token_id=b"\x11" * 32,
                amount=1000 * 10**6,
                deposit_id=b"\x22" * 32,
            )

    mock_accounting.credit_deposit.assert_not_called()


@pytest.mark.asyncio
async def test_wait_for_receipt_timeout(engine):
    """_wait_for_receipt raises TimeoutError when receipt never arrives."""
    w3 = AsyncMock()
    w3.eth.get_transaction_receipt = AsyncMock(return_value=None)

    with pytest.raises(TimeoutError, match="not mined"):
        await engine._wait_for_receipt(w3, b"\xaa" * 32, timeout=0)


@pytest.mark.asyncio
async def test_wait_for_receipt_survives_rpc_errors(engine):
    """_wait_for_receipt continues polling after transient RPC errors."""
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
    deposit_addr = "0x" + "aa" * 20
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
        w3.eth.get_block = AsyncMock(return_value={"baseFeePerGas": 1_000_000_000})
        w3.eth.send_raw_transaction = AsyncMock(return_value=b"\xdd" * 32)
        w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 1, "blockNumber": 100})
        mock_get_w3.return_value = w3

        await engine.sweep_native(
            deposit_address=deposit_addr,
            beneficiary="0x" + "bb" * 20,
            chain_type="evm",
            version=0,
            chain_id=84532,
            token_id=b"\x11" * 32,
            amount=10**18,
            deposit_id=b"\x22" * 32,
        )

    mock_fee.assert_called_once_with(w3, 84532, is_erc20=False)
    # gas_amount = static 200_000_000_000_000 + l1_fee 50_000_000_000_000
    call_kwargs = mock_accounting.generate_gas_funding_tx.call_args
    assert call_kwargs.kwargs["gas_amount"] == 200_000_000_000_000 + l1_fee


@pytest.mark.asyncio
async def test_sweep_erc20_includes_l1_data_fee(engine, mock_accounting):
    """On L2 chains, ERC20 gas funding should include the L1 data fee."""
    deposit_addr = "0x" + "aa" * 20
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
        w3.eth.get_transaction_count = AsyncMock(return_value=0)
        w3.eth.gas_price = _AwaitableValue(1_000_000_000)
        w3.eth.get_block = AsyncMock(return_value={"baseFeePerGas": 1_000_000_000})
        w3.eth.send_raw_transaction = AsyncMock(side_effect=[b"\xaa" * 32, b"\xbb" * 32])
        w3.eth.get_transaction_receipt = AsyncMock(
            side_effect=[
                {"status": 1, "blockNumber": 100},
                {"status": 1, "blockNumber": 101},
            ]
        )
        mock_get_w3.return_value = w3

        await engine.sweep_erc20(
            deposit_address=deposit_addr,
            beneficiary="0x" + "bb" * 20,
            chain_type="evm",
            version=0,
            chain_id=84532,
            token_address=token_addr,
            token_id=b"\x11" * 32,
            amount=1000 * 10**6,
            deposit_id=b"\x22" * 32,
        )

    mock_fee.assert_called_once_with(w3, 84532, is_erc20=True)
    call_kwargs = mock_accounting.generate_gas_funding_tx.call_args
    assert call_kwargs.kwargs["gas_amount"] == 200_000_000_000_000 + l1_fee


def test_persist_error_skips_record_with_broadcast_sweep_tx(engine):
    """A broadcast sweep tx must never be flagged as terminally errored."""
    deposit_id_hex = "0x" + "dd" * 32
    record = SweepRecord(
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


def _register_pending(engine, *, lock_authorization=None, deposit_id_hex="0x" + "22" * 32):
    """Pre-register a PENDING record the way process_deposit does."""
    return engine.register_pending_sweep(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        token_address=None,
        deposit_id_hex=deposit_id_hex,
        lock_authorization=lock_authorization,
    )


@pytest.mark.asyncio
async def test_resolve_already_processed_unexecuted_lock_marks_lock_failed(
    engine, mock_accounting, caplog
):
    """balance=0 + already processed + unexecuted lock auth → LOCK_FAILED, warning not page."""
    deposit_addr = "0x" + "aa" * 20
    mock_accounting.is_deposit_processed = AsyncMock(return_value=True)
    mock_accounting.has_deposit_lock_authorization_executed = AsyncMock(return_value=False)
    _register_pending(engine, lock_authorization=LOCK_AUTHORIZATION)

    with (
        patch.object(engine, "_get_web3") as mock_get_w3,
        caplog.at_level(logging.DEBUG, logger=SWEEP_LOGGER),
    ):
        w3 = AsyncMock()
        w3.eth.get_balance = AsyncMock(return_value=0)
        mock_get_w3.return_value = w3

        await engine.sweep_native(
            deposit_address=deposit_addr,
            beneficiary="0x" + "bb" * 20,
            chain_type="evm",
            version=0,
            chain_id=84532,
            token_id=b"\x11" * 32,
            amount=10**18,
            deposit_id=b"\x22" * 32,
            lock_authorization=LOCK_AUTHORIZATION,
        )

    record = engine.get_sweep_record(deposit_addr, 84532)
    assert record is not None
    assert record.state == SweepState.LOCK_FAILED
    assert record.credited_without_lock is True
    assert "without requested lock_authorization" in record.error
    mock_accounting.credit_deposit.assert_not_called()

    sweep_logs = [r for r in caplog.records if r.name == SWEEP_LOGGER]
    assert any(r.levelname == "WARNING" for r in sweep_logs)
    assert not any(r.levelname == "CRITICAL" for r in sweep_logs)


@pytest.mark.asyncio
async def test_resolve_already_processed_executed_lock_deletes_record(engine, mock_accounting):
    """balance=0 + already processed + lock auth already executed on-chain → record deleted."""
    deposit_addr = "0x" + "aa" * 20
    mock_accounting.is_deposit_processed = AsyncMock(return_value=True)
    mock_accounting.has_deposit_lock_authorization_executed = AsyncMock(return_value=True)
    _register_pending(engine, lock_authorization=LOCK_AUTHORIZATION)

    with patch.object(engine, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        w3.eth.get_balance = AsyncMock(return_value=0)
        mock_get_w3.return_value = w3

        await engine.sweep_native(
            deposit_address=deposit_addr,
            beneficiary="0x" + "bb" * 20,
            chain_type="evm",
            version=0,
            chain_id=84532,
            token_id=b"\x11" * 32,
            amount=10**18,
            deposit_id=b"\x22" * 32,
            lock_authorization=LOCK_AUTHORIZATION,
        )

    assert engine.get_sweep_record(deposit_addr, 84532) is None
    mock_accounting.credit_deposit.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_already_processed_no_lock_deletes_record(engine, mock_accounting):
    """balance=0 + already processed + no lock auth → stale PENDING record is deleted."""
    deposit_addr = "0x" + "aa" * 20
    mock_accounting.is_deposit_processed = AsyncMock(return_value=True)
    _register_pending(engine)

    with patch.object(engine, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        w3.eth.get_balance = AsyncMock(return_value=0)
        mock_get_w3.return_value = w3

        await engine.sweep_native(
            deposit_address=deposit_addr,
            beneficiary="0x" + "bb" * 20,
            chain_type="evm",
            version=0,
            chain_id=84532,
            token_id=b"\x11" * 32,
            amount=10**18,
            deposit_id=b"\x22" * 32,
        )

    assert engine.get_sweep_record(deposit_addr, 84532) is None
    mock_accounting.credit_deposit.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_reuses_registered_record_and_credits_with_attached_lock(
    engine, mock_accounting
):
    """A record registered without auth, then attached, must credit+lock with that auth."""
    deposit_addr = "0x" + "aa" * 20
    _register_pending(engine)
    assert engine.attach_lock_authorization("0x" + "22" * 32, LOCK_AUTHORIZATION)

    with patch.object(engine, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        w3.eth.get_balance = AsyncMock(return_value=10**18)
        w3.eth.get_transaction_count = AsyncMock(return_value=0)
        w3.eth.gas_price = _AwaitableValue(1_000_000_000)
        w3.eth.get_block = AsyncMock(return_value={"baseFeePerGas": 1_000_000_000})
        w3.eth.send_raw_transaction = AsyncMock(return_value=b"\xdd" * 32)
        w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 1, "blockNumber": 100})
        mock_get_w3.return_value = w3

        # No lock_authorization passed here — it must come from the attached record.
        await engine.sweep_native(
            deposit_address=deposit_addr,
            beneficiary="0x" + "bb" * 20,
            chain_type="evm",
            version=0,
            chain_id=84532,
            token_id=b"\x11" * 32,
            amount=10**18,
            deposit_id=b"\x22" * 32,
        )

    mock_accounting.credit_deposit_and_create_lock.assert_awaited_once()
    assert (
        mock_accounting.credit_deposit_and_create_lock.call_args.kwargs["lock_authorization"]
        == LOCK_AUTHORIZATION
    )
    mock_accounting.credit_deposit.assert_not_called()
    assert engine.get_sweep_record(deposit_addr, 84532) is None


def test_get_record_by_deposit_id_ignores_stale_index_after_clobber(engine):
    """Same-address re-deposit overwrites the file; the old deposit_id must resolve to None."""
    deposit_a = "0x" + "22" * 32
    deposit_b = "0x" + "44" * 32
    _register_pending(engine, deposit_id_hex=deposit_a)
    _register_pending(engine, deposit_id_hex=deposit_b)

    assert engine.get_record_by_deposit_id(deposit_a) is None
    record_b = engine.get_record_by_deposit_id(deposit_b)
    assert record_b is not None
    assert record_b.deposit_id_hex == deposit_b


def test_mark_lock_authorization_failed_logs_critical_for_plain_validation_error(engine, caplog):
    """A non-DepositCreditedWithoutLock validation failure still pages (CRITICAL)."""
    record = _register_pending(engine, lock_authorization=LOCK_AUTHORIZATION)

    with caplog.at_level(logging.DEBUG, logger=SWEEP_LOGGER):
        engine._mark_lock_authorization_failed(
            record, DepositLockAuthorizationValidationError("bad signature")
        )

    persisted = engine.get_sweep_record(record.deposit_address, record.chain_id)
    assert persisted is not None
    assert persisted.state == SweepState.LOCK_FAILED

    sweep_logs = [r for r in caplog.records if r.name == SWEEP_LOGGER]
    assert any(r.levelname == "CRITICAL" for r in sweep_logs)


def test_attach_lock_authorization_stays_terminal_for_credited_without_lock(engine):
    """No authorization can execute for an already-credited deposit: never reopen."""
    record = _register_pending(engine, lock_authorization=LOCK_AUTHORIZATION)
    record.state = SweepState.LOCK_FAILED
    record.error = "deposit already processed without requested lock_authorization"
    record.credited_without_lock = True
    engine._save_record(record)

    fresh_intent = dict(LOCK_AUTHORIZATION, intent_id="0x" + "55" * 32)
    assert engine.attach_lock_authorization("0x" + "22" * 32, fresh_intent) is False

    persisted = engine.get_sweep_record(record.deposit_address, record.chain_id)
    assert persisted.state == SweepState.LOCK_FAILED
    assert persisted.credited_without_lock is True
    assert persisted.lock_authorization == LOCK_AUTHORIZATION


@pytest.mark.asyncio
async def test_credited_without_lock_after_sweep_is_terminal_not_credit_pending(
    engine, mock_accounting, caplog
):
    """A race-lost credit+lock after a successful sweep must not report
    'credit pending' — the deposit is credited, only the lock is missing."""
    deposit_addr = "0x" + "aa" * 20
    mock_accounting.has_deposit_lock_authorization_executed = AsyncMock(return_value=False)
    mock_accounting.credit_deposit_and_create_lock = AsyncMock(
        side_effect=TransactionRevertedError(
            "Transaction reverted: DepositAlreadyProcessed",
            error_name="DepositAlreadyProcessed",
        )
    )
    _register_pending(engine, lock_authorization=LOCK_AUTHORIZATION)

    with (
        patch.object(engine, "_get_web3") as mock_get_w3,
        caplog.at_level(logging.DEBUG, logger=SWEEP_LOGGER),
    ):
        w3 = AsyncMock()
        w3.eth.get_balance = AsyncMock(return_value=10**18)
        w3.eth.get_transaction_count = AsyncMock(return_value=0)
        w3.eth.gas_price = _AwaitableValue(1_000_000_000)
        w3.eth.get_block = AsyncMock(return_value={"baseFeePerGas": 1_000_000_000})
        w3.eth.send_raw_transaction = AsyncMock(return_value=b"\xdd" * 32)
        w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 1, "blockNumber": 100})
        mock_get_w3.return_value = w3

        # Must return cleanly — no SweepCreditPendingError.
        await engine.sweep_native(
            deposit_address=deposit_addr,
            beneficiary="0x" + "bb" * 20,
            chain_type="evm",
            version=0,
            chain_id=84532,
            token_id=b"\x11" * 32,
            amount=10**18,
            deposit_id=b"\x22" * 32,
        )

    record = engine.get_sweep_record(deposit_addr, 84532)
    assert record is not None
    assert record.state == SweepState.LOCK_FAILED
    assert record.credited_without_lock is True

    sweep_logs = [r for r in caplog.records if r.name == SWEEP_LOGGER]
    assert not any("Credit failed" in r.message for r in sweep_logs)
    assert not any(r.levelname == "CRITICAL" for r in sweep_logs)
