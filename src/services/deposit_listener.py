"""Service for listening to ETH deposits across configured chains."""

import asyncio
import logging
from typing import Dict, Optional, Set

from web3 import Web3
from web3.types import BlockData

from src.config import CHAIN_NAMES, ERC20_TOKENS, load_settings
from src.services.accounting_contract import AccountingContractService
# TODO: MOCK TESTING - Proof generator not needed for testing
# from src.services.proof_generator import get_proof_generator

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
        # TODO: MOCK TESTING - Proof generator not needed for testing
        # self._proof_generator = get_proof_generator(self.chain_rpc_urls)

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
            tx_receipt = await asyncio.to_thread(web3.eth.get_transaction_receipt, tx_hash)

            if tx_receipt["status"] != 1:
                logger.warning(f"Transaction {tx_hash} failed on chain {chain_id}, skipping")
                return

            # TODO: MOCK TESTING - Verification data commented out for testing purposes
            # raw_tx = await asyncio.to_thread(web3.eth.get_raw_transaction, tx_hash)
            # evm_transaction_data = raw_tx.hex()

            token_id = await asyncio.to_thread(self._get_native_token_id, chain_id)

            # TODO: MOCK TESTING - Proof generation commented out for testing purposes
            # proof = await asyncio.to_thread(
            #     self._proof_generator.generate_tx_proof,
            #     chain_id,
            #     tx_hash
            # )

            payload = {
                "user_address": from_address,
                "token_id": token_id,
                # TODO: MOCK TESTING - Verification fields commented out for testing purposes
                # "evm_transaction_data": evm_transaction_data,
                # "rlp_block_header": proof["rlp_block_header"],
                # "transaction_index_rlp": proof["transaction_index_rlp"],
                # "transaction_proof_stack": proof["transaction_proof_stack"],
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
            tx_receipt = await asyncio.to_thread(web3.eth.get_transaction_receipt, tx_hash)

            if tx_receipt["status"] != 1:
                logger.warning(f"Transaction {tx_hash} failed on chain {chain_id}, skipping")
                return

            # TODO: MOCK TESTING - Verification data commented out for testing purposes
            # raw_tx = await asyncio.to_thread(web3.eth.get_raw_transaction, tx_hash)
            # evm_transaction_data = raw_tx.hex()

            token_id = await asyncio.to_thread(self._get_erc20_token_id, chain_id, token_address)

            # TODO: MOCK TESTING - Proof generation commented out for testing purposes
            # proof = await asyncio.to_thread(
            #     self._proof_generator.generate_tx_proof,
            #     chain_id,
            #     tx_hash
            # )

            payload = {
                "user_address": from_address,
                "token_id": token_id,
                # TODO: MOCK TESTING - Verification fields commented out for testing purposes
                # "evm_transaction_data": evm_transaction_data,
                # "rlp_block_header": proof["rlp_block_header"],
                # "transaction_index_rlp": proof["transaction_index_rlp"],
                # "transaction_proof_stack": proof["transaction_proof_stack"],
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
                        block_number = await asyncio.to_thread(lambda: web3.eth.block_number)
                        lookback_blocks = 100
                        start_block = max(0, block_number - lookback_blocks)
                        self._last_processed_blocks[chain_id] = start_block
                        logger.info(f"Initialized {chain_name} at block {start_block}, scanning last {lookback_blocks} blocks")
                    except Exception as e:
                        logger.warning(f"Failed to get block number for {chain_name}: {e}. Retrying...")
                        await asyncio.sleep(poll_interval)
                        continue

                current_block = await asyncio.to_thread(lambda: web3.eth.block_number)
                last_processed = self._last_processed_blocks[chain_id]

                if current_block > last_processed:
                    for block_num in range(last_processed + 1, current_block + 1):
                        try:
                            block: BlockData = await asyncio.to_thread(
                                web3.eth.get_block, block_num, True
                            )

                            for tx in block["transactions"]:
                                tx_hash = tx["hash"].hex()
                                has_native_deposit = False

                                if (
                                    tx.get("to")
                                    and tx["to"].lower() == deposit_address
                                    and tx.get("value", 0) > 0
                                ):
                                    has_native_deposit = True
                                    logger.info(f"FOUND NATIVE DEPOSIT in tx {tx_hash} at block {block_num}")
                                    await self._process_deposit(
                                        chain_id=chain_id,
                                        tx_hash=tx_hash,
                                        from_address=tx["from"],
                                        value=tx["value"],
                                        block_number=block_num,
                                    )

                                if erc20_tokens or has_native_deposit:
                                    receipt = await asyncio.to_thread(
                                        web3.eth.get_transaction_receipt, tx_hash
                                    )

                                    if erc20_tokens:
                                        for log in receipt.get("logs", []):
                                            if len(log["topics"]) >= 3 and log["topics"][0].hex() == self.TRANSFER_EVENT_SIGNATURE:
                                                token_address = log["address"].lower()
                                                if token_address in erc20_tokens:
                                                    to_address = "0x" + log["topics"][2].hex()[-40:]
                                                    if to_address.lower() == deposit_address:
                                                        from_address = "0x" + log["topics"][1].hex()[-40:]
                                                        value = int.from_bytes(log["data"], byteorder="big")

                                                        logger.info(f"Found ERC20 deposit ({erc20_tokens[token_address]}) in tx {tx_hash} at block {block_num}")
                                                        await self._process_erc20_deposit(
                                                            chain_id=chain_id,
                                                            tx_hash=tx_hash,
                                                            token_address=token_address,
                                                            from_address=from_address,
                                                            value=value,
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
