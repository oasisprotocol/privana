"""Tests for AccountingContractService parsing and request validation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from hexbytes import HexBytes
from web3 import Web3
from web3.constants import ADDRESS_ZERO
from web3.exceptions import BadFunctionCallOutput

from src.abi.accounting_history import ACCOUNTING_HISTORY_ABI
from src.clients.rofl import RoflSubmissionResult
from src.config.tokens import (
    _reset_rose_token_id_cache,
    get_rose_token_id,
)
from src.models.accounting import HistoryKind
from src.services.accounting_contract import (
    AccountingContractService,
    TokenContext,
    _decode_bridge_tx_identifier,
)
from src.services.bridge_startup_check import (
    BridgeStartupCheckError,
    verify_bridge_runtime,
)


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
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ACCOUNTING_ADDRESS = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HISTORY_MODULE_ADDRESS = "0x1111111111111111111111111111111111111111"
HISTORY_MODULE_ADDRESS_2 = "0x3333333333333333333333333333333333333333"
HISTORY_MODULE_ID = Web3.keccak(text="privana.accounting.historyModule.v1")


def _history_reader(module_id: bytes = HISTORY_MODULE_ID) -> MagicMock:
    reader = MagicMock()
    reader.functions.MODULE_ID.return_value.call = AsyncMock(return_value=module_id)
    return reader


def _history_resolution_service(
    accounting_reader: MagicMock,
    history_reader: MagicMock,
    module_address: str = ZERO_ADDRESS,
) -> AccountingContractService:
    service = AccountingContractService.__new__(AccountingContractService)
    service.history_module_address = Web3.to_checksum_address(module_address)
    service.contract_address = Web3.to_checksum_address(ACCOUNTING_ADDRESS)
    service.contract_reader = accounting_reader
    service.w3 = Web3()
    service.reader_w3 = MagicMock()
    service.reader_w3.eth.get_code = AsyncMock(return_value=b"\x01")
    service.reader_w3.eth.contract.return_value = history_reader
    service._confidential_history_contract_reader = None
    service._history_module_validated = False
    service._siwe_auth_address = None
    return service


@pytest.mark.asyncio
async def test_resolve_history_module_address_reads_accounting_proxy() -> None:
    reader = MagicMock()
    reader.functions.historyModule.return_value.call = AsyncMock(
        return_value=HISTORY_MODULE_ADDRESS
    )
    history_reader = _history_reader()

    service = _history_resolution_service(reader, history_reader)

    resolved = await service._resolve_history_module_address()

    assert resolved == Web3.to_checksum_address(HISTORY_MODULE_ADDRESS)
    assert service.history_module_address == Web3.to_checksum_address(HISTORY_MODULE_ADDRESS)
    service.reader_w3.eth.get_code.assert_awaited_once_with(
        Web3.to_checksum_address(HISTORY_MODULE_ADDRESS)
    )
    history_reader.functions.MODULE_ID.assert_called_once_with()


@pytest.mark.asyncio
async def test_resolve_history_module_address_rejects_missing_module() -> None:
    reader = MagicMock()
    reader.functions.historyModule.return_value.call = AsyncMock(return_value=ZERO_ADDRESS)

    service = AccountingContractService.__new__(AccountingContractService)
    service.history_module_address = Web3.to_checksum_address(ZERO_ADDRESS)
    service.contract_reader = reader
    service._history_module_validated = False

    with pytest.raises(ValueError, match="AccountingHistoryModule is not configured"):
        await service._resolve_history_module_address()


@pytest.mark.asyncio
async def test_resolve_history_module_address_revalidates_cached_module() -> None:
    reader = MagicMock()
    reader.functions.historyModule.return_value.call = AsyncMock(
        return_value=HISTORY_MODULE_ADDRESS
    )
    history_reader = _history_reader()
    service = _history_resolution_service(reader, history_reader, HISTORY_MODULE_ADDRESS)

    resolved = await service._resolve_history_module_address()

    assert resolved == Web3.to_checksum_address(HISTORY_MODULE_ADDRESS)
    reader.functions.historyModule.assert_called_once_with()
    assert service._history_module_validated is True


@pytest.mark.asyncio
async def test_resolve_history_module_address_revalidates_on_rotation() -> None:
    reader = MagicMock()
    reader.functions.historyModule.return_value.call = AsyncMock(
        side_effect=[HISTORY_MODULE_ADDRESS, HISTORY_MODULE_ADDRESS_2]
    )
    history_reader = _history_reader()
    service = _history_resolution_service(reader, history_reader)

    resolved = await service._resolve_history_module_address()
    assert resolved == Web3.to_checksum_address(HISTORY_MODULE_ADDRESS)

    cached_reader = MagicMock()
    service._confidential_history_contract_reader = cached_reader
    rotated = await service._resolve_history_module_address()

    assert rotated == Web3.to_checksum_address(HISTORY_MODULE_ADDRESS_2)
    assert service.history_module_address == Web3.to_checksum_address(HISTORY_MODULE_ADDRESS_2)
    assert service._confidential_history_contract_reader is None
    assert history_reader.functions.MODULE_ID.call_count == 2
    assert service.reader_w3.eth.get_code.await_args_list == [
        call(Web3.to_checksum_address(HISTORY_MODULE_ADDRESS)),
        call(Web3.to_checksum_address(HISTORY_MODULE_ADDRESS_2)),
    ]


@pytest.mark.asyncio
async def test_resolve_history_module_address_rejects_wrong_module_id() -> None:
    reader = MagicMock()
    reader.functions.historyModule.return_value.call = AsyncMock(
        return_value=HISTORY_MODULE_ADDRESS
    )
    history_reader = _history_reader(module_id=bytes.fromhex("00" * 32))
    service = _history_resolution_service(reader, history_reader, HISTORY_MODULE_ADDRESS)

    with pytest.raises(
        ValueError,
        match="AccountingHistoryModule has unexpected module id",
    ):
        await service._resolve_history_module_address()


@pytest.mark.asyncio
async def test_resolve_history_module_address_rejects_unreadable_module_id() -> None:
    reader = MagicMock()
    reader.functions.historyModule.return_value.call = AsyncMock(
        return_value=HISTORY_MODULE_ADDRESS
    )
    history_reader = MagicMock()
    history_reader.functions.MODULE_ID.return_value.call = AsyncMock(
        side_effect=BadFunctionCallOutput("missing MODULE_ID")
    )
    service = _history_resolution_service(reader, history_reader, HISTORY_MODULE_ADDRESS)

    with pytest.raises(
        ValueError,
        match="Could not read AccountingHistoryModule MODULE_ID",
    ):
        await service._resolve_history_module_address()


@pytest.mark.asyncio
async def test_resolve_history_module_address_rejects_address_without_code() -> None:
    reader = MagicMock()
    reader.functions.historyModule.return_value.call = AsyncMock(
        return_value=HISTORY_MODULE_ADDRESS
    )
    history_reader = _history_reader()
    service = _history_resolution_service(reader, history_reader, HISTORY_MODULE_ADDRESS)
    service.reader_w3.eth.get_code = AsyncMock(return_value=b"")

    with pytest.raises(
        ValueError,
        match="AccountingHistoryModule address has no contract code",
    ):
        await service._resolve_history_module_address()


@pytest.mark.asyncio
async def test_confidential_history_reader_calls_accounting_proxy_with_module_abi() -> None:
    history_reader = MagicMock()
    service = AccountingContractService.__new__(AccountingContractService)
    service.contract_address = Web3.to_checksum_address(ACCOUNTING_ADDRESS)
    service._confidential_history_contract_reader = None
    service._resolve_history_module_address = AsyncMock(
        return_value=Web3.to_checksum_address(HISTORY_MODULE_ADDRESS)
    )
    service._get_confidential_reader_contract = AsyncMock()
    service._confidential_reader_w3 = MagicMock()
    service._confidential_reader_w3.eth.contract.return_value = history_reader

    resolved = await service._get_confidential_history_reader_contract()

    assert resolved is history_reader
    service._confidential_reader_w3.eth.contract.assert_called_once_with(
        address=Web3.to_checksum_address(ACCOUNTING_ADDRESS),
        abi=ACCOUNTING_HISTORY_ABI,
    )


@pytest.mark.asyncio
async def test_confidential_history_reader_revalidates_before_returning_cached_reader() -> None:
    cached_reader = MagicMock()
    service = AccountingContractService.__new__(AccountingContractService)
    service._confidential_history_contract_reader = cached_reader
    service._resolve_history_module_address = AsyncMock(
        return_value=Web3.to_checksum_address(HISTORY_MODULE_ADDRESS)
    )

    resolved = await service._get_confidential_history_reader_contract()

    assert resolved is cached_reader
    service._resolve_history_module_address.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_confidential_history_reader_rebuilds_after_module_rotation() -> None:
    old_reader = MagicMock()
    new_reader = MagicMock()
    service = AccountingContractService.__new__(AccountingContractService)
    service.contract_address = Web3.to_checksum_address(ACCOUNTING_ADDRESS)
    service._confidential_history_contract_reader = old_reader
    service._get_confidential_reader_contract = AsyncMock()
    service._confidential_reader_w3 = MagicMock()
    service._confidential_reader_w3.eth.contract.return_value = new_reader

    async def _clear_cached_reader() -> str:
        service._confidential_history_contract_reader = None
        return Web3.to_checksum_address(HISTORY_MODULE_ADDRESS_2)

    service._resolve_history_module_address = AsyncMock(side_effect=_clear_cached_reader)

    resolved = await service._get_confidential_history_reader_contract()

    assert resolved is new_reader
    service._resolve_history_module_address.assert_awaited_once_with()
    service._get_confidential_reader_contract.assert_awaited_once_with()
    service._confidential_reader_w3.eth.contract.assert_called_once_with(
        address=Web3.to_checksum_address(ACCOUNTING_ADDRESS),
        abi=ACCOUNTING_HISTORY_ABI,
    )


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
    service._get_confidential_history_reader_contract = AsyncMock(return_value=reader)
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
        )
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
        AccountingContractService._decode_history_payload(kind, payload)


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

    parsed = await service._history_entry_to_dict(entry)

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

    parsed = await service._history_entry_to_dict((99, 1710000002, b""))

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
    service._get_confidential_history_reader_contract = AsyncMock(return_value=reader)
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
        )
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
    service._get_confidential_history_reader_contract = AsyncMock(return_value=reader)

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
    service._get_confidential_history_reader_contract = AsyncMock(return_value=reader)

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


@pytest.mark.asyncio
async def test_request_bridge_withdrawal_encodes_correct_args() -> None:
    user = "0x1111111111111111111111111111111111111111"
    to = "0x2222222222222222222222222222222222222222"
    route = "0x3333333333333333333333333333333333333333"
    dest_chain_id = 84532
    amount = 10**18
    max_gas_cost = 0
    user_nonce = 7
    signature_hex = "0x" + "ab" * 65

    contract = MagicMock()
    encoder = MagicMock()
    encoder._encode_transaction_data.return_value = b"\xca\xfe\xba\xbe"
    contract.functions.requestBridgeWithdrawal.return_value = encoder

    rofl_client = MagicMock()
    rofl_client.submit_tx = AsyncMock(
        return_value=RoflSubmissionResult(submission_id="sub-bridge", ok_payload=None)
    )

    service = AccountingContractService.__new__(AccountingContractService)
    service.contract = contract
    service.contract_address = "0x" + "11" * 20
    service.gas_limit = 500_000
    service.rofl_client = rofl_client

    result = await service.request_bridge_withdrawal(
        {
            "user_address": user,
            "to_address": to,
            "dest_chain_id": dest_chain_id,
            "route_address": route,
            "amount": amount,
            "max_gas_cost": max_gas_cost,
            "user_nonce": user_nonce,
            "signature": signature_hex,
        }
    )

    contract.functions.requestBridgeWithdrawal.assert_called_once_with(
        Web3.to_checksum_address(user),
        Web3.to_checksum_address(to),
        dest_chain_id,
        Web3.to_checksum_address(route),
        amount,
        max_gas_cost,
        user_nonce,
        HexBytes(signature_hex),
    )
    rofl_client.submit_tx.assert_awaited_once()
    assert result.submission_id == "sub-bridge"
    assert f"destChainId={dest_chain_id}" in (result.detail or "")
    assert "routeAddress=" in (result.detail or "")


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


# --- get_rose_token_id helper -------------------------------------------------

_ROSE_TOKEN_ID_HEX = "ca91975d6c6810eb4077546d4fbdb49fa231f351cddfc915862f7c0dad81a7aa"


@pytest.fixture
def _clear_rose_token_id_cache():
    _reset_rose_token_id_cache()
    yield
    _reset_rose_token_id_cache()


@pytest.mark.asyncio
async def test_rose_token_id_fetches_from_contract(_clear_rose_token_id_cache) -> None:
    token_id = bytes.fromhex(_ROSE_TOKEN_ID_HEX)
    reader = MagicMock()
    reader.functions.ROSE_TOKEN_ID.return_value.call = AsyncMock(return_value=token_id)
    service = _make_service_with_reader(reader)

    assert await get_rose_token_id(service) == token_id


@pytest.mark.asyncio
async def test_rose_token_id_caches_after_first_call(_clear_rose_token_id_cache) -> None:
    token_id = bytes.fromhex(_ROSE_TOKEN_ID_HEX)
    reader = MagicMock()
    reader.functions.ROSE_TOKEN_ID.return_value.call = AsyncMock(return_value=token_id)
    service = _make_service_with_reader(reader)

    first = await get_rose_token_id(service)
    second = await get_rose_token_id(service)
    third = await get_rose_token_id(service)

    assert first == second == third == token_id
    reader.functions.ROSE_TOKEN_ID.return_value.call.assert_awaited_once()


@pytest.mark.asyncio
async def test_rose_token_id_reset_clears_cache(_clear_rose_token_id_cache) -> None:
    token_id = bytes.fromhex(_ROSE_TOKEN_ID_HEX)
    reader = MagicMock()
    reader.functions.ROSE_TOKEN_ID.return_value.call = AsyncMock(return_value=token_id)
    service = _make_service_with_reader(reader)

    await get_rose_token_id(service)
    _reset_rose_token_id_cache()
    await get_rose_token_id(service)

    assert reader.functions.ROSE_TOKEN_ID.return_value.call.await_count == 2


# --- _decode_bridge_tx_identifier + get_all_pending_withdrawals BridgeAsset ---


_ROFL_BRIDGE_TEST_ADDR = "0x" + "bb" * 20
_ROSE_TOKEN_ID = bytes.fromhex("ca91975d6c6810eb4077546d4fbdb49fa231f351cddfc915862f7c0dad81a7aa")


def _encode_tx_identifier(
    dest_chain_id: int, dest_tx_nonce: int, route_address: str, max_gas_cost: int
) -> bytes:
    from eth_abi import encode as abi_encode

    return abi_encode(
        ["uint256", "uint64", "address", "uint256"],
        [dest_chain_id, dest_tx_nonce, route_address, max_gas_cost],
    )


def test_decode_bridge_tx_identifier_base_route() -> None:
    tx_id = _encode_tx_identifier(84532, 7, _ROFL_BRIDGE_TEST_ADDR, 0)

    dest_chain_id, dest_tx_nonce, route, max_gas_cost = _decode_bridge_tx_identifier(tx_id)

    assert dest_chain_id == 84532
    assert dest_tx_nonce == 7
    assert route == Web3.to_checksum_address(_ROFL_BRIDGE_TEST_ADDR)
    assert max_gas_cost == 0


def test_decode_bridge_tx_identifier_sapphire_release() -> None:
    sapphire_chain = 23295
    reserve = 10_000_000_000_000_000  # 0.01 ROSE in wei
    tx_id = _encode_tx_identifier(sapphire_chain, 42, ADDRESS_ZERO, reserve)

    dest_chain_id, dest_tx_nonce, route, max_gas_cost = _decode_bridge_tx_identifier(tx_id)

    assert dest_chain_id == sapphire_chain
    assert dest_tx_nonce == 42
    assert route == Web3.to_checksum_address(ADDRESS_ZERO)
    assert max_gas_cost == reserve


@pytest.mark.asyncio
async def test_get_all_pending_withdrawals_decodes_bridge_record() -> None:
    """End-to-end: BridgeAsset record carries decoded chain_id from txIdentifier."""
    user = "0x1234567890123456789012345678901234567890"
    to_address = "0x9876543210987654321098765432109876543210"
    tx_id = _encode_tx_identifier(84532, 11, _ROFL_BRIDGE_TEST_ADDR, 0)

    reader = MagicMock()
    reader.functions.withdrawalCount.return_value.call = AsyncMock(return_value=1)
    reader.functions.withdrawals.return_value.call = AsyncMock(
        return_value=(
            user,
            to_address,
            500,
            42,
            _ROSE_TOKEN_ID,
            False,
            tx_id,
        )
    )

    service = AccountingContractService.__new__(AccountingContractService)
    service.contract_reader = reader
    service.reader_w3 = SimpleNamespace(
        eth=SimpleNamespace(block_number=AsyncMock(return_value=100)())
    )
    service._get_reader_contract = MagicMock(return_value=reader)
    service._get_token_context = AsyncMock(
        return_value=TokenContext(
            chain_id=None,
            token_address=None,
            is_native=False,
            is_bridge_asset=True,
        )
    )

    parsed = await service.get_all_pending_withdrawals()

    assert len(parsed["pending"]) == 1
    record = parsed["pending"][0]
    assert record["chain_id"] == 84532
    assert record["dest_tx_nonce"] == 11
    assert record["route_address"] == Web3.to_checksum_address(_ROFL_BRIDGE_TEST_ADDR)
    assert record["max_gas_cost"] == 0
    assert record["is_bridge_asset"] is True
    # Cross-layer naming: bridge records never expose a generic "nonce" key.
    assert "nonce" not in record


@pytest.mark.asyncio
async def test_get_all_pending_withdrawals_non_bridge_unaffected() -> None:
    """Non-bridge records keep the legacy shape with chain_id from TokenContext."""
    user = "0x1234567890123456789012345678901234567890"
    to_address = "0x9876543210987654321098765432109876543210"
    token_id = bytes.fromhex("11" * 32)

    reader = MagicMock()
    reader.functions.withdrawalCount.return_value.call = AsyncMock(return_value=1)
    reader.functions.withdrawals.return_value.call = AsyncMock(
        return_value=(
            user,
            to_address,
            500,
            42,
            token_id,
            False,
            b"",
        )
    )

    service = AccountingContractService.__new__(AccountingContractService)
    service.contract_reader = reader
    service.reader_w3 = SimpleNamespace(
        eth=SimpleNamespace(block_number=AsyncMock(return_value=100)())
    )
    service._get_reader_contract = MagicMock(return_value=reader)
    service._get_token_context = AsyncMock(
        return_value=TokenContext(
            chain_id=84532,
            token_address=None,
            is_native=True,
            is_bridge_asset=False,
        )
    )

    parsed = await service.get_all_pending_withdrawals()

    assert len(parsed["pending"]) == 1
    record = parsed["pending"][0]
    assert record["chain_id"] == 84532
    assert "dest_tx_nonce" not in record
    assert "route_address" not in record
    assert "is_bridge_asset" not in record


# --- _fetch_token_context BridgeAsset support ---


@pytest.mark.asyncio
async def test_fetch_token_context_bridge_asset_returns_marker() -> None:
    token_id = HexBytes("0xca91975d6c6810eb4077546d4fbdb49fa231f351cddfc915862f7c0dad81a7aa")
    reader = MagicMock()
    reader.functions.tokens.return_value.call = AsyncMock(
        return_value=(2, b"ROSE"),
    )
    service = _make_service_with_reader(reader)

    context = await service._fetch_token_context(token_id)

    assert context.is_bridge_asset is True
    assert context.chain_id is None
    assert context.token_address is None
    assert context.is_native is False


@pytest.mark.asyncio
async def test_fetch_token_context_native_evm_unchanged() -> None:
    token_id = HexBytes("0x" + "11" * 32)
    chain_id = 84532
    data = chain_id.to_bytes(32, "big")
    reader = MagicMock()
    reader.functions.tokens.return_value.call = AsyncMock(return_value=(0, data))
    service = _make_service_with_reader(reader)

    context = await service._fetch_token_context(token_id)

    assert context.is_bridge_asset is False
    assert context.is_native is True
    assert context.chain_id == chain_id
    assert context.token_address is None


@pytest.mark.asyncio
async def test_fetch_token_context_erc20_unchanged() -> None:
    token_id = HexBytes("0x" + "22" * 32)
    chain_id = 84532
    erc20 = bytes.fromhex("abcdef0123456789abcdef0123456789abcdef01")
    data = chain_id.to_bytes(32, "big") + erc20
    reader = MagicMock()
    reader.functions.tokens.return_value.call = AsyncMock(return_value=(1, data))
    service = _make_service_with_reader(reader)

    context = await service._fetch_token_context(token_id)

    assert context.is_bridge_asset is False
    assert context.is_native is False
    assert context.chain_id == chain_id
    assert context.token_address is not None
    assert context.token_address.lower() == "0x" + erc20.hex().lower()


# ---------------------------------------------------------------------------
# Bridge startup sanity checks
# ---------------------------------------------------------------------------


_PROXY_ADDR = "0x" + "aa" * 20
_ROFL_BRIDGE_ADDR = "0x" + "bb" * 20
_XROSE_ADDR = "0x" + "cc" * 20
_BRIDGE_MODULE_ADDR = "0x" + "ee" * 20
_CUSTODY_ADDR = "0x" + "ff" * 20


def _bridge_settings(**overrides) -> SimpleNamespace:
    base = dict(
        accounting_contract_address=_PROXY_ADDR,
        rofl_bridge_address=_ROFL_BRIDGE_ADDR,
        xrose_address=_XROSE_ADDR,
        bridge_mint_limit_wei=10**18,
        bridge_burn_limit_wei=10**18,
        sapphire_chain_id=23295,
        chain_rpc_urls={23295: "http://sapphire", 84532: "http://base"},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def bridge_check_inputs(monkeypatch):
    """Wire a service + reader + base provider + xrose + rofl_bridge.

    Defaults pass every check; tests mutate one input per failure mode.
    """
    from src.services import bridge_startup_check as bsc

    # Stub the pinned-config delegate so contract checks are the ones
    # under test; the delegation itself is covered by a dedicated test.
    monkeypatch.setattr(bsc, "validate_bridge_settings", lambda s: None)
    _reset_rose_token_id_cache()

    settings = _bridge_settings()

    reader = MagicMock()
    reader.address = settings.accounting_contract_address
    reader.abi = [
        {"type": "function", "name": "bridgeModule", "inputs": []},
        {
            "type": "function",
            "name": "roflBridgeAddress",
            "inputs": [{"type": "uint256"}],
        },
        {"type": "function", "name": "requestBridgeWithdrawal", "inputs": []},
        {"type": "function", "name": "ROSE_TOKEN_ID", "inputs": []},
    ]
    reader.functions.bridgeModule.return_value.call = AsyncMock(return_value=_BRIDGE_MODULE_ADDR)
    reader.functions.roflBridgeAddress.return_value.call = AsyncMock(return_value=_ROFL_BRIDGE_ADDR)
    reader.functions.evmAddress.return_value.call = AsyncMock(return_value=_CUSTODY_ADDR)
    reader.functions.ROSE_TOKEN_ID.return_value.call = AsyncMock(return_value=b"\x01" * 32)

    service = _make_service_with_reader(reader)
    service.reader_w3 = MagicMock()
    service.reader_w3.eth.get_code = AsyncMock(return_value=b"\x60\x80")

    base_w3 = MagicMock()
    base_w3.eth.get_code = AsyncMock(return_value=b"\x60\x80")

    xrose_handle = MagicMock()
    xrose_handle.functions.mintingMaxLimitOf.return_value.call = AsyncMock(
        return_value=settings.bridge_mint_limit_wei
    )
    xrose_handle.functions.burningMaxLimitOf.return_value.call = AsyncMock(
        return_value=settings.bridge_burn_limit_wei
    )

    rofl_bridge_handle = MagicMock()
    rofl_bridge_handle.functions.roflSigner.return_value.call = AsyncMock(
        return_value=_CUSTODY_ADDR
    )

    def _contract(address, abi):
        if address.lower() == settings.xrose_address.lower():
            return xrose_handle
        return rofl_bridge_handle

    base_w3.eth.contract = _contract

    service._get_chain_web3 = AsyncMock(return_value=base_w3)

    return SimpleNamespace(
        service=service,
        settings=settings,
        reader=reader,
        base_w3=base_w3,
        xrose=xrose_handle,
        rofl_bridge=rofl_bridge_handle,
    )


@pytest.mark.asyncio
async def test_startup_sanity_happy_path(bridge_check_inputs) -> None:
    await verify_bridge_runtime(bridge_check_inputs.service, bridge_check_inputs.settings)


@pytest.mark.asyncio
async def test_startup_sanity_zero_rose_token_id(bridge_check_inputs) -> None:
    bridge_check_inputs.reader.functions.ROSE_TOKEN_ID.return_value.call = AsyncMock(
        return_value=b"\x00" * 32
    )
    with pytest.raises(BridgeStartupCheckError, match="ROSE_TOKEN_ID returned"):
        await verify_bridge_runtime(bridge_check_inputs.service, bridge_check_inputs.settings)


@pytest.mark.asyncio
async def test_startup_sanity_short_rose_token_id(bridge_check_inputs) -> None:
    bridge_check_inputs.reader.functions.ROSE_TOKEN_ID.return_value.call = AsyncMock(
        return_value=b"\x01" * 16
    )
    with pytest.raises(BridgeStartupCheckError, match="ROSE_TOKEN_ID returned"):
        await verify_bridge_runtime(bridge_check_inputs.service, bridge_check_inputs.settings)


@pytest.mark.asyncio
async def test_startup_sanity_bridge_module_zero(bridge_check_inputs) -> None:
    bridge_check_inputs.reader.functions.bridgeModule.return_value.call = AsyncMock(
        return_value=ADDRESS_ZERO
    )
    with pytest.raises(BridgeStartupCheckError, match="BridgeModule was never set"):
        await verify_bridge_runtime(bridge_check_inputs.service, bridge_check_inputs.settings)


@pytest.mark.asyncio
async def test_startup_sanity_bridge_module_is_proxy(bridge_check_inputs) -> None:
    bridge_check_inputs.reader.functions.bridgeModule.return_value.call = AsyncMock(
        return_value=bridge_check_inputs.settings.accounting_contract_address
    )
    with pytest.raises(BridgeStartupCheckError, match="returned the proxy address"):
        await verify_bridge_runtime(bridge_check_inputs.service, bridge_check_inputs.settings)


@pytest.mark.asyncio
async def test_startup_sanity_bridge_module_no_code(bridge_check_inputs) -> None:
    bridge_check_inputs.service.reader_w3.eth.get_code = AsyncMock(return_value=b"")
    with pytest.raises(BridgeStartupCheckError, match="has no code on Sapphire"):
        await verify_bridge_runtime(bridge_check_inputs.service, bridge_check_inputs.settings)


@pytest.mark.asyncio
async def test_startup_sanity_reader_pointing_at_impl(bridge_check_inputs) -> None:
    bridge_check_inputs.reader.address = "0x" + "12" * 20
    with pytest.raises(BridgeStartupCheckError, match="reader bound to"):
        await verify_bridge_runtime(bridge_check_inputs.service, bridge_check_inputs.settings)


@pytest.mark.asyncio
async def test_startup_sanity_abi_missing_selector(bridge_check_inputs) -> None:
    bridge_check_inputs.reader.abi = [
        item for item in bridge_check_inputs.reader.abi if item["name"] != "bridgeModule"
    ]
    with pytest.raises(BridgeStartupCheckError, match="missing bridge selector 'bridgeModule'"):
        await verify_bridge_runtime(bridge_check_inputs.service, bridge_check_inputs.settings)


@pytest.mark.asyncio
async def test_startup_sanity_rofl_bridge_address_mismatch_is_soft(
    bridge_check_inputs, caplog
) -> None:
    """Route divergence no longer aborts startup — the in-TEE reconciler handles it.

    Asserts: ``verify_bridge_runtime`` proceeds without raising and emits a
    warning that names the reconciler so the operator knows the route will
    be reconciled rather than treated as a fatal config error.
    """
    bridge_check_inputs.reader.functions.roflBridgeAddress.return_value.call = AsyncMock(
        return_value="0x" + "34" * 20
    )
    with caplog.at_level("WARNING", logger="src.services.bridge_startup_check"):
        await verify_bridge_runtime(bridge_check_inputs.service, bridge_check_inputs.settings)
    assert any("reconciler" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_startup_sanity_rofl_bridge_address_zero_logs_bootstrap(
    bridge_check_inputs, caplog
) -> None:
    """When on-chain route is zero, startup proceeds and logs a bootstrap hint."""
    bridge_check_inputs.reader.functions.roflBridgeAddress.return_value.call = AsyncMock(
        return_value="0x" + "00" * 20
    )
    with caplog.at_level("INFO", logger="src.services.bridge_startup_check"):
        await verify_bridge_runtime(bridge_check_inputs.service, bridge_check_inputs.settings)
    assert any("bootstrap" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_startup_sanity_xrose_no_code_on_base(bridge_check_inputs) -> None:
    settings = bridge_check_inputs.settings
    xrose_addr = Web3.to_checksum_address(settings.xrose_address)

    async def _get_code(addr):
        return b"" if addr == xrose_addr else b"\x60\x80"

    bridge_check_inputs.base_w3.eth.get_code = AsyncMock(side_effect=_get_code)
    with pytest.raises(BridgeStartupCheckError, match=r"XROSE_ADDRESS=.* has no code"):
        await verify_bridge_runtime(bridge_check_inputs.service, settings)


@pytest.mark.asyncio
async def test_startup_sanity_rofl_bridge_no_code_on_base(bridge_check_inputs) -> None:
    settings = bridge_check_inputs.settings
    rofl_bridge_addr = Web3.to_checksum_address(settings.rofl_bridge_address)

    async def _get_code(addr):
        return b"" if addr == rofl_bridge_addr else b"\x60\x80"

    bridge_check_inputs.base_w3.eth.get_code = AsyncMock(side_effect=_get_code)
    with pytest.raises(BridgeStartupCheckError, match=r"ROFL_BRIDGE_ADDRESS=.* has no code"):
        await verify_bridge_runtime(bridge_check_inputs.service, settings)


@pytest.mark.asyncio
async def test_startup_sanity_rofl_signer_mismatch(bridge_check_inputs) -> None:
    bridge_check_inputs.rofl_bridge.functions.roflSigner.return_value.call = AsyncMock(
        return_value="0x" + "56" * 20
    )
    with pytest.raises(BridgeStartupCheckError, match="cannot authenticate custody"):
        await verify_bridge_runtime(bridge_check_inputs.service, bridge_check_inputs.settings)


@pytest.mark.asyncio
async def test_startup_sanity_minting_limit_mismatch(bridge_check_inputs) -> None:
    bridge_check_inputs.xrose.functions.mintingMaxLimitOf.return_value.call = AsyncMock(
        return_value=1
    )
    with pytest.raises(BridgeStartupCheckError, match="mintingMaxLimitOf"):
        await verify_bridge_runtime(bridge_check_inputs.service, bridge_check_inputs.settings)


@pytest.mark.asyncio
async def test_startup_sanity_burning_limit_mismatch(bridge_check_inputs) -> None:
    bridge_check_inputs.xrose.functions.burningMaxLimitOf.return_value.call = AsyncMock(
        return_value=1
    )
    with pytest.raises(BridgeStartupCheckError, match="burningMaxLimitOf"):
        await verify_bridge_runtime(bridge_check_inputs.service, bridge_check_inputs.settings)


@pytest.mark.asyncio
async def test_startup_sanity_pinned_config_delegated() -> None:
    """``verify_bridge_runtime`` must call ``validate_bridge_settings``.

    No fixture: ``validate_bridge_settings`` is not stubbed, so a
    pinned-config violation in the settings object must fail before any
    contract read.
    """
    bad_settings = _bridge_settings(rofl_bridge_address="")
    service = _make_service_with_reader(MagicMock())

    with pytest.raises(ValueError, match="ROFL_BRIDGE_ADDRESS"):
        await verify_bridge_runtime(service, bad_settings)
