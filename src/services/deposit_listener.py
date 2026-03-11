"""Service for listening to ETH deposits across configured chains."""

import asyncio
import logging
import time
from typing import Dict, Optional, Set

from web3 import Web3
from web3.types import BlockData

from src.abi.rofl_adapter import ROFL_ADAPTER_ABI
from src.clients.rofl import TransactionRevertedError
from src.config import CHAIN_NAMES, ERC20_TOKENS, load_settings
from src.services.accounting_contract import AccountingContractService
from src.services.block_state import PendingTx, get_block_state_manager
from src.services.proof_generator import get_proof_generator

logger = logging.getLogger(__name__)


class DepositListener:
    """Monitors ETH and ERC20 token deposits to the ROFL deposit address."""

    TRANSFER_EVENT_SIGNATURE = Web3.keccak(text="Transfer(address,address,uint256)").hex()

    # Retry worker configuration
    RETRY_LOOP_INTERVAL = 30  # seconds between retry loop iterations
    RETRY_WARNING_INTERVAL = 10  # log warning every N retry attempts
    MAX_RETRY_ATTEMPTS = 100  # mark as dead after this many retries
    STALE_PROCESSING_TIMEOUT = 900  # seconds before processing tx is considered stale

    # Errors that indicate the deposit was already successfully processed.
    # These should be treated as success, not failure.
    SUCCESS_EQUIVALENT_ERRORS = {"DepositAlreadyProcessed"}

    def __init__(self):
        self.settings = load_settings()
        self.accounting_service = AccountingContractService(self.settings)
        self.chain_rpc_urls: Dict[int, str] = dict(self.settings.chain_rpc_urls)
        if not self.chain_rpc_urls:
            logger.warning("WARNING: No chain RPC URLs configured! Check ALCHEMY_API_KEY in .env")
        self._chain_web3: Dict[int, Web3] = {}
        self._is_running = False
        self._tasks: Set[asyncio.Task] = set()
        self._deposit_tasks: Set[asyncio.Task] = set()
        self._retry_task: Optional[asyncio.Task] = None
        self._deposit_address: Optional[str] = None
        self._last_processed_blocks: Dict[int, int] = {}
        self._native_token_ids: Dict[int, str] = {}
        self._erc20_token_ids: Dict[tuple, str] = {}
        self._last_rpc_call: Dict[int, float] = {}
        self._min_rpc_interval = 0.05
        self._proof_generator = get_proof_generator(self.chain_rpc_urls)
        self._block_state = get_block_state_manager()
        self._sapphire_web3 = Web3(Web3.HTTPProvider(self.settings.sapphire_rpc_url))
        self._rofl_adapter_address = Web3.to_checksum_address(self.settings.rofl_adapter_address)
        self._rofl_adapter_contract = self._sapphire_web3.eth.contract(
            address=self._rofl_adapter_address, abi=ROFL_ADAPTER_ABI
        )
        logger.info(
            f"ROFL Adapter contract: {self._rofl_adapter_address} on {self.settings.sapphire_rpc_url}"
        )
        try:
            if self._sapphire_web3.is_connected():
                sapphire_chain_id = self._sapphire_web3.eth.chain_id
                logger.info(f"Sapphire connection OK, chain_id={sapphire_chain_id}")
                test_hash = self._rofl_adapter_contract.functions.getHash(1, 1).call()
                logger.info(
                    f"ROFL Adapter contract call test OK: getHash(1,1)={Web3.to_hex(test_hash)}"
                )
            else:
                logger.warning("Sapphire connection failed on startup")
        except Exception as e:
            logger.warning(f"Sapphire/ROFL Adapter startup check failed: {type(e).__name__}: {e}")

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
                if (
                    "429" in error_str
                    or "too many requests" in error_str
                    or "rate limit" in error_str
                ):
                    if attempt < max_retries - 1:
                        delay = base_delay * (2**attempt)
                        chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}")
                        logger.warning(
                            f"Rate limit hit on {chain_name}, retrying in {delay}s (attempt {attempt + 1}/{max_retries})"
                        )
                        await asyncio.sleep(delay)
                        continue
                raise

    async def _get_deposit_address(self) -> str:
        if self._deposit_address is None:
            self._deposit_address = await self.accounting_service._get_deposit_address()
            logger.info(f"Monitoring deposit address: {self._deposit_address}")
        return self._deposit_address

    def _spawn_deposit_task(self, coro, task_name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=task_name)
        self._deposit_tasks.add(task)
        task.add_done_callback(self._deposit_tasks.discard)
        return task

    def _decode_contract_error_name(self, exc: Exception) -> Optional[str]:
        if isinstance(exc, TransactionRevertedError) and exc.error_name:
            return exc.error_name
        return None

    def _is_success_equivalent_error(self, exc: Exception) -> bool:
        """Check if error indicates deposit was already processed successfully."""
        error_name = self._decode_contract_error_name(exc)
        return error_name in self.SUCCESS_EQUIVALENT_ERRORS

    def _is_terminal_deposit_error(self, exc: Exception) -> bool:
        """Check if error is a terminal failure that should not be retried."""
        error_name = self._decode_contract_error_name(exc)
        if error_name is None:
            return False
        # Success-equivalent errors are not terminal failures
        if error_name in self.SUCCESS_EQUIVALENT_ERRORS:
            return False
        # Other contract reverts are deterministic business-logic failures
        return True

    def _mark_processing_exception(
        self, chain_id: int, tx_hash: str, exc: Exception, is_erc20: bool = False
    ) -> None:
        if self._is_success_equivalent_error(exc):
            # Deposit was already processed - treat as success
            deposit_type = "ERC20 deposit" if is_erc20 else "deposit"
            logger.info(
                f"{deposit_type} {tx_hash} on chain {chain_id} was already processed, marking complete"
            )
            self._block_state.complete_pending_tx(chain_id, tx_hash)
        elif self._is_terminal_deposit_error(exc):
            self._block_state.mark_dead(chain_id, tx_hash, str(exc))
        else:
            self._block_state.mark_failed(chain_id, tx_hash, str(exc))

    def _query_block_hash(self, chain_id: int, block_number: int) -> bytes:
        return self._rofl_adapter_contract.functions.getHash(chain_id, block_number).call()

    async def _wait_for_block_hash_stored(
        self,
        block_number: int,
        chain_id: int,
        max_wait_seconds: int = 300,
        poll_interval: int = 5,
    ) -> bool:
        chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}")
        logger.info(
            f"Waiting for block hash to be stored for block {block_number} on {chain_name}..."
        )

        start_time = time.time()
        zero_hash = "0x" + "0" * 64

        while time.time() - start_time < max_wait_seconds:
            try:
                stored_hash = await asyncio.to_thread(
                    self._query_block_hash, chain_id, block_number
                )
                stored_hash_hex = Web3.to_hex(stored_hash)

                if stored_hash_hex != zero_hash:
                    logger.info(
                        f"Block hash found for block {block_number} on {chain_name}: {stored_hash_hex}"
                    )
                    return True

                logger.debug(
                    f"Block hash not yet available for block {block_number} on {chain_name}, "
                    f"waiting {poll_interval}s... (elapsed: {int(time.time() - start_time)}s)"
                )
                await asyncio.sleep(poll_interval)

            except Exception as e:
                logger.warning(
                    f"Error checking block hash for {chain_name} block {block_number} "
                    f"via adapter {self._rofl_adapter_address}: {type(e).__name__}: {e}"
                )
                await asyncio.sleep(poll_interval)

        logger.error(f"Timeout waiting for block hash for block {block_number} on {chain_name}")
        return False

    async def _get_native_token_id(self, chain_id: int) -> str:
        if chain_id in self._native_token_ids:
            return self._native_token_ids[chain_id]

        contract = self.accounting_service._get_reader_contract()
        token_data = await contract.functions.encodeEVMNativeTokenData(chain_id).call()
        token_info = (0, token_data)
        token_id = Web3.to_hex(await contract.functions.getTokenId(token_info).call())

        self._native_token_ids[chain_id] = token_id
        return token_id

    async def _get_erc20_token_id(self, chain_id: int, token_address: str) -> str:
        cache_key = (chain_id, token_address.lower())
        if cache_key in self._erc20_token_ids:
            return self._erc20_token_ids[cache_key]

        contract = self.accounting_service._get_reader_contract()
        checksummed_address = Web3.to_checksum_address(token_address)
        token_data = await contract.functions.encodeEVMErc20TokenData(
            chain_id, checksummed_address
        ).call()
        token_info = (1, token_data)
        token_id = Web3.to_hex(await contract.functions.getTokenId(token_info).call())

        chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}")
        logger.info(
            f"Retrieved ERC20 token ID for {checksummed_address} on {chain_name}: {token_id}"
        )

        self._erc20_token_ids[cache_key] = token_id
        return token_id

    async def _process_pending_tx(
        self,
        chain_id: int,
        tx_hash: str,
        from_address: str,
        value: Optional[int],
        block_number: int,
        token_address: Optional[str],
    ):
        try:
            chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}")
            is_erc20 = token_address is not None
            existing = self._block_state.get_pending_txs(chain_id).get(tx_hash)
            if existing and existing.status == "dead":
                deposit_type = "ERC20 deposit" if is_erc20 else "deposit"
                logger.info(
                    "%s %s on chain %s is marked dead, skipping reprocessing",
                    deposit_type,
                    tx_hash,
                    chain_id,
                )
                return
            is_already_tracked = existing is not None
            token_name = None
            if token_address:
                token_name = ERC20_TOKENS.get(chain_id, {}).get(token_address, token_address)
            retry_info = " (retry)" if is_already_tracked else ""
            if is_erc20:
                value_text = "unknown" if value is None else str(value)
                logger.info(
                    f"Processing {token_name} deposit on {chain_name}{retry_info}: "
                    f"tx={tx_hash}, from={from_address}, value={value_text}, block={block_number}"
                )
            else:
                value_text = "unknown" if value is None else f"{value} wei"
                logger.info(
                    f"Processing deposit on {chain_name}{retry_info}: "
                    f"tx={tx_hash}, from={from_address}, value={value_text}, block={block_number}"
                )

            if not is_already_tracked:
                self._block_state.add_pending_tx(
                    chain_id,
                    tx_hash,
                    block_number,
                    token_address=token_address,
                    from_address=from_address,
                )

            web3 = self._get_chain_web3(chain_id)
            tx_receipt = await self._rate_limited_rpc_call(
                chain_id, web3.eth.get_transaction_receipt, tx_hash
            )

            if tx_receipt["status"] != 1:
                logger.warning(f"Transaction {tx_hash} failed on chain {chain_id}, skipping")
                self._block_state.complete_pending_tx(chain_id, tx_hash)
                return

            hash_stored = await self._wait_for_block_hash_stored(block_number, chain_id)
            if not hash_stored:
                error = f"Block hash not available for block {block_number}"
                deposit_type = "ERC20 deposit" if is_erc20 else "deposit"
                logger.error(f"{error}, skipping {deposit_type} {tx_hash}")
                self._block_state.mark_failed(chain_id, tx_hash, error)
                return

            proofs = await asyncio.to_thread(
                self._proof_generator.generate_deposit_proofs, chain_id, tx_hash
            )

            if is_erc20:
                token_id = await self._get_erc20_token_id(chain_id, token_address)
            else:
                token_id = await self._get_native_token_id(chain_id)

            payload = {
                "user_address": from_address,
                "token_id": token_id,
                **proofs,
            }

            result = await self.accounting_service.include_deposit(payload)
            if is_erc20:
                logger.info(
                    f"{token_name} deposit included successfully: submission_id={result.submission_id}, "
                    f"status={result.status}"
                )
            else:
                logger.info(
                    f"Deposit included successfully: submission_id={result.submission_id}, "
                    f"status={result.status}"
                )

            self._block_state.complete_pending_tx(chain_id, tx_hash)

        except Exception as e:
            deposit_type = "ERC20 deposit" if is_erc20 else "deposit"
            logger.exception("Failed to process %s %s on chain %s", deposit_type, tx_hash, chain_id)
            self._mark_processing_exception(chain_id, tx_hash, e, is_erc20=is_erc20)

    def _get_retry_delay(self, retry_count: int) -> float:
        """Calculate delay before next retry using exponential backoff.

        Backoff schedule:
        - Attempt 1: immediate (already happened)
        - Attempt 2: 30s delay
        - Attempt 3: 60s delay
        - Attempt 4: 2min delay
        - Attempt 5: 5min delay
        - Attempt 6+: 10min delay (cap)
        """
        delays = [0, 30, 60, 120, 300, 600]  # seconds
        if retry_count >= len(delays):
            return delays[-1]
        return delays[retry_count]

    def _recover_stale_processing_txs(
        self,
        chain_id: int,
        chain_name: str,
        pending_txs: Dict[str, PendingTx],
        now: float,
        stale_processing_timeout: int,
    ) -> None:
        """Mark stale or inconsistent processing entries as failed."""
        for tx_hash, pending_tx in pending_txs.items():
            if pending_tx.status != "processing":
                continue

            if pending_tx.processing_started_at <= 0:
                error = "processing state missing start timestamp"
            elif now - pending_tx.processing_started_at >= stale_processing_timeout:
                error = f"processing timeout after {stale_processing_timeout}s"
            else:
                continue

            logger.warning(
                "%s: Marking stale processing deposit %s as failed: %s",
                chain_name,
                tx_hash,
                error,
            )
            self._block_state.mark_failed(chain_id, tx_hash, error)

    def _is_retry_ready(self, pending_tx: PendingTx, now: float) -> bool:
        required_delay = self._get_retry_delay(pending_tx.retry_count)
        return pending_tx.last_retry_at <= 0 or (now - pending_tx.last_retry_at >= required_delay)

    async def _retry_pending_deposits(self):
        """Background task that periodically retries failed deposits sequentially."""
        logger.info("Starting deposit retry worker")

        while self._is_running:
            try:
                await asyncio.sleep(self.RETRY_LOOP_INTERVAL)

                if not self._is_running:
                    break

                for chain_id in self.chain_rpc_urls.keys():
                    chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}")
                    now = time.time()
                    pending_txs = self._block_state.get_pending_txs(chain_id)
                    self._recover_stale_processing_txs(
                        chain_id, chain_name, pending_txs, now, self.STALE_PROCESSING_TIMEOUT
                    )

                    failed_txs = self._block_state.get_failed_txs(chain_id)
                    for tx_hash, pending_tx in failed_txs.items():
                        if not self._is_running:
                            break

                        if pending_tx.retry_count >= self.MAX_RETRY_ATTEMPTS:
                            error = f"max retries exceeded ({pending_tx.retry_count}/{self.MAX_RETRY_ATTEMPTS})"
                            logger.error(
                                "%s: Marking deposit %s as dead-letter: %s",
                                chain_name,
                                tx_hash,
                                error,
                            )
                            self._block_state.mark_dead(chain_id, tx_hash, error)
                            continue

                        if not self._is_retry_ready(pending_tx, now):
                            continue

                        if not pending_tx.from_address:
                            error = "missing from_address"
                            logger.warning(f"{chain_name}: Cannot retry deposit {tx_hash}, {error}")
                            self._block_state.mark_dead(chain_id, tx_hash, error)
                            continue

                        # Claim for retry and increment retry count.
                        retry_count = self._block_state.claim_retry(chain_id, tx_hash)
                        if retry_count is None:
                            continue

                        # Log warning periodically for stuck deposits
                        if retry_count > 0 and retry_count % self.RETRY_WARNING_INTERVAL == 0:
                            logger.warning(
                                f"{chain_name}: Deposit {tx_hash} has been retried {retry_count} times. "
                                f"This may indicate a persistent issue."
                            )

                        logger.info(
                            f"{chain_name}: Retrying deposit {tx_hash} "
                            f"(attempt {retry_count + 1}, block={pending_tx.block_number})"
                        )

                        # Process retry sequentially
                        await self._process_pending_tx(
                            chain_id=chain_id,
                            tx_hash=tx_hash,
                            from_address=pending_tx.from_address,
                            value=None,
                            block_number=pending_tx.block_number,
                            token_address=pending_tx.token_address,
                        )

            except asyncio.CancelledError:
                logger.info("Deposit retry worker cancelled")
                break
            except Exception:
                logger.exception("Error in deposit retry worker")
                await asyncio.sleep(self.RETRY_LOOP_INTERVAL)

        logger.info("Deposit retry worker stopped")

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
                        deposit_address = await self._get_deposit_address()
                        deposit_address = deposit_address.lower()
                        logger.info(
                            f"Retrieved deposit address for {chain_name}: {deposit_address}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to get deposit address for {chain_name}: {e}. Retrying..."
                        )
                        await asyncio.sleep(poll_interval)
                        continue

                if chain_id not in self._last_processed_blocks:
                    try:
                        current_block_number = await self._rate_limited_rpc_call(
                            chain_id, lambda: web3.eth.block_number
                        )
                        lookback_blocks = 100

                        start_block = self._block_state.get_backfill_start_block(
                            chain_id, current_block_number, lookback_blocks
                        )

                        persisted_block = self._block_state.get_last_processed_block(chain_id)
                        if persisted_block is not None:
                            blocks_behind = current_block_number - start_block
                            logger.info(
                                f"Restored {chain_name} from persisted state, starting scan from block {start_block}, "
                                f"{blocks_behind} blocks to process"
                            )

                            pending_txs = self._block_state.get_pending_txs(chain_id)
                            if pending_txs:
                                logger.warning(
                                    f"{chain_name}: {len(pending_txs)} pending transactions will be reprocessed: "
                                    f"{list(pending_txs.keys())[:5]}{'...' if len(pending_txs) > 5 else ''}"
                                )
                        else:
                            logger.info(
                                f"Initialized {chain_name} at block {start_block}, scanning last {lookback_blocks} blocks"
                            )
                            self._block_state.set_last_processed_block(chain_id, start_block)

                        self._last_processed_blocks[chain_id] = start_block
                    except Exception as e:
                        logger.warning(
                            f"Failed to get block number for {chain_name}: {e}. Retrying..."
                        )
                        await asyncio.sleep(poll_interval)
                        continue

                current_block = await self._rate_limited_rpc_call(
                    chain_id, lambda: web3.eth.block_number
                )
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
                                    if (
                                        tx_to
                                        and tx_to.lower() == deposit_address
                                        and tx.get("value", 0) > 0
                                    ):
                                        if tx_hash in self._block_state.get_pending_txs(chain_id):
                                            continue
                                        logger.info(
                                            f"{chain_name}: Native deposit in block {block_num}"
                                        )
                                        self._spawn_deposit_task(
                                            self._process_pending_tx(
                                                chain_id=chain_id,
                                                tx_hash=tx_hash,
                                                from_address=tx["from"],
                                                value=tx["value"],
                                                block_number=block_num,
                                                token_address=None,
                                            ),
                                            f"deposit-{tx_hash[:10]}",
                                        )

                                    # ERC20 deposit
                                    if erc20_tokens and tx_to and tx_to.lower() in erc20_tokens:
                                        receipt = await self._rate_limited_rpc_call(
                                            chain_id, web3.eth.get_transaction_receipt, tx_hash
                                        )

                                        for log in receipt.get("logs", []):
                                            if (
                                                len(log["topics"]) >= 3
                                                and log["topics"][0].hex()
                                                == self.TRANSFER_EVENT_SIGNATURE
                                                and log["address"].lower() in erc20_tokens
                                                and "0x" + log["topics"][2].hex()[-40:]
                                                == deposit_address
                                            ):
                                                if tx_hash in self._block_state.get_pending_txs(
                                                    chain_id
                                                ):
                                                    continue
                                                logger.info(
                                                    f"{chain_name}: {erc20_tokens[log['address'].lower()]} deposit in block {block_num}"
                                                )
                                                self._spawn_deposit_task(
                                                    self._process_pending_tx(
                                                        chain_id=chain_id,
                                                        tx_hash=tx_hash,
                                                        from_address="0x"
                                                        + log["topics"][1].hex()[-40:],
                                                        value=int.from_bytes(
                                                            log["data"], byteorder="big"
                                                        ),
                                                        block_number=block_num,
                                                        token_address=log["address"].lower(),
                                                    ),
                                                    f"erc20-deposit-{tx_hash[:10]}",
                                                )

                            except Exception as e:
                                logger.error(f"{chain_name}: Error in block {block_num}: {e}")

                        self._last_processed_blocks[chain_id] = to_block
                        self._block_state.set_last_processed_block(chain_id, to_block)

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

        # Start the retry worker for failed deposits
        self._retry_task = asyncio.create_task(self._retry_pending_deposits())

    async def stop(self):
        if not self._is_running:
            return

        logger.info("Stopping deposit listener...")
        self._is_running = False

        for task in self._tasks:
            task.cancel()

        # Cancel retry task
        if self._retry_task:
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
            self._retry_task = None

        if self._deposit_tasks:
            logger.info(
                f"Waiting for {len(self._deposit_tasks)} active deposit tasks to complete..."
            )
            await asyncio.gather(*self._deposit_tasks, return_exceptions=True)
            self._deposit_tasks.clear()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Deposit listener stopped")


_listener_instance: Optional[DepositListener] = None


def get_deposit_listener() -> DepositListener:
    global _listener_instance
    if _listener_instance is None:
        _listener_instance = DepositListener()
    return _listener_instance
