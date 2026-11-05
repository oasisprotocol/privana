"""Deposit processor: orchestrates sweep → credit.

Called from the /deposits/check API route. Coordinates the sweep engine and the
accounting contract service.

Every asset is claimed by balance: the whole finalized balance of the asset at the
deposit address is swept and credited. The deposit_id comes from the deposit address's
nonce, so no tx hash or amount is needed and repeated claims cannot credit the same
funds twice. The zero address stands for the native asset.

The sweep lifecycle runs in a background task so the API responds within
the ROFL HTTP proxy timeout (~30s). Clients poll GET /deposits/status/{key}.
"""

import asyncio
import logging
from typing import Dict

from web3 import Web3
from web3.constants import ADDRESS_ZERO

from src.config import load_settings
from src.config.chain_config import CHAIN_CONFIGS
from src.models.private_read import PrivateReadAuth
from src.services.accounting_contract import get_accounting_contract_service
from src.services.sweep_engine import (
    BalanceClaimStatus,
    DepositAccountingProtocol,
    SweepCreditPendingError,
    SweepEngine,
    _to_hex,
)

logger = logging.getLogger(__name__)


# Tasks cancelled before their claim was saved leave nothing on disk — the client saw
# "pending" but recovery has nothing to resume.
BACKGROUND_SWEEP_SHUTDOWN_TIMEOUT = 60


class DepositProcessor:
    """Orchestrates the full deposit flow: sweep → credit."""

    def __init__(
        self,
        sweep_engine: SweepEngine,
        accounting_service: DepositAccountingProtocol,
    ):
        self._sweep = sweep_engine
        self._accounting = accounting_service
        # Strong refs — asyncio.create_task returns can be GC'd without one.
        self._background_tasks: set[asyncio.Task] = set()

    @property
    def sweep_engine(self) -> SweepEngine:
        """In-flight sweep state."""
        return self._sweep

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
        deposit_id_hex: str | None,
        amount: int | None,
        token_address: str | None,
        detail: str | None = None,
    ) -> Dict:
        resp: Dict = {
            "status": status,
            "deposit_id": deposit_id_hex,
            "amount": None if amount is None else str(amount),
            "token_address": token_address,
        }
        if detail is not None:
            resp["detail"] = detail
        return resp

    async def process_deposit(
        self,
        chain_type: str,
        chain_id: int,
        version: int,
        auth: PrivateReadAuth,
        token_address: str,
    ) -> Dict:
        """Claim funds that arrived at the caller's deposit address and start the sweep.

        Returns immediately. The actual sweep runs as a background asyncio task. Clients
        poll GET /deposits/status/{deposit_id} for completion.

        ``token_address`` names the asset to claim (the zero address is native). The whole
        finalized balance is claimed.

        Returns:
            Dict with status ("pending" | "none"), deposit_id and amount. "none" means
            nothing claimable was found.

        Raises:
            ValueError: If the chain or token is not supported.
        """
        if chain_id not in CHAIN_CONFIGS:
            raise ValueError(f"Unsupported chain_id {chain_id}")

        is_native = token_address.lower() == ADDRESS_ZERO
        asset = None if is_native else Web3.to_checksum_address(token_address)

        deposit_address = await self._accounting.get_deposit_address(
            chain_type, version, auth.token
        )

        token_id = await self._get_token_id(chain_id, asset)

        if not await self._accounting.is_token_registered(token_id):
            raise ValueError("Token not supported")

        claim_args = dict(
            deposit_address=deposit_address,
            beneficiary=auth.user_address,
            chain_type=chain_type,
            version=version,
            chain_id=chain_id,
            token_address=asset,
            token_id=token_id,
        )
        claim = await self._sweep.claim_balance(**claim_args)

        record = claim.record
        if (
            claim.status is BalanceClaimStatus.IN_FLIGHT
            and record.error
            and not record.sweep_tx_hash
        ):
            # Failed before any sweep tx existed: nothing on-chain to orphan, so
            # make a fresh plan from the current balance.
            self._sweep.cleanup_record(record.deposit_id_hex)
            claim = await self._sweep.claim_balance(**claim_args)
            record = claim.record

        if claim.status is BalanceClaimStatus.NONE:
            detail = (
                "Finalized balance is below the minimum deposit"
                if claim.amount
                else "No finalized balance to credit"
            )
            return self._deposit_response("none", None, claim.amount, asset, detail=detail)
        if claim.status is BalanceClaimStatus.BUSY:
            return self._deposit_response(
                "pending",
                None,
                None,
                asset,
                detail="Another sweep for this deposit address is in progress; check again shortly",
            )

        assert record is not None
        if claim.status is BalanceClaimStatus.STARTED:
            # Fire background sweep — returns immediately
            task = asyncio.create_task(
                self._background_sweep(
                    deposit_id=bytes.fromhex(record.deposit_id_hex.removeprefix("0x")),
                    is_native=is_native,
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return self._deposit_response(
            "pending", record.deposit_id_hex, claim.amount or record.amount, asset
        )

    async def _background_sweep(self, deposit_id: bytes, is_native: bool) -> None:
        """Run sweep + credit in background. Errors are persisted to the record."""
        deposit_id_hex = _to_hex(deposit_id)
        try:
            if is_native:
                await self._sweep.sweep_native(deposit_id)
            else:
                await self._sweep.sweep_erc20(deposit_id)
            logger.info("Background sweep completed: deposit_id=%s", deposit_id_hex)
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
                "error",
                deposit_id_hex,
                record.amount,
                record.token_address,
                detail=record.error,
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
        sweep = SweepEngine(
            accounting_service=accounting,
            chain_rpc_urls=dict(settings.chain_rpc_urls),
        )
        _processor_instance = DepositProcessor(
            sweep_engine=sweep,
            accounting_service=accounting,
        )
    return _processor_instance
