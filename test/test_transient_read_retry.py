from unittest.mock import AsyncMock, patch

import pytest
from web3.exceptions import ContractLogicError

from src.services.accounting_contract import _call_with_transient_retry


@pytest.mark.asyncio
async def test_retries_transient_failures_then_succeeds():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("rpc dropped")
        return 42

    with patch("src.services.accounting_contract.asyncio.sleep", AsyncMock()):
        assert await _call_with_transient_retry(factory, op="balanceOf") == 42
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_raises_after_exhausting_attempts():
    async def factory():
        raise ConnectionError("rpc dropped")

    with (
        patch("src.services.accounting_contract.asyncio.sleep", AsyncMock()),
        pytest.raises(ConnectionError),
    ):
        await _call_with_transient_retry(factory, op="balanceOf")


@pytest.mark.asyncio
async def test_contract_reverts_are_not_retried():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise ContractLogicError("execution reverted")

    with pytest.raises(ContractLogicError):
        await _call_with_transient_retry(factory, op="balanceOf")
    assert calls["n"] == 1
