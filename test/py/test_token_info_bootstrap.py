"""Tests for the token registration startup bootstrap."""

from unittest.mock import AsyncMock

import pytest

import src.services.token_info_bootstrap as token_info_bootstrap
from src.services.token_info_bootstrap import _MAX_ATTEMPTS, bootstrap_token_info

CHAIN_A = 84532
CHAIN_B = 11155111
USDC_A = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
USDC_B = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"

NATIVE_A = {"chain_id": CHAIN_A}


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip real backoff sleeps between retry attempts."""
    monkeypatch.setattr(token_info_bootstrap.asyncio, "sleep", AsyncMock())


ERC20_A = {"chain_id": CHAIN_A, "token_address": USDC_A}
NATIVE_B = {"chain_id": CHAIN_B}


def _make_service(data_and_id: dict, registered: set[bytes]) -> AsyncMock:
    """data_and_id maps (chain_id, token_address) -> (data, token_id)."""
    service = AsyncMock()

    async def _get_token_data_and_id(chain_id: int, token_address: str | None):
        return data_and_id[(chain_id, token_address)]

    async def _is_token_registered(token_id: bytes) -> bool:
        return token_id in registered

    service.get_token_data_and_id = AsyncMock(side_effect=_get_token_data_and_id)
    service.is_token_registered = AsyncMock(side_effect=_is_token_registered)
    service.set_token_info = AsyncMock()
    return service


@pytest.mark.asyncio
async def test_noop_when_no_env_configured() -> None:
    """ACCOUNTING_TOKEN_INFO unset → nothing to sync, no reads or writes."""
    service = _make_service({}, set())

    await bootstrap_token_info(service, [])

    service.get_token_data_and_id.assert_not_awaited()
    service.set_token_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_registers_native_token_when_unregistered() -> None:
    data = b"\x00" * 31 + bytes([1])
    token_id = b"\xaa" * 32
    service = _make_service({(CHAIN_A, None): (data, token_id)}, registered=set())

    await bootstrap_token_info(service, [NATIVE_A])

    service.set_token_info.assert_awaited_once_with(0, data)


@pytest.mark.asyncio
async def test_registers_erc20_token_when_unregistered() -> None:
    data = b"\x00" * 32 + bytes.fromhex(USDC_A[2:])
    token_id = b"\xbb" * 32
    service = _make_service({(CHAIN_A, USDC_A): (data, token_id)}, registered=set())

    await bootstrap_token_info(service, [ERC20_A])

    service.set_token_info.assert_awaited_once_with(1, data)


@pytest.mark.asyncio
async def test_noop_when_already_registered() -> None:
    data = b"\x00" * 31 + bytes([1])
    token_id = b"\xaa" * 32
    service = _make_service({(CHAIN_A, None): (data, token_id)}, registered={token_id})

    await bootstrap_token_info(service, [NATIVE_A])

    service.set_token_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_registers_multiple_tokens_independently() -> None:
    native_a_data, native_a_id = b"\x01" * 32, b"\xaa" * 32
    erc20_a_data, erc20_a_id = b"\x02" * 32, b"\xbb" * 32
    native_b_data, native_b_id = b"\x03" * 32, b"\xcc" * 32

    service = _make_service(
        {
            (CHAIN_A, None): (native_a_data, native_a_id),
            (CHAIN_A, USDC_A): (erc20_a_data, erc20_a_id),
            (CHAIN_B, None): (native_b_data, native_b_id),
        },
        registered={native_a_id},  # only the first is already registered
    )

    await bootstrap_token_info(service, [NATIVE_A, ERC20_A, NATIVE_B])

    assert service.set_token_info.await_count == 2
    service.set_token_info.assert_any_await(1, erc20_a_data)
    service.set_token_info.assert_any_await(0, native_b_data)


@pytest.mark.asyncio
async def test_one_token_failure_does_not_block_others() -> None:
    """A persistent failure on one token is retried, logged, and does not stop the others."""
    service = AsyncMock()

    async def _get_token_data_and_id(chain_id: int, token_address):
        if chain_id == CHAIN_A:
            raise RuntimeError("rpc error")
        return (b"\x03" * 32, b"\xcc" * 32)

    service.get_token_data_and_id = AsyncMock(side_effect=_get_token_data_and_id)
    service.is_token_registered = AsyncMock(return_value=False)
    service.set_token_info = AsyncMock()

    await bootstrap_token_info(service, [NATIVE_A, NATIVE_B])

    service.set_token_info.assert_awaited_once_with(0, b"\x03" * 32)


@pytest.mark.asyncio
async def test_submit_failure_is_retried_until_success() -> None:
    """A transient tx submission failure is retried and eventually succeeds."""
    data = b"\x00" * 31 + bytes([1])
    token_id = b"\xaa" * 32
    service = _make_service({(CHAIN_A, None): (data, token_id)}, registered=set())
    service.set_token_info = AsyncMock(side_effect=[RuntimeError("rofl-appd unavailable"), None])

    await bootstrap_token_info(service, [NATIVE_A])

    assert service.set_token_info.await_count == 2
    service.set_token_info.assert_awaited_with(0, data)


@pytest.mark.asyncio
async def test_submit_failure_gives_up_after_max_attempts() -> None:
    """A persistent tx submission failure stops after the retry budget is exhausted."""
    data = b"\x00" * 31 + bytes([1])
    token_id = b"\xaa" * 32
    service = _make_service({(CHAIN_A, None): (data, token_id)}, registered=set())
    service.set_token_info = AsyncMock(side_effect=RuntimeError("rofl-appd unavailable"))

    await bootstrap_token_info(service, [NATIVE_A])

    assert service.set_token_info.await_count == _MAX_ATTEMPTS
