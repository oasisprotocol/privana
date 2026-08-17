"""Same-chain E2E (M6.8): the accounting chain is also the deposit source chain.

One chain ID plays both roles — deposits arrive, sweeps and gas funding are
broadcast, credits are recorded, and withdrawals are signed and broadcast, all
against a single endpoint. Every per-chain number comes from a ``ChainConfig``
this module builds itself and injects over the consuming modules' lookups, so
the scenarios stay true to the shape of the config rather than to whatever
``CHAIN_CONFIGS`` or ``.env.localnet`` happens to hold.

Mocking level matches ``test_sweep_engine.py`` / ``test_withdrawals.py``: real
DepositVerifier, SweepEngine, DepositProcessor and WithdrawalProcessor over a
mocked AccountingContractService and a mocked node. No live RPC.
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

# The one chain: accounting home, deposit source, sweep target, withdrawal
# destination. 23293 is the sapphire-localnet mirror of the 23295 config.
SAME_CHAIN_ID = 23293
SAME_CHAIN_RPC_URL = "https://sapphire-localnet.example.invalid"
# A second configured-but-unverified chain, used only to show the same-chain
# wiring still fails closed for everything else.
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
LATEST_BLOCK = 110  # 10 confirmations, well past the injected finality depth
# Sapphire publishes 100 gwei; the injected gas funding amount is sized for it.
SAPPHIRE_GAS_PRICE = 100_000_000_000

ONE_HONOR = 10**18  # 18-decimal localnet ERC-20, exactly the injected floor
TWO_ROSE = 2 * 10**18

PRIVATE_READ_AUTH = PrivateReadAuth(token=b"\x00" * 65, user_address=BENEFICIARY)


class _AwaitableValue:
    """Re-awaitable stand-in for async properties (``w3.eth.gas_price``)."""

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
            bytes(12) + bytes.fromhex("de" * 20),  # from: some funder
            bytes(12) + bytes.fromhex(DEPOSIT_ADDRESS[2:]),  # to: deposit address
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
    """The one node serving both roles.

    Deposit reads and sweep broadcasts hit the same object because on this chain
    they are the same endpoint: a hash the deposit path asks about could just as
    well be a hash the sweep path broadcast. So receipts are answered by hash,
    never by call order.
    """
    receipts = {_hash_key(k): v for k, v in receipts.items()}
    transactions = {_hash_key(k): v for k, v in (transactions or {}).items()}
    # Broadcast transactions mine immediately, into the same hash table.
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
    """The single chain's config, built here rather than read from CHAIN_CONFIGS."""
    return ChainConfig(
        chain_id=SAME_CHAIN_ID,
        finality_depth=2,
        min_deposit_native_wei=10_000_000_000_000_000,  # 0.01 ROSE
        min_deposit_erc20_wei=ONE_HONOR,  # 1 HONOR (18 decimals)
        gas_funding_amount_wei=6_500_000_000_000_000,  # 65k gas * 100 gwei
        l2_type=L2Type.NONE,
        discovery_scan_chunk_blocks=100,  # Sapphire gateway eth_getLogs cap
        discovery_lookback_blocks=640,
        discovery_max_lookback_blocks=3_800,
    )


@pytest.fixture(autouse=True)
def injected_chain(monkeypatch, same_chain_config) -> ChainConfig:
    """Serve the whole flow from the injected config, and only that chain.

    The deposit floor, finality depth and gas funding amount are read from
    module-level derived lookups; replacing them with a single-chain mapping is
    what makes these scenarios independent of the shipped chain registry.
    """
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
        # l2_type NONE: Sapphire posts no calldata to an L1, so gas funding is
        # exactly the injected amount and the assertions can name it.
        assert chain_id == cfg.chain_id
        assert cfg.l2_type is L2Type.NONE
        return 0

    monkeypatch.setattr(sweep_engine, "estimate_l1_data_fee", _no_l1_data_fee)
    return cfg


