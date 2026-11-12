from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config.chain_config import TRANSFER_EVENT_TOPIC
from src.services import deposit_funding
from src.services.deposit_funding import (
    FundingTransfer,
    UnsupportedChainError,
    get_funding_transfers,
)

HOLDER = "0x" + "aa" * 20
TOKEN = "0x" + "cc" * 20


def _tx(n: int) -> str:
    return "0x" + f"{n:064x}"


def _transfer(n: int, amount: int, category: str = "external") -> dict:
    return {"hash": _tx(n), "category": category, "rawContract": {"value": hex(amount)}}


def _log(amount: int, log_index: int, to: str = HOLDER, token: str = TOKEN) -> dict:
    return {
        "address": token,
        "topics": [TRANSFER_EVENT_TOPIC, "0x" + "00" * 32, "0x" + "00" * 12 + to[2:]],
        "data": amount.to_bytes(32, "big"),
        "logIndex": log_index,
    }


def _w3(*pages: dict, receipts: dict | None = None) -> MagicMock:
    w3 = MagicMock()
    w3.provider.make_request = AsyncMock(side_effect=[{"result": page} for page in pages])
    w3.eth.get_transaction_receipt = AsyncMock(
        side_effect=lambda tx_hash: {"logs": (receipts or {})[tx_hash]}
    )
    return w3


@pytest.mark.asyncio
async def test_transfers_covering_the_balance_are_returned_oldest_first_with_receipt_log_index():
    # Newest first from Alchemy; the oldest transfer is not needed to cover the balance.
    w3 = _w3(
        {
            "transfers": [
                _transfer(3, 30, "erc20"),
                _transfer(2, 20, "erc20"),
                _transfer(1, 10, "erc20"),
            ]
        },
        receipts={_tx(3): [_log(30, 14)], _tx(2): [_log(20, 7)]},
    )

    found = await get_funding_transfers(w3, HOLDER, TOKEN, balance=50, min_amount=0)

    assert found == [FundingTransfer(_tx(2), 20, 7), FundingTransfer(_tx(3), 30, 14)]
    params = w3.provider.make_request.await_args.args[1][0]
    assert (params["category"], params["contractAddresses"]) == (["erc20"], [TOKEN])


@pytest.mark.asyncio
async def test_equal_transfers_in_one_tx_each_take_their_own_log():
    w3 = _w3(
        {"transfers": [_transfer(1, 5, "erc20"), _transfer(1, 5, "erc20")]},
        receipts={
            _tx(1): [
                _log(5, 3),
                _log(5, 9, to="0x" + "dd" * 20),  # another recipient
                _log(7, 4),  # another amount
                _log(5, 6, token="0x" + "ee" * 20),  # another token
                _log(5, 8),
            ]
        },
    )

    found = await get_funding_transfers(w3, HOLDER, TOKEN, balance=10, min_amount=0)

    assert [t.log_index for t in found] == [3, 8]


@pytest.mark.asyncio
async def test_native_transfers_use_log_index_zero_and_skip_dust():
    w3 = _w3({"transfers": [_transfer(2, 5), _transfer(1, 100)]})

    found = await get_funding_transfers(w3, HOLDER, None, balance=105, min_amount=10)

    assert found == [FundingTransfer(_tx(1), 100, 0)]
    assert w3.provider.make_request.await_args.args[1][0]["category"] == ["external", "internal"]
    w3.eth.get_transaction_receipt.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_transaction_is_claimed_once_by_its_top_level_value_else_its_internal_transfers():
    w3 = _w3(
        {
            "transfers": [
                _transfer(3, 7, "internal"),
                _transfer(3, 5, "internal"),
                _transfer(2, 9, "internal"),
                _transfer(2, 100),
            ]
        }
    )

    found = await get_funding_transfers(w3, HOLDER, None, balance=121, min_amount=0)

    assert found == [FundingTransfer(_tx(2), 100, 0), FundingTransfer(_tx(3), 12, 0)]


@pytest.mark.asyncio
async def test_native_lookup_falls_back_to_external_when_internal_is_not_listed():
    w3 = MagicMock()
    w3.provider.make_request = AsyncMock(
        side_effect=[
            {"error": {"message": "category internal is not supported"}},
            {"result": {"transfers": [_transfer(1, 10)]}},
        ]
    )

    found = await get_funding_transfers(w3, HOLDER, None, balance=10, min_amount=0)

    assert found == [FundingTransfer(_tx(1), 10, 0)]
    assert w3.provider.make_request.await_args.args[1][0]["category"] == ["external"]


@pytest.mark.asyncio
async def test_pages_are_followed_until_the_balance_is_covered():
    w3 = _w3(
        {"transfers": [_transfer(2, 10)], "pageKey": "next"},
        {"transfers": [_transfer(1, 10)]},
    )

    found = await get_funding_transfers(w3, HOLDER, None, balance=20, min_amount=0)

    assert [t.tx_hash for t in found] == [_tx(1), _tx(2)]
    assert w3.provider.make_request.await_args.args[1][0]["pageKey"] == "next"


@pytest.mark.asyncio
async def test_collection_is_capped(monkeypatch):
    monkeypatch.setattr(deposit_funding, "MAX_TRANSFERS", 2)
    w3 = _w3({"transfers": [_transfer(3, 1), _transfer(2, 1), _transfer(1, 1)], "pageKey": "x"})

    found = await get_funding_transfers(w3, HOLDER, None, balance=100, min_amount=0)

    assert [t.tx_hash for t in found] == [_tx(2), _tx(3)]


@pytest.mark.asyncio
async def test_alchemy_errors_are_raised():
    w3 = MagicMock()
    w3.provider.make_request = AsyncMock(
        side_effect=[
            {
                "error": {
                    "message": "EAPIs not enabled on specified network: [HYPERLIQUID_TESTNET]."
                }
            },
            {"error": {"message": "rate limited"}},
        ]
    )

    with pytest.raises(UnsupportedChainError):
        await get_funding_transfers(w3, HOLDER, TOKEN, balance=1, min_amount=0)
    with pytest.raises(RuntimeError, match="rate limited"):
        await get_funding_transfers(w3, HOLDER, TOKEN, balance=1, min_amount=0)
