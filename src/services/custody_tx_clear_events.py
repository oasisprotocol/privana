"""Sapphire watcher for ``Accounting.CustodyTxCleared`` that drives the
executor's ``_apply_clear_action``.

A clear resolving to ``deferred`` is parked and retried until terminal;
first-clear-wins means the operator cannot re-issue it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from enum import IntEnum
from pathlib import Path
from typing import Any, Optional

from eth_abi import encode as abi_encode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak
from hexbytes import HexBytes
from web3.exceptions import BadFunctionCallOutput, ContractLogicError

from src.config.chain_config import get_finality_depth
from src.utils.eth_logs import paginated_get_logs

logger = logging.getLogger(__name__)

# Blocks re-scanned behind the persisted cursor on every pass, deduped by the
# on-disk seen-set. Covers at-least-once delivery and a restart mid-window.
SAPPHIRE_CLEAR_OVERLAP_BLOCKS = 64

DEFAULT_POLL_INTERVAL_SECONDS = 5.0

_CURSOR_FILENAME = "custody_tx_clear_cursor.json"

# A clear is consumed iff its verdict is one of these; "deferred" is retried.
_TERMINAL_VERDICTS = frozenset({"applied", "refused_status", "refused_kind", "refused_proof"})

# Emit a CRITICAL + stalled metric every Nth consecutive deferral of the same
# pending clear (~5 min at the 5s poll) so a clear that can never apply (e.g.
# Abandon on a never-broadcast nonce that can never advance) surfaces above INFO.
# Observability only — does not change consume behavior.
_DEFERRED_STALL_RETRY_THRESHOLD = 60


class ClearAction(IntEnum):
    """Mirrors the on-chain ``Accounting.ClearAction`` enum order."""

    REQUEUE = 0
    ABANDON = 1
    MARK_SUCCESS_WITH_HASH = 2
    BURN_NONCE = 3


def _expected_applied_hash(action: int, vouched: bytes) -> bytes:
    """Recompute the contract's clearAppliedHash preimage for cross-checking."""
    return keccak(abi_encode(["uint8", "bytes32"], [int(action), bytes(vouched)]))


def _emit_metric(name: str, **labels: Any) -> None:
    suffix = " ".join(f"{k}={v}" for k, v in labels.items())
    logger.info("custody_tx_metric name=%s %s", name, suffix)


def _pending_key(chain_id: int, nonce: int) -> str:
    return f"{chain_id}_{nonce}"


