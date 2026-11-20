import asyncio
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eth_account import Account
from web3.exceptions import TransactionNotFound

import src.services.rpc_identity as rpc_identity
import src.services.sweep_engine as sweep_engine_module
from src.clients.rofl import TransactionRevertedError
from src.config.chain_config import get_finality_depth
from src.services.sweep_engine import SweepEngine, SweepRecord, SweepState


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path)


@pytest.fixture
def fast_clock(monkeypatch):
    """Let _wait_for_final_receipt poll a few times, then time out, without sleeping."""
    ticks = itertools.count(0, 100)
    monkeypatch.setattr(sweep_engine_module, "time", SimpleNamespace(time=lambda: next(ticks)))
    monkeypatch.setattr(sweep_engine_module.asyncio, "sleep", AsyncMock())


def _write_record(state_dir, record):
    """Helper to persist a SweepRecord to the state directory."""
    path = Path(state_dir) / f"sweep_{record.deposit_id_hex.removeprefix('0x')}.json"
    path.write_text(json.dumps(record.to_dict()))


def _write_legacy_record(state_dir, record):
    """Persist a record under the old address-keyed filename."""
    path = Path(state_dir) / f"sweep_{record.deposit_address.lower()}_{record.chain_id}.json"
    path.write_text(json.dumps(record.to_dict()))
    return path


def test_load_incomplete_sweeps(state_dir):
    """Startup should find incomplete sweep records with recovery metadata."""
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
        gas_funding_tx_hash="0x" + "ff" * 32,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    incomplete = engine.load_incomplete_sweeps()
    assert len(incomplete) == 1
    assert incomplete[0].state == SweepState.GAS_FUNDED
    assert incomplete[0].gas_funding_tx_hash == "0x" + "ff" * 32
    # Recovery metadata must be populated
    assert incomplete[0].amount == 10**18
    assert incomplete[0].token_id_hex == "0x" + "11" * 32
    assert incomplete[0].deposit_id_hex == "0x" + "22" * 32


def test_load_migrates_legacy_address_keyed_record(state_dir):
    """A pre-deposit_id file is rewritten under the deposit_id key exactly once.

    The old path must be unlinked, or the legacy file would resurrect the
    record on every restart.
    """
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
    legacy_path = _write_legacy_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    incomplete = engine.load_incomplete_sweeps()
    assert len(incomplete) == 1
    assert not legacy_path.exists()
    migrated = engine.get_record_by_deposit_id("0x" + "22" * 32)
    assert migrated is not None
    assert migrated.state == SweepState.GAS_FUNDED

    # A second load must see exactly one record, not a resurrected duplicate.
    assert len(engine.load_incomplete_sweeps()) == 1


def test_load_dedupes_legacy_and_new_keyed_files(state_dir):
    """Legacy and deposit_id-keyed files for the same deposit load as one record.

    This happens when new code wrote the deposit_id file before the legacy
    file from an older deploy was cleaned up: the newer copy must win and the
    legacy file must be dropped without double-counting the deposit.
    """
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
    legacy_path = _write_legacy_record(state_dir, record)
    newer = SweepRecord.from_dict({**record.to_dict(), "state": SweepState.SWEPT.value})
    _write_record(state_dir, newer)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    incomplete = engine.load_incomplete_sweeps()
    assert len(incomplete) == 1
    assert incomplete[0].state == SweepState.SWEPT
    assert not legacy_path.exists()


@pytest.mark.asyncio
async def test_resume_swept_record_credits_and_cleans_up(state_dir):
    """A record in SWEPT state should retry creditDeposit and clean up on success."""
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.SWEPT,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
    )
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.credit_deposit = AsyncMock()

    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    await engine.resume_incomplete_sweeps()

    # Credit should have been called with the persisted metadata
    mock_accounting.credit_deposit.assert_called_once()
    call_kwargs = mock_accounting.credit_deposit.call_args
    assert call_kwargs[1]["beneficiary"] == "0x" + "bb" * 20
    assert call_kwargs[1]["amount"] == 10**18

    # Record should be cleaned up after successful credit
    assert engine.get_record_by_deposit_id("0x" + "22" * 32) is None


def _seed_web3(engine: SweepEngine, chain_id: int, receipt_outcome) -> AsyncMock:
    """Inject a mocked AsyncWeb3 whose get_transaction_receipt behaves as specified.

    receipt_outcome is either a dict (returned), or an Exception class/instance (raised).
    """
    get_receipt = AsyncMock()
    if isinstance(receipt_outcome, dict):
        get_receipt.return_value = receipt_outcome
    else:
        get_receipt.side_effect = receipt_outcome

    w3 = SimpleNamespace(
        eth=SimpleNamespace(
            get_block=AsyncMock(return_value={"number": 1000}),
            get_transaction_count=AsyncMock(return_value=0),
            get_transaction_receipt=get_receipt,
            send_raw_transaction=AsyncMock(),
        )
    )
    engine._web3_cache[chain_id] = w3
    return get_receipt


