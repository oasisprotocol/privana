"""Settings from bench/.env. The mnemonic stays in memory and is never printed."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils.address import to_checksum_address

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
TESTNET_CHAIN_ID = 23295


class ConfigError(Exception):
    pass


class PreflightError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    api_url: str
    rpc_url: str
    accounting: str
    token_id: str
    mnemonic: str = field(repr=False)


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set (see .env.example)")
    return value


def load(env_file: Path = ENV_FILE) -> Config:
    load_dotenv(env_file)
    return Config(
        api_url=_require("PRIVANA_API_URL").rstrip("/"),
        rpc_url=_require("SAPPHIRE_RPC_URL"),
        accounting=to_checksum_address(_require("ACCOUNTING_ADDRESS")),
        token_id=_require("TOKEN_ID").lower(),
        mnemonic=_require("BENCH_MNEMONIC"),
    )


def wallets(mnemonic: str) -> tuple[LocalAccount, LocalAccount]:
    """The sender (index 0) and the receiver (index 1)."""
    Account.enable_unaudited_hdwallet_features()
    sender = Account.from_mnemonic(mnemonic, account_path="m/44'/60'/0'/0/0")
    receiver = Account.from_mnemonic(mnemonic, account_path="m/44'/60'/0'/0/1")
    return sender, receiver


def indexed_wallets(mnemonic: str, first: int, n: int) -> list[LocalAccount]:
    """The wallets at indices first to first + n - 1."""
    Account.enable_unaudited_hdwallet_features()
    return [
        Account.from_mnemonic(mnemonic, account_path=f"m/44'/60'/0'/0/{i}")
        for i in range(first, first + n)
    ]
