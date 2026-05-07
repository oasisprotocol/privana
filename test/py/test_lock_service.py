"""Unit tests for lock-related AccountingContractService nonce getters and validation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from src.clients.rofl import RoflSubmissionResult
from src.services.accounting_contract import AccountingContractService, TokenContext

USER = "0xaBcDef1234567890AbCDeF1234567890aBcDeF12"
SERVICE = "0x1111111111111111111111111111111111111111"
TEST_EIP712_DOMAIN = {
    "name": "AccountingModule",
    "version": "1",
    "chainId": 23295,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}


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


def _setup_user_nonce(service_obj, fn_name, expected_nonce=0):
    return _mock_reader(service_obj, fn_name, expected_nonce)


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


class TestEip712SignerRecovery:
    @pytest.mark.asyncio
    async def test_recovers_signer_from_typed_data(self, service):
        account = Account.from_key("0x" + "11" * 32)
        service._eip712_domain = TEST_EIP712_DOMAIN
        message_types = [
            {"name": "tokenId", "type": "bytes32"},
            {"name": "amount", "type": "uint256"},
            {"name": "nonce", "type": "uint256"},
        ]
        message = {
            "tokenId": "0x" + "aa" * 32,
            "amount": 1000,
            "nonce": 0,
        }
        signable = encode_typed_data(
            domain_data=TEST_EIP712_DOMAIN,
            message_types={"Withdraw": message_types},
            message_data=message,
        )
        signed = Account.sign_message(signable, account.key)

        signer = await service._recover_eip712_signer(
            "Withdraw", message_types, message, signed.signature
        )

        assert signer == account.address


class TestLockFundsSubmission:
    @staticmethod
    def _make_payload(nonce=0):
        return {
            "service_address": SERVICE,
            "token_id": "0x" + "aa" * 32,
            "amount": 1000,
            "expiry": 9999999999,
            "nonce": nonce,
            "signature": "0x" + "ab" * 65,
        }

    @pytest.mark.asyncio
    async def test_rejects_when_recovered_signer_nonce_does_not_match(self, service):
        service._recover_eip712_signer = AsyncMock(return_value=USER)
        _setup_user_nonce(service, "createLockNonces", expected_nonce=5)
        with pytest.raises(ValueError, match="nonce mismatch"):
            await service.lock_funds(self._make_payload(nonce=0))

    @pytest.mark.asyncio
    async def test_submits_when_recovered_signer_nonce_matches(self, service):
        service._recover_eip712_signer = AsyncMock(return_value=USER)
        _setup_user_nonce(service, "createLockNonces", expected_nonce=0)
        service._submit = AsyncMock(return_value=MagicMock(submission_id="0xabc"))
        await service.lock_funds(self._make_payload(nonce=0))
        service._submit.assert_called_once()


class TestModifyLockSubmission:
    @staticmethod
    def _make_payload(nonce=0):
        return {
            "lock_id": 1,
            "amount": 500,
            "new_expiry": 9999999999,
            "nonce": nonce,
            "signature": "0x" + "ab" * 65,
        }

    @pytest.mark.asyncio
    async def test_rejects_when_recovered_signer_nonce_does_not_match(self, service):
        service._recover_eip712_signer = AsyncMock(return_value=USER)
        _setup_user_nonce(service, "modifyLockNonces", expected_nonce=3)
        with pytest.raises(ValueError, match="nonce mismatch"):
            await service.modify_lock(self._make_payload(nonce=0))

    @pytest.mark.asyncio
    async def test_submits_when_recovered_signer_nonce_matches(self, service):
        service._recover_eip712_signer = AsyncMock(return_value=USER)
        _setup_user_nonce(service, "modifyLockNonces", expected_nonce=0)
        service._submit = AsyncMock(return_value=MagicMock(submission_id="0xabc"))
        await service.modify_lock(self._make_payload(nonce=0))
        service._submit.assert_called_once()


class TestTransferFundsSubmission:
    @staticmethod
    def _make_payload(nonce=0):
        return {
            "to_address": USER,
            "token_id": "0x" + "aa" * 32,
            "amount": 1000,
            "nonce": nonce,
            "signature": "0x" + "ab" * 65,
        }

    @pytest.mark.asyncio
    async def test_rejects_when_recovered_signer_nonce_does_not_match(self, service):
        service._recover_eip712_signer = AsyncMock(return_value=USER)
        _setup_user_nonce(service, "transferNonces", expected_nonce=2)
        with pytest.raises(ValueError, match="nonce mismatch"):
            await service.transfer_funds(self._make_payload(nonce=0))

    @pytest.mark.asyncio
    async def test_submits_when_recovered_signer_nonce_matches(self, service):
        service._recover_eip712_signer = AsyncMock(return_value=USER)
        _setup_user_nonce(service, "transferNonces", expected_nonce=0)
        service._submit = AsyncMock(return_value=MagicMock(submission_id="0xabc"))
        await service.transfer_funds(self._make_payload(nonce=0))
        service._submit.assert_called_once()


class TestRequestWithdrawalSubmission:
    @staticmethod
    def _make_payload(nonce=0):
        return {
            "token_id": "0x" + "aa" * 32,
            "amount": 1000,
            "nonce": nonce,
            "signature": "0x" + "ab" * 65,
        }

    @pytest.mark.asyncio
    async def test_rejects_when_recovered_signer_nonce_does_not_match(self, service):
        service._recover_eip712_signer = AsyncMock(return_value=USER)
        _setup_user_nonce(service, "withdrawalNonces", expected_nonce=4)
        with pytest.raises(ValueError, match="nonce mismatch"):
            await service.request_withdrawal(self._make_payload(nonce=0))

    @pytest.mark.asyncio
    async def test_submits_when_recovered_signer_nonce_matches(self, service):
        service._recover_eip712_signer = AsyncMock(return_value=USER)
        _setup_user_nonce(service, "withdrawalNonces", expected_nonce=0)
        service.settings.chain_rpc_urls = {84532: "https://example"}
        service._get_token_context = AsyncMock(
            return_value=TokenContext(chain_id=84532, token_address=None, is_native=True)
        )
        service._check_destination_balance = AsyncMock(return_value=None)
        service.rofl_client.submit_tx = AsyncMock(
            return_value=RoflSubmissionResult(submission_id="0xabc", ok_payload=None)
        )

        await service.request_withdrawal(self._make_payload(nonce=0))

        service.rofl_client.submit_tx.assert_awaited_once()


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
