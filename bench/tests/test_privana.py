from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from privana import AccountingApiError, NetworkError, ValidationError

from privana_bench import privana as module
from privana_bench.config import TESTNET_CHAIN_ID, wallets
from privana_bench.privana import SDK_TIMEOUT_S, LoginError, Privana, Reply, safe_detail

MNEMONIC = "test test test test test test test test test test test junk"
ACCOUNTING = "0x910CFfe4e8B27bc367F6E34D6D8e3C31DED68B6f"
TOKEN = "0x" + "ab" * 32


class FakeClient:
    """Stands in for the SDK's PrivanaClient."""

    instances: list[FakeClient] = []
    transfer_error: Exception | None = None
    first_errors: list[Exception] = []  # raised once each, before transfer_error
    login_error: Exception | None = None

    def __init__(self, base_url: str, timeout: float) -> None:
        self.timeout = timeout
        self.logins: list[tuple[str, str]] = []
        self.transfers: list[Any] = []
        FakeClient.instances.append(self)

    async def get_siwe_domain(self) -> Any:
        return SimpleNamespace(domains=["API.Testnet.Privana.Finance"])

    async def get_siwe_nonce(self, address: str) -> Any:
        return SimpleNamespace(nonce="n0nce")

    async def authenticate_private_reads(self, message: str, signature: str) -> None:
        if FakeClient.login_error:
            raise FakeClient.login_error
        self.logins.append((message, signature))

    async def transfer_funds(self, request: Any) -> None:
        self.transfers.append(request)
        error = FakeClient.first_errors.pop(0) if FakeClient.first_errors else None
        if error or FakeClient.transfer_error:
            raise error or FakeClient.transfer_error  # type: ignore[misc]

    async def list_tokens(self) -> Any:
        token = SimpleNamespace(
            token_id=TOKEN.upper().replace("0X", "0x"), decimals=6, symbol="USDC"
        )
        return SimpleNamespace(tokens=[token])


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Privana:
    FakeClient.instances = []
    FakeClient.transfer_error = None
    FakeClient.first_errors = []
    FakeClient.login_error = None
    monkeypatch.setattr(module, "PrivanaClient", FakeClient)
    return Privana("https://api.example", ACCOUNTING, TESTNET_CHAIN_ID)


def test_safe_detail_drops_inputs_and_masks_secrets() -> None:
    validation = [{"loc": ["body", "signature"], "msg": "bad", "input": {"signature": "0xabc"}}]
    assert safe_detail(validation) == "['body', 'signature']: bad"
    assert safe_detail("sig 0x" + "a" * 130 + " end") == "sig 0x… end"
    assert safe_detail("token eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl") == "token <jwt>"
    assert len(safe_detail("x" * 1000)) == 300


def test_siwe_message() -> None:
    text = module.siwe_message("api.example", "0xA", "n0nce", 23295, 1_800_000_000)
    assert text.splitlines() == [
        "api.example wants you to sign in with your Ethereum account:",
        "0xA",
        "",
        "Sign in to Privana on chain 23295",
        "",
        "URI: https://api.example",
        "Version: 1",
        "Chain ID: 23295",
        "Nonce: n0nce",
        "Issued At: 2027-01-15T08:00:00Z",
        "Expiration Time: 2027-01-16T07:00:00Z",  # 23 h later
    ]


