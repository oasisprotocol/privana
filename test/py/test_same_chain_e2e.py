"""Same-chain E2E: single chain acts as both accounting and deposit source chain.

Uses mocked node responding by hash, with injected ChainConfig overrides.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_abi import encode
from web3 import Web3
from web3.exceptions import TransactionNotFound

import src.services.deposit_processor as deposit_processor
import src.services.deposit_verifier as deposit_verifier
import src.services.rpc_identity as rpc_identity
import src.services.sweep_engine as sweep_engine
from src.config.chain_config import (
    DEFAULT_FINALITY_DEPTH,
    TRANSFER_EVENT_TOPIC,
    ChainConfig,
    L2Type,
)
from src.models.private_read import PrivateReadAuth
from src.services.deposit_processor import DepositProcessor
from src.services.deposit_verifier import DepositVerifier
from src.services.rpc_identity import initialize_verified_chain_rpc_urls
from src.services.sweep_engine import SweepEngine
from src.services.withdrawal_processor import WithdrawalProcessor

SAME_CHAIN_ID = 23293
SAME_CHAIN_RPC_URL = "https://sapphire-localnet.example.invalid"
# Unverified second chain used to test fail-closed behavior.
FOREIGN_CHAIN_ID = 23295
FOREIGN_RPC_URL = "https://testnet.sapphire.example.invalid"

DEPOSIT_ADDRESS = Web3.to_checksum_address("0x" + "a1" * 20)
BENEFICIARY = Web3.to_checksum_address("0x" + "b2" * 20)
TOKEN_ADDRESS = Web3.to_checksum_address("0x" + "c3" * 20)
GAS_TANK_ADDRESS = Web3.to_checksum_address("0x" + "d4" * 20)
WITHDRAWAL_TO_ADDRESS = Web3.to_checksum_address("0x" + "e5" * 20)
EVM_SIGNER_ADDRESS = Web3.to_checksum_address("0x" + "f6" * 20)

ERC20_DEPOSIT_TX = "0x" + "11" * 32
NATIVE_DEPOSIT_TX = "0x" + "22" * 32
GAS_FUNDING_TX_HASH = b"\xa0" * 32
SWEEP_TX_HASH = b"\xb0" * 32
WITHDRAWAL_SIGNED_TX = b"\xc0" * 64
WITHDRAWAL_TX_HASH = "0x" + "d0" * 32

DEPOSIT_BLOCK = 100
LATEST_BLOCK = 110
SAPPHIRE_GAS_PRICE = 100_000_000_000
# EVMSignerAndVerifier sweep gas limits — what gas funding is sized from.
NATIVE_SWEEP_GAS_LIMIT = 25_000
ERC20_SWEEP_GAS_LIMIT = 65_000

ONE_HONOR = 10**18
TWO_ROSE = 2 * 10**18

PRIVATE_READ_AUTH = PrivateReadAuth(token=b"\x00" * 65, user_address=BENEFICIARY)


class _AwaitableValue:
    def __init__(self, val):
        self._val = val

    def __await__(self):
        if False:
            yield  # makes this a generator
        return self._val


def _hash_key(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    return str(value).lower()


def _hash_lookup(known: dict) -> AsyncMock:
    """Async mock shaped like an EVM node: answers only for hashes it knows."""

    def _lookup(tx_hash, *args, **kwargs):
        key = _hash_key(tx_hash)
        if key not in known:
            raise TransactionNotFound(f"{tx_hash!r} not found")
        return known[key]

    return AsyncMock(side_effect=_lookup)


def _erc20_transfer_log(amount: int, log_index: int = 3) -> dict:
    return {
        "address": TOKEN_ADDRESS,
        "topics": [
            bytes.fromhex(TRANSFER_EVENT_TOPIC[2:]),
            bytes(12) + bytes.fromhex("de" * 20),
            bytes(12) + bytes.fromhex(DEPOSIT_ADDRESS[2:]),
        ],
        "data": amount.to_bytes(32, "big"),
        "logIndex": log_index,
    }


def _single_chain_node(
    *,
    receipts: dict,
    transactions: dict | None = None,
    native_balance: int = 0,
    broadcast_hashes: tuple[bytes, ...] = (),
) -> AsyncMock:
    """Mock node answering transaction lookups by hash rather than call order."""
    receipts = {_hash_key(k): v for k, v in receipts.items()}
    transactions = {_hash_key(k): v for k, v in (transactions or {}).items()}
    for offset, tx_hash in enumerate(broadcast_hashes, start=1):
        receipts.setdefault(_hash_key(tx_hash), {"status": 1, "blockNumber": LATEST_BLOCK + offset})

    node = AsyncMock()
    node.eth.get_block = AsyncMock(
        side_effect=lambda _block="latest": {
            "number": LATEST_BLOCK,
            "baseFeePerGas": SAPPHIRE_GAS_PRICE,
        }
    )
    node.eth.gas_price = _AwaitableValue(SAPPHIRE_GAS_PRICE)
    node.eth.get_transaction_receipt = _hash_lookup(receipts)
    node.eth.get_transaction = _hash_lookup(transactions)
    node.eth.get_balance = AsyncMock(return_value=native_balance)
    node.eth.get_transaction_count = AsyncMock(return_value=0)
    node.eth.send_raw_transaction = AsyncMock(side_effect=list(broadcast_hashes))
    return node


# ─── Injected configuration ─────────────────────────────────────────────


@pytest.fixture
def same_chain_config() -> ChainConfig:
    return ChainConfig(
        chain_id=SAME_CHAIN_ID,
        finality_depth=2,
        min_deposit_native_wei=10_000_000_000_000_000,
        min_deposit_erc20_wei=ONE_HONOR,
        gas_funding_amount_wei=6_500_000_000_000_000,
        l2_type=L2Type.NONE,
        discovery_scan_chunk_blocks=100,
        discovery_lookback_blocks=640,
        discovery_max_lookback_blocks=3_800,
    )


@pytest.fixture(autouse=True)
def injected_chain(monkeypatch, same_chain_config) -> ChainConfig:
    """Override module-level minimums and lookups with isolated test config."""
    cfg = same_chain_config
    monkeypatch.setattr(
        deposit_processor,
        "MIN_DEPOSIT_NATIVE_WEI",
        {cfg.chain_id: cfg.min_deposit_native_wei},
    )
    monkeypatch.setattr(
        deposit_processor,
        "MIN_DEPOSIT_ERC20_WEI",
        {cfg.chain_id: cfg.min_deposit_erc20_wei},
    )
    monkeypatch.setattr(
        sweep_engine,
        "GAS_FUNDING_AMOUNT_WEI",
        {cfg.chain_id: cfg.gas_funding_amount_wei},
    )
    monkeypatch.setattr(
        deposit_verifier,
        "get_finality_depth",
        lambda chain_id: cfg.finality_depth if chain_id == cfg.chain_id else DEFAULT_FINALITY_DEPTH,
    )

    async def _no_l1_data_fee(w3, chain_id, is_erc20):
        # Sapphire posts no calldata to L1; gas funding is purely L2 execution.
        assert chain_id == cfg.chain_id
        assert cfg.l2_type is L2Type.NONE
        return 0

    monkeypatch.setattr(sweep_engine, "estimate_l1_data_fee", _no_l1_data_fee)
    return cfg


@pytest.fixture
def token_registry() -> dict:
    """Map (chain_id, token_address) to keccak token ID; None key denotes native token."""
    return {
        (SAME_CHAIN_ID, None): Web3.keccak(
            encode(["uint256", "address"], [SAME_CHAIN_ID, "0x" + "00" * 20])
        ),
        (SAME_CHAIN_ID, TOKEN_ADDRESS.lower()): Web3.keccak(
            encode(["uint256", "address"], [SAME_CHAIN_ID, TOKEN_ADDRESS])
        ),
    }


@pytest.fixture
def mock_accounting(token_registry) -> AsyncMock:

    async def get_token_id(chain_id: int, token_address: str | None) -> bytes:
        key = (chain_id, token_address.lower() if token_address else None)
        if key not in token_registry:
            raise ValueError(f"token {token_address} not registered for chain {chain_id}")
        return token_registry[key]

    svc = AsyncMock()
    svc.get_deposit_address = AsyncMock(return_value=DEPOSIT_ADDRESS)
    svc.get_token_id = AsyncMock(side_effect=get_token_id)
    svc.is_token_registered = AsyncMock(
        side_effect=lambda token_id: token_id in set(token_registry.values())
    )
    svc.is_deposit_processed = AsyncMock(return_value=False)
    svc.get_gas_tank_address = AsyncMock(return_value=GAS_TANK_ADDRESS)
    svc.generate_gas_funding_tx = AsyncMock(return_value=b"\x01gas")
    svc.generate_sweep_native = AsyncMock(return_value=b"\x02native")
    svc.generate_sweep_erc20 = AsyncMock(return_value=b"\x03erc20")
    svc.get_native_sweep_gas_limit = AsyncMock(return_value=NATIVE_SWEEP_GAS_LIMIT)
    svc.get_erc20_sweep_gas_limit = AsyncMock(return_value=ERC20_SWEEP_GAS_LIMIT)
    svc.credit_deposit = AsyncMock()
    return svc


@pytest.fixture
def engine(tmp_path, mock_accounting) -> SweepEngine:
    return SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={SAME_CHAIN_ID: SAME_CHAIN_RPC_URL},
        state_dir=str(tmp_path),
    )


@pytest.fixture
def verifier() -> DepositVerifier:
    return DepositVerifier({SAME_CHAIN_ID: SAME_CHAIN_RPC_URL})


@pytest.fixture
def processor(verifier, engine, mock_accounting) -> DepositProcessor:
    return DepositProcessor(
        verifier=verifier,
        sweep_engine=engine,
        accounting_service=mock_accounting,
    )


def _serve_node(verifier: DepositVerifier, engine: SweepEngine, node: AsyncMock):
    return (
        patch.object(verifier, "_get_web3", return_value=node),
        patch.object(engine, "_get_web3", return_value=node),
    )


async def _drain_background_sweeps(processor: DepositProcessor) -> None:
    await processor.stop()


# ─── Deposit → sweep → credit, all on the accounting chain ──────────────


@pytest.mark.asyncio
async def test_erc20_deposit_on_the_accounting_chain_is_verified_swept_and_credited(
    processor, verifier, engine, mock_accounting, injected_chain
):
    node = _single_chain_node(
        receipts={
            ERC20_DEPOSIT_TX: {
                "status": 1,
                "blockNumber": DEPOSIT_BLOCK,
                "to": TOKEN_ADDRESS,
                "logs": [_erc20_transfer_log(ONE_HONOR)],
            }
        },
        broadcast_hashes=(GAS_FUNDING_TX_HASH, SWEEP_TX_HASH),
    )
    verifier_patch, engine_patch = _serve_node(verifier, engine, node)

    with (
        verifier_patch,
        engine_patch,
        patch.object(engine, "_get_erc20_balance", new_callable=AsyncMock, return_value=ONE_HONOR),
    ):
        result = await processor.process_deposit(
            chain_type="evm",
            chain_id=SAME_CHAIN_ID,
            tx_hash=ERC20_DEPOSIT_TX,
            amount=ONE_HONOR,
            log_index=3,
            version=0,
            auth=PRIVATE_READ_AUTH,
        )

        assert result["status"] == "pending"
        assert result["token_address"] == TOKEN_ADDRESS
        deposit_id_hex = result["deposit_id"]

        await _drain_background_sweeps(processor)

    mock_accounting.get_token_id.assert_awaited_once_with(SAME_CHAIN_ID, TOKEN_ADDRESS)
    sweep_kwargs = mock_accounting.generate_sweep_erc20.await_args.kwargs
    assert sweep_kwargs["chain_id"] == SAME_CHAIN_ID
    assert sweep_kwargs["token_address"] == TOKEN_ADDRESS
    assert sweep_kwargs["amount"] == ONE_HONOR

    credit_kwargs = mock_accounting.credit_deposit.await_args.kwargs
    assert credit_kwargs["beneficiary"] == BENEFICIARY
    assert credit_kwargs["amount"] == ONE_HONOR
    assert credit_kwargs["token_id"] == await mock_accounting.get_token_id(
        SAME_CHAIN_ID, TOKEN_ADDRESS
    )
    assert "0x" + credit_kwargs["deposit_id"].hex() == deposit_id_hex

    assert engine.get_record_by_deposit_id(deposit_id_hex) is None


@pytest.mark.asyncio
async def test_erc20_sweep_sizes_gas_funding_from_the_contract_sweep_limit(
    processor, verifier, engine, mock_accounting, injected_chain
):
    """Gas funding is derived from the contract's sweep limit, not a hardcoded default."""
    node = _single_chain_node(
        receipts={
            ERC20_DEPOSIT_TX: {
                "status": 1,
                "blockNumber": DEPOSIT_BLOCK,
                "to": TOKEN_ADDRESS,
                "logs": [_erc20_transfer_log(ONE_HONOR)],
            }
        },
        broadcast_hashes=(GAS_FUNDING_TX_HASH, SWEEP_TX_HASH),
    )
    verifier_patch, engine_patch = _serve_node(verifier, engine, node)

    with (
        verifier_patch,
        engine_patch,
        patch.object(engine, "_get_erc20_balance", new_callable=AsyncMock, return_value=ONE_HONOR),
    ):
        await processor.process_deposit(
            chain_type="evm",
            chain_id=SAME_CHAIN_ID,
            tx_hash=ERC20_DEPOSIT_TX,
            amount=ONE_HONOR,
            log_index=3,
            version=0,
            auth=PRIVATE_READ_AUTH,
        )
        await _drain_background_sweeps(processor)

    gas_kwargs = mock_accounting.generate_gas_funding_tx.await_args.kwargs
    assert gas_kwargs["chain_id"] == SAME_CHAIN_ID
    assert gas_kwargs["to_deposit_address"] == DEPOSIT_ADDRESS
    assert gas_kwargs["gas_amount"] == ERC20_SWEEP_GAS_LIMIT * SAPPHIRE_GAS_PRICE * 13 // 10
    assert gas_kwargs["gas_amount"] != 200_000_000_000_000
    assert gas_kwargs["gas_price"] == SAPPHIRE_GAS_PRICE
    node.eth.get_transaction_count.assert_any_await(GAS_TANK_ADDRESS, "pending")


