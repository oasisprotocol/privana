"""Service for processing withdrawals: resolve on Sapphire and broadcast to destination chains."""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Set

from eth_abi import decode
from hexbytes import HexBytes
from web3 import AsyncWeb3, Web3
from web3.exceptions import TransactionNotFound
from web3.providers import AsyncHTTPProvider

from src.abi.accounting import ERROR_SELECTORS as _ERROR_SELECTORS_BYTES
from src.config import CHAIN_NAMES, load_settings
from src.services.accounting_contract import AccountingContractService
from src.services.rpc_identity import require_verified_web3

logger = logging.getLogger(__name__)


# Convert bytes selectors to hex strings for substring matching in error messages
ERROR_SELECTORS: dict[str, str] = {
    selector.hex(): name for selector, name in _ERROR_SELECTORS_BYTES.items()
}


def decode_contract_error(exc: Exception) -> tuple[str, str]:
    """Decode a contract error to (selector, name). Returns ('', error_str) if unknown."""
    error_str = str(exc).lower()
    for selector, name in ERROR_SELECTORS.items():
        if selector in error_str:
            return selector, name
    return "", str(exc)


_MIN_RPC_INTERVAL = 0.1

# Consecutive nonce observations with an unmined signed withdrawal before alarming
_STUCK_NONCE_ALARM_CYCLES = 10


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
        self._contract_address = Web3.to_checksum_address(self.settings.accounting_contract_address)

        self._sapphire_web3 = AsyncWeb3(AsyncHTTPProvider(self.settings.sapphire_rpc_url))
        self._contract = self._sapphire_web3.eth.contract(
            address=self._contract_address,
            abi=self.accounting_service.contract.abi,
        )

        self._is_running = False
        self._task: Optional[asyncio.Task] = None
        # Track highest processed withdrawal index per chain (for sequential processing)
        self._chain_high_water_mark: Dict[int, int] = {}
        self._last_rpc_call: float = 0
        self._destination_web3: Dict[int, AsyncWeb3] = {}
        self._evm_address: Optional[str] = None
        # Divergence is logged critical the first time and error after, to stay loud
        # without flooding every poll cycle
        self._nonce_divergence_reported: Set[int] = set()
        # Consecutive nonce reads where one of our signed withdrawals sits unmined
        self._stuck_nonce_cycles: Dict[int, int] = {}
        # (chain_id, withdrawal_index) -> hash of the bytes we last broadcast.
        # resolveWithdrawal re-signs at the current gasPrices[chainId], so after a
        # setGasPrice the fresh bytes hash differently from what we actually sent.
        # In-process only (cleared in stop()): after a restart there is nothing to
        # remember and recognition falls back to the freshly signed bytes.
        self._last_broadcast_hash: Dict[tuple[int, int], str] = {}

    async def _rate_limited_call(self, coro_factory):
        """Execute an async call with rate limiting and retries.

        Args:
            coro_factory: A callable that returns a coroutine. Must be a callable
                          (not a pre-created coroutine) to allow retry logic to
                          create fresh coroutines on each attempt.
        """
        now = time.time()
        time_since_last = now - self._last_rpc_call
        if time_since_last < _MIN_RPC_INTERVAL:
            await asyncio.sleep(_MIN_RPC_INTERVAL - time_since_last)

        max_retries = 5
        base_delay = 1.0

        for attempt in range(max_retries):
            try:
                self._last_rpc_call = time.time()
                return await coro_factory()
            except Exception as e:
                if attempt < max_retries - 1:
                    delay = base_delay * (2**attempt)
                    logger.warning(f"RPC error, retrying in {delay}s (attempt {attempt + 1}): {e}")
                    await asyncio.sleep(delay)
                    continue
                raise

    def _get_destination_web3(self, chain_id: int) -> AsyncWeb3:
        """Get the destination chain's startup-verified client (see `rpc_identity`).

        Withdrawals are signed for one chain ID and broadcast here, so an endpoint filed
        under the wrong chain would send a signed transaction somewhere it never belonged.
        """
        return require_verified_web3(chain_id, self.settings.chain_rpc_urls, self._destination_web3)

    async def _get_evm_address(self) -> str:
        """Get the EVM address used for withdrawals."""
        if self._evm_address is None:
            self._evm_address = await self._rate_limited_call(
                lambda: self._contract.functions.evmAddress().call()
            )
        return self._evm_address

    async def _chain_nonce_state(self, chain_id: int) -> Optional[tuple[int, int]]:
        """Read ``(contract_next_nonce, chain_latest_nonce)``, or None if unsafe.

        ``nonces[chainId]`` is the nonce the contract embeds in the next withdrawal it
        signs for that chain. It starts at 0 and ``EVMSignerAndVerifier`` only ever
        increments it (``getEVMNonceAndIncrement``) — there is no setter, so divergence
        cannot be repaired from this side.

        ``contract >= chain`` is safe: equal means caught up, greater means signed
        withdrawals still awaiting broadcast. ``contract < chain`` is not — the chain has
        already spent nonces the contract never issued, so everything signed from now on
        reuses a spent nonce and can never land. None makes callers refuse the chain
        outright rather than sign into that gap.

        The gate compares against ``pending``, so a queued transaction counts as spent.
        ``latest`` is the nonce returned, because the two disagree exactly when a broadcast
        is stuck unmined, and that gap is what still needs re-broadcasting.
        """
        contract_next_nonce = await self._rate_limited_call(
            lambda: self._contract.functions.nonces(chain_id).call()
        )

        evm_address = await self._get_evm_address()
        dest_web3 = self._get_destination_web3(chain_id)
        # "pending" so queued-but-unmined transactions count as spent nonces
        chain_pending_nonce = await self._rate_limited_call(
            lambda: dest_web3.eth.get_transaction_count(evm_address, "pending")
        )
        # "latest" counts only mined nonces; anything above it has not landed yet
        chain_latest_nonce = await self._rate_limited_call(
            lambda: dest_web3.eth.get_transaction_count(evm_address, "latest")
        )

        if contract_next_nonce >= chain_pending_nonce:
            self._nonce_divergence_reported.discard(chain_id)
            self._track_stuck_nonce(
                chain_id, contract_next_nonce, chain_pending_nonce, chain_latest_nonce
            )
            return contract_next_nonce, chain_latest_nonce

        chain_name = CHAIN_NAMES.get(chain_id, f"chain {chain_id}")
        message = (
            f"{chain_name}: REFUSING WITHDRAWALS - contract nonce {contract_next_nonce} is behind "
            f"the pending nonce {chain_pending_nonce} of {evm_address}. The chain has spent "
            f"{chain_pending_nonce - contract_next_nonce} nonce(s) the contract never issued, so "
            f"every transaction signed for chain {chain_id} would reuse a spent nonce and never "
            f"pay out. nonces({chain_id}) can only advance by signing, so this needs operator "
            f"intervention."
        )
        if chain_id in self._nonce_divergence_reported:
            logger.error(message)
        else:
            self._nonce_divergence_reported.add(chain_id)
            logger.critical(message)
        return None

    def _track_stuck_nonce(
        self,
        chain_id: int,
        contract_next_nonce: int,
        chain_pending_nonce: int,
        chain_latest_nonce: int,
    ) -> None:
        """Alarm when one of our signed withdrawals has been broadcast but never mines.

        ``pending > latest`` means the node is holding a transaction it has not mined, and
        ``contract_next > latest`` means a nonce the contract signed is what sits there.
        Re-broadcasting the same bytes cannot rescue an underpriced transaction - only a
        higher ``gasPrices[chainId]``, which re-signs the payout, can - so a gap that
        persists across cycles needs an operator.
        """
        stuck = (
            chain_pending_nonce > chain_latest_nonce and contract_next_nonce > chain_latest_nonce
        )
        if not stuck:
            self._stuck_nonce_cycles.pop(chain_id, None)
            return

        cycles = self._stuck_nonce_cycles.get(chain_id, 0) + 1
        self._stuck_nonce_cycles[chain_id] = cycles
        if cycles >= _STUCK_NONCE_ALARM_CYCLES:
            chain_name = CHAIN_NAMES.get(chain_id, f"chain {chain_id}")
            logger.error(
                f"{chain_name}: nonce stuck: pending exceeds latest for {cycles} cycles - "
                f"resolved withdrawal may be underpriced, consider raising the published gas price"
            )

    async def _fetch_receipt(self, chain_id: int, tx_hash: str) -> Optional[Any]:
        """Receipt for ``tx_hash`` on ``chain_id``; None when absent or unreadable."""
        dest_web3 = self._get_destination_web3(chain_id)

        async def fetch_receipt():
            try:
                return await dest_web3.eth.get_transaction_receipt(tx_hash)
            except TransactionNotFound:
                return None

        try:
            return await self._rate_limited_call(fetch_receipt)
        except Exception as exc:
            logger.warning(f"Could not look up receipt for {tx_hash} on chain {chain_id}: {exc}")
            return None

    def _remember_broadcast(self, chain_id: int, index: int, tx_hash: str) -> None:
        """Record the hash we sent, normalised, so a re-sign at a new price stays traceable."""
        self._last_broadcast_hash[(chain_id, index)] = HexBytes(tx_hash).to_0x_hex()

    @staticmethod
    def _expected_tx_hash(signed_tx: Any) -> str:
        """Compute the hash a node files ``signed_tx`` under: keccak256 of its raw bytes."""
        raw_bytes = AccountingContractService._as_raw_tx_bytes(signed_tx)
        return HexBytes(Web3.keccak(raw_bytes)).to_0x_hex()

    async def _find_broadcast_tx(self, chain_id: int, index: int, signed_tx: Any) -> Optional[str]:
        """Return the hash of the transaction that proves this withdrawal was paid, or None
        when nothing proves it.

        A rejected broadcast only proves the *nonce* is unusable, never that our
        transaction is what used it: "nonce too low" (geth), "already known" and "invalid
        nonce" (Oasis) read identically whether the withdrawal landed or an unrelated
        transaction burned the nonce. Only a status-1 receipt for the exact signed bytes
        proves payment: a status-0 receipt means the payout reverted and a mempool entry
        is merely in flight, so both answer None. None means "not proven", and the caller
        must leave the withdrawal unresolved.

        Two hashes can carry that proof, and the one we actually broadcast is checked
        first: ``resolveWithdrawal`` re-signs at the *current* ``gasPrices[chainId]``, so
        once an operator republishes a price the freshly resolved bytes hash differently
        from the ones already on the chain, and looking up only today's hash would ask
        after a transaction that never existed and report a paid withdrawal as unproven.
        """
        expected_hash = self._expected_tx_hash(signed_tx)
        remembered = self._last_broadcast_hash.get((chain_id, index))
        candidates = [] if remembered is None else [remembered]
        if expected_hash != remembered:
            candidates.append(expected_hash)

        for candidate in candidates:
            receipt = await self._fetch_receipt(chain_id, candidate)
            if receipt is None:
                continue
            if receipt["status"] == 1:
                return candidate
            if candidate == remembered:
                logger.error(
                    f"Withdrawal #{index}: our earlier broadcast {remembered} mined on chain "
                    f"{chain_id} with status {receipt['status']} - the payout reverted, so this "
                    f"withdrawal is NOT paid; manual investigation required"
                )
            else:
                logger.error(
                    f"Withdrawal #{index}: tx {expected_hash} mined on chain {chain_id} with "
                    f"status {receipt['status']} - the payout reverted, so this withdrawal is "
                    f"NOT paid and stays unresolved; manual investigation required"
                )
            return None

        # Nothing mined. A mempool entry proves nothing either way, so this lookup only
        # reports, and today's hash is the one a retry would put back on the wire.
        dest_web3 = self._get_destination_web3(chain_id)

        async def fetch_transaction():
            try:
                return await dest_web3.eth.get_transaction(expected_hash)
            except TransactionNotFound:
                return None

        try:
            pending_tx = await self._rate_limited_call(fetch_transaction)
        except Exception as exc:
            logger.warning(
                f"Could not look up pending tx for {expected_hash} on chain {chain_id}: {exc}"
            )
            return None

        if pending_tx is not None:
            logger.info(
                f"Withdrawal #{index}: tx {expected_hash} in flight on chain {chain_id} with no "
                f"receipt yet - retrying next cycle"
            )

        return None

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

                nonce_state = await self._chain_nonce_state(chain_id)
                if nonce_state is None:
                    continue
                contract_next_nonce, chain_latest_nonce = nonce_state

                if contract_next_nonce == chain_latest_nonce:
                    logger.info(f"{chain_name}: no missing broadcasts")
                    continue

                missing_count = contract_next_nonce - chain_latest_nonce
                logger.info(
                    f"{chain_name}: found {missing_count} un-mined nonce(s) "
                    f"(nonces {chain_latest_nonce} to {contract_next_nonce - 1})"
                )

                # From "latest", not "pending": a nonce the node holds unmined is a stuck
                # broadcast, and re-sending is how a replacement re-signed at a raised gas
                # price gets out. Identical bytes are harmless - the node answers "already
                # known" and `_find_broadcast_tx` reports in flight, so the next cycle retries.
                target_nonces = set(range(chain_latest_nonce, contract_next_nonce))
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
                lambda: contract_reader.functions.withdrawalCount().call()
            )
        except Exception as e:
            logger.error(f"{chain_name}: failed to get withdrawal count: {e}")
            return

        logger.info(
            f"{chain_name}: scanning {total_withdrawals} withdrawals for {len(target_nonces)} missing nonces"
        )

        # Scan in reverse - missing broadcasts are likely recent
        for index in range(total_withdrawals - 1, -1, -1):
            if len(found_nonces) >= len(target_nonces):
                break

            try:
                result = await self._rate_limited_call(
                    lambda idx=index: contract_reader.functions.withdrawals(idx).call()
                )
            except Exception as exc:
                logger.warning(f"Withdrawal #{index}: failed to read, skipping: {exc}")
                continue

            resolved = result[5]
            tx_identifier = result[6]

            if not resolved:
                # Not yet processed by the main polling loop, skip
                continue
            if not tx_identifier or len(tx_identifier) < 32:
                logger.warning(
                    f"Withdrawal #{index}: resolved but has invalid txIdentifier, skipping"
                )
                continue

            # Decode nonce from txIdentifier
            nonce = decode(["uint64"], tx_identifier)[0]
            if nonce not in target_nonces:
                continue

            # Get chain_id for this withdrawal
            token_id_bytes = result[4]
            try:
                context = await self.accounting_service._get_token_context(HexBytes(token_id_bytes))
                withdrawal_chain_id = context.chain_id
            except Exception as e:
                logger.warning(f"Withdrawal #{index}: failed to get token context - {e}")
                withdrawal_chain_id = 0

            if withdrawal_chain_id != chain_id:
                continue

            # Found a missing one - broadcast it
            logger.info(f"Withdrawal #{index}: broadcasting missing nonce {nonce}")
            signed_tx = await self._rate_limited_call(
                lambda idx=index: contract_reader.functions.resolveWithdrawal(idx).call()
            )
            try:
                tx_hash = await self._rate_limited_call(
                    lambda: self.accounting_service._send_raw_transaction(chain_id, signed_tx)
                )
                logger.info(f"Withdrawal #{index}: broadcast successful, tx_hash={tx_hash}")
                self._remember_broadcast(chain_id, index, tx_hash)
                found_nonces.add(nonce)
            except Exception as exc:
                broadcast_hash = await self._find_broadcast_tx(chain_id, index, signed_tx)
                if broadcast_hash is not None:
                    logger.info(f"Withdrawal #{index}: already mined, tx_hash={broadcast_hash}")
                    found_nonces.add(nonce)
                else:
                    logger.error(
                        f"Withdrawal #{index}: broadcast failed and no successful transaction "
                        f"matching the signed payload exists on {chain_name} - nonce {nonce} may "
                        f"have been spent by a different transaction, leaving this withdrawal "
                        f"unpayable: "
                        f"{exc}"
                    )

            if index > 0 and index % 100 == 0:
                logger.info(
                    f"{chain_name}: scanned {index}/{total_withdrawals} withdrawals, found {len(found_nonces)}/{len(target_nonces)}"
                )

        logger.info(
            f"{chain_name}: catch-up complete, broadcast {len(found_nonces)}/{len(target_nonces)} missing"
        )

    async def _get_pending_withdrawals(self) -> Dict[int, List[Dict]]:
        """Get pending withdrawals grouped by chain for ordered processing."""
        try:
            result = await self._rate_limited_call(
                lambda: self.accounting_service.get_all_pending_withdrawals()
            )

            pending = result.get("pending", [])
            current_block = result.get("current_block", 0)

            # Filter by block delay and not already processed (per-chain high water mark)
            eligible = [
                w
                for w in pending
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
                lambda: contract_reader.functions.withdrawals(index).call()
            )
            is_resolved = withdrawal_data[5]

            # Step 2: If not resolved, submit resolveWithdrawal and wait
            if not is_resolved:
                logger.info(f"Withdrawal #{index}: submitting resolveWithdrawal")
                await self._rate_limited_call(
                    lambda: self.accounting_service.resolve_withdrawal(index)
                )
                logger.info(f"Withdrawal #{index}: submitted")

                # Wait for resolution (poll until resolved)
                for _ in range(self.settings.withdrawal_resolution_timeout):
                    await asyncio.sleep(1)
                    withdrawal_data = await self._rate_limited_call(
                        lambda: contract_reader.functions.withdrawals(index).call()
                    )
                    if withdrawal_data[5]:  # resolved
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
                lambda: contract_reader.functions.resolveWithdrawal(index).call()
            )

            # Step 4: Broadcast to destination chain
            logger.info(f"Withdrawal #{index}: broadcasting to {chain_name}")
            try:
                tx_hash = await self._rate_limited_call(
                    lambda: self.accounting_service._send_raw_transaction(chain_id, signed_tx)
                )
            except Exception as exc:
                # The hash we actually sent is checked before the freshly signed one's: a
                # setGasPrice in between re-signs to bytes that hash differently.
                tx_hash = await self._find_broadcast_tx(chain_id, index, signed_tx)
                if tx_hash is None:
                    logger.error(
                        f"Withdrawal #{index}: broadcast to {chain_name} failed and no "
                        f"successful transaction matching the signed payload "
                        f"({self._expected_tx_hash(signed_tx)}) exists there - leaving it "
                        f"unresolved rather than marking it paid: {exc}"
                    )
                    return False
                if tx_hash == self._expected_tx_hash(signed_tx):
                    logger.info(
                        f"Withdrawal #{index}: already mined on {chain_name}, tx_hash={tx_hash}"
                    )
                else:
                    logger.info(
                        f"Withdrawal #{index}: paid on {chain_name} by our earlier broadcast, "
                        f"tx_hash={tx_hash}"
                    )
            else:
                logger.info(f"Withdrawal #{index}: broadcast successful, tx_hash={tx_hash}")
                self._remember_broadcast(chain_id, index, tx_hash)

            self._chain_high_water_mark[chain_id] = max(
                self._chain_high_water_mark.get(chain_id, -1), index
            )
            return True

        except Exception as exc:
            selector, error_name = decode_contract_error(exc)
            if selector:
                logger.error(f"Withdrawal #{index}: contract error - {error_name}")
            else:
                logger.error(f"Withdrawal #{index}: failed - {exc}")
            return False

    async def _process_chain(self, chain_id: int, withdrawals: List[Dict]):
        """Process one chain's pending withdrawals in index order.

        The nonce gate runs once per chain, before anything is signed: on divergence the
        whole chain is skipped, so no withdrawal is resolved against a nonce the
        destination chain has already spent.
        """
        chain_name = CHAIN_NAMES.get(chain_id, f"chain {chain_id}")

        try:
            nonce_state = await self._chain_nonce_state(chain_id)
        except Exception as exc:
            logger.error(f"{chain_name}: nonce readiness check failed, skipping chain: {exc}")
            return

        if nonce_state is None:
            logger.error(
                f"{chain_name}: skipping {len(withdrawals)} pending withdrawal(s) - "
                f"destination nonce check failed"
            )
            return

        if withdrawals:
            logger.info(f"Processing {len(withdrawals)} withdrawals for {chain_name}")

        for withdrawal in withdrawals:
            if not self._is_running:
                return

            if not await self._resolve_and_broadcast(withdrawal):
                # Withdrawal failed - run catch-up to handle any nonce gaps,
                # then retry on next poll cycle
                logger.warning(f"Withdrawal failed, pausing {chain_name} processing")
                await self._catch_up_missing_broadcasts([chain_id])
                return

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

                    await self._process_chain(chain_id, withdrawals)

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
        self._nonce_divergence_reported.clear()
        self._stuck_nonce_cycles.clear()
        self._last_broadcast_hash.clear()
        logger.info("Withdrawal processor stopped")


_processor_instance: Optional[WithdrawalProcessor] = None


def get_withdrawal_processor() -> WithdrawalProcessor:
    """Return singleton processor instance."""
    global _processor_instance
    if _processor_instance is None:
        _processor_instance = WithdrawalProcessor()
    return _processor_instance
