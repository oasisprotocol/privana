"""Service for processing withdrawals: resolve on Sapphire and broadcast to destination chains."""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Set

from eth_abi import decode
from web3 import Web3
from hexbytes import HexBytes

from src.config import CHAIN_NAMES, load_settings
from src.services.accounting_contract import AccountingContractService
from src.abi.accounting import ACCOUNTING_ABI

logger = logging.getLogger(__name__)


def _build_error_selectors(abi: list) -> dict[str, str]:
    """Build a mapping of error selectors to names from the ABI."""
    selectors = {}
    for item in abi:
        if item.get("type") == "error":
            name = item["name"]
            inputs = item.get("inputs", [])
            # Build signature: ErrorName(type1,type2,...)
            types = ",".join(inp["type"] for inp in inputs)
            signature = f"{name}({types})"
            # Compute selector (first 4 bytes of keccak256)
            selector = Web3.keccak(text=signature)[:4].hex()
            selectors[selector] = name
    return selectors


ERROR_SELECTORS = _build_error_selectors(ACCOUNTING_ABI)


def decode_contract_error(exc: Exception) -> tuple[str, str]:
    """Decode a contract error to (selector, name). Returns ('', error_str) if unknown."""
    error_str = str(exc).lower()
    for selector, name in ERROR_SELECTORS.items():
        if selector in error_str:
            return selector, name
    return "", str(exc)


_MIN_RPC_INTERVAL = 0.1


