"""Sweep engine: state machine for sweeping deposit addresses to the encumbered wallet.

State machine per (deposit_address, chain_id):
    (no record) → PENDING → GAS_FUNDED → SWEPT → (credit) → record deleted

    SWEPT is only set after the sweep tx is confirmed on-chain (receipt status=1).
    On failure: record stays in current state for recovery or manual investigation.
    On ROFL restart / periodic recovery:
      - SWEPT → retry credit (idempotent via DepositAlreadyProcessed).
      - GAS_FUNDED with mined sweep_tx_hash → promoted to SWEPT, then credit.
      - PENDING / GAS_FUNDED with NO sweep_tx_hash → re-run sweep from scratch
        (no broadcast tx means no nonce-collision risk).
      - GAS_FUNDED with a sweep_tx_hash that reconciliation couldn't resolve
        (unmined, dropped from mempool, or reverted) → logged for manual
        investigation; the nonce may still be encumbered.

State persisted to JSON files so sweeps survive ROFL restarts.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, Set, runtime_checkable

from web3 import AsyncWeb3
from web3.exceptions import TransactionNotFound
from web3.providers import AsyncHTTPProvider

from src.abi.rofl_bridge import ROFL_BRIDGE_ABI
from src.clients.rofl import TransactionRevertedError
from src.config.chain_config import GAS_FUNDING_AMOUNT_WEI, SWEEP_GAS_LIMIT_NATIVE
from src.services.accounting_contract import BridgeBurnReservation
from src.services.custody_tx_executor import (
    CustodyTxKind,
    CustodyTxRecord,
    CustodyTxRequest,
    CustodyTxStatus,
)
from src.services.l2_fee_estimator import estimate_l1_data_fee

logger = logging.getLogger(__name__)


class SweepCreditPendingError(Exception):
    """Sweep succeeded but on-chain credit failed — funds are safe, credit will be retried."""

    def __init__(self, deposit_id_hex: str, original_error: Exception):
        self.deposit_id_hex = deposit_id_hex
        super().__init__(
            f"Deposit swept successfully but credit pending — "
            f"funds are safe and will be credited automatically "
            f"(deposit_id={deposit_id_hex})"
        )


class SweepRecoveryStuckError(Exception):
    """Recovery cannot progress: deposit-address nonce encumbered by an unmined
    or reverted sweep tx; operator must investigate before the record can advance.
    """

    def __init__(
        self,
        deposit_address: str,
        chain_id: int,
        state: "SweepState",
        sweep_tx_hash: str,
        flow_type: str,
    ):
        self.deposit_address = deposit_address
        self.chain_id = chain_id
        self.state = state
        self.sweep_tx_hash = sweep_tx_hash
        self.flow_type = flow_type
        super().__init__(
            f"Stuck sweep for {deposit_address} on chain {chain_id} "
            f"(state={state.value}, flow={flow_type}, sweep_tx={sweep_tx_hash}) — "
            f"deposit-address nonce encumbered, manual investigation required"
        )

    @classmethod
    def from_record(cls, record: "SweepRecord") -> "SweepRecoveryStuckError":
        return cls(
            record.deposit_address,
            record.chain_id,
            record.state,
            record.sweep_tx_hash,
            record.flow_type,
        )


class ReconstructionEvidenceError(Exception):
    """On-chain evidence for an xROSE deposit is contradictory; fail closed.

    Raised when reconstruction's independent on-chain signals disagree, or
    when local state corruption may hide a deposit. A caller using
    reconstruction as a gating signal must treat this as a hard "do not
    proceed" — the divergence breaks the bridge invariant regardless of
    which side is "right".
    """

    def __init__(self, deposit_id: bytes, reason: str):
        self.deposit_id = deposit_id
        self.reason = reason
        super().__init__(f"xROSE reconstruction failed for depositId={deposit_id.hex()}: {reason}")


# Cadence of the periodic recovery pass over every loaded sweep record;
# longer than withdrawal polling (12s) because the per-record work is
# idempotent and the worst stall is a one-minute credit delay.
SWEEP_RECOVERY_INTERVAL = 60
# Wall-clock budget for waiting on a single burn to clear the executor before
# we hand the record back to the recovery loop. Burns are async on the wire
# (Base block time ~2s + executor poll); ~3 minutes covers the long tail
# without making /deposits/check requests block on an unhealthy chain.
BURN_RESOLUTION_TIMEOUT = 180.0
# Bounded poll for the BridgeBurnReserved event after ROFL submission.
# Sapphire block time is ~6s; 10 attempts gives ~60s headroom.
BRIDGE_BURN_RESERVATION_POLL_ATTEMPTS = 10
BRIDGE_BURN_RESERVATION_POLL_INTERVAL = 6.0
FLOW_STANDARD = "standard"
FLOW_NATIVE_ROSE_BRIDGE_IN = "native_rose_bridge_in"
FLOW_XROSE_BRIDGE_IN = "xrose_bridge_in"
BASE_SEPOLIA_CHAIN_ID = 84532


@runtime_checkable
class DepositAccountingProtocol(Protocol):
    """Interface expected by deposit processing from AccountingContractService.

    Formalizes the methods used by SweepEngine and DepositProcessor.
    """

    async def generate_sweep_native(
        self,
        beneficiary: str,
        chain_type: str,
        version: int,
        chain_id: int,
        amount: int,
        nonce: int,
        gas_price: int,
    ) -> bytes: ...

    async def generate_sweep_erc20(
        self,
        beneficiary: str,
        chain_type: str,
        version: int,
        chain_id: int,
        token_address: str,
        amount: int,
        nonce: int,
        gas_price: int,
    ) -> bytes: ...

    async def generate_sweep_erc20_to_bridge(
        self,
        beneficiary: str,
        chain_type: str,
        version: int,
        chain_id: int,
        token_address: str,
        amount: int,
        source_chain_nonce: int,
        gas_price: int,
    ) -> bytes: ...

    async def generate_gas_funding_tx(
        self,
        to_deposit_address: str,
        chain_id: int,
        gas_amount: int,
        gas_tank_nonce: int,
        gas_price: int,
    ) -> bytes: ...

    async def get_gas_tank_address(self) -> str: ...

    async def credit_deposit(
        self,
        beneficiary: str,
        token_id: bytes,
        amount: int,
        deposit_id: bytes,
    ) -> Any: ...

    async def get_token_id(self, chain_id: int, token_address: str | None) -> bytes: ...

    async def get_rose_token_id(self) -> bytes: ...

    async def get_custody_address(self) -> str: ...

    async def get_deposit_address(
        self,
        chain_type: str,
        version: int,
        siwe_token: bytes,
    ) -> str: ...

    async def is_token_registered(self, token_id: bytes) -> bool: ...

    async def is_deposit_processed(self, deposit_id: bytes) -> bool: ...

    async def reserve_bridge_burn(
        self,
        deposit_id: bytes,
        chain_id: int,
        bridge: str,
        amount: int,
    ) -> Any: ...

    async def get_bridge_burn_nonce(self, deposit_id: bytes) -> int: ...

    async def generate_bridge_burn_transfer(self, deposit_id: bytes) -> bytes: ...

    async def list_bridge_burn_reservations(
        self, chain_id: Optional[int] = None
    ) -> list[BridgeBurnReservation]: ...


@runtime_checkable
class CustodyTxExecutorProtocol(Protocol):
    """Subset of ``CustodyTxExecutor`` the sweep engine relies on.

    A protocol rather than the concrete class so tests can substitute mocks.
    """

    async def enqueue(self, request: CustodyTxRequest) -> str: ...

    async def wait_for_resolution(
        self, key: str, timeout: float | None = None
    ) -> CustodyTxRecord: ...

    def get_record(self, chain_id: int, evm_nonce: int) -> Optional[CustodyTxRecord]: ...


def _to_hex(value) -> str:
    """Convert bytes or str to 0x-prefixed hex string."""
    if isinstance(value, bytes):
        return "0x" + value.hex()
    return value


def _from_hex(hex_str: str) -> bytes:
    """Inverse of ``_to_hex`` for hex-encoded ``SweepRecord`` fields."""
    return bytes.fromhex(hex_str.removeprefix("0x"))


DEFAULT_STATE_DIR = os.getenv("SWEEP_STATE_DIR", "/data/sweep-engine")


class SweepState(str, Enum):
    PENDING = "pending"
    GAS_FUNDED = "gas_funded"
    SWEPT = "swept"
    BURN_PENDING = "burn_pending"
    BURNED = "burned"
    MANUAL_REVIEW = "manual_review"


@dataclass
class SweepRecord:
    """Persistent state for a sweep in progress."""

    deposit_address: str
    chain_id: int
    state: SweepState
    beneficiary: str
    chain_type: str
    version: int
    amount: int = 0
    token_id_hex: str = ""  # hex-encoded bytes32
    token_address: Optional[str] = None  # None for native
    deposit_id_hex: str = ""  # hex-encoded bytes32
    source_tx_hash: str = ""  # deposit tx on source chain
    deposit_index: int = 0
    flow_type: str = FLOW_STANDARD
    destination: Optional[str] = None
    bridge_address: Optional[str] = None
    burn_tx_hash: Optional[str] = None
    burn_reserved: bool = False
    sweep_tx_hash: Optional[str] = None
    gas_funding_tx_hash: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    retry_count: int = 0
    error: Optional[str] = None

    def __post_init__(self):
        if self.chain_id <= 0:
            raise ValueError("chain_id must be positive")
        if not self.deposit_address:
            raise ValueError("deposit_address required")
        if not self.beneficiary:
            raise ValueError("beneficiary required")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @staticmethod
    def _migrate_record_data(data: dict) -> dict:
        migrated = dict(data)
        migrated.setdefault("flow_type", FLOW_STANDARD)
        migrated.setdefault("destination", None)
        migrated.setdefault("bridge_address", None)
        migrated.setdefault("burn_tx_hash", None)
        migrated.setdefault("burn_reserved", False)
        return migrated

    @classmethod
    def from_dict(cls, data: dict) -> "SweepRecord":
        data = cls._migrate_record_data(data)
        data["state"] = SweepState(data["state"])
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class ReconstructionKind(str, Enum):
    """Terminal verdict from :meth:`SweepEngine.reconstruct_xrose_deposit_state`."""

    UNKNOWN = "unknown"
    SWEPT_ONLY = "swept_only"
    BURN_RESERVED_NOT_MINED = "burn_reserved_not_mined"
    BURNED = "burned"
    CREDITED = "credited"


@dataclass(frozen=True)
class Reconstruction:
    """Evidence-grounded view of one xROSE deposit's state, derived from
    on-chain reads plus local sweep records when present.

    ``kind`` is the only safe terminal signal; the optional payload fields
    carry the corroborating evidence a caller needs before acting.
    """

    deposit_id: bytes
    kind: ReconstructionKind
    reservation: Optional[BridgeBurnReservation] = None
    burn_amount: Optional[int] = None
    credited: bool = False
    burn_view: bool = False

    def __post_init__(self) -> None:
        if self.kind is ReconstructionKind.CREDITED:
            if not self.credited:
                raise ValueError("CREDITED kind requires credited=True")
            if self.reservation is None:
                raise ValueError("CREDITED kind requires a reservation")
        elif self.kind is ReconstructionKind.BURNED:
            if not self.burn_view:
                raise ValueError("BURNED kind requires burn_view=True")
            if self.burn_amount is None:
                raise ValueError("BURNED kind requires burn_amount")
        elif self.kind is ReconstructionKind.BURN_RESERVED_NOT_MINED:
            if self.reservation is None:
                raise ValueError("BURN_RESERVED_NOT_MINED kind requires a reservation")
            if self.burn_view or self.burn_amount is not None:
                raise ValueError("BURN_RESERVED_NOT_MINED kind cannot carry burn evidence")
            if self.credited:
                raise ValueError("BURN_RESERVED_NOT_MINED kind cannot be credited")
        elif self.kind in (ReconstructionKind.SWEPT_ONLY, ReconstructionKind.UNKNOWN):
            if self.credited or self.burn_view or self.burn_amount is not None:
                raise ValueError(f"{self.kind.value} kind cannot carry chain evidence")


class SweepEngine:
    """Manages sweep lifecycle: sign on-chain, broadcast, confirm, credit.

    Concurrency model: one asyncio.Lock per (deposit_address, chain_id).
    Multiple /deposits/check calls for the same address queue behind this lock.
    After the sweep confirms, subsequent callers see balance=0 and skip to credit.
    """

    def __init__(
        self,
        accounting_service: DepositAccountingProtocol,
        chain_rpc_urls: Dict[int, str],
        state_dir: str = DEFAULT_STATE_DIR,
        executor: Optional[CustodyTxExecutorProtocol] = None,
    ):
        self._accounting = accounting_service
        self._chain_rpc_urls = chain_rpc_urls
        self._executor = executor
        self._state_dir = Path(state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._web3_cache: Dict[int, AsyncWeb3] = {}
        # Per-address lock: prevents concurrent sweeps for the same deposit address
        self._address_locks: Dict[str, asyncio.Lock] = {}
        # Global lock: serializes gas tank nonce reads across all concurrent sweeps
        self._gas_tank_lock = asyncio.Lock()
        # Track gas funding tx hashes to exclude from deposit verification
        self._gas_funding_tx_hashes: Set[str] = set()
        # Reverse index: deposit_id_hex → (deposit_address, chain_id)
        # Rebuilt from disk on startup via load_incomplete_sweeps()
        self._deposit_id_index: Dict[str, tuple[str, int]] = {}
        # Recovery loop state
        self._recovery_running = False
        self._recovery_task: Optional[asyncio.Task] = None
        # xROSE preflight closures die with the process, so on the first recovery
        # pass after restart we re-enqueue to refresh them; subsequent passes
        # skip to avoid spending a Sapphire confidential read per tick.
        self._xrose_preflights_refreshed: Set[str] = set()

    def _get_address_lock(self, deposit_address: str, chain_id: int) -> asyncio.Lock:
        """Get or create the per-address lock for concurrent sweep protection."""
        key = f"{deposit_address.lower()}_{chain_id}"
        if key not in self._address_locks:
            self._address_locks[key] = asyncio.Lock()
        return self._address_locks[key]

    @property
    def gas_funding_tx_hashes(self) -> Set[str]:
        """Set of gas funding tx hashes — exclude these from deposit claims."""
        return self._gas_funding_tx_hashes

    def _get_web3(self, chain_id: int) -> AsyncWeb3:
        if chain_id not in self._web3_cache:
            rpc_url = self._chain_rpc_urls.get(chain_id)
            if not rpc_url:
                raise ValueError(f"No RPC URL configured for chain {chain_id}")
            self._web3_cache[chain_id] = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        return self._web3_cache[chain_id]

    async def _get_safe_gas_price(self, w3: AsyncWeb3, chain_id: int) -> int:
        """Return a gas price that is safe from underpricing on L2s.

        Takes the max of baseFeePerGas (from latest block), eth_gasPrice (RPC),
        and a 1 gwei floor. eth_gasPrice alone can return stale/low values on L2s.
        """
        latest_block = await w3.eth.get_block("latest")
        base_fee = latest_block.get("baseFeePerGas", 0)
        rpc_gas_price = await w3.eth.gas_price
        gas_price = max(base_fee, rpc_gas_price, 1_000_000_000)
        logger.debug(
            "Gas price: chain=%d base_fee=%d rpc=%d chosen=%d",
            chain_id,
            base_fee,
            rpc_gas_price,
            gas_price,
        )
        return gas_price

    def _record_path(self, deposit_address: str, chain_id: int) -> Path:
        key = f"{deposit_address.lower()}_{chain_id}"
        return self._state_dir / f"sweep_{key}.json"

    def _save_record(self, record: SweepRecord) -> None:
        path = self._record_path(record.deposit_address, record.chain_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record.to_dict(), indent=2))
        os.replace(str(tmp), str(path))
        if record.deposit_id_hex:
            self._deposit_id_index[record.deposit_id_hex.lower()] = (
                record.deposit_address,
                record.chain_id,
            )

    def _delete_record(self, deposit_address: str, chain_id: int) -> None:
        # Use the index to find deposit_id_hex without a disk read
        for key, (addr, cid) in self._deposit_id_index.items():
            if addr.lower() == deposit_address.lower() and cid == chain_id:
                self._deposit_id_index.pop(key, None)
                break
        path = self._record_path(deposit_address, chain_id)
        path.unlink(missing_ok=True)

    def get_sweep_record(self, deposit_address: str, chain_id: int) -> Optional[SweepRecord]:
        path = self._record_path(deposit_address, chain_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return SweepRecord.from_dict(data)

    def get_record_by_deposit_id(self, deposit_id_hex: str) -> Optional[SweepRecord]:
        """Look up a SweepRecord by deposit_id_hex (used by polling endpoint)."""
        location = self._deposit_id_index.get(deposit_id_hex.lower())
        if location is None:
            return None
        return self.get_sweep_record(*location)

    def cleanup_record(self, deposit_id_hex: str) -> None:
        """Delete a sweep record by deposit_id (public API for DepositProcessor).

        Refuses if a sweep tx has already broadcast: deleting the record then
        would orphan an in-flight burn (xROSE flow) or an unmined sweep nonce,
        and a subsequent confirmation would have nowhere to drive credit from.
        Recovery owns post-broadcast records.
        """
        location = self._deposit_id_index.get(deposit_id_hex.lower())
        if location is None:
            return
        record = self.get_sweep_record(*location)
        if record is not None and record.sweep_tx_hash:
            logger.warning(
                "cleanup_record refused for %s on chain %d: sweep_tx=%s already broadcast; "
                "recovery owns this record",
                location[0],
                location[1],
                record.sweep_tx_hash,
            )
            return
        self._delete_record(*location)

    def persist_error(self, deposit_id_hex: str, error: str) -> None:
        """Mark a sweep record as failed (public API for DepositProcessor)."""
        record = self.get_record_by_deposit_id(deposit_id_hex)
        if record is not None:
            record.error = error
            self._save_record(record)

    def _mark_manual_review(self, record: SweepRecord, error: Optional[str] = None) -> None:
        record.state = SweepState.MANUAL_REVIEW
        if error is not None:
            record.error = error
        self._save_record(record)

    async def sweep_native_rose_bridge(
        self,
        deposit_address: str,
        beneficiary: str,
        chain_type: str,
        version: int,
        chain_id: int,
        token_id: bytes,
        amount: int,
        deposit_id: bytes,
    ) -> None:
        """Sweep Sapphire native ROSE and credit the measured custody delta.

        Sapphire native ROSE bridge-in differs from standard native deposits:
        the deposit address pays its own sweep gas, so credited value must be
        the net amount that arrives in custody, not the gross verified tx value.
        """
        lock = self._get_address_lock(deposit_address, chain_id)
        async with lock:
            w3 = self._get_web3(chain_id)

            try:
                balance = await w3.eth.get_balance(deposit_address)

                if balance == 0:
                    if await self._accounting.is_deposit_processed(deposit_id):
                        logger.info(
                            "Native ROSE bridge deposit %s already swept and processed on chain %d",
                            deposit_address,
                            chain_id,
                        )
                        return
                    raise ValueError(
                        f"Native ROSE bridge balance is 0 at {deposit_address} on chain "
                        f"{chain_id} but deposit_id is not yet processed — nothing to sweep"
                    )

                if balance < amount:
                    raise ValueError(
                        f"Native ROSE bridge balance ({balance}) < verified amount ({amount}) "
                        f"at {deposit_address} on chain {chain_id}"
                    )

                gas_price = await self._get_safe_gas_price(w3, chain_id)
                l1_data_fee = await estimate_l1_data_fee(w3, chain_id, is_erc20=False)
                sweep_gas_cost = (SWEEP_GAS_LIMIT_NATIVE * gas_price) + l1_data_fee
                if balance <= sweep_gas_cost:
                    raise ValueError(
                        f"Native ROSE bridge balance ({balance}) cannot cover sweep gas "
                        f"({sweep_gas_cost}) at {deposit_address} on chain {chain_id}"
                    )
                sweep_amount = balance - sweep_gas_cost

                record = SweepRecord(
                    deposit_address=deposit_address,
                    chain_id=chain_id,
                    state=SweepState.PENDING,
                    beneficiary=beneficiary,
                    chain_type=chain_type,
                    version=version,
                    amount=amount,
                    token_id_hex=_to_hex(token_id),
                    token_address=None,
                    deposit_id_hex=_to_hex(deposit_id),
                    flow_type=FLOW_NATIVE_ROSE_BRIDGE_IN,
                )
                self._save_record(record)

                nonce = await w3.eth.get_transaction_count(deposit_address)
                signed_tx = await self._accounting.generate_sweep_native(
                    beneficiary=beneficiary,
                    chain_type=chain_type,
                    version=version,
                    chain_id=chain_id,
                    amount=sweep_amount,
                    nonce=nonce,
                    gas_price=gas_price,
                )

                tx_hash = await w3.eth.send_raw_transaction(signed_tx)
                tx_hash_hex = _to_hex(tx_hash)
                record.sweep_tx_hash = tx_hash_hex
                self._save_record(record)

                logger.info(
                    "Native ROSE bridge sweep broadcast: chain=%d tx=%s",
                    chain_id,
                    tx_hash_hex,
                )

                receipt = await self._wait_for_receipt(w3, tx_hash)
                if receipt["status"] != 1:
                    raise ValueError(f"Native ROSE bridge sweep tx reverted: {tx_hash_hex}")

                # Credit the value this sweep tx sent to custody (its tx.value =
                # sweep_amount = balance − sweep_gas_cost), never a delta measured
                # on the shared custody address: that delta races across concurrent
                # bridge-ins, which run in parallel under the per-deposit-address
                # lock and would each credit the other's value too.
                record.amount = sweep_amount
                record.state = SweepState.SWEPT
                self._save_record(record)

                try:
                    await self._idempotent_credit(
                        beneficiary=beneficiary,
                        token_id=token_id,
                        amount=sweep_amount,
                        deposit_id=deposit_id,
                    )
                except Exception as exc:
                    logger.exception(
                        "Credit failed after native ROSE bridge sweep for %s on chain %d",
                        deposit_address,
                        chain_id,
                    )
                    raise SweepCreditPendingError(record.deposit_id_hex, exc) from exc

                logger.info(
                    "Native ROSE bridge deposit credited: beneficiary=%s amount=%d",
                    beneficiary,
                    sweep_amount,
                )

                self._delete_record(deposit_address, chain_id)

            except SweepCreditPendingError:
                raise
            except Exception:
                logger.exception(
                    "Native ROSE bridge sweep failed for %s on chain %d",
                    deposit_address,
                    chain_id,
                )
                raise

    async def sweep_xrose_bridge(
        self,
        deposit_address: str,
        beneficiary: str,
        chain_type: str,
        version: int,
        chain_id: int,
        token_id: bytes,
        token_address: str,
        bridge_address: str,
        amount: int,
        deposit_id: bytes,
    ) -> None:
        """Bridge-in xROSE: sweep to ROFLBridge → reserve burn → enqueue → credit.

        Differs from ``sweep_erc20`` in two ways: the sweep recipient is the
        configured ROFLBridge route on chain ``84532``, and credit must not run
        until the custody-tx executor confirms the bridge burn at receipt
        ``status == 1``. Crediting earlier would let an attacker mint xROSE on
        Base and double-credit on Sapphire.
        """
        if self._executor is None:
            raise ValueError(
                "executor is required for xROSE bridge-in flow; "
                "wire CustodyTxExecutor into SweepEngine.__init__"
            )

        lock = self._get_address_lock(deposit_address, chain_id)
        async with lock:
            w3 = self._get_web3(chain_id)

            try:
                erc20_balance = await self._get_erc20_balance(w3, token_address, deposit_address)
                if erc20_balance == 0:
                    if await self._accounting.is_deposit_processed(deposit_id):
                        logger.info(
                            "xROSE bridge: %s already swept and processed on chain %d",
                            deposit_address,
                            chain_id,
                        )
                        return
                    raise ValueError(
                        f"xROSE balance is 0 at {deposit_address} on chain {chain_id} "
                        f"but deposit_id is not yet processed — nothing to sweep"
                    )
                if erc20_balance < amount:
                    raise ValueError(
                        f"xROSE balance ({erc20_balance}) < verified amount ({amount}) "
                        f"for {token_address} at {deposit_address} on chain {chain_id}. "
                        f"Possible fee-on-transfer token — not supported."
                    )

                gas_price = await self._get_safe_gas_price(w3, chain_id)

                record = SweepRecord(
                    deposit_address=deposit_address,
                    chain_id=chain_id,
                    state=SweepState.PENDING,
                    beneficiary=beneficiary,
                    chain_type=chain_type,
                    version=version,
                    amount=amount,
                    token_id_hex=_to_hex(token_id),
                    token_address=token_address,
                    deposit_id_hex=_to_hex(deposit_id),
                    flow_type=FLOW_XROSE_BRIDGE_IN,
                    bridge_address=bridge_address,
                )
                self._save_record(record)

                await self._gas_fund_deposit_address(
                    w3, chain_id, deposit_address, record, gas_price
                )

                gas_price = await self._get_safe_gas_price(w3, chain_id)
                nonce = await w3.eth.get_transaction_count(deposit_address)

                signed_sweep = await self._accounting.generate_sweep_erc20_to_bridge(
                    beneficiary=beneficiary,
                    chain_type=chain_type,
                    version=version,
                    chain_id=chain_id,
                    token_address=token_address,
                    amount=amount,
                    source_chain_nonce=nonce,
                    gas_price=gas_price,
                )

                sweep_tx_hash = await w3.eth.send_raw_transaction(signed_sweep)
                sweep_tx_hash_hex = _to_hex(sweep_tx_hash)
                record.sweep_tx_hash = sweep_tx_hash_hex
                self._save_record(record)

                logger.info(
                    "xROSE bridge sweep broadcast: chain=%d tx=%s recipient=%s",
                    chain_id,
                    sweep_tx_hash_hex,
                    bridge_address,
                )

                sweep_receipt = await self._wait_for_receipt(w3, sweep_tx_hash)
                if sweep_receipt["status"] != 1:
                    raise ValueError(f"xROSE bridge sweep tx reverted: {sweep_tx_hash_hex}")

                record.state = SweepState.SWEPT
                self._save_record(record)

                executor_key = await self._reserve_burn_and_enqueue(
                    record=record,
                    deposit_id=deposit_id,
                    amount=amount,
                    bridge_address=bridge_address,
                )

                executor_record = await self._wait_for_burn_resolution(record, executor_key)
                if executor_record.tx_hash:
                    record.burn_tx_hash = executor_record.tx_hash
                record.state = SweepState.BURNED
                self._save_record(record)

                try:
                    await self._idempotent_credit(
                        beneficiary=beneficiary,
                        token_id=token_id,
                        amount=amount,
                        deposit_id=deposit_id,
                    )
                except Exception as exc:
                    logger.exception(
                        "Credit failed after xROSE bridge burn for %s on chain %d",
                        deposit_address,
                        chain_id,
                    )
                    raise SweepCreditPendingError(record.deposit_id_hex, exc) from exc

                logger.info(
                    "xROSE bridge deposit credited: beneficiary=%s amount=%d deposit_id=%s",
                    beneficiary,
                    amount,
                    record.deposit_id_hex,
                )
                self._delete_record(deposit_address, chain_id)

            except SweepCreditPendingError:
                raise
            except Exception:
                logger.exception(
                    "xROSE bridge flow failed for %s on chain %d",
                    deposit_address,
                    chain_id,
                )
                raise

    async def _gas_fund_deposit_address(
        self,
        w3: AsyncWeb3,
        chain_id: int,
        deposit_address: str,
        record: "SweepRecord",
        gas_price: int,
    ) -> None:
        """Fund the per-user deposit address with ETH for the ERC20 sweep.

        Same 2-step gas-tank-locked pattern as ``sweep_erc20``: serializes
        gas-tank nonce reads until the receipt arrives so concurrent sweeps
        cannot pick up the same pending nonce.
        """
        gas_amount = GAS_FUNDING_AMOUNT_WEI.get(chain_id, 200_000_000_000_000)
        l1_data_fee = await estimate_l1_data_fee(w3, chain_id, is_erc20=True)
        gas_amount += l1_data_fee

        async with self._gas_tank_lock:
            gas_tank_nonce = await w3.eth.get_transaction_count(
                await self._get_gas_tank_address(), "pending"
            )

            gas_tx = await self._accounting.generate_gas_funding_tx(
                to_deposit_address=deposit_address,
                chain_id=chain_id,
                gas_amount=gas_amount,
                gas_tank_nonce=gas_tank_nonce,
                gas_price=gas_price,
            )
            gas_tx_hash = await w3.eth.send_raw_transaction(gas_tx)
            gas_tx_hash_hex = _to_hex(gas_tx_hash)
            self._gas_funding_tx_hashes.add(gas_tx_hash_hex.lower())
            record.gas_funding_tx_hash = gas_tx_hash_hex
            record.state = SweepState.GAS_FUNDED
            self._save_record(record)

            logger.info("xROSE gas funding broadcast: chain=%d tx=%s", chain_id, gas_tx_hash_hex)
            gas_receipt = await self._wait_for_receipt(w3, gas_tx_hash)
            if gas_receipt["status"] != 1:
                raise ValueError(f"Gas funding tx reverted: {gas_tx_hash_hex}")

    async def _reserve_burn_and_enqueue(
        self,
        record: "SweepRecord",
        deposit_id: bytes,
        amount: int,
        bridge_address: str,
    ) -> str:
        """Reserve the burn nonce on Sapphire and hand the signed burn to the executor.

        Idempotent on the on-chain side: ``reserveBridgeBurn`` for the same
        ``(depositId, chainId, bridge, amount)`` is a no-op on the second
        call, and ``executor.enqueue`` is idempotent per ``(chain_id, nonce)``.
        Returns the executor record key for later resolution waiting.
        """
        if not record.burn_reserved:
            await self._accounting.reserve_bridge_burn(
                deposit_id=deposit_id,
                chain_id=BASE_SEPOLIA_CHAIN_ID,
                bridge=bridge_address,
                amount=amount,
            )
            record.burn_reserved = True
            self._save_record(record)

        evm_nonce = await self._await_bridge_burn_nonce(deposit_id)

        signed_burn = await self._accounting.generate_bridge_burn_transfer(deposit_id)
        # route_address + amount let the executor's kind-routed preflight
        # eth_call `ROFLBridge.burn(amount, depositId)` from the custody EOA
        # on every broadcast attempt — paused / limit-exhausted reverts auto-
        # retry instead of broadcasting a doomed tx and stalling the Base
        # nonce sequence in MANUAL_REVIEW.
        request = CustodyTxRequest(
            chain_id=BASE_SEPOLIA_CHAIN_ID,
            evm_nonce=evm_nonce,
            kind=CustodyTxKind.XROSE_BURN,
            id=record.deposit_id_hex,
            signed_tx=bytes(signed_burn),
            route_address=bridge_address,
            amount=amount,
        )
        assert self._executor is not None
        key = await self._executor.enqueue(request)
        record.state = SweepState.BURN_PENDING
        self._save_record(record)
        logger.info(
            "xROSE burn enqueued: chain=%d nonce=%d deposit_id=%s",
            BASE_SEPOLIA_CHAIN_ID,
            evm_nonce,
            record.deposit_id_hex,
        )
        return key

    async def _await_bridge_burn_nonce(self, deposit_id: bytes) -> int:
        """Poll the on-chain reservation mapping until the nonce is visible.

        ROFL submission returns before the Sapphire block is mined, so the
        reservation is not immediately readable. ``get_bridge_burn_nonce``
        calls the ``getBridgeBurnRequest`` view on ``BridgeModule`` and raises
        ``ValueError`` until the mapping slot is populated; we retry on a fixed
        cadence rather than blocking on receipt confirmation.
        """
        last_error: Optional[Exception] = None
        for attempt in range(BRIDGE_BURN_RESERVATION_POLL_ATTEMPTS):
            try:
                return await self._accounting.get_bridge_burn_nonce(deposit_id)
            except Exception as exc:
                last_error = exc
                if attempt + 1 < BRIDGE_BURN_RESERVATION_POLL_ATTEMPTS:
                    await asyncio.sleep(BRIDGE_BURN_RESERVATION_POLL_INTERVAL)
        raise ValueError(
            f"Bridge burn reservation for deposit_id={_to_hex(deposit_id)} "
            f"not visible after {BRIDGE_BURN_RESERVATION_POLL_ATTEMPTS} attempts"
        ) from last_error

    async def _read_rofl_bridge_burned(
        self,
        chain_id: int,
        bridge_address: str,
        deposit_id: bytes,
        *,
        block_identifier: Optional[int] = None,
    ) -> bool:
        """Read ``ROFLBridge.burnedDepositIds(depositId)`` for the given bridge.

        ``bridge_address`` is taken from the on-chain reservation event so a
        stale per-deposit binding survives a config rotation. RPC failure
        propagates as an exception — never returns False on missing evidence.

        When ``block_identifier`` is supplied the view is evaluated at that
        block, letting callers pin a snapshot across multiple reads.
        """
        if len(deposit_id) != 32:
            raise ValueError("deposit_id must be 32 bytes")
        w3 = self._get_web3(chain_id)
        contract = w3.eth.contract(
            address=w3.to_checksum_address(bridge_address), abi=ROFL_BRIDGE_ABI
        )
        call = contract.functions.burnedDepositIds(deposit_id)
        if block_identifier is None:
            return bool(await call.call())
        return bool(await call.call(block_identifier=block_identifier))

    async def _read_rofl_bridge_burned_event(
        self,
        chain_id: int,
        bridge_address: str,
        deposit_id: bytes,
        *,
        to_block: Optional[int] = None,
    ) -> Optional[int]:
        """Return ``amount`` from the ``Burned`` event for ``deposit_id``.

        ``Burned(bytes32 indexed depositId, uint256 amount)`` is topic-filtered
        on the indexed ``depositId``, so the bloom-filter does the heavy
        lifting. Returns ``None`` when no event is observed. RPC failure
        propagates as an exception — never returns ``None`` on missing
        evidence.

        ``ROFLBridge.burn`` reverts on a repeat ``depositId`` (single-burn
        invariant). Two or more matching events therefore reflect a reorg
        or duplicated log: raise ``ReconstructionEvidenceError`` rather than
        silently picking one.
        """
        if len(deposit_id) != 32:
            raise ValueError("deposit_id must be 32 bytes")
        w3 = self._get_web3(chain_id)
        contract = w3.eth.contract(
            address=w3.to_checksum_address(bridge_address), abi=ROFL_BRIDGE_ABI
        )
        filter_kwargs: Dict[str, Any] = {
            "from_block": 0,
            "argument_filters": {"depositId": deposit_id},
        }
        if to_block is not None:
            filter_kwargs["to_block"] = to_block
        events = await contract.events.Burned.get_logs(**filter_kwargs)
        if not events:
            return None
        if len(events) > 1:
            raise ReconstructionEvidenceError(
                deposit_id,
                f"{len(events)} Burned events observed for single-burn invariant",
            )
        return int(events[0]["args"]["amount"])

    @staticmethod
    def _executor_record_matches_xrose_burn(
        executor_record: CustodyTxRecord, deposit_id_hex: str
    ) -> bool:
        """Identity guard for an executor record claiming to be this deposit's burn.

        Defends against the executor honoring a record at the same nonce that
        belongs to a different caller (state-file corruption, future caller
        misuse).
        """
        return (
            executor_record.kind is CustodyTxKind.XROSE_BURN
            and executor_record.id == deposit_id_hex
        )

    async def _wait_for_burn_resolution(
        self, record: "SweepRecord", executor_key: str
    ) -> CustodyTxRecord:
        """Block until the executor flips the matching record to a terminal state.

        Raises ``SweepCreditPendingError`` on timeout or on any terminal status
        other than SUCCESS so the recovery loop continues the deposit instead
        of crediting prematurely.
        """
        assert self._executor is not None
        try:
            executor_record = await self._executor.wait_for_resolution(
                executor_key, timeout=BURN_RESOLUTION_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            raise SweepCreditPendingError(record.deposit_id_hex, exc) from exc

        if executor_record.status is not CustodyTxStatus.SUCCESS:
            raise SweepCreditPendingError(
                record.deposit_id_hex,
                ValueError(f"burn resolved to non-success state: {executor_record.status.value}"),
            )
        if not self._executor_record_matches_xrose_burn(executor_record, record.deposit_id_hex):
            raise SweepCreditPendingError(
                record.deposit_id_hex,
                ValueError(
                    f"executor record at burn nonce does not identify this deposit "
                    f"(kind={executor_record.kind.value!r}, id={executor_record.id!r}); "
                    f"refusing to credit"
                ),
            )
        return executor_record

    async def sweep_native(
        self,
        deposit_address: str,
        beneficiary: str,
        chain_type: str,
        version: int,
        chain_id: int,
        token_id: bytes,
        amount: int,
        deposit_id: bytes,
    ) -> None:
        """Execute full native sweep: gas fund → sweep → confirm → credit.

        Uses the same 2-step gas-tank pattern as ERC20 sweeps: fund gas
        externally so the full verified amount is swept and credited.

        Concurrency: acquires per-address lock so concurrent claims queue.
        Zero-balance: if balance=0 after another sweep, skips straight to credit.
        """
        lock = self._get_address_lock(deposit_address, chain_id)
        async with lock:
            w3 = self._get_web3(chain_id)

            try:
                balance = await w3.eth.get_balance(deposit_address)

                if balance == 0:
                    if await self._accounting.is_deposit_processed(deposit_id):
                        logger.info(
                            "Deposit address %s already empty and deposit_id already processed on chain %d",
                            deposit_address,
                            chain_id,
                        )
                        return
                    raise ValueError(
                        f"Native balance is 0 at {deposit_address} on chain {chain_id} "
                        f"but deposit_id is not yet processed — nothing to sweep"
                    )

                if balance < amount:
                    raise ValueError(
                        f"Native balance ({balance}) < verified amount ({amount}) "
                        f"at {deposit_address} on chain {chain_id}"
                    )

                gas_price = await self._get_safe_gas_price(w3, chain_id)

                record = SweepRecord(
                    deposit_address=deposit_address,
                    chain_id=chain_id,
                    state=SweepState.PENDING,
                    beneficiary=beneficiary,
                    chain_type=chain_type,
                    version=version,
                    amount=amount,
                    token_id_hex=_to_hex(token_id),
                    token_address=None,  # native
                    deposit_id_hex=_to_hex(deposit_id),
                )
                self._save_record(record)

                # Step 1: Fund gas to deposit address (same pattern as ERC20)
                # Base gas covers L2 execution; L1 data fee covers calldata posting
                gas_amount = GAS_FUNDING_AMOUNT_WEI.get(chain_id, 200_000_000_000_000)
                l1_data_fee = await estimate_l1_data_fee(w3, chain_id, is_erc20=False)
                gas_amount += l1_data_fee

                # Hold the gas tank lock through receipt confirmation.
                # Releasing after broadcast would let a concurrent sweep read
                # the same nonce before this tx is mined — get_transaction_count
                # defaults to "latest" (confirmed), and even "pending" has an
                # RPC-to-submit race window. Serializing until the receipt
                # arrives is correct at the cost of gas-funding throughput.
                async with self._gas_tank_lock:
                    gas_tank_nonce = await w3.eth.get_transaction_count(
                        await self._get_gas_tank_address(), "pending"
                    )

                    gas_tx = await self._accounting.generate_gas_funding_tx(
                        to_deposit_address=deposit_address,
                        chain_id=chain_id,
                        gas_amount=gas_amount,
                        gas_tank_nonce=gas_tank_nonce,
                        gas_price=gas_price,
                    )

                    gas_tx_hash = await w3.eth.send_raw_transaction(gas_tx)

                    gas_tx_hash_hex = _to_hex(gas_tx_hash)
                    self._gas_funding_tx_hashes.add(gas_tx_hash_hex.lower())
                    record.gas_funding_tx_hash = gas_tx_hash_hex
                    record.state = SweepState.GAS_FUNDED
                    self._save_record(record)

                    logger.info(
                        "Native gas funding broadcast: chain=%d tx=%s", chain_id, gas_tx_hash_hex
                    )

                    gas_receipt = await self._wait_for_receipt(w3, gas_tx_hash)
                    if gas_receipt["status"] != 1:
                        raise ValueError(f"Gas funding tx reverted: {gas_tx_hash_hex}")

                # Step 2: Sweep full verified amount (re-read gas price — may have changed)
                gas_price = await self._get_safe_gas_price(w3, chain_id)

                nonce = await w3.eth.get_transaction_count(deposit_address)

                signed_tx = await self._accounting.generate_sweep_native(
                    beneficiary=beneficiary,
                    chain_type=chain_type,
                    version=version,
                    chain_id=chain_id,
                    amount=amount,
                    nonce=nonce,
                    gas_price=gas_price,
                )

                tx_hash = await w3.eth.send_raw_transaction(signed_tx)
                tx_hash_hex = _to_hex(tx_hash)
                record.sweep_tx_hash = tx_hash_hex
                self._save_record(record)  # persist tx hash for crash recovery (still GAS_FUNDED)

                logger.info("Native sweep broadcast: chain=%d tx=%s", chain_id, tx_hash_hex)

                receipt = await self._wait_for_receipt(w3, tx_hash)
                if receipt["status"] != 1:
                    raise ValueError(f"Sweep tx reverted: {tx_hash_hex}")

                record.state = SweepState.SWEPT
                self._save_record(record)

                # Step 3: Credit
                try:
                    await self._idempotent_credit(
                        beneficiary=beneficiary,
                        token_id=token_id,
                        amount=amount,
                        deposit_id=deposit_id,
                    )
                except Exception as exc:
                    logger.exception(
                        "Credit failed after successful native sweep for %s on chain %d",
                        deposit_address,
                        chain_id,
                    )
                    raise SweepCreditPendingError(record.deposit_id_hex, exc) from exc

                logger.info(
                    "Deposit credited after native sweep: beneficiary=%s amount=%d",
                    beneficiary,
                    amount,
                )

                # Only clean up on full success — partial failures (sweep ok, credit failed)
                # leave the record for recovery
                self._delete_record(deposit_address, chain_id)

            except SweepCreditPendingError:
                raise
            except Exception:
                logger.exception(
                    "Native sweep failed for %s on chain %d", deposit_address, chain_id
                )
                raise

    async def sweep_erc20(
        self,
        deposit_address: str,
        beneficiary: str,
        chain_type: str,
        version: int,
        chain_id: int,
        token_address: str,
        token_id: bytes,
        amount: int,
        deposit_id: bytes,
    ) -> None:
        """Execute full ERC20 sweep: gas fund → sweep → confirm → credit.

        Concurrency: acquires per-address lock so concurrent claims queue.
        Zero-balance: if ERC20 balance=0 after another sweep, skips straight to credit.
        """
        lock = self._get_address_lock(deposit_address, chain_id)
        async with lock:
            w3 = self._get_web3(chain_id)

            try:
                erc20_balance = await self._get_erc20_balance(w3, token_address, deposit_address)
                if erc20_balance == 0:
                    if await self._accounting.is_deposit_processed(deposit_id):
                        logger.info(
                            "ERC20 %s balance=0 at %s on chain %d and deposit_id already processed",
                            token_address,
                            deposit_address,
                            chain_id,
                        )
                        return
                    raise ValueError(
                        f"ERC20 {token_address} balance is 0 at {deposit_address} on chain {chain_id} "
                        f"but deposit_id is not yet processed — nothing to sweep"
                    )

                if erc20_balance < amount:
                    raise ValueError(
                        f"ERC20 balance ({erc20_balance}) < verified amount ({amount}) "
                        f"for {token_address} at {deposit_address} on chain {chain_id}. "
                        f"Possible fee-on-transfer token — not supported."
                    )

                gas_price = await self._get_safe_gas_price(w3, chain_id)

                record = SweepRecord(
                    deposit_address=deposit_address,
                    chain_id=chain_id,
                    state=SweepState.PENDING,
                    beneficiary=beneficiary,
                    chain_type=chain_type,
                    version=version,
                    amount=amount,
                    token_id_hex=_to_hex(token_id),
                    token_address=token_address,
                    deposit_id_hex=_to_hex(deposit_id),
                )
                self._save_record(record)

                # Step 1: Fund gas to deposit address
                # Base gas covers L2 execution; L1 data fee covers calldata posting
                gas_amount = GAS_FUNDING_AMOUNT_WEI.get(chain_id, 200_000_000_000_000)
                l1_data_fee = await estimate_l1_data_fee(w3, chain_id, is_erc20=True)
                gas_amount += l1_data_fee

                # Hold the gas tank lock through receipt confirmation.
                # Releasing after broadcast would let a concurrent sweep read
                # the same nonce before this tx is mined — get_transaction_count
                # defaults to "latest" (confirmed), and even "pending" has an
                # RPC-to-submit race window. Serializing until the receipt
                # arrives is correct at the cost of gas-funding throughput.
                async with self._gas_tank_lock:
                    gas_tank_nonce = await w3.eth.get_transaction_count(
                        await self._get_gas_tank_address(), "pending"
                    )

                    gas_tx = await self._accounting.generate_gas_funding_tx(
                        to_deposit_address=deposit_address,
                        chain_id=chain_id,
                        gas_amount=gas_amount,
                        gas_tank_nonce=gas_tank_nonce,
                        gas_price=gas_price,
                    )

                    gas_tx_hash = await w3.eth.send_raw_transaction(gas_tx)

                    gas_tx_hash_hex = _to_hex(gas_tx_hash)
                    self._gas_funding_tx_hashes.add(gas_tx_hash_hex.lower())
                    record.gas_funding_tx_hash = gas_tx_hash_hex
                    record.state = SweepState.GAS_FUNDED
                    self._save_record(record)

                    logger.info("Gas funding broadcast: chain=%d tx=%s", chain_id, gas_tx_hash_hex)

                    gas_receipt = await self._wait_for_receipt(w3, gas_tx_hash)
                    if gas_receipt["status"] != 1:
                        raise ValueError(f"Gas funding tx reverted: {gas_tx_hash_hex}")

                # Step 2: Sweep ERC20 (re-read gas price — may have changed during gas funding)
                gas_price = await self._get_safe_gas_price(w3, chain_id)

                deposit_nonce = await w3.eth.get_transaction_count(deposit_address)

                signed_tx = await self._accounting.generate_sweep_erc20(
                    beneficiary=beneficiary,
                    chain_type=chain_type,
                    version=version,
                    chain_id=chain_id,
                    token_address=token_address,
                    amount=amount,
                    nonce=deposit_nonce,
                    gas_price=gas_price,
                )

                sweep_tx_hash = await w3.eth.send_raw_transaction(signed_tx)
                sweep_hash_hex = _to_hex(sweep_tx_hash)
                record.sweep_tx_hash = sweep_hash_hex
                self._save_record(record)  # persist tx hash for crash recovery (still GAS_FUNDED)

                logger.info("ERC20 sweep broadcast: chain=%d tx=%s", chain_id, sweep_hash_hex)

                receipt = await self._wait_for_receipt(w3, sweep_tx_hash)
                if receipt["status"] != 1:
                    raise ValueError(f"ERC20 sweep tx reverted: {sweep_hash_hex}")

                record.state = SweepState.SWEPT
                self._save_record(record)

                # Step 3: Credit
                try:
                    await self._idempotent_credit(
                        beneficiary=beneficiary,
                        token_id=token_id,
                        amount=amount,
                        deposit_id=deposit_id,
                    )
                except Exception as exc:
                    logger.exception(
                        "Credit failed after successful ERC20 sweep for %s on chain %d",
                        deposit_address,
                        chain_id,
                    )
                    raise SweepCreditPendingError(record.deposit_id_hex, exc) from exc

                logger.info(
                    "Deposit credited after ERC20 sweep: beneficiary=%s amount=%d",
                    beneficiary,
                    amount,
                )

                # Only clean up on full success
                self._delete_record(deposit_address, chain_id)

            except SweepCreditPendingError:
                raise
            except Exception:
                logger.exception("ERC20 sweep failed for %s on chain %d", deposit_address, chain_id)
                raise

    async def _get_gas_tank_address(self) -> str:
        """Read gasTankAddress from the contract via public getter."""
        return await self._accounting.get_gas_tank_address()

    async def _get_erc20_balance(self, w3: AsyncWeb3, token_address: str, holder: str) -> int:
        """Read ERC20 balance for holder. Uses minimal ABI (balanceOf only)."""
        erc20_abi = [
            {
                "constant": True,
                "inputs": [{"name": "", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function",
            }
        ]
        contract = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=erc20_abi)
        return await contract.functions.balanceOf(w3.to_checksum_address(holder)).call()

    async def _idempotent_credit(
        self, beneficiary: str, token_id: bytes, amount: int, deposit_id: bytes
    ) -> None:
        """Credit deposit, treating DepositAlreadyProcessed revert as success.

        Race condition defense: two /deposits/check requests for the same deposit
        can both pass the processedDeposits pre-check. The first credits successfully;
        the second hits the on-chain DepositAlreadyProcessed revert. That's not an error —
        the deposit was credited, just by the other request.
        """
        try:
            await self._accounting.credit_deposit(
                beneficiary=beneficiary,
                token_id=token_id,
                amount=amount,
                deposit_id=deposit_id,
            )
        except TransactionRevertedError as exc:
            if exc.error_name == "DepositAlreadyProcessed":
                logger.info(
                    "Deposit already processed (concurrent race): deposit_id=%s",
                    _to_hex(deposit_id),
                )
                return  # Not an error — deposit was credited by another request
            raise

    async def _credit_and_delete(self, record: SweepRecord) -> None:
        """Idempotent-credit a SWEPT record and remove it from disk on success."""
        await self._idempotent_credit(
            beneficiary=record.beneficiary,
            token_id=_from_hex(record.token_id_hex),
            amount=record.amount,
            deposit_id=_from_hex(record.deposit_id_hex),
        )
        self._delete_record(record.deposit_address, record.chain_id)

    def _promote_to_manual_review_on_kind_mismatch(
        self,
        record: SweepRecord,
        executor_record: CustodyTxRecord,
        evm_nonce: int,
        status: CustodyTxStatus,
    ) -> bool:
        """Promote ``record`` to MANUAL_REVIEW if the executor record at this
        nonce belongs to a different deposit.

        A nonce-collision with a different caller is durable state corruption;
        one persistent signal on the sweep record beats recurring log spam. The
        caller is responsible for stopping after a return value of ``True``.
        """
        if self._executor_record_matches_xrose_burn(executor_record, record.deposit_id_hex):
            return False
        logger.critical(
            "xROSE recovery: executor record at nonce %d is %s but kind/id does not "
            "match this deposit (kind=%r id=%r expected xrose_burn id=%s) — "
            "marking sweep record MANUAL_REVIEW",
            evm_nonce,
            status.value,
            executor_record.kind.value,
            executor_record.id,
            record.deposit_id_hex,
        )
        record.state = SweepState.MANUAL_REVIEW
        record.error = (
            f"executor record kind/id mismatch at nonce {evm_nonce} "
            f"(status={status.value}): got kind={executor_record.kind.value} "
            f"id={executor_record.id!r}, expected xrose_burn id={record.deposit_id_hex}"
        )
        self._save_record(record)
        return True

    def load_incomplete_sweeps(self) -> list[SweepRecord]:
        """Load all non-idle sweep records (for restart recovery).

        Also re-populates gas_funding_tx_hashes from persisted records
        to prevent gas funding txs from being claimed as deposits after restart.
        """
        records = []
        for path in self._state_dir.glob("sweep_*.json"):
            try:
                data = json.loads(path.read_text())
                record = SweepRecord.from_dict(data)
                records.append(record)
                if record.gas_funding_tx_hash:
                    self._gas_funding_tx_hashes.add(record.gas_funding_tx_hash.lower())
                if record.deposit_id_hex:
                    self._deposit_id_index[record.deposit_id_hex.lower()] = (
                        record.deposit_address,
                        record.chain_id,
                    )
            except Exception as exc:
                raw = path.read_text()
                logger.critical(
                    "Corrupt sweep record %s — renamed to .corrupt: %s\nRaw content: %s",
                    path,
                    exc,
                    raw,
                )
                path.rename(path.with_suffix(".corrupt"))
        return records

    async def _reconcile_sweep_tx(self, record: SweepRecord) -> None:
        """Promote a record to SWEPT if its sweep tx already mined on-chain.

        Closes the crash window between sweep broadcast and the state flip:
        sweep_native/sweep_erc20 persist sweep_tx_hash *before* flipping state
        to SWEPT (see the "persist tx hash for crash recovery" save). A crash
        during _wait_for_receipt would otherwise leave the record as GAS_FUNDED
        with a valid, possibly-mined tx hash — and without reconciliation the
        funds would be swept but never credited.

        Safe: only inspects an existing tx hash, never re-signs or re-broadcasts,
        so the nonce-collision concern that blocks re-sweeping does not apply.
        Mutates record in place and persists when promoted; leaves it untouched
        on any failure so the next pass can try again.
        """
        # Reconcile only exists to promote GAS_FUNDED → SWEPT after a mid-receipt
        # crash. Anything already at SWEPT or downstream of it (BURN_PENDING /
        # BURNED) must NOT be demoted: doing so would lock the xROSE bridge flow
        # out of its BURN_PENDING → executor SUCCESS → credit branch, and would
        # erase an operator's MANUAL_REVIEW marker on records still carrying a
        # sweep_tx_hash.
        if record.state in (
            SweepState.SWEPT,
            SweepState.BURN_PENDING,
            SweepState.BURNED,
            SweepState.MANUAL_REVIEW,
        ):
            return
        if not record.sweep_tx_hash:
            return

        try:
            w3 = self._get_web3(record.chain_id)
            receipt = await w3.eth.get_transaction_receipt(record.sweep_tx_hash)
        except TransactionNotFound:
            return
        except Exception:
            logger.exception(
                "Reconcile: receipt lookup failed for %s on chain %d (tx=%s)",
                record.deposit_address,
                record.chain_id,
                record.sweep_tx_hash,
            )
            return

        if receipt["status"] == 1:
            if record.flow_type == FLOW_NATIVE_ROSE_BRIDGE_IN:
                # Native ROSE bridge credits the actual value moved, not the
                # verified gross. The happy path credits the sweep tx value
                # (sweep_amount = balance − gas); after a crash we recover the
                # same value from the on-chain tx.value. It equals sweep_amount
                # in the no-concurrent-inbound-transfer case (the norm) and is
                # strictly less in the edge case, never over-credits.
                try:
                    tx = await w3.eth.get_transaction(record.sweep_tx_hash)
                except Exception:
                    logger.exception(
                        "Reconcile: failed to read native ROSE bridge sweep tx %s on chain "
                        "%d — leaving record at %s for next pass",
                        record.sweep_tx_hash,
                        record.chain_id,
                        record.state.value,
                    )
                    return
                tx_value = int(tx["value"])
                if tx_value <= 0:
                    logger.critical(
                        "Reconcile: native ROSE bridge sweep tx %s has non-positive value "
                        "%d on chain %d — refusing to promote SWEPT",
                        record.sweep_tx_hash,
                        tx_value,
                        record.chain_id,
                    )
                    return
                record.amount = tx_value
            record.state = SweepState.SWEPT
            self._save_record(record)
            logger.warning(
                "Reconcile: sweep tx %s mined during crash window — promoted to SWEPT for %s on chain %d",
                record.sweep_tx_hash,
                record.deposit_address,
                record.chain_id,
            )
        else:
            reverted_tx_hash = record.sweep_tx_hash
            if record.flow_type == FLOW_NATIVE_ROSE_BRIDGE_IN:
                # Re-read residual balance + clear sweep_tx_hash so the next
                # resume re-broadcasts instead of raising SweepRecoveryStuckError.
                try:
                    w3 = self._get_web3(record.chain_id)
                    current_balance = await w3.eth.get_balance(record.deposit_address)
                except Exception:
                    logger.exception(
                        "Reconcile-recover: balance read failed for %s on chain %d; "
                        "leaving record at PENDING for next pass",
                        record.deposit_address,
                        record.chain_id,
                    )
                    return
                if current_balance <= 0:
                    logger.critical(
                        "Reconcile-recover: deposit %s drained after reverted sweep on chain %d; "
                        "expected residual but got zero — refusing to advance",
                        record.deposit_address,
                        record.chain_id,
                    )
                    return
                record.amount = current_balance
                record.sweep_tx_hash = None
                self._save_record(record)
                logger.warning(
                    "Reconcile-recover: cleared reverted native-rose sweep %s for %s on chain %d; "
                    "next reconcile pass will re-enter sweep_native_rose_bridge with amount=%d",
                    reverted_tx_hash,
                    record.deposit_address,
                    record.chain_id,
                    current_balance,
                )
                return
            logger.error(
                "Reconcile: sweep tx %s reverted on chain for %s on chain %d — manual investigation required",
                reverted_tx_hash,
                record.deposit_address,
                record.chain_id,
            )

    def _persist_resume_error(self, record: SweepRecord, exc: Exception) -> None:
        """Flag a resumable record as errored so a user retry can cleanup and restart.

        Skip if a sweep tx was broadcast mid-attempt: user retry would then
        cleanup_record and orphan the pending tx. Re-read disk — the local
        record is stale after _resume_sweep_from_pending ran.
        """
        current = self.get_sweep_record(record.deposit_address, record.chain_id)
        if current is None or current.sweep_tx_hash:
            return
        current.error = str(exc)
        self._save_record(current)

    async def _resume_sweep_from_pending(self, record: SweepRecord) -> None:
        """Re-run sweep for a PENDING/GAS_FUNDED record with no broadcast sweep tx.

        Safe because no tx was broadcast: no nonce-collision risk, and
        sweep_native/sweep_erc20 short-circuit on balance=0 + is_deposit_processed
        if the original task resolved it concurrently.
        """
        token_id = _from_hex(record.token_id_hex)
        deposit_id = _from_hex(record.deposit_id_hex)

        if record.token_address is None:
            await self.sweep_native(
                deposit_address=record.deposit_address,
                beneficiary=record.beneficiary,
                chain_type=record.chain_type,
                version=record.version,
                chain_id=record.chain_id,
                token_id=token_id,
                amount=record.amount,
                deposit_id=deposit_id,
            )
        else:
            await self.sweep_erc20(
                deposit_address=record.deposit_address,
                beneficiary=record.beneficiary,
                chain_type=record.chain_type,
                version=record.version,
                chain_id=record.chain_id,
                token_address=record.token_address,
                token_id=token_id,
                amount=record.amount,
                deposit_id=deposit_id,
            )

    async def _resume_xrose_bridge_record(self, record: SweepRecord) -> None:
        """Recover one xROSE bridge-in record by inspecting its on-chain progress.

        The four cases mirror the forward path: SWEPT (need to reserve+enqueue),
        BURN_PENDING (executor owns broadcast, just credit on its SUCCESS),
        BURNED (credit retry), and pre-SWEPT (reuse the generic resume path).
        """
        if self._executor is None:
            raise ValueError(
                "executor is required for xROSE recovery; "
                "wire CustodyTxExecutor into SweepEngine.__init__"
            )

        deposit_id = _from_hex(record.deposit_id_hex)
        token_id = _from_hex(record.token_id_hex)

        if record.state == SweepState.MANUAL_REVIEW:
            logger.warning(
                "xROSE recovery: record %s is MANUAL_REVIEW (deposit_id=%s) — leaving for operator",
                record.deposit_address,
                record.deposit_id_hex,
            )
            return

        if record.state == SweepState.BURNED:
            await self._credit_and_delete(record)
            return

        if record.state == SweepState.SWEPT:
            if not record.bridge_address:
                raise ValueError(
                    f"xROSE bridge record for {record.deposit_address} has no bridge_address"
                )
            await self._reserve_burn_and_enqueue(
                record=record,
                deposit_id=deposit_id,
                amount=record.amount,
                bridge_address=record.bridge_address,
            )
            return  # next pass picks it up in BURN_PENDING

        if record.state == SweepState.BURN_PENDING:
            try:
                evm_nonce = await self._accounting.get_bridge_burn_nonce(deposit_id)
            except ValueError:
                logger.info(
                    "xROSE recovery: BridgeBurnReserved event not yet visible for %s; "
                    "executor catch-up will create the record on next pass",
                    record.deposit_id_hex,
                )
                return

            executor_record = self._executor.get_record(BASE_SEPOLIA_CHAIN_ID, evm_nonce)
            status = executor_record.status if executor_record else None

            if status is CustodyTxStatus.SUCCESS:
                assert executor_record is not None
                if self._promote_to_manual_review_on_kind_mismatch(
                    record, executor_record, evm_nonce, status
                ):
                    return
                if executor_record.tx_hash:
                    record.burn_tx_hash = executor_record.tx_hash
                record.state = SweepState.BURNED
                self._save_record(record)
                await self._credit_and_delete(record)
                return
            if status in {
                CustodyTxStatus.AWAITING_CLEAR,
                CustodyTxStatus.AWAITING_CLEAR_GAS_CAP,
            }:
                assert executor_record is not None
                if self._promote_to_manual_review_on_kind_mismatch(
                    record, executor_record, evm_nonce, status
                ):
                    return
                logger.critical(
                    "xROSE recovery: marking sweep record MANUAL_REVIEW "
                    "(deposit_id=%s status=%s executor_error=%r)",
                    record.deposit_id_hex,
                    status.value,
                    executor_record.error,
                )
                record.state = SweepState.MANUAL_REVIEW
                record.error = (
                    f"executor reported {status.value}; "
                    f"executor_error={executor_record.error or 'none'}"
                )
                self._save_record(record)
                return
            if status is CustodyTxStatus.WAITING_FOR_GAS_CAP:
                # Soft block: gas refill resolves without manual surgery on most
                # paths. Leave the sweep record at BURN_PENDING so the next pass
                # picks it up after the executor retries.
                logger.warning(
                    "xROSE recovery: executor record for %s waiting for gas cap "
                    "(deposit_id=%s) — leaving at BURN_PENDING",
                    record.deposit_address,
                    record.deposit_id_hex,
                )
                return

            # Preflight closures live in the executor's in-process dict and do
            # not survive a ROFL restart. Recovery refreshes the preflight once
            # per deposit per process; afterwards the executor owns the record
            # and re-enqueueing every recovery tick would just burn Sapphire
            # confidential reads.
            if record.deposit_id_hex in self._xrose_preflights_refreshed:
                logger.info(
                    "xROSE recovery: executor still working chain=%d nonce=%d status=%s",
                    BASE_SEPOLIA_CHAIN_ID,
                    evm_nonce,
                    status.value if status else "none",
                )
                return

            if not record.bridge_address:
                raise ValueError(
                    f"BURN_PENDING xROSE record for {record.deposit_address} is missing "
                    f"bridge_address — cannot refresh preflight"
                )
            await self._reserve_burn_and_enqueue(
                record=record,
                deposit_id=deposit_id,
                amount=record.amount,
                bridge_address=record.bridge_address,
            )
            self._xrose_preflights_refreshed.add(record.deposit_id_hex)
            return

        # Pre-SWEPT xROSE record must re-enter the bridge flow, not the generic
        # ERC20 sweep. The generic path targets the custody EOA and credits
        # without burning — that would break the burn-before-credit invariant
        # and let xROSE supply on Base diverge from credited ROSE on Sapphire.
        if not record.sweep_tx_hash:
            if not record.bridge_address or not record.token_address:
                raise ValueError(
                    f"xROSE bridge record for {record.deposit_address} is missing "
                    f"bridge_address or token_address — cannot resume safely"
                )
            await self.sweep_xrose_bridge(
                deposit_address=record.deposit_address,
                beneficiary=record.beneficiary,
                chain_type=record.chain_type,
                version=record.version,
                chain_id=record.chain_id,
                token_id=token_id,
                token_address=record.token_address,
                bridge_address=record.bridge_address,
                amount=record.amount,
                deposit_id=deposit_id,
            )
            return
        raise SweepRecoveryStuckError.from_record(record)

    async def _resume_standard_record(self, record: SweepRecord) -> None:
        """Recover one ``standard`` flow record by state."""
        if record.state == SweepState.SWEPT:
            await self._credit_and_delete(record)
            return

        if not record.sweep_tx_hash:
            await self._resume_sweep_from_pending(record)
            return

        raise SweepRecoveryStuckError.from_record(record)

    async def _resume_native_rose_record(self, record: SweepRecord) -> None:
        """Recover one ``native_rose_bridge_in`` flow record.

        Pre-SWEPT recovery must re-enter ``sweep_native_rose_bridge``, not
        ``_resume_sweep_from_pending`` — the latter routes to ``sweep_native``
        which is gas-tank funded and credits the gross verified amount, both
        of which break the native-bridge invariants (deposit pays own gas,
        credit at the net custody delta).
        """
        if record.state in {SweepState.BURN_PENDING, SweepState.BURNED}:
            raise ValueError(
                f"native_rose_bridge_in record {record.deposit_address} in unexpected "
                f"state {record.state.value}; native flow has no BURN_PENDING/BURNED states"
            )

        if record.state == SweepState.SWEPT:
            await self._credit_and_delete(record)
            return

        if not record.sweep_tx_hash:
            token_id = _from_hex(record.token_id_hex)
            deposit_id = _from_hex(record.deposit_id_hex)
            await self.sweep_native_rose_bridge(
                deposit_address=record.deposit_address,
                beneficiary=record.beneficiary,
                chain_type=record.chain_type,
                version=record.version,
                chain_id=record.chain_id,
                token_id=token_id,
                amount=record.amount,
                deposit_id=deposit_id,
            )
            return

        raise SweepRecoveryStuckError.from_record(record)

    async def _resume_one_record(self, record: SweepRecord) -> bool:
        """Reconcile + dispatch one record by ``flow_type``.

        The reconcile step is idempotent and only acts on pre-SWEPT records
        with a broadcast sweep tx, so it is safe to call before every dispatch.
        ``MANUAL_REVIEW`` records are operator-owned and never auto-advance.

        Returns ``True`` if a recovery handler ran, ``False`` if the record
        was skipped (MANUAL_REVIEW). Callers use this to distinguish a real
        recovery from an operator-owned skip in success counters and logs.
        """
        await self._reconcile_sweep_tx(record)

        if record.state == SweepState.MANUAL_REVIEW:
            logger.info(
                "Recovery: record %s in MANUAL_REVIEW (flow=%s deposit_id=%s) — "
                "leaving for operator",
                record.deposit_address,
                record.flow_type,
                record.deposit_id_hex,
            )
            return False

        if record.flow_type == FLOW_XROSE_BRIDGE_IN:
            await self._resume_xrose_bridge_record(record)
            return True
        if record.flow_type == FLOW_NATIVE_ROSE_BRIDGE_IN:
            await self._resume_native_rose_record(record)
            return True
        await self._resume_standard_record(record)
        return True

    async def resume_incomplete_sweeps(self) -> None:
        """Resume incomplete sweeps after ROFL restart.

        Called from lifespan startup. Dispatches by ``flow_type`` so each
        flow's invariants are pinned by the relevant handler — see
        ``_resume_one_record`` for the dispatch table.
        """
        records = self.load_incomplete_sweeps()
        if not records:
            return

        logger.info("Found %d incomplete sweep(s) to resume", len(records))

        succeeded = 0
        skipped = 0
        failed = 0
        for record in records:
            try:
                advanced = await self._resume_one_record(record)
                if advanced:
                    succeeded += 1
                    logger.info(
                        "Recovered sweep for %s on chain %d (flow=%s state=%s)",
                        record.deposit_address,
                        record.chain_id,
                        record.flow_type,
                        record.state.value,
                    )
                else:
                    skipped += 1
            except SweepCreditPendingError:
                # Record is SWEPT/BURN_PENDING on disk; next pass retries credit.
                succeeded += 1
                logger.warning(
                    "Resume: credit pending for %s on chain %d — recovery loop will retry",
                    record.deposit_address,
                    record.chain_id,
                )
            except SweepRecoveryStuckError as stuck:
                failed += 1
                logger.warning(
                    "Resume: stuck sweep in state %s with pending sweep_tx=%s for %s on "
                    "chain %d (flow=%s) — requires manual investigation",
                    stuck.state.value,
                    stuck.sweep_tx_hash,
                    stuck.deposit_address,
                    stuck.chain_id,
                    stuck.flow_type,
                )
            except Exception as exc:
                failed += 1
                self._persist_resume_error(record, exc)
                logger.exception(
                    "Failed to resume sweep for %s on chain %d",
                    record.deposit_address,
                    record.chain_id,
                )

        if failed:
            logger.critical(
                "Sweep recovery: %d/%d succeeded, %d failed, %d operator-owned — "
                "uncredited deposits need attention",
                succeeded,
                len(records),
                failed,
                skipped,
            )

    # ------------------------------------------------------------------
    # Periodic recovery loop — retries SWEPT records that failed credit
    # ------------------------------------------------------------------

    async def _run_recovery_loop(self) -> None:
        """Periodically retry credit/recovery for incomplete sweeps.

        Complements resume_incomplete_sweeps() (which runs once at startup)
        by retrying on a timer so a transient RPC failure during startup
        doesn't leave funds uncredited until the next full restart.
        """
        while self._recovery_running:
            await asyncio.sleep(SWEEP_RECOVERY_INTERVAL)
            if not self._recovery_running:
                break

            records = self.load_incomplete_sweeps()
            if not records:
                continue

            for record in records:
                try:
                    await self._resume_one_record(record)
                except SweepCreditPendingError:
                    logger.warning(
                        "Recovery loop: credit pending for %s on chain %d — next pass will retry",
                        record.deposit_address,
                        record.chain_id,
                    )
                except SweepRecoveryStuckError as stuck:
                    logger.warning(
                        "Recovery loop: stuck sweep in state %s with pending sweep_tx=%s "
                        "for %s on chain %d (flow=%s) — requires manual investigation",
                        stuck.state.value,
                        stuck.sweep_tx_hash,
                        stuck.deposit_address,
                        stuck.chain_id,
                        stuck.flow_type,
                    )
                except Exception as exc:
                    self._persist_resume_error(record, exc)
                    logger.exception(
                        "Recovery loop: resume failed for %s on chain %d — will retry in %ds",
                        record.deposit_address,
                        record.chain_id,
                        SWEEP_RECOVERY_INTERVAL,
                    )

    def start_recovery_loop(self) -> None:
        """Start the periodic SWEPT-record recovery task."""
        if getattr(self, "_recovery_task", None) is not None:
            return
        self._recovery_running = True
        self._recovery_task = asyncio.create_task(self._run_recovery_loop())
        logger.info("Sweep recovery loop started (interval=%ds)", SWEEP_RECOVERY_INTERVAL)

    async def stop_recovery_loop(self) -> None:
        """Stop the periodic recovery task gracefully."""
        self._recovery_running = False
        task = getattr(self, "_recovery_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._recovery_task = None
        logger.info("Sweep recovery loop stopped")

    async def xrose_bridge_in_in_flight(self) -> bool:
        """Return ``True`` if any xROSE bridge-in deposit is unresolved.

        Evidence sources (fail-closed on any failure):
        - Local ``SweepRecord``s — any xROSE record not in MANUAL_REVIEW is
          in flight; MANUAL_REVIEW means operator owns it.
        - On-chain ``BridgeBurnReserved`` events on Sapphire — for each
          reservation, the depositId is settled only if ``processedDeposits``
          is true on Sapphire AND ``burnedDepositIds`` is true on Base. Any
          credited-but-not-burned state is contradictory and fails closed —
          this check applies to every reservation including those whose local
          record is MANUAL_REVIEW, because Sapphire/Base divergence breaks the
          bridge invariant regardless of operator status.
        """
        # load_incomplete_sweeps() quarantines undecodable records by renaming
        # them to .corrupt. An xROSE record that corrupted before its
        # BridgeBurnReserved event landed on Sapphire would otherwise disappear
        # from both local enumeration and the on-chain reservation scan — the
        # predicate cannot prove the deposit is settled, so it must fail closed.
        try:
            corrupt_files = list(self._state_dir.glob("sweep_*.corrupt"))
        except OSError:
            logger.exception(
                "xrose_bridge_in_in_flight: state-dir glob failed in %s — fail closed",
                self._state_dir,
            )
            return True
        if corrupt_files:
            logger.critical(
                "xrose_bridge_in_in_flight: %d corrupt sweep record(s) present in %s — "
                "fail closed (cannot prove no xROSE deposit was lost to corruption): %s",
                len(corrupt_files),
                self._state_dir,
                [p.name for p in corrupt_files],
            )
            return True

        try:
            records = self.load_incomplete_sweeps()
        except OSError:
            logger.exception(
                "xrose_bridge_in_in_flight: load_incomplete_sweeps failed — fail closed"
            )
            return True

        manual_review_ids: Set[str] = set()
        for record in records:
            if record.flow_type != FLOW_XROSE_BRIDGE_IN:
                continue
            if record.state == SweepState.MANUAL_REVIEW:
                manual_review_ids.add(record.deposit_id_hex.lower().removeprefix("0x"))
                continue
            logger.info(
                "xrose_bridge_in_in_flight: local record %s state=%s — in flight",
                record.deposit_id_hex,
                record.state.value,
            )
            return True

        try:
            reservations = await self._accounting.list_bridge_burn_reservations(
                chain_id=BASE_SEPOLIA_CHAIN_ID
            )
        except Exception:
            logger.exception(
                "xrose_bridge_in_in_flight: BridgeBurnReserved scan failed — fail closed"
            )
            return True

        for res in reservations:
            deposit_id_hex = res.deposit_id.hex().lower()
            try:
                # Sapphire processedDeposits and Base burnedDepositIds are
                # independent reads; gather halves wall-clock per reservation.
                credited, burned = await asyncio.gather(
                    self._accounting.is_deposit_processed(res.deposit_id),
                    self._read_rofl_bridge_burned(res.chain_id, res.bridge, res.deposit_id),
                )
            except Exception:
                logger.exception(
                    "xrose_bridge_in_in_flight: chain read failed for %s — fail closed",
                    deposit_id_hex,
                )
                return True

            if credited and not burned:
                # Contradiction is fail-closed even for MANUAL_REVIEW deposits —
                # divergence between Sapphire credit and Base burn is a hard bridge
                # invariant break that operator action cannot retroactively make safe.
                logger.critical(
                    "xrose_bridge_in_in_flight: CONTRADICTION — depositId=%s credited on "
                    "Sapphire but not burned on Base; fail closed",
                    deposit_id_hex,
                )
                return True
            if deposit_id_hex in manual_review_ids:
                continue
            if credited and burned:
                continue
            logger.info(
                "xrose_bridge_in_in_flight: reserved depositId=%s credited=%s burned=%s — "
                "in flight",
                deposit_id_hex,
                credited,
                burned,
            )
            return True

        return False

    async def reconstruct_xrose_deposit_state(
        self,
        deposit_id: bytes,
        *,
        rofl_bridge_address: Optional[str] = None,
    ) -> Reconstruction:
        """Reconstruct one xROSE deposit's state from on-chain evidence.

        Read-only; never mutates a sweep record.

        Reads Sapphire signals (reservation event + ``processedDeposits``)
        before Base signals (``burnedDepositIds`` view + ``Burned`` event):
        a credit on Sapphire implies a prior burn on Base, so any burn
        observable when credited is True must also be observable in a
        later Base read of the same chain. Both Base reads are pinned to
        one ``block_identifier`` so a burn landing mid-call cannot create
        a phantom contradiction.

        When a reservation is present, all Base reads use ``reservation.bridge``
        (the per-deposit binding) so the verdict survives a bridge-address
        rotation. ``rofl_bridge_address`` is the fallback when no reservation
        has been observed yet.

        Args:
            deposit_id: 32-byte canonical deposit identifier.
            rofl_bridge_address: ROFLBridge address on Base. Ignored when a
                reservation is present.

        Raises:
            ValueError: ``deposit_id`` is not 32 bytes, or no bridge address
                is available (no reservation AND ``rofl_bridge_address=None``).
            ReconstructionEvidenceError: evidence sources contradict each
                other, more than one ``Burned`` event is observed for the
                same depositId, or the local state directory contains
                quarantined ``.corrupt`` sweep records that may hide an
                xROSE deposit. The caller must fail closed.
        """
        if len(deposit_id) != 32:
            raise ValueError("deposit_id must be 32 bytes")

        try:
            corrupt_files = list(self._state_dir.glob("sweep_*.corrupt"))
        except OSError as exc:
            raise ReconstructionEvidenceError(deposit_id, f"state-dir glob failed: {exc}") from exc
        if corrupt_files:
            raise ReconstructionEvidenceError(
                deposit_id,
                f"{len(corrupt_files)} corrupt sweep record(s) in state dir "
                f"may hide an xROSE deposit: {[p.name for p in corrupt_files]}",
            )

        try:
            reservations = await self._accounting.list_bridge_burn_reservations(
                chain_id=BASE_SEPOLIA_CHAIN_ID
            )
        except Exception:
            logger.exception(
                "reconstruct: BridgeBurnReserved scan failed for depositId=%s",
                deposit_id.hex(),
            )
            raise
        reservation: Optional[BridgeBurnReservation] = next(
            (r for r in reversed(reservations) if r.deposit_id == deposit_id), None
        )

        if reservation is not None:
            bridge_address = reservation.bridge
        elif rofl_bridge_address is not None:
            bridge_address = rofl_bridge_address
        else:
            raise ValueError(
                "no on-chain reservation found and rofl_bridge_address not "
                "supplied; cannot read Base bridge views"
            )

        try:
            credited = await self._accounting.is_deposit_processed(deposit_id)
        except Exception:
            logger.exception(
                "reconstruct: processedDeposits read failed for depositId=%s",
                deposit_id.hex(),
            )
            raise

        w3_base = self._get_web3(BASE_SEPOLIA_CHAIN_ID)
        try:
            pinned_block = await w3_base.eth.block_number
        except Exception:
            logger.exception(
                "reconstruct: Base block_number read failed for depositId=%s",
                deposit_id.hex(),
            )
            raise

        try:
            burn_view, burn_amount = await asyncio.gather(
                self._read_rofl_bridge_burned(
                    BASE_SEPOLIA_CHAIN_ID,
                    bridge_address,
                    deposit_id,
                    block_identifier=pinned_block,
                ),
                self._read_rofl_bridge_burned_event(
                    BASE_SEPOLIA_CHAIN_ID,
                    bridge_address,
                    deposit_id,
                    to_block=pinned_block,
                ),
            )
        except Exception:
            logger.exception(
                "reconstruct: Base read failed for depositId=%s at block %s",
                deposit_id.hex(),
                pinned_block,
            )
            raise

        if credited and not burn_view:
            raise ReconstructionEvidenceError(
                deposit_id,
                "credited on Sapphire but burnedDepositIds=False on Base",
            )
        if credited and reservation is None:
            raise ReconstructionEvidenceError(
                deposit_id,
                "credited on Sapphire but no BridgeBurnReserved event observed",
            )
        if burn_view and burn_amount is None:
            raise ReconstructionEvidenceError(
                deposit_id,
                "burnedDepositIds=True but no Burned event observable",
            )
        if not burn_view and burn_amount is not None:
            raise ReconstructionEvidenceError(
                deposit_id,
                "Burned event present but burnedDepositIds=False",
            )
        if (
            reservation is not None
            and burn_amount is not None
            and reservation.amount != burn_amount
        ):
            raise ReconstructionEvidenceError(
                deposit_id,
                f"reservation amount {reservation.amount} != Burned event amount {burn_amount}",
            )

        if credited:
            kind = ReconstructionKind.CREDITED
        elif burn_view:
            kind = ReconstructionKind.BURNED
        elif reservation is not None:
            kind = ReconstructionKind.BURN_RESERVED_NOT_MINED
        elif self._has_active_xrose_local_record(deposit_id):
            kind = ReconstructionKind.SWEPT_ONLY
        else:
            kind = ReconstructionKind.UNKNOWN

        return Reconstruction(
            deposit_id=deposit_id,
            kind=kind,
            reservation=reservation,
            burn_amount=burn_amount,
            credited=credited,
            burn_view=burn_view,
        )

    def _has_active_xrose_local_record(self, deposit_id: bytes) -> bool:
        """Return True if local state shows an xROSE record for ``deposit_id``
        in a pre-burn-reserved active state (PENDING/GAS_FUNDED/SWEPT).

        ``MANUAL_REVIEW`` and burn-side states (``BURN_PENDING``, ``BURNED``)
        intentionally do not count: reconstruction reports what the chain
        shows, not what the operator is already handling locally.
        """
        target_hex = deposit_id.hex().lower()
        active_states = {SweepState.PENDING, SweepState.GAS_FUNDED, SweepState.SWEPT}
        for record in self.load_incomplete_sweeps():
            if record.flow_type != FLOW_XROSE_BRIDGE_IN:
                continue
            if record.state not in active_states:
                continue
            if record.deposit_id_hex.lower().removeprefix("0x") == target_hex:
                return True
        return False

    def has_any_active_xrose_bridge_in_flow(self) -> tuple[bool, list[str]]:
        """Return (has_any, deposit_id_hexes) for non-terminal xrose_bridge_in records.

        Broader than ``_has_active_xrose_local_record``: this predicate gates
        route rotation, so it also blocks on ``BURN_PENDING`` and
        ``MANUAL_REVIEW`` — both still need the current route address to
        stay constant until the record drains to ``BURNED`` and credited.
        """
        active_states = {
            SweepState.PENDING,
            SweepState.GAS_FUNDED,
            SweepState.SWEPT,
            SweepState.BURN_PENDING,
            SweepState.MANUAL_REVIEW,
        }
        deposit_ids: list[str] = []
        for record in self.load_incomplete_sweeps():
            if record.flow_type != FLOW_XROSE_BRIDGE_IN:
                continue
            if record.state not in active_states:
                continue
            if record.deposit_id_hex:
                deposit_ids.append(record.deposit_id_hex.lower().removeprefix("0x"))
        return (bool(deposit_ids), deposit_ids)

    async def _wait_for_receipt(self, w3: AsyncWeb3, tx_hash, timeout: int = 120):
        """Poll for transaction receipt with timeout."""
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            try:
                receipt = await w3.eth.get_transaction_receipt(tx_hash)
                if receipt is not None:
                    return receipt
            except (ConnectionError, TimeoutError, OSError, ValueError, TransactionNotFound) as exc:
                last_error = exc
                logger.warning("RPC error polling receipt for %s: %s", tx_hash, exc)
            await asyncio.sleep(2)
        msg = f"Transaction {tx_hash} not mined within {timeout}s"
        if last_error:
            msg += f" (last RPC error: {last_error})"
        raise TimeoutError(msg)