@pytest.mark.asyncio
async def test_resume_sweep_prepared_with_mined_sweep_tx_is_promoted_and_credited(state_dir):
    """Crash window regression: SWEEP_PREPARED + mined sweep tx must credit on resume.

    A crash between the sweep broadcast and the SWEPT state flip would otherwise
    leave funds swept on-chain but permanently uncredited.
    """
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.SWEEP_PREPARED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
        sweep_tx="0x010203",
        sweep_nonce=0,
        gas_funding_tx_hash="0x" + "ff" * 32,
    )
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.credit_deposit = AsyncMock()

    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    _seed_web3(engine, 84532, {"status": 1, "blockNumber": 100})

    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_called_once()
    assert engine.get_record_by_deposit_id("0x" + "22" * 32) is None


@pytest.mark.asyncio
async def test_resume_does_not_credit_a_sweep_that_is_not_final_yet(state_dir, fast_clock):
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.SWEEP_PREPARED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
        sweep_tx="0x010203",
        sweep_nonce=0,
    )
    _write_record(state_dir, record)
    mock_accounting = AsyncMock()
    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    # Mined at block 995 with 1000 as the latest block: 5 of the 15 confirmations required.
    _seed_web3(engine, 84532, {"status": 1, "blockNumber": 995})

    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_not_called()
    assert engine.get_record_by_deposit_id("0x" + "22" * 32).state == SweepState.SWEEP_PREPARED


@pytest.mark.asyncio
async def test_resume_does_not_credit_a_receipt_read_while_the_chain_was_dropped(
    state_dir, monkeypatch
):
    """Recovery reads the receipt through a client that may lose its chain
    mid-call. A success receipt from that endpoint must not promote, credit, or
    delete the record; the next pass re-reads it once the chain is served again."""
    url = "https://fake"
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

    async def receipt(_tx_hash):
        answers[url] = 1  # the endpoint drifts to another chain mid-call
        await rpc_identity.reverify_chain_rpc_urls(settings)
        return {"status": 1, "blockNumber": 100}

    w3 = SimpleNamespace(
        eth=SimpleNamespace(
            get_block=AsyncMock(return_value={"number": 1000}),
            get_transaction_count=AsyncMock(return_value=0),
            get_transaction_receipt=receipt,
        )
    )
    monkeypatch.setitem(rpc_identity._clients, url, w3)

    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.SWEEP_PREPARED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
        sweep_tx="0x010203",
        sweep_nonce=0,
    )
    _write_record(state_dir, record)
    mock_accounting = AsyncMock()
    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={84532: url},
        state_dir=state_dir,
    )

    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_not_called()
    persisted = engine.get_record_by_deposit_id("0x" + "22" * 32)
    assert persisted.state == SweepState.SWEEP_PREPARED
    assert persisted.sweep_tx_hash == "0x" + "dd" * 32


@pytest.mark.asyncio
async def test_resume_sweep_prepared_with_unmined_sweep_tx_is_left_alone(state_dir, fast_clock):
    """A sweep tx that never mines leaves the record SWEEP_PREPARED for the next pass."""
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.SWEEP_PREPARED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
        sweep_tx="0x010203",
        sweep_nonce=0,
    )
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.credit_deposit = AsyncMock()

    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    _seed_web3(engine, 84532, TransactionNotFound("not mined"))

    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_not_called()
    persisted = engine.get_record_by_deposit_id("0x" + "22" * 32)
    assert persisted is not None
    assert persisted.state == SweepState.SWEEP_PREPARED


@pytest.mark.asyncio
async def test_resume_sweep_prepared_with_reverted_sweep_tx_releases_the_record(state_dir):
    """A final reverted sweep moved nothing: it is not credited, and its record is released."""
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.SWEEP_PREPARED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
        sweep_tx="0x010203",
        sweep_nonce=0,
    )
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.credit_deposit = AsyncMock()

    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    _seed_web3(engine, 84532, {"status": 0, "blockNumber": 100})

    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_not_called()
    assert engine.get_record_by_deposit_id("0x" + "22" * 32) is None
    assert engine.get_record(record.token_id_hex, record.deposit_address) is None


@pytest.mark.asyncio
async def test_resume_drives_each_record_through_sweep(state_dir):
    """Every unfinished record on disk is driven through sweep()."""
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.SCHEDULED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        token_address=None,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    engine.sweep = AsyncMock()

    await engine.resume_incomplete_sweeps()

    engine.sweep.assert_called_once_with(b"\x22" * 32)


@pytest.mark.asyncio
async def test_resume_swept_record_already_processed(state_dir):
    """If credit reverts with DepositAlreadyProcessed, record is still cleaned up."""
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.SWEPT,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
    )
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.credit_deposit = AsyncMock(
        side_effect=TransactionRevertedError(
            "Transaction reverted: DepositAlreadyProcessed",
            error_name="DepositAlreadyProcessed",
        )
    )

    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    await engine.resume_incomplete_sweeps()

    # Record still cleaned up — DepositAlreadyProcessed means credit already happened
    assert engine.get_record_by_deposit_id("0x" + "22" * 32) is None