class WithdrawalProcessor:
    """Polls the Accounting contract for pending withdrawals, resolves them on
    Sapphire to obtain signed transactions, and broadcasts those transactions
    to the appropriate destination chains.

    Withdrawals are processed sequentially per chain to preserve nonce ordering.
    A periodic catch-up pass re-broadcasts any resolved withdrawals whose
    transactions have not yet landed on-chain.
    """

    def __init__(self):
        self.settings = load_settings()
        self.accounting_service = AccountingContractService(self.settings)
        self._sapphire_web3 = Web3(Web3.HTTPProvider(self.settings.sapphire_rpc_url))
        self._contract_address = Web3.to_checksum_address(self.settings.accounting_contract_address)
        self._contract = self._sapphire_web3.eth.contract(
            address=self._contract_address,
            abi=self.accounting_service.contract.abi,
        )
        self._is_running = False
        self._task: Optional[asyncio.Task] = None
        # Track highest processed withdrawal index per chain (for sequential processing)
        self._chain_high_water_mark: Dict[int, int] = {}
        self._last_rpc_call: float = 0
        self._destination_web3: Dict[int, Web3] = {}
        self._evm_address: Optional[str] = None

    async def _rate_limited_call(self, func, *args, **kwargs):
        """Execute an RPC call with rate limiting and retries."""
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
                    logger.warning(f"RPC error, retrying in {delay}s (attempt {attempt + 1}): {e}")
                    await asyncio.sleep(delay)
                    continue
                raise

    def _get_destination_web3(self, chain_id: int) -> Web3:
        """Get or create Web3 instance for a destination chain."""
        if chain_id not in self._destination_web3:
            rpc_url = self.settings.chain_rpc_urls.get(chain_id)
            if not rpc_url:
                raise ValueError(f"No RPC URL configured for chain {chain_id}")
            self._destination_web3[chain_id] = Web3(Web3.HTTPProvider(rpc_url))
        return self._destination_web3[chain_id]

    def _get_evm_address(self) -> str:
        """Get the EVM address used for withdrawals."""
        if self._evm_address is None:
            self._evm_address = self._contract.functions.evmAddress().call()
        return self._evm_address

    async def _catch_up_missing_broadcasts(self, chain_ids: Optional[List[int]] = None):
        """Find and broadcast any resolved-but-not-broadcast withdrawals.

        Args:
            chain_ids: Optional list of chain IDs to check. If None, checks all configured chains.
        """
        if chain_ids is None:
            chain_ids = list(self.settings.chain_rpc_urls.keys())
            logger.info("Checking for missing withdrawal broadcasts...")
        else:
            chain_names = [CHAIN_NAMES.get(c, f"chain {c}") for c in chain_ids]
            logger.info(f"Checking for missing broadcasts on {', '.join(chain_names)}...")

        for chain_id in chain_ids:
            try:
                chain_name = CHAIN_NAMES.get(chain_id, f"chain {chain_id}")

                # Get contract's next nonce (what it will use next)
                contract_next_nonce = await self._rate_limited_call(
                    self._contract.functions.nonces(chain_id).call
                )

                # Get current nonce on destination chain (what's been broadcast)
                evm_address = await self._rate_limited_call(self._get_evm_address)
                dest_web3 = self._get_destination_web3(chain_id)
                chain_current_nonce = await self._rate_limited_call(
                    dest_web3.eth.get_transaction_count, evm_address
                )

                if contract_next_nonce <= chain_current_nonce:
                    logger.info(f"{chain_name}: no missing broadcasts")
                    continue

                missing_count = contract_next_nonce - chain_current_nonce
                logger.info(
                    f"{chain_name}: found {missing_count} missing broadcasts "
                    f"(nonces {chain_current_nonce} to {contract_next_nonce - 1})"
                )

                # Find and broadcast missing withdrawals
                target_nonces = set(range(chain_current_nonce, contract_next_nonce))
                await self._broadcast_missing_for_chain(chain_id, target_nonces)

            except Exception as e:
                chain_name = CHAIN_NAMES.get(chain_id, f"chain {chain_id}")
                logger.error(f"Error catching up {chain_name}: {e}")

        logger.info("Finished checking for missing broadcasts")

    async def _broadcast_missing_for_chain(self, chain_id: int, target_nonces: Set[int]):
        """Find and broadcast resolved withdrawals with the given nonces."""
        if not target_nonces:
            return

        chain_name = CHAIN_NAMES.get(chain_id, f"chain {chain_id}")
        contract_reader = self.accounting_service._get_reader_contract()
        found_nonces: Set[int] = set()

        # Get total withdrawal count from contract
        try:
            total_withdrawals = await self._rate_limited_call(
                contract_reader.functions.withdrawalCount().call
            )
        except Exception as e:
            logger.error(f"{chain_name}: failed to get withdrawal count: {e}")
            return

        logger.info(f"{chain_name}: scanning {total_withdrawals} withdrawals for {len(target_nonces)} missing nonces")

        # Scan in reverse - missing broadcasts are likely recent
        for index in range(total_withdrawals - 1, -1, -1):
            if len(found_nonces) >= len(target_nonces):
                break

            try:
                result = await self._rate_limited_call(
                    contract_reader.functions.withdrawals(index).call
                )
            except Exception as exc:
                logger.warning(f"Withdrawal #{index}: failed to read, skipping: {exc}")
                continue

            resolved = result[4]
            tx_identifier = result[5]

            if not resolved:
                # Not yet processed by the main polling loop, skip
                continue
            if not tx_identifier or len(tx_identifier) < 32:
                logger.warning(f"Withdrawal #{index}: resolved but has invalid txIdentifier, skipping")
                continue

            # Decode nonce from txIdentifier
            nonce = decode(['uint64'], tx_identifier)[0]

            # Get chain_id for this withdrawal
            token_id_bytes = result[3]
            try:
                context = self.accounting_service._get_token_context(HexBytes(token_id_bytes))
                withdrawal_chain_id = context.chain_id
            except Exception as e:
                logger.warning(f"Withdrawal #{index}: failed to get token context - {e}")
                withdrawal_chain_id = 0

            if withdrawal_chain_id != chain_id or nonce not in target_nonces:
                continue

            # Found a missing one - broadcast it
            logger.info(f"Withdrawal #{index}: broadcasting missing nonce {nonce}")
            signed_tx = await self._rate_limited_call(
                contract_reader.functions.resolveWithdrawal(index).call
            )
            try:
                tx_hash = await self._rate_limited_call(
                    self.accounting_service._send_raw_transaction,
                    chain_id,
                    signed_tx,
                )
                logger.info(f"Withdrawal #{index}: broadcast successful, tx_hash={tx_hash}")
                found_nonces.add(nonce)
            except Exception as exc:
                error_str = str(exc).lower()
                if "nonce too low" in error_str or "already known" in error_str:
                    logger.info(f"Withdrawal #{index}: already broadcast")
                    found_nonces.add(nonce)
                else:
                    logger.error(f"Withdrawal #{index}: broadcast failed - {exc}")

            if index > 0 and index % 100 == 0:
                logger.info(f"{chain_name}: scanned {index}/{total_withdrawals} withdrawals, found {len(found_nonces)}/{len(target_nonces)}")

        logger.info(f"{chain_name}: catch-up complete, broadcast {len(found_nonces)}/{len(target_nonces)} missing")

    async def _get_pending_withdrawals(self) -> Dict[int, List[Dict]]:
        """Get pending withdrawals grouped by chain for ordered processing."""
        try:
            result = await self._rate_limited_call(
                self.accounting_service.get_all_pending_withdrawals,
            )

            pending = result.get("pending", [])
            current_block = result.get("current_block", 0)

            # Filter by block delay and not already processed (per-chain high water mark)
            eligible = [
                w for w in pending
                if (current_block - w["block_number"] >= 1)
                and w["index"] > self._chain_high_water_mark.get(w.get("chain_id", 0), -1)
            ]

            # Group by chain_id and sort by index within each chain
            # Skip withdrawals with invalid chain_id (None or 0) - these have unregistered tokens
            by_chain: Dict[int, List[Dict]] = {}
            for w in eligible:
                chain_id = w.get("chain_id")
                if not chain_id:
                    # Token not registered - already logged in accounting_contract.py
                    # Skip to avoid infinite retries; requires token registration to fix
                    continue
                if chain_id not in by_chain:
                    by_chain[chain_id] = []
                by_chain[chain_id].append(w)

            # Sort each chain's withdrawals by index
            for chain_id in by_chain:
                by_chain[chain_id].sort(key=lambda x: x["index"])

            return by_chain

        except Exception as e:
            logger.error(f"Error getting pending withdrawals: {e}")
            return {}

    async def _resolve_and_broadcast(self, withdrawal: Dict) -> bool:
        """Resolve a withdrawal and broadcast to destination chain.

        Returns True if successful, False if should retry.
        """
        index = withdrawal["index"]
        chain_id = withdrawal.get("chain_id")

        # Safety check: skip withdrawals with invalid chain_id
        if not chain_id:
            logger.error(f"Withdrawal #{index}: invalid chain_id, skipping (token not registered)")
            return True  # Return True to prevent infinite retries

        chain_name = CHAIN_NAMES.get(chain_id, f"chain {chain_id}")

        try:
            logger.info(f"Processing withdrawal #{index} for {chain_name}")

            # Step 1: Check if already resolved
            contract_reader = self.accounting_service._get_reader_contract()
            withdrawal_data = await self._rate_limited_call(
                contract_reader.functions.withdrawals(index).call
            )
            is_resolved = withdrawal_data[4]

            # Step 2: If not resolved, submit resolveWithdrawal and wait
            if not is_resolved:
                logger.info(f"Withdrawal #{index}: submitting resolveWithdrawal")
                result = await self._rate_limited_call(
                    self.accounting_service.resolve_withdrawal,
                    index,
                )
                logger.info(f"Withdrawal #{index}: submitted ({result.submission_id})")

                # Wait for resolution (poll until resolved)
                for _ in range(self.settings.withdrawal_resolution_timeout):
                    await asyncio.sleep(1)
                    withdrawal_data = await self._rate_limited_call(
                        contract_reader.functions.withdrawals(index).call
                    )
                    if withdrawal_data[4]:  # resolved
                        is_resolved = True
                        break

                if not is_resolved:
                    # ROFL hasn't confirmed the resolution yet; returning False
                    # stops processing this chain and triggers a catch-up pass,
                    # which will retry on the next poll cycle.
                    logger.warning(f"Withdrawal #{index}: timeout waiting for resolution")
                    return False

            # Step 3: Get signedTx by calling resolveWithdrawal (idempotent)
            logger.info(f"Withdrawal #{index}: getting signed transaction")
            signed_tx = await self._rate_limited_call(
                contract_reader.functions.resolveWithdrawal(index).call
            )

            # Step 4: Broadcast to destination chain
            logger.info(f"Withdrawal #{index}: broadcasting to {chain_name}")
            tx_hash = await self._rate_limited_call(
                self.accounting_service._send_raw_transaction,
                chain_id,
                signed_tx,
            )
            logger.info(f"Withdrawal #{index}: broadcast successful, tx_hash={tx_hash}")

            self._chain_high_water_mark[chain_id] = max(
                self._chain_high_water_mark.get(chain_id, -1), index
            )
            return True

        except Exception as exc:
            error_str = str(exc).lower()
            selector, error_name = decode_contract_error(exc)

            if "nonce too low" in error_str or "already known" in error_str:
                logger.info(f"Withdrawal #{index}: already broadcast to {chain_name}")
                self._chain_high_water_mark[chain_id] = max(
                    self._chain_high_water_mark.get(chain_id, -1), index
                )
                return True
            elif selector:
                logger.error(f"Withdrawal #{index}: contract error - {error_name}")
                return False
            else:
                logger.error(f"Withdrawal #{index}: failed - {exc}")
                return False

    async def _run_processor(self):
        """Main processing loop."""
        poll_interval = self.settings.withdrawal_poll_interval
        logger.info(f"Starting withdrawal processor with poll interval {poll_interval}s")

        # On startup, catch up any resolved-but-not-broadcast withdrawals
        await self._catch_up_missing_broadcasts()

        poll_count = 0
        catchup_interval = 10  # Run catch-up every N polls

        while self._is_running:
            try:
                # Periodically check for missed broadcasts (not just on startup)
                poll_count += 1
                if poll_count % catchup_interval == 0:
                    await self._catch_up_missing_broadcasts()

                pending_by_chain = await self._get_pending_withdrawals()

                for chain_id, withdrawals in pending_by_chain.items():
                    if not self._is_running:
                        break

                    chain_name = CHAIN_NAMES.get(chain_id, f"chain {chain_id}")
                    if withdrawals:
                        logger.info(f"Processing {len(withdrawals)} withdrawals for {chain_name}")

                    # Process in order for this chain
                    for withdrawal in withdrawals:
                        if not self._is_running:
                            break

                        success = await self._resolve_and_broadcast(withdrawal)
                        if not success:
                            # Withdrawal failed - run catch-up to handle any nonce gaps,
                            # then retry on next poll cycle
                            logger.warning(f"Withdrawal failed, pausing {chain_name} processing")
                            await self._catch_up_missing_broadcasts([chain_id])
                            break

            except Exception:
                logger.exception("Error during withdrawal processing poll")

            await asyncio.sleep(poll_interval)

        logger.info("Withdrawal processor stopped")

    async def start(self):
        """Start the withdrawal processor."""
        if self._is_running:
            logger.warning("Withdrawal processor is already running")
            return

        self._is_running = True
        logger.info("Starting withdrawal processor...")
        self._task = asyncio.create_task(self._run_processor())

    async def stop(self):
        """Stop the withdrawal processor gracefully."""
        if not self._is_running:
            return

        logger.info("Stopping withdrawal processor...")
        self._is_running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        self._chain_high_water_mark.clear()
        logger.info("Withdrawal processor stopped")


_processor_instance: Optional[WithdrawalProcessor] = None


def get_withdrawal_processor() -> WithdrawalProcessor:
    """Return singleton processor instance."""
    global _processor_instance
    if _processor_instance is None:
        _processor_instance = WithdrawalProcessor()
    return _processor_instance
