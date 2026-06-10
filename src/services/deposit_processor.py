"""Deposit processor: orchestrates verification → sweep → credit.

Called from the /deposits/check API route. Coordinates the deposit verifier,
sweep engine, and accounting contract service.

The sweep lifecycle runs in a background task so the API responds within
the ROFL HTTP proxy timeout (~30s). Clients poll GET /deposits/status/{key}.
"""

import asyncio
import logging
from typing import Dict

from web3 import Web3

from src.config import load_settings
from src.config.chain_config import MIN_DEPOSIT_ERC20_WEI, MIN_DEPOSIT_NATIVE_WEI
from src.services.accounting_contract import get_accounting_contract_service
from src.services.deposit_verifier import DepositVerifier
from src.services.sweep_engine import (
    DepositAccountingProtocol,
    SweepCreditPendingError,
    SweepEngine,
    _to_hex,
)

logger = logging.getLogger(__name__)


# Tasks cancelled before their first SweepRecord checkpoint (inside
# sweep_native/sweep_erc20, after the balance read + gas-price fetch) leave
# nothing on disk — the client saw "pending" but recovery has nothing to resume.
BACKGROUND_SWEEP_SHUTDOWN_TIMEOUT = 60


def compute_deposit_id(chain_id: int, tx_hash: str, token_id: bytes, deposit_index: int) -> bytes:
    """Compute the deposit dedup key: keccak256(chainId, txHash, tokenId, depositIndex).

    All arguments are fixed-size types, so abi.encode and abi.encodePacked produce
    identical output. Safe to use solidity_keccak here.
    """
    tx_hash_bytes = bytes.fromhex(tx_hash.removeprefix("0x"))
    return Web3.solidity_keccak(
        ["uint256", "bytes32", "bytes32", "uint256"],
        [chain_id, tx_hash_bytes, token_id, deposit_index],
    )


