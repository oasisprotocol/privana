"""Tests for AccountingContractService parsing and request validation."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_abi import encode
from hexbytes import HexBytes
from web3 import AsyncWeb3, Web3
from web3.exceptions import ContractLogicError
from web3.providers import AsyncHTTPProvider

from src.abi.accounting import ACCOUNTING_ABI
from src.clients.rofl import RoflSubmissionResult
from src.models.accounting import HistoryKind
from src.models.private_read import PrivateReadAuth
from src.services import accounting_contract as accounting_module
from src.services.accounting_contract import AccountingContractService


def _make_service_with_reader(reader: MagicMock) -> AccountingContractService:
    service = AccountingContractService.__new__(AccountingContractService)
    service.contract_reader = reader
    service._withdrawals_scanned = 0
    service._unresolved_withdrawals = {}
    service._withdrawals_lock = asyncio.Lock()
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


WD_USER = "0x1234567890123456789012345678901234567890"
WD_OTHER = "0x2222222222222222222222222222222222222222"
WD_TO = "0x9876543210987654321098765432109876543210"


def _withdrawal(user: str, resolved: bool = False, amount: int = 99) -> list:
    return [user, WD_TO, amount, 1234, bytes.fromhex("22" * 32), resolved, b"\x56\x78"]


class _FakeWithdrawals:
    """An in-memory `withdrawals` array behind the service's two RPC seams."""

    def __init__(self, entries: list[list]) -> None:
        self.entries = entries
        self.reads: list[list[int]] = []
        self.count_override: int | None = None
        self.fail_at: int | None = None

    async def read(self, indices: list[int]) -> list[list]:
        if not indices:
            return []  # the real batch reader sends no request
        self.reads.append(list(indices))
        await asyncio.sleep(0)
        if self.fail_at is not None and self.fail_at in indices:
            raise ConnectionError("rpc unavailable")
        if any(i >= self.count() for i in indices):
            raise ContractLogicError("execution reverted")
        return [list(self.entries[i]) for i in indices]

    def count(self) -> int:
        return len(self.entries) if self.count_override is None else self.count_override


def _make_indexed_service(fake: _FakeWithdrawals) -> AccountingContractService:
    reader = MagicMock()
    reader.functions.withdrawalCount.return_value.call = AsyncMock(side_effect=fake.count)
    service = _make_service_with_reader(reader)
    service._read_withdrawals = fake.read
    return service


@pytest.mark.asyncio
async def test_get_pending_withdrawals_includes_to_address() -> None:
    service = _make_indexed_service(_FakeWithdrawals([_withdrawal(WD_USER)]))

    parsed = await service.get_pending_withdrawals(WD_USER)

    assert parsed["user_address"] == WD_USER
    assert len(parsed["pending_withdrawals"]) == 1
    pending = parsed["pending_withdrawals"][0]
    assert pending["index"] == 0
    assert pending["to_address"] == WD_TO
    assert pending["amount"] == "99"
    assert pending["token_id"] == "0x" + ("22" * 32)
    assert pending["resolved"] is False
    assert pending["tx_identifier"] == "0x5678"


@pytest.mark.asyncio
async def test_get_pending_withdrawals_filters_user_and_resolved() -> None:
    fake = _FakeWithdrawals(
        [
            _withdrawal(WD_USER.lower()),
            _withdrawal(WD_OTHER),
            _withdrawal(WD_USER, resolved=True),
        ]
    )
    service = _make_indexed_service(fake)

    parsed = await service.get_pending_withdrawals(WD_USER)

    assert [w["index"] for w in parsed["pending_withdrawals"]] == [0]


@pytest.mark.asyncio
async def test_get_pending_withdrawals_reads_only_new_entries() -> None:
    fake = _FakeWithdrawals(
        [
            _withdrawal(WD_USER, resolved=True),
            _withdrawal(WD_OTHER),
            _withdrawal(WD_USER, resolved=True),
        ]
    )
    service = _make_indexed_service(fake)

    assert (await service.get_pending_withdrawals(WD_USER))["pending_withdrawals"] == []
    assert fake.reads == [[0, 1, 2]]

    fake.reads.clear()
    fake.entries.append(_withdrawal(WD_USER))
    parsed = await service.get_pending_withdrawals(WD_USER)

    assert [w["index"] for w in parsed["pending_withdrawals"]] == [3]
    # The new tail entry, then this user's cached candidate re-read.
    assert fake.reads == [[3], [3]]


