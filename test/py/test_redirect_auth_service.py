from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from web3 import Web3

from src.auth import siwe_service

TEST_ADDRESS = "0x000000000000000000000000000000000000dEaD"
TEST_DOMAIN = "auth.flexvaults.com"


class FakeSiweMessage:
    def __init__(
        self,
        *,
        address: str = TEST_ADDRESS,
        nonce: str = "a" * 16,
        issued_at: datetime | None,
        expiration_time: datetime | None,
        chain_id: int = 1,
        statement: str = "Sign in to Flexvaults",
        resources: list[str] | None = None,
    ) -> None:
        self.address = address
        self.nonce = nonce
        self.issued_at = issued_at
        self.expiration_time = expiration_time
        self.chain_id = chain_id
        self.statement = statement
        self.resources = resources or ["https://flexvaults.com/privacy"]
        self.verify_calls: list[dict[str, str]] = []

    def verify(
        self, signature: str, domain: str, nonce: str, provider: object | None = None
    ) -> None:
        self.verify_calls.append(
            {
                "signature": signature,
                "domain": domain,
                "nonce": nonce,
                "provider": provider,
            }
        )


@pytest.fixture
def auth_settings():
    return SimpleNamespace(
        siwe_domain=TEST_DOMAIN,
        auth_token_validity_seconds=600,
        environment="production",
        siwe_allowed_chain_ids={1},
        chain_rpc_urls={},
        sapphire_chain_id=23295,
    )


def test_authenticate_siwe_message_success(monkeypatch, auth_settings):
    now = datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc)
    fake_message = FakeSiweMessage(
        issued_at=now,
        expiration_time=now + timedelta(seconds=auth_settings.auth_token_validity_seconds),
    )
    token_store = MagicMock()
    token_store.is_nonce_valid.return_value = True
    token_store.consume_nonce.return_value = True

    auth_token_service = MagicMock()
    auth_token_service.create_and_encrypt.return_value = b"\xaa\xbb"

    monkeypatch.setattr(siwe_service, "load_settings", lambda: auth_settings)
    monkeypatch.setattr(siwe_service, "get_token_store", lambda: token_store)
    monkeypatch.setattr(siwe_service, "get_auth_token_service", lambda: auth_token_service)
    monkeypatch.setattr(siwe_service.SiweMessage, "from_message", lambda _: fake_message)
    monkeypatch.setattr(siwe_service.time, "time", lambda: now.timestamp())

    result = siwe_service.authenticate_siwe_message("message", "0x1234")

    assert result.address == Web3.to_checksum_address(TEST_ADDRESS)
    assert result.valid_until == int(
        (now + timedelta(seconds=auth_settings.auth_token_validity_seconds)).timestamp()
    )
    assert result.statement == fake_message.statement
    assert result.resources == fake_message.resources
    assert result.siwe_token_bytes == b"\xaa\xbb"
    assert result.siwe_token_hex == "0xaabb"
    assert fake_message.verify_calls == [
        {
            "signature": "0x1234",
            "domain": TEST_DOMAIN,
            "nonce": fake_message.nonce,
            "provider": None,
        }
    ]
    auth_token_service.create_and_encrypt.assert_called_once_with(
        domain=TEST_DOMAIN,
        user_addr=Web3.to_checksum_address(TEST_ADDRESS),
        valid_until=result.valid_until,
        statement=fake_message.statement,
        resources=fake_message.resources,
    )
    token_store.is_nonce_valid.assert_called_once_with(fake_message.nonce)
    token_store.consume_nonce.assert_called_once_with(fake_message.nonce)


def test_authenticate_siwe_message_rejects_invalid_nonce(monkeypatch, auth_settings):
    now = datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc)
    fake_message = FakeSiweMessage(
        issued_at=now,
        expiration_time=now + timedelta(seconds=auth_settings.auth_token_validity_seconds),
    )
    token_store = MagicMock()
    token_store.is_nonce_valid.return_value = False

    monkeypatch.setattr(siwe_service, "load_settings", lambda: auth_settings)
    monkeypatch.setattr(siwe_service, "get_token_store", lambda: token_store)
    monkeypatch.setattr(siwe_service.SiweMessage, "from_message", lambda _: fake_message)

    with pytest.raises(siwe_service.SiweAuthError, match="Invalid or expired nonce"):
        siwe_service.authenticate_siwe_message("message", "0x1234")


def test_authenticate_siwe_message_rejects_stale_issued_at(monkeypatch, auth_settings):
    now = datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc)
    fake_message = FakeSiweMessage(
        issued_at=now - timedelta(minutes=7),
        expiration_time=now + timedelta(seconds=auth_settings.auth_token_validity_seconds),
    )
    token_store = MagicMock()
    token_store.is_nonce_valid.return_value = True

    monkeypatch.setattr(siwe_service, "load_settings", lambda: auth_settings)
    monkeypatch.setattr(siwe_service, "get_token_store", lambda: token_store)
    monkeypatch.setattr(siwe_service, "get_auth_token_service", lambda: MagicMock())
    monkeypatch.setattr(siwe_service.SiweMessage, "from_message", lambda _: fake_message)
    monkeypatch.setattr(siwe_service.time, "time", lambda: now.timestamp())

    with pytest.raises(
        siwe_service.SiweAuthError, match="issued_at is outside acceptable time range"
    ):
        siwe_service.authenticate_siwe_message("message", "0x1234")


