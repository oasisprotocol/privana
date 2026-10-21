"""Unit tests for lock-related AccountingContractService nonce getters and validation."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from src.clients.rofl import RoflSubmissionResult, TransactionRevertedError
from src.services.accounting_contract import (
    AccountingContractService,
    SubmissionResult,
    TokenContext,
    TransferOutcomeUnknownError,
)
from src.services.operation_store import OperationStore

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


SERVICE_ACCOUNT = Account.from_key("0x" + "22" * 32)
TRANSFER_LOCKED_TYPES = [
    {"name": "userAddress", "type": "address"},
    {"name": "toAddress", "type": "address"},
    {"name": "lockId", "type": "uint256"},
    {"name": "amount", "type": "uint256"},
    {"name": "nonce", "type": "uint256"},
    {"name": "serviceAddress", "type": "address"},
]


def _signed_transfer_locked(nonce=0, signer=SERVICE_ACCOUNT):
    """Return a transfer-locked payload signed by `signer` and its EIP-712 digest."""
    signable = encode_typed_data(
        domain_data=TEST_EIP712_DOMAIN,
        message_types={"TransferLocked": TRANSFER_LOCKED_TYPES},
        message_data={
            "userAddress": USER,
            "toAddress": USER,
            "lockId": 1,
            "amount": 1000,
            "nonce": nonce,
            "serviceAddress": SERVICE_ACCOUNT.address,
        },
    )
    signed = Account.sign_message(signable, signer.key)
    payload = {
        "user_address": USER,
        "lock_id": 1,
        "to_address": USER,
        "amount": 1000,
        "service_address": SERVICE_ACCOUNT.address,
        "nonce": nonce,
        "signature": signed.signature.hex(),
    }
    return payload, "0x" + signed.message_hash.hex().removeprefix("0x")


class TestTransferLockedFunds:
    @pytest.fixture
    def settle_service(self, service, tmp_path):
        service._eip712_domain = TEST_EIP712_DOMAIN
        service._operation_store = OperationStore(str(tmp_path))
        self._set_chain_nonce(service, 0)
        return service

    @staticmethod
    def _set_chain_nonce(service_obj, nonce):
        _mock_reader(service_obj, "transferLockedNonces", nonce)

    @pytest.mark.asyncio
    async def test_rejects_when_nonce_does_not_match(self, settle_service):
        self._set_chain_nonce(settle_service, 5)
        settle_service._submit = AsyncMock()
        payload, _ = _signed_transfer_locked(nonce=0)
        with pytest.raises(ValueError, match="nonce mismatch"):
            await settle_service.transfer_locked_funds(payload)
        settle_service._submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_signature_not_from_service(self, settle_service):
        settle_service._submit = AsyncMock()
        payload, _ = _signed_transfer_locked(signer=Account.from_key("0x" + "33" * 32))
        with pytest.raises(ValueError, match="not from service_address"):
            await settle_service.transfer_locked_funds(payload)
        settle_service._submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_submits_and_records_executed(self, settle_service):
        settle_service._submit = AsyncMock(
            return_value=SubmissionResult(submission_id="0xabc", status="submitted")
        )
        payload, digest = _signed_transfer_locked()

        result = await settle_service.transfer_locked_funds(payload)

        assert result.submission_id == "0xabc"
        assert settle_service._operation_store.get(digest).status == "executed"

    @pytest.mark.asyncio
    async def test_resend_after_execution_replays_without_reading_the_nonce(self, settle_service):
        settle_service._submit = AsyncMock(
            return_value=SubmissionResult(submission_id="0xabc", status="submitted")
        )
        payload, _ = _signed_transfer_locked()
        await settle_service.transfer_locked_funds(payload)
        settle_service._get_reader_contract = MagicMock(side_effect=RuntimeError("RPC down"))

        result = await settle_service.transfer_locked_funds(payload)

        assert result.submission_id == "0xabc"
        settle_service._submit.assert_called_once()

    @pytest.mark.asyncio
    async def test_resend_in_flight_joins_the_first(self, settle_service):
        release = asyncio.Event()

        async def slow_submit(_data):
            await release.wait()
            return SubmissionResult(submission_id="0xabc", status="submitted")

        settle_service._submit = AsyncMock(side_effect=slow_submit)
        payload, _ = _signed_transfer_locked()

        first = asyncio.create_task(settle_service.transfer_locked_funds(payload))
        second = asyncio.create_task(settle_service.transfer_locked_funds(payload))
        for _ in range(3):
            await asyncio.sleep(0)
        release.set()

        results = await asyncio.gather(first, second)
        assert [r.submission_id for r in results] == ["0xabc", "0xabc"]
        settle_service._submit.assert_called_once()

    @pytest.mark.asyncio
    async def test_resend_during_nonce_read_joins_the_first(self, settle_service):
        release = asyncio.Event()

        async def slow_nonce():
            await release.wait()
            return 0

        reader = _mock_reader(settle_service, "transferLockedNonces", 0)
        nonce_call = reader.functions.transferLockedNonces.return_value.call
        nonce_call.side_effect = slow_nonce
        settle_service._submit = AsyncMock(
            return_value=SubmissionResult(submission_id="0xabc", status="submitted")
        )
        payload, _ = _signed_transfer_locked()

        first = asyncio.create_task(settle_service.transfer_locked_funds(payload))
        second = asyncio.create_task(settle_service.transfer_locked_funds(payload))
        for _ in range(3):
            await asyncio.sleep(0)
        release.set()

        results = await asyncio.gather(first, second)
        assert [r.submission_id for r in results] == ["0xabc", "0xabc"]
        nonce_call.assert_awaited_once()
        settle_service._submit.assert_called_once()

    @pytest.mark.asyncio
    async def test_dropped_caller_still_records_outcome(self, settle_service):
        release = asyncio.Event()

        async def slow_submit(_data):
            await release.wait()
            return SubmissionResult(submission_id="0xabc", status="submitted")

        settle_service._submit = AsyncMock(side_effect=slow_submit)
        payload, digest = _signed_transfer_locked()

        caller = asyncio.create_task(settle_service.transfer_locked_funds(payload))
        await asyncio.sleep(0)
        (submission,) = settle_service._settle_tasks.values()
        caller.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await submission

        assert settle_service._operation_store.get(digest).status == "executed"

    @pytest.mark.asyncio
    async def test_first_submission_revert_is_final(self, settle_service):
        settle_service._submit = AsyncMock(
            side_effect=TransactionRevertedError("Transaction reverted: InsufficientLockedAmount")
        )
        payload, digest = _signed_transfer_locked()

        for _ in range(2):
            with pytest.raises(TransactionRevertedError):
                await settle_service.transfer_locked_funds(payload)

        assert settle_service._operation_store.get(digest) is None
        assert settle_service._submit.call_count == 2

    @pytest.mark.asyncio
    async def test_resubmission_revert_after_unseen_submission_is_unknown(self, settle_service):
        settle_service._submit = AsyncMock(
            side_effect=[
                RuntimeError("appd timed out"),
                TransactionRevertedError("Transaction reverted: InvalidNonce"),
            ]
        )
        payload, digest = _signed_transfer_locked()
        with pytest.raises(RuntimeError):
            await settle_service.transfer_locked_funds(payload)

        with pytest.raises(TransferOutcomeUnknownError, match="resubmitting it reverted"):
            await settle_service.transfer_locked_funds(payload)

        assert settle_service._operation_store.get(digest).status == "pending"

    @pytest.mark.asyncio
    async def test_resubmission_after_unseen_submission_can_succeed(self, settle_service):
        settle_service._submit = AsyncMock(
            side_effect=[
                RuntimeError("appd timed out"),
                SubmissionResult(submission_id="0xabc", status="submitted"),
            ]
        )
        payload, digest = _signed_transfer_locked()
        with pytest.raises(RuntimeError):
            await settle_service.transfer_locked_funds(payload)

        result = await settle_service.transfer_locked_funds(payload)

        assert result.submission_id == "0xabc"
        assert settle_service._operation_store.get(digest).status == "executed"

    @pytest.mark.asyncio
    async def test_unseen_submission_survives_restart(self, settle_service, tmp_path):
        settle_service._submit = AsyncMock(side_effect=RuntimeError("appd timed out"))
        payload, _ = _signed_transfer_locked()
        with pytest.raises(RuntimeError):
            await settle_service.transfer_locked_funds(payload)
        settle_service._operation_store = OperationStore(str(tmp_path))
        self._set_chain_nonce(settle_service, 1)

        with pytest.raises(TransferOutcomeUnknownError, match="nonce is now 1"):
            await settle_service.transfer_locked_funds(payload)

        settle_service._submit.assert_called_once()

    @pytest.mark.asyncio
    async def test_failure_to_record_pending_prevents_submission(self, settle_service):
        settle_service._operation_store.put = MagicMock(side_effect=OSError("disk full"))
        settle_service._submit = AsyncMock()
        payload, _ = _signed_transfer_locked()

        with pytest.raises(OSError):
            await settle_service.transfer_locked_funds(payload)

        settle_service._submit.assert_not_called()


class TestGetTransferToLockNonce:
    @pytest.mark.asyncio
    async def test_returns_nonce_for_valid_service_address(self, service):
        _mock_reader(service, "transferToLockNonces", 9)
        result = await service.get_transfer_to_lock_nonce(SERVICE)
        assert result["nonce"] == 9
        assert "service_address" in result

    @pytest.mark.asyncio
    async def test_rejects_invalid_address(self, service):
        with pytest.raises(ValueError, match="service_address"):
            await service.get_transfer_to_lock_nonce("bad")


class TestTransferToLockSubmission:
    @staticmethod
    def _make_payload(nonce=0):
        return {
            "user_address": USER,
            "lock_id": 1,
            "amount": 1000,
            "nonce": nonce,
            "signature": "0x" + "ab" * 65,
        }

    @pytest.mark.asyncio
    async def test_rejects_when_recovered_signer_nonce_does_not_match(self, service):
        service._recover_eip712_signer = AsyncMock(return_value=SERVICE)
        _setup_user_nonce(service, "transferToLockNonces", expected_nonce=5)
        with pytest.raises(ValueError, match="nonce mismatch"):
            await service.transfer_to_lock(self._make_payload(nonce=0))

    @pytest.mark.asyncio
    async def test_submits_when_recovered_signer_nonce_matches(self, service):
        service._recover_eip712_signer = AsyncMock(return_value=SERVICE)
        _setup_user_nonce(service, "transferToLockNonces", expected_nonce=0)
        service._submit = AsyncMock(return_value=MagicMock(submission_id="0xabc"))
        await service.transfer_to_lock(self._make_payload(nonce=0))
        service._submit.assert_called_once()