@pytest.mark.asyncio
async def test_resolved_withdrawal_leaves_index() -> None:
    fake = _FakeWithdrawals([_withdrawal(WD_USER)])
    service = _make_indexed_service(fake)
    assert len((await service.get_pending_withdrawals(WD_USER))["pending_withdrawals"]) == 1

    fake.entries[0][5] = True
    assert (await service.get_pending_withdrawals(WD_USER))["pending_withdrawals"] == []
    assert service._unresolved_withdrawals == {}

    fake.reads.clear()
    assert (await service.get_pending_withdrawals(WD_USER))["pending_withdrawals"] == []
    assert fake.reads == []


@pytest.mark.asyncio
async def test_failed_read_raises_and_resumes_from_last_full_batch(monkeypatch) -> None:
    monkeypatch.setattr(accounting_module, "_WITHDRAWAL_READ_BATCH_SIZE", 2)
    fake = _FakeWithdrawals([_withdrawal(WD_USER) for _ in range(5)])
    fake.fail_at = 3
    service = _make_indexed_service(fake)

    with pytest.raises(ConnectionError):
        await service.get_pending_withdrawals(WD_USER)
    assert service._withdrawals_scanned == 2

    fake.fail_at = None
    fake.reads.clear()
    parsed = await service.get_pending_withdrawals(WD_USER)

    assert [w["index"] for w in parsed["pending_withdrawals"]] == [0, 1, 2, 3, 4]
    assert fake.reads[0] == [2, 3]


@pytest.mark.asyncio
async def test_lagging_withdrawal_count_serves_cached_index(caplog) -> None:
    fake = _FakeWithdrawals([_withdrawal(WD_USER), _withdrawal(WD_USER)])
    service = _make_indexed_service(fake)
    await service.get_pending_withdrawals(WD_USER)

    fake.count_override = 1
    fake.reads.clear()
    parsed = await service.get_pending_withdrawals(WD_USER)

    assert [w["index"] for w in parsed["pending_withdrawals"]] == [0, 1]
    # Index 1 reverts on the lagging node, so only index 0 is re-read.
    assert fake.reads == [[0]]
    assert service._withdrawals_scanned == 2
    assert "below the 2 already indexed" in caplog.text


@pytest.mark.asyncio
async def test_concurrent_callers_read_new_entries_once() -> None:
    fake = _FakeWithdrawals([_withdrawal(WD_OTHER), _withdrawal(WD_OTHER), _withdrawal(WD_USER)])
    service = _make_indexed_service(fake)

    first, second = await asyncio.gather(
        service.get_pending_withdrawals(WD_USER),
        service.get_pending_withdrawals(WD_USER),
    )

    assert [w["index"] for w in first["pending_withdrawals"]] == [2]
    assert [w["index"] for w in second["pending_withdrawals"]] == [2]
    assert fake.reads.count([0, 1, 2]) == 1


@pytest.mark.asyncio
async def test_refresh_without_new_entries_skips_lock() -> None:
    service = _make_indexed_service(_FakeWithdrawals([_withdrawal(WD_USER)]))
    await service.get_pending_withdrawals(WD_USER)

    async with service._withdrawals_lock:
        parsed = await asyncio.wait_for(service.get_pending_withdrawals(WD_USER), timeout=1)

    assert [w["index"] for w in parsed["pending_withdrawals"]] == [0]


@pytest.mark.asyncio
async def test_failed_candidate_reread_raises_and_keeps_index() -> None:
    fake = _FakeWithdrawals([_withdrawal(WD_USER)])
    service = _make_indexed_service(fake)
    await service.get_pending_withdrawals(WD_USER)

    fake.entries[0][5] = True
    fake.fail_at = 0
    with pytest.raises(ConnectionError):
        await service.get_pending_withdrawals(WD_USER)
    assert list(service._unresolved_withdrawals) == [0]


