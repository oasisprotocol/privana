"""Service for listening to ETH deposits across configured chains."""

import asyncio
import logging
from typing import Dict, Optional, Set

from web3 import Web3
from web3.types import BlockData

from src.config import CHAIN_NAMES, load_settings
from src.services.accounting_contract import AccountingContractService

logger = logging.getLogger(__name__)


class DepositListener:
    """Monitors ETH deposits to the ROFL deposit address."""

    def __init__(self):
        self.settings = load_settings()
        self.accounting_service = AccountingContractService(self.settings)
        self.chain_rpc_urls: Dict[int, str] = dict(self.settings.chain_rpc_urls)
        self._chain_web3: Dict[int, Web3] = {}
        self._is_running = False
        self._tasks: Set[asyncio.Task] = set()
        self._deposit_address: Optional[str] = None
        self._last_processed_blocks: Dict[int, int] = {}

    def _get_chain_web3(self, chain_id: int) -> Web3:
        if chain_id in self._chain_web3:
            return self._chain_web3[chain_id]

        rpc_url = self.chain_rpc_urls.get(chain_id)
        if not rpc_url:
            raise ValueError(f"No RPC endpoint configured for chain ID {chain_id}")

        web3 = Web3(Web3.HTTPProvider(rpc_url))
        if not web3.is_connected():
            raise ValueError(f"Failed to connect to RPC endpoint for chain ID {chain_id}")

        self._chain_web3[chain_id] = web3
        return web3

    def _get_deposit_address(self) -> str:
        if self._deposit_address is None:
            self._deposit_address = self.accounting_service._get_deposit_address()
            logger.info(f"Monitoring deposit address: {self._deposit_address}")
        return self._deposit_address

    async def _process_deposit(
        self,
        chain_id: int,
        tx_hash: str,
        from_address: str,
        value: int,
        block_number: int,
    ):
        try:
            chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}")
            logger.info(
                f"Processing deposit on {chain_name}: "
                f"tx={tx_hash}, from={from_address}, value={value} wei, block={block_number}"
            )

            web3 = self._get_chain_web3(chain_id)
            tx = web3.eth.get_transaction(tx_hash)
            tx_receipt = web3.eth.get_transaction_receipt(tx_hash)

            if tx_receipt["status"] != 1:
                logger.warning(f"Transaction {tx_hash} failed on chain {chain_id}, skipping")
                return

            evm_transaction_data = f"0x{tx.hash.hex()[2:]}"

            # TODO: Add RLP block header, transaction index, and transaction proof stack
            # Need to ask Noah about this
            payload = {
                "user_address": from_address,
                "token_id": f"0x{'0' * 24}{chain_id:016x}{'0' * 40}",
                "evm_transaction_data": evm_transaction_data,
                "rlp_block_header": None,
                "transaction_index_rlp": None,
                "transaction_proof_stack": None,
            }

            result = self.accounting_service.include_deposit(payload)
            logger.info(
                f"Deposit included successfully: submission_id={result.submission_id}, "
                f"status={result.status}"
            )

        except Exception as e:
            logger.error(f"Failed to process deposit {tx_hash} on chain {chain_id}: {str(e)}")

    async def _monitor_chain(self, chain_id: int):
        chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}")
        logger.info(f"Starting deposit monitoring for {chain_name} (chain_id={chain_id})")

        web3 = self._get_chain_web3(chain_id)
        deposit_address = self._get_deposit_address().lower()

        if chain_id not in self._last_processed_blocks:
            self._last_processed_blocks[chain_id] = web3.eth.block_number

        poll_interval = self.settings.deposit_poll_interval

        while self._is_running:
            try:
                current_block = web3.eth.block_number
                last_processed = self._last_processed_blocks[chain_id]

                if current_block > last_processed:
                    for block_num in range(last_processed + 1, current_block + 1):
                        try:
                            block: BlockData = web3.eth.get_block(block_num, full_transactions=True)

                            for tx in block["transactions"]:
                                if (
                                    tx.get("to")
                                    and tx["to"].lower() == deposit_address
                                    and tx.get("value", 0) > 0
                                ):
                                    await self._process_deposit(
                                        chain_id=chain_id,
                                        tx_hash=tx["hash"].hex(),
                                        from_address=tx["from"],
                                        value=tx["value"],
                                        block_number=block_num,
                                    )

                            self._last_processed_blocks[chain_id] = block_num

                        except Exception as e:
                            logger.error(
                                f"Error processing block {block_num} on {chain_name}: {str(e)}"
                            )

                await asyncio.sleep(poll_interval)

            except Exception as e:
                logger.error(f"Error monitoring {chain_name}: {str(e)}")
                await asyncio.sleep(poll_interval)

    async def start(self):
        if self._is_running:
            logger.warning("Deposit listener is already running")
            return

        self._is_running = True
        logger.info(f"Starting deposit listener for {len(self.chain_rpc_urls)} chains")

        for chain_id in self.chain_rpc_urls.keys():
            task = asyncio.create_task(self._monitor_chain(chain_id))
            self._tasks.add(task)

    async def stop(self):
        if not self._is_running:
            return

        logger.info("Stopping deposit listener...")
        self._is_running = False

        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Deposit listener stopped")


_listener_instance: Optional[DepositListener] = None


def get_deposit_listener() -> DepositListener:
    global _listener_instance
    if _listener_instance is None:
        _listener_instance = DepositListener()
    return _listener_instance
