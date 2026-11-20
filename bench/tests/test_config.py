from __future__ import annotations

from pathlib import Path

import pytest

from privana_bench.config import ConfigError, load, wallets

MNEMONIC = "test test test test test test test test test test test junk"
SETTINGS = {
    "PRIVANA_API_URL": "https://api.example/",
    "SAPPHIRE_RPC_URL": "https://rpc.example",
    "ACCOUNTING_ADDRESS": "0x910cffe4e8b27bc367f6e34d6d8e3c31ded68b6f",
    "TOKEN_ID": "0x" + "AB" * 32,
    "BENCH_MNEMONIC": MNEMONIC,
}
VARS = (*SETTINGS, "BENCH_FUNDER_INDEX")


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
    cfg = load(write_env(tmp_path, **SETTINGS))
    assert cfg.api_url == "https://api.example"
    assert cfg.accounting == "0x910CFfe4e8B27bc367F6E34D6D8e3C31DED68B6f"
    assert cfg.token_id == "0x" + "ab" * 32
    assert cfg.funder_index == 0
    assert "junk" not in repr(cfg)


def test_load_requires_every_setting(tmp_path: Path) -> None:
    settings = {k: v for k, v in SETTINGS.items() if k != "BENCH_MNEMONIC"}
    with pytest.raises(ConfigError, match="BENCH_MNEMONIC is not set"):
        load(write_env(tmp_path, **settings))


def test_load_names_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\.env\.localnet does not exist"):
        load(tmp_path / ".env.localnet")


def test_load_reads_the_funder_index(tmp_path: Path) -> None:
    assert load(write_env(tmp_path, **SETTINGS, BENCH_FUNDER_INDEX="1")).funder_index == 1


def test_load_refuses_a_funder_index_that_is_no_index(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="BENCH_FUNDER_INDEX"):
        load(write_env(tmp_path, **SETTINGS, BENCH_FUNDER_INDEX="-1"))


def test_wallets_are_two_hd_accounts_in_a_row() -> None:
    first, second, third = (
        "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
        "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",
    )
    assert [w.address for w in wallets(MNEMONIC)] == [first, second]
    assert [w.address for w in wallets(MNEMONIC, 1)] == [second, third]
