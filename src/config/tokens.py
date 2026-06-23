"""ROSE token id helper.

``ROSE_TOKEN_ID`` is a fixed ``bytes32`` constant on the on-chain
``Accounting`` contract (see ``solidity/contracts/Accounting.sol``). The
off-chain service treats the contract as the source of truth: it fetches
the value once per process and caches it. Recomputing it locally would
silently drift if the on-chain encoding ever changed.

The cache is intentionally process-local. ROFL service restarts re-fetch.
A benign race is possible if two coroutines hit a cold cache concurrently
— both will fetch, both will get the same immutable value, and the last
write wins. No lock is needed.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.services.accounting_contract import AccountingContractService


_rose_token_id_cache: bytes | None = None


async def get_rose_token_id(service: "AccountingContractService") -> bytes:
    """Return the on-chain ``ROSE_TOKEN_ID`` constant (cached)."""
    global _rose_token_id_cache
    if _rose_token_id_cache is None:
        reader = service._get_reader_contract()
        _rose_token_id_cache = await reader.functions.ROSE_TOKEN_ID().call()
    return _rose_token_id_cache


def _reset_rose_token_id_cache() -> None:
    """Reset the process-local cache. Tests only."""
    global _rose_token_id_cache
    _rose_token_id_cache = None
