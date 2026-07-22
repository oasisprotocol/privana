"""Provider-neutral signed on-ramp intent codec tests."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from src.services import onramp
from src.services import onramp_intent as oi

USER = "0x" + "11" * 20
WALLET = "0x" + "22" * 20
TOKEN_ID = "0x" + "33" * 32
SIGNING_KEY = "0123456789abcdef0123456789abcdef"
ISSUED_AT = 1_781_000_000
NONCE = "0102030405060708"

# This freezes the initial struct order, Transak provider ID, and v1 HMAC
# domain. Any intentional incompatible change requires a new wire version.
GOLDEN_TOKEN = (
    "privana_"
    "AQJqJ-dAaik4wAABSjQRERERERERERERERERERERERERESIiIiIiIiIiIiIiIiIiIiIiIiIiMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMBAgMEBQYHCAl1c2RjOmJhc2U"
    ".M0UwAS-OP-DX3nemSVhjqvDxw5uJat0UdCQNPjSmYnw"
)


def _configure_keys(
    monkeypatch: pytest.MonkeyPatch,
    *,
    current: str | None = SIGNING_KEY,
    previous: tuple[str, ...] = (),
) -> None:
    settings = SimpleNamespace(
        onramp_intent_signing_key=current,
        onramp_intent_previous_signing_keys=previous,
    )
    monkeypatch.setattr(oi, "load_settings", lambda: settings)


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_keys(monkeypatch)


def _signed_raw(payload: bytes, *, key: str = SIGNING_KEY) -> str:
    payload_b64 = oi._b64url_encode(payload)
    signature = oi._signature(payload_b64, key)
    return f"{oi.INTENT_PREFIX}{payload_b64}.{oi._b64url_encode(signature)}"


def _token_payload_bytes(token: str) -> bytes:
    payload_b64 = token.removeprefix(oi.INTENT_PREFIX).split(".", 1)[0]
    return base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))


def _create_transak_intent() -> tuple[str, dict]:
    return oi.create_intent(
        provider=oi.PROVIDER_TRANSAK,
        user_address=USER,
        wallet_address=WALLET,
        token_id=TOKEN_ID,
        chain_id=84_532,
        asset_code="USDC:Base",
    )


def test_frozen_initial_token_decodes_with_permanent_provider_id() -> None:
    wire_payload = _token_payload_bytes(GOLDEN_TOKEN)

    assert wire_payload[:2] == bytes([oi.INTENT_VERSION, 2])
    assert oi.decode_intent(GOLDEN_TOKEN, allow_expired=True) == {
        "v": oi.INTENT_VERSION,
        "p": oi.PROVIDER_TRANSAK,
        "u": USER.removeprefix("0x"),
        "w": WALLET.removeprefix("0x"),
        "t": TOKEN_ID.removeprefix("0x"),
        "c": 84_532,
        "a": "usdc:base",
        "iat": ISSUED_AT,
        "exp": ISSUED_AT + oi.INTENT_TTL_SECONDS,
        "n": NONCE,
    }


def test_mint_is_byte_identical_to_frozen_initial_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oi.time, "time", lambda: ISSUED_AT)
    monkeypatch.setattr(oi.secrets, "token_hex", lambda _length: NONCE)

    token, payload = _create_transak_intent()

    assert token == GOLDEN_TOKEN
    assert payload["v"] == oi.INTENT_VERSION
    assert payload["p"] == oi.PROVIDER_TRANSAK
    assert payload["a"] == "usdc:base"


@pytest.mark.parametrize("provider", [oi.PROVIDER_MOONPAY, oi.PROVIDER_TRANSAK])
def test_roundtrip_binds_provider_and_complete_privana_context(provider: str) -> None:
    token, payload = oi.create_intent(
        provider=provider,
        user_address=USER,
        wallet_address=WALLET,
        token_id=TOKEN_ID,
        chain_id=84_532,
        asset_code="USDC:Base",
    )

    expected_provider_id = {oi.PROVIDER_MOONPAY: 1, oi.PROVIDER_TRANSAK: 2}[provider]
    assert _token_payload_bytes(token)[1] == expected_provider_id
    assert len(token) <= oi.INTENT_MAX_LENGTH
    assert oi.decode_intent(token) == payload
    assert payload == {
        "v": oi.INTENT_VERSION,
        "p": provider,
        "u": USER.removeprefix("0x"),
        "w": WALLET.removeprefix("0x"),
        "t": TOKEN_ID.removeprefix("0x"),
        "c": 84_532,
        "a": "usdc:base",
        "iat": payload["iat"],
        "exp": payload["iat"] + oi.INTENT_TTL_SECONDS,
        "n": payload["n"],
    }


def test_moonpay_adapter_accepts_the_provider_neutral_payload() -> None:
    token, payload = oi.create_intent(
        provider=oi.PROVIDER_MOONPAY,
        user_address=USER,
        wallet_address=WALLET,
        token_id=TOKEN_ID,
        chain_id=84_532,
        asset_code="USDC",
    )

    assert onramp.decode_onramp_intent(token) == payload


def test_moonpay_adapter_rejects_valid_intent_for_another_provider() -> None:
    token, _payload = _create_transak_intent()

    with pytest.raises(onramp.OnRampError, match="not a MoonPay intent"):
        onramp.decode_onramp_intent(token)


def test_mint_and_verify_fail_closed_without_any_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_keys(monkeypatch, current=None)

    with pytest.raises(oi.OnRampNotConfiguredError, match="not configured"):
        _create_transak_intent()
    with pytest.raises(oi.OnRampNotConfiguredError, match="not configured"):
        oi.decode_intent(GOLDEN_TOKEN, allow_expired=True)


def test_rotation_verifies_previous_keys_but_mints_only_with_current_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_key = "old_provider_neutral_signing_key_01"
    new_key = "new_provider_neutral_signing_key_02"
    _configure_keys(monkeypatch, current=old_key)
    old_token, old_payload = _create_transak_intent()

    _configure_keys(monkeypatch, current=new_key, previous=(old_key, old_key))
    assert oi.decode_intent(old_token) == old_payload
    new_token, new_payload = _create_transak_intent()
    assert oi.decode_intent(new_token) == new_payload

    _configure_keys(monkeypatch, current=None, previous=(old_key,))
    assert oi.decode_intent(old_token) == old_payload
    with pytest.raises(oi.OnRampNotConfiguredError, match="not configured"):
        _create_transak_intent()
    with pytest.raises(oi.OnRampError, match="signature mismatch"):
        oi.decode_intent(new_token)


@pytest.mark.parametrize(
    "key",
    [
        "too-short",
        " " + SIGNING_KEY,
        SIGNING_KEY + "\n",
        SIGNING_KEY[:16] + "," + SIGNING_KEY[17:],
        "é" * oi.INTENT_MIN_SIGNING_KEY_BYTES,
    ],
)
def test_invalid_current_signing_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    _configure_keys(monkeypatch, current=key)

    with pytest.raises(oi.OnRampNotConfiguredError, match="configuration is invalid"):
        _create_transak_intent()


def test_invalid_previous_signing_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_keys(monkeypatch, previous=(" " + SIGNING_KEY,))

    with pytest.raises(oi.OnRampNotConfiguredError, match="configuration is invalid"):
        _create_transak_intent()
    with pytest.raises(oi.OnRampNotConfiguredError, match="configuration is invalid"):
        oi.decode_intent(GOLDEN_TOKEN, allow_expired=True)


def test_payload_or_signature_tampering_is_rejected() -> None:
    token, _payload = _create_transak_intent()
    payload_b64, signature_b64 = token.removeprefix(oi.INTENT_PREFIX).split(".")

    payload_bytes = bytearray(oi._b64url_decode(payload_b64))
    payload_bytes[-1] ^= 1
    tampered_payload = (
        f"{oi.INTENT_PREFIX}{oi._b64url_encode(bytes(payload_bytes))}.{signature_b64}"
    )
    signature_bytes = bytearray(oi._b64url_decode(signature_b64))
    signature_bytes[0] ^= 1
    tampered_signature = (
        f"{oi.INTENT_PREFIX}{payload_b64}.{oi._b64url_encode(bytes(signature_bytes))}"
    )

    with pytest.raises(oi.OnRampError, match="signature mismatch"):
        oi.decode_intent(tampered_payload)
    with pytest.raises(oi.OnRampError, match="signature mismatch"):
        oi.decode_intent(tampered_signature)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda token: token + ".extra",
        lambda token: token + "=",
        lambda token: token.replace(".", "=.", 1),
        lambda token: token.rsplit(".", 1)[0] + ".AA",
        lambda _token: "not_privana",
    ],
)
def test_noncanonical_or_malformed_encoding_is_rejected(mutate) -> None:
    token, _payload = _create_transak_intent()

    with pytest.raises(oi.OnRampError):
        oi.decode_intent(mutate(token))


def test_oversized_token_is_rejected_before_decode() -> None:
    token = oi.INTENT_PREFIX + "a" * oi.INTENT_MAX_LENGTH

    with pytest.raises(oi.OnRampError, match="too large"):
        oi.decode_intent(token)


def test_expiry_boundary_and_explicit_recovery_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued_at = 1_800_000_000
    monkeypatch.setattr(oi.time, "time", lambda: issued_at)
    token, payload = _create_transak_intent()

    monkeypatch.setattr(oi.time, "time", lambda: payload["exp"])
    assert oi.decode_intent(token)["exp"] == payload["exp"]

    monkeypatch.setattr(oi.time, "time", lambda: payload["exp"] + 1)
    with pytest.raises(oi.OnRampError, match="expired"):
        oi.decode_intent(token)
    assert oi.decode_intent(token, allow_expired=True)["p"] == oi.PROVIDER_TRANSAK


@pytest.mark.parametrize(
    "payload_mutation",
    [
        lambda payload: bytes([9]) + payload[1:],
        lambda payload: payload[:1] + bytes([99]) + payload[2:],
        lambda payload: payload[:-1],
        lambda payload: payload + b"x",
        lambda payload: payload[:-1] + b"\xff",
        lambda payload: payload[:-9] + payload[-9:].upper(),
    ],
)
def test_validly_signed_invalid_payloads_are_rejected(payload_mutation) -> None:
    original = _token_payload_bytes(GOLDEN_TOKEN)
    signed = _signed_raw(payload_mutation(original))

    with pytest.raises(oi.OnRampError):
        oi.decode_intent(signed, allow_expired=True)


def test_validly_signed_expiry_before_issue_is_rejected() -> None:
    payload = bytearray(_token_payload_bytes(GOLDEN_TOKEN))
    payload[6:10] = (ISSUED_AT - 1).to_bytes(4, "big")

    with pytest.raises(oi.OnRampError, match="expires before"):
        oi.decode_intent(_signed_raw(bytes(payload)), allow_expired=True)


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"provider": "unknown"}, "provider"),
        ({"user_address": "not-an-address"}, "user_address"),
        ({"wallet_address": "not-an-address"}, "wallet_address"),
        ({"token_id": "0x1234"}, "token_id"),
        ({"chain_id": -1}, "chain_id"),
        ({"chain_id": 0x1_0000_0000}, "chain_id"),
        ({"asset_code": "usdc base"}, "asset code"),
        ({"asset_code": "usdç"}, "asset code"),
        ({"asset_code": "a" * 33}, "asset code"),
    ],
)
def test_creation_rejects_invalid_fields(overrides, error: str) -> None:
    arguments = {
        "provider": oi.PROVIDER_TRANSAK,
        "user_address": USER,
        "wallet_address": WALLET,
        "token_id": TOKEN_ID,
        "chain_id": 84_532,
        "asset_code": "usdc:base",
    }
    arguments.update(overrides)

    with pytest.raises(oi.OnRampError, match=error):
        oi.create_intent(**arguments)


def test_maximum_asset_stays_within_intent_length_budget() -> None:
    token, payload = oi.create_intent(
        provider=oi.PROVIDER_TRANSAK,
        user_address=USER,
        wallet_address=WALLET,
        token_id=TOKEN_ID,
        chain_id=84_532,
        asset_code="a" * oi.INTENT_MAX_ASSET_BYTES,
    )

    assert len(token) <= oi.INTENT_MAX_LENGTH
    assert oi.decode_intent(token) == payload