@pytest.fixture
def token_registry() -> dict:
    """The token registration for this chain: native ROSE plus one local ERC-20.

    Keyed the way the contract keys it — (chainId, tokenAddress) — with None for
    native, so a lookup for a token that was never registered on this chain
    fails instead of quietly resolving.
    """
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
    """AccountingContractService stand-in, living on SAME_CHAIN_ID itself."""

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
    """Point both the verifier and the sweep engine at the one node."""
    return (
        patch.object(verifier, "_get_web3", return_value=node),
        patch.object(engine, "_get_web3", return_value=node),
    )


async def _drain_background_sweeps(processor: DepositProcessor) -> None:
    """Await the background sweep the API route fires and returns without."""
    await processor.stop()


# ─── Deposit → sweep → credit, all on the accounting chain ──────────────


@pytest.mark.asyncio
async def test_erc20_deposit_on_the_accounting_chain_is_verified_swept_and_credited(
    processor, verifier, engine, mock_accounting, injected_chain
):
    """A HONOR deposit whose source chain *is* the accounting chain.

    Same chain ID on the verification read, the gas funding broadcast, the sweep
    broadcast and the credit — nothing in the path treats the accounting chain as
    unable to be its own source.
    """
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

    # Verification, token resolution and sweep all name the one chain.
    mock_accounting.get_token_id.assert_awaited_once_with(SAME_CHAIN_ID, TOKEN_ADDRESS)
    sweep_kwargs = mock_accounting.generate_sweep_erc20.await_args.kwargs
    assert sweep_kwargs["chain_id"] == SAME_CHAIN_ID
    assert sweep_kwargs["token_address"] == TOKEN_ADDRESS
    assert sweep_kwargs["amount"] == ONE_HONOR

    # Credited against the token registered for this chain, for the full amount.
    credit_kwargs = mock_accounting.credit_deposit.await_args.kwargs
    assert credit_kwargs["beneficiary"] == BENEFICIARY
    assert credit_kwargs["amount"] == ONE_HONOR
    assert credit_kwargs["token_id"] == await mock_accounting.get_token_id(
        SAME_CHAIN_ID, TOKEN_ADDRESS
    )
    assert "0x" + credit_kwargs["deposit_id"].hex() == deposit_id_hex

    # Sweep completed: the record is gone, so nothing is left mid-flight.
    assert engine.get_record_by_deposit_id(deposit_id_hex) is None


@pytest.mark.asyncio
async def test_erc20_sweep_funds_gas_with_the_injected_chain_amount(
    processor, verifier, engine, mock_accounting, injected_chain
):
    """The gas funding leg spends the injected chain's amount, not the fallback.

    ``SweepEngine`` falls back to 200_000_000_000_000 wei for a chain it has no
    configuration for — a Sapphire sweep funded at that level cannot pay for
    itself at 100 gwei, so the assertion is that the injected value wins.
    """
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
    assert gas_kwargs["gas_amount"] == injected_chain.gas_funding_amount_wei
    assert gas_kwargs["gas_amount"] != 200_000_000_000_000  # engine's unknown-chain fallback
    # Gas price comes off the same node the sweep broadcasts to.
    assert gas_kwargs["gas_price"] == SAPPHIRE_GAS_PRICE
    # The gas tank nonce was read on this chain, for the gas tank address.
    node.eth.get_transaction_count.assert_any_await(GAS_TANK_ADDRESS, "pending")


