"""Tests for the sweep state machine.

Mocks AccountingContractService and web3 to test state transitions
without hitting real chains.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.clients.rofl import TransactionRevertedError
from src.services.sweep_engine import SweepEngine, SweepRecord, SweepState

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


def test_initial_state_is_idle(engine):
    record = engine.get_sweep_record("0x" + "aa" * 20, 84532)
    assert record is None  # no record = idle


def test_state_persistence(engine, state_dir):
    """Sweep records should survive engine restart."""
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.PENDING,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
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
