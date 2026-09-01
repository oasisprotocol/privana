"""Tests for the deposit discovery service (GET /deposits/pending scan logic)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import Web3RPCError

from src.services.deposit_discovery import (
    MAX_CLASSIFIED_CANDIDATES,
    MAX_PENDING_CANDIDATES,
    DepositDiscoveryService,
    DiscoveryRPCError,
)
from src.services.deposit_processor import compute_deposit_id

CHAIN_ID = 84532  # finality 15, min erc20 1_000_000, lookback 1800, chunk 5000
FINALITY = 15
LOOKBACK = 1_800
DEPOSIT_ADDRESS = Web3.to_checksum_address("0x" + "aa" * 20)
BENEFICIARY = Web3.to_checksum_address("0x" + "bb" * 20)
USDC = Web3.to_checksum_address("0x" + "11" * 20)
OTHER_TOKEN = Web3.to_checksum_address("0x" + "22" * 20)
USDC_TOKEN_ID = "0x" + "33" * 32
LATEST_BLOCK = 100_000
TO_BLOCK = LATEST_BLOCK - FINALITY
FROM_BLOCK = TO_BLOCK - LOOKBACK + 1


def _registry_rows() -> list[dict]:
    return [
        {"token_id": USDC_TOKEN_ID, "token_type": 1, "chain_id": CHAIN_ID, "token_address": USDC},
        {"token_id": "0x" + "44" * 32, "token_type": 0, "chain_id": CHAIN_ID},  # native
        {  # other chain — must be excluded from the scan filter
            "token_id": "0x" + "55" * 32,
            "token_type": 1,
            "chain_id": 11155111,
            "token_address": OTHER_TOKEN,
        },
    ]


def _log(tx_byte: str, block_number: int, log_index: int = 0, amount: int = 5_000_000) -> dict:
    return {
        "transactionHash": "0x" + tx_byte * 32,
        "logIndex": log_index,
        "address": USDC,
        "data": amount.to_bytes(32, "big"),
        "blockNumber": block_number,
    }


def _make_service(
    logs: list[dict] | list[list[dict]] | None = None,
    registry: list[dict] | None = None,
) -> DepositDiscoveryService:
    accounting = MagicMock()
    accounting.list_all_tokens = AsyncMock(
        return_value=_registry_rows() if registry is None else registry
    )
    accounting.is_deposit_processed = AsyncMock(return_value=False)
    sweep = MagicMock()
    sweep.get_record_by_deposit_id = MagicMock(return_value=None)

    service = DepositDiscoveryService(
        accounting_service=accounting,
        sweep_engine=sweep,
        chain_rpc_urls={CHAIN_ID: "http://rpc.invalid"},
    )
    w3 = MagicMock()
    w3.eth.get_block = AsyncMock(return_value={"number": LATEST_BLOCK})
    if logs and isinstance(logs[0], list):
        w3.eth.get_logs = AsyncMock(side_effect=logs)
    else:
        w3.eth.get_logs = AsyncMock(return_value=logs or [])
    service._web3_cache[CHAIN_ID] = w3
    return service


async def _discover(service: DepositDiscoveryService, **overrides):
    params = {
        "deposit_address": DEPOSIT_ADDRESS,
        "beneficiary": BENEFICIARY,
        "chain_id": CHAIN_ID,
        "version": 0,
    }
    params.update(overrides)
    return await service.discover_pending_deposits(**params)


async def test_discovers_uncredited_transfer():
    service = _make_service(logs=[_log("ab", 99_000, log_index=3)])
    result = await _discover(service)

    assert result.scanned_from_block == FROM_BLOCK
    assert result.scanned_to_block == TO_BLOCK
    assert len(result.pending) == 1
    candidate = result.pending[0]
    assert candidate.tx_hash == "0x" + "ab" * 32
    assert candidate.log_index == 3
    assert candidate.amount == 5_000_000
    assert candidate.token_address == USDC
    assert candidate.token_id_hex == USDC_TOKEN_ID
    assert candidate.status == "discovered"
    assert candidate.deposit_id_hex is None

    w3 = service._web3_cache[CHAIN_ID]
    call = w3.eth.get_logs.call_args[0][0]
    assert call["fromBlock"] == FROM_BLOCK
    assert call["toBlock"] == TO_BLOCK
    assert call["address"] == [USDC]  # other-chain and native rows excluded
    assert call["topics"][2] == "0x" + "00" * 12 + DEPOSIT_ADDRESS.removeprefix("0x").lower()


async def test_decodes_hexbytes_tx_and_hex_string_data():
    # Live RPCs return transactionHash as HexBytes and data as a hex string,
    # not the bytes/str shapes the other fixtures feed.
    amount = 5_000_000
    log = {
        "transactionHash": HexBytes("0x" + "cd" * 32),
        "logIndex": 1,
        "address": USDC,
        "data": hex(amount),
        "blockNumber": 99_000,
    }
    service = _make_service(logs=[log])
    result = await _discover(service)

    assert len(result.pending) == 1
    assert result.pending[0].tx_hash == "0x" + "cd" * 32
    assert result.pending[0].amount == amount


async def test_cap_keeps_newest_candidates():
    logs = [_log("ab", 98_000 + i, log_index=i) for i in range(MAX_PENDING_CANDIDATES + 5)]
    service = _make_service(logs=logs)
    result = await _discover(service)

    assert len(result.pending) == MAX_PENDING_CANDIDATES
    blocks = [c.block_number for c in result.pending]
    assert blocks == sorted(blocks, reverse=True)
    assert blocks[0] == 98_000 + MAX_PENDING_CANDIDATES + 4  # newest kept
    assert 98_000 not in blocks  # oldest dropped
    # Coverage claim must shrink to what was examined: the oldest returned
    # candidate is block 98_005, so blocks at or below it are not covered.
    assert result.scanned_from_block == 98_005 + 1


async def test_cap_in_newest_block_reports_empty_coverage():
    """Caps tripping inside the newest block yield the documented empty interval.

    scanned_from_block == scanned_to_block + 1 signals that no block was fully
    covered, rather than claiming coverage the scan never performed.
    """
    logs = [_log("ab", TO_BLOCK, log_index=i) for i in range(MAX_PENDING_CANDIDATES + 1)]
    service = _make_service(logs=logs)
    result = await _discover(service)

    assert len(result.pending) == MAX_PENDING_CANDIDATES
    assert result.scanned_from_block == TO_BLOCK + 1
    assert result.scanned_to_block == TO_BLOCK


async def test_classification_bound_shrinks_scanned_window():
    """An early classification-bound exit must not claim the full window."""
    logs = [_log("ab", 98_000 + i, log_index=i) for i in range(MAX_CLASSIFIED_CANDIDATES + 10)]
    service = _make_service(logs=logs)
    service._accounting.is_deposit_processed = AsyncMock(return_value=True)
    result = await _discover(service)

    assert result.pending == []
    # 70 logs at blocks 98_000..98_069, examined newest-first; the 61st entry
    # (block 98_009) hits the bound unclassified, so coverage stops above it.
    assert result.scanned_from_block == 98_009 + 1
    assert result.scanned_to_block == TO_BLOCK


async def test_empty_token_registry_makes_no_rpc_calls():
    service = _make_service(registry=[])
    result = await _discover(service)

    assert result.pending == []
    w3 = service._web3_cache[CHAIN_ID]
    w3.eth.get_block.assert_not_called()
    w3.eth.get_logs.assert_not_called()


async def test_dust_transfer_dropped():
    service = _make_service(logs=[_log("ab", 99_000, amount=999_999)])
    result = await _discover(service)
    assert result.pending == []


async def test_processed_deposit_dropped():
    service = _make_service(logs=[_log("ab", 99_000)])
    service._accounting.is_deposit_processed = AsyncMock(return_value=True)
    result = await _discover(service)
    assert result.pending == []


async def test_in_flight_sweep_reported_as_processing():
    service = _make_service(logs=[_log("ab", 99_000, log_index=2)])
    record = MagicMock()
    record.beneficiary = BENEFICIARY
    record.error = None
    service._sweep.get_record_by_deposit_id = MagicMock(return_value=record)
    result = await _discover(service)

    expected_deposit_id = (
        "0x"
        + compute_deposit_id(CHAIN_ID, "0x" + "ab" * 32, bytes.fromhex(USDC_TOKEN_ID[2:]), 2).hex()
    )
    assert len(result.pending) == 1
    assert result.pending[0].status == "processing"
    assert result.pending[0].deposit_id_hex == expected_deposit_id
    # The on-chain processed check is skipped for in-flight records
    service._accounting.is_deposit_processed.assert_not_called()


async def test_errored_retryable_record_reported_as_discovered():
    service = _make_service(logs=[_log("ab", 99_000)])
    record = MagicMock()
    record.beneficiary = BENEFICIARY
    record.error = "gas estimation failed"
    record.sweep_tx_hash = None
    service._sweep.get_record_by_deposit_id = MagicMock(return_value=record)
    result = await _discover(service)

    assert len(result.pending) == 1
    assert result.pending[0].status == "discovered"
    assert result.pending[0].deposit_id_hex is None

    # Once a sweep tx was broadcast the record is preserved — still processing
    record.sweep_tx_hash = "0x" + "ee" * 32
    service._scan_cache.clear()
    result = await _discover(service)
    assert result.pending[0].status == "processing"


async def test_credited_transfers_do_not_crowd_out_older_uncredited():
    logs = [_log("ab", 98_000 + i, log_index=i) for i in range(MAX_PENDING_CANDIDATES + 1)]
    service = _make_service(logs=logs)
    # Newest 20 already credited; the oldest is still uncredited
    service._accounting.is_deposit_processed = AsyncMock(
        side_effect=[True] * MAX_PENDING_CANDIDATES + [False]
    )
    result = await _discover(service)

    assert len(result.pending) == 1
    assert result.pending[0].block_number == 98_000
    assert result.pending[0].status == "discovered"


async def test_classification_bound_stops_scan():
    logs = [_log("ab", 98_000 + i, log_index=i) for i in range(MAX_CLASSIFIED_CANDIDATES + 10)]
    service = _make_service(logs=logs)
    service._accounting.is_deposit_processed = AsyncMock(return_value=True)
    result = await _discover(service)

    assert result.pending == []
    assert service._accounting.is_deposit_processed.await_count == MAX_CLASSIFIED_CANDIDATES


async def test_in_flight_record_with_mismatched_beneficiary_is_hidden():
    service = _make_service(logs=[_log("ab", 99_000)])
    record = MagicMock()
    record.beneficiary = "0x" + "cc" * 20
    service._sweep.get_record_by_deposit_id = MagicMock(return_value=record)
    result = await _discover(service)
    assert result.pending == []


async def test_token_filter_narrows_scan_and_rejects_unregistered():
    service = _make_service(logs=[])
    await _discover(service, token_address=USDC)
    call = service._web3_cache[CHAIN_ID].eth.get_logs.call_args[0][0]
    assert call["address"] == [USDC]

    with pytest.raises(ValueError, match="not a registered token"):
        await _discover(service, token_address=OTHER_TOKEN)


async def test_scan_chunks_descending():
    # Explicit lookbacks quantize up to full chunks: 12_000 → 15_000, 3 chunks
    service = _make_service(logs=[[], [], []])
    result = await _discover(service, lookback_blocks=12_000)

    w3 = service._web3_cache[CHAIN_ID]
    ranges = [(c[0][0]["fromBlock"], c[0][0]["toBlock"]) for c in w3.eth.get_logs.call_args_list]
    from_block = TO_BLOCK - 15_000 + 1
    assert ranges == [
        (TO_BLOCK - 5_000 + 1, TO_BLOCK),
        (TO_BLOCK - 10_000 + 1, TO_BLOCK - 5_000),
        (from_block, TO_BLOCK - 10_000),
    ]
    assert result.scanned_from_block == from_block


async def test_multi_chunk_scan_finds_candidate_in_oldest_chunk():
    # lookback 12_000 → 15_000 → three 5_000-block chunks scanned newest-first,
    # with an uncredited transfer in each including the oldest chunk.
    logs = [
        [_log("a1", 99_000, log_index=1)],
        [_log("a2", 90_000, log_index=2)],
        [_log("a3", 85_000, log_index=3)],
    ]
    service = _make_service(logs=logs)
    result = await _discover(service, lookback_blocks=12_000)

    assert len(result.pending) == 3
    assert min(c.block_number for c in result.pending) == 85_000  # oldest chunk scanned
    assert result.scanned_from_block == TO_BLOCK - 15_000 + 1
    assert service._web3_cache[CHAIN_ID].eth.get_logs.call_count == 3


async def test_pending_cap_mid_scan_leaves_older_chunk_unfetched():
    # 21 uncredited transfers in the newest chunk trip the pending cap before
    # the older chunks are reached, so their getLogs calls never happen.
    newest_chunk = [_log("ab", 98_000 + i, log_index=i) for i in range(MAX_PENDING_CANDIDATES + 1)]
    older_chunk = [_log("cd", 85_000, log_index=0)]
    service = _make_service(logs=[newest_chunk, older_chunk, older_chunk])
    result = await _discover(service, lookback_blocks=12_000)

    assert len(result.pending) == MAX_PENDING_CANDIDATES
    assert service._web3_cache[CHAIN_ID].eth.get_logs.call_count == 1
    # The 20th kept candidate is block 98_001, so coverage stops just above it.
    assert result.scanned_from_block == 98_001 + 1


async def test_classification_bound_mid_scan_leaves_older_chunk_unfetched():
    # 65 already-credited transfers in the newest chunk exhaust the
    # classification bound before the older chunks are fetched.
    newest_chunk = [
        _log("ab", 98_000 + i, log_index=i) for i in range(MAX_CLASSIFIED_CANDIDATES + 5)
    ]
    older_chunk = [_log("cd", 85_000, log_index=0)]
    service = _make_service(logs=[newest_chunk, older_chunk, older_chunk])
    service._accounting.is_deposit_processed = AsyncMock(return_value=True)
    result = await _discover(service, lookback_blocks=12_000)

    assert result.pending == []
    assert service._web3_cache[CHAIN_ID].eth.get_logs.call_count == 1
    # The 61st entry (block 98_004) hits the bound unclassified.
    assert result.scanned_from_block == 98_004 + 1


async def test_lookback_quantized_to_chunk_multiples():
    service = _make_service(logs=[])
    result = await _discover(service, lookback_blocks=1)
    assert result.scanned_from_block == TO_BLOCK - 5_000 + 1  # one full chunk


async def test_lookback_clamped_to_chain_maximum():
    service = _make_service(logs=[[] for _ in range(9)])
    result = await _discover(service, lookback_blocks=999_999)
    assert result.scanned_from_block == TO_BLOCK - 43_200 + 1  # chain max, ~24h


async def test_scan_results_cached():
    service = _make_service(logs=[_log("ab", 99_000)])
    first = await _discover(service)
    second = await _discover(service)
    assert first is second
    assert service._web3_cache[CHAIN_ID].eth.get_logs.call_count == 1


async def test_rpc_failure_raises_discovery_error():
    service = _make_service()
    service._web3_cache[CHAIN_ID].eth.get_block = AsyncMock(side_effect=OSError("conn refused"))
    with pytest.raises(DiscoveryRPCError):
        await _discover(service)


async def test_provider_error_raises_discovery_error():
    # Neither is an OSError subclass — see _rpc
    for error in (ClientError("429 Too Many Requests"), Web3RPCError("query limit exceeded")):
        service = _make_service()
        service._web3_cache[CHAIN_ID].eth.get_logs = AsyncMock(side_effect=error)
        with pytest.raises(DiscoveryRPCError):
            await _discover(service)


async def test_unsupported_chain_rejected():
    service = _make_service()
    with pytest.raises(ValueError, match="Unsupported chain_id"):
        await _discover(service, chain_id=999_999)