@pytest.mark.asyncio
async def test_get_all_pending_withdrawals_adds_chain_and_can_resolve() -> None:
    fake = _FakeWithdrawals([_withdrawal(WD_USER), _withdrawal(WD_OTHER, resolved=True)])
    service = _make_indexed_service(fake)

    async def _block_number() -> int:
        return 1300

    service.reader_w3 = SimpleNamespace(eth=SimpleNamespace(block_number=_block_number()))
    service._get_token_context = AsyncMock(return_value=SimpleNamespace(chain_id=8453))

    result = await service.get_all_pending_withdrawals()

    assert result["current_block"] == 1300
    assert result["pending"] == [
        {
            "index": 0,
            "user_address": WD_USER,
            "to_address": WD_TO,
            "amount": "99",
            "token_id": "0x" + ("22" * 32),
            "block_number": 1234,
            "can_resolve": True,
            "chain_id": 8453,
        }
    ]


@pytest.mark.asyncio
async def test_get_all_pending_withdrawals_raises_when_count_fails() -> None:
    service = _make_indexed_service(_FakeWithdrawals([]))
    service.reader_w3 = None
    service.contract_reader.functions.withdrawalCount.return_value.call = AsyncMock(
        side_effect=ConnectionError("rpc unavailable")
    )

    with pytest.raises(ConnectionError):
        await service.get_all_pending_withdrawals()


_WITHDRAWAL_TYPES = ["address", "address", "uint256", "uint256", "bytes32", "bool", "bytes"]
_WITHDRAWAL_COUNT_SELECTOR = Web3.keccak(text="withdrawalCount()")[:4].hex().removeprefix("0x")


class _WithdrawalsNode:
    """A JSON-RPC node serving an ABI-encoded `withdrawals` array over HTTP posts."""

    def __init__(
        self, entries: list[tuple], revert_at: int | None = None, drop_last: bool = False
    ) -> None:
        self.entries = entries
        self.revert_at = revert_at
        self.drop_last = drop_last
        self.batch_sizes: list[int] = []

    def _answer(self, request: dict) -> dict:
        if request["method"] == "eth_chainId":
            return {"jsonrpc": "2.0", "id": request["id"], "result": "0x5aff"}
        data = request["params"][0]["data"].removeprefix("0x")
        if data.startswith(_WITHDRAWAL_COUNT_SELECTOR):
            result = encode(["uint256"], [len(self.entries)])
        elif int(data[8:], 16) == self.revert_at:
            error = {"code": 3, "message": "execution reverted"}
            return {"jsonrpc": "2.0", "id": request["id"], "error": error}
        else:
            result = encode(_WITHDRAWAL_TYPES, list(self.entries[int(data[8:], 16)]))
        return {"jsonrpc": "2.0", "id": request["id"], "result": "0x" + result.hex()}

    async def post(self, endpoint_uri: Any, data: bytes, **kwargs: Any) -> bytes:
        await asyncio.sleep(0)  # lets concurrent callers interleave mid-request
        request = json.loads(data)
        if isinstance(request, dict):
            return json.dumps(self._answer(request)).encode()
        self.batch_sizes.append(len(request))
        responses = [self._answer(r) for r in request]
        if self.drop_last:
            responses.pop()
        # Answered out of order, as a node may: the provider must match by id.
        return json.dumps(responses[::-1]).encode()


def _make_rpc_service(node: _WithdrawalsNode) -> AccountingContractService:
    provider = AsyncHTTPProvider("http://sapphire.invalid")
    provider._request_session_manager.async_make_post_request = node.post
    contract = AsyncWeb3(provider).eth.contract(address="0x" + "11" * 20, abi=ACCOUNTING_ABI)
    return _make_service_with_reader(contract)


@pytest.mark.asyncio
async def test_pending_withdrawals_decode_through_web3_batches(monkeypatch) -> None:
    monkeypatch.setattr(accounting_module, "_WITHDRAWAL_READ_BATCH_SIZE", 2)
    token = bytes.fromhex("22" * 32)
    node = _WithdrawalsNode(
        [
            (WD_USER, WD_TO, 5, 100, token, False, b"\x56\x78"),
            (WD_USER, WD_TO, 6, 101, token, True, b""),
            (WD_OTHER, WD_TO, 7, 102, token, False, b""),
        ]
    )
    service = _make_rpc_service(node)

    parsed = await service.get_pending_withdrawals(WD_USER)

    assert parsed["pending_withdrawals"] == [
        {
            "index": 0,
            "user_address": WD_USER,
            "to_address": WD_TO,
            "amount": "5",
            "block_number": 100,
            "token_id": "0x" + "22" * 32,
            "resolved": False,
            "tx_identifier": "0x5678",
        }
    ]
    # The tail in batches of two, then this user's one candidate re-read.
    assert node.batch_sizes == [2, 1, 1]
    assert await service._read_withdrawals([]) == []
    assert node.batch_sizes == [2, 1, 1]