def test_authenticate_siwe_message_rejects_future_issued_at(monkeypatch, auth_settings):
    now = datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc)
    fake_message = FakeSiweMessage(
        issued_at=now + timedelta(minutes=7),
        expiration_time=now + timedelta(seconds=auth_settings.auth_token_validity_seconds),
    )
    token_store = MagicMock()
    token_store.is_nonce_valid.return_value = True

    monkeypatch.setattr(siwe_service, "load_settings", lambda: auth_settings)
    monkeypatch.setattr(siwe_service, "get_token_store", lambda: token_store)
    monkeypatch.setattr(siwe_service, "get_auth_token_service", lambda: MagicMock())
    monkeypatch.setattr(siwe_service.SiweMessage, "from_message", lambda _: fake_message)
    monkeypatch.setattr(siwe_service.time, "time", lambda: now.timestamp())

    with pytest.raises(
        siwe_service.SiweAuthError, match="issued_at is outside acceptable time range"
    ):
        siwe_service.authenticate_siwe_message("message", "0x1234")


def test_authenticate_siwe_message_rejects_expiration_beyond_max_window(monkeypatch, auth_settings):
    now = datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc)
    fake_message = FakeSiweMessage(
        issued_at=now,
        expiration_time=now + timedelta(seconds=auth_settings.auth_token_validity_seconds + 601),
    )
    token_store = MagicMock()
    token_store.is_nonce_valid.return_value = True

    monkeypatch.setattr(siwe_service, "load_settings", lambda: auth_settings)
    monkeypatch.setattr(siwe_service, "get_token_store", lambda: token_store)
    monkeypatch.setattr(siwe_service, "get_auth_token_service", lambda: MagicMock())
    monkeypatch.setattr(siwe_service.SiweMessage, "from_message", lambda _: fake_message)
    monkeypatch.setattr(siwe_service.time, "time", lambda: now.timestamp())

    with pytest.raises(siwe_service.SiweAuthError, match="exceeds the maximum allowed"):
        siwe_service.authenticate_siwe_message("message", "0x1234")


def test_authenticate_siwe_message_rejects_invalid_address(monkeypatch, auth_settings):
    now = datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc)
    fake_message = FakeSiweMessage(
        address="not-an-address",
        issued_at=now,
        expiration_time=now + timedelta(seconds=auth_settings.auth_token_validity_seconds),
    )
    token_store = MagicMock()
    token_store.is_nonce_valid.return_value = True

    monkeypatch.setattr(siwe_service, "load_settings", lambda: auth_settings)
    monkeypatch.setattr(siwe_service, "get_token_store", lambda: token_store)
    monkeypatch.setattr(siwe_service.SiweMessage, "from_message", lambda _: fake_message)

    with pytest.raises(siwe_service.SiweAuthError, match="Invalid SIWE address"):
        siwe_service.authenticate_siwe_message("message", "0x1234")


def test_authenticate_siwe_message_rejects_disallowed_chain_id(monkeypatch, auth_settings):
    now = datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc)
    fake_message = FakeSiweMessage(
        issued_at=now,
        expiration_time=now + timedelta(seconds=auth_settings.auth_token_validity_seconds),
        chain_id=999,
    )
    token_store = MagicMock()
    token_store.is_nonce_valid.return_value = True

    monkeypatch.setattr(siwe_service, "load_settings", lambda: auth_settings)
    monkeypatch.setattr(siwe_service, "get_token_store", lambda: token_store)
    monkeypatch.setattr(siwe_service.SiweMessage, "from_message", lambda _: fake_message)

    with pytest.raises(siwe_service.SiweAuthError, match="Chain ID 999 is not supported"):
        siwe_service.authenticate_siwe_message("message", "0x1234")


def test_authenticate_siwe_message_rejects_nonce_reuse_race(monkeypatch, auth_settings):
    now = datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc)
    fake_message = FakeSiweMessage(
        issued_at=now,
        expiration_time=now + timedelta(seconds=auth_settings.auth_token_validity_seconds),
    )
    token_store = MagicMock()
    token_store.is_nonce_valid.return_value = True
    token_store.consume_nonce.return_value = False

    auth_token_service = MagicMock()
    auth_token_service.create_and_encrypt.return_value = b"\xaa\xbb"

    monkeypatch.setattr(siwe_service, "load_settings", lambda: auth_settings)
    monkeypatch.setattr(siwe_service, "get_token_store", lambda: token_store)
    monkeypatch.setattr(siwe_service, "get_auth_token_service", lambda: auth_token_service)
    monkeypatch.setattr(siwe_service.SiweMessage, "from_message", lambda _: fake_message)
    monkeypatch.setattr(siwe_service.time, "time", lambda: now.timestamp())

    with pytest.raises(siwe_service.SiweAuthError, match="Nonce already used"):
        siwe_service.authenticate_siwe_message("message", "0x1234")


def test_authenticate_siwe_message_accepts_shorter_expiration_window(monkeypatch, auth_settings):
    now = datetime(2026, 3, 12, 12, 0, tzinfo=timezone.utc)
    fake_message = FakeSiweMessage(
        issued_at=now,
        expiration_time=now + timedelta(minutes=10),
    )
    token_store = MagicMock()
    token_store.is_nonce_valid.return_value = True
    token_store.consume_nonce.return_value = True

    auth_token_service = MagicMock()
    auth_token_service.create_and_encrypt.return_value = b"\xaa\xbb"

    monkeypatch.setattr(siwe_service, "load_settings", lambda: auth_settings)
    monkeypatch.setattr(siwe_service, "get_token_store", lambda: token_store)
    monkeypatch.setattr(siwe_service, "get_auth_token_service", lambda: auth_token_service)
    monkeypatch.setattr(siwe_service.SiweMessage, "from_message", lambda _: fake_message)
    monkeypatch.setattr(siwe_service.time, "time", lambda: now.timestamp())

    result = siwe_service.authenticate_siwe_message("message", "0x1234")

    assert result.valid_until == int((now + timedelta(minutes=10)).timestamp())
