"""Deposit processor: orchestrates sweep → credit.

Called from the /deposits/check API route. Coordinates the sweep engine and the
accounting contract service.

Every asset is claimed by balance: the whole balance of the asset at the
deposit address is swept and credited, so no tx hash or amount is needed. Each sweep
gets a random deposit_id; at most one unfinished sweep exists per asset and address.

The sweep lifecycle runs in a background task so the API responds within
the ROFL HTTP proxy timeout (~30s). Clients poll GET /deposits/status/{key}.
"""

import asyncio
import logging
from typing import Dict

from web3 import Web3

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

    async def check_and_schedule_deposits(
        self,
        version: int,
        auth: PrivateReadAuth,
        token_id: str | None,
    ) -> list[Dict]:
        """Initiate checking deposit address balance and schedule a sweep if needed.

        Returns immediately. The actual sweep runs as a background asyncio task. Clients
        poll GET /deposits/status/{deposit_id} for completion.

        ``token_id`` is the asset to claim; without it every registered asset is
        checked. The whole balance is claimed.

        Returns:
            The in-flight deposits found or started, each as a pending status; empty when
            there is none.

        Raises:
            ValueError: If the ``token_id`` is not a configured token.
        """
        deposit_address = await self._accounting.get_deposit_address("evm", version, auth.token)

        token_ids = load_settings().token_infos
        if token_id is not None:
            if token_id not in token_ids:
                raise ValueError("Token not supported")
            token_ids = {token_id: token_ids[token_id]}

        # Read balances concurrently.
        results = await asyncio.gather(
            *(
                self._check_and_schedule_deposit(key, deposit_address, auth, version)
                for key in token_ids
            ),
            return_exceptions=True,
        )
        deposits = []
        for key, result in zip(token_ids, results):
            if isinstance(result, BaseException):
                logger.warning("Skipping token %s for address %s: %s", key, deposit_address, result)
                continue
            deposit, new_sweep = result
            if deposit is not None:
                deposits.append(deposit)
            if new_sweep is not None:
                task = asyncio.create_task(self._background_sweep(new_sweep))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

        return deposits

    async def _check_and_schedule_deposit(
        self,
        token_id_hex: str,
        deposit_address: str,
        auth: PrivateReadAuth,
        version: int,
    ) -> tuple[Dict | None, bytes | None]:
        """Check deposit and schedule a sweep, if needed.

        Returns the in-flight deposit (None when there is none) and, for a newly
        scheduled sweep, its ``deposit_id``.
        """
        token_id = bytes.fromhex(token_id_hex.removeprefix("0x"))
        info = load_settings().token_infos[token_id_hex]
        chain_id = info["chain_id"]
        asset = Web3.to_checksum_address(info["token_address"]) if info["token_address"] else None

        if chain_id not in CHAIN_CONFIGS:
            raise ValueError(f"Unsupported chain_id {chain_id}")

        claim = await self._sweep.claim_balance(
            deposit_address=deposit_address,
            beneficiary=auth.user_address,
            chain_type="evm",
            version=version,
            chain_id=chain_id,
            token_address=asset,
            token_id=token_id,
        )

        record = claim.record
        if claim.status in (BalanceClaimStatus.NONE, BalanceClaimStatus.BUSY):
            return None, None

        assert record is not None
        sweep = None
        if claim.status is BalanceClaimStatus.STARTED:
            sweep = bytes.fromhex(record.deposit_id_hex.removeprefix("0x"))

        deposit = self._deposit_response(
            "pending", record.deposit_id_hex, claim.amount or record.amount, asset
        )
        return deposit, sweep

    async def _background_sweep(self, deposit_id: bytes) -> None:
        """Run sweep + credit in background. Errors are persisted to the record."""
        deposit_id_hex = _to_hex(deposit_id)
        try:
            await self._sweep.sweep(deposit_id)
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
        """Get current status of a deposit from the persisted sweep record.

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
