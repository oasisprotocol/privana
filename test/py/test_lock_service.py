"""Unit tests for lock-related AccountingContractService nonce getters and validation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.accounting_contract import AccountingContractService

USER = "0xaBcDef1234567890AbCDeF1234567890aBcDeF12"
SERVICE = "0x1111111111111111111111111111111111111111"


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


def _mock_reader(service_obj, fn_name, return_value):
    mock_reader = MagicMock()
    getattr(mock_reader.functions, fn_name).return_value.call = AsyncMock(return_value=return_value)
    service_obj._get_reader_contract = MagicMock(return_value=mock_reader)
    return mock_reader


class TestGetLockNonce:
    @pytest.mark.asyncio
    async def test_returns_nonce_for_valid_address(self, service):
        _mock_reader(service, "createLockNonces", 7)
        result = await service.get_lock_nonce(USER)
        assert result["nonce"] == 7

    @pytest.mark.asyncio
    async def test_rejects_invalid_address(self, service):
        with pytest.raises(ValueError, match="user_address"):
            await service.get_lock_nonce("not-an-address")

    @pytest.mark.asyncio
    async def test_nonce_zero_is_valid(self, service):
        _mock_reader(service, "createLockNonces", 0)
        result = await service.get_lock_nonce(USER)
        assert result["nonce"] == 0


class TestGetModifyLockNonce:
    @pytest.mark.asyncio
    async def test_returns_nonce_for_valid_address(self, service):
        _mock_reader(service, "modifyLockNonces", 3)
        result = await service.get_modify_lock_nonce(USER)
        assert result["nonce"] == 3

    @pytest.mark.asyncio
    async def test_rejects_invalid_address(self, service):
        with pytest.raises(ValueError, match="user_address"):
            await service.get_modify_lock_nonce("bad")


class TestGetTransferLockedNonce:
    @pytest.mark.asyncio
    async def test_returns_nonce_for_valid_service_address(self, service):
        _mock_reader(service, "transferLockedNonces", 5)
        result = await service.get_transfer_locked_nonce(SERVICE)
        assert result["nonce"] == 5
        assert "service_address" in result

    @pytest.mark.asyncio
    async def test_rejects_invalid_address(self, service):
        with pytest.raises(ValueError, match="service_address"):
            await service.get_transfer_locked_nonce("bad")


class TestLockFundsNonceValidation:
    @staticmethod
    def _make_payload(nonce=0):
        return {
            "user_address": USER,
            "service_address": SERVICE,
            "token_id": "0x" + "aa" * 32,
            "amount": 1000,
            "expiry": 9999999999,
            "nonce": nonce,
            "signature": "0x" + "ab" * 65,
        }

    @staticmethod
    def _setup_reader(service_obj, expected_nonce=0):
        mock_reader = MagicMock()
        mock_reader.functions.createLockNonces.return_value.call = AsyncMock(
            return_value=expected_nonce
        )
        service_obj._get_reader_contract = MagicMock(return_value=mock_reader)

    @pytest.mark.asyncio
    async def test_rejects_when_nonce_does_not_match(self, service):
        self._setup_reader(service, expected_nonce=5)
        with pytest.raises(ValueError, match="nonce mismatch"):
            await service.lock_funds(self._make_payload(nonce=0))

    @pytest.mark.asyncio
    async def test_submits_when_nonce_matches(self, service):
        self._setup_reader(service, expected_nonce=0)
        service._submit = AsyncMock(return_value=MagicMock(submission_id="0xabc"))
        await service.lock_funds(self._make_payload(nonce=0))
        service._submit.assert_called_once()


class TestModifyLockNonceValidation:
    @staticmethod
    def _make_payload(nonce=0):
        return {
            "user_address": USER,
            "lock_id": 1,
            "amount": 500,
            "new_expiry": 9999999999,
            "nonce": nonce,
            "signature": "0x" + "ab" * 65,
        }

    @staticmethod
    def _setup_reader(service_obj, expected_nonce=0):
        mock_reader = MagicMock()
        mock_reader.functions.modifyLockNonces.return_value.call = AsyncMock(
            return_value=expected_nonce
        )
        service_obj._get_reader_contract = MagicMock(return_value=mock_reader)

    @pytest.mark.asyncio
    async def test_rejects_when_nonce_does_not_match(self, service):
        self._setup_reader(service, expected_nonce=3)
        with pytest.raises(ValueError, match="nonce mismatch"):
            await service.modify_lock(self._make_payload(nonce=0))

    @pytest.mark.asyncio
    async def test_submits_when_nonce_matches(self, service):
        self._setup_reader(service, expected_nonce=0)
        service._submit = AsyncMock(return_value=MagicMock(submission_id="0xabc"))
        await service.modify_lock(self._make_payload(nonce=0))
        service._submit.assert_called_once()


class TestTransferLockedFundsNonceValidation:
    @staticmethod
    def _make_payload(nonce=0):
        return {
            "user_address": USER,
            "lock_id": 1,
            "to_address": USER,
            "amount": 1000,
            "service_address": SERVICE,
            "nonce": nonce,
            "signature": "0x" + "ab" * 65,
        }

    @staticmethod
    def _setup_reader(service_obj, expected_nonce=0):
        mock_reader = MagicMock()
        mock_reader.functions.transferLockedNonces.return_value.call = AsyncMock(
            return_value=expected_nonce
        )
        service_obj._get_reader_contract = MagicMock(return_value=mock_reader)

    @pytest.mark.asyncio
    async def test_rejects_when_nonce_does_not_match(self, service):
        self._setup_reader(service, expected_nonce=5)
        with pytest.raises(ValueError, match="nonce mismatch"):
            await service.transfer_locked_funds(self._make_payload(nonce=0))

    @pytest.mark.asyncio
    async def test_submits_when_nonce_matches(self, service):
        self._setup_reader(service, expected_nonce=0)
        service._submit = AsyncMock(return_value=MagicMock(submission_id="0xabc"))
        await service.transfer_locked_funds(self._make_payload(nonce=0))
        service._submit.assert_called_once()
