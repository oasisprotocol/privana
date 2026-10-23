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
from src.services.accounting_store import AccountingStore

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
    """Return a transfer-locked payload signed by `signer` and its record key."""
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
    digest = signed.message_hash.to_0x_hex()
    return payload, f"{SERVICE_ACCOUNT.address}:{digest}"


class TestTransferLockedFunds:
    @pytest.fixture
    def settle_service(self, service, tmp_path):
        service._eip712_domain = TEST_EIP712_DOMAIN
        service._accounting_store = AccountingStore(str(tmp_path))
        _mock_reader(service, "transferLockedNonces", 0)
        return service

    @pytest.mark.asyncio
    async def test_rejects_when_nonce_does_not_match(self, settle_service):
        _mock_reader(settle_service, "transferLockedNonces", 5)
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
        payload, key = _signed_transfer_locked()

        result = await settle_service.transfer_locked_funds(payload)

        assert result.submission_id == "0xabc"
        assert settle_service._accounting_store.get(key).status == "executed"
        assert not settle_service._settle_tasks

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

    # The first result is unseen in the join tests, so a resend that doesn't join submits again.
    @pytest.mark.asyncio
    async def test_resend_in_flight_joins_the_first(self, settle_service):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_submit(_data):
            started.set()
            await release.wait()
            raise RuntimeError("appd timed out")

        settle_service._submit = AsyncMock(side_effect=slow_submit)
        payload, _ = _signed_transfer_locked()

        first = asyncio.create_task(settle_service.transfer_locked_funds(payload))
        await started.wait()
        second = asyncio.create_task(settle_service.transfer_locked_funds(payload))
        release.set()

        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(r, TransferOutcomeUnknownError) for r in results)
        settle_service._submit.assert_called_once()

    @pytest.mark.asyncio
    async def test_resend_during_nonce_read_joins_the_first(self, settle_service):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_nonce():
            started.set()
            await release.wait()
            return 0

        reader = _mock_reader(settle_service, "transferLockedNonces", 0)
        nonce_call = reader.functions.transferLockedNonces.return_value.call
        nonce_call.side_effect = slow_nonce
        settle_service._submit = AsyncMock(side_effect=RuntimeError("appd timed out"))
        payload, _ = _signed_transfer_locked()

        first = asyncio.create_task(settle_service.transfer_locked_funds(payload))
        await started.wait()
        second = asyncio.create_task(settle_service.transfer_locked_funds(payload))
        release.set()

        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(r, TransferOutcomeUnknownError) for r in results)
        nonce_call.assert_awaited_once()
        settle_service._submit.assert_called_once()

    @pytest.mark.asyncio
    async def test_dropped_caller_still_records_outcome(self, settle_service):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_submit(_data):
            started.set()
            await release.wait()
            return SubmissionResult(submission_id="0xabc", status="submitted")

        settle_service._submit = AsyncMock(side_effect=slow_submit)
        payload, key = _signed_transfer_locked()

        caller = asyncio.create_task(settle_service.transfer_locked_funds(payload))
        await started.wait()
        submission = settle_service._settle_tasks[key]
        caller.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await submission

        assert settle_service._accounting_store.get(key).status == "executed"

    @pytest.mark.asyncio
    async def test_first_submission_revert_is_final(self, settle_service):
        settle_service._submit = AsyncMock(
            side_effect=TransactionRevertedError("Transaction reverted: InsufficientLockedAmount")
        )
        payload, key = _signed_transfer_locked()

        for _ in range(2):
            with pytest.raises(TransactionRevertedError):
                await settle_service.transfer_locked_funds(payload)

        assert settle_service._accounting_store.get(key) is None
        assert settle_service._submit.call_count == 2

    @pytest.mark.asyncio
    async def test_resubmission_revert_after_unseen_submission_is_unknown(self, settle_service):
        settle_service._submit = AsyncMock(
            side_effect=[
                RuntimeError("appd timed out"),
                TransactionRevertedError("Transaction reverted: InvalidNonce"),
            ]
        )
        payload, key = _signed_transfer_locked()
        with pytest.raises(TransferOutcomeUnknownError):
            await settle_service.transfer_locked_funds(payload)

        with pytest.raises(TransferOutcomeUnknownError) as exc_info:
            await settle_service.transfer_locked_funds(payload)

        assert isinstance(exc_info.value.__cause__, TransactionRevertedError)
        assert settle_service._accounting_store.get(key).status == "pending"

    @pytest.mark.asyncio
    async def test_resubmission_after_unseen_submission_can_succeed(self, settle_service):
        settle_service._submit = AsyncMock(
            side_effect=[
                RuntimeError("appd timed out"),
                SubmissionResult(submission_id="0xabc", status="submitted"),
            ]
        )
        payload, key = _signed_transfer_locked()
        with pytest.raises(TransferOutcomeUnknownError):
            await settle_service.transfer_locked_funds(payload)
        assert settle_service._accounting_store.get(key).status == "pending"

        result = await settle_service.transfer_locked_funds(payload)

        assert result.submission_id == "0xabc"
        assert settle_service._accounting_store.get(key).status == "executed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "break_nonce_read",
        [
            lambda s: setattr(
                s, "_get_reader_contract", MagicMock(side_effect=ValueError("RPC unverified"))
            ),
            lambda s: _mock_reader(s, "transferLockedNonces", 1),
        ],
        ids=["read fails", "nonce moved"],
    )
    async def test_unseen_submission_survives_restart(
        self, settle_service, tmp_path, break_nonce_read
    ):
        settle_service._submit = AsyncMock(side_effect=RuntimeError("appd timed out"))
        payload, key = _signed_transfer_locked()
        with pytest.raises(TransferOutcomeUnknownError):
            await settle_service.transfer_locked_funds(payload)
        settle_service._accounting_store = AccountingStore(str(tmp_path))
        break_nonce_read(settle_service)

        with pytest.raises(TransferOutcomeUnknownError) as exc_info:
            await settle_service.transfer_locked_funds(payload)

        assert isinstance(exc_info.value.__cause__, ValueError)
        assert settle_service._accounting_store.get(key).status == "pending"
        settle_service._submit.assert_called_once()

    @pytest.mark.asyncio
    async def test_failure_to_record_pending_prevents_submission(self, settle_service):
        settle_service._accounting_store.put = MagicMock(side_effect=OSError("disk full"))
        settle_service._submit = AsyncMock()
        payload, _ = _signed_transfer_locked()

        with pytest.raises(OSError):
            await settle_service.transfer_locked_funds(payload)

        settle_service._submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_to_record_execution_is_unknown(self, settle_service):
        store = settle_service._accounting_store
        put = store.put

        def put_pending_only(key, call):
            if call.status == "executed":
                raise OSError("disk full")
            put(key, call)

        store.put = put_pending_only
        settle_service._submit = AsyncMock(
            return_value=SubmissionResult(submission_id="0xabc", status="submitted")
        )
        payload, _ = _signed_transfer_locked()

        with pytest.raises(TransferOutcomeUnknownError):
            await settle_service.transfer_locked_funds(payload)


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
