"""Service for processing relay transactions: read signed tx from contract and broadcast to destination chains."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from web3 import Web3

from src.config import CHAIN_NAMES, load_settings
from src.services.accounting_contract import AccountingContractService

logger = logging.getLogger(__name__)

_MIN_RPC_INTERVAL = 0.1


@dataclass
class RelayEntry:
    submission_id: str
    chain_id: int
    status: str = "submitted"
    relay_id: Optional[int] = None
    tx_hash: Optional[str] = None
    error: Optional[str] = None


class RelayProcessor:
    """Polls the Accounting contract for completed relay results, broadcasts
    the signed transactions to destination chains, and clears the stored results.

    Relay processing flow:
    1. API calls executeRelay via ROFL → contract signs tx and stores in relayResults[id]
    2. This processor polls nextRelayId for new entries
    3. Reads signed tx bytes from relayResults[id]
    4. Broadcasts to the destination chain
    5. Clears the relay result from contract storage
    """

    def __init__(self):
        self.settings = load_settings()
        self.accounting_service = AccountingContractService(self.settings)
        self._sapphire_web3 = Web3(Web3.HTTPProvider(self.settings.sapphire_rpc_url))
        self._contract_address = Web3.to_checksum_address(
            self.settings.accounting_contract_address
        )
        self._contract = self._sapphire_web3.eth.contract(
            address=self._contract_address,
            abi=self.accounting_service.contract.abi,
        )
        self._is_running = False
        self._task: Optional[asyncio.Task] = None
        self._relays: Dict[str, RelayEntry] = {}
        self._pending_queue: List[str] = []
        self._processed_up_to: int = 0
        self._last_rpc_call: float = 0

    async def _rate_limited_call(self, func, *args, **kwargs):
        now = time.time()
        time_since_last = now - self._last_rpc_call
        if time_since_last < _MIN_RPC_INTERVAL:
            await asyncio.sleep(_MIN_RPC_INTERVAL - time_since_last)

        max_retries = 5
        base_delay = 1.0

        for attempt in range(max_retries):
            try:
                self._last_rpc_call = time.time()
                return await asyncio.to_thread(func, *args, **kwargs)
            except Exception as e:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        f"RPC error, retrying in {delay}s (attempt {attempt + 1}): {e}"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

    def register_relay(self, submission_id: str, chain_id: int) -> None:
        entry = RelayEntry(submission_id=submission_id, chain_id=chain_id)
        self._relays[submission_id] = entry
        self._pending_queue.append(submission_id)

    def get_relay_status(self, submission_id: str) -> Optional[Dict]:
        entry = self._relays.get(submission_id)
        if entry is None:
            return None
        return {
            "relay_id": entry.relay_id,
            "status": entry.status,
            "chain_id": entry.chain_id,
            "tx_hash": entry.tx_hash,
            "error": entry.error,
        }

    async def _broadcast_relay(self, relay_id: int, signed_tx: bytes, entry: RelayEntry) -> None:
        chain_id = entry.chain_id
        chain_name = CHAIN_NAMES.get(chain_id, f"chain {chain_id}")

        try:
            logger.info(f"Relay #{relay_id}: broadcasting to {chain_name}")
            tx_hash = await self._rate_limited_call(
                self.accounting_service._send_raw_transaction,
                chain_id,
                signed_tx,
            )
            logger.info(f"Relay #{relay_id}: broadcast successful, tx_hash={tx_hash}")
            entry.tx_hash = tx_hash
            entry.status = "broadcast"
        except Exception as exc:
            error_str = str(exc).lower()
            if "nonce too low" in error_str or "already known" in error_str:
                logger.info(f"Relay #{relay_id}: already broadcast to {chain_name}")
                entry.status = "broadcast"
            else:
                logger.error(f"Relay #{relay_id}: broadcast failed - {exc}")
                entry.error = str(exc)
                entry.status = "failed"

        try:
            await self._rate_limited_call(
                self.accounting_service.clear_relay_result, relay_id
            )
        except Exception as exc:
            logger.warning(f"Relay #{relay_id}: failed to clear result - {exc}")

    async def _process_new_relays(self):
        next_id = await self._rate_limited_call(
            self._contract.functions.nextRelayId().call
        )

        for relay_id in range(self._processed_up_to, next_id):
            result = await self._rate_limited_call(
                self._contract.functions.relayResults(relay_id).call
            )

            if not result:
                self._processed_up_to = relay_id + 1
                continue

            entry = None
            while self._pending_queue:
                sid = self._pending_queue.pop(0)
                candidate = self._relays.get(sid)
                if candidate and candidate.status == "submitted":
                    entry = candidate
                    entry.relay_id = relay_id
                    entry.status = "signed"
                    break

            if entry is None:
                logger.warning(f"Relay #{relay_id}: no matching registration, skipping")
                self._processed_up_to = relay_id + 1
                continue

            await self._broadcast_relay(relay_id, result, entry)
            self._processed_up_to = relay_id + 1

    async def _run_processor(self):
        poll_interval = self.settings.relay_poll_interval
        logger.info(f"Starting relay processor with poll interval {poll_interval}s")

        try:
            self._processed_up_to = await self._rate_limited_call(
                self._contract.functions.nextRelayId().call
            )
            logger.info(f"Relay processor initialized at relay_id {self._processed_up_to}")
        except Exception as e:
            logger.error(f"Failed to initialize relay processor: {e}")
            self._processed_up_to = 0

        while self._is_running:
            try:
                await self._process_new_relays()
            except Exception:
                logger.exception("Error during relay processing poll")
            await asyncio.sleep(poll_interval)

        logger.info("Relay processor stopped")

    async def start(self):
        if self._is_running:
            logger.warning("Relay processor is already running")
            return
        self._is_running = True
        logger.info("Starting relay processor...")
        self._task = asyncio.create_task(self._run_processor())

    async def stop(self):
        if not self._is_running:
            return
        logger.info("Stopping relay processor...")
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Relay processor stopped")


_processor_instance: Optional[RelayProcessor] = None


def get_relay_processor() -> RelayProcessor:
    global _processor_instance
    if _processor_instance is None:
        _processor_instance = RelayProcessor()
    return _processor_instance