@pytest.mark.asyncio
async def test_native_deposit_on_the_accounting_chain_is_swept_and_credited(
    processor, verifier, engine, mock_accounting, injected_chain
):
    node = _single_chain_node(
        receipts={
            NATIVE_DEPOSIT_TX: {
                "status": 1,
                "blockNumber": DEPOSIT_BLOCK,
                "to": DEPOSIT_ADDRESS,
                "logs": [],
            }
        },
        transactions={
            NATIVE_DEPOSIT_TX: {
                "to": DEPOSIT_ADDRESS,
                "value": TWO_ROSE,
                "from": "0x" + "de" * 20,
            }
        },
        native_balance=TWO_ROSE,
        broadcast_hashes=(GAS_FUNDING_TX_HASH, SWEEP_TX_HASH),
    )
    verifier_patch, engine_patch = _serve_node(verifier, engine, node)

    with verifier_patch, engine_patch:
        result = await processor.process_deposit(
            chain_type="evm",
            chain_id=SAME_CHAIN_ID,
            tx_hash=NATIVE_DEPOSIT_TX,
            amount=TWO_ROSE,
            log_index=0,
            version=0,
            auth=PRIVATE_READ_AUTH,
        )
        assert result["status"] == "pending"
        assert result["token_address"] is None
        await _drain_background_sweeps(processor)

    mock_accounting.get_token_id.assert_awaited_once_with(SAME_CHAIN_ID, None)
    sweep_kwargs = mock_accounting.generate_sweep_native.await_args.kwargs
    assert sweep_kwargs["chain_id"] == SAME_CHAIN_ID
    assert sweep_kwargs["amount"] == TWO_ROSE
    gas_kwargs = mock_accounting.generate_gas_funding_tx.await_args.kwargs
    assert gas_kwargs["gas_amount"] == NATIVE_SWEEP_GAS_LIMIT * SAPPHIRE_GAS_PRICE * 13 // 10

    credit_kwargs = mock_accounting.credit_deposit.await_args.kwargs
    assert credit_kwargs["amount"] == TWO_ROSE
    assert credit_kwargs["beneficiary"] == BENEFICIARY
    assert engine.get_record_by_deposit_id(result["deposit_id"]) is None