@pytest.mark.asyncio
async def test_reverted_batch_read_raises_through_web3(monkeypatch) -> None:
    monkeypatch.setattr(accounting_module, "_WITHDRAWAL_READ_BATCH_SIZE", 2)
    token = bytes.fromhex("22" * 32)
    node = _WithdrawalsNode([(WD_USER, WD_TO, 5, 100, token, False, b"")] * 3, revert_at=2)
    service = _make_rpc_service(node)

    with pytest.raises(ContractLogicError):
        await service.get_pending_withdrawals(WD_USER)
    assert service._withdrawals_scanned == 2


@pytest.mark.asyncio
async def test_concurrent_reads_keep_web3_batches_separate(monkeypatch) -> None:
    monkeypatch.setattr(accounting_module, "_WITHDRAWAL_READ_BATCH_SIZE", 2)
    token = bytes.fromhex("22" * 32)
    node = _WithdrawalsNode(
        [
            (WD_USER, WD_TO, 5, 100, token, False, b""),
            (WD_OTHER, WD_TO, 6, 101, token, False, b""),
            (WD_USER, WD_TO, 7, 102, token, False, b""),
        ]
    )
    service = _make_rpc_service(node)

    mine, theirs, single = await asyncio.gather(
        service.get_pending_withdrawals(WD_USER),
        service.get_pending_withdrawals(WD_OTHER),
        service.get_withdrawal(1),
    )

    assert [w["amount"] for w in mine["pending_withdrawals"]] == ["5", "7"]
    assert [w["amount"] for w in theirs["pending_withdrawals"]] == ["6"]
    assert single["amount"] == "6"


@pytest.mark.asyncio
async def test_short_batch_response_raises_through_web3() -> None:
    token = bytes.fromhex("22" * 32)
    node = _WithdrawalsNode([(WD_USER, WD_TO, 5, 100, token, False, b"")] * 3, drop_last=True)
    service = _make_rpc_service(node)

    with pytest.raises(RuntimeError, match="2 results for 3 reads"):
        await service._read_withdrawals([0, 1, 2])


USER_A = "0x1234567890123456789012345678901234567890"


def _history_amount(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _history_payload(token_id: bytes, amount: int, tail: bytes) -> bytes:
    return token_id + _history_amount(amount) + tail


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

    parsed = await service.get_history(2, 5, b"\x12\x34")

    assert parsed["total"] == 9
    assert parsed["history"][0] == {
        "kind": "deposit",
        "timestamp": 1710000000,
        "token_id": "0x" + ("33" * 32),
        "amount": "123",
        "counterparty": None,
        "deposit_id": "0x" + ("dd" * 32),
        "chain_id": 84532,
    }
    assert parsed["history"][1] == {
        "kind": "transferFromLockIn",
        "timestamp": 1710000001,
        "token_id": "0x" + ("44" * 32),
        "amount": "456",
        "counterparty": Web3.to_checksum_address("0x" + ("12" * 20)),
        "deposit_id": None,
        "chain_id": 84532,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "kind_name"),
    [
        (1, "withdraw"),
        (2, "createLock"),
        (3, "transferFromLockOut"),
        (4, "transferFromLockIn"),
        (5, "transferBalanceOut"),
        (6, "transferBalanceIn"),
        (7, "modifyLock"),
        (8, "unlockLock"),
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
        "deposit_id": None,
        "chain_id": 84532,
    }


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
            HistoryKind.TransferBalanceOut,
            bytes.fromhex("77" * 32) + _history_amount(1) + bytes.fromhex("cd" * 21),
            "must be 84 bytes",
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
    service._get_confidential_reader_contract = AsyncMock(return_value=reader)
    service._get_token_context = AsyncMock(return_value=SimpleNamespace(chain_id=84532))

    parsed = await service.get_history(0, 10, b"\x12\x34")

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
        "deposit_id": None,
        "chain_id": None,
    }