class DepositProcessor:
    """Orchestrates the full deposit flow: verify → sweep → credit."""

    def __init__(
        self,
        verifier: DepositVerifier,
        sweep_engine: SweepEngine,
        accounting_service: DepositAccountingProtocol,
    ):
        self._verifier = verifier
        self._sweep = sweep_engine
        self._accounting = accounting_service
        # Strong refs — asyncio.create_task returns can be GC'd without one.
        self._background_tasks: set[asyncio.Task] = set()

    async def resume_incomplete_sweeps(self) -> None:
        """Resume incomplete sweeps after TEE restart."""
        await self._sweep.resume_incomplete_sweeps()

    def start_recovery_loop(self) -> None:
        """Start periodic retry for SWEPT records whose credit failed."""
        self._sweep.start_recovery_loop()

    async def stop(self) -> None:
        """Stop the recovery loop and wait for in-flight background sweeps.

        Uses asyncio.wait (NOT wait_for) — tasks must not be cancelled on
        timeout; the event loop outlives the timeout and the next recovery
        pass will pick up their persisted state.
        """
        await self._sweep.stop_recovery_loop()
        if not self._background_tasks:
            return
        in_flight = list(self._background_tasks)
        logger.info("Awaiting %d in-flight background sweep(s)...", len(in_flight))
        _, still_pending = await asyncio.wait(in_flight, timeout=BACKGROUND_SWEEP_SHUTDOWN_TIMEOUT)
        if still_pending:
            logger.warning(
                "Shutdown timeout — %d sweep(s) still in flight; "
                "state is persisted, recovery will resume on next start",
                len(still_pending),
            )

    async def _get_token_id(self, chain_id: int, token_address: str | None) -> bytes:
        """Get tokenId via AccountingContractService — avoids abi.encode mismatch."""
        return await self._accounting.get_token_id(chain_id, token_address)

    @staticmethod
    def _deposit_response(
        status: str,
        deposit_id_hex: str,
        amount: int,
        token_address: str | None,
        detail: str | None = None,
    ) -> Dict:
        resp: Dict = {
            "status": status,
            "deposit_id": deposit_id_hex,
            "amount": str(amount),
            "token_address": token_address,
        }
        if detail is not None:
            resp["detail"] = detail
        return resp

    async def process_deposit(
        self,
        chain_type: str,
        chain_id: int,
        tx_hash: str,
        amount: int,
        log_index: int,
        version: int,
        siwe_token: bytes,
        beneficiary: str,
    ) -> Dict:
        """Verify a deposit and start the sweep in the background.

        Returns immediately after verification (~2-3s). The actual sweep
        runs as a background asyncio task. Clients poll
        GET /deposits/status/{deposit_id} for completion.

        Idempotent: if the deposit is already credited on-chain, returns
        status="credited" immediately.

        Returns:
            Dict with status ("credited" | "pending") and deposit_id.

        Raises:
            ValueError: If verification fails or tx is a gas funding tx.
        """
        tx_hash_lower = tx_hash.lower()

        if tx_hash_lower in self._sweep.gas_funding_tx_hashes:
            raise ValueError(
                f"Transaction {tx_hash} is a gas funding tx and cannot be claimed as a deposit"
            )

        deposit_address = await self._accounting.get_deposit_address(
            chain_type, version, siwe_token
        )

        verified = await self._verifier.verify_deposit(
            chain_id=chain_id,
            tx_hash=tx_hash,
            deposit_address=deposit_address,
            expected_amount=amount,
            log_index=log_index,
        )

        if verified.is_native:
            min_amount = MIN_DEPOSIT_NATIVE_WEI.get(chain_id, 0)
        else:
            min_amount = MIN_DEPOSIT_ERC20_WEI.get(chain_id, 0)
        if min_amount > 0 and verified.amount < min_amount:
            raise ValueError("Deposit does not meet minimum requirements")

        token_id = await self._get_token_id(chain_id, verified.token_address)

        if not await self._accounting.is_token_registered(token_id):
            raise ValueError("Token not supported")

        deposit_id = compute_deposit_id(chain_id, tx_hash, token_id, verified.deposit_index)
        deposit_id_hex = _to_hex(deposit_id)

        already_processed = await self._accounting.is_deposit_processed(deposit_id)
        if already_processed:
            logger.info("Deposit already processed: deposit_id=%s", deposit_id_hex)
            return self._deposit_response(
                "credited", deposit_id_hex, verified.amount, verified.token_address
            )

        # If a sweep is already in progress for this deposit, return pending
        existing = self._sweep.get_record_by_deposit_id(deposit_id_hex)
        if existing is not None:
            if existing.error:
                if existing.sweep_tx_hash:
                    logger.warning(
                        "Sweep record has error after sweep tx broadcast; preserving record: "
                        "deposit_id=%s sweep_tx=%s",
                        deposit_id_hex,
                        existing.sweep_tx_hash,
                    )
                    return self._deposit_response(
                        "pending", deposit_id_hex, verified.amount, verified.token_address
                    )
                self._sweep.cleanup_record(deposit_id_hex)
            else:
                return self._deposit_response(
                    "pending", deposit_id_hex, verified.amount, verified.token_address
                )

        # Fire background sweep — returns immediately
        task = asyncio.create_task(
            self._background_sweep(
                deposit_address=deposit_address,
                beneficiary=beneficiary,
                chain_type=chain_type,
                version=version,
                chain_id=chain_id,
                token_address=verified.token_address,
                token_id=token_id,
                amount=verified.amount,
                deposit_id=deposit_id,
                is_native=verified.is_native,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return self._deposit_response(
            "pending", deposit_id_hex, verified.amount, verified.token_address
        )

    async def _background_sweep(
        self,
        deposit_address: str,
        beneficiary: str,
        chain_type: str,
        version: int,
        chain_id: int,
        token_address: str | None,
        token_id: bytes,
        amount: int,
        deposit_id: bytes,
        is_native: bool,
    ) -> None:
        """Run sweep + credit in background. Errors are persisted to the record."""
        deposit_id_hex = _to_hex(deposit_id)
        try:
            if is_native:
                await self._sweep.sweep_native(
                    deposit_address=deposit_address,
                    beneficiary=beneficiary,
                    chain_type=chain_type,
                    version=version,
                    chain_id=chain_id,
                    token_id=token_id,
                    amount=amount,
                    deposit_id=deposit_id,
                )
            else:
                await self._sweep.sweep_erc20(
                    deposit_address=deposit_address,
                    beneficiary=beneficiary,
                    chain_type=chain_type,
                    version=version,
                    chain_id=chain_id,
                    token_address=token_address,
                    token_id=token_id,
                    amount=amount,
                    deposit_id=deposit_id,
                )
            logger.info(
                "Background sweep completed: deposit_id=%s beneficiary=%s",
                deposit_id_hex,
                beneficiary,
            )
        except SweepCreditPendingError:
            logger.warning(
                "Background sweep: credit pending for deposit_id=%s — recovery loop will retry",
                deposit_id_hex,
            )
        except Exception as exc:
            logger.exception("Background sweep failed for deposit_id=%s", deposit_id_hex)
            self._sweep.persist_error(deposit_id_hex, str(exc))

    def get_deposit_status(self, deposit_id_hex: str, beneficiary: str) -> Dict | None:
        """Get current status of a deposit from in-memory sweep state.

        Returns None if deposit_id is unknown (caller should check on-chain).
        """
        record = self._sweep.get_record_by_deposit_id(deposit_id_hex)
        if record is None:
            return None

        if record.beneficiary.lower() != beneficiary.lower():
            return None

        if record.error:
            if record.sweep_tx_hash:
                logger.warning(
                    "Sweep record has error after sweep tx broadcast; returning pending status: "
                    "deposit_id=%s sweep_tx=%s",
                    deposit_id_hex,
                    record.sweep_tx_hash,
                )
                return self._deposit_response(
                    "pending", deposit_id_hex, record.amount, record.token_address
                )
            return self._deposit_response(
                "error", deposit_id_hex, record.amount, record.token_address, detail=record.error
            )

        return self._deposit_response(
            "pending", deposit_id_hex, record.amount, record.token_address
        )


_processor_instance: DepositProcessor | None = None


def get_deposit_processor() -> DepositProcessor:
    """Return singleton processor instance, creating it on first call."""
    global _processor_instance
    if _processor_instance is None:
        settings = load_settings()
        accounting = get_accounting_contract_service()
        verifier = DepositVerifier(dict(settings.chain_rpc_urls))
        sweep = SweepEngine(
            accounting_service=accounting,
            chain_rpc_urls=dict(settings.chain_rpc_urls),
        )
        _processor_instance = DepositProcessor(
            verifier=verifier,
            sweep_engine=sweep,
            accounting_service=accounting,
        )
    return _processor_instance
