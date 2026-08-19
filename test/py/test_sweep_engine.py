"""Tests for the sweep state machine.

Mocks AccountingContractService and web3 to test state transitions
without hitting real chains.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.clients.rofl import TransactionRevertedError
from src.config.chain_config import GAS_FUNDING_AMOUNT_WEI
from src.services.sweep_engine import SweepEngine, SweepRecord, SweepState

L2_FEE_PATCH = "src.services.sweep_engine.estimate_l1_data_fee"
NATIVE_SWEEP_GAS_LIMIT = 25_000
ERC20_SWEEP_GAS_LIMIT = 65_000


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
    # Contract sweep gas limits, the basis for gas funding (EVMSignerAndVerifier).
    svc.get_native_sweep_gas_limit = AsyncMock(return_value=NATIVE_SWEEP_GAS_LIMIT)
    svc.get_erc20_sweep_gas_limit = AsyncMock(return_value=ERC20_SWEEP_GAS_LIMIT)
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
    record = engine.get_record_by_deposit_id("0x" + "22" * 32)
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
    assert engine.get_record_by_deposit_id("0x" + "22" * 32) is None


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
    assert engine.get_record_by_deposit_id("0x" + "22" * 32) is None


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
    # gas_amount = 25,000 gas * 1 gwei * 1.3 headroom + l1_fee 50_000_000_000_000
    call_kwargs = mock_accounting.generate_gas_funding_tx.call_args
    assert call_kwargs.kwargs["gas_amount"] == 32_500_000_000_000 + l1_fee


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
    # gas_amount = 65,000 gas * 1 gwei * 1.3 headroom + l1_fee 80_000_000_000_000
    call_kwargs = mock_accounting.generate_gas_funding_tx.call_args
    assert call_kwargs.kwargs["gas_amount"] == 84_500_000_000_000 + l1_fee


@pytest.mark.asyncio
async def test_sweep_native_gas_funding_sized_from_contract_limit(engine, mock_accounting):
    """Native funding = gasLimitNativeSweep * the price the sweep is signed at * 1.3."""
    gas_price = 100_000_000_000  # 100 gwei

    with (
        patch.object(engine, "_get_web3") as mock_get_w3,
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
        w3.eth.gas_price = _AwaitableValue(gas_price)
        w3.eth.get_block = AsyncMock(return_value={"baseFeePerGas": gas_price})
        w3.eth.send_raw_transaction = AsyncMock(side_effect=[b"\xaa" * 32, b"\xbb" * 32])
        w3.eth.get_transaction_receipt = AsyncMock(
            side_effect=[
                {"status": 1, "blockNumber": 100},
                {"status": 1, "blockNumber": 101},
            ]
        )
        mock_get_w3.return_value = w3

        await engine.sweep_native(
            deposit_address="0x" + "aa" * 20,
            beneficiary="0x" + "bb" * 20,
            chain_type="evm",
            version=0,
            chain_id=84532,
            token_id=b"\x11" * 32,
            amount=10**18,
            deposit_id=b"\x22" * 32,
        )

    gas_kwargs = mock_accounting.generate_gas_funding_tx.call_args.kwargs
    assert gas_kwargs["gas_amount"] == NATIVE_SWEEP_GAS_LIMIT * gas_price * 13 // 10
    # Funding and sweep must agree on the price, or the funding can fall short
    assert gas_kwargs["gas_price"] == gas_price
    assert mock_accounting.generate_sweep_native.call_args.kwargs["gas_price"] == gas_price


@pytest.mark.asyncio
async def test_sweep_erc20_gas_funding_sized_from_contract_limit(engine, mock_accounting):
    """ERC20 funding = gasLimitERC20Sweep * the price the sweep is signed at * 1.3."""
    gas_price = 100_000_000_000  # 100 gwei

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
        w3.eth.gas_price = _AwaitableValue(gas_price)
        w3.eth.get_block = AsyncMock(return_value={"baseFeePerGas": gas_price})
        w3.eth.send_raw_transaction = AsyncMock(side_effect=[b"\xaa" * 32, b"\xbb" * 32])
        w3.eth.get_transaction_receipt = AsyncMock(
            side_effect=[
                {"status": 1, "blockNumber": 100},
                {"status": 1, "blockNumber": 101},
            ]
        )
        mock_get_w3.return_value = w3

        await engine.sweep_erc20(
            deposit_address="0x" + "aa" * 20,
            beneficiary="0x" + "bb" * 20,
            chain_type="evm",
            version=0,
            chain_id=84532,
            token_address="0x" + "cc" * 20,
            token_id=b"\x11" * 32,
            amount=1000 * 10**6,
            deposit_id=b"\x22" * 32,
        )

    gas_kwargs = mock_accounting.generate_gas_funding_tx.call_args.kwargs
    assert gas_kwargs["gas_amount"] == ERC20_SWEEP_GAS_LIMIT * gas_price * 13 // 10
    assert gas_kwargs["gas_price"] == gas_price
    assert mock_accounting.generate_sweep_erc20.call_args.kwargs["gas_price"] == gas_price


@pytest.mark.asyncio
async def test_gas_funding_falls_back_to_static_amount(engine, mock_accounting):
    """An unreadable contract limit funds the chain's static GAS_FUNDING_AMOUNT_WEI."""
    mock_accounting.get_native_sweep_gas_limit = AsyncMock(
        side_effect=ValueError("SAPPHIRE_RPC_URL must be configured")
    )

    with (
        patch.object(engine, "_get_web3") as mock_get_w3,
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
        w3.eth.send_raw_transaction = AsyncMock(side_effect=[b"\xaa" * 32, b"\xbb" * 32])
        w3.eth.get_transaction_receipt = AsyncMock(
            side_effect=[
                {"status": 1, "blockNumber": 100},
                {"status": 1, "blockNumber": 101},
            ]
        )
        mock_get_w3.return_value = w3

        await engine.sweep_native(
            deposit_address="0x" + "aa" * 20,
            beneficiary="0x" + "bb" * 20,
            chain_type="evm",
            version=0,
            chain_id=84532,
            token_id=b"\x11" * 32,
            amount=10**18,
            deposit_id=b"\x22" * 32,
        )

    gas_kwargs = mock_accounting.generate_gas_funding_tx.call_args.kwargs
    assert gas_kwargs["gas_amount"] == GAS_FUNDING_AMOUNT_WEI[84532]


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


def _same_address_record(deposit_id_hex: str) -> SweepRecord:
    return SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.PENDING,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        deposit_id_hex=deposit_id_hex,
    )