@pytest.mark.asyncio
async def test_recovery_loop_resumes_scheduled_record(state_dir, monkeypatch):
    """The periodic recovery loop — not just startup — must resume SCHEDULED records.

    This is the path that matters for long-running instances: a crash two hours
    into uptime shouldn't need a full restart to pick up a stuck sweep.
    """
    monkeypatch.setattr(sweep_engine_module, "SWEEP_RECOVERY_INTERVAL", 0.01)

    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.SCHEDULED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        token_address=None,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    engine.sweep = AsyncMock()

    engine.start_recovery_loop()
    try:
        for _ in range(100):  # up to 1s
            if engine.sweep.called:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Recovery loop did not call sweep within 1s")
    finally:
        await engine.stop_recovery_loop()

    assert engine.sweep.called


@pytest.mark.asyncio
async def test_recovery_loop_persists_error_for_scheduled_record(state_dir, monkeypatch):
    """Persistent resume failures must set record.error so user retries can cleanup."""
    monkeypatch.setattr(sweep_engine_module, "SWEEP_RECOVERY_INTERVAL", 0.01)

    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.SCHEDULED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        token_address=None,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    engine.sweep = AsyncMock(side_effect=RuntimeError("gas tank empty"))

    engine.start_recovery_loop()
    try:
        for _ in range(100):
            if engine.sweep.called:
                break
            await asyncio.sleep(0.01)
    finally:
        await engine.stop_recovery_loop()

    persisted = engine.get_record_by_deposit_id("0x" + "22" * 32)
    assert persisted is not None
    assert persisted.error == "gas tank empty"


@pytest.mark.asyncio
async def test_persist_error_does_not_overwrite_a_signed_sweep(state_dir):
    """If sweep() fails AFTER the sweep tx was signed, the error flag must NOT be set —
    doing so lets user retries call cleanup_record and orphan the pending tx.
    """
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.SWEEP_PREPARED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
        sweep_tx="0x010203",
        sweep_nonce=0,
        gas_funding_tx_hash="0x" + "ff" * 32,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )

    engine.persist_error(record.deposit_id_hex, "boom")

    persisted = engine.get_record_by_deposit_id("0x" + "22" * 32)
    assert persisted is not None
    assert persisted.error is None, "error must not overwrite a record that has sweep_tx_hash"
    assert persisted.sweep_tx_hash == "0x" + "dd" * 32


def _lost_tx_engine(state_dir, account_nonce, send):
    """An engine whose node lost a sweep tx signed with nonce 5."""
    raw = (
        Account.create()
        .sign_transaction(
            dict(
                nonce=5,
                gasPrice=10**9,
                gas=21000,
                to=Account.create().address,
                value=1,
                chainId=84532,
            )
        )
        .raw_transaction
    )
    record = SweepRecord(
        token_id_hex="0x" + "11" * 32,
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.SWEEP_PREPARED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
        sweep_tx="0x" + raw.hex(),
        sweep_nonce=5,
    )
    _write_record(state_dir, record)
    engine = SweepEngine(
        accounting_service=AsyncMock(), chain_rpc_urls={84532: "https://fake"}, state_dir=state_dir
    )
    engine._web3_cache[84532] = SimpleNamespace(
        eth=SimpleNamespace(
            get_block=AsyncMock(return_value={"number": 1000}),
            get_transaction_receipt=AsyncMock(side_effect=TransactionNotFound("lost")),
            get_transaction_count=AsyncMock(return_value=account_nonce),
            send_raw_transaction=send,
        )
    )
    return engine, record, raw


@pytest.mark.asyncio
@pytest.mark.parametrize("send_error", [None, RuntimeError("already known")])
async def test_sweep_rebroadcasts_a_sweep_tx_the_node_lost_and_keeps_its_hash(
    state_dir, fast_clock, send_error
):
    """The node does not know the tx: resend the same signed tx and never re-sign."""
    send = AsyncMock(side_effect=send_error)
    engine, record, raw = _lost_tx_engine(state_dir, 5, send)

    await engine.resume_incomplete_sweeps()

    send.assert_awaited_once_with(raw)
    assert (
        engine.get_record_by_deposit_id(record.deposit_id_hex).sweep_tx_hash == record.sweep_tx_hash
    )


@pytest.mark.asyncio
async def test_sweep_deletes_a_record_whose_nonce_was_used_by_another_tx(state_dir):
    send = AsyncMock()
    engine, record, _ = _lost_tx_engine(state_dir, 6, send)

    await engine.resume_incomplete_sweeps()

    send.assert_not_awaited()
    assert engine.get_record_by_deposit_id(record.deposit_id_hex) is None
    # The nonce is read at a final block (the mocked latest block is 1000).
    engine._web3_cache[84532].eth.get_transaction_count.assert_awaited_once_with(
        record.deposit_address, 1000 - get_finality_depth(84532)
    )


@pytest.mark.asyncio
async def test_resume_skips_an_address_held_by_a_live_sweep(state_dir):
    engine, record, _ = _lost_tx_engine(state_dir, 5, AsyncMock())
    engine.sweep = AsyncMock()

    async with engine._get_address_lock(record.deposit_address, record.chain_id):
        await engine.resume_incomplete_sweeps()

    engine.sweep.assert_not_called()
