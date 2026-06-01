"""Service for processing withdrawals: resolve on Sapphire and broadcast to destination chains."""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Set

from eth_abi import decode
from hexbytes import HexBytes
from web3 import AsyncWeb3, Web3
from web3.providers import AsyncHTTPProvider

from src.abi.accounting import ERROR_SELECTORS as _ERROR_SELECTORS_BYTES
from src.config import CHAIN_NAMES, load_settings
from src.config.bridge_validation import destination_chain_ids
from src.services.accounting_contract import (
    AccountingContractService,
    _decode_bridge_tx_identifier,
)
from src.services.custody_tx_executor import (
    CustodyTxExecutor,
    CustodyTxKind,
    CustodyTxRequest,
)

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

# A handful of consecutive fetch failures means the processor is blind, not idle.
_PENDING_POLL_CRITICAL_THRESHOLD = 5


class WithdrawalProcessor:
    """Polls the Accounting contract for pending withdrawals, resolves them on
    Sapphire to obtain signed transactions, and broadcasts those transactions
    to the appropriate destination chains.

    Withdrawals are processed sequentially per chain to preserve nonce ordering.
    A periodic catch-up pass re-broadcasts any resolved withdrawals whose
    transactions have not yet landed on-chain.
    """

    def __init__(self, custody_executor: Optional[CustodyTxExecutor] = None):
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
        self._last_rpc_call: float = 0
        self._destination_web3: Dict[int, AsyncWeb3] = {}
        self._evm_address: Optional[str] = None
        self._custody_executor = custody_executor
        self._consecutive_poll_failures = 0

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
        """Get or create AsyncWeb3 instance for a destination chain."""
        if chain_id not in self._destination_web3:
            rpc_url = self.settings.chain_rpc_urls.get(chain_id)
            if not rpc_url:
                raise ValueError(f"No RPC URL configured for chain {chain_id}")
            self._destination_web3[chain_id] = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        return self._destination_web3[chain_id]

    async def _get_evm_address(self) -> str:
        """Get the EVM address used for withdrawals."""
        if self._evm_address is None:
            self._evm_address = await self._rate_limited_call(
                lambda: self._contract.functions.evmAddress().call()
            )
        return self._evm_address

    def _get_custody_executor(self) -> CustodyTxExecutor:
        if self._custody_executor is None:
            from src.services.custody_tx_executor import get_custody_tx_executor

            self._custody_executor = get_custody_tx_executor(self.accounting_service)
        return self._custody_executor

    def _derive_kind(self, chain_id: int, is_bridge_asset: bool) -> CustodyTxKind:
        if is_bridge_asset:
            return (
                CustodyTxKind.SAPPHIRE_RELEASE
                if chain_id == self.settings.sapphire_chain_id
                else CustodyTxKind.BASE_MINT
            )
        return CustodyTxKind.NORMAL_WITHDRAWAL

    @staticmethod
    def _decode_normal_withdrawal_nonce(tx_identifier: bytes) -> int:
        return decode(["uint64"], tx_identifier)[0]

    async def _enqueue_for_executor(
        self,
        chain_id: int,
        evm_nonce: int,
        kind: CustodyTxKind,
        id: str,
        signed_tx: bytes,
        *,
        route_address: Optional[str] = None,
        max_gas_cost: Optional[int] = None,
        withdrawal_index: Optional[int] = None,
        to_address: Optional[str] = None,
        amount: Optional[int] = None,
    ) -> None:
        # Bridge metadata is threaded onto the request so the executor's
        # kind-routed preflight can reconstruct policy gates after a restart
        # without relying on in-memory closures.
        executor = self._get_custody_executor()
        await executor.enqueue(
            CustodyTxRequest(
                chain_id=chain_id,
                evm_nonce=evm_nonce,
                kind=kind,
                id=id,
                signed_tx=bytes(signed_tx),
                route_address=route_address,
                max_gas_cost=max_gas_cost,
                withdrawal_index=withdrawal_index,
                to_address=to_address,
                amount=amount,
            )
        )

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
                    lambda: self._contract.functions.nonces(chain_id).call()
                )

                # Get current nonce on destination chain (what's been broadcast)
                evm_address = await self._get_evm_address()
                dest_web3 = self._get_destination_web3(chain_id)
                chain_current_nonce = await self._rate_limited_call(
                    lambda: dest_web3.eth.get_transaction_count(evm_address)
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

            # Bridge txIdentifier (128 bytes) layout is
            #   abi.encode(uint256 destChainId, uint64 destTxNonce, address, uint256)
            # so decoding the first uint64 slot reads destChainId, not the
            # nonce we want. Non-bridge withdrawals carry abi.encode(uint64).
            is_bridge_asset = len(tx_identifier) >= 128
            route_address: Optional[str] = None
            max_gas_cost: int = 0
            if is_bridge_asset:
                dest_chain_id, nonce, route_address, max_gas_cost = _decode_bridge_tx_identifier(
                    tx_identifier
                )
                # BridgeAsset tokens have TokenContext.chain_id=None, so
                # routing must come from the decoded destChainId.
                withdrawal_chain_id = dest_chain_id
            else:
                nonce = decode(["uint64"], tx_identifier)[0]
                token_id_bytes = result[4]
                try:
                    context = await self.accounting_service._get_token_context(
                        HexBytes(token_id_bytes)
                    )
                    withdrawal_chain_id = context.chain_id
                except Exception as e:
                    logger.warning(f"Withdrawal #{index}: failed to get token context - {e}")
                    withdrawal_chain_id = 0

            if withdrawal_chain_id != chain_id or nonce not in target_nonces:
                continue

            # Sapphire records defer signing — preflight regenerates per attempt.
            logger.info(f"Withdrawal #{index}: enqueueing missing nonce {nonce}")
            bridge_chain_ids = destination_chain_ids(self.settings)
            sapphire_chain_id = self.settings.sapphire_chain_id
            if is_bridge_asset and chain_id in bridge_chain_ids:
                signed_tx = await self._rate_limited_call(
                    lambda idx=index: self.accounting_service.resolve_bridge_withdrawal(idx)
                )
            elif is_bridge_asset and chain_id == sapphire_chain_id:
                signed_tx = b""
            else:
                signed_tx = await self._rate_limited_call(
                    lambda idx=index: contract_reader.functions.resolveWithdrawal(idx).call()
                )
            try:
                kind = self._derive_kind(
                    chain_id=chain_id,
                    is_bridge_asset=is_bridge_asset,
                )
                await self._enqueue_for_executor(
                    chain_id=chain_id,
                    evm_nonce=nonce,
                    kind=kind,
                    id=str(index),
                    signed_tx=signed_tx,
                    route_address=(route_address if is_bridge_asset else None),
                    max_gas_cost=(max_gas_cost if is_bridge_asset else None),
                    withdrawal_index=index,
                    to_address=(result[1] if is_bridge_asset else None),
                    amount=(int(result[2]) if is_bridge_asset else None),
                )
                found_nonces.add(nonce)
            except Exception as exc:
                logger.error(f"Withdrawal #{index}: enqueue failed - {exc}")

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

            # Skip indices the executor already tracks (any status): the executor
            # halts on blocking statuses, so re-pulling a stuck index only churns
            # redundant resolve attempts. Double-spend is prevented by enqueue's
            # per-(chain_id, evm_nonce) idempotency and the on-chain idempotent
            # resolve — not by this filter. Fail closed on read errors: a chain
            # whose record set is unknown is skipped this poll.
            known_by_chain: Dict[int, Set[int]] = {}
            skip_chains: Set[int] = set()
            executor = self._get_custody_executor()
            for w in pending:
                cid = w.get("chain_id")
                if not cid or cid in known_by_chain or cid in skip_chains:
                    continue
                try:
                    records = executor.get_records_for_chain(cid)
                except Exception:
                    logger.exception(
                        "Could not read executor records for chain %s; "
                        "skipping pending withdrawals for this chain this poll",
                        cid,
                    )
                    skip_chains.add(cid)
                    continue
                known_by_chain[cid] = {
                    r.withdrawal_index for r in records if r.withdrawal_index is not None
                }

            eligible = [
                w
                for w in pending
                if (current_block - w["block_number"] >= 1)
                and w.get("chain_id", 0) not in skip_chains
                and w["index"] not in known_by_chain.get(w.get("chain_id", 0), set())
            ]

            # Group by chain_id and sort by index within each chain
            # Skip withdrawals with invalid chain_id (None or 0) - these have unregistered tokens
            by_chain: Dict[int, List[Dict]] = {}
            for w in eligible:
                chain_id = w.get("chain_id")
                if not chain_id:
                    if w.get("is_bridge_asset"):
                        # BridgeAsset records must carry a decoded destChainId from
                        # the on-chain txIdentifier by the time they reach this loop.
                        # A missing chain_id here means the upstream decoder did not
                        # run — a cross-layer contract violation, not a routing issue.
                        logger.error(
                            f"Bridge withdrawal #{w.get('index')} missing decoded "
                            f"destChainId — upstream txIdentifier decode did not run"
                        )
                    # else: token not registered - already logged in accounting_contract.py
                    # Skip to avoid infinite retries; requires token registration to fix
                    continue
                if chain_id not in by_chain:
                    by_chain[chain_id] = []
                by_chain[chain_id].append(w)

            # Sort each chain's withdrawals by index
            for chain_id in by_chain:
                by_chain[chain_id].sort(key=lambda x: x["index"])

            self._consecutive_poll_failures = 0
            return by_chain

        except Exception:
            self._consecutive_poll_failures += 1
            logger.exception(
                "Error getting pending withdrawals (consecutive=%d); treating as no work this poll",
                self._consecutive_poll_failures,
            )
            if self._consecutive_poll_failures == _PENDING_POLL_CRITICAL_THRESHOLD:
                logger.critical(
                    "Pending-withdrawal fetch failed %d consecutive polls — withdrawal "
                    "processing is stalled; operator attention required",
                    self._consecutive_poll_failures,
                )
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

            # Step 2: If not resolved, submit the resolve tx and wait.
            # BridgeAsset must use resolveBridgeWithdrawal — resolveWithdrawal
            # reverts UnsupportedTokenType.
            is_bridge_asset = bool(withdrawal.get("is_bridge_asset"))
            if not is_resolved:
                if is_bridge_asset:
                    logger.info(f"Withdrawal #{index}: submitting resolveBridgeWithdrawal")
                    await self._rate_limited_call(
                        lambda: self.accounting_service.submit_resolve_bridge_withdrawal(index)
                    )
                else:
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

            # BridgeAsset must use `resolveBridgeWithdrawal` — `resolveWithdrawal`
            # reverts UnsupportedTokenType. Sapphire defers signing to the preflight.
            logger.info(f"Withdrawal #{index}: getting signed transaction")
            bridge_chain_ids = destination_chain_ids(self.settings)
            sapphire_chain_id = self.settings.sapphire_chain_id
            if is_bridge_asset and chain_id in bridge_chain_ids:
                signed_tx = await self._rate_limited_call(
                    lambda: self.accounting_service.resolve_bridge_withdrawal(index)
                )
            elif is_bridge_asset and chain_id == sapphire_chain_id:
                signed_tx = b""
            else:
                signed_tx = await self._rate_limited_call(
                    lambda: contract_reader.functions.resolveWithdrawal(index).call()
                )

            # Step 4: Hand the signed tx to the custody executor.
            logger.info(f"Withdrawal #{index}: enqueueing for executor on {chain_name}")
            tx_identifier = withdrawal_data[6]
            if is_bridge_asset:
                evm_nonce = int(withdrawal["dest_tx_nonce"])
            else:
                evm_nonce = self._decode_normal_withdrawal_nonce(tx_identifier)
            kind = self._derive_kind(chain_id=chain_id, is_bridge_asset=is_bridge_asset)
            await self._enqueue_for_executor(
                chain_id=chain_id,
                evm_nonce=evm_nonce,
                kind=kind,
                id=str(index),
                signed_tx=signed_tx,
                route_address=(withdrawal.get("route_address") if is_bridge_asset else None),
                max_gas_cost=(int(withdrawal.get("max_gas_cost", 0)) if is_bridge_asset else None),
                withdrawal_index=index,
                to_address=(withdrawal_data[1] if is_bridge_asset else None),
                amount=(int(withdrawal_data[2]) if is_bridge_asset else None),
            )

            return True

        except Exception as exc:
            selector, error_name = decode_contract_error(exc)
            if selector:
                logger.error(f"Withdrawal #{index}: contract error - {error_name}")
                return False
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

        logger.info("Withdrawal processor stopped")


_processor_instance: Optional[WithdrawalProcessor] = None


def get_withdrawal_processor() -> WithdrawalProcessor:
    """Return singleton processor instance."""
    global _processor_instance
    if _processor_instance is None:
        _processor_instance = WithdrawalProcessor()
    return _processor_instance
