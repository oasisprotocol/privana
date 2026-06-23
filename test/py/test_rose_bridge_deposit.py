import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from web3 import Web3

from src.config.chain_config import SWEEP_GAS_LIMIT_NATIVE
from src.models.private_read import PrivateReadAuth
from src.models.types import Settings
from src.services.custody_tx_executor import CustodyTxKind, CustodyTxStatus
from src.services.deposit_processor import DepositProcessor, compute_deposit_id
from src.services.deposit_verifier import DepositVerifier, VerifiedDeposit
from src.services.sweep_engine import (
    FLOW_XROSE_BRIDGE_IN,
    SweepCreditPendingError,
    SweepEngine,
    SweepState,
)

SAPPHIRE_CHAIN_ID = 23295
BASE_CHAIN_ID = 84532
DEPOSIT_ADDR = Web3.to_checksum_address("0x" + "aa" * 20)
BENEFICIARY = Web3.to_checksum_address("0x" + "bb" * 20)
CUSTODY_ADDR = Web3.to_checksum_address("0x" + "ff" * 20)
XROSE_ADDR = Web3.to_checksum_address("0x" + "ee" * 20)
ROFL_BRIDGE_ADDR = Web3.to_checksum_address("0x" + "dd" * 20)
GAS_TANK_ADDR = Web3.to_checksum_address("0x" + "ab" * 20)
TX_HASH = "0x" + "cc" * 32
XROSE_TX_HASH = "0x" + "11" * 32
ROSE_TOKEN_ID = bytes.fromhex("ca91975d6c6810eb4077546d4fbdb49fa231f351cddfc915862f7c0dad81a7aa")
GROSS_ROSE = 100 * 10**18
XROSE_AMOUNT = 50 * 10**18
GAS_PRICE = 10_000_000_000
EXPECTED_SWEEP_VALUE = GROSS_ROSE - (SWEEP_GAS_LIMIT_NATIVE * GAS_PRICE)
CUSTODY_BEFORE = 250 * 10**18
CUSTODY_AFTER = CUSTODY_BEFORE + EXPECTED_SWEEP_VALUE
RESERVED_NONCE = 42


class _AwaitableValue:
    """Re-awaitable mock for async web3 properties."""

    def __init__(self, value):
        self._value = value

    def __await__(self):
        if False:
            yield
        return self._value


@pytest.fixture
def mock_verifier():
    return AsyncMock()


@pytest.fixture
def mock_sweep_engine():
    engine = AsyncMock()
    engine.gas_funding_tx_hashes = set()
    engine.get_record_by_deposit_id = MagicMock(return_value=None)
    engine.cleanup_record = MagicMock()
    engine.persist_error = MagicMock()
    engine.sweep_native_rose_bridge = AsyncMock()
    return engine


@pytest.fixture
def mock_accounting():
    svc = AsyncMock()
    svc.get_deposit_address = AsyncMock(return_value=DEPOSIT_ADDR)
    svc.get_rose_token_id = AsyncMock(return_value=ROSE_TOKEN_ID)
    svc.get_token_id = AsyncMock(return_value=b"\x11" * 32)
    svc.is_deposit_processed = AsyncMock(return_value=False)
    svc.is_token_registered = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def processor(mock_verifier, mock_sweep_engine, mock_accounting):
    return DepositProcessor(
        verifier=mock_verifier,
        sweep_engine=mock_sweep_engine,
        accounting_service=mock_accounting,
    )


@pytest.fixture
def sweep_accounting():
    svc = AsyncMock()
    svc.generate_sweep_native = AsyncMock(return_value=b"\x01\x02\x03")
    svc.generate_gas_funding_tx = AsyncMock()
    svc.credit_deposit = AsyncMock()
    svc.get_custody_address = AsyncMock(return_value=CUSTODY_ADDR)
    svc.get_rose_token_id = AsyncMock(return_value=ROSE_TOKEN_ID)
    svc.is_deposit_processed = AsyncMock(return_value=False)
    return svc


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path)


@pytest.fixture
def sweep_engine(state_dir, sweep_accounting):
    return SweepEngine(
        accounting_service=sweep_accounting,
        chain_rpc_urls={SAPPHIRE_CHAIN_ID: "https://fake-sapphire.example.invalid"},
        state_dir=state_dir,
    )


def _sapphire_verified_deposit(amount: int = GROSS_ROSE) -> VerifiedDeposit:
    return VerifiedDeposit(
        chain_id=SAPPHIRE_CHAIN_ID,
        tx_hash=TX_HASH,
        amount=amount,
        is_native=True,
        token_address=None,
        deposit_index=0,
        block_number=100,
    )


def _mock_sapphire_sweep_web3(
    *,
    gross_balance: int = GROSS_ROSE,
    custody_before: int = CUSTODY_BEFORE,
    custody_after: int = CUSTODY_AFTER,
    gas_price: int = GAS_PRICE,
):
    custody_reads = [custody_before, custody_after]
    w3 = AsyncMock()

    async def get_balance(address, *_, **__):
        if address.lower() == DEPOSIT_ADDR.lower():
            return gross_balance
        if address.lower() == CUSTODY_ADDR.lower():
            return custody_reads.pop(0)
        raise AssertionError(f"unexpected balance read for {address}")

    w3.eth.get_balance = AsyncMock(side_effect=get_balance)
    w3.eth.get_transaction_count = AsyncMock(return_value=7)
    w3.eth.gas_price = _AwaitableValue(gas_price)
    w3.eth.get_block = AsyncMock(return_value={"baseFeePerGas": gas_price})
    w3.eth.send_raw_transaction = AsyncMock(return_value=b"\xdd" * 32)
    w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 1, "blockNumber": 111})
    return w3


@pytest.mark.asyncio
async def test_sapphire_native_routes_to_rose_token_id_and_bridge_flow(
    processor,
    mock_verifier,
    mock_sweep_engine,
    mock_accounting,
):
    mock_verifier.verify_deposit.return_value = _sapphire_verified_deposit()

    result = await processor.process_deposit(
        chain_type="evm",
        chain_id=SAPPHIRE_CHAIN_ID,
        tx_hash=TX_HASH,
        amount=GROSS_ROSE,
        log_index=0,
        version=0,
        auth=PrivateReadAuth(token=b"\x00" * 65, user_address=BENEFICIARY),
    )

    expected_deposit_id = compute_deposit_id(SAPPHIRE_CHAIN_ID, TX_HASH, ROSE_TOKEN_ID, 0)
    assert result["status"] == "pending"
    assert result["deposit_id"] == "0x" + expected_deposit_id.hex()

    mock_accounting.get_rose_token_id.assert_awaited_once()
    mock_accounting.get_token_id.assert_not_awaited()

    await asyncio.sleep(0)
    mock_sweep_engine.sweep_native_rose_bridge.assert_awaited_once()
    call_kwargs = mock_sweep_engine.sweep_native_rose_bridge.await_args.kwargs
    assert call_kwargs["token_id"] == ROSE_TOKEN_ID
    assert call_kwargs["deposit_id"] == expected_deposit_id
    mock_sweep_engine.sweep_native.assert_not_called()
    mock_sweep_engine.sweep_erc20.assert_not_called()


