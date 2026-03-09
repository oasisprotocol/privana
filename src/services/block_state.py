"""Persistent block state management for deposit listener recovery."""

import fcntl
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Literal, Optional

TxStatus = Literal["processing", "failed", "dead"]

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = "/data/deposit-listener"
STATE_FILENAME = "block_state.json"
LOCK_FILENAME = "block_state.lock"


@dataclass
class PendingTx:
    """A pending transaction with its block number for reprocessing."""

    tx_hash: str
    block_number: int
    retry_count: int = 0
    last_retry_at: float = 0.0  # timestamp
    status: TxStatus = "processing"
    last_error: Optional[str] = None
    processing_started_at: float = 0.0  # timestamp
    token_address: Optional[str] = None  # for ERC20 deposits
    from_address: Optional[str] = None  # original sender

    def to_dict(self) -> dict:
        return {
            "tx_hash": self.tx_hash,
            "block_number": self.block_number,
            "retry_count": self.retry_count,
            "last_retry_at": self.last_retry_at,
            "status": self.status,
            "last_error": self.last_error,
            "processing_started_at": self.processing_started_at,
            "token_address": self.token_address,
            "from_address": self.from_address,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PendingTx":
        return cls(
            tx_hash=data["tx_hash"],
            block_number=data.get("block_number", 0),
            retry_count=data.get("retry_count", 0),
            last_retry_at=data.get("last_retry_at", 0.0),
            status=data.get("status", "processing"),
            last_error=data.get("last_error"),
            processing_started_at=data.get("processing_started_at", 0.0),
            token_address=data.get("token_address"),
            from_address=data.get("from_address"),
        )


@dataclass
class ChainState:
    """State for a single chain's block processing."""

    last_processed_block: int = 0
    pending_txs: Dict[str, PendingTx] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "last_processed_block": self.last_processed_block,
            "pending_txs": {k: v.to_dict() for k, v in self.pending_txs.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChainState":
        pending_txs_data = data.get("pending_txs", {})
        if isinstance(pending_txs_data, list):
            # Legacy state format only stored tx hashes. Mark as failed so these
            # entries are recoverable by the retry worker and won't stay
            # indefinitely in "processing" with no start timestamp.
            pending_txs = {
                tx: PendingTx(
                    tx_hash=tx, block_number=0, status="failed", last_error="legacy state"
                )
                for tx in pending_txs_data
            }
        else:
            pending_txs = {k: PendingTx.from_dict(v) for k, v in pending_txs_data.items()}
        return cls(
            last_processed_block=data.get("last_processed_block", 0),
            pending_txs=pending_txs,
        )


@dataclass
class BlockState:
    """Complete block processing state across all chains."""

    chains: Dict[int, ChainState] = field(default_factory=dict)
    version: int = 1

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "chains": {str(k): v.to_dict() for k, v in self.chains.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BlockState":
        version = data.get("version", 1)
        chains_data = data.get("chains", {})
        chains = {int(k): ChainState.from_dict(v) for k, v in chains_data.items()}
        return cls(chains=chains, version=version)


class BlockStateManager:
    """Manages persistent block state for deposit listener crash recovery.

    Uses atomic file writes (write to temp, then rename) to ensure state
    consistency even during crashes. Stored in ROFL persistent storage
    which survives upgrades and node restarts.

    Thread Safety:
        This class uses a threading lock for file writes and a file lock for
        cross-process synchronization. Individual state mutations are NOT
        fully atomic - the mutation and save are separate operations. This
        is acceptable because:

        1. Initial deposit tasks may run concurrently but typically operate
           on distinct transactions
        2. The retry worker processes failed transactions sequentially
        3. State methods handle re-entry gracefully (add_pending_tx updates
           existing entries, claim_retry validates status before claiming)

        If concurrent mutation of the same transaction becomes needed,
        wrap operations in _thread_lock.
    """

    def __init__(self, state_dir: Optional[str] = None):
        self._state_dir = Path(state_dir or os.getenv("BLOCK_STATE_DIR", DEFAULT_STATE_DIR))
        self._state_file = self._state_dir / STATE_FILENAME
        self._lock_file = self._state_dir / LOCK_FILENAME
        self._state: BlockState = BlockState()
        self._initialized = False
        self._thread_lock = threading.Lock()
        self._file_lock_fd: Optional[int] = None

    def initialize(self) -> None:
        """Initialize state manager, loading existing state if available."""
        if self._initialized:
            return

        self._state_dir.mkdir(parents=True, exist_ok=True)

        if self._state_file.exists():
            try:
                self._load_state()
                logger.info(
                    f"Loaded block state from {self._state_file}: {len(self._state.chains)} chains"
                )
                for chain_id, chain_state in self._state.chains.items():
                    logger.info(
                        f"  Chain {chain_id}: last_block={chain_state.last_processed_block}, "
                        f"pending_txs={len(chain_state.pending_txs)}"
                    )
            except Exception as e:
                logger.warning(f"Failed to load block state, starting fresh: {e}")
                self._state = BlockState()
        else:
            logger.info(f"No existing block state found at {self._state_file}, starting fresh")

        self._initialized = True

    def _load_state(self) -> None:
        """Load state from persistent storage."""
        with open(self._state_file, "r") as f:
            data = json.load(f)
        self._state = BlockState.from_dict(data)

    def _save_state(self) -> None:
        """Atomically save state to persistent storage with file locking."""
        self._state_dir.mkdir(parents=True, exist_ok=True)

        with self._thread_lock:
            lock_fd = os.open(self._lock_file, os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)

                fd, temp_path = tempfile.mkstemp(dir=self._state_dir, suffix=".tmp")
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(self._state.to_dict(), f, indent=2)
                    os.rename(temp_path, self._state_file)
                except Exception:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)
                    raise
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    def get_last_processed_block(self, chain_id: int) -> Optional[int]:
        """Get the last processed block for a chain, or None if not tracked."""
        chain_state = self._state.chains.get(chain_id)
        if chain_state is None or chain_state.last_processed_block == 0:
            return None
        return chain_state.last_processed_block

    def set_last_processed_block(self, chain_id: int, block_number: int) -> None:
        """Update the last processed block for a chain and persist."""
        if chain_id not in self._state.chains:
            self._state.chains[chain_id] = ChainState()
        self._state.chains[chain_id].last_processed_block = block_number
        self._save_state()

    def add_pending_tx(
        self,
        chain_id: int,
        tx_hash: str,
        block_number: int = 0,
        token_address: Optional[str] = None,
        from_address: Optional[str] = None,
    ) -> None:
        """Mark a transaction as pending processing."""
        if chain_id not in self._state.chains:
            self._state.chains[chain_id] = ChainState()
        self._state.chains[chain_id].pending_txs[tx_hash] = PendingTx(
            tx_hash=tx_hash,
            block_number=block_number,
            processing_started_at=time.time(),
            token_address=token_address,
            from_address=from_address,
        )
        self._save_state()

    def mark_failed(self, chain_id: int, tx_hash: str, error: str) -> None:
        """Mark a pending transaction as failed so it is eligible for retries."""
        chain_state = self._state.chains.get(chain_id)
        if chain_state is None or tx_hash not in chain_state.pending_txs:
            return
        pending_tx = chain_state.pending_txs[tx_hash]
        pending_tx.status = "failed"
        pending_tx.last_error = error[:500]
        pending_tx.processing_started_at = 0.0
        self._save_state()

    def mark_dead(self, chain_id: int, tx_hash: str, error: str) -> None:
        """Mark a pending transaction as terminally failed (no more retries)."""
        chain_state = self._state.chains.get(chain_id)
        if chain_state is None or tx_hash not in chain_state.pending_txs:
            return
        pending_tx = chain_state.pending_txs[tx_hash]
        pending_tx.status = "dead"
        pending_tx.last_error = error[:500]
        pending_tx.processing_started_at = 0.0
        self._save_state()

    def claim_retry(self, chain_id: int, tx_hash: str) -> Optional[int]:
        """Claim a failed tx for retry, returning updated retry count."""
        chain_state = self._state.chains.get(chain_id)
        if chain_state is None or tx_hash not in chain_state.pending_txs:
            return None
        pending_tx = chain_state.pending_txs[tx_hash]
        if pending_tx.status != "failed":
            return None
        now = time.time()
        pending_tx.retry_count += 1
        pending_tx.last_retry_at = now
        pending_tx.status = "processing"
        pending_tx.last_error = None
        pending_tx.processing_started_at = now
        self._save_state()
        return pending_tx.retry_count

    def complete_pending_tx(self, chain_id: int, tx_hash: str) -> None:
        """Mark a transaction as completed."""
        chain_state = self._state.chains.get(chain_id)
        if chain_state and tx_hash in chain_state.pending_txs:
            del chain_state.pending_txs[tx_hash]
            self._save_state()

    def get_pending_txs(self, chain_id: int) -> Dict[str, PendingTx]:
        """Get all pending transactions for a chain that need reprocessing."""
        chain_state = self._state.chains.get(chain_id)
        if chain_state is None:
            return {}
        return chain_state.pending_txs.copy()

    def get_failed_txs(self, chain_id: int) -> Dict[str, PendingTx]:
        """Get only failed transactions that should be considered for retries."""
        chain_state = self._state.chains.get(chain_id)
        if chain_state is None:
            return {}
        return {k: v for k, v in chain_state.pending_txs.items() if v.status == "failed"}

    def get_earliest_pending_block(self, chain_id: int) -> Optional[int]:
        """Get the earliest block number among pending transactions."""
        chain_state = self._state.chains.get(chain_id)
        if chain_state is None or not chain_state.pending_txs:
            return None
        blocks = [
            tx.block_number
            for tx in chain_state.pending_txs.values()
            if tx.block_number > 0 and tx.status != "dead"
        ]
        return min(blocks) if blocks else None

    def get_backfill_start_block(
        self,
        chain_id: int,
        current_block: int,
        default_lookback: int = 100,
    ) -> int:
        """Calculate the starting block for scanning on startup.

        Returns the block to start scanning from.

        If we have persisted state, we start from the last processed block.
        If there are pending transactions, we start from the earliest pending
        block to ensure those transactions get reprocessed.
        """
        chain_state = self._state.chains.get(chain_id)

        if chain_state is None or chain_state.last_processed_block == 0:
            return max(0, current_block - default_lookback)

        start_block = chain_state.last_processed_block

        earliest_pending = self.get_earliest_pending_block(chain_id)
        if earliest_pending is not None and earliest_pending > 0:
            start_block = min(start_block, earliest_pending - 1)
            logger.info(
                f"Chain {chain_id}: Found {len(chain_state.pending_txs)} pending txs, "
                f"earliest at block {earliest_pending}, starting from block {start_block}"
            )

        return start_block


_manager_instance: Optional[BlockStateManager] = None


def get_block_state_manager(state_dir: Optional[str] = None) -> BlockStateManager:
    """Get the singleton BlockStateManager instance."""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = BlockStateManager(state_dir)
        _manager_instance.initialize()
    return _manager_instance
