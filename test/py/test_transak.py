"""Unit tests for the bounded Transak partner API adapter."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from web3 import Web3

import src.services.onramp_intent as onramp_intent
import src.services.transak as transak

USER = Web3.to_checksum_address("0x" + "bb" * 20)
WALLET = Web3.to_checksum_address("0x" + "aa" * 20)
TOKEN_ADDRESS = Web3.to_checksum_address("0x" + "cc" * 20)
TOKEN_ID = "0x" + "11" * 32
TX_HASH = "0x" + "22" * 32
SIGNING_KEY = b"transak-test-intent-signing-key-0001"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
IP_ATTESTATION_WORKER_RUNNER = REPOSITORY_ROOT / "test/js/ip_attestation_worker_runner.mjs"

CONFIG = transak.TransakConfig(
    api_key="transak-api-key",
    api_secret="transak-api-secret",
    api_base_url="https://api-stg.transak.test",
    gateway_base_url="https://gateway-stg.transak.test",
    referrer_domain="app.testnet.privana.finance",
    crypto_currency_code="USDC",
    network="base",
    chain_id=84532,
    token_address=TOKEN_ADDRESS,
)
HEADER_SESSION_IP_CONFIG = transak.TransakSessionIpConfig(
    mode="header",
    header="x-original-user-ip",
    attestation_secret=None,
)


@pytest.fixture(autouse=True)
def _intent_key(monkeypatch):
    manager = onramp_intent.OnRampIntentKeyManager()
    manager._current_key = SIGNING_KEY
    manager._verification_keys = (SIGNING_KEY,)
    monkeypatch.setattr(
        onramp_intent,
        "_onramp_intent_key_manager_instance",
        manager,
    )


def _intent() -> dict:
    return transak.create_transak_intent(
        user_address=USER,
        wallet_address=WALLET,
        token_id=TOKEN_ID,
        chain_id=CONFIG.chain_id,
        config=CONFIG,
    )


def _order(transaction_id: str, **overrides) -> dict:
    order = {
        "_id": "transak-order-1",
        "partnerOrderId": transaction_id,
        "walletAddress": WALLET,
        "status": "COMPLETED",
        "cryptoCurrency": "USDC",
        "network": "base",
        "isBuyOrSell": "BUY",
        "transactionHash": TX_HASH,
        "createdAt": "2026-07-23T10:00:00.000Z",
        "updatedAt": "2026-07-23T10:01:00.000Z",
        "fiatCurrency": "USD",
        "fiatAmount": 100,
        "cryptoAmount": "99.5",
    }
    order.update(overrides)
    return order


def _token_response(token: str, expires_at: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": {"accessToken": token, "expiresAt": expires_at}},
    )


def test_load_config_is_lazy_fail_closed_and_secret_safe(monkeypatch) -> None:
    values = {
        "transak_api_key": CONFIG.api_key,
        "transak_api_secret": CONFIG.api_secret,
        "transak_api_base_url": CONFIG.api_base_url,
        "transak_gateway_base_url": CONFIG.gateway_base_url,
        "transak_referrer_domain": CONFIG.referrer_domain,
        # Session-only settings must not gate intent creation or order recovery.
        "transak_client_ip_mode": "attested",
        "transak_client_ip_header": None,
        "transak_ip_attestation_secret": None,
        "transak_crypto_currency_code": CONFIG.crypto_currency_code,
        "transak_network": CONFIG.network,
        "transak_chain_id": CONFIG.chain_id,
        "transak_token_address": CONFIG.token_address,
    }
    monkeypatch.setattr(transak, "load_settings", lambda: SimpleNamespace(**values))
    loaded = transak.load_transak_config()
    assert loaded == CONFIG
    assert CONFIG.api_secret not in repr(loaded)

    for field, bad_value in [
        ("transak_api_key", ""),
        ("transak_api_secret", None),
        ("transak_api_base_url", "http://api.transak.test"),
        ("transak_api_base_url", "https://[bad"),
        ("transak_api_base_url", "https://api.transak.test:not-a-port"),
        ("transak_gateway_base_url", "https://user:pass@gateway.transak.test"),
        ("transak_gateway_base_url", "https://gateway.transak.test:70000"),
        ("transak_referrer_domain", "https://app.testnet.privana.finance"),
        ("transak_referrer_domain", "app.testnet.privana.finance:not-a-port"),
        ("transak_referrer_domain", "app.testnet.privana.finance:443"),
        ("transak_referrer_domain", "APP.TESTNET.PRIVANA.FINANCE"),
        ("transak_referrer_domain", "app_testnet.privana.finance"),
        ("transak_referrer_domain", "-app.testnet.privana.finance"),
        ("transak_referrer_domain", "app..privana.finance"),
        ("transak_referrer_domain", "app.testnet.privana.finance."),
        ("transak_referrer_domain", "localhost"),
        ("transak_referrer_domain", "127.0.0.1"),
        ("transak_referrer_domain", "127.1"),
        ("transak_referrer_domain", "|"),
        ("transak_crypto_currency_code", "USDC,ETH"),
        ("transak_network", ""),
        ("transak_api_secret", "secret\u2603"),
        ("transak_chain_id", None),
        ("transak_token_address", "0x0"),
    ]:
        invalid = {**values, field: bad_value}
        monkeypatch.setattr(
            transak, "load_settings", lambda invalid=invalid: SimpleNamespace(**invalid)
        )
        with pytest.raises(onramp_intent.OnRampNotConfiguredError):
            transak.load_transak_config()


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["203.0.113.4"], "203.0.113.4"),
        (["2001:0db8::1"], "2001:db8::1"),
    ],
)
def test_client_ip_requires_one_literal_ip(values, expected) -> None:
    assert transak.client_ip_from_values(values, header_name="x-original-user-ip") == expected


@pytest.mark.parametrize("values", [[], ["a", "b"], ["1.2.3.4, 5.6.7.8"], ["not-an-ip"]])
def test_client_ip_rejects_missing_ambiguous_or_invalid(values) -> None:
    with pytest.raises(onramp_intent.OnRampError):
        transak.client_ip_from_values(values, header_name="x-original-user-ip")


ATTESTATION_SECRET = "attestation-shared-secret-0123456789abcdef"
ATTESTED_SESSION_IP_CONFIG = transak.TransakSessionIpConfig(
    mode="attested",
    header=None,
    attestation_secret=ATTESTATION_SECRET,
)
ATTESTATION_NOW = 1_800_000_000
ATTESTATION_TRANSACTION_ID = "signed-intent-value"
ATTESTATION_NONCE = "00112233445566778899aabbccddeeff"


@pytest.fixture(autouse=True)
def _fresh_attestation_nonce_cache(monkeypatch):
    monkeypatch.setattr(transak, "_ip_attestation_nonces", {})


def _attestation(
    ip: str,
    *,
    transaction_id: str = ATTESTATION_TRANSACTION_ID,
    issued_at: int = ATTESTATION_NOW,
    expires_at: int = ATTESTATION_NOW + 60,
    nonce: str = "0123456789abcdef0123456789abcdef",
    secret: str = ATTESTATION_SECRET,
    referrer_domain: str = CONFIG.referrer_domain,
) -> dict:
    intent_hash = hashlib.sha256(transaction_id.encode()).hexdigest()
    payload = "|".join(
        ("v1", referrer_domain, intent_hash, ip, str(issued_at), str(expires_at), nonce)
    )
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return {
        "version": 1,
        "ip": ip,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "nonce": nonce,
        "signature": signature,
    }


def _worker_input(**overrides) -> dict:
    intent_hash = hashlib.sha256(ATTESTATION_TRANSACTION_ID.encode()).hexdigest()
    worker_input = {
        "url": "https://app.testnet.privana.finance/__onramp-ip-attest",
        "headers": {
            "cf-connecting-ip": "2606:4700:4700:0000::1111",
            "content-type": "application/json",
        },
        "body": {"intentHash": intent_hash},
        "env": {
            "ATTESTATION_SECRET": ATTESTATION_SECRET,
            "REFERRER_DOMAIN": CONFIG.referrer_domain,
        },
        "nowMs": ATTESTATION_NOW * 1000,
        "nonceHex": ATTESTATION_NONCE,
    }
    worker_input.update(overrides)
    return worker_input


def _run_worker(worker_input: dict) -> dict:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for Worker/Python conformance tests"
    completed = subprocess.run(
        [node, str(IP_ATTESTATION_WORKER_RUNNER)],
        cwd=REPOSITORY_ROOT,
        input=json.dumps(worker_input),
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize(
    ("observed_ip", "canonical_ip"),
    [
        ("8.8.8.8", "8.8.8.8"),
        ("2606:4700:4700:0000::1111", "2606:4700:4700::1111"),
        # First /32 after the rejected Cloudflare Worker egress /29.
        ("2a06:98c8::1", "2a06:98c8::1"),
    ],
)
def test_worker_python_ip_attestation_conformance(observed_ip, canonical_ip) -> None:
    worker_input = _worker_input()
    worker_input["headers"]["cf-connecting-ip"] = observed_ip
    result = _run_worker(worker_input)
    assert result["status"] == 200, result
    assert result["headers"]["cache-control"] == "no-store"
    claim = result["body"]
    assert set(claim) == {"v", "ip", "iat", "exp", "nonce", "sig"}
    assert claim["ip"] == canonical_ip
    assert claim["iat"] == ATTESTATION_NOW
    assert claim["exp"] == ATTESTATION_NOW + 60
    assert claim["nonce"] == ATTESTATION_NONCE

    intent_hash = hashlib.sha256(ATTESTATION_TRANSACTION_ID.encode()).hexdigest()
    payload = "|".join(
        (
            "v1",
            CONFIG.referrer_domain,
            intent_hash,
            claim["ip"],
            str(claim["iat"]),
            str(claim["exp"]),
            claim["nonce"],
        )
    )
    assert (
        claim["sig"]
        == hmac.new(ATTESTATION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    )
    assert (
        transak.verify_ip_attestation(
            version=claim["v"],
            ip=claim["ip"],
            issued_at=claim["iat"],
            expires_at=claim["exp"],
            nonce=claim["nonce"],
            signature=claim["sig"],
            transaction_id=ATTESTATION_TRANSACTION_ID,
            config=CONFIG,
            session_ip_config=ATTESTED_SESSION_IP_CONFIG,
            now=ATTESTATION_NOW,
        )
        == claim["ip"]
    )


@pytest.mark.parametrize(
    ("overrides", "expected_status"),
    [
        (
            {
                "body": {
                    "intentHash": hashlib.sha256(ATTESTATION_TRANSACTION_ID.encode()).hexdigest(),
                    "extra": True,
                }
            },
            400,
        ),
        (
            {
                "env": {
                    "ATTESTATION_SECRET": ATTESTATION_SECRET,
                    "REFERRER_DOMAIN": "bad|domain",
                }
            },
            503,
        ),
        (
            {
                "env": {
                    "ATTESTATION_SECRET": "a" * 31 + " ",
                    "REFERRER_DOMAIN": CONFIG.referrer_domain,
                }
            },
            503,
        ),
        (
            {
                "headers": {
                    "cf-connecting-ip": "2606:4700:4700::1111%zone",
                    "content-type": "application/json",
                }
            },
            400,
        ),
        (
            {
                "headers": {
                    "cf-connecting-ip": "999.1.1.1",
                    "content-type": "application/json",
                }
            },
            400,
        ),
        ({"url": "https://attacker.example/__onramp-ip-attest"}, 403),
        (
            {
                "headers": {
                    "cf-connecting-ip": "8.8.8.8",
                    "content-type": "text/plain",
                }
            },
            415,
        ),
        (
            {
                "headers": {
                    "cf-connecting-ip": "8.8.8.8",
                    "cf-worker": "attacker.example",
                    "content-type": "application/json",
                }
            },
            403,
        ),
        (
            {
                "headers": {
                    "cf-connecting-ip": "2a06:98c7::1",
                    "content-type": "application/json",
                }
            },
            400,
        ),
        (
            {
                "headers": {
                    "cf-connecting-ip": "fec0::1",
                    "content-type": "application/json",
                }
            },
            400,
        ),
        ({"bodyText": "x" * 513}, 413),
        ({"url": "https://app.testnet.privana.finance/other"}, 403),
        ({"url": "https://app.testnet.privana.finance/__onramp-ip-attest?x=1"}, 403),
    ],
)
def test_worker_rejects_noncanonical_or_ambiguous_inputs(overrides, expected_status) -> None:
    result = _run_worker(_worker_input(**overrides))
    assert result["status"] == expected_status, result


def test_session_ip_config_is_mode_aware_fail_closed(monkeypatch) -> None:
    values = {
        "transak_client_ip_mode": "attested",
        "transak_client_ip_header": None,
        "transak_ip_attestation_secret": ATTESTATION_SECRET,
    }
    monkeypatch.setattr(transak, "load_settings", lambda: SimpleNamespace(**values))
    loaded = transak.load_transak_session_ip_config()
    assert loaded == ATTESTED_SESSION_IP_CONFIG
    assert ATTESTATION_SECRET not in repr(loaded)

    for field, bad_value in [
        ("transak_ip_attestation_secret", None),
        ("transak_ip_attestation_secret", "too-short"),
        ("transak_ip_attestation_secret", "a" * 31 + " "),
        ("transak_ip_attestation_secret", "a" * 31 + "\u2603"),
    ]:
        invalid = {**values, field: bad_value}
        monkeypatch.setattr(
            transak, "load_settings", lambda invalid=invalid: SimpleNamespace(**invalid)
        )
        with pytest.raises(onramp_intent.OnRampNotConfiguredError):
            transak.load_transak_session_ip_config()

    for field, bad_value in [
        ("transak_client_ip_header", "bad header"),
        ("transak_client_ip_mode", "bogus"),
    ]:
        invalid = {
            **values,
            "transak_client_ip_mode": None,
            "transak_client_ip_header": HEADER_SESSION_IP_CONFIG.header,
            field: bad_value,
        }
        monkeypatch.setattr(
            transak, "load_settings", lambda invalid=invalid: SimpleNamespace(**invalid)
        )
        with pytest.raises(onramp_intent.OnRampNotConfiguredError):
            transak.load_transak_session_ip_config()


@pytest.mark.parametrize(
    ("ip", "expected"),
    [
        ("8.8.8.8", "8.8.8.8"),
        ("2606:4700:4700::1111", "2606:4700:4700::1111"),
    ],
)
def test_ip_attestation_accepts_valid_global_claims(ip, expected) -> None:
    claim = _attestation(ip)
    attested = transak.verify_ip_attestation(
        transaction_id=ATTESTATION_TRANSACTION_ID,
        config=CONFIG,
        session_ip_config=ATTESTED_SESSION_IP_CONFIG,
        now=ATTESTATION_NOW + 1,
        **claim,
    )
    assert attested == expected


def test_ip_attestation_binds_intent_domain_and_secret() -> None:
    for claim in [
        _attestation("8.8.8.8", transaction_id="other-intent"),
        _attestation("8.8.8.8", referrer_domain="evil.example"),
        _attestation("8.8.8.8", secret="wrong-secret-0123456789abcdef0123"),
        {**_attestation("8.8.8.8"), "signature": "ab" * 32},
    ]:
        with pytest.raises(onramp_intent.OnRampError, match="Invalid client IP attestation"):
            transak.verify_ip_attestation(
                transaction_id=ATTESTATION_TRANSACTION_ID,
                config=CONFIG,
                session_ip_config=ATTESTED_SESSION_IP_CONFIG,
                now=ATTESTATION_NOW + 1,
                **claim,
            )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"issued_at": 0}, "Invalid client IP attestation"),
        ({"expires_at": 0}, "Invalid client IP attestation"),
        (
            {"issued_at": ATTESTATION_NOW + 31, "expires_at": ATTESTATION_NOW + 91},
            "not yet valid",
        ),
        ({"expires_at": ATTESTATION_NOW - 1}, "has expired"),
        (
            {"issued_at": ATTESTATION_NOW - 1, "expires_at": ATTESTATION_NOW + 90},
            "window is too long",
        ),
        (
            {"issued_at": ATTESTATION_NOW + 20, "expires_at": ATTESTATION_NOW + 10},
            "Invalid client IP attestation",
        ),
    ],
)
def test_ip_attestation_enforces_short_single_window(overrides, message) -> None:
    claim = _attestation(
        "8.8.8.8",
        issued_at=overrides.get("issued_at", ATTESTATION_NOW),
        expires_at=overrides.get("expires_at", ATTESTATION_NOW + 60),
    )
    with pytest.raises(onramp_intent.OnRampError, match=message):
        transak.verify_ip_attestation(
            transaction_id=ATTESTATION_TRANSACTION_ID,
            config=CONFIG,
            session_ip_config=ATTESTED_SESSION_IP_CONFIG,
            now=ATTESTATION_NOW,
            **claim,
        )


@pytest.mark.parametrize(
    ("ip", "message"),
    [
        ("10.0.0.1", "public IP"),
        ("127.0.0.1", "public IP"),
        ("169.254.1.1", "public IP"),
        ("fc00::1", "public IP"),
        ("2a06:98c0:3600::1", "direct request"),
        ("not-an-ip", "Invalid client IP attestation"),
        ("203.0.113.4 ", "Invalid client IP attestation"),
        ("2606:4700:4700:0000::1111", "canonical IP"),
        ("2606:4700:4700::1111%zone", "Invalid client IP attestation"),
        ("224.0.0.1", "public IP"),
        ("ff00::1", "public IP"),
        ("::ffff:808:808", "public IP"),
        ("::808:808", "public IP"),
        ("fec0::1", "public IP"),
    ],
)
def test_ip_attestation_requires_direct_global_ip(ip, message) -> None:
    claim = _attestation(ip)
    with pytest.raises(onramp_intent.OnRampError, match=message):
        transak.verify_ip_attestation(
            transaction_id=ATTESTATION_TRANSACTION_ID,
            config=CONFIG,
            session_ip_config=ATTESTED_SESSION_IP_CONFIG,
            now=ATTESTATION_NOW + 1,
            **claim,
        )


def test_ip_attestation_rejects_malformed_and_wrong_version_claims() -> None:
    base = _attestation("8.8.8.8")
    with pytest.raises(onramp_intent.OnRampError, match="version"):
        transak.verify_ip_attestation(
            transaction_id=ATTESTATION_TRANSACTION_ID,
            config=CONFIG,
            session_ip_config=ATTESTED_SESSION_IP_CONFIG,
            now=ATTESTATION_NOW + 1,
            **{**base, "version": 2},
        )
    for field, bad_value in [
        ("nonce", "short"),
        ("nonce", "zz" * 16),
        ("nonce", "AB" * 16),
        ("signature", "AB" * 32),
        ("signature", "ab" * 31),
        ("ip", ""),
        ("ip", "3" * 65),
    ]:
        with pytest.raises(onramp_intent.OnRampError, match="Invalid client IP attestation"):
            transak.verify_ip_attestation(
                transaction_id=ATTESTATION_TRANSACTION_ID,
                config=CONFIG,
                session_ip_config=ATTESTED_SESSION_IP_CONFIG,
                now=ATTESTATION_NOW + 1,
                **{**base, field: bad_value},
            )


def test_ip_attestation_is_single_use_and_bounded() -> None:
    claim = _attestation("8.8.8.8")
    first = transak.verify_ip_attestation(
        transaction_id=ATTESTATION_TRANSACTION_ID,
        config=CONFIG,
        session_ip_config=ATTESTED_SESSION_IP_CONFIG,
        now=ATTESTATION_NOW + 1,
        **claim,
    )
    assert first == "8.8.8.8"
    replay = _attestation("8.8.8.8", nonce=claim["nonce"])
    with pytest.raises(onramp_intent.OnRampError, match="already used"):
        transak.verify_ip_attestation(
            transaction_id=ATTESTATION_TRANSACTION_ID,
            config=CONFIG,
            session_ip_config=ATTESTED_SESSION_IP_CONFIG,
            now=ATTESTATION_NOW + 1,
            **replay,
        )

    transak._ip_attestation_nonces.clear()
    transak._ip_attestation_nonces.update(
        {f"{index:032x}": ATTESTATION_NOW + 600 for index in range(10_000)}
    )
    fresh = _attestation("8.8.8.8", nonce="f" * 32)
    with pytest.raises(onramp_intent.OnRampError, match="could not be recorded"):
        transak.verify_ip_attestation(
            transaction_id=ATTESTATION_TRANSACTION_ID,
            config=CONFIG,
            session_ip_config=ATTESTED_SESSION_IP_CONFIG,
            now=ATTESTATION_NOW + 1,
            **fresh,
        )

    transak._ip_attestation_nonces.clear()
    transak._ip_attestation_nonces["stale" + "0" * 27] = ATTESTATION_NOW - 1
    transak.verify_ip_attestation(
        transaction_id=ATTESTATION_TRANSACTION_ID,
        config=CONFIG,
        session_ip_config=ATTESTED_SESSION_IP_CONFIG,
        now=ATTESTATION_NOW + 1,
        **fresh,
    )
    assert "stale" + "0" * 27 not in transak._ip_attestation_nonces


def test_ip_attestation_requires_attested_mode() -> None:
    claim = _attestation("8.8.8.8")
    with pytest.raises(onramp_intent.OnRampNotConfiguredError):
        transak.verify_ip_attestation(
            transaction_id=ATTESTATION_TRANSACTION_ID,
            config=CONFIG,
            session_ip_config=HEADER_SESSION_IP_CONFIG,
            now=ATTESTATION_NOW + 1,
            **claim,
        )


async def test_token_cache_and_session_request_are_exact() -> None:
    now = [1_800_000_000.0]
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/refresh-token"):
            return _token_response("access-token-1", int(now[0]) + 600)
        assert request.url.path == "/api/v2/auth/session"
        return httpx.Response(
            200,
            json={"data": {"widgetUrl": "https://global-stg.transak.test?sessionId=one"}},
        )

    service = transak.TransakService(transport=httpx.MockTransport(handler), now=lambda: now[0])
    intent = _intent()
    first = await service.create_widget_session(
        transaction_id=intent["transaction_id"],
        wallet_address=WALLET,
        user_ip="8.8.8.8",
        config=CONFIG,
    )
    await service.create_widget_session(
        transaction_id=intent["transaction_id"],
        wallet_address=WALLET,
        user_ip="8.8.8.8",
        config=CONFIG,
    )

    assert first == {
        "provider": "transak",
        "url": "https://global-stg.transak.test?sessionId=one",
        "expires_at": int(now[0]) + 300,
    }
    assert [request.url.path for request in requests].count("/partners/api/v2/refresh-token") == 1
    session_request = requests[1]
    assert session_request.headers["access-token"] == "access-token-1"
    assert session_request.headers["x-api-key"] == CONFIG.api_key
    assert session_request.headers["x-user-ip"] == "8.8.8.8"
    assert json.loads(session_request.content) == {
        "widgetParams": {
            "apiKey": CONFIG.api_key,
            "referrerDomain": CONFIG.referrer_domain,
            "productsAvailed": "BUY",
            "cryptoCurrencyCode": CONFIG.crypto_currency_code,
            "network": CONFIG.network,
            "walletAddress": WALLET,
            "disableWalletAddressForm": True,
            "partnerOrderId": intent["transaction_id"],
        }
    }


async def test_session_expiry_is_measured_from_request_start() -> None:
    now = [1_800_000_000.0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/refresh-token"):
            return _token_response("access-token", int(now[0]) + 600)
        now[0] += 120
        return httpx.Response(
            200,
            json={"data": {"widgetUrl": "https://global-stg.transak.test?sessionId=one"}},
        )

    service = transak.TransakService(transport=httpx.MockTransport(handler), now=lambda: now[0])
    session = await service.create_widget_session(
        transaction_id=_intent()["transaction_id"],
        wallet_address=WALLET,
        user_ip="8.8.8.8",
        config=CONFIG,
    )

    assert session["expires_at"] == 1_800_000_300


@pytest.mark.parametrize(
    "widget_url",
    [
        "http://global-stg.transak.test?sessionId=one",
        "https://user:pass@global-stg.transak.test?sessionId=one",
        "https://[bad",
        "https://global-stg.transak.test:not-a-port",
        "https://global-stg.transak.test:70000",
    ],
)
async def test_malformed_widget_urls_are_rejected(widget_url) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/refresh-token"):
            return _token_response("access-token", 1_800_000_600)
        return httpx.Response(200, json={"data": {"widgetUrl": widget_url}})

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )
    with pytest.raises(transak.TransakAPIError, match="malformed"):
        await service.create_widget_session(
            transaction_id=_intent()["transaction_id"],
            wallet_address=WALLET,
            user_ip="8.8.8.8",
            config=CONFIG,
        )


async def test_concurrent_cold_requests_refresh_once() -> None:
    refresh_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_count
        if request.url.path.endswith("/refresh-token"):
            refresh_count += 1
            await asyncio.sleep(0)
            return _token_response("shared-token", 1_800_000_600)
        return httpx.Response(200, json={"meta": {"totalCount": 0}, "data": []})

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )
    await asyncio.gather(*(service.get_orders_by_wallet(WALLET, config=CONFIG) for _ in range(8)))
    assert refresh_count == 1


async def test_concurrent_401s_use_one_compare_and_swap_refresh() -> None:
    refresh_count = 0
    order_tokens: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_count
        if request.url.path.endswith("/refresh-token"):
            refresh_count += 1
            token = "old-token" if refresh_count == 1 else "new-token"
            return _token_response(token, 1_800_000_600)
        token = request.headers["access-token"]
        order_tokens.append(token)
        if token == "old-token":
            await asyncio.sleep(0)
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"meta": {"totalCount": 0}, "data": []})

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )
    await asyncio.gather(
        service.get_orders_by_wallet(WALLET, config=CONFIG),
        service.get_orders_by_wallet(WALLET, config=CONFIG),
    )
    assert refresh_count == 2
    assert order_tokens.count("old-token") == 2
    assert order_tokens.count("new-token") == 2


async def test_authenticated_request_retries_only_one_explicit_401() -> None:
    refresh_count = 0
    order_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_count, order_count
        if request.url.path.endswith("/refresh-token"):
            refresh_count += 1
            return _token_response(f"token-{refresh_count}", 1_800_000_600)
        order_count += 1
        return httpx.Response(401, json={"error": "unauthorized"})

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )
    with pytest.raises(transak.TransakAPIError):
        await service.get_orders_by_wallet(WALLET, config=CONFIG)
    assert refresh_count == 2
    assert order_count == 2


async def test_expiry_uses_returned_unix_seconds_and_retains_bounded_previous_token() -> None:
    now = [1_800_000_000.0]
    refresh_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_count
        if request.url.path.endswith("/refresh-token"):
            refresh_count += 1
            return _token_response(f"token-{refresh_count}", int(now[0]) + 10)
        return httpx.Response(200, json={"meta": {"totalCount": 0}, "data": []})

    service = transak.TransakService(transport=httpx.MockTransport(handler), now=lambda: now[0])
    await service.get_orders_by_wallet(WALLET, config=CONFIG)
    now[0] += 10
    await service.get_orders_by_wallet(WALLET, config=CONFIG)
    assert refresh_count == 2
    assert service._previous_token is not None
    assert service._previous_token.value == "token-1"
    assert service._previous_token_valid_until == 1_800_000_010 + 900


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {}},
        {"data": {"accessToken": "", "expiresAt": 1_900_000_000}},
        {"data": {"accessToken": "token", "expiresAt": "tomorrow"}},
        {"data": {"accessToken": "token", "expiresAt": 1_900_000_000.5}},
        {"data": {"accessToken": "token\u2603", "expiresAt": 1_900_000_000}},
        {"data": {"accessToken": "token\nvalue", "expiresAt": 1_900_000_000}},
        {"data": {"accessToken": "token", "expiresAt": 1}},
    ],
)
async def test_malformed_token_response_fails_closed(payload) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )
    with pytest.raises(transak.TransakAPIError):
        await service.get_orders_by_wallet(WALLET, config=CONFIG)


@pytest.mark.parametrize(
    "raw_body",
    [
        b'{"data": 1e9999}',
        b'{"data": NaN}',
        b"[" * 2_000 + b"]" * 2_000,
        b'{"data":' + b"9" * 5_000 + b"}",
    ],
)
async def test_malformed_success_json_is_mapped_to_provider_error(raw_body) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw_body)

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )
    with pytest.raises(transak.TransakAPIError, match="malformed"):
        await service.get_orders_by_wallet(WALLET, config=CONFIG)


async def test_wallet_orders_use_default_dates_filters_and_pagination() -> None:
    intent = _intent()
    requests: list[httpx.Request] = []
    full_page = [_order(intent["transaction_id"], _id=f"order-{index}") for index in range(100)]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/refresh-token"):
            return _token_response("token", 1_800_000_600)
        skip = int(request.url.params["skip"])
        data = full_page if skip == 0 else [_order(intent["transaction_id"], _id="last")]
        return httpx.Response(200, json={"meta": {"totalCount": 101}, "data": data})

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_784_806_400,  # 2026-07-23T11:33:20Z
    )
    orders = await service.get_orders_by_wallet(WALLET, config=CONFIG)
    assert len(orders) == 101
    order_requests = [request for request in requests if request.url.path.endswith("/orders")]
    assert len(order_requests) == 2
    params = order_requests[0].url.params
    assert "startDate" not in params
    assert "endDate" not in params
    assert params["filter[productsAvailed]"] == '["BUY"]'
    assert params["filter[status]"] == "COMPLETED"
    assert params["filter[sortOrder]"] == "desc"
    assert params["filter[walletAddress]"] == WALLET
    assert [request.url.params["skip"] for request in order_requests] == ["0", "100"]


async def test_exact_order_lookup_is_one_capped_page() -> None:
    intent = _intent()
    issued_at = 1_784_806_400  # 2026-07-23T11:33:20Z
    order_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/refresh-token"):
            return _token_response("token", 1_800_000_600)
        order_requests.append(request)
        return httpx.Response(
            200,
            json={
                "meta": {"totalCount": 500},
                "data": [_order(intent["transaction_id"], _id=f"order-{i}") for i in range(100)],
            },
        )

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )
    await service.get_orders_by_partner_order_id(
        intent["transaction_id"],
        issued_at=issued_at,
        config=CONFIG,
    )
    assert len(order_requests) == 1
    params = order_requests[0].url.params
    assert params["filter[partnerOrderId]"] == intent["transaction_id"]
    assert params["startDate"] == "2026-07-23"
    assert params["endDate"] == "2026-08-23"


async def test_exact_order_lookup_uses_today_for_a_fresh_intent() -> None:
    intent = _intent()
    order_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/refresh-token"):
            return _token_response("token", 1_800_000_600)
        order_requests.append(request)
        return httpx.Response(200, json={"data": []})

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_784_806_400,  # 2026-07-23T11:33:20Z
    )
    await service.get_orders_by_partner_order_id(
        intent["transaction_id"],
        issued_at=1_784_806_400,
        config=CONFIG,
    )

    params = order_requests[0].url.params
    assert params["startDate"] == "2026-07-23"
    assert params["endDate"] == "2026-07-23"


async def test_exact_order_not_found_response_is_empty() -> None:
    intent = _intent()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/refresh-token"):
            return _token_response("token", 1_800_000_600)
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Invalid partnerOrderId or order not found",
                    "name": "Bad Request",
                }
            },
        )

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )

    assert (
        await service.get_orders_by_partner_order_id(
            intent["transaction_id"],
            issued_at=1_784_806_400,
            config=CONFIG,
        )
        == []
    )


@pytest.mark.parametrize(
    ("status_code", "payload", "error_type"),
    [
        (
            400,
            {
                "error": {
                    "message": (
                        "Date difference between start and end date should not be greater than "
                        "31 days"
                    )
                }
            },
            transak.TransakAPIError,
        ),
        (401, {"error": {"message": "Unauthorized"}}, transak.TransakAPIError),
        (429, {"error": {"message": "Rate limited"}}, transak.TransakRateLimitError),
        (500, {"error": {"message": "Unavailable"}}, transak.TransakAPIError),
    ],
)
async def test_exact_order_lookup_preserves_other_provider_errors(
    status_code, payload, error_type
) -> None:
    intent = _intent()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/refresh-token"):
            return _token_response("token", 1_800_000_600)
        return httpx.Response(status_code, json=payload)

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )

    with pytest.raises(error_type):
        await service.get_orders_by_partner_order_id(
            intent["transaction_id"],
            issued_at=1_784_806_400,
            config=CONFIG,
        )


async def test_wallet_lookup_does_not_hide_exact_not_found_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/refresh-token"):
            return _token_response("token", 1_800_000_600)
        return httpx.Response(
            400,
            json={"error": {"message": "Invalid partnerOrderId or order not found"}},
        )

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )

    with pytest.raises(transak.TransakAPIError):
        await service.get_orders_by_wallet(WALLET, config=CONFIG)


async def test_wallet_order_lookup_is_capped_at_ten_pages() -> None:
    intent = _intent()
    order_requests: list[httpx.Request] = []
    full_page = [_order(intent["transaction_id"], _id=f"order-{index}") for index in range(100)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/refresh-token"):
            return _token_response("token", 1_800_000_600)
        order_requests.append(request)
        return httpx.Response(200, json={"meta": {"totalCount": 2_000}, "data": full_page})

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )
    orders = await service.get_orders_by_wallet(WALLET, config=CONFIG)

    assert len(orders) == 1_000
    assert len(order_requests) == transak.TRANSAK_ORDER_MAX_PAGES
    assert [request.url.params["skip"] for request in order_requests] == [
        str(page * transak.TRANSAK_ORDER_PAGE_LIMIT)
        for page in range(transak.TRANSAK_ORDER_MAX_PAGES)
    ]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"data": "not-a-list"}, "malformed"),
        ({"meta": [], "data": []}, "malformed"),
        ({"meta": {"totalCount": "1"}, "data": []}, "malformed"),
        ({"meta": {}, "data": ["not-an-order"]}, "malformed"),
    ],
)
async def test_malformed_order_envelopes_are_rejected(payload, message) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/refresh-token"):
            return _token_response("token", 1_800_000_600)
        return httpx.Response(200, json=payload)

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )
    with pytest.raises(transak.TransakAPIError, match=message):
        await service.get_orders_by_wallet(WALLET, config=CONFIG)


async def test_order_page_cannot_exceed_requested_limit() -> None:
    intent = _intent()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/refresh-token"):
            return _token_response("token", 1_800_000_600)
        return httpx.Response(
            200,
            json={
                "meta": {"totalCount": 101},
                "data": [
                    _order(intent["transaction_id"], _id=f"order-{index}") for index in range(101)
                ],
            },
        )

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )
    with pytest.raises(transak.TransakAPIError, match="malformed"):
        await service.get_orders_by_wallet(WALLET, config=CONFIG)


async def test_oversized_response_and_429_are_bounded_and_mapped() -> None:
    mode = ["large"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/refresh-token"):
            return _token_response("token", 1_800_000_600)
        if mode[0] == "large":
            return httpx.Response(200, content=b"x" * (transak.TRANSAK_MAX_RESPONSE_BYTES + 1))
        return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "slow down"})

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )
    with pytest.raises(transak.TransakAPIError, match="too large"):
        await service.get_orders_by_wallet(WALLET, config=CONFIG)
    mode[0] = "rate"
    with pytest.raises(transak.TransakRateLimitError) as raised:
        await service.get_orders_by_wallet(WALLET, config=CONFIG)
    assert raised.value.retry_after == 7


async def test_redirect_or_server_error_is_not_retried_or_accepted() -> None:
    mode = ["redirect"]
    order_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal order_count
        if request.url.path.endswith("/refresh-token"):
            return _token_response("token", 1_800_000_600)
        order_count += 1
        status = 302 if mode[0] == "redirect" else 500
        return httpx.Response(status, json={"data": []})

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )
    with pytest.raises(transak.TransakAPIError):
        await service.get_orders_by_wallet(WALLET, config=CONFIG)
    mode[0] = "server-error"
    with pytest.raises(transak.TransakAPIError):
        await service.get_orders_by_wallet(WALLET, config=CONFIG)
    assert order_count == 2


def test_completed_order_is_normalized_with_provider_amounts_display_only() -> None:
    intent = _intent()
    record, reason = transak.transak_order_to_onramp_record(
        _order(intent["transaction_id"]),
        expected_user_address=USER,
        expected_wallet_address=WALLET,
        expected_token_id=TOKEN_ID,
        config=CONFIG,
    )
    assert reason is None
    assert record is not None
    assert record["provider"] == "transak"
    assert record["provider_transaction_id"] == "transak-order-1"
    assert record["provider_asset_code"] == "usdc"
    assert record["moonpay_transaction_id"] is None
    assert record["base_currency_code"] == "usd"
    assert record["base_currency_amount"] == "100"
    assert record["quote_currency_amount"] == "99.5"
    assert record["on_chain_tx_hash"] == TX_HASH


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"_id": None}, "missing_provider_transaction_id"),
        ({"partnerOrderId": None}, "missing_partner_order_id"),
        ({"partnerOrderId": "tampered"}, "invalid_partner_order_id"),
        ({"walletAddress": "0x" + "dd" * 20}, "transak_wallet_mismatch"),
        ({"status": "PENDING"}, "not_completed"),
        ({"isBuyOrSell": "SELL"}, "not_buy"),
        ({"cryptoCurrency": "ETH"}, "order_asset_mismatch"),
        ({"network": "ethereum"}, "network_mismatch"),
        ({"transactionHash": "DUMMY_TX_ID"}, "missing_on_chain_tx_hash"),
        ({"transactionHash": "0x1234"}, "missing_on_chain_tx_hash"),
    ],
)
def test_order_admission_rejects_mismatches(overrides, reason) -> None:
    intent = _intent()
    record, actual_reason = transak.transak_order_to_onramp_record(
        _order(intent["transaction_id"], **overrides),
        expected_user_address=USER,
        expected_wallet_address=WALLET,
        expected_token_id=TOKEN_ID,
        config=CONFIG,
    )
    assert record is None
    assert actual_reason == reason


def test_order_admission_rejects_wrong_owner_token_chain_and_exact_id() -> None:
    wrong_user = transak.create_transak_intent(
        user_address="0x" + "dd" * 20,
        wallet_address=WALLET,
        token_id=TOKEN_ID,
        chain_id=CONFIG.chain_id,
        config=CONFIG,
    )
    record, reason = transak.transak_order_to_onramp_record(
        _order(wrong_user["transaction_id"]),
        expected_user_address=USER,
        expected_wallet_address=WALLET,
        expected_token_id=TOKEN_ID,
        config=CONFIG,
    )
    assert record is None and reason == "user_address_mismatch"

    intent = _intent()
    record, reason = transak.transak_order_to_onramp_record(
        _order(intent["transaction_id"]),
        expected_user_address=USER,
        expected_wallet_address=WALLET,
        expected_token_id="0x" + "33" * 32,
        expected_transaction_id="different",
        config=CONFIG,
    )
    assert record is None and reason == "partner_order_id_mismatch"


def test_order_admission_rejects_other_provider_intent() -> None:
    transaction_id, _payload = onramp_intent.create_intent(
        provider=onramp_intent.PROVIDER_MOONPAY,
        user_address=USER,
        wallet_address=WALLET,
        token_id=TOKEN_ID,
        chain_id=CONFIG.chain_id,
        asset_code=CONFIG.canonical_asset_code,
    )
    record, reason = transak.transak_order_to_onramp_record(
        _order(transaction_id),
        expected_user_address=USER,
        expected_wallet_address=WALLET,
        expected_token_id=TOKEN_ID,
        config=CONFIG,
    )
    assert record is None and reason == "provider_mismatch"


def test_order_admission_distinguishes_intent_asset_mismatch() -> None:
    transaction_id, _payload = onramp_intent.create_intent(
        provider=onramp_intent.PROVIDER_TRANSAK,
        user_address=USER,
        wallet_address=WALLET,
        token_id=TOKEN_ID,
        chain_id=CONFIG.chain_id,
        asset_code="eth",
    )
    record, reason = transak.transak_order_to_onramp_record(
        _order(transaction_id),
        expected_user_address=USER,
        expected_wallet_address=WALLET,
        expected_token_id=TOKEN_ID,
        config=CONFIG,
    )
    assert record is None and reason == "intent_asset_mismatch"


def test_order_normalization_rejects_unsafe_text_and_nonfinite_timestamps() -> None:
    intent = _intent()
    record, reason = transak.transak_order_to_onramp_record(
        _order(
            intent["transaction_id"],
            _id="\ud800",
            fiatAmount="\ud800",
            createdAt=float("inf"),
            updatedAt=float("nan"),
        ),
        expected_user_address=USER,
        expected_wallet_address=WALLET,
        expected_token_id=TOKEN_ID,
        config=CONFIG,
    )
    assert record is None and reason == "missing_provider_transaction_id"

    record, reason = transak.transak_order_to_onramp_record(
        _order(
            intent["transaction_id"],
            fiatAmount="\ud800",
            createdAt=float("inf"),
            updatedAt=float("nan"),
        ),
        expected_user_address=USER,
        expected_wallet_address=WALLET,
        expected_token_id=TOKEN_ID,
        config=CONFIG,
    )
    assert reason is None
    assert record is not None
    assert record["base_currency_amount"] is None
    assert record["created_at"] == onramp_intent.decode_intent(intent["transaction_id"])["iat"]
    assert record["updated_at"] == record["created_at"]


async def test_webhook_accepts_current_and_bounded_previous_hs256_only() -> None:
    now = [1_800_000_000.0]
    service = transak.TransakService(now=lambda: now[0])
    current_token = "current-token-" + "c" * 32
    previous_token = "previous-token-" + "p" * 32
    current_expires_at = int(now[0]) + 60
    service._current_token = transak._AccessToken(current_token, current_expires_at)
    service._previous_token = transak._AccessToken(previous_token, int(now[0]) + 30)
    service._previous_token_valid_until = now[0] + 30
    claims = {
        "webhookData": {
            "id": "order-1",
            "status": "COMPLETED",
            "partnerOrderId": "sensitive-intent",
        },
        "eventID": "ORDER_COMPLETED",
    }
    for secret in (current_token, previous_token):
        body = json.dumps({"data": jwt.encode(claims, secret, algorithm="HS256")}).encode()
        assert await service.verify_webhook(body, config=CONFIG) == claims

    wrong_algorithm = json.dumps(
        {"data": jwt.encode(claims, current_token + "x" * 32, algorithm="HS512")}
    ).encode()
    with pytest.raises(transak.TransakWebhookVerificationError):
        await service.verify_webhook(wrong_algorithm, config=CONFIG)

    now[0] += 31
    previous = json.dumps({"data": jwt.encode(claims, previous_token, algorithm="HS256")}).encode()
    with pytest.raises(transak.TransakWebhookVerificationError):
        await service.verify_webhook(previous, config=CONFIG)

    current = json.dumps({"data": jwt.encode(claims, current_token, algorithm="HS256")}).encode()
    now[0] = current_expires_at + transak.TRANSAK_WEBHOOK_PREVIOUS_TOKEN_OVERLAP_SECONDS
    assert await service.verify_webhook(current, config=CONFIG) == claims

    now[0] += 1
    with pytest.raises(onramp_intent.OnRampNotConfiguredError):
        await service.verify_webhook(current, config=CONFIG)


async def test_webhook_with_empty_cache_does_not_refresh() -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return _token_response("new-token", 1_800_000_600)

    service = transak.TransakService(
        transport=httpx.MockTransport(handler),
        now=lambda: 1_800_000_000,
    )
    body = json.dumps(
        {
            "data": jwt.encode(
                {"webhookData": {}, "eventID": "ORDER_COMPLETED"},
                "unknown-token-" + "u" * 32,
                algorithm="HS256",
            )
        }
    ).encode()
    with pytest.raises(onramp_intent.OnRampNotConfiguredError):
        await service.verify_webhook(body, config=CONFIG)
    assert request_count == 0


async def test_webhook_rejects_non_ascii_jwt_before_verification() -> None:
    service = transak.TransakService(now=lambda: 1_800_000_000)
    service._current_token = transak._AccessToken("current-token", 1_800_000_600)
    with pytest.raises(onramp_intent.OnRampError, match="Invalid Transak webhook payload"):
        await service.verify_webhook(
            json.dumps({"data": "\u2603"}).encode(),
            config=CONFIG,
        )


async def test_webhook_jwt_is_bounded_before_verification() -> None:
    service = transak.TransakService(now=lambda: 1_800_000_000)
    body = json.dumps({"data": "a" * (transak.TRANSAK_MAX_WEBHOOK_JWT_BYTES + 1)}).encode()
    with pytest.raises(onramp_intent.OnRampError, match="Invalid Transak webhook payload"):
        await service.verify_webhook(body, config=CONFIG)


def test_webhook_log_summary_is_redacted() -> None:
    sensitive_intent = "privana_sensitive.signed"
    summary = transak.transak_webhook_log_summary(
        {
            "eventID": "ORDER_COMPLETED",
            "webhookData": {
                "id": "provider-order-1",
                "partnerOrderId": sensitive_intent,
                "walletAddress": WALLET,
                "transactionHash": TX_HASH,
                "status": "COMPLETED",
                "isBuyOrSell": "BUY",
            },
        }
    )
    assert summary["has_partner_order_id"] is True
    assert sensitive_intent not in repr(summary)
    assert WALLET not in repr(summary)

    unsafe = transak.transak_webhook_log_summary(
        {
            "eventID": "ORDER\nINJECT",
            "webhookData": {"status": "COMPLETED\nINJECT", "isBuyOrSell": "BUY\nINJECT"},
        }
    )
    assert unsafe["event_id"] is None
    assert unsafe["status"] is None
    assert unsafe["product"] is None