@pytest.mark.asyncio
async def test_get_history_accepts_negative_offset() -> None:
    reader = MagicMock()
    reader.functions.getHistory.return_value.call = AsyncMock(return_value=([], 9))

    service = AccountingContractService.__new__(AccountingContractService)
    service._get_confidential_reader_contract = AsyncMock(return_value=reader)

    parsed = await service.get_history(-1, 10, b"\x12")

    assert parsed == {"history": [], "total": 9}
    reader.functions.getHistory.assert_called_once_with(-1, 10, b"\x12")


@pytest.mark.asyncio
async def test_get_history_rejects_offset_outside_int256() -> None:
    service = AccountingContractService.__new__(AccountingContractService)

    with pytest.raises(ValueError, match="offset must fit int256"):
        await service.get_history(-(2**255) - 1, 10, b"\x12")

    with pytest.raises(ValueError, match="offset must fit int256"):
        await service.get_history(2**255, 10, b"\x12")


@pytest.mark.asyncio
async def test_get_history_rejects_negative_limit() -> None:
    service = AccountingContractService.__new__(AccountingContractService)

    with pytest.raises(ValueError, match="limit must be >= 0"):
        await service.get_history(0, -1, b"\x12")


@pytest.mark.asyncio
async def test_get_history_preserves_empty_pages_and_total() -> None:
    reader = MagicMock()
    reader.functions.getHistory.return_value.call = AsyncMock(return_value=([], 9))

    service = AccountingContractService.__new__(AccountingContractService)
    service._get_confidential_reader_contract = AsyncMock(return_value=reader)

    parsed = await service.get_history(9, 0, b"\x12\x34")

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
            auth=PrivateReadAuth(token=b"\x00" * 32, user_address=USER_A),
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
            auth=PrivateReadAuth(token=b"\x11" * 32, user_address=USER_A),
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
        auth=PrivateReadAuth(token=siwe_token, user_address=USER_A),
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
async def test_get_gas_price_returns_int() -> None:
    reader = MagicMock()
    reader.functions.gasPrices.return_value.call = AsyncMock(return_value=1_000_000_000)

    service = _make_service_with_reader(reader)
    result = await service.get_gas_price(84532)

    reader.functions.gasPrices.assert_called_once_with(84532)
    assert result == 1_000_000_000


@pytest.mark.asyncio
async def test_set_gas_price_submits_tx() -> None:
    calldata = b"\xde\xad\xbe\xef" + b"\x00" * 32

    contract = MagicMock()
    encoder = MagicMock()
    encoder._encode_transaction_data.return_value = calldata
    contract.functions.setGasPrice.return_value = encoder

    rofl_client = MagicMock()
    rofl_client.submit_tx = AsyncMock(
        return_value=RoflSubmissionResult(submission_id="sub-3", ok_payload=None)
    )

    service = AccountingContractService.__new__(AccountingContractService)
    service.contract = contract
    service.contract_address = "0x" + "11" * 20
    service.gas_limit = 500_000
    service.rofl_client = rofl_client

    result = await service.set_gas_price(84532, 1_000_000_000)

    contract.functions.setGasPrice.assert_called_once_with(84532, 1_000_000_000)
    rofl_client.submit_tx.assert_awaited_once()
    assert result.submission_id == "sub-3"


@pytest.mark.asyncio
async def test_encode_token_data_native() -> None:
    data = b"\x00" * 31 + bytes([1])
    reader = MagicMock()
    reader.functions.encodeEVMNativeTokenData.return_value.call = AsyncMock(return_value=data)

    service = _make_service_with_reader(reader)
    result = await service.encode_token_data(84532, None)

    reader.functions.encodeEVMNativeTokenData.assert_called_once_with(84532)
    assert result == data


@pytest.mark.asyncio
async def test_encode_token_data_erc20_checksums_address() -> None:
    token_address = "0x036cbd53842c5426634e7929541ec2318f3dcf7e"
    data = b"\x00" * 32 + bytes.fromhex(token_address[2:])
    reader = MagicMock()
    reader.functions.encodeEVMErc20TokenData.return_value.call = AsyncMock(return_value=data)

    service = _make_service_with_reader(reader)
    result = await service.encode_token_data(84532, token_address)

    reader.functions.encodeEVMErc20TokenData.assert_called_once_with(
        84532, Web3.to_checksum_address(token_address)
    )
    assert result == data