class CustodyTxClearWatcher:
    """Polls Sapphire for ``CustodyTxCleared`` and applies each clear once.

    Duck-types ``accounting_service`` (needs ``reader_w3``,
    ``_get_reader_contract``, ``get_clear_applied_hash``) and ``executor``
    (needs ``_apply_clear_action``). Never imports either concrete class.
    """

    def __init__(
        self,
        accounting_service: Any,
        executor: Any,
        *,
        state_dir: Path,
        sapphire_chain_id: int,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        overlap_blocks: int = SAPPHIRE_CLEAR_OVERLAP_BLOCKS,
    ) -> None:
        self._accounting = accounting_service
        self._executor = executor
        self._state_dir = Path(state_dir)
        self._sapphire_chain_id = int(sapphire_chain_id)
        self._poll_interval = poll_interval_seconds
        self._overlap_blocks = int(overlap_blocks)
        self._cursor_path = self._state_dir / _CURSOR_FILENAME

        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Discovery high-water mark.
        self._cursor: Optional[int] = None
        # (txHash, logIndex) -> blockNumber for terminally-applied clears; the
        # block lets the prune drop entries by age rather than by count.
        self._seen: dict[tuple[str, int], int] = {}
        # f"{chain_id}_{nonce}" -> deferred clear, retried every pass.
        self._pending: dict[str, dict] = {}
        self._load_cursor()

    async def start(self) -> None:
        """Probe the clear surface, then spawn the poll task.

        A pre-upgrade contract (no ``clearAppliedHash`` getter) or an unconfigured
        Sapphire reader disables the watcher rather than crashing the executor —
        the clear channel is a last resort, not a hard dependency.
        """
        if self._running:
            return
        try:
            await self._accounting.get_clear_applied_hash(self._sapphire_chain_id, 0)
        except (BadFunctionCallOutput, ContractLogicError, DecodingError, ValueError) as exc:
            logger.warning(
                "Custody-tx clear watcher disabled — clearAppliedHash unavailable "
                "(pre-upgrade contract or Sapphire not configured): %s",
                exc,
            )
            _emit_metric("custody_tx.clear.watcher_disabled", reason=type(exc).__name__)
            return

        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="custody-tx-clear-watcher")
        logger.info("Custody-tx clear watcher started (chain=%d)", self._sapphire_chain_id)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Custody-tx clear watcher exited with error")
            self._task = None
        logger.info("Custody-tx clear watcher stopped")

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Custody-tx clear watcher: scan pass failed")
            await asyncio.sleep(self._poll_interval)

    async def _scan_once(self) -> None:
        # Retry parked clears first, independent of the log-scan cursor: a clear
        # that deferred earlier must keep being re-driven even if its log event
        # has already aged past the discovery window.
        await self._retry_pending()

        reader_w3 = getattr(self._accounting, "reader_w3", None)
        if reader_w3 is None:
            return
        head = int(await reader_w3.eth.block_number)
        safe_head = head - get_finality_depth(self._sapphire_chain_id)
        if safe_head < 0:
            return

        if self._cursor is None:
            # First boot, no persisted state: seed at the confirmed head minus the
            # overlap. A clear emitted before the watcher ever ran is not discovered.
            self._cursor = max(0, safe_head - self._overlap_blocks)
            self._save_cursor()

        if safe_head < self._cursor - self._overlap_blocks:
            return

        from_block = max(0, self._cursor - self._overlap_blocks)
        reader_contract = self._accounting._get_reader_contract()
        events = await paginated_get_logs(
            reader_contract.events.CustodyTxCleared,
            from_block=from_block,
            to_block=safe_head,
        )

        # Track the earliest block we could not fully process so the cursor never
        # advances past an event whose discovery/cross-check failed.
        min_unprocessed_block: Optional[int] = None
        for event in events:
            try:
                min_unprocessed_block = await self._process_event(event, min_unprocessed_block)
            except asyncio.CancelledError:
                raise
            except Exception:
                block = int(event.get("blockNumber") or safe_head)
                logger.exception(
                    "Custody-tx clear: event processing raised at block %d — holding cursor back",
                    block,
                )
                min_unprocessed_block = (
                    block if min_unprocessed_block is None else min(min_unprocessed_block, block)
                )

        # Never advance past an unprocessed event; otherwise jump to the head.
        if min_unprocessed_block is not None:
            self._cursor = min_unprocessed_block - 1
        else:
            self._cursor = safe_head
        self._prune_seen()
        self._save_cursor()

    async def _process_event(
        self, event: Any, min_unprocessed_block: Optional[int]
    ) -> Optional[int]:
        """Discover one clear event and either apply it or park it as pending.

        Returns the updated ``min_unprocessed_block`` (lowered when this event
        could not be processed so the caller holds the cursor back).
        """
        tx_hash = HexBytes(event["transactionHash"]).to_0x_hex()
        log_index = int(event["logIndex"])
        block = int(event.get("blockNumber") or 0)
        seen_key = (tx_hash, log_index)

        def hold_back() -> Optional[int]:
            return block if min_unprocessed_block is None else min(min_unprocessed_block, block)

        if seen_key in self._seen:
            return min_unprocessed_block

        args = event["args"]
        chain_id = int(args["chainId"])
        nonce = int(args["nonce"])
        action = ClearAction(int(args["action"]))
        vouched_bytes = bytes(HexBytes(args["vouchedTxHash"]))

        if _pending_key(chain_id, nonce) in self._pending:
            # Already tracked — driven by _retry_pending, not here.
            return min_unprocessed_block

        try:
            on_chain = bytes(await self._accounting.get_clear_applied_hash(chain_id, nonce))
        except Exception:
            logger.exception(
                "Custody-tx clear: clearAppliedHash read failed (chain=%d nonce=%d) — "
                "holding cursor back",
                chain_id,
                nonce,
            )
            # Do NOT mark seen: hold the cursor so the next pass re-reads.
            return hold_back()

        if on_chain == b"\x00" * 32 or not on_chain:
            # Emitted but the slot reads zero — shouldn't happen post-emit under
            # first-clear-wins; hold the cursor back so a later pass re-checks.
            logger.debug(
                "Custody-tx clear: chain=%d nonce=%d emitted but clearAppliedHash is zero",
                chain_id,
                nonce,
            )
            return hold_back()

        expected = _expected_applied_hash(int(action), vouched_bytes)
        if on_chain != expected:
            logger.critical(
                "Custody-tx clear: on-chain clearAppliedHash disagrees with emitted event "
                "(chain=%d nonce=%d action=%s) — refusing to apply (fail closed)",
                chain_id,
                nonce,
                action.name,
            )
            _emit_metric("custody_tx.clear.contract_disagreement_total", chain_id=chain_id)
            # Permanent disagreement: fail closed and consume so it isn't rescanned.
            self._seen[seen_key] = block
            return min_unprocessed_block

        vouched_hex = HexBytes(vouched_bytes).to_0x_hex()
        verdict = await self._executor._apply_clear_action(chain_id, nonce, action, vouched_hex)
        if verdict in _TERMINAL_VERDICTS:
            self._seen[seen_key] = block
        else:
            # Deferred: park and retry from the pending set until it terminally
            # applies, independent of the log-scan cursor.
            self._pending[_pending_key(chain_id, nonce)] = {
                "chain_id": chain_id,
                "nonce": nonce,
                "action": int(action),
                "vouched": vouched_hex,
                "block": block,
                "tx_hash": tx_hash,
                "log_index": log_index,
                "retries": 0,
            }
        return min_unprocessed_block

    async def _retry_pending(self) -> None:
        """Re-drive every parked clear; a terminal verdict graduates it out of
        the pending set (and into the seen-set if it carries a log location)."""
        if not self._pending:
            return
        changed = False
        for entry in list(self._pending.values()):
            chain_id = int(entry["chain_id"])
            nonce = int(entry["nonce"])
            try:
                verdict = await self._executor._apply_clear_action(
                    chain_id, nonce, ClearAction(int(entry["action"])), entry["vouched"]
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Custody-tx clear: pending retry raised (chain=%d nonce=%d) — keeping pending",
                    chain_id,
                    nonce,
                )
                continue
            if verdict in _TERMINAL_VERDICTS:
                self._pending.pop(_pending_key(chain_id, nonce), None)
                tx_hash = entry.get("tx_hash")
                log_index = entry.get("log_index")
                if tx_hash is not None and log_index is not None:
                    self._seen[(str(tx_hash), int(log_index))] = int(entry.get("block") or 0)
                changed = True
                continue
            # Still deferred: count the consecutive deferral and escalate a clear
            # that may never apply (e.g. Abandon on a never-broadcast, never-
            # advancing nonce — first-clear-wins blocks re-clearing as BurnNonce).
            entry["retries"] = int(entry.get("retries") or 0) + 1
            changed = True
            if entry["retries"] % _DEFERRED_STALL_RETRY_THRESHOLD == 0:
                action = ClearAction(int(entry["action"]))
                logger.critical(
                    "Custody-tx clear: %s deferred %d times (chain=%d nonce=%d) — "
                    "may never apply; operator action required",
                    action.name,
                    entry["retries"],
                    chain_id,
                    nonce,
                )
                _emit_metric(
                    "custody_tx.clear.deferred_stalled",
                    chain_id=chain_id,
                    nonce=nonce,
                    action=action.name,
                    retries=entry["retries"],
                )
        if changed:
            self._save_cursor()

    def _prune_seen(self) -> None:
        # Drop seen-set entries below the actual rescan floor (next pass scans
        # from cursor - overlap). Anchoring on safe_head instead would prune
        # entries the held-back cursor can still rediscover -> re-apply.
        cutoff = (self._cursor or 0) - self._overlap_blocks
        stale = [key for key, block in self._seen.items() if block < cutoff]
        for key in stale:
            self._seen.pop(key, None)

    def _load_cursor(self) -> None:
        if not self._cursor_path.exists():
            return
        try:
            data = json.loads(self._cursor_path.read_text())
        except Exception:
            logger.warning("Custody-tx clear cursor %s unreadable — reseeding", self._cursor_path)
            return
        cursor = data.get("cursor")
        self._cursor = int(cursor) if cursor is not None else None
        # Tolerate an older on-disk format that stored seen as 2-tuples
        # [txHash, logIndex] without a block; default those to 0 so the
        # age-based prune drops them on the first pass.
        seen: dict[tuple[str, int], int] = {}
        for entry in data.get("seen", []):
            if not isinstance(entry, (list, tuple)):
                continue
            if len(entry) == 3:
                seen[(str(entry[0]), int(entry[1]))] = int(entry[2])
            elif len(entry) == 2:
                seen[(str(entry[0]), int(entry[1]))] = 0
        self._seen = seen
        pending: dict[str, dict] = {}
        for entry in data.get("pending", []):
            try:
                pending[_pending_key(int(entry["chain_id"]), int(entry["nonce"]))] = {
                    "chain_id": int(entry["chain_id"]),
                    "nonce": int(entry["nonce"]),
                    "action": int(entry["action"]),
                    "vouched": str(entry["vouched"]),
                    "block": int(entry.get("block") or 0),
                    "tx_hash": entry.get("tx_hash"),
                    "log_index": entry.get("log_index"),
                    "retries": int(entry.get("retries") or 0),
                }
            except (KeyError, TypeError, ValueError):
                continue
        self._pending = pending

    def _save_cursor(self) -> None:
        payload = json.dumps(
            {
                "cursor": self._cursor,
                "seen": [[h, i, b] for (h, i), b in sorted(self._seen.items())],
                "pending": list(self._pending.values()),
            },
            indent=2,
        )
        tmp = self._cursor_path.with_suffix(".tmp")
        if tmp.exists():
            tmp.unlink()
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(str(tmp), str(self._cursor_path))
