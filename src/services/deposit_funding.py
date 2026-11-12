"""Find the transfers that funded a deposit address, via the Alchemy Transfers API.

Used by POST /deposits/check-all to turn an address balance back into the
(tx_hash, amount, log_index) triples that POST /deposits/check verifies. Alchemy
finds the funding transactions; an ERC20 transfer's log index is read from the
transaction receipt, not from Alchemy's transfer id. Native transfers include internal
calls (multisig, forwarding contracts) where the chain's endpoint lists them; a
transaction is one deposit, as in /deposits/check.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from web3 import AsyncWeb3

from src.config.chain_config import TRANSFER_EVENT_TOPIC

logger = logging.getLogger(__name__)

# One small page normally covers the balance: deposits to one address are few.
PAGE_SIZE = 100
# Transfers collected per asset. The newest ones are taken first, so spam to the address
# drops the oldest, never a fresh deposit.
MAX_TRANSFERS = 50

_ERC20_BALANCE_OF_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]


class UnsupportedChainError(Exception):
    """The chain's Alchemy endpoint does not serve the Transfers API."""


@dataclass(frozen=True)
class FundingTransfer:
    """One transfer to the deposit address, shaped for POST /deposits/check."""

    tx_hash: str
    amount: int
    log_index: int  # 0 for native


async def get_balance(w3: AsyncWeb3, token_address: Optional[str], holder: str) -> int:
    """Balance of ``holder``; ``token_address`` None is the native balance."""
    if token_address is None:
        return await w3.eth.get_balance(holder)
    contract = w3.eth.contract(
        address=w3.to_checksum_address(token_address), abi=_ERC20_BALANCE_OF_ABI
    )
    return await contract.functions.balanceOf(w3.to_checksum_address(holder)).call()


async def get_funding_transfers(
    w3: AsyncWeb3,
    holder: str,
    token_address: Optional[str],
    balance: int,
    min_amount: int,
) -> list[FundingTransfer]:
    """Newest transfers to ``holder`` that add up to ``balance``, oldest first.

    Transfers below ``min_amount`` are skipped: /deposits/check rejects them anyway, and
    they must not count towards the balance (gas funding leaves dust on the address).

    Raises:
        UnsupportedChainError: If the chain's endpoint has no Transfers API.
    """
    params: dict[str, Any] = {
        "toAddress": holder,
        "category": ["external", "internal"] if token_address is None else ["erc20"],
        "order": "desc",
        "excludeZeroValue": True,
        "maxCount": hex(PAGE_SIZE),
    }
    if token_address is not None:
        params["contractAddresses"] = [token_address]

    found: list[tuple[str, int, str]] = []  # (tx_hash, amount, category), newest first
    total = 0
    while total < balance and len(found) < MAX_TRANSFERS:
        response = await w3.provider.make_request("alchemy_getAssetTransfers", [params])
        if "error" in response:
            message = response["error"].get("message", "")
            if "EAPIs not enabled" in message:
                raise UnsupportedChainError(message)
            if "internal" in params["category"]:
                # Not every chain lists internal transfers: fall back to external ones.
                logger.warning("Internal transfers not listed, using external only: %s", message)
                params["category"] = ["external"]
                continue
            raise RuntimeError(f"alchemy_getAssetTransfers failed: {message}")
        result = response["result"]

        for transfer in result["transfers"]:
            amount = int(transfer["rawContract"]["value"], 16)
            if amount < min_amount:
                continue
            found.append((transfer["hash"], amount, transfer["category"]))
            total += amount
            if total >= balance or len(found) >= MAX_TRANSFERS:
                break

        page_key = result.get("pageKey")
        if page_key is None:
            break
        params["pageKey"] = page_key

    if token_address is None:
        # A transaction is claimed once: its top-level value, else its internal transfers.
        per_tx: dict[str, list[int]] = {}  # tx_hash -> [external, internal], newest first
        for tx_hash, amount, category in found:
            per_tx.setdefault(tx_hash, [0, 0])[category == "internal"] += amount
        return [
            FundingTransfer(tx_hash, external or internal, 0)
            for tx_hash, (external, internal) in reversed(per_tx.items())
        ]
    return await _with_log_indexes(
        w3, holder, token_address, [(h, a) for h, a, _ in reversed(found)]
    )


async def _with_log_indexes(
    w3: AsyncWeb3, holder: str, token_address: str, found: list[tuple[str, int]]
) -> list[FundingTransfer]:
    """Attach each transfer's log index, read from its transaction's receipt."""
    receipts = await asyncio.gather(*(w3.eth.get_transaction_receipt(h) for h in dict(found)))
    # tx hash -> the (log_index, amount) of its Transfer events from the token to the holder
    logs = {
        tx_hash: _transfer_logs(receipt, holder, token_address)
        for tx_hash, receipt in zip(dict(found), receipts)
    }
    transfers = []
    for tx_hash, amount in found:
        # A transaction can hold several equal transfers: each takes its own log.
        candidates = logs[tx_hash]
        index = next((i for i, (_, a) in enumerate(candidates) if a == amount), None)
        if index is None:
            logger.warning("No matching Transfer log for %s (amount %d)", tx_hash, amount)
            continue
        transfers.append(FundingTransfer(tx_hash, amount, candidates.pop(index)[0]))
    return transfers


def _transfer_logs(receipt: Any, holder: str, token_address: str) -> list[tuple[int, int]]:
    holder_lower, token_lower = holder.lower(), token_address.lower()
    logs = []
    for entry in receipt["logs"]:
        topics = [t.hex() if isinstance(t, bytes) else t for t in entry["topics"]]
        if len(topics) < 3 or topics[0].removeprefix("0x").lower() != TRANSFER_EVENT_TOPIC[2:]:
            continue
        address = entry["address"]
        if (
            address if isinstance(address, str) else "0x" + address.hex().removeprefix("0x")
        ).lower() != token_lower:
            continue
        if "0x" + topics[2].removeprefix("0x")[-40:].lower() != holder_lower:
            continue
        data = entry["data"]
        amount = int.from_bytes(data[:32], "big") if isinstance(data, bytes) else int(data, 16)
        logs.append((entry["logIndex"], amount))
    return logs
