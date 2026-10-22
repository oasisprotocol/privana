"""Privana through the published SDK, the way an integrator calls it.

No client gets a token provider, so the SDK never replays a request. A write
goes out again only when its connection never opened, here, or after a lost
reply in the one-after-another phases (transfers.py).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from privana import (
    AccountingApiError,
    LockFundsRequest,
    LockMessage,
    NetworkError,
    PrivanaClient,
    SignLockParams,
    SignTransferLockedParams,
    SignTransferParams,
    TransferFundsRequest,
    TransferLockedFundsRequest,
    TransferLockedMessage,
    TransferMessage,
    ValidationError,
    sign_lock_message,
    sign_transfer_locked_message,
    sign_transfer_message,
)

# Above the API's own 60 s wait on the enclave, so its reply is what we see.
SDK_TIMEOUT_S = 90.0
# Long runs outlast an hour. Privana accepts up to its token validity, 24 h on testnet.
SIWE_TTL_S = 23 * 3600
NEVER_SENT = "never sent: ConnectError"
CONNECT_TRIES = 3
CONNECT_RETRY_S = 2.0

ReplyKind = Literal["ok", "refused", "unknown"]


@dataclass(frozen=True)
class Reply:
    """What the API said about one write.

    refused: a 4xx, the SDK rejected it before sending, or the connection never
    opened on any try. Privana did not queue it, or it reverted.
    unknown: a 5xx, a 409, a 400 Privana raised after sending, a timeout or a
    lost connection. It can still land.
    connect_failures: tries whose connection never opened, so nothing went out.
    retry_s: seconds from the first try to the last one, so a reply time can
    count from the try that went out.
    """

    kind: ReplyKind
    status: int | None = None
    detail: str = ""
    connect_failures: int = 0
    retry_s: float = field(default=0.0, compare=False)


class LoginError(Exception):
    pass


async def submit(write: Callable[[], Awaitable[object]]) -> Reply:
    """Send one SDK write and classify the reply. A try whose connection never
    opened sent nothing, so the same write goes out again, up to CONNECT_TRIES."""
    first = time.monotonic()
    for tries in range(1, CONNECT_TRIES + 1):
        tried = time.monotonic() - first
        reply = await _reply(write())
        if reply.detail != NEVER_SENT:
            return replace(reply, connect_failures=tries - 1, retry_s=tried)
        if tries < CONNECT_TRIES:
            await asyncio.sleep(CONNECT_RETRY_S)
    return replace(reply, connect_failures=CONNECT_TRIES, retry_s=tried)


async def _reply(write: Awaitable[object]) -> Reply:
    try:
        await write
    except AccountingApiError as exc:
        # The 4xx replies raised after the write went out: a 409, submitted without a
        # seen outcome, and a 400 naming ROFL, whose enclave reply did not parse.
        sent = exc.status_code == 409 or (exc.status_code == 400 and "ROFL" in str(exc.detail))
        kind: ReplyKind = "refused" if 400 <= exc.status_code < 500 and not sent else "unknown"
        return Reply(kind, exc.status_code, safe_detail(exc.detail))
    except ValidationError as exc:
        return Reply("refused", None, f"SDK rejected it before sending: {safe_detail(exc)}")
    except NetworkError as exc:
        if isinstance(exc.__cause__, httpx.ConnectError):
            # The connection never opened, so the request never went out.
            return Reply("refused", None, NEVER_SENT)
        cause = type(exc.__cause__).__name__ if exc.__cause__ else "error"
        return Reply("unknown", None, f"network: {cause}")
    except Exception as exc:
        return Reply("unknown", None, type(exc).__name__)
    return Reply("ok", 200)


def safe_detail(detail: Any) -> str:
    """An error detail fit for the journal and the terminal.

    FastAPI's validation errors echo each rejected `input`, which can be the
    whole signed body, so only where and why are kept. Long hex (signatures)
    and JWTs are masked.
    """
    if isinstance(detail, list):
        text = "; ".join(
            f"{e.get('loc')}: {e.get('msg')}" if isinstance(e, dict) else "?" for e in detail
        )
    else:
        text = str(detail)
    text = re.sub(r"0x[0-9a-fA-F]{100,}", "0x…", text)
    text = re.sub(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", "<jwt>", text)
    return text[:300]


def siwe_message(domain: str, address: str, nonce: str, chain_id: int, now: float) -> str:
    """The EIP-4361 text Privana accepts. The statement must name the Sapphire chain."""

    def iso(ts: float) -> str:
        return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    return (
        f"{domain} wants you to sign in with your Ethereum account:\n{address}\n\n"
        f"Sign in to Privana on chain {chain_id}\n\n"
        f"URI: https://{domain}\nVersion: 1\nChain ID: {chain_id}\nNonce: {nonce}\n"
        f"Issued At: {iso(now)}\nExpiration Time: {iso(now + SIWE_TTL_S)}"
    )


class Privana:
    def __init__(self, api_url: str, accounting: str, chain_id: int) -> None:
        self._api_url = api_url
        self._accounting = accounting
        self._chain_id = chain_id
        self._clients: dict[str, PrivanaClient] = {}

    def client(self, address: str) -> PrivanaClient:
        """One client per wallet, so each holds only its own login."""
        key = address.lower()
        if key not in self._clients:
            self._clients[key] = PrivanaClient(base_url=self._api_url, timeout=SDK_TIMEOUT_S)
        return self._clients[key]

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()

    async def token(self, token_id: str) -> tuple[int, str] | None:
        """The token's decimals and symbol if this deployment lists it, else None."""
        listing = await self.client("public").list_tokens()
        for token in listing.tokens:
            if token.token_id.lower() == token_id and token.decimals is not None:
                return token.decimals, token.symbol or "?"
        return None

    async def transfer_nonce(self, address: str) -> int:
        """The nonce the API's own pre-check reads."""
        return int((await self.client("public").get_transfer_nonce(address)).nonce)

    async def login(self, account: LocalAccount, now: float) -> None:
        """SIWE login for private reads. The signature is checked locally first,
        because one that does not recover sends Privana down a slow fallback path."""
        client = self.client(account.address)
        try:
            domain = (await client.get_siwe_domain()).domains[0].lower()
            nonce = (await client.get_siwe_nonce(account.address)).nonce
            message = siwe_message(domain, account.address, nonce, self._chain_id, now)
            signable = encode_defunct(text=message)
            signature = account.sign_message(signable).signature
            if Account.recover_message(signable, signature=signature) != account.address:
                raise LoginError(f"local SIWE signature check failed for {account.address}")
            await client.authenticate_private_reads(message, "0x" + signature.hex())
        except AccountingApiError as exc:
            hint = (
                " (logins share a 10 per minute limit, retry shortly)"
                if exc.status_code == 429
                else ""
            )
            raise LoginError(
                f"login {account.address}: {exc.status_code} {safe_detail(exc.detail)}{hint}"
            ) from None

    async def transfer_locked_nonce(self, service: str) -> int:
        return int((await self.client("public").get_transfer_locked_nonce(service)).nonce)

    async def balance(self, account: LocalAccount, token_id: str) -> int:
        return int((await self.client(account.address).get_balance(token_id)).balance)

    def sign_transfer(
        self, account: LocalAccount, to: str, token_id: str, amount: int, nonce: int
    ) -> str:
        message = TransferMessage(to_address=to, token_id=token_id, amount=amount, nonce=nonce)
        params = SignTransferParams(
            account=account,
            network="testnet",
            verifying_contract=self._accounting,
            message=message,
        )
        return sign_transfer_message(params)

    async def transfer(
        self,
        account: LocalAccount,
        to: str,
        token_id: str,
        amount: int,
        nonce: int,
        signature: str,
    ) -> Reply:
        request = TransferFundsRequest(
            to_address=to, token_id=token_id, amount=amount, nonce=nonce, signature=signature
        )
        return await submit(lambda: self.client(account.address).transfer_funds(request))

    async def lock(
        self,
        account: LocalAccount,
        service: str,
        token_id: str,
        amount: int,
        expiry: int,
        nonce: int,
    ) -> Reply:
        """Sign a lock of `amount` for `service` and send it once."""
        message = LockMessage(
            service_address=service, token_id=token_id, amount=amount, expiry=expiry, nonce=nonce
        )
        params = SignLockParams(
            account=account,
            network="testnet",
            verifying_contract=self._accounting,
            message=message,
        )
        request = LockFundsRequest(
            service_address=service,
            token_id=token_id,
            amount=amount,
            expiry=expiry,
            nonce=nonce,
            signature=sign_lock_message(params),
        )
        return await submit(lambda: self.client(account.address).lock_funds(request))

    async def settle(
        self,
        service: LocalAccount,
        user: str,
        to: str,
        lock_id: int,
        amount: int,
        nonce: int,
    ) -> Reply:
        """Sign, as the service, a move of `amount` from `user`'s lock to `to`, and send it once."""
        message = TransferLockedMessage(
            user_address=user,
            to_address=to,
            lock_id=lock_id,
            amount=amount,
            nonce=nonce,
            service_address=service.address,
        )
        params = SignTransferLockedParams(
            account=service,
            network="testnet",
            verifying_contract=self._accounting,
            message=message,
        )
        request = TransferLockedFundsRequest(
            user_address=user,
            lock_id=lock_id,
            to_address=to,
            amount=amount,
            service_address=service.address,
            nonce=nonce,
            signature=sign_transfer_locked_message(params),
        )
        return await submit(lambda: self.client(service.address).transfer_locked_funds(request))