@pytest.mark.asyncio
async def test_deposit_below_the_injected_erc20_floor_is_rejected(
    processor, verifier, engine, injected_chain
):
    short = injected_chain.min_deposit_erc20_wei - 1
    node = _single_chain_node(
        receipts={
            ERC20_DEPOSIT_TX: {
                "status": 1,
                "blockNumber": DEPOSIT_BLOCK,
                "to": TOKEN_ADDRESS,
                "logs": [_erc20_transfer_log(short)],
            }
        },
    )
    verifier_patch, engine_patch = _serve_node(verifier, engine, node)

    with verifier_patch, engine_patch, pytest.raises(ValueError, match="minimum"):
        await processor.process_deposit(
            chain_type="evm",
            chain_id=SAME_CHAIN_ID,
            tx_hash=ERC20_DEPOSIT_TX,
            amount=short,
            log_index=3,
            version=0,
            auth=PRIVATE_READ_AUTH,
        )


@pytest.mark.asyncio
async def test_gas_funding_tx_cannot_be_claimed_as_a_deposit_on_the_same_chain(
    processor, verifier, engine, injected_chain
):
    """Native transfer to deposit address from gas tank must not trigger self-deposit."""
    node = _single_chain_node(
        receipts={
            ERC20_DEPOSIT_TX: {
                "status": 1,
                "blockNumber": DEPOSIT_BLOCK,
                "to": TOKEN_ADDRESS,
                "logs": [_erc20_transfer_log(ONE_HONOR)],
            }
        },
        broadcast_hashes=(GAS_FUNDING_TX_HASH, SWEEP_TX_HASH),
    )
    verifier_patch, engine_patch = _serve_node(verifier, engine, node)

    with (
        verifier_patch,
        engine_patch,
        patch.object(engine, "_get_erc20_balance", new_callable=AsyncMock, return_value=ONE_HONOR),
    ):
        await processor.process_deposit(
            chain_type="evm",
            chain_id=SAME_CHAIN_ID,
            tx_hash=ERC20_DEPOSIT_TX,
            amount=ONE_HONOR,
            log_index=3,
            version=0,
            auth=PRIVATE_READ_AUTH,
        )
        await _drain_background_sweeps(processor)

        gas_tx_hex = "0x" + GAS_FUNDING_TX_HASH.hex()
        assert gas_tx_hex.lower() in engine.gas_funding_tx_hashes

        with pytest.raises(ValueError, match="[Gg]as funding"):
            await processor.process_deposit(
                chain_type="evm",
                chain_id=SAME_CHAIN_ID,
                tx_hash=gas_tx_hex,
                amount=injected_chain.gas_funding_amount_wei,
                log_index=0,
                version=0,
                auth=PRIVATE_READ_AUTH,
            )