def test_same_address_records_coexist(engine):
    """Two deposits to the same address keep independent records.

    Records are keyed by deposit_id, so a later same-address deposit must
    not overwrite or shadow an earlier one's persisted state.
    """
    id_a = "0x" + "1a" * 32
    id_b = "0x" + "2b" * 32
    engine._save_record(_same_address_record(id_a))
    engine._save_record(_same_address_record(id_b))

    stored_a = engine.get_record_by_deposit_id(id_a)
    stored_b = engine.get_record_by_deposit_id(id_b)
    assert stored_a is not None and stored_a.deposit_id_hex == id_a
    assert stored_b is not None and stored_b.deposit_id_hex == id_b


def test_cleanup_record_leaves_same_address_sibling(engine):
    id_a = "0x" + "1a" * 32
    id_b = "0x" + "2b" * 32
    engine._save_record(_same_address_record(id_a))
    engine._save_record(_same_address_record(id_b))

    engine.cleanup_record(id_a)

    assert engine.get_record_by_deposit_id(id_a) is None
    assert engine.get_record_by_deposit_id(id_b) is not None


def test_persist_error_targets_only_its_deposit(engine):
    """An error on one deposit must not land on a same-address sibling."""
    id_a = "0x" + "1a" * 32
    id_b = "0x" + "2b" * 32
    engine._save_record(_same_address_record(id_a))
    engine._save_record(_same_address_record(id_b))

    engine.persist_error(id_a, "gas funding failed")

    stored_a = engine.get_record_by_deposit_id(id_a)
    stored_b = engine.get_record_by_deposit_id(id_b)
    assert stored_a is not None and stored_a.error == "gas funding failed"
    assert stored_b is not None and stored_b.error is None


def test_save_record_requires_deposit_id(engine):
    record = _same_address_record("0x" + "1a" * 32)
    for bad_id in ("", "0x12", "0x" + "zz" * 32, "0x../" + "22" * 31):
        record.deposit_id_hex = bad_id
        with pytest.raises(ValueError):
            engine._save_record(record)
    # Malformed ids on the read path are a client error, not ours: not-found.
    assert engine.get_record_by_deposit_id("0x../etc/passwd") is None