@pytest.mark.asyncio
async def test_sapphire_native_sweep_credits_net_custody_delta(
    sweep_engine,
    sweep_accounting,
):
    with (
        patch.object(sweep_engine, "_get_web3") as mock_get_w3,
        patch(
            "src.services.sweep_engine.estimate_l1_data_fee",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        mock_get_w3.return_value = _mock_sapphire_sweep_web3()

        await sweep_engine.sweep_native_rose_bridge(
            deposit_address=DEPOSIT_ADDR,
            beneficiary=BENEFICIARY,
            chain_type="evm",
            version=0,
            chain_id=SAPPHIRE_CHAIN_ID,
            token_id=ROSE_TOKEN_ID,
            amount=GROSS_ROSE,
            deposit_id=b"\x22" * 32,
        )

    sweep_accounting.generate_gas_funding_tx.assert_not_called()
    sweep_accounting.generate_sweep_native.assert_awaited_once()
    sweep_kwargs = sweep_accounting.generate_sweep_native.await_args.kwargs
    assert sweep_kwargs["amount"] == EXPECTED_SWEEP_VALUE

    sweep_accounting.credit_deposit.assert_awaited_once()
    credit_kwargs = sweep_accounting.credit_deposit.await_args.kwargs
    assert credit_kwargs["token_id"] == ROSE_TOKEN_ID
    assert credit_kwargs["amount"] == EXPECTED_SWEEP_VALUE

    assert sweep_engine.get_sweep_record(DEPOSIT_ADDR, SAPPHIRE_CHAIN_ID) is None


@pytest.mark.asyncio
async def test_sapphire_native_receipt_fixture_parses_and_credits_net_amount(
    sweep_engine,
    sweep_accounting,
):
    receipt_fixture = {
        "status": 1,
        "blockNumber": 100,
        "transactionHash": bytes.fromhex("cc" * 32),
        "to": DEPOSIT_ADDR,
        "logs": [],
    }
    tx_fixture = {
        "to": DEPOSIT_ADDR,
        "value": GROSS_ROSE,
        "from": Web3.to_checksum_address("0x" + "dd" * 20),
    }
    verifier = DepositVerifier({SAPPHIRE_CHAIN_ID: "https://fake-sapphire.example.invalid"})

    with patch.object(verifier, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        w3.eth.get_transaction_receipt = AsyncMock(return_value=receipt_fixture)
        w3.eth.get_transaction = AsyncMock(return_value=tx_fixture)
        w3.eth.get_block = AsyncMock(return_value={"number": 101})
        mock_get_w3.return_value = w3

        verified = await verifier.verify_deposit(
            chain_id=SAPPHIRE_CHAIN_ID,
            tx_hash=TX_HASH,
            deposit_address=DEPOSIT_ADDR,
            expected_amount=GROSS_ROSE,
            log_index=0,
        )

    assert verified.is_native is True
    assert verified.token_address is None
    assert verified.amount == GROSS_ROSE
    assert verified.deposit_index == 0

    with (
        patch.object(sweep_engine, "_get_web3") as mock_get_w3,
        patch(
            "src.services.sweep_engine.estimate_l1_data_fee",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        mock_get_w3.return_value = _mock_sapphire_sweep_web3()

        await sweep_engine.sweep_native_rose_bridge(
            deposit_address=DEPOSIT_ADDR,
            beneficiary=BENEFICIARY,
            chain_type="evm",
            version=0,
            chain_id=SAPPHIRE_CHAIN_ID,
            token_id=ROSE_TOKEN_ID,
            amount=verified.amount,
            deposit_id=b"\x33" * 32,
        )

    credit_kwargs = sweep_accounting.credit_deposit.await_args.kwargs
    assert credit_kwargs["amount"] == EXPECTED_SWEEP_VALUE
    assert credit_kwargs["token_id"] == ROSE_TOKEN_ID


@pytest.mark.asyncio
async def test_sapphire_native_credit_is_tx_value_not_shared_custody_delta(
    sweep_engine,
    sweep_accounting,
):
    """Regression: concurrent bridge-ins must not double-credit.

    The custody EOA is shared across deposit addresses while the lock is
    per-deposit-address, so a parallel bridge-in can inflate the custody
    balance inside this sweep's read window. Crediting the value this tx sent
    (sweep_amount = balance − gas) is race-free; crediting a custody delta is
    not. Here any read of the custody address raises — the credit must still
    be exactly sweep_amount, proving it never depends on custody state.
    """
    w3 = AsyncMock()

    async def get_balance(address, *_, **__):
        if address.lower() == DEPOSIT_ADDR.lower():
            return GROSS_ROSE
        # Simulate a concurrent bridge-in: the shared custody balance jumps by
        # an unrelated amount mid-sweep. The old delta-based credit would have
        # over-credited; the fixed code must never read this at all.
        raise AssertionError(
            "happy path read the shared custody balance — credit must use tx.value, not a delta"
        )

    w3.eth.get_balance = AsyncMock(side_effect=get_balance)
    w3.eth.get_transaction_count = AsyncMock(return_value=7)
    w3.eth.gas_price = _AwaitableValue(GAS_PRICE)
    w3.eth.get_block = AsyncMock(return_value={"baseFeePerGas": GAS_PRICE})
    w3.eth.send_raw_transaction = AsyncMock(return_value=b"\xdd" * 32)
    w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 1, "blockNumber": 111})

    with (
        patch.object(sweep_engine, "_get_web3") as mock_get_w3,
        patch(
            "src.services.sweep_engine.estimate_l1_data_fee",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        mock_get_w3.return_value = w3

        await sweep_engine.sweep_native_rose_bridge(
            deposit_address=DEPOSIT_ADDR,
            beneficiary=BENEFICIARY,
            chain_type="evm",
            version=0,
            chain_id=SAPPHIRE_CHAIN_ID,
            token_id=ROSE_TOKEN_ID,
            amount=GROSS_ROSE,
            deposit_id=b"\x44" * 32,
        )

    sweep_accounting.get_custody_address.assert_not_awaited()
    sweep_accounting.credit_deposit.assert_awaited_once()
    credit_kwargs = sweep_accounting.credit_deposit.await_args.kwargs
    assert credit_kwargs["amount"] == EXPECTED_SWEEP_VALUE
    assert credit_kwargs["token_id"] == ROSE_TOKEN_ID


def test_sapphire_native_deposit_id_uses_token_id_not_token_type():
    deposit_index = 0
    expected = Web3.solidity_keccak(
        ["uint256", "bytes32", "bytes32", "uint256"],
        [
            SAPPHIRE_CHAIN_ID,
            bytes.fromhex(TX_HASH.removeprefix("0x")),
            ROSE_TOKEN_ID,
            deposit_index,
        ],
    )

    assert compute_deposit_id(SAPPHIRE_CHAIN_ID, TX_HASH, ROSE_TOKEN_ID, deposit_index) == expected
    assert compute_deposit_id(SAPPHIRE_CHAIN_ID, TX_HASH, ROSE_TOKEN_ID, deposit_index) != (
        Web3.solidity_keccak(
            ["uint256", "bytes32", "uint256", "uint256"],
            [SAPPHIRE_CHAIN_ID, bytes.fromhex(TX_HASH.removeprefix("0x")), 0, deposit_index],
        )
    )


# ──────────────────────────────────────────────────────────────────────────
# xROSE Base bridge-in flow
# ──────────────────────────────────────────────────────────────────────────


def _xrose_settings() -> Settings:
    return Settings(
        rofl_bridge_address=ROFL_BRIDGE_ADDR,
        xrose_address=XROSE_ADDR,
    )


def _xrose_verified_deposit(amount: int = XROSE_AMOUNT) -> VerifiedDeposit:
    return VerifiedDeposit(
        chain_id=BASE_CHAIN_ID,
        tx_hash=XROSE_TX_HASH,
        amount=amount,
        is_native=False,
        token_address=XROSE_ADDR,
        deposit_index=0,
        block_number=100,
    )


@pytest.fixture
def xrose_processor(mock_verifier, mock_sweep_engine, mock_accounting):
    return DepositProcessor(
        verifier=mock_verifier,
        sweep_engine=mock_sweep_engine,
        accounting_service=mock_accounting,
        settings=_xrose_settings(),
    )


@pytest.fixture
def mock_executor():
    executor = MagicMock()
    enqueued: list = []

    async def _enqueue(request):
        enqueued.append(request)
        return f"{BASE_CHAIN_ID}_{RESERVED_NONCE}"

    executor.enqueue = AsyncMock(side_effect=_enqueue)

    async def _wait_for_resolution(_key, timeout=None):
        rec = MagicMock()
        rec.status = CustodyTxStatus.SUCCESS
        rec.tx_hash = "0x" + "ab" * 32
        rec.kind = CustodyTxKind.XROSE_BURN
        rec.id = enqueued[-1].id if enqueued else "0x" + "00" * 32
        return rec

    executor.wait_for_resolution = AsyncMock(side_effect=_wait_for_resolution)
    executor.get_record = MagicMock(return_value=None)
    return executor


@pytest.fixture
def xrose_sweep_accounting():
    svc = AsyncMock()
    svc.generate_gas_funding_tx = AsyncMock(return_value=b"\xaa" * 70)
    svc.generate_sweep_erc20 = AsyncMock(return_value=b"\xbb" * 70)
    svc.generate_sweep_erc20_to_bridge = AsyncMock(return_value=b"\xcc" * 70)
    svc.reserve_bridge_burn = AsyncMock(return_value=None)
    svc.get_bridge_burn_nonce = AsyncMock(return_value=RESERVED_NONCE)
    svc.generate_bridge_burn_transfer = AsyncMock(return_value=b"\xdd" * 90)
    svc.credit_deposit = AsyncMock()
    svc.get_custody_address = AsyncMock(return_value=CUSTODY_ADDR)
    svc.get_gas_tank_address = AsyncMock(return_value=GAS_TANK_ADDR)
    svc.get_rose_token_id = AsyncMock(return_value=ROSE_TOKEN_ID)
    svc.is_deposit_processed = AsyncMock(return_value=False)
    return svc


@pytest.fixture
def xrose_sweep_engine(state_dir, xrose_sweep_accounting, mock_executor):
    return SweepEngine(
        accounting_service=xrose_sweep_accounting,
        chain_rpc_urls={BASE_CHAIN_ID: "https://fake-base.example.invalid"},
        state_dir=state_dir,
        executor=mock_executor,
    )


def _mock_base_xrose_web3(
    *,
    erc20_balance: int = XROSE_AMOUNT,
    gas_price: int = GAS_PRICE,
    sweep_reverts: bool = False,
):
    """Build an AsyncMock web3 for a Base-Sepolia xROSE sweep.

    Covers: ERC20 balanceOf call (via contract.functions.balanceOf), gas tank
    nonce read, gas funding broadcast + receipt, sweep nonce read, sweep
    broadcast + receipt.
    """
    w3 = AsyncMock()

    async def get_balance(_address, *_, **__):
        return 10**18  # ample ETH; not used for xROSE flow

    w3.eth.get_balance = AsyncMock(side_effect=get_balance)

    nonce_reads = {GAS_TANK_ADDR.lower(): 7, DEPOSIT_ADDR.lower(): 3}

    async def get_transaction_count(address, *_, **__):
        return nonce_reads.get(address.lower(), 0)

    w3.eth.get_transaction_count = AsyncMock(side_effect=get_transaction_count)
    w3.eth.gas_price = _AwaitableValue(gas_price)
    w3.eth.get_block = AsyncMock(return_value={"baseFeePerGas": gas_price})

    sweep_receipt = {"status": 0 if sweep_reverts else 1, "blockNumber": 200}
    w3.eth.send_raw_transaction = AsyncMock(return_value=b"\x33" * 32)
    # First call: gas funding receipt (success); subsequent: sweep receipt.
    w3.eth.get_transaction_receipt = AsyncMock(
        side_effect=[{"status": 1, "blockNumber": 199}, sweep_receipt, sweep_receipt]
    )

    def contract(address, abi):  # noqa: ANN001 — minimal duck-typed shim
        c = MagicMock()

        async def balance_of_call(*_args, **_kwargs):
            return erc20_balance

        balance_of = MagicMock()
        balance_of.call = AsyncMock(side_effect=balance_of_call)
        c.functions.balanceOf = MagicMock(return_value=balance_of)
        # burn(...) preflight is exercised in its own test against a separate
        # contract mock — irrelevant here.
        return c

    w3.eth.contract = contract
    w3.to_checksum_address = Web3.to_checksum_address
    return w3


@pytest.mark.asyncio
async def test_base_xrose_routes_to_rose_token_id_and_bridge_flow(
    xrose_processor,
    mock_verifier,
    mock_sweep_engine,
    mock_accounting,
):
    mock_verifier.verify_deposit.return_value = _xrose_verified_deposit()
    mock_sweep_engine.sweep_xrose_bridge = AsyncMock()

    result = await xrose_processor.process_deposit(
        chain_type="evm",
        chain_id=BASE_CHAIN_ID,
        tx_hash=XROSE_TX_HASH,
        amount=XROSE_AMOUNT,
        log_index=0,
        version=0,
        auth=PrivateReadAuth(token=b"\x00" * 65, user_address=BENEFICIARY),
    )

    expected_deposit_id = compute_deposit_id(BASE_CHAIN_ID, XROSE_TX_HASH, ROSE_TOKEN_ID, 0)
    assert result["status"] == "pending"
    assert result["deposit_id"] == "0x" + expected_deposit_id.hex()

    mock_accounting.get_rose_token_id.assert_awaited_once()
    mock_accounting.get_token_id.assert_not_awaited()

    await asyncio.sleep(0)
    mock_sweep_engine.sweep_xrose_bridge.assert_awaited_once()
    call_kwargs = mock_sweep_engine.sweep_xrose_bridge.await_args.kwargs
    assert call_kwargs["token_id"] == ROSE_TOKEN_ID
    assert call_kwargs["deposit_id"] == expected_deposit_id
    assert call_kwargs["token_address"] == XROSE_ADDR
    assert call_kwargs["bridge_address"] == ROFL_BRIDGE_ADDR


@pytest.mark.asyncio
async def test_xrose_sweep_destination_is_rofl_bridge_not_custody(
    xrose_sweep_engine, xrose_sweep_accounting
):
    deposit_id = b"\x22" * 32
    with (
        patch.object(xrose_sweep_engine, "_get_web3") as mock_get_w3,
        patch(
            "src.services.sweep_engine.estimate_l1_data_fee",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        mock_get_w3.return_value = _mock_base_xrose_web3()
        await xrose_sweep_engine.sweep_xrose_bridge(
            deposit_address=DEPOSIT_ADDR,
            beneficiary=BENEFICIARY,
            chain_type="evm",
            version=0,
            chain_id=BASE_CHAIN_ID,
            token_id=ROSE_TOKEN_ID,
            token_address=XROSE_ADDR,
            bridge_address=ROFL_BRIDGE_ADDR,
            amount=XROSE_AMOUNT,
            deposit_id=deposit_id,
        )

    xrose_sweep_accounting.generate_sweep_erc20_to_bridge.assert_awaited_once()
    xrose_sweep_accounting.generate_sweep_erc20.assert_not_called()


@pytest.mark.asyncio
async def test_xrose_burn_signed_via_generate_bridge_burn_transfer_only(
    xrose_sweep_engine, xrose_sweep_accounting
):
    deposit_id = b"\x33" * 32
    with (
        patch.object(xrose_sweep_engine, "_get_web3") as mock_get_w3,
        patch(
            "src.services.sweep_engine.estimate_l1_data_fee",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        mock_get_w3.return_value = _mock_base_xrose_web3()
        await xrose_sweep_engine.sweep_xrose_bridge(
            deposit_address=DEPOSIT_ADDR,
            beneficiary=BENEFICIARY,
            chain_type="evm",
            version=0,
            chain_id=BASE_CHAIN_ID,
            token_id=ROSE_TOKEN_ID,
            token_address=XROSE_ADDR,
            bridge_address=ROFL_BRIDGE_ADDR,
            amount=XROSE_AMOUNT,
            deposit_id=deposit_id,
        )

    xrose_sweep_accounting.generate_bridge_burn_transfer.assert_awaited_once_with(deposit_id)


@pytest.mark.asyncio
async def test_xrose_reserve_burn_uses_settings_and_chain_id(
    xrose_sweep_engine, xrose_sweep_accounting
):
    deposit_id = b"\x44" * 32
    with (
        patch.object(xrose_sweep_engine, "_get_web3") as mock_get_w3,
        patch(
            "src.services.sweep_engine.estimate_l1_data_fee",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        mock_get_w3.return_value = _mock_base_xrose_web3()
        await xrose_sweep_engine.sweep_xrose_bridge(
            deposit_address=DEPOSIT_ADDR,
            beneficiary=BENEFICIARY,
            chain_type="evm",
            version=0,
            chain_id=BASE_CHAIN_ID,
            token_id=ROSE_TOKEN_ID,
            token_address=XROSE_ADDR,
            bridge_address=ROFL_BRIDGE_ADDR,
            amount=XROSE_AMOUNT,
            deposit_id=deposit_id,
        )

    reserve_kwargs = xrose_sweep_accounting.reserve_bridge_burn.await_args.kwargs
    assert reserve_kwargs["chain_id"] == BASE_CHAIN_ID
    assert reserve_kwargs["bridge"] == ROFL_BRIDGE_ADDR
    assert reserve_kwargs["amount"] == XROSE_AMOUNT
    assert reserve_kwargs["deposit_id"] == deposit_id


@pytest.mark.asyncio
async def test_xrose_burn_enqueued_with_xrose_burn_kind_and_reserved_nonce(
    xrose_sweep_engine, xrose_sweep_accounting, mock_executor
):
    from src.services.custody_tx_executor import CustodyTxKind

    deposit_id = b"\x55" * 32
    with (
        patch.object(xrose_sweep_engine, "_get_web3") as mock_get_w3,
        patch(
            "src.services.sweep_engine.estimate_l1_data_fee",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        mock_get_w3.return_value = _mock_base_xrose_web3()
        await xrose_sweep_engine.sweep_xrose_bridge(
            deposit_address=DEPOSIT_ADDR,
            beneficiary=BENEFICIARY,
            chain_type="evm",
            version=0,
            chain_id=BASE_CHAIN_ID,
            token_id=ROSE_TOKEN_ID,
            token_address=XROSE_ADDR,
            bridge_address=ROFL_BRIDGE_ADDR,
            amount=XROSE_AMOUNT,
            deposit_id=deposit_id,
        )

    mock_executor.enqueue.assert_awaited_once()
    request = mock_executor.enqueue.await_args.args[0]
    assert request.chain_id == BASE_CHAIN_ID
    assert request.evm_nonce == RESERVED_NONCE
    assert request.kind == CustodyTxKind.XROSE_BURN
    assert request.id == "0x" + deposit_id.hex()


@pytest.mark.asyncio
async def test_xrose_credit_gated_on_burn_success(xrose_sweep_engine, xrose_sweep_accounting):
    deposit_id = b"\x66" * 32
    with (
        patch.object(xrose_sweep_engine, "_get_web3") as mock_get_w3,
        patch(
            "src.services.sweep_engine.estimate_l1_data_fee",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        mock_get_w3.return_value = _mock_base_xrose_web3()
        await xrose_sweep_engine.sweep_xrose_bridge(
            deposit_address=DEPOSIT_ADDR,
            beneficiary=BENEFICIARY,
            chain_type="evm",
            version=0,
            chain_id=BASE_CHAIN_ID,
            token_id=ROSE_TOKEN_ID,
            token_address=XROSE_ADDR,
            bridge_address=ROFL_BRIDGE_ADDR,
            amount=XROSE_AMOUNT,
            deposit_id=deposit_id,
        )

    xrose_sweep_accounting.credit_deposit.assert_awaited_once()
    credit_kwargs = xrose_sweep_accounting.credit_deposit.await_args.kwargs
    assert credit_kwargs["token_id"] == ROSE_TOKEN_ID
    assert credit_kwargs["amount"] == XROSE_AMOUNT
    assert credit_kwargs["deposit_id"] == deposit_id
    assert xrose_sweep_engine.get_sweep_record(DEPOSIT_ADDR, BASE_CHAIN_ID) is None


@pytest.mark.asyncio
async def test_xrose_credit_blocked_when_burn_in_awaiting_clear(
    xrose_sweep_engine, xrose_sweep_accounting, mock_executor
):
    review_record = MagicMock()
    review_record.status = CustodyTxStatus.AWAITING_CLEAR
    review_record.tx_hash = "0x" + "ab" * 32
    mock_executor.wait_for_resolution = AsyncMock(return_value=review_record)

    deposit_id = b"\x77" * 32
    with (
        patch.object(xrose_sweep_engine, "_get_web3") as mock_get_w3,
        patch(
            "src.services.sweep_engine.estimate_l1_data_fee",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        mock_get_w3.return_value = _mock_base_xrose_web3()
        with pytest.raises(SweepCreditPendingError):
            await xrose_sweep_engine.sweep_xrose_bridge(
                deposit_address=DEPOSIT_ADDR,
                beneficiary=BENEFICIARY,
                chain_type="evm",
                version=0,
                chain_id=BASE_CHAIN_ID,
                token_id=ROSE_TOKEN_ID,
                token_address=XROSE_ADDR,
                bridge_address=ROFL_BRIDGE_ADDR,
                amount=XROSE_AMOUNT,
                deposit_id=deposit_id,
            )

    xrose_sweep_accounting.credit_deposit.assert_not_called()
    record = xrose_sweep_engine.get_sweep_record(DEPOSIT_ADDR, BASE_CHAIN_ID)
    assert record is not None
    assert record.state == SweepState.BURN_PENDING


@pytest.mark.asyncio
async def test_xrose_credit_blocked_during_burn_pending(
    xrose_sweep_engine, xrose_sweep_accounting, mock_executor
):
    mock_executor.wait_for_resolution = AsyncMock(side_effect=asyncio.TimeoutError())

    deposit_id = b"\x88" * 32
    with (
        patch.object(xrose_sweep_engine, "_get_web3") as mock_get_w3,
        patch(
            "src.services.sweep_engine.estimate_l1_data_fee",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        mock_get_w3.return_value = _mock_base_xrose_web3()
        with pytest.raises(SweepCreditPendingError):
            await xrose_sweep_engine.sweep_xrose_bridge(
                deposit_address=DEPOSIT_ADDR,
                beneficiary=BENEFICIARY,
                chain_type="evm",
                version=0,
                chain_id=BASE_CHAIN_ID,
                token_id=ROSE_TOKEN_ID,
                token_address=XROSE_ADDR,
                bridge_address=ROFL_BRIDGE_ADDR,
                amount=XROSE_AMOUNT,
                deposit_id=deposit_id,
            )

    xrose_sweep_accounting.credit_deposit.assert_not_called()
    record = xrose_sweep_engine.get_sweep_record(DEPOSIT_ADDR, BASE_CHAIN_ID)
    assert record is not None
    assert record.state == SweepState.BURN_PENDING


@pytest.mark.asyncio
async def test_xrose_sweep_revert_blocks_burn_and_credit(
    xrose_sweep_engine, xrose_sweep_accounting, mock_executor
):
    deposit_id = b"\xa1" * 32
    with (
        patch.object(xrose_sweep_engine, "_get_web3") as mock_get_w3,
        patch(
            "src.services.sweep_engine.estimate_l1_data_fee",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        mock_get_w3.return_value = _mock_base_xrose_web3(sweep_reverts=True)
        with pytest.raises(ValueError, match="sweep tx reverted"):
            await xrose_sweep_engine.sweep_xrose_bridge(
                deposit_address=DEPOSIT_ADDR,
                beneficiary=BENEFICIARY,
                chain_type="evm",
                version=0,
                chain_id=BASE_CHAIN_ID,
                token_id=ROSE_TOKEN_ID,
                token_address=XROSE_ADDR,
                bridge_address=ROFL_BRIDGE_ADDR,
                amount=XROSE_AMOUNT,
                deposit_id=deposit_id,
            )

    xrose_sweep_accounting.reserve_bridge_burn.assert_not_called()
    xrose_sweep_accounting.generate_bridge_burn_transfer.assert_not_called()
    mock_executor.enqueue.assert_not_called()
    xrose_sweep_accounting.credit_deposit.assert_not_called()
    record = xrose_sweep_engine.get_sweep_record(DEPOSIT_ADDR, BASE_CHAIN_ID)
    assert record is not None
    assert record.state != SweepState.SWEPT
    assert record.sweep_tx_hash is not None


@pytest.mark.asyncio
async def test_xrose_sweep_wrapper_targets_xrose_token_on_base(
    xrose_sweep_engine, xrose_sweep_accounting
):
    deposit_id = b"\x99" * 32
    with (
        patch.object(xrose_sweep_engine, "_get_web3") as mock_get_w3,
        patch(
            "src.services.sweep_engine.estimate_l1_data_fee",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        mock_get_w3.return_value = _mock_base_xrose_web3()
        await xrose_sweep_engine.sweep_xrose_bridge(
            deposit_address=DEPOSIT_ADDR,
            beneficiary=BENEFICIARY,
            chain_type="evm",
            version=0,
            chain_id=BASE_CHAIN_ID,
            token_id=ROSE_TOKEN_ID,
            token_address=XROSE_ADDR,
            bridge_address=ROFL_BRIDGE_ADDR,
            amount=XROSE_AMOUNT,
            deposit_id=deposit_id,
        )

    sweep_kwargs = xrose_sweep_accounting.generate_sweep_erc20_to_bridge.await_args.kwargs
    assert sweep_kwargs["chain_id"] == BASE_CHAIN_ID
    assert sweep_kwargs["token_address"] == XROSE_ADDR
    assert sweep_kwargs["amount"] == XROSE_AMOUNT


def test_deposit_id_cross_chain_collision_xrose_vs_sapphire_native():
    sapphire_id = compute_deposit_id(SAPPHIRE_CHAIN_ID, XROSE_TX_HASH, ROSE_TOKEN_ID, 0)
    base_id = compute_deposit_id(BASE_CHAIN_ID, XROSE_TX_HASH, ROSE_TOKEN_ID, 0)
    assert sapphire_id != base_id, (
        "depositId derivation must distinguish source chains so a Sapphire "
        "native deposit and a Base xROSE deposit with the same tx_hash cannot collide"
    )


@pytest.mark.asyncio
async def test_xrose_resume_burn_pending_credits_on_executor_success(
    xrose_sweep_engine, xrose_sweep_accounting, mock_executor
):
    deposit_id = b"\xa1" * 32
    record = xrose_sweep_engine._build_record_for_test = None  # noqa: SLF001
    from src.services.sweep_engine import SweepRecord

    record = SweepRecord(
        deposit_address=DEPOSIT_ADDR,
        chain_id=BASE_CHAIN_ID,
        state=SweepState.BURN_PENDING,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=XROSE_AMOUNT,
        token_id_hex="0x" + ROSE_TOKEN_ID.hex(),
        token_address=XROSE_ADDR,
        deposit_id_hex="0x" + deposit_id.hex(),
        flow_type=FLOW_XROSE_BRIDGE_IN,
        destination=ROFL_BRIDGE_ADDR,
        bridge_address=ROFL_BRIDGE_ADDR,
        burn_reserved=True,
    )
    xrose_sweep_engine._save_record(record)  # noqa: SLF001

    success_record = MagicMock()
    success_record.status = CustodyTxStatus.SUCCESS
    success_record.tx_hash = "0x" + "fe" * 32
    success_record.kind = CustodyTxKind.XROSE_BURN
    success_record.id = "0x" + deposit_id.hex()
    mock_executor.get_record = MagicMock(return_value=success_record)

    await xrose_sweep_engine._resume_xrose_bridge_record(record)  # noqa: SLF001

    xrose_sweep_accounting.credit_deposit.assert_awaited_once()
    credit_kwargs = xrose_sweep_accounting.credit_deposit.await_args.kwargs
    assert credit_kwargs["amount"] == XROSE_AMOUNT
    assert credit_kwargs["deposit_id"] == deposit_id
    assert xrose_sweep_engine.get_sweep_record(DEPOSIT_ADDR, BASE_CHAIN_ID) is None


@pytest.mark.asyncio
async def test_reconcile_sweep_tx_leaves_burn_pending_alone(
    xrose_sweep_engine,
):
    """A BURN_PENDING record must NOT be demoted to SWEPT by reconcile even
    though its sweep_tx_hash is set and mined — demotion would lock the bridge
    flow out of its BURN_PENDING → executor SUCCESS → credit branch."""
    from src.services.sweep_engine import SweepRecord

    deposit_id = b"\xa5" * 32
    record = SweepRecord(
        deposit_address=DEPOSIT_ADDR,
        chain_id=BASE_CHAIN_ID,
        state=SweepState.BURN_PENDING,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=XROSE_AMOUNT,
        token_id_hex="0x" + ROSE_TOKEN_ID.hex(),
        token_address=XROSE_ADDR,
        deposit_id_hex="0x" + deposit_id.hex(),
        flow_type=FLOW_XROSE_BRIDGE_IN,
        destination=ROFL_BRIDGE_ADDR,
        bridge_address=ROFL_BRIDGE_ADDR,
        burn_reserved=True,
        sweep_tx_hash="0x" + "ee" * 32,
    )
    xrose_sweep_engine._save_record(record)  # noqa: SLF001

    w3 = AsyncMock()
    w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 1, "blockNumber": 99})

    with patch.object(xrose_sweep_engine, "_get_web3", return_value=w3):
        await xrose_sweep_engine._reconcile_sweep_tx(record)  # noqa: SLF001

    # In-memory and on-disk state must stay BURN_PENDING.
    assert record.state == SweepState.BURN_PENDING
    persisted = xrose_sweep_engine.get_sweep_record(DEPOSIT_ADDR, BASE_CHAIN_ID)
    assert persisted is not None
    assert persisted.state == SweepState.BURN_PENDING


@pytest.mark.asyncio
async def test_xrose_forward_refuses_credit_on_executor_identity_mismatch(
    xrose_sweep_engine, xrose_sweep_accounting, mock_executor
):
    """Forward path: if the executor record at the reserved nonce is SUCCESS
    but its kind/id belongs to a different caller, refuse to credit."""
    impostor = MagicMock()
    impostor.status = CustodyTxStatus.SUCCESS
    impostor.tx_hash = "0x" + "ab" * 32
    impostor.kind = CustodyTxKind.NORMAL_WITHDRAWAL
    impostor.id = "0x" + "ff" * 32  # someone else's id
    mock_executor.wait_for_resolution = AsyncMock(return_value=impostor)

    deposit_id = b"\xa8" * 32
    with (
        patch.object(xrose_sweep_engine, "_get_web3") as mock_get_w3,
        patch(
            "src.services.sweep_engine.estimate_l1_data_fee",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        mock_get_w3.return_value = _mock_base_xrose_web3()
        with pytest.raises(SweepCreditPendingError):
            await xrose_sweep_engine.sweep_xrose_bridge(
                deposit_address=DEPOSIT_ADDR,
                beneficiary=BENEFICIARY,
                chain_type="evm",
                version=0,
                chain_id=BASE_CHAIN_ID,
                token_id=ROSE_TOKEN_ID,
                token_address=XROSE_ADDR,
                bridge_address=ROFL_BRIDGE_ADDR,
                amount=XROSE_AMOUNT,
                deposit_id=deposit_id,
            )

    xrose_sweep_accounting.credit_deposit.assert_not_called()


@pytest.mark.asyncio
async def test_xrose_recovery_refuses_credit_on_executor_identity_mismatch(
    xrose_sweep_engine, xrose_sweep_accounting, mock_executor
):
    """Recovery path: same identity guard as the forward path."""
    from src.services.sweep_engine import SweepRecord

    deposit_id = b"\xa9" * 32
    record = SweepRecord(
        deposit_address=DEPOSIT_ADDR,
        chain_id=BASE_CHAIN_ID,
        state=SweepState.BURN_PENDING,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=XROSE_AMOUNT,
        token_id_hex="0x" + ROSE_TOKEN_ID.hex(),
        token_address=XROSE_ADDR,
        deposit_id_hex="0x" + deposit_id.hex(),
        flow_type=FLOW_XROSE_BRIDGE_IN,
        destination=ROFL_BRIDGE_ADDR,
        bridge_address=ROFL_BRIDGE_ADDR,
        burn_reserved=True,
        sweep_tx_hash="0x" + "ee" * 32,
    )
    xrose_sweep_engine._save_record(record)  # noqa: SLF001

    impostor = MagicMock()
    impostor.status = CustodyTxStatus.SUCCESS
    impostor.tx_hash = "0x" + "ab" * 32
    impostor.kind = CustodyTxKind.NORMAL_WITHDRAWAL
    impostor.id = "0x" + "ff" * 32
    mock_executor.get_record = MagicMock(return_value=impostor)

    await xrose_sweep_engine._resume_xrose_bridge_record(record)  # noqa: SLF001

    xrose_sweep_accounting.credit_deposit.assert_not_called()
    # Promoted to MANUAL_REVIEW so the operator sees one persistent signal
    # instead of recurring critical-log spam every recovery tick.
    persisted = xrose_sweep_engine.get_sweep_record(DEPOSIT_ADDR, BASE_CHAIN_ID)
    assert persisted is not None
    assert persisted.state == SweepState.MANUAL_REVIEW
    assert persisted.error is not None
    assert "normal_withdrawal" in persisted.error


@pytest.mark.asyncio
async def test_xrose_retry_preserves_in_flight_record(tmp_path):
    """A /deposits/check retry for a deposit_id whose SweepRecord is past
    sweep broadcast (sweep_tx_hash set) AND carries error must NOT delete the
    record — doing so would orphan the in-flight burn and lose funds when it
    confirms."""
    from src.services.sweep_engine import SweepRecord

    deposit_id = b"\xa7" * 32
    deposit_id_hex = "0x" + deposit_id.hex()

    sweep_accounting = AsyncMock()
    sweep_accounting.get_custody_address = AsyncMock(return_value=CUSTODY_ADDR)
    sweep = SweepEngine(
        accounting_service=sweep_accounting,
        chain_rpc_urls={BASE_CHAIN_ID: "https://fake-base.example.invalid"},
        state_dir=str(tmp_path),
        executor=MagicMock(),
    )

    record = SweepRecord(
        deposit_address=DEPOSIT_ADDR,
        chain_id=BASE_CHAIN_ID,
        state=SweepState.BURN_PENDING,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=XROSE_AMOUNT,
        token_id_hex="0x" + ROSE_TOKEN_ID.hex(),
        token_address=XROSE_ADDR,
        deposit_id_hex=deposit_id_hex,
        flow_type=FLOW_XROSE_BRIDGE_IN,
        destination=ROFL_BRIDGE_ADDR,
        bridge_address=ROFL_BRIDGE_ADDR,
        burn_reserved=True,
        sweep_tx_hash="0x" + "ee" * 32,
        error="transient RPC failure",
    )
    sweep._save_record(record)  # noqa: SLF001

    sweep.cleanup_record(deposit_id_hex)

    persisted = sweep.get_sweep_record(DEPOSIT_ADDR, BASE_CHAIN_ID)
    assert persisted is not None, (
        "cleanup_record must refuse to delete a record with a broadcast sweep_tx_hash"
    )
    assert persisted.state == SweepState.BURN_PENDING
    assert persisted.sweep_tx_hash == "0x" + "ee" * 32


@pytest.mark.asyncio
async def test_xrose_resume_burn_pending_refreshes_preflight_when_executor_queued(
    xrose_sweep_engine, xrose_sweep_accounting, mock_executor
):
    """A BURN_PENDING SweepRecord paired with a non-terminal executor record
    (or no executor record) must re-enqueue so the executor's chain loop sees
    a fresh preflight after restart — otherwise the burn stalls to
    AWAITING_CLEAR the moment the chain loop runs."""
    from src.services.sweep_engine import SweepRecord

    deposit_id = b"\xa6" * 32
    record = SweepRecord(
        deposit_address=DEPOSIT_ADDR,
        chain_id=BASE_CHAIN_ID,
        state=SweepState.BURN_PENDING,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=XROSE_AMOUNT,
        token_id_hex="0x" + ROSE_TOKEN_ID.hex(),
        token_address=XROSE_ADDR,
        deposit_id_hex="0x" + deposit_id.hex(),
        flow_type=FLOW_XROSE_BRIDGE_IN,
        destination=ROFL_BRIDGE_ADDR,
        bridge_address=ROFL_BRIDGE_ADDR,
        burn_reserved=True,
    )
    xrose_sweep_engine._save_record(record)  # noqa: SLF001

    # Executor has a non-terminal record at this nonce (e.g. catch-up created
    # a QUEUED record but no preflight is registered yet).
    queued_record = MagicMock()
    queued_record.status = CustodyTxStatus.QUEUED
    mock_executor.get_record = MagicMock(return_value=queued_record)

    await xrose_sweep_engine._resume_xrose_bridge_record(record)  # noqa: SLF001

    # Reserve must NOT run (already reserved); enqueue MUST run to refresh preflight.
    xrose_sweep_accounting.reserve_bridge_burn.assert_not_called()
    mock_executor.enqueue.assert_awaited()
    xrose_sweep_accounting.credit_deposit.assert_not_called()


@pytest.mark.asyncio
async def test_xrose_resume_pre_swept_does_not_credit_via_generic_sweep(
    xrose_sweep_engine, xrose_sweep_accounting, mock_executor
):
    """Pre-SWEPT xROSE recovery must re-enter the bridge flow, not the generic
    ERC20 sweep — the latter would target the custody EOA and credit without
    burning, breaking the burn-before-credit invariant."""
    from src.services.sweep_engine import SweepRecord

    deposit_id = b"\xa4" * 32
    record = SweepRecord(
        deposit_address=DEPOSIT_ADDR,
        chain_id=BASE_CHAIN_ID,
        state=SweepState.GAS_FUNDED,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=XROSE_AMOUNT,
        token_id_hex="0x" + ROSE_TOKEN_ID.hex(),
        token_address=XROSE_ADDR,
        deposit_id_hex="0x" + deposit_id.hex(),
        flow_type=FLOW_XROSE_BRIDGE_IN,
        destination=ROFL_BRIDGE_ADDR,
        bridge_address=ROFL_BRIDGE_ADDR,
        burn_reserved=False,
        sweep_tx_hash=None,
    )
    xrose_sweep_engine._save_record(record)  # noqa: SLF001

    with (
        patch.object(xrose_sweep_engine, "_get_web3") as mock_get_w3,
        patch(
            "src.services.sweep_engine.estimate_l1_data_fee",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        mock_get_w3.return_value = _mock_base_xrose_web3()
        await xrose_sweep_engine._resume_xrose_bridge_record(record)  # noqa: SLF001

    # The bridge-targeted sweep helper must be the one that ran; the generic
    # custody-EOA helper must never be invoked for an xROSE record.
    xrose_sweep_accounting.generate_sweep_erc20_to_bridge.assert_awaited()
    xrose_sweep_accounting.generate_sweep_erc20.assert_not_called()
    # And of course the executor must see the burn enqueued.
    mock_executor.enqueue.assert_awaited()


@pytest.mark.asyncio
async def test_xrose_resume_swept_without_reservation_retries_reserve_and_enqueue(
    xrose_sweep_engine, xrose_sweep_accounting, mock_executor
):
    from src.services.sweep_engine import SweepRecord

    deposit_id = b"\xa2" * 32
    record = SweepRecord(
        deposit_address=DEPOSIT_ADDR,
        chain_id=BASE_CHAIN_ID,
        state=SweepState.SWEPT,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=XROSE_AMOUNT,
        token_id_hex="0x" + ROSE_TOKEN_ID.hex(),
        token_address=XROSE_ADDR,
        deposit_id_hex="0x" + deposit_id.hex(),
        flow_type=FLOW_XROSE_BRIDGE_IN,
        destination=ROFL_BRIDGE_ADDR,
        bridge_address=ROFL_BRIDGE_ADDR,
        burn_reserved=False,
        sweep_tx_hash="0x" + "ee" * 32,
    )
    xrose_sweep_engine._save_record(record)  # noqa: SLF001

    await xrose_sweep_engine._resume_xrose_bridge_record(record)  # noqa: SLF001

    xrose_sweep_accounting.reserve_bridge_burn.assert_awaited_once()
    mock_executor.enqueue.assert_awaited_once()
    persisted = xrose_sweep_engine.get_sweep_record(DEPOSIT_ADDR, BASE_CHAIN_ID)
    assert persisted is not None
    assert persisted.state == SweepState.BURN_PENDING
    assert persisted.burn_reserved is True


@pytest.mark.asyncio
async def test_xrose_executor_nonce_gap_blocks_later_burn(tmp_path):
    """Real executor regression: when two xROSE bridge burns sit on disk with
    a nonce gap between them, the executor refuses to broadcast the later one
    until catch-up fills the gap. Pre-seeds nonce 4 as SUCCESS so the loop
    advances past it and the gap check between 4 and 6 fires.
    """
    from src.services.custody_tx_executor import (
        CustodyTxExecutor,
        CustodyTxKind,
        CustodyTxRecord,
        CustodyTxRequest,
        CustodyTxStatus,
    )

    accounting_stub = MagicMock()
    accounting_stub.get_custody_address = AsyncMock(return_value=CUSTODY_ADDR)
    accounting_stub.contract_address = "0x" + "01" * 20
    accounting_stub.settings = MagicMock(min_withdrawal_gas_balance=0)

    chain_w3 = MagicMock()
    chain_w3.eth.send_raw_transaction = AsyncMock(return_value=b"\xfe" * 32)
    chain_w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 1, "blockNumber": 1})
    chain_w3.eth.get_transaction_count = AsyncMock(return_value=0)
    accounting_stub._get_chain_web3 = AsyncMock(return_value=chain_w3)  # noqa: SLF001
    accounting_stub._get_reader_contract = MagicMock(side_effect=RuntimeError)  # noqa: SLF001

    executor = CustodyTxExecutor(
        accounting_stub,
        state_dir=str(tmp_path),
        chain_ids=(BASE_CHAIN_ID,),
    )

    # Pre-seed nonce 4 as SUCCESS so the loop iterates past it and the gap to 6
    # registers between two disk records (rather than treating 6 as the first
    # nonce on the chain).
    earlier = CustodyTxRecord(
        chain_id=BASE_CHAIN_ID,
        accounting_contract_address=accounting_stub.contract_address,
        evm_sender=CUSTODY_ADDR,
        evm_nonce=4,
        kind=CustodyTxKind.XROSE_BURN,
        id="0x" + "a1" * 32,
        signed_tx_hex="0x" + "ff" * 70,
        status=CustodyTxStatus.SUCCESS,
        tx_hash="0x" + "fd" * 32,
    )
    executor._save_record(earlier)  # noqa: SLF001

    await executor.enqueue(
        CustodyTxRequest(
            chain_id=BASE_CHAIN_ID,
            evm_nonce=6,  # gap: nonce 5 missing
            kind=CustodyTxKind.XROSE_BURN,
            id="0x" + "a2" * 32,
            signed_tx=b"\xff" * 70,
        )
    )

    await executor._process_next_for_chain(BASE_CHAIN_ID)  # noqa: SLF001

    chain_w3.eth.send_raw_transaction.assert_not_called()
    later = executor.get_record(BASE_CHAIN_ID, 6)
    assert later is not None and later.status.value == "queued"