@pytest.mark.asyncio
async def test_get_token_data_and_id_native() -> None:
    data = b"\x00" * 31 + bytes([1])
    token_id = b"\xaa" * 32
    reader = MagicMock()
    reader.functions.encodeEVMNativeTokenData.return_value.call = AsyncMock(return_value=data)
    reader.functions.getTokenId.return_value.call = AsyncMock(return_value=token_id)

    service = _make_service_with_reader(reader)
    result_data, result_id = await service.get_token_data_and_id(84532, None)

    reader.functions.getTokenId.assert_called_once_with((0, data))
    assert result_data == data
    assert result_id == token_id


@pytest.mark.asyncio
async def test_get_token_id_returns_id_only() -> None:
    data = b"\x00" * 32 + bytes.fromhex("036cbd53842c5426634e7929541ec2318f3dcf7e")
    token_id = b"\xbb" * 32
    reader = MagicMock()
    reader.functions.encodeEVMErc20TokenData.return_value.call = AsyncMock(return_value=data)
    reader.functions.getTokenId.return_value.call = AsyncMock(return_value=token_id)

    service = _make_service_with_reader(reader)
    result = await service.get_token_id(84532, "0x036cbd53842c5426634e7929541ec2318f3dcf7e")

    reader.functions.getTokenId.assert_called_once_with((1, data))
    assert result == token_id


@pytest.mark.asyncio
async def test_set_token_info_submits_tx() -> None:
    calldata = b"\xde\xad\xbe\xef" + b"\x00" * 32
    data = b"\x00" * 31 + bytes([1])

    contract = MagicMock()
    encoder = MagicMock()
    encoder._encode_transaction_data.return_value = calldata
    contract.functions.setTokenInfo.return_value = encoder

    rofl_client = MagicMock()
    rofl_client.submit_tx = AsyncMock(
        return_value=RoflSubmissionResult(submission_id="sub-4", ok_payload=None)
    )

    service = AccountingContractService.__new__(AccountingContractService)
    service.contract = contract
    service.contract_address = "0x" + "11" * 20
    service.gas_limit = 500_000
    service.rofl_client = rofl_client

    result = await service.set_token_info(0, data)

    contract.functions.setTokenInfo.assert_called_once_with((0, data))
    rofl_client.submit_tx.assert_awaited_once()
    assert result.submission_id == "sub-4"


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


def _domain_tuple(fields: bytes, chain_id: int = 0, salt: bytes = b"\x00" * 32) -> tuple:
    return (
        fields,
        "AccountingModule",
        "1",
        chain_id,
        "0x" + "11" * 20,
        salt,
        [],
    )


def _make_domain_service(domain_tuple: tuple) -> AccountingContractService:
    service = AccountingContractService.__new__(AccountingContractService)
    service._eip712_domain = None
    reader = MagicMock()
    reader.functions.eip712Domain.return_value.call = AsyncMock(return_value=domain_tuple)
    service._get_reader_contract = MagicMock(return_value=reader)
    return service


@pytest.mark.asyncio
async def test_eip712_domain_honors_fields_bitmap() -> None:
    salt = (23294).to_bytes(32, "big")
    service = _make_domain_service(_domain_tuple(b"\x1b", salt=salt))

    domain = await service._get_eip712_domain()

    assert domain == {
        "name": "AccountingModule",
        "version": "1",
        "verifyingContract": Web3.to_checksum_address("0x" + "11" * 20),
        "salt": salt,
    }


@pytest.mark.asyncio
async def test_eip712_domain_keeps_chain_id_for_legacy_contract() -> None:
    service = _make_domain_service(_domain_tuple(b"\x0f", chain_id=23295))

    domain = await service._get_eip712_domain()

    assert domain["chainId"] == 23295
    assert "salt" not in domain


@pytest.mark.asyncio
async def test_recover_signer_roundtrips_the_salted_domain() -> None:
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    salt = (23294).to_bytes(32, "big")
    service = _make_domain_service(_domain_tuple(b"\x1b", salt=salt))
    domain = await service._get_eip712_domain()

    account = Account.create()
    message_types = [
        {"name": "tokenId", "type": "bytes32"},
        {"name": "amount", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
    ]
    message = {"tokenId": "0x" + "22" * 32, "amount": 100, "nonce": 0}
    signable = encode_typed_data(
        domain_data=domain, message_types={"Withdraw": message_types}, message_data=message
    )
    signature = account.sign_message(signable).signature

    recovered = await service._recover_eip712_signer(
        "Withdraw", message_types, message, HexBytes(signature)
    )

    assert recovered == account.address
