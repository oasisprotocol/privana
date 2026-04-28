"""Tests for the ROFL signer address startup bootstrap."""

from unittest.mock import AsyncMock

import pytest
from web3.constants import ADDRESS_ZERO

from src.services.rofl_signer_bootstrap import bootstrap_rofl_signer_address

SIGNER_ADDRESS = "0x" + "ab" * 20
OTHER_ADDRESS = "0x" + "cd" * 20


def _make_rofl_client(address: str) -> AsyncMock:
    client = AsyncMock()
    client.get_keypair = AsyncMock(return_value=("0xdeadbeef", address))
    return client


def _make_service(current_onchain: str) -> AsyncMock:
    service = AsyncMock()
    service.get_rofl_signer_address = AsyncMock(return_value=current_onchain)
    service.set_rofl_signer_address = AsyncMock()
    return service


@pytest.mark.asyncio
async def test_publishes_when_unset() -> None:
    """On fresh deploy, roflSignerAddress is zero → bootstrap should submit the setter tx."""
    service = _make_service(current_onchain=ADDRESS_ZERO)
    rofl_client = _make_rofl_client(SIGNER_ADDRESS)

    await bootstrap_rofl_signer_address(service, rofl_client)

    service.set_rofl_signer_address.assert_awaited_once_with(SIGNER_ADDRESS)


@pytest.mark.asyncio
async def test_noop_when_already_synced() -> None:
    """On restart, on-chain value matches derived → bootstrap should not call setter."""
    service = _make_service(current_onchain=SIGNER_ADDRESS)
    rofl_client = _make_rofl_client(SIGNER_ADDRESS)

    await bootstrap_rofl_signer_address(service, rofl_client)

    service.set_rofl_signer_address.assert_not_awaited()


@pytest.mark.asyncio
async def test_noop_when_already_synced_case_insensitive() -> None:
    """Address comparison must be case-insensitive — checksum differences should not cause re-publish."""
    service = _make_service(current_onchain=SIGNER_ADDRESS.upper().replace("0X", "0x"))
    rofl_client = _make_rofl_client(SIGNER_ADDRESS)

    await bootstrap_rofl_signer_address(service, rofl_client)

    service.set_rofl_signer_address.assert_not_awaited()


@pytest.mark.asyncio
async def test_updates_when_onchain_differs() -> None:
    """If ROFL's derived address changed (e.g., key rotation), bootstrap publishes the new one."""
    service = _make_service(current_onchain=OTHER_ADDRESS)
    rofl_client = _make_rofl_client(SIGNER_ADDRESS)

    await bootstrap_rofl_signer_address(service, rofl_client)

    service.set_rofl_signer_address.assert_awaited_once_with(SIGNER_ADDRESS)
