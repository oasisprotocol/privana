"""Deposit verification: reads source chain via RPC, confirms deposit, extracts amount.

The ROFL TEE verifies deposits off-chain before crediting. This replaces
on-chain Merkle proof verification.
"""

import logging
from dataclasses import dataclass
from typing import Dict, Optional

from web3 import AsyncWeb3

from src.config.chain_config import (
    TRANSFER_EVENT_TOPIC,
    get_finality_depth,
)
from src.services.rpc_identity import require_verified_web3

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerifiedDeposit:
    """Result of a successful deposit verification."""

    chain_id: int
    tx_hash: str
    amount: int
    is_native: bool
    token_address: Optional[str]  # None for native
    deposit_index: int  # logIndex for ERC20, 0 for native
    block_number: int

    def __post_init__(self):
        if self.is_native and self.token_address is not None:
            raise ValueError("Native deposit must have token_address=None")
        if not self.is_native and not self.token_address:
            raise ValueError("ERC20 deposit requires token_address")


class DepositVerifier:
    """Verifies deposits by reading source chain via RPC.

    Checks:
    1. Transaction exists and succeeded (status == 1)
    2. Sufficient confirmations (per-chain finality depth)
    3. Deposit arrived at the expected deposit address
    4. Extracts amount from tx value (native) or Transfer log (ERC20)
    """

    def __init__(self, chain_rpc_urls: Dict[int, str]):
        self._chain_rpc_urls = chain_rpc_urls
        self._web3_cache: Dict[int, AsyncWeb3] = {}

    def _get_web3(self, chain_id: int) -> AsyncWeb3:
        """Get the chain's startup-verified client (see `rpc_identity`).

        A chain missing here is unconfigured or was excluded by that check; both fail
        closed rather than verify a deposit against a possibly different chain.
        """
        return require_verified_web3(chain_id, self._chain_rpc_urls, self._web3_cache)

    async def verify_deposit(
        self,
        chain_id: int,
        tx_hash: str,
        deposit_address: str,
        expected_amount: int,
        log_index: int = 0,
    ) -> VerifiedDeposit:
        """Verify a deposit transaction and return the verified deposit info.

        The caller declares the amount they expect to be credited. The verifier
        confirms on-chain evidence supports at least that amount, then returns
        ``expected_amount`` as the verified amount. This prevents block-scoped
        balance deltas from inflating credited amounts.

        Args:
            chain_id: Source chain ID
            tx_hash: Transaction hash on the source chain
            deposit_address: Expected deposit address (derived from beneficiary)
            expected_amount: Caller-declared deposit amount (wei). On-chain
                evidence must be >= this value.
            log_index: Log index for ERC20 deposits (0 for native)

        Returns:
            VerifiedDeposit with amount set to ``expected_amount``.

        Raises:
            ValueError: If deposit verification fails (reverted, insufficient
                        finality, no deposit found, amount mismatch, etc.)
        """
        w3 = self._get_web3(chain_id)
        deposit_addr_lower = deposit_address.lower()

        # 1. Fetch tx receipt
        receipt = await w3.eth.get_transaction_receipt(tx_hash)
        if receipt is None:
            raise ValueError(f"Transaction {tx_hash} not found on chain {chain_id}")

        if receipt["status"] != 1:
            raise ValueError(f"Transaction {tx_hash} reverted (status=0)")

        tx_block = receipt["blockNumber"]

        # 2. Check finality
        latest_block = await w3.eth.get_block("latest")
        confirmations = latest_block["number"] - tx_block
        required = get_finality_depth(chain_id)
        if confirmations < required:
            raise ValueError(
                f"Insufficient finality: {confirmations}/{required} confirmations "
                f"on chain {chain_id}"
            )

        # 3a. Collect all ERC20 Transfer events to the deposit address
        matching_transfers: list[tuple[int, dict]] = []
        for log_entry in receipt.get("logs", []):
            topics = log_entry.get("topics", [])
            if len(topics) < 3:
                continue
            topic0 = topics[0]
            if isinstance(topic0, bytes):
                topic0 = "0x" + topic0.hex()
            if topic0.lower() != TRANSFER_EVENT_TOPIC.lower():
                continue
            # Decode Transfer(from, to, amount)
            to_topic = topics[2]
            if isinstance(to_topic, bytes):
                to_addr = "0x" + to_topic[-20:].hex()
            else:
                to_addr = "0x" + to_topic[-40:]

            if to_addr.lower() == deposit_addr_lower:
                matching_transfers.append((log_entry.get("logIndex", 0), log_entry))

        if matching_transfers:
            if len(matching_transfers) == 1:
                actual_log_index, log_entry = matching_transfers[0]
            else:
                # Multiple Transfer events to the deposit address — match by
                # EVM logIndex (position within the block's log list)
                match = [(i, e) for i, e in matching_transfers if i == log_index]
                if not match:
                    valid = [i for i, _ in matching_transfers]
                    raise ValueError(
                        f"No Transfer event with logIndex={log_index} "
                        f"in tx {tx_hash} on chain {chain_id}. "
                        f"Valid logIndex values: {valid}"
                    )
                actual_log_index, log_entry = match[0]

            data = log_entry.get("data", b"")
            if isinstance(data, bytes):
                on_chain_amount = int.from_bytes(data[:32], "big")
            else:
                on_chain_amount = int(data, 16) if data else 0
            token_addr = log_entry.get("address", "")
            if isinstance(token_addr, bytes):
                token_addr = "0x" + token_addr.hex()

            if on_chain_amount < expected_amount:
                raise ValueError(
                    f"On-chain Transfer amount ({on_chain_amount}) is less than "
                    f"claimed amount ({expected_amount}) in tx {tx_hash}"
                )

            logger.info(
                "Verified ERC20 deposit: chain=%d tx=%s claimed=%d on_chain=%d token=%s logIndex=%d",
                chain_id,
                tx_hash,
                expected_amount,
                on_chain_amount,
                token_addr,
                actual_log_index,
            )
            return VerifiedDeposit(
                chain_id=chain_id,
                tx_hash=tx_hash,
                amount=expected_amount,
                is_native=False,
                token_address=token_addr,
                deposit_index=actual_log_index,
                block_number=tx_block,
            )

        # 3b. Check for native ETH deposit (log_index == 0 signals native claim)
        if log_index == 0:
            # Fast path: direct top-level transfer (tx.to == deposit_address)
            tx = await w3.eth.get_transaction(tx_hash)
            tx_to = tx.get("to", "")
            if isinstance(tx_to, bytes):
                tx_to = "0x" + tx_to.hex()

            if tx_to and tx_to.lower() == deposit_addr_lower:
                value = tx.get("value", 0)
                if value > 0:
                    if value < expected_amount:
                        raise ValueError(
                            f"On-chain tx value ({value}) is less than "
                            f"claimed amount ({expected_amount}) in tx {tx_hash}"
                        )
                    logger.info(
                        "Verified native deposit (direct): chain=%d tx=%s claimed=%d on_chain=%d",
                        chain_id,
                        tx_hash,
                        expected_amount,
                        value,
                    )
                    return VerifiedDeposit(
                        chain_id=chain_id,
                        tx_hash=tx_hash,
                        amount=expected_amount,
                        is_native=True,
                        token_address=None,
                        deposit_index=0,
                        block_number=tx_block,
                    )

            # Fallback: balance-delta detection for internal calls (multisig,
            # contract forwarding, etc.). Compares deposit address balance before
            # and after the tx's block to catch ETH that arrived via internal
            # EVM calls rather than top-level tx.value.
            # If multiple of these tranactions land in the same block, tx_hash
            # attribution can be off, but the integrity of deposits and onchain
            # flows stays the same.
            balance_before = await w3.eth.get_balance(
                deposit_address, block_identifier=tx_block - 1
            )
            balance_after = await w3.eth.get_balance(deposit_address, block_identifier=tx_block)
            delta = balance_after - balance_before

            if delta >= expected_amount:
                logger.info(
                    "Verified native deposit (balance-delta): chain=%d tx=%s claimed=%d delta=%d",
                    chain_id,
                    tx_hash,
                    expected_amount,
                    delta,
                )
                return VerifiedDeposit(
                    chain_id=chain_id,
                    tx_hash=tx_hash,
                    amount=expected_amount,
                    is_native=True,
                    token_address=None,
                    deposit_index=0,
                    block_number=tx_block,
                )

        raise ValueError(
            f"No deposit found to {deposit_address} in tx {tx_hash} "
            f"(log_index={log_index}) on chain {chain_id}"
        )