@pytest.mark.asyncio
async def test_native_deposit_on_the_accounting_chain_is_swept_and_credited(
    processor, verifier, engine, mock_accounting, injected_chain
):
    """Native ROSE deposited on the chain that also holds the accounting contract."""
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

    # Native token id for this chain, resolved through the registry.
    mock_accounting.get_token_id.assert_awaited_once_with(SAME_CHAIN_ID, None)
    sweep_kwargs = mock_accounting.generate_sweep_native.await_args.kwargs
    assert sweep_kwargs["chain_id"] == SAME_CHAIN_ID
    assert sweep_kwargs["amount"] == TWO_ROSE
    # Native sweeps fund gas from the same injected amount.
    gas_kwargs = mock_accounting.generate_gas_funding_tx.await_args.kwargs
    assert gas_kwargs["gas_amount"] == injected_chain.gas_funding_amount_wei

    credit_kwargs = mock_accounting.credit_deposit.await_args.kwargs
    assert credit_kwargs["amount"] == TWO_ROSE
    assert credit_kwargs["beneficiary"] == BENEFICIARY
    assert engine.get_record_by_deposit_id(result["deposit_id"]) is None


@pytest.mark.asyncio
async def test_deposit_below_the_injected_erc20_floor_is_rejected(
    processor, verifier, engine, injected_chain
):
    """The floor that applies is the injected chain's, on this chain too."""
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
    """Same-chain hazard: gas funding lands on the very chain deposits come from.

    The engine's own funding transfer is a native transfer to the deposit
    address, so without the exclusion it would read as a fresh deposit.
    """
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
    """AccountingContractService stand-in for the withdrawal side."""
    service = MagicMock()
    service.get_all_pending_withdrawals = AsyncMock(
        return_value={"pending": [], "current_block": LATEST_BLOCK}
    )
    service.resolve_withdrawal = AsyncMock(return_value=MagicMock(status="submitted"))
    service._send_raw_transaction = AsyncMock(return_value=WITHDRAWAL_TX_HASH)

    reader = MagicMock()
    # withdrawals(index): (user, to, amount, block, tokenId, resolved, txId)
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
    """Construct a WithdrawalProcessor whose accounting chain is its destination.

    ``sapphire_rpc_url`` and the destination endpoint for SAME_CHAIN_ID are the
    same URL — that is the whole point of the scenario.
    """
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
    """A withdrawal signed on SAME_CHAIN_ID is broadcast back to SAME_CHAIN_ID.

    Goes through ``_process_chain`` so the nonce readiness gate runs: the
    contract's ``nonces(23293)`` is compared against the signer's pending nonce
    on 23293 itself, which is the same-chain case of that check.
    """
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

    # Nonce gate consulted the contract for this chain and the chain for itself.
    processor._contract.functions.nonces.assert_any_call(SAME_CHAIN_ID)
    node.eth.get_transaction_count.assert_any_await(EVM_SIGNER_ADDRESS, "pending")
    # Resolved signature broadcast to the same chain it was signed for.
    withdrawal_accounting._send_raw_transaction.assert_awaited_once_with(
        SAME_CHAIN_ID, WITHDRAWAL_SIGNED_TX
    )
    assert processor._chain_high_water_mark[SAME_CHAIN_ID] == 0


@pytest.mark.asyncio
async def test_pending_withdrawal_for_the_accounting_chain_is_grouped_and_eligible(
    withdrawal_accounting,
):
    """Discovery groups the withdrawal under the chain it is destined for."""
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
    assert [w["index"] for w in pending[SAME_CHAIN_ID]] == [0]  # index 1 lacks block delay


# ─── One verified endpoint, both roles ──────────────────────────────────


@pytest.mark.asyncio
async def test_one_verified_endpoint_serves_deposits_sweeps_and_withdrawals(
    monkeypatch, mock_accounting, withdrawal_accounting, tmp_path
):
    """The identity check leaves one client, and all three services share it.

    Deposit verification, sweep broadcast and withdrawal broadcast resolving the
    same client for SAME_CHAIN_ID is what "the accounting chain is its own source
    chain" means at the transport layer. The unverified second chain stays
    refused everywhere, so sharing an endpoint does not widen what is served.
    """

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

    # Every service still holds the un-narrowed mapping; the verified set gates.
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