def test_transfer_signature_recovers_as_privana_checks_it(api: Privana) -> None:
    """Privana builds the domain from the contract's ERC-5267 report (fields 0x1b)."""
    sender, receiver = wallets(MNEMONIC)
    signature = api.sign_transfer(sender, receiver.address, TOKEN, 10_000, 7)
    domain = {
        "name": "AccountingModule",
        "version": "1",
        "verifyingContract": ACCOUNTING,
        "salt": TESTNET_CHAIN_ID.to_bytes(32, "big"),
    }
    types = [
        {"name": "toAddress", "type": "address"},
        {"name": "tokenId", "type": "bytes32"},
        {"name": "amount", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
    ]
    message = {"toAddress": receiver.address, "tokenId": TOKEN, "amount": 10_000, "nonce": 7}
    signable = encode_typed_data(
        domain_data=domain, message_types={"Transfer": types}, message_data=message
    )
    assert Account.recover_message(signable, signature=signature) == sender.address


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (None, Reply("ok", 200)),
        (
            AccountingApiError("x", 400, "Transfer nonce mismatch"),
            Reply("refused", 400, "Transfer nonce mismatch"),
        ),
        (
            AccountingApiError("x", 400, "ROFL appd returned invalid CBOR response: x"),
            Reply("unknown", 400, "ROFL appd returned invalid CBOR response: x"),
        ),
        (AccountingApiError("x", 409, "outcome unseen"), Reply("unknown", 409, "outcome unseen")),
        (AccountingApiError("x", 422, "reverted"), Reply("refused", 422, "reverted")),
        (AccountingApiError("x", 429, "slow down"), Reply("refused", 429, "slow down")),
        (
            AccountingApiError("x", 500, "Failed to submit transaction"),
            Reply("unknown", 500, "Failed to submit transaction"),
        ),
        (AccountingApiError("x", 502, None), Reply("unknown", 502, "None")),
        (NetworkError("Request timeout after 90.0s"), Reply("unknown", None, "network: error")),
        (
            NetworkError("lost", ConnectionResetError()),
            Reply("unknown", None, "network: ConnectionResetError"),
        ),
        (
            NetworkError("cut", httpx.RemoteProtocolError("x")),
            Reply("unknown", None, "network: RemoteProtocolError"),
        ),
        (
            ValidationError("Token is not enabled"),
            Reply("refused", None, "SDK rejected it before sending: Token is not enabled"),
        ),
        (KeyError("submission_id"), Reply("unknown", None, "KeyError")),
    ],
)
async def test_transfer_reply_kinds(api: Privana, error: Exception | None, expected: Reply) -> None:
    sender, receiver = wallets(MNEMONIC)
    FakeClient.transfer_error = error
    reply = await api.transfer(sender, receiver.address, TOKEN, 1, 0, "0xsig")
    assert reply == expected
    assert len(FakeClient.instances[0].transfers) == 1


async def test_only_a_write_that_never_connected_goes_out_again(
    api: Privana, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "CONNECT_RETRY_S", 0.01)
    sender, receiver = wallets(MNEMONIC)
    no_connection = NetworkError("no connection", httpx.ConnectError("x"))
    FakeClient.first_errors = [no_connection]
    reply = await api.transfer(sender, receiver.address, TOKEN, 1, 0, "0xsig")
    assert reply == Reply("ok", 200, connect_failures=1)
    assert reply.retry_s >= 0.01
    sent = FakeClient.instances[0].transfers
    assert sent[0] is sent[1]  # the same signed request, not a new one
    FakeClient.transfer_error = no_connection
    reply = await api.transfer(sender, receiver.address, TOKEN, 1, 1, "0xsig")
    assert reply == Reply("refused", None, "never sent: ConnectError", connect_failures=3)
    assert len(sent) == 2 + 3
    FakeClient.first_errors = [no_connection]
    FakeClient.transfer_error = NetworkError("Request timeout after 90.0s")
    reply = await api.transfer(sender, receiver.address, TOKEN, 1, 2, "0xsig")
    assert reply == Reply("unknown", None, "network: error", connect_failures=1)
    assert len(sent) == 2 + 3 + 2  # a timeout is never tried again


async def test_sdk_timeout_is_above_the_api_wait(api: Privana) -> None:
    sender, receiver = wallets(MNEMONIC)
    await api.transfer(sender, receiver.address, TOKEN, 1, 0, "0xsig")
    assert FakeClient.instances[0].timeout == SDK_TIMEOUT_S > 60


async def test_login_signs_a_message_that_recovers(api: Privana) -> None:
    sender, receiver = wallets(MNEMONIC)
    await api.login(sender, 1_800_000_000)
    await api.login(receiver, 1_800_000_000)
    assert len(FakeClient.instances) == 2  # one client, so one login, per wallet
    message, signature = FakeClient.instances[0].logins[0]
    assert message.startswith("api.testnet.privana.finance wants you to sign in")
    assert f"\n{sender.address}\n" in message
    recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
    assert recovered == sender.address


async def test_login_rate_limit_is_explained(api: Privana) -> None:
    sender, _ = wallets(MNEMONIC)
    FakeClient.login_error = AccountingApiError("x", 429, "Too many requests")
    with pytest.raises(LoginError, match="10 per minute"):
        await api.login(sender, 1_800_000_000)


async def test_token_lookup(api: Privana) -> None:
    assert await api.token(TOKEN) == (6, "USDC")
    assert await api.token("0x" + "cd" * 32) is None
