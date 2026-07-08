"""Deposit discovery: scan source-chain logs for inbound ERC20 transfers.

Read-only companion to POST /deposits/check for external-wallet deposits,
where no webhook tells the backend the tx hash. Scans finalized blocks only,
so results are reorg-stable and immediately consumable by /deposits/check.
Native transfers emit no logs and are not discoverable here; they remain
verifiable by submitting the tx hash to /deposits/check directly.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from aiohttp import ClientError
from web3 import AsyncWeb3, Web3
from web3.exceptions import Web3Exception
from web3.providers import AsyncHTTPProvider

from src.config.chain_config import (
    CHAIN_CONFIGS,
    TRANSFER_EVENT_TOPIC,
    get_finality_depth,
)
from src.services.cache import AsyncTTLCache
from src.services.deposit_processor import compute_deposit_id

logger = logging.getLogger(__name__)

# The descending scan stops once this many candidates are collected, so under
# transfer-spam to a known deposit address the cap drops the oldest transfers,
# never a fresh deposit.
MAX_PENDING_CANDIDATES = 20
# Each candidate not already in-flight costs one Sapphire processed-check call.
# This bound keeps a window dense with already-credited transfers from turning
# one request into unbounded Sapphire reads.
MAX_CLASSIFIED_CANDIDATES = 60
RPC_TIMEOUT_SECONDS = 10
# Scan results are cached briefly so refresh-spam (or IP-rotated abuse) costs
# one chain scan per (address, params) per window instead of one per request.
SCAN_CACHE_TTL_SECONDS = 30
_SCAN_CACHE_MAXSIZE = 1024


class DiscoveryRPCError(Exception):
    """Source-chain RPC failed or timed out during a discovery scan."""


class DiscoveryNotConfiguredError(Exception):
    """No source-chain RPC URL is configured for the requested chain.

    Deployment fault, not caller error: the chain passed route validation
    (it is in CHAIN_CONFIGS) but settings carry no RPC URL for it.
    """


@dataclass(frozen=True)
class DiscoveredDeposit:
    """One uncredited inbound transfer, shaped to feed /deposits/check."""

    chain_id: int
    tx_hash: str
    log_index: int
    amount: int
    token_address: str
    token_id_hex: str
    block_number: int
    version: int
    status: str  # "discovered" | "processing"
    deposit_id_hex: Optional[str] = None  # set when status == "processing"


@dataclass(frozen=True)
class DiscoveryResult:
    pending: List[DiscoveredDeposit]
    scanned_from_block: int
    scanned_to_block: int


class DepositDiscoveryService:
    """Scans a source chain for uncredited ERC20 transfers to a deposit address."""

    def __init__(
        self,
        accounting_service,
        sweep_engine,
        chain_rpc_urls: Dict[int, str],
    ):
        self._accounting = accounting_service
        self._sweep = sweep_engine
        self._chain_rpc_urls = chain_rpc_urls
        self._web3_cache: Dict[int, AsyncWeb3] = {}
        self._scan_cache: AsyncTTLCache[tuple, DiscoveryResult] = AsyncTTLCache(
            maxsize=_SCAN_CACHE_MAXSIZE, ttl=SCAN_CACHE_TTL_SECONDS
        )

    def _get_web3(self, chain_id: int) -> AsyncWeb3:
        if chain_id not in self._web3_cache:
            rpc_url = self._chain_rpc_urls.get(chain_id)
            if not rpc_url:
                raise DiscoveryNotConfiguredError(f"No RPC URL configured for chain {chain_id}")
            self._web3_cache[chain_id] = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        return self._web3_cache[chain_id]

    async def discover_pending_deposits(
        self,
        deposit_address: str,
        beneficiary: str,
        chain_id: int,
        version: int,
        token_address: Optional[str] = None,
        lookback_blocks: Optional[int] = None,
    ) -> DiscoveryResult:
        """Return uncredited finalized ERC20 transfers to ``deposit_address``.

        ``lookback_blocks`` defaults to the chain's ``discovery_lookback_blocks``
        and is clamped to ``discovery_max_lookback_blocks``.
        """
        cfg = CHAIN_CONFIGS.get(chain_id)
        if cfg is None:
            raise ValueError(f"Unsupported chain_id {chain_id}")
        lookback = min(
            lookback_blocks if lookback_blocks is not None else cfg.discovery_lookback_blocks,
            cfg.discovery_max_lookback_blocks,
        )
        if lookback < 1:
            raise ValueError("lookback_blocks must be positive")
        if lookback_blocks is not None:
            # Round explicit lookbacks up to full scan chunks: same getLogs call
            # count, but bounds cache-key cardinality so a caller cannot bypass
            # the scan cache with many distinct lookback values.
            chunk = cfg.discovery_scan_chunk_blocks
            lookback = min(
                (lookback + chunk - 1) // chunk * chunk, cfg.discovery_max_lookback_blocks
            )
        token_lower = token_address.lower() if token_address else None
        cache_key = (deposit_address.lower(), chain_id, version, token_lower, lookback)
        return await self._scan_cache.get_or_set_async(
            cache_key,
            lambda: self._scan(
                deposit_address=deposit_address,
                beneficiary=beneficiary,
                chain_id=chain_id,
                version=version,
                token_lower=token_lower,
                lookback=lookback,
            ),
        )

    async def _scan(
        self,
        deposit_address: str,
        beneficiary: str,
        chain_id: int,
        version: int,
        token_lower: Optional[str],
        lookback: int,
    ) -> DiscoveryResult:
        cfg = CHAIN_CONFIGS[chain_id]
        w3 = self._get_web3(chain_id)

        token_ids_by_address = await self._registered_erc20s(chain_id)
        if token_lower is not None:
            if token_lower not in token_ids_by_address:
                raise ValueError(
                    f"token_address {token_lower} is not a registered token on chain {chain_id}"
                )
            token_ids_by_address = {token_lower: token_ids_by_address[token_lower]}
        scan_addresses = [Web3.to_checksum_address(addr) for addr in token_ids_by_address]
        # Empty address list must never reach eth_getLogs: `address: []` matches
        # every contract on most providers, turning a no-op into a full-chain scan.
        if not scan_addresses:
            return DiscoveryResult(pending=[], scanned_from_block=0, scanned_to_block=0)

        latest = await self._rpc(w3.eth.get_block("latest"))
        to_block = latest["number"] - get_finality_depth(chain_id)
        from_block = max(to_block - lookback + 1, 0)
        if to_block < 0:
            return DiscoveryResult(pending=[], scanned_from_block=0, scanned_to_block=0)

        # Classify inside the descending scan so the cap counts returnable
        # candidates: already-credited transfers must not crowd out an older
        # uncredited deposit still inside the window.
        pending: List[DiscoveredDeposit] = []
        classified = 0
        bound_reached = False
        chunk_end = to_block
        # Oldest block fully examined. Every log in [scanned_from, to_block] was
        # classified; an early cap exit shrinks the interval to what was actually
        # covered instead of claiming the whole requested window.
        scanned_from = to_block
        while (
            chunk_end >= from_block and len(pending) < MAX_PENDING_CANDIDATES and not bound_reached
        ):
            chunk_start = max(chunk_end - cfg.discovery_scan_chunk_blocks + 1, from_block)
            logs = await self._rpc(
                w3.eth.get_logs(
                    {
                        "fromBlock": chunk_start,
                        "toBlock": chunk_end,
                        "address": scan_addresses,
                        "topics": [
                            TRANSFER_EVENT_TOPIC,
                            None,
                            _address_topic(deposit_address),
                        ],
                    }
                )
            )
            scanned_from = chunk_start
            for entry in sorted(
                logs, key=lambda e: (e.get("blockNumber", 0), e.get("logIndex", 0)), reverse=True
            ):
                candidate = _decode_transfer_log(entry)
                if candidate is None:
                    continue
                if candidate["amount"] < cfg.min_deposit_erc20_wei:
                    continue  # /deposits/check would reject it as dust
                if classified >= MAX_CLASSIFIED_CANDIDATES:
                    logger.info(
                        "Discovery classification bound reached; older transfers not "
                        "examined: address=%s chain_id=%s",
                        deposit_address,
                        chain_id,
                    )
                    bound_reached = True
                    # This entry was not classified, so its block is not covered.
                    scanned_from = candidate["block_number"] + 1
                    break
                classified += 1
                discovered = await self._classify(
                    candidate, chain_id, version, beneficiary, token_ids_by_address
                )
                if discovered is not None:
                    pending.append(discovered)
                    if len(pending) >= MAX_PENDING_CANDIDATES:
                        # Older logs in this block may exist below the cap cutoff.
                        scanned_from = candidate["block_number"] + 1
                        break
            chunk_end = chunk_start - 1

        return DiscoveryResult(
            pending=pending,
            scanned_from_block=scanned_from,
            scanned_to_block=to_block,
        )

    async def _classify(
        self,
        candidate: dict,
        chain_id: int,
        version: int,
        beneficiary: str,
        token_ids_by_address: Dict[str, str],
    ) -> Optional[DiscoveredDeposit]:
        """Classify one raw transfer; None means it is not a returnable candidate."""
        token_id_hex = token_ids_by_address.get(candidate["token_address"].lower())
        if token_id_hex is None:
            return None
        token_id = bytes.fromhex(token_id_hex.removeprefix("0x"))
        deposit_id = compute_deposit_id(
            chain_id, candidate["tx_hash"], token_id, candidate["log_index"]
        )
        deposit_id_hex = "0x" + deposit_id.hex()

        status = "discovered"
        record = self._sweep.get_record_by_deposit_id(deposit_id_hex)
        if record is not None:
            if record.beneficiary.lower() != beneficiary.lower():
                # Deposit addresses are per-user, so this cannot happen unless
                # state is corrupted; never expose another user's record.
                logger.warning(
                    "Discovery found sweep record with mismatched beneficiary: deposit_id=%s",
                    deposit_id_hex,
                )
                return None
            if not (record.error and not record.sweep_tx_hash):
                status = "processing"
            # else: errored before any sweep tx — /deposits/check cleans the
            # record up and retries, so report it as re-submittable below.
        if status == "discovered" and await self._accounting.is_deposit_processed(deposit_id):
            return None

        return DiscoveredDeposit(
            chain_id=chain_id,
            tx_hash=candidate["tx_hash"],
            log_index=candidate["log_index"],
            amount=candidate["amount"],
            token_address=Web3.to_checksum_address(candidate["token_address"]),
            token_id_hex=token_id_hex,
            block_number=candidate["block_number"],
            version=version,
            status=status,
            deposit_id_hex=deposit_id_hex if status == "processing" else None,
        )

    async def _registered_erc20s(self, chain_id: int) -> Dict[str, str]:
        """Map lowercase ERC20 contract address → token_id hex, for one chain."""
        tokens = await self._accounting.list_all_tokens()
        return {
            str(t["token_address"]).lower(): str(t["token_id"])
            for t in tokens
            if t.get("chain_id") == chain_id and t.get("token_type") == 1 and t.get("token_address")
        }

    @staticmethod
    async def _rpc(coro) -> Any:
        try:
            return await asyncio.wait_for(coro, timeout=RPC_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise DiscoveryRPCError("Source-chain RPC timed out") from exc
        # ClientError covers provider HTTP failures (e.g. 429) that are not
        # OSError; Web3Exception covers RPC-level errors like log-size limits.
        except (OSError, ClientError, Web3Exception) as exc:
            raise DiscoveryRPCError(f"Source-chain RPC failed: {exc}") from exc


def _address_topic(address: str) -> str:
    """Left-pad an EVM address to a 32-byte log topic."""
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def _decode_transfer_log(entry: dict) -> Optional[dict]:
    """Decode one Transfer log into a raw candidate dict, None if malformed."""
    tx_hash = entry.get("transactionHash")
    if isinstance(tx_hash, bytes):
        tx_hash = "0x" + tx_hash.hex()
    token_address = entry.get("address", "")
    if isinstance(token_address, bytes):
        token_address = "0x" + token_address.hex()
    data = entry.get("data", b"")
    if isinstance(data, bytes):
        amount = int.from_bytes(data[:32], "big")
    else:
        amount = int(data, 16) if data and data != "0x" else 0
    if not tx_hash or not token_address:
        return None
    return {
        "tx_hash": tx_hash,
        "log_index": entry.get("logIndex", 0),
        "amount": amount,
        "token_address": token_address,
        "block_number": entry.get("blockNumber", 0),
    }


_discovery_instance: Optional[DepositDiscoveryService] = None


def get_deposit_discovery_service() -> DepositDiscoveryService:
    """Return singleton discovery service, creating it on first call."""
    global _discovery_instance
    if _discovery_instance is None:
        from src.config import load_settings
        from src.services.accounting_contract import get_accounting_contract_service
        from src.services.deposit_processor import get_deposit_processor

        settings = load_settings()
        _discovery_instance = DepositDiscoveryService(
            accounting_service=get_accounting_contract_service(),
            sweep_engine=get_deposit_processor().sweep_engine,
            chain_rpc_urls=dict(settings.chain_rpc_urls),
        )
    return _discovery_instance
