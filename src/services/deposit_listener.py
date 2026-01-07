"""Service for listening to ETH deposits across configured chains."""

import asyncio
import logging
import time
from typing import Dict, Optional, Set

from web3 import Web3
from web3.types import BlockData

from src.config import CHAIN_NAMES, ERC20_TOKENS, load_settings
from src.services.accounting_contract import AccountingContractService
from src.services.proof_generator import get_proof_generator

logger = logging.getLogger(__name__)


class DepositListener:
    """Monitors ETH and ERC20 token deposits to the ROFL deposit address."""

    TRANSFER_EVENT_SIGNATURE = Web3.keccak(text="Transfer(address,address,uint256)").hex()

    def __init__(self):
        self.settings = load_settings()
        self.accounting_service = AccountingContractService(self.settings)
        self.chain_rpc_urls: Dict[int, str] = dict(self.settings.chain_rpc_urls)
        if not self.chain_rpc_urls:
            logger.warning("WARNING: No chain RPC URLs configured! Check ALCHEMY_API_KEY in .env")
        self._chain_web3: Dict[int, Web3] = {}
        self._is_running = False
        self._tasks: Set[asyncio.Task] = set()
        self._deposit_address: Optional[str] = None
        self._last_processed_blocks: Dict[int, int] = {}
        self._native_token_ids: Dict[int, str] = {}
        self._erc20_token_ids: Dict[tuple, str] = {}
        self._last_rpc_call: Dict[int, float] = {}
        self._min_rpc_interval = 0.05
        self._proof_generator = get_proof_generator(self.chain_rpc_urls)

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

    async def _rate_limited_rpc_call(self, chain_id: int, func, *args, **kwargs):
        """Execute RPC call with rate limiting and exponential backoff."""
        now = time.time()
        last_call = self._last_rpc_call.get(chain_id, 0)
        time_since_last = now - last_call

        if time_since_last < self._min_rpc_interval:
            await asyncio.sleep(self._min_rpc_interval - time_since_last)

        max_retries = 5
        base_delay = 1.0

        for attempt in range(max_retries):
            try:
                self._last_rpc_call[chain_id] = time.time()
                result = await asyncio.to_thread(func, *args, **kwargs)
                return result
            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "too many requests" in error_str or "rate limit" in error_str:
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}")
                        logger.warning(
                            f"Rate limit hit on {chain_name}, retrying in {delay}s (attempt {attempt + 1}/{max_retries})"
                        )
                        await asyncio.sleep(delay)
                        continue
                raise

    def _get_deposit_address(self) -> str:
        if self._deposit_address is None:
            self._deposit_address = self.accounting_service._get_deposit_address()
            logger.info(f"Monitoring deposit address: {self._deposit_address}")
        return self._deposit_address


    def _get_native_token_id(self, chain_id: int) -> str:
        if chain_id in self._native_token_ids:
            return self._native_token_ids[chain_id]

        contract = self.accounting_service._get_reader_contract()
        token_data = contract.functions.encodeEVMNativeTokenData(chain_id).call()
        token_info = (0, token_data)
        token_id = Web3.to_hex(contract.functions.getTokenId(token_info).call())

        self._native_token_ids[chain_id] = token_id
        return token_id

    def _get_erc20_token_id(self, chain_id: int, token_address: str) -> str:
        cache_key = (chain_id, token_address.lower())
        if cache_key in self._erc20_token_ids:
            return self._erc20_token_ids[cache_key]

        contract = self.accounting_service._get_reader_contract()
        checksummed_address = Web3.to_checksum_address(token_address)
        token_data = contract.functions.encodeEVMErc20TokenData(chain_id, checksummed_address).call()
        token_info = (1, token_data)
        token_id = Web3.to_hex(contract.functions.getTokenId(token_info).call())

        chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}")
        logger.info(f"Retrieved ERC20 token ID for {checksummed_address} on {chain_name}: {token_id}")

        self._erc20_token_ids[cache_key] = token_id
        return token_id

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
            tx_receipt = await self._rate_limited_rpc_call(chain_id, web3.eth.get_transaction_receipt, tx_hash)

            if tx_receipt["status"] != 1:
                logger.warning(f"Transaction {tx_hash} failed on chain {chain_id}, skipping")
                return

            proofs = await asyncio.to_thread(
                self._proof_generator.generate_deposit_proofs, chain_id, tx_hash
            )

            token_id = await asyncio.to_thread(self._get_native_token_id, chain_id)

            payload = {
                "user_address": from_address,
                "token_id": token_id,
                **proofs,
            }

            result = self.accounting_service.include_deposit(payload)
            logger.info(
                f"Deposit included successfully: submission_id={result.submission_id}, "
                f"status={result.status}"
            )

        except Exception:
            logger.exception("Failed to process deposit %s on chain %s", tx_hash, chain_id)

    async def _process_erc20_deposit(
        self,
        chain_id: int,
        tx_hash: str,
        token_address: str,
        from_address: str,
        value: int,
        block_number: int,
    ):
        try:
            chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}")
            token_name = ERC20_TOKENS.get(chain_id, {}).get(token_address, token_address)
            logger.info(
                f"Processing {token_name} deposit on {chain_name}: "
                f"tx={tx_hash}, from={from_address}, value={value}, block={block_number}"
            )

            web3 = self._get_chain_web3(chain_id)
            tx_receipt = await self._rate_limited_rpc_call(chain_id, web3.eth.get_transaction_receipt, tx_hash)

            if tx_receipt["status"] != 1:
                logger.warning(f"Transaction {tx_hash} failed on chain {chain_id}, skipping")
                return

            proofs = await asyncio.to_thread(
                self._proof_generator.generate_deposit_proofs, chain_id, tx_hash
            )

            token_id = await asyncio.to_thread(self._get_erc20_token_id, chain_id, token_address)

            payload = {
                "user_address": from_address,
                "token_id": token_id,
                **proofs,
            }

            result = self.accounting_service.include_deposit(payload)
            logger.info(
                f"{token_name} deposit included successfully: submission_id={result.submission_id}, "
                f"status={result.status}"
            )

        except Exception:
            logger.exception("Failed to process ERC20 deposit %s on chain %s", tx_hash, chain_id)

    async def _monitor_chain(self, chain_id: int):
        chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}")
        logger.info(f"Starting deposit monitoring for {chain_name} (chain_id={chain_id})")

        poll_interval = self.settings.deposit_poll_interval
        web3 = None
        deposit_address = None
        erc20_tokens = {addr.lower(): name for addr, name in ERC20_TOKENS.get(chain_id, {}).items()}

        while self._is_running:
            try:
                if web3 is None:
                    try:
                        web3 = await asyncio.to_thread(self._get_chain_web3, chain_id)
                        logger.info(f"Connected to {chain_name} RPC")
                    except Exception as e:
                        logger.warning(f"Failed to connect to {chain_name} RPC: {e}. Retrying...")
                        await asyncio.sleep(poll_interval)
                        continue

                if deposit_address is None:
                    try:
                        deposit_address = await asyncio.to_thread(self._get_deposit_address)
                        deposit_address = deposit_address.lower()
                        logger.info(f"Retrieved deposit address for {chain_name}: {deposit_address}")
                    except Exception as e:
                        logger.warning(f"Failed to get deposit address for {chain_name}: {e}. Retrying...")
                        await asyncio.sleep(poll_interval)
                        continue

                if chain_id not in self._last_processed_blocks:
                    try:
                        block_number = await self._rate_limited_rpc_call(chain_id, lambda: web3.eth.block_number)
                        lookback_blocks = 100
                        start_block = max(0, block_number - lookback_blocks)
                        self._last_processed_blocks[chain_id] = start_block
                        logger.info(f"Initialized {chain_name} at block {start_block}, scanning last {lookback_blocks} blocks")
                    except Exception as e:
                        logger.warning(f"Failed to get block number for {chain_name}: {e}. Retrying...")
                        await asyncio.sleep(poll_interval)
                        continue

                current_block = await self._rate_limited_rpc_call(chain_id, lambda: web3.eth.block_number)
                last_processed = self._last_processed_blocks[chain_id]

                if current_block > last_processed:
                    to_block = min(current_block, last_processed + 20)

                    try:
                        for block_num in range(last_processed + 1, to_block + 1):
                            try:
                                block: BlockData = await self._rate_limited_rpc_call(
                                    chain_id, web3.eth.get_block, block_num, True
                                )

                                for tx in block.get("transactions", []):
                                    tx_hash = tx["hash"].hex()
                                    tx_to = tx.get("to")

                                    # Native deposit
                                    if tx_to and tx_to.lower() == deposit_address and tx.get("value", 0) > 0:
                                        logger.info(f"{chain_name}: Native deposit in block {block_num}")
                                        await self._process_deposit(
                                            chain_id, tx_hash, tx["from"], tx["value"], block_num
                                        )

                                    # ERC20 deposit
                                    if erc20_tokens and tx_to and tx_to.lower() in erc20_tokens:
                                        receipt = await self._rate_limited_rpc_call(
                                            chain_id, web3.eth.get_transaction_receipt, tx_hash
                                        )

                                        for log in receipt.get("logs", []):
                                            if (
                                                len(log["topics"]) >= 3
                                                and log["topics"][0].hex() == self.TRANSFER_EVENT_SIGNATURE
                                                and log["address"].lower() in erc20_tokens
                                                and "0x" + log["topics"][2].hex()[-40:] == deposit_address
                                            ):
                                                logger.info(
                                                    f"{chain_name}: {erc20_tokens[log['address'].lower()]} deposit in block {block_num}"
                                                )
                                                await self._process_erc20_deposit(
                                                    chain_id,
                                                    tx_hash,
                                                    log["address"].lower(),
                                                    "0x" + log["topics"][1].hex()[-40:],
                                                    int.from_bytes(log["data"], byteorder="big"),
                                                    block_num,
                                                )

                            except Exception as e:
                                logger.error(f"{chain_name}: Error in block {block_num}: {e}")

                        self._last_processed_blocks[chain_id] = to_block

                    except Exception as e:
                        logger.error(f"{chain_name}: Scan error: {e}")

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
