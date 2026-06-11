"""Tests for AccountingContractService parsing and request validation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from hexbytes import HexBytes
from web3 import Web3

from src.clients.rofl import RoflSubmissionResult
from src.models.accounting import HistoryKind
from src.services.accounting_contract import AccountingContractService


def _make_service_with_reader(reader: MagicMock) -> AccountingContractService:
    service = AccountingContractService.__new__(AccountingContractService)
    service.contract_reader = reader
    return service


@pytest.mark.asyncio
async def test_get_withdrawal_parses_new_tuple_shape_with_to_address() -> None:
    user = "0x1234567890123456789012345678901234567890"
    to_address = "0x9876543210987654321098765432109876543210"
    token = bytes.fromhex("11" * 32)
    tx_identifier = b"\x12\x34"

    reader = MagicMock()
    reader.functions.withdrawals.return_value.call = AsyncMock(
        return_value=(
            user,
            to_address,
            42,
            777,
            token,
            False,
            tx_identifier,
        )
    )

    service = _make_service_with_reader(reader)
    parsed = await service.get_withdrawal(3)

    assert parsed["index"] == 3
    assert parsed["user_address"] == user
    assert parsed["to_address"] == to_address
    assert parsed["amount"] == "42"
    assert parsed["block_number"] == 777
    assert parsed["token_id"] == "0x" + ("11" * 32)
    assert parsed["resolved"] is False
    assert parsed["tx_identifier"] == "0x1234"


@pytest.mark.asyncio
async def test_get_pending_withdrawals_includes_to_address() -> None:
    user = "0x1234567890123456789012345678901234567890"
    to_address = "0x9876543210987654321098765432109876543210"
    token = bytes.fromhex("22" * 32)
    tx_identifier = b"\x56\x78"

    reader = MagicMock()

    def _withdrawals(index: int):
        result = MagicMock()
        if index == 0:
            result.call = AsyncMock(
                return_value=(
                    user,
                    to_address,
                    99,
                    1234,
                    token,
                    False,
                    tx_identifier,
                )
            )
        else:
            result.call = AsyncMock(side_effect=Exception("end"))
        return result

    reader.functions.withdrawals.side_effect = _withdrawals

    service = _make_service_with_reader(reader)
    parsed = await service.get_pending_withdrawals(user)

    assert parsed["user_address"] == user
    assert len(parsed["pending_withdrawals"]) == 1
    pending = parsed["pending_withdrawals"][0]
    assert pending["to_address"] == to_address
    assert pending["amount"] == "99"
    assert pending["token_id"] == "0x" + ("22" * 32)
    assert pending["resolved"] is False


USER_A = "0x1234567890123456789012345678901234567890"
USER_B = "0x9876543210987654321098765432109876543210"
USER_C = "0x5555555555555555555555555555555555555555"


def _history_amount(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _history_payload(token_id: bytes, amount: int, tail: bytes) -> bytes:
    return token_id + _history_amount(amount) + tail


def _history_pair_payload(
    token_id: bytes, amount: int, from_address: str, to_address: str
) -> bytes:
    return (
        token_id
        + _history_amount(amount)
        + bytes(HexBytes(from_address))
        + bytes(HexBytes(to_address))
    )


@pytest.mark.asyncio
async def test_get_history_parses_contract_entries() -> None:
    reader = MagicMock()
    deposit_id = bytes.fromhex("dd" * 32)
    destination = bytes.fromhex("12" * 20)
    reader.functions.getHistory.return_value.call = AsyncMock(
        return_value=(
            [
                (
                    0,
                    1710000000,
                    _history_payload(bytes.fromhex("33" * 32), 123, deposit_id),
                ),
                (
                    4,
                    1710000001,
                    _history_payload(bytes.fromhex("44" * 32), 456, destination),
                ),
            ],
            9,
        )
    )

    service = AccountingContractService.__new__(AccountingContractService)
    service._get_confidential_reader_contract = AsyncMock(return_value=reader)
    service._get_token_context = AsyncMock(return_value=SimpleNamespace(chain_id=84532))

    parsed = await service.get_history(2, 5, b"\x12\x34", USER_A)

    assert parsed["total"] == 9
    assert parsed["history"][0] == {
        "kind": "deposit",
        "timestamp": 1710000000,
        "token_id": "0x" + ("33" * 32),
        "amount": "123",
        "counterparty": None,
        "from_address": None,
        "to_address": None,
        "deposit_id": "0x" + ("dd" * 32),
        "chain_id": 84532,
    }
    assert parsed["history"][1] == {
        "kind": "transferBalance",
        "timestamp": 1710000001,
        "token_id": "0x" + ("44" * 32),
        "amount": "456",
        "counterparty": Web3.to_checksum_address("0x" + ("12" * 20)),
        "from_address": None,
        "to_address": None,
        "deposit_id": None,
        "chain_id": 84532,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "kind_name"),
    [
        (1, "withdraw"),
        (2, "createLock"),
        (3, "transferFromLock"),
        (4, "transferBalance"),
        (5, "modifyLock"),
        (6, "unlockLock"),
    ],
)
async def test_history_entry_decodes_address_payload_kinds(kind: int, kind_name: str) -> None:
    service = AccountingContractService.__new__(AccountingContractService)
    service._get_token_context = AsyncMock(return_value=SimpleNamespace(chain_id=84532))

    parsed = await service._history_entry_to_dict(
        (
            kind,
            1710000002,
            _history_payload(bytes.fromhex("55" * 32), 789, bytes.fromhex("ab" * 20)),
        ),
        USER_A,
    )

    assert parsed == {
        "kind": kind_name,
        "timestamp": 1710000002,
        "token_id": "0x" + ("55" * 32),
        "amount": "789",
        "counterparty": Web3.to_checksum_address("0x" + ("ab" * 20)),
        "from_address": None,
        "to_address": None,
        "deposit_id": None,
        "chain_id": 84532,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "kind_name"),
    [
        (3, "transferFromLock"),
        (4, "transferBalance"),
    ],
)
@pytest.mark.parametrize(
    ("owner", "expected_counterparty"),
    [
        (USER_A, USER_B),
        (USER_B, USER_A),
    ],
)
async def test_history_entry_decodes_paired_transfer_payload_relative_to_owner(
    kind: int, kind_name: str, owner: str, expected_counterparty: str
) -> None:
    service = AccountingContractService.__new__(AccountingContractService)
    service._get_token_context = AsyncMock(return_value=SimpleNamespace(chain_id=84532))

    parsed = await service._history_entry_to_dict(
        (
            kind,
            1710000004,
            _history_pair_payload(bytes.fromhex("99" * 32), 654, USER_A, USER_B),
        ),
        owner,
    )

    assert parsed == {
        "kind": kind_name,
        "timestamp": 1710000004,
        "token_id": "0x" + ("99" * 32),
        "amount": "654",
        "counterparty": Web3.to_checksum_address(expected_counterparty),
        "from_address": Web3.to_checksum_address(USER_A),
        "to_address": Web3.to_checksum_address(USER_B),
        "deposit_id": None,
        "chain_id": 84532,
    }


@pytest.mark.asyncio
async def test_history_entry_keeps_unknown_counterparty_when_owner_is_not_in_pair() -> None:
    service = AccountingContractService.__new__(AccountingContractService)
    service._get_token_context = AsyncMock(return_value=SimpleNamespace(chain_id=84532))

    parsed = await service._history_entry_to_dict(
        (
            4,
            1710000004,
            _history_pair_payload(bytes.fromhex("99" * 32), 654, USER_A, USER_B),
        ),
        USER_C,
    )

    assert parsed["counterparty"] is None
    assert parsed["from_address"] == Web3.to_checksum_address(USER_A)
    assert parsed["to_address"] == Web3.to_checksum_address(USER_B)


@pytest.mark.parametrize(
    ("kind", "payload", "match"),
    [
        (HistoryKind.Deposit, b"\x00", "must be 96 bytes"),
        (
            HistoryKind.Withdraw,
            bytes.fromhex("77" * 32) + _history_amount(1) + bytes.fromhex("cd" * 19),
            "must be 84 bytes",
        ),
        (
            HistoryKind.TransferBalance,
            bytes.fromhex("77" * 32) + _history_amount(1) + bytes.fromhex("cd" * 21),
            "must be 84 or 104 bytes",
        ),
    ],
)
def test_decode_history_payload_rejects_invalid_shapes(
    kind: HistoryKind, payload: bytes, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        AccountingContractService._decode_history_payload(kind, payload, USER_A)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        (0, 1710000000, b"\x00"),
        (
            1,
            1710000001,
            bytes.fromhex("77" * 32) + _history_amount(1) + bytes.fromhex("cd" * 19),
        ),
    ],
)
async def test_history_entry_degrades_for_invalid_payload(entry: tuple) -> None:
    service = AccountingContractService.__new__(AccountingContractService)
    service._get_token_context = AsyncMock(return_value=SimpleNamespace(chain_id=84532))

    parsed = await service._history_entry_to_dict(entry, USER_A)

    assert parsed == {
        "kind": "unknown",
        "timestamp": entry[1],
        "token_id": None,
        "amount": None,
        "counterparty": None,
        "from_address": None,
        "to_address": None,
        "deposit_id": None,
        "chain_id": None,
    }


@pytest.mark.asyncio
async def test_history_entry_degrades_for_unknown_kind() -> None:
    service = AccountingContractService.__new__(AccountingContractService)

    parsed = await service._history_entry_to_dict((99, 1710000002, b""), USER_A)

    assert parsed["kind"] == "unknown"
    assert parsed["timestamp"] == 1710000002
    assert parsed["token_id"] is None
    assert parsed["chain_id"] is None


@pytest.mark.asyncio
async def test_get_history_preserves_page_when_one_entry_is_unknown() -> None:
    reader = MagicMock()
    reader.functions.getHistory.return_value.call = AsyncMock(
        return_value=(
            [
                (
                    0,
                    1710000000,
                    _history_payload(bytes.fromhex("11" * 32), 1, b"\xdd" * 32),
                ),
                (99, 1710000001, b""),
            ],
            2,
        )
    )

    service = AccountingContractService.__new__(AccountingContractService)
    service._get_confidential_reader_contract = AsyncMock(return_value=reader)
    service._get_token_context = AsyncMock(return_value=SimpleNamespace(chain_id=84532))

    parsed = await service.get_history(0, 10, b"\x12\x34", USER_A)

    assert parsed["total"] == 2
    assert parsed["history"][0]["kind"] == "deposit"
    assert parsed["history"][1]["kind"] == "unknown"


@pytest.mark.asyncio
async def test_history_entry_preserves_decoded_payload_without_token_context() -> None:
    service = AccountingContractService.__new__(AccountingContractService)
    service._get_token_context = AsyncMock(side_effect=ValueError("missing token context"))

    parsed = await service._history_entry_to_dict(
        (
            1,
            1710000003,
            _history_payload(bytes.fromhex("66" * 32), 321, bytes.fromhex("12" * 20)),
        ),
        USER_A,
    )

    assert parsed == {
        "kind": "withdraw",
        "timestamp": 1710000003,
        "token_id": "0x" + ("66" * 32),
        "amount": "321",
        "counterparty": Web3.to_checksum_address("0x" + ("12" * 20)),
        "from_address": None,
        "to_address": None,
        "deposit_id": None,
        "chain_id": None,
    }


@pytest.mark.asyncio
async def test_get_history_accepts_negative_offset() -> None:
    reader = MagicMock()
    reader.functions.getHistory.return_value.call = AsyncMock(return_value=([], 9))

    service = AccountingContractService.__new__(AccountingContractService)
    service._get_confidential_reader_contract = AsyncMock(return_value=reader)

    parsed = await service.get_history(-1, 10, b"\x12", USER_A)

    assert parsed == {"history": [], "total": 9}
    reader.functions.getHistory.assert_called_once_with(-1, 10, b"\x12")


@pytest.mark.asyncio
async def test_get_history_rejects_offset_outside_int256() -> None:
    service = AccountingContractService.__new__(AccountingContractService)

    with pytest.raises(ValueError, match="offset must fit int256"):
        await service.get_history(-(2**255) - 1, 10, b"\x12", USER_A)

    with pytest.raises(ValueError, match="offset must fit int256"):
        await service.get_history(2**255, 10, b"\x12", USER_A)


@pytest.mark.asyncio
async def test_get_history_rejects_negative_limit() -> None:
    service = AccountingContractService.__new__(AccountingContractService)

    with pytest.raises(ValueError, match="limit must be >= 0"):
        await service.get_history(0, -1, b"\x12", USER_A)


@pytest.mark.asyncio
async def test_get_history_preserves_empty_pages_and_total() -> None:
    reader = MagicMock()
    reader.functions.getHistory.return_value.call = AsyncMock(return_value=([], 9))

    service = AccountingContractService.__new__(AccountingContractService)
    service._get_confidential_reader_contract = AsyncMock(return_value=reader)

    parsed = await service.get_history(9, 0, b"\x12\x34", USER_A)

    assert parsed == {"history": [], "total": 9}
    reader.functions.getHistory.assert_called_once_with(9, 0, b"\x12\x34")


@pytest.mark.asyncio
async def test_withdraw_from_lock_rejects_zero_to_address() -> None:
    service = AccountingContractService.__new__(AccountingContractService)

    with pytest.raises(ValueError, match="to_address must not be the zero address"):
        await service.withdraw_from_lock(
            {
                "to_address": "0x0000000000000000000000000000000000000000",
                "lock_id": 1,
                "amount": 10,
                "nonce": 0,
                "signature": "0x1234",
            },
            user_address=USER_A,
            siwe_token=b"\x00" * 32,
        )


@pytest.mark.asyncio
async def test_withdraw_from_lock_rejects_missing_lock() -> None:
    service = AccountingContractService.__new__(AccountingContractService)
    service._fetch_user_locks = AsyncMock(return_value=[])

    with pytest.raises(ValueError, match="lock_id 1 not found"):
        await service.withdraw_from_lock(
            {
                "to_address": "0x9876543210987654321098765432109876543210",
                "lock_id": 1,
                "amount": 10,
                "nonce": 0,
                "signature": "0x1234",
            },
            user_address=USER_A,
            siwe_token=b"\x11" * 32,
        )


@pytest.mark.asyncio
async def test_withdraw_from_lock_reads_locks_via_confidential_reader() -> None:
    to_addr = "0x9876543210987654321098765432109876543210"
    lock_id = 7
    token_id = b"\x22" * 32
    siwe_token = b"\x33" * 32
    signature_hex = "0xabcd"

    # Matches FundLock tuple shape: (lock_id, service, token_id, amount, expiry)
    lock_tuple = (lock_id, USER_A, token_id, 500, 9999999999)

    from src.services.accounting_contract import TokenContext

    token_ctx = TokenContext(chain_id=84532, token_address=None, is_native=True)

    contract = MagicMock()
    encoder = MagicMock()
    encoder._encode_transaction_data.return_value = b"\xde\xad\xbe\xef"
    contract.functions.withdrawFromLock.return_value = encoder

    rofl_client = MagicMock()
    rofl_client.submit_tx = AsyncMock(
        return_value=RoflSubmissionResult(submission_id="sub-2", ok_payload=None)
    )

    settings = MagicMock()
    settings.chain_rpc_urls = {84532: "https://example"}

    service = AccountingContractService.__new__(AccountingContractService)
    service.contract = contract
    service.contract_address = "0x" + "11" * 20
    service.gas_limit = 500_000
    service.rofl_client = rofl_client
    service.settings = settings
    service._fetch_user_locks = AsyncMock(return_value=[lock_tuple])
    service._get_token_context = AsyncMock(return_value=token_ctx)
    service._check_destination_balance = AsyncMock(return_value=None)

    result = await service.withdraw_from_lock(
        {
            "to_address": to_addr,
            "lock_id": lock_id,
            "amount": 100,
            "nonce": 0,
            "signature": signature_hex,
        },
        user_address=USER_A,
        siwe_token=siwe_token,
    )

    service._fetch_user_locks.assert_awaited_once_with(siwe_token)
    contract.functions.withdrawFromLock.assert_called_once_with(
        Web3.to_checksum_address(USER_A),
        Web3.to_checksum_address(to_addr),
        lock_id,
        100,
        0,
        HexBytes(signature_hex),
    )
    assert result.submission_id == "sub-2"
    assert "chain_id=84532" in (result.detail or "")


@pytest.mark.asyncio
async def test_get_rofl_signer_address_returns_checksum() -> None:
    lowercase = "0xabababababababababababababababababababab"
    reader = MagicMock()
    reader.functions.roflSignerAddress.return_value.call = AsyncMock(return_value=lowercase)

    service = _make_service_with_reader(reader)
    result = await service.get_rofl_signer_address()

    assert result == Web3.to_checksum_address(lowercase)


@pytest.mark.asyncio
async def test_set_rofl_signer_address_submits_tx_with_checksum_arg() -> None:
    signer = "0xabababababababababababababababababababab"
    calldata = b"\xde\xad\xbe\xef" + b"\x00" * 32

    contract = MagicMock()
    encoder = MagicMock()
    encoder._encode_transaction_data.return_value = calldata
    contract.functions.setRoflSignerAddress.return_value = encoder

    rofl_client = MagicMock()
    rofl_client.submit_tx = AsyncMock(
        return_value=RoflSubmissionResult(submission_id="sub-1", ok_payload=None)
    )

    service = AccountingContractService.__new__(AccountingContractService)
    service.contract = contract
    service.contract_address = "0x" + "11" * 20
    service.gas_limit = 500_000
    service.rofl_client = rofl_client

    result = await service.set_rofl_signer_address(signer)

    contract.functions.setRoflSignerAddress.assert_called_once_with(
        Web3.to_checksum_address(signer)
    )
    rofl_client.submit_tx.assert_awaited_once()
    assert result.submission_id == "sub-1"


@pytest.mark.asyncio
async def test_set_rofl_signer_address_rejects_invalid_address() -> None:
    service = AccountingContractService.__new__(AccountingContractService)

    with pytest.raises(ValueError, match="Invalid new_signer"):
        await service.set_rofl_signer_address("not-an-address")


@pytest.mark.asyncio
async def test_set_auth_token_enc_key_submits_encrypted_tx() -> None:
    auth_address = "0x2222222222222222222222222222222222222222"
    enc_key = bytes.fromhex("11" * 32)

    rofl_client = MagicMock()
    rofl_client.submit_tx = AsyncMock(
        return_value=RoflSubmissionResult(submission_id="sub-1", ok_payload=None)
    )

    service = AccountingContractService.__new__(AccountingContractService)
    service.gas_limit = 500_000
    service.rofl_client = rofl_client
    service._get_siwe_auth_address = AsyncMock(return_value=auth_address)

    await service.set_auth_token_enc_key(enc_key)

    selector = Web3.keccak(text="setAuthTokenEncKey(bytes32)")[:4]
    expected_tx = {
        "to": auth_address,
        "value": 0,
        "gas": 500_000,
        "data": Web3.to_hex(selector + enc_key),
    }
    rofl_client.submit_tx.assert_awaited_once_with(expected_tx, encrypt=True)


@pytest.mark.asyncio
async def test_set_auth_token_enc_key_rejects_wrong_key_length() -> None:
    service = AccountingContractService.__new__(AccountingContractService)

    with pytest.raises(ValueError, match="Encryption key must be 32 bytes"):
        await service.set_auth_token_enc_key(b"\x11" * 31)


def _make_service_with_confidential_reader(contract: MagicMock) -> AccountingContractService:
    service = AccountingContractService.__new__(AccountingContractService)
    service._get_confidential_reader_contract = AsyncMock(return_value=contract)
    return service


@pytest.mark.asyncio
async def test_generate_sweep_native_calls_view_function() -> None:
    beneficiary = "0xabababababababababababababababababababab"
    expected_bytes = b"\xde\xad\xbe\xef"

    contract = MagicMock()
    contract.functions.generateSweepNativeTransfer.return_value.call = AsyncMock(
        return_value=expected_bytes
    )

    service = _make_service_with_confidential_reader(contract)
    result = await service.generate_sweep_native(
        beneficiary, "evm", 1, 84532, 1000, 0, 1_000_000_000
    )

    assert result == expected_bytes
    contract.functions.generateSweepNativeTransfer.assert_called_once_with(
        Web3.to_checksum_address(beneficiary), 0, 1, 84532, 1000, 0, 1_000_000_000
    )


@pytest.mark.asyncio
async def test_generate_sweep_erc20_calls_view_function() -> None:
    beneficiary = "0xabababababababababababababababababababab"
    token = "0xcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"
    expected_bytes = b"\xca\xfe\xba\xbe"

    contract = MagicMock()
    contract.functions.generateSweepERC20Transfer.return_value.call = AsyncMock(
        return_value=expected_bytes
    )

    service = _make_service_with_confidential_reader(contract)
    result = await service.generate_sweep_erc20(
        beneficiary, "evm", 1, 84532, token, 2000, 5, 1_000_000_000
    )

    assert result == expected_bytes
    contract.functions.generateSweepERC20Transfer.assert_called_once_with(
        Web3.to_checksum_address(beneficiary),
        0,
        1,
        84532,
        Web3.to_checksum_address(token),
        2000,
        5,
        1_000_000_000,
    )


@pytest.mark.asyncio
async def test_generate_gas_funding_tx_calls_view_function() -> None:
    to_addr = "0xefefefefefefefefefefefefefefefefefefefef"
    expected_bytes = b"\x11\x22\x33\x44"

    contract = MagicMock()
    contract.functions.generateGasFundingTx.return_value.call = AsyncMock(
        return_value=expected_bytes
    )

    service = _make_service_with_confidential_reader(contract)
    result = await service.generate_gas_funding_tx(to_addr, 84532, 10_000, 42, 1_000_000_000)

    assert result == expected_bytes
    contract.functions.generateGasFundingTx.assert_called_once_with(
        Web3.to_checksum_address(to_addr), 84532, 10_000, 42, 1_000_000_000
    )
