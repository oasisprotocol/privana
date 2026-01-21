"""Service for automatically resolving pending withdrawals after block delay."""

import asyncio
import logging
from typing import Optional, Set

from web3 import Web3

from src.config import load_settings
from src.services.accounting_contract import AccountingContractService

logger = logging.getLogger(__name__)


class WithdrawalResolver:
    """Monitors and automatically resolves pending withdrawals after the required block delay."""

    def __init__(self):
        self.settings = load_settings()
        self.accounting_service = AccountingContractService(self.settings)
        self._is_running = False
        self._task: Optional[asyncio.Task] = None
        self._resolve_tasks: Set[asyncio.Task] = set()
        self._sapphire_web3 = Web3(Web3.HTTPProvider(self.settings.sapphire_rpc_url))
        self._processed_indices: Set[int] = set()

    def _spawn_resolve_task(self, coro, task_name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=task_name)
        self._resolve_tasks.add(task)
        task.add_done_callback(self._resolve_tasks.discard)
        return task

    async def _get_current_block(self) -> int:
        return await asyncio.to_thread(self._sapphire_web3.eth.block_number)

    async def _get_all_pending_withdrawals(self) -> list:
        contract_reader = self.accounting_service._get_reader_contract()
        pending = []
        index = 0
        max_iterations = 10000

        while index < max_iterations:
            try:
                result = await asyncio.to_thread(
                    contract_reader.functions.withdrawals(index).call
                )
                resolved = result[4]

                if not resolved:
                    pending.append({
                        "index": index,
                        "user_address": result[0],
                        "amount": result[1],
                        "block_number": result[2],
                        "token_id": "0x" + result[3].hex(),
                        "resolved": result[4],
                    })
                index += 1
            except Exception:
                break

        return pending

    async def _resolve_withdrawal(self, withdrawal: dict):
        index = withdrawal["index"]
        user = withdrawal["user_address"]
        amount = withdrawal["amount"]

        try:
            logger.info(
                f"Resolving withdrawal #{index} for {user}, amount={amount}"
            )

            result = await asyncio.to_thread(
                self.accounting_service.resolve_withdrawal,
                {"index": index}
            )

            self._processed_indices.add(index)

            logger.info(
                f"Withdrawal #{index} resolved: submission_id={result.submission_id}, "
                f"status={result.status}"
            )

        except Exception:
            logger.exception(f"Failed to resolve withdrawal #{index}")

    async def _poll_and_resolve(self):
        poll_interval = self.settings.withdrawal_poll_interval
        logger.info(f"Starting withdrawal resolver with poll interval {poll_interval}s")

        while self._is_running:
            try:
                current_block = await self._get_current_block()
                pending = await self._get_all_pending_withdrawals()

                eligible = [
                    w for w in pending
                    if current_block > w["block_number"]
                    and w["index"] not in self._processed_indices
                ]

                if eligible:
                    logger.info(
                        f"Found {len(eligible)} eligible withdrawals to resolve "
                        f"(current_block={current_block})"
                    )

                for withdrawal in eligible:
                    self._spawn_resolve_task(
                        self._resolve_withdrawal(withdrawal),
                        f"resolve-{withdrawal['index']}"
                    )

            except Exception:
                logger.exception("Error during withdrawal resolution poll")

            await asyncio.sleep(poll_interval)

    async def start(self):
        if self._is_running:
            logger.warning("Withdrawal resolver is already running")
            return

        self._is_running = True
        logger.info("Starting withdrawal resolver...")
        self._task = asyncio.create_task(self._poll_and_resolve())

    async def stop(self):
        if not self._is_running:
            return

        logger.info("Stopping withdrawal resolver...")
        self._is_running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self._resolve_tasks:
            logger.info(f"Waiting for {len(self._resolve_tasks)} active resolve tasks...")
            await asyncio.gather(*self._resolve_tasks, return_exceptions=True)
            self._resolve_tasks.clear()

        logger.info("Withdrawal resolver stopped")


_resolver_instance: Optional[WithdrawalResolver] = None


def get_withdrawal_resolver() -> WithdrawalResolver:
    global _resolver_instance
    if _resolver_instance is None:
        _resolver_instance = WithdrawalResolver()
    return _resolver_instance