# ─── Withdrawal back out over the same chain ────────────────────────────


@pytest.fixture
def withdrawal_accounting() -> MagicMock:
    service = MagicMock()
    service.get_all_pending_withdrawals = AsyncMock(
        return_value={"pending": [], "current_block": LATEST_BLOCK}
    )
    service.resolve_withdrawal = AsyncMock(return_value=MagicMock(status="submitted"))
    service._send_raw_transaction = AsyncMock(return_value=WITHDRAWAL_TX_HASH)

    reader = MagicMock()
    reader.functions.withdrawals.return_value.call = AsyncMock(
        return_value=(
            BENEFICIARY,
            WITHDRAWAL_TO_ADDRESS,
            TWO_ROSE,
            DEPOSIT_BLOCK,
            b"\x00" * 32,
            True,
            encode(["uint64"], [0]),
        )
    )
    reader.functions.resolveWithdrawal.return_value.call = AsyncMock(
        return_value=WITHDRAWAL_SIGNED_TX
    )
    reader.functions.withdrawalCount.return_value.call = AsyncMock(return_value=1)
    service._get_reader_contract = MagicMock(return_value=reader)
    service._get_token_context = AsyncMock(return_value=SimpleNamespace(chain_id=SAME_CHAIN_ID))
    return service


def _build_withdrawal_processor(
    withdrawal_accounting: MagicMock, chain_rpc_urls: dict[int, str]
) -> WithdrawalProcessor:
    settings = MagicMock(
        withdrawal_poll_interval=1,
        withdrawal_resolution_timeout=1,
        sapphire_rpc_url=SAME_CHAIN_RPC_URL,
        accounting_contract_address="0x" + "ab" * 20,
        chain_rpc_urls=dict(chain_rpc_urls),
    )
    contract = MagicMock()
    contract.functions.evmAddress.return_value.call = AsyncMock(return_value=EVM_SIGNER_ADDRESS)
    contract.functions.nonces.return_value.call = AsyncMock(return_value=0)

    with (
        patch(
            "src.services.withdrawal_processor.load_settings",
            return_value=settings,
        ),
        patch(
            "src.services.withdrawal_processor.AccountingContractService",
            return_value=withdrawal_accounting,
        ),
        patch("src.services.withdrawal_processor.AsyncWeb3") as mock_async_web3,
    ):
        sapphire = MagicMock()
        sapphire.eth.contract.return_value = contract
        mock_async_web3.return_value = sapphire
        proc = WithdrawalProcessor()

    proc.accounting_service = withdrawal_accounting
    proc._contract = contract
    return proc


