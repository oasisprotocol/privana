from __future__ import annotations

from pathlib import Path

import pytest

from privana_bench.config import ConfigError, load, wallets

MNEMONIC = "test test test test test test test test test test test junk"
VARS = ("PRIVANA_API_URL", "SAPPHIRE_RPC_URL", "ACCOUNTING_ADDRESS", "TOKEN_ID", "BENCH_MNEMONIC")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # setenv first so teardown removes whatever load_dotenv adds
    for name in VARS:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def write_env(tmp_path: Path, **values: str) -> Path:
    path = tmp_path / ".env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return path


def test_load_normalizes_and_hides_the_mnemonic(tmp_path: Path) -> None:
    cfg = load(
        write_env(
            tmp_path,
            PRIVANA_API_URL="https://api.example/",
            SAPPHIRE_RPC_URL="https://rpc.example",
            ACCOUNTING_ADDRESS="0x910cffe4e8b27bc367f6e34d6d8e3c31ded68b6f",
            TOKEN_ID="0x" + "AB" * 32,
            BENCH_MNEMONIC=MNEMONIC,
        )
    )
    assert cfg.api_url == "https://api.example"
    assert cfg.accounting == "0x910CFfe4e8B27bc367F6E34D6D8e3C31DED68B6f"
    assert cfg.token_id == "0x" + "ab" * 32
    assert "junk" not in repr(cfg)


def test_load_requires_every_setting(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="BENCH_MNEMONIC is not set"):
        load(
            write_env(
                tmp_path,
                PRIVANA_API_URL="https://api.example",
                SAPPHIRE_RPC_URL="https://rpc.example",
                ACCOUNTING_ADDRESS="0x910cffe4e8b27bc367f6e34d6d8e3c31ded68b6f",
                TOKEN_ID="0x" + "ab" * 32,
            )
        )


def test_wallets_are_the_first_two_hd_accounts() -> None:
    sender, receiver = wallets(MNEMONIC)
    assert sender.address == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    assert receiver.address == "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
