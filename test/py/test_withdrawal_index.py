"""Unit tests for the post-submit withdrawal index lookup."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hexbytes import HexBytes

from src.services.accounting_contract import AccountingContractService

USER = "0xaBcDef1234567890AbCDeF1234567890aBcDeF12"
OTHER = "0x2222222222222222222222222222222222222222"
TOKEN = HexBytes("0x" + "11" * 32)
OTHER_TOKEN = HexBytes("0x" + "22" * 32)
AMOUNT = 2_000_000


@pytest.fixture
def service():
    with (
        patch("src.services.accounting_contract.load_settings") as mock_settings,
        patch("src.services.accounting_contract.RoflAppdClient"),
    ):
        mock_settings.return_value = MagicMock(
            accounting_contract_address="0x" + "00" * 20,
            sapphire_rpc_url="",
            sapphire_chain_id=23295,
            accounting_gas_limit=500_000,
            chain_rpc_urls=[],
        )
        yield AccountingContractService()


def _withdrawal(user: str, amount: int, token: HexBytes, resolved: bool = False) -> tuple:
    return (user, user, amount, 100, bytes(token), resolved, b"")


def _mock_withdrawals(service_obj, entries: list[tuple]) -> None:
    mock_reader = MagicMock()
    mock_reader.functions.withdrawalCount.return_value.call = AsyncMock(return_value=len(entries))

    def withdrawals(index: int):
        call = MagicMock()
        call.call = AsyncMock(return_value=entries[index])
        return call

    mock_reader.functions.withdrawals.side_effect = withdrawals
    service_obj._get_reader_contract = MagicMock(return_value=mock_reader)


@pytest.mark.asyncio
async def test_finds_newest_matching_withdrawal(service):
    _mock_withdrawals(
        service,
        [
            _withdrawal(USER, AMOUNT, TOKEN),  # older identical request
            _withdrawal(OTHER, AMOUNT, TOKEN),
            _withdrawal(USER, AMOUNT, TOKEN),  # the one just submitted
        ],
    )
    assert await service._locate_withdrawal_index(USER, TOKEN, AMOUNT) == 2


@pytest.mark.asyncio
async def test_skips_other_users_and_tokens(service):
    _mock_withdrawals(
        service,
        [
            _withdrawal(USER, AMOUNT, TOKEN),
            _withdrawal(USER, AMOUNT, OTHER_TOKEN),
            _withdrawal(OTHER, AMOUNT, TOKEN),
        ],
    )
    assert await service._locate_withdrawal_index(USER, TOKEN, AMOUNT) == 0


@pytest.mark.asyncio
async def test_matches_case_insensitively(service):
    _mock_withdrawals(service, [_withdrawal(USER.lower(), AMOUNT, TOKEN)])
    assert await service._locate_withdrawal_index(USER, TOKEN, AMOUNT) == 0


@pytest.mark.asyncio
async def test_returns_none_when_no_match(service):
    _mock_withdrawals(service, [_withdrawal(OTHER, AMOUNT, TOKEN)])
    assert await service._locate_withdrawal_index(USER, TOKEN, AMOUNT) is None


@pytest.mark.asyncio
async def test_returns_none_on_read_failure(service):
    mock_reader = MagicMock()
    mock_reader.functions.withdrawalCount.return_value.call = AsyncMock(
        side_effect=RuntimeError("rpc down")
    )
    service._get_reader_contract = MagicMock(return_value=mock_reader)
    assert await service._locate_withdrawal_index(USER, TOKEN, AMOUNT) is None


@pytest.mark.asyncio
async def test_scan_stays_within_window(service):
    # 20 entries, match sits below the 16-entry window: must NOT be found.
    entries = [_withdrawal(OTHER, AMOUNT, TOKEN) for _ in range(20)]
    entries[2] = _withdrawal(USER, AMOUNT, TOKEN)
    _mock_withdrawals(service, entries)
    assert await service._locate_withdrawal_index(USER, TOKEN, AMOUNT) is None