@pytest.mark.asyncio
async def test_withdrawal_to_the_accounting_chain_resolves_and_broadcasts(withdrawal_accounting):
    """Withdrawal signed on SAME_CHAIN_ID resolves and broadcasts back to SAME_CHAIN_ID."""
    processor = _build_withdrawal_processor(
        withdrawal_accounting, {SAME_CHAIN_ID: SAME_CHAIN_RPC_URL}
    )
    node = _single_chain_node(receipts={})
    processor._destination_web3[SAME_CHAIN_ID] = node
    processor._is_running = True

    await processor._process_chain(
        SAME_CHAIN_ID,
        [{"index": 0, "chain_id": SAME_CHAIN_ID, "block_number": DEPOSIT_BLOCK}],
    )

    processor._contract.functions.nonces.assert_any_call(SAME_CHAIN_ID)
    node.eth.get_transaction_count.assert_any_await(EVM_SIGNER_ADDRESS, "pending")
    withdrawal_accounting._send_raw_transaction.assert_awaited_once_with(
        SAME_CHAIN_ID, WITHDRAWAL_SIGNED_TX
    )
    assert processor._chain_high_water_mark[SAME_CHAIN_ID] == 0


@pytest.mark.asyncio
async def test_pending_withdrawal_for_the_accounting_chain_is_grouped_and_eligible(
    withdrawal_accounting,
):
    processor = _build_withdrawal_processor(
        withdrawal_accounting, {SAME_CHAIN_ID: SAME_CHAIN_RPC_URL}
    )
    withdrawal_accounting.get_all_pending_withdrawals.return_value = {
        "pending": [
            {"index": 0, "block_number": DEPOSIT_BLOCK, "chain_id": SAME_CHAIN_ID},
            {"index": 1, "block_number": LATEST_BLOCK, "chain_id": SAME_CHAIN_ID},
        ],
        "current_block": LATEST_BLOCK,
    }

    pending = await processor._get_pending_withdrawals()

    assert list(pending) == [SAME_CHAIN_ID]
    assert [w["index"] for w in pending[SAME_CHAIN_ID]] == [0]


# ─── One verified endpoint, both roles ──────────────────────────────────


@pytest.mark.asyncio
async def test_one_verified_endpoint_serves_deposits_sweeps_and_withdrawals(
    monkeypatch, mock_accounting, withdrawal_accounting, tmp_path
):
    """Single verified endpoint is shared across verifier, engine, and withdrawals."""

    async def probe(url: str, timeout: float) -> int:
        if url == SAME_CHAIN_RPC_URL:
            return SAME_CHAIN_ID
        raise ConnectionError("localnet has no second chain")

    monkeypatch.setattr(rpc_identity, "_probe_chain_id", probe)

    configured = {SAME_CHAIN_ID: SAME_CHAIN_RPC_URL, FOREIGN_CHAIN_ID: FOREIGN_RPC_URL}
    served = await initialize_verified_chain_rpc_urls(
        SimpleNamespace(chain_rpc_urls=dict(configured))
    )
    assert served == {SAME_CHAIN_ID: SAME_CHAIN_RPC_URL}

    verifier = DepositVerifier(dict(configured))
    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls=dict(configured),
        state_dir=str(tmp_path),
    )
    withdrawals = _build_withdrawal_processor(withdrawal_accounting, configured)

    client = verifier._get_web3(SAME_CHAIN_ID)
    assert engine._get_web3(SAME_CHAIN_ID) is client
    assert withdrawals._get_destination_web3(SAME_CHAIN_ID) is client

    for resolve in (
        lambda: verifier._get_web3(FOREIGN_CHAIN_ID),
        lambda: engine._get_web3(FOREIGN_CHAIN_ID),
        lambda: withdrawals._get_destination_web3(FOREIGN_CHAIN_ID),
    ):
        with pytest.raises(ValueError, match="No verified RPC endpoint"):
            resolve()
