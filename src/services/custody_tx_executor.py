"""Shared durable executor for custody-EOA-signed transactions.

Owns the on-chain nonce sequence for `Accounting.evmAddress()` on Sapphire
testnet (23295) and Base Sepolia (84532). Every outbound tx signed by the
custody EOA — normal withdrawals, Sapphire native releases, Base xROSE
mints, and Base xROSE burns — flows through one queue per chain.

Invariants:
- Next executable `evm_nonce` is computed from the queue, not from
  `eth_getTransactionCount()`. RPC is consulted only for reconciliation
  of orphaned records, never for live nonce allocation.
- A request is persisted at `QUEUED` *before* `send_raw_transaction` is
  awaited; the `tx_hash` is persisted *after* it returns and *before* the
  receipt poll begins.
- `AWAITING_CLEAR` and `AWAITING_CLEAR_GAS_CAP` are operator-only-clear
  hard blocks: no later nonce on the same chain advances past one.
- `QUEUED` and `WAITING_FOR_GAS_CAP` are runnable: the kind-routed
  preflight re-runs on every loop iteration. Downstream nonces still
  cannot advance past a stuck record (the loop returns at it without
  visiting nonce+1), but recovery from a transient pause / gas spike
  needs no operator action.
- Success is only marked after a receipt with `status == 1`.

Records live as one JSON file per `(chain_id, evm_nonce)` under
``CUSTODY_TX_STATE_DIR``. Atomic writes via tmp-then-rename so a crash
mid-write leaves the previous record intact.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

from eth_abi import encode as abi_encode
from eth_utils import function_signature_to_4byte_selector, keccak
from hexbytes import HexBytes
from web3 import AsyncWeb3, Web3
from web3.exceptions import (
    BadFunctionCallOutput,
    BadResponseFormat,
    ContractLogicError,
    TimeExhausted,
    TransactionNotFound,
    Web3RPCError,
    Web3ValidationError,
)

try:
    import aiohttp
except ImportError:  # pragma: no cover - aiohttp is a hard dep via web3 AsyncHTTPProvider
    aiohttp = None

from src.abi.rofl_bridge import ROFL_BRIDGE_ABI
from src.config import CHAIN_NAMES
from src.config.bridge_validation import destination_chain_ids
from src.services.custody_tx_clear_events import ClearAction, CustodyTxClearWatcher

logger = logging.getLogger(__name__)


DEFAULT_STATE_DIR = os.getenv("CUSTODY_TX_STATE_DIR", "/data/custody-tx-executor")
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_RECEIPT_TIMEOUT_SECONDS = 120
# Probe the on-chain nonce (and re-broadcast) every Nth receipt-poll pass; cheap
# passes in between just persist the attempt counter. See _handle_receipt_retry.
DEFAULT_RECEIPT_PROBE_INTERVAL = 5
# Wall-clock deadline: an unmined tx whose nonce never advances past this many
# seconds escalates to AWAITING_CLEAR so a dropped/under-priced tx can't stall
# the chain loop forever.
DEFAULT_RECEIPT_STUCK_DEADLINE_SECONDS = 6 * 60 * 60
EXECUTOR_CHAIN_IDS = (23295, 84532)

# Chain-loop circuit-breaker tuning. The CRITICAL one-shot fires when a chain
# has failed every pass for ~30 seconds of wall time so operators get a single
# clear alert instead of a stream of per-pass exception traces.
_CHAIN_LOOP_CRITICAL_THRESHOLD = 30
_CHAIN_LOOP_MAX_BACKOFF_SECONDS = 60.0

# Re-inspect BLOCKING_STATUSES records every Nth pass; cap how many freshly
# unblocked records one chain drains in the same tick so it can't monopolise the
# cooperative scheduler. See _self_heal_blocked / _run_self_heal_pass.
_SELF_HEAL_INTERVAL_PASSES = 60
_SAME_TICK_PROMOTION_BUDGET = 8

# 4-byte error selectors for the reverts the Base-mint preflight classifies as
# RETRY_LATER. Matched against the raw revert data on
# ``ContractLogicError.data`` so a future upstream wording change to the human
# message doesn't silently re-route these to AWAITING_CLEAR.
_ENFORCED_PAUSE_SELECTOR = HexBytes(function_signature_to_4byte_selector("EnforcedPause()"))
_XRC20_NOT_HIGH_ENOUGH_LIMITS_SELECTOR = HexBytes(
    function_signature_to_4byte_selector("IXERC20_NotHighEnoughLimits()")
)
_ALREADY_PROCESSED_SELECTOR = HexBytes(function_signature_to_4byte_selector("AlreadyProcessed()"))
_GAS_BUDGET_EXCEEDED_SELECTOR = HexBytes(
    function_signature_to_4byte_selector("GasBudgetExceeded()")
)


class CustodyTxStartupError(RuntimeError):
    """Raised when the executor cannot safely start (e.g. insufficient gas)."""


class CorruptCustodyTxRecordError(RuntimeError):
    """Raised when a persisted record fails to decode.

    Refusing to silently drop a corrupt record preserves the durability
    guarantee for blocking statuses: a damaged ``AWAITING_CLEAR`` file must
    not look like an empty slot, or the next ``enqueue()`` would overwrite
    an operator-imposed block.
    """


class CustodyTxKind(str, Enum):
    BASE_MINT = "base_mint"
    XROSE_BURN = "xrose_burn"
    SAPPHIRE_RELEASE = "sapphire_release"
    NORMAL_WITHDRAWAL = "normal_withdrawal"


class CustodyTxStatus(str, Enum):
    QUEUED = "queued"
    BROADCAST = "broadcast"
    SUCCESS = "success"
    # On-chain tx at this nonce reverted; nonce is burned. Preserved for
    # forensics but non-blocking so the chain loop can advance past it.
    FAILED_FINAL = "failed_final"
    AWAITING_CLEAR = "awaiting_clear"
    WAITING_FOR_GAS_CAP = "waiting_for_gas_cap"
    AWAITING_CLEAR_GAS_CAP = "awaiting_clear_gas_cap"
    # A reserved-but-un-mineable nonce being burned via an owner-authorized
    # value-0 self-transfer (signed by signNonceBurn). Non-blocking but NOT
    # runnable: the chain loop advances once the burn mines, and must never
    # re-broadcast the original stuck tx. Dispatched like BROADCAST.
    BURNING_NONCE = "burning_nonce"


TERMINAL_STATUSES = frozenset(
    {
        CustodyTxStatus.SUCCESS,
        CustodyTxStatus.FAILED_FINAL,
        CustodyTxStatus.AWAITING_CLEAR,
        CustodyTxStatus.AWAITING_CLEAR_GAS_CAP,
    }
)
# Operator-only-clear: chain loop halts at any record in one of these.
BLOCKING_STATUSES = frozenset(
    {
        CustodyTxStatus.AWAITING_CLEAR,
        CustodyTxStatus.AWAITING_CLEAR_GAS_CAP,
    }
)
# Preflight re-runs on every loop iteration for records in these statuses.
# Downstream nonces still cannot advance past a stuck record because the
# loop returns at it without visiting the next one.
RUNNABLE_STATUSES = frozenset(
    {
        CustodyTxStatus.QUEUED,
        CustodyTxStatus.WAITING_FOR_GAS_CAP,
    }
)


class PreflightOutcome(str, Enum):
    ALLOW = "allow"
    RETRY_LATER = "retry_later"
    AWAITING_CLEAR = "awaiting_clear"
    MARK_RECOVERED = "mark_recovered"


@dataclass
class PreflightDecision:
    """Result of a kind-routed preflight check.

    RETRY_LATER stays runnable; ``target_status`` controls the user-visible
    label (QUEUED for transient external conditions like ROFLBridge.paused,
    WAITING_FOR_GAS_CAP for Sapphire gas-cap blocks).
    AWAITING_CLEAR is terminal; the chain loop halts.
    ALLOW proceeds to ``send_raw_transaction``; ``fresh_signed_tx`` lets
    SAPPHIRE_RELEASE swap in a freshly-signed tx that the executor
    regenerates per-attempt with live ``(gas_limit, gas_price)``.
    MARK_RECOVERED carries the originating tx_hash + block_number from the
    matched on-chain event so the record can be promoted to SUCCESS with
    full forensic data instead of synthesising values.
    """

    outcome: PreflightOutcome
    error: Optional[str] = None
    target_status: Optional[CustodyTxStatus] = None
    fresh_signed_tx: Optional[bytes] = None
    recovered_tx_hash: Optional[str] = None
    recovered_block_number: Optional[int] = None


@dataclass
class CustodyTxRequest:
    """Caller-facing request shape — fields the executor needs to enqueue a tx.

    Bridge-only fields (route_address, max_gas_cost, withdrawal_index,
    to_address, amount) carry the inputs the kind-routed preflight needs to
    reconstruct policy gates after a restart. Non-bridge kinds leave them
    None and the dispatcher returns ALLOW.

    signed_tx defaults to b"" for SAPPHIRE_RELEASE records: the executor
    re-signs per broadcast attempt via `accounting.resolve_bridge_withdrawal`.
    """

    chain_id: int
    evm_nonce: int
    kind: CustodyTxKind
    id: str
    signed_tx: bytes = b""
    route_address: Optional[str] = None
    max_gas_cost: Optional[int] = None
    withdrawal_index: Optional[int] = None
    to_address: Optional[str] = None
    amount: Optional[int] = None


@dataclass
class CustodyTxRecord:
    """Persisted state for one in-flight custody-EOA tx.

    `accounting_contract_address`, `evm_sender`, `evm_nonce`, `kind`, and
    `id` together let an operator identify which on-chain contract
    reservation a blocked record corresponds to without needing the
    in-memory queue state.
    """

    chain_id: int
    accounting_contract_address: str
    evm_sender: str
    evm_nonce: int
    kind: CustodyTxKind
    id: str
    signed_tx_hex: str
    tx_hash: Optional[str] = None
    status: CustodyTxStatus = CustodyTxStatus.QUEUED
    receipt_block_number: Optional[int] = None
    receipt_status: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    retry_count: int = 0
    # Set on the first receipt miss, cleared on any nonce advance; drives the
    # wall-clock terminal deadline in the receipt-poll liveness loop. Also reset
    # to the broadcast time when a nonce-burn starts, to bound the burn poll.
    stuck_since: Optional[float] = None
    # Set when a BURNING_NONCE record broadcasts its owner-authorized nonce-burn
    # self-transfer. Polled (NOT signed_tx_hex) to drive the burn to FAILED_FINAL.
    burn_nonce_tx_hash: Optional[str] = None
    error: Optional[str] = None
    # Bridge-record preflight inputs (None for non-bridge kinds).
    route_address: Optional[str] = None
    max_gas_cost: Optional[int] = None
    withdrawal_index: Optional[int] = None
    to_address: Optional[str] = None
    amount: Optional[int] = None
    # Populated by `_apply_receipt`; surplus_delta is SAPPHIRE_RELEASE only.
    gas_used: Optional[int] = None
    effective_gas_price: Optional[int] = None
    surplus_delta: Optional[int] = None
    # Every attempt's `keccak(signed_tx)` is appended before `send_raw`.
    # Reconciliation iterates this list so SAPPHIRE_RELEASE fresh-signs don't
    # orphan a prior broadcast hash on restart.
    broadcast_hashes: list[str] = field(default_factory=list)
    # Set only when duplicate-id recovery promotes the record to SUCCESS via
    # an on-chain event match. Distinguishes "our broadcast mined" (None)
    # from "we recognised a prior incarnation's mined id" (populated).
    recovered_tx_hash: Optional[str] = None
    recovered_block_number: Optional[int] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["status"] = self.status.value
        return d

    @staticmethod
    def _migrate_record_data(data: dict) -> dict:
        migrated = dict(data)
        migrated.setdefault("tx_hash", None)
        migrated.setdefault("receipt_block_number", None)
        migrated.setdefault("receipt_status", None)
        migrated.setdefault("error", None)
        migrated.setdefault("retry_count", 0)
        migrated.setdefault("stuck_since", None)
        migrated.setdefault("burn_nonce_tx_hash", None)
        migrated.setdefault("route_address", None)
        migrated.setdefault("max_gas_cost", None)
        migrated.setdefault("withdrawal_index", None)
        migrated.setdefault("to_address", None)
        migrated.setdefault("amount", None)
        migrated.setdefault("gas_used", None)
        migrated.setdefault("effective_gas_price", None)
        migrated.setdefault("surplus_delta", None)
        migrated.setdefault("broadcast_hashes", [])
        migrated.setdefault("recovered_tx_hash", None)
        migrated.setdefault("recovered_block_number", None)
        # Legacy records persisted the pre-rename status strings; translate
        # them before the enum coercion in from_dict (a removed enum value
        # would raise ValueError on load).
        status_value = migrated.get("status")
        if status_value == "manual_review":
            migrated["status"] = "awaiting_clear"
        elif status_value == "manual_review_gas_cap":
            migrated["status"] = "awaiting_clear_gas_cap"
        return migrated

    @classmethod
    def from_dict(cls, data: dict) -> "CustodyTxRecord":
        data = cls._migrate_record_data(data)
        data["kind"] = CustodyTxKind(data["kind"])
        data["status"] = CustodyTxStatus(data["status"])
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class CustodyTxAccountingProtocol(Protocol):
    """Interface needed from AccountingContractService.

    Spelled out so the executor can be unit-tested with a small mock without
    pulling in the full Accounting wire-up.
    """

    async def _get_chain_web3(self, chain_id: int) -> AsyncWeb3: ...

    async def get_custody_address(self) -> str: ...

    def _get_reader_contract(self) -> Any: ...

    async def resolve_bridge_withdrawal(self, index: int) -> bytes: ...

    async def generate_bridge_burn_transfer(self, deposit_id: bytes) -> bytes: ...

    async def sign_nonce_burn(self, chain_id: int, nonce: int) -> bytes: ...

    async def get_clear_applied_hash(self, chain_id: int, nonce: int) -> bytes: ...

    @property
    def contract_address(self) -> str: ...

    @property
    def settings(self) -> Any: ...


def _to_hex(value: Any) -> str:
    return HexBytes(value).to_0x_hex()


def _signed_tx_to_hex(signed_tx: Any) -> str:
    return HexBytes(signed_tx).to_0x_hex()


def _hex_to_bytes(hex_str: str) -> bytes:
    return bytes(HexBytes(hex_str))


def _classify_rofl_bridge_revert(
    exc: ContractLogicError,
    record: "CustodyTxRecord",
    *,
    kind_label: str,
) -> "PreflightDecision":
    """Map a ROFLBridge eth_call revert to a preflight decision.

    Paused / limit-exhausted reverts auto-retry on the next loop iteration
    so a transient bridge pause or a momentarily exhausted mint/burn limit
    self-recovers without an operator. Any other revert is operator-only
    territory. Unrecognised provider shapes log a warning before failing
    closed so the new shape can be added to ``_extract_revert_selector``.
    """
    selector = _extract_revert_selector(exc)
    if selector == _ENFORCED_PAUSE_SELECTOR:
        return PreflightDecision(
            outcome=PreflightOutcome.RETRY_LATER,
            target_status=CustodyTxStatus.QUEUED,
            error="ROFLBridge paused",
        )
    if selector == _XRC20_NOT_HIGH_ENOUGH_LIMITS_SELECTOR:
        verb = "mint" if "mint" in kind_label else "burn"
        return PreflightDecision(
            outcome=PreflightOutcome.RETRY_LATER,
            target_status=CustodyTxStatus.QUEUED,
            error=f"{verb} limit exhausted",
        )
    if selector is None:
        logger.warning(
            "%s preflight: revert selector unextractable from data=%r "
            "(chain=%s nonce=%s) — failing closed",
            kind_label,
            getattr(exc, "data", None),
            record.chain_id,
            record.evm_nonce,
        )
    return PreflightDecision(
        outcome=PreflightOutcome.AWAITING_CLEAR,
        error=f"{kind_label} preflight reverted: {exc}",
    )


def _extract_revert_selector(exc: ContractLogicError) -> Optional[HexBytes]:
    """Return the 4-byte error selector from a revert, or ``None`` if absent.

    Web3.py surfaces the raw revert data on ``ContractLogicError.data`` in
    one of three shapes depending on the RPC provider: a hex string
    (``"0x..."``), a dict that nests the hex string under ``data``/``error``
    (Anvil, some Geth forks, gateway proxies), or ``None`` when the node
    only set ``message``. The dict shape comes from web3.py's
    ``raise_contract_logic_error_on_revert``: when an RPC ``error.data``
    field is itself a dict and a message is present, the whole dict is
    forwarded to ``ContractLogicError.data``.
    """
    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        for key in ("data", "error", "result"):
            inner = data.get(key)
            if isinstance(inner, str):
                data = inner
                break
            if isinstance(inner, dict):
                deeper = inner.get("data") or inner.get("error")
                if isinstance(deeper, str):
                    data = deeper
                    break
        else:
            return None
    if not isinstance(data, str) or not data.startswith("0x") or len(data) < 10:
        return None
    try:
        return HexBytes(data[:10])
    except Exception:
        return None


# Substrings (lower-cased) and JSON-RPC error codes that mark a server-side /
# rate-limit / timeout condition worth retrying rather than escalating.
_TRANSIENT_RPC_MESSAGE_HINTS = (
    "timeout",
    "timed out",
    "rate limit",
    "too many requests",
    "429",
    "502",
    "503",
    "504",
    "try again",
    "temporarily",
)
_TRANSIENT_RPC_CODES = frozenset({-32005, -32016, -32603})
# Deterministic-revert JSON-RPC codes: a real "execution reverted", never retry.
_REVERT_RPC_CODES = frozenset({3, -32015})


def is_transient_rpc_error(exc: BaseException) -> bool:
    """Classify an exception as a retryable transport/server blip (True) or a
    deterministic failure that must escalate (False). Fails closed — anything
    not positively recognised as transient returns False.

    web3.py 7.x already auto-retries idempotent calls (including
    ``eth_sendRawTransaction``) on transport ``aiohttp.ClientError`` /
    ``TimeoutError`` before re-raising, so callers only ever see the
    post-retry exception here — do NOT stack a large retry budget on top.

    The executor talks to nodes over ``AsyncHTTPProvider`` (aiohttp), so there
    is no ``requests.*`` branch.
    """
    # Deterministic reverts / validation errors are never transient.
    # ContractCustomError / ContractPanicError subclass ContractLogicError.
    if isinstance(exc, (ContractLogicError, Web3ValidationError, BadFunctionCallOutput)):
        return False

    if isinstance(exc, Web3RPCError):
        rpc_response = getattr(exc, "rpc_response", None)
        if isinstance(rpc_response, dict):
            err = rpc_response.get("error") or {}
            code = err.get("code")
            message = str(err.get("message") or "").lower()
            data = err.get("data")
            # A real revert: human "execution reverted", revert code, or
            # selector-bearing data. Never retry these.
            if (
                "execution reverted" in message
                or "revert" in message
                or code in _REVERT_RPC_CODES
                or (isinstance(data, str) and data.startswith("0x") and len(data) >= 10)
            ):
                return False
            if (
                any(hint in message for hint in _TRANSIENT_RPC_MESSAGE_HINTS)
                or code in _TRANSIENT_RPC_CODES
            ):
                return True
        # Unknown Web3RPCError shape — fall through to type checks / fail closed.

    if isinstance(exc, (TransactionNotFound, TimeExhausted, BadResponseFormat)):
        return True
    if isinstance(exc, asyncio.TimeoutError):
        return True
    if aiohttp is not None:
        if isinstance(exc, (aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError)):
            return True
        if isinstance(exc, aiohttp.ClientResponseError) and getattr(exc, "status", None) in {
            429,
            500,
            502,
            503,
            504,
        }:
            return True

    return False


def _rpc_failure_decision(exc: Exception, error: str) -> "PreflightDecision":
    """Map an RPC-call exception to a preflight decision: transient blips stay
    runnable (RETRY_LATER/QUEUED), everything else is operator-only AWAITING_CLEAR."""
    if is_transient_rpc_error(exc):
        return PreflightDecision(
            outcome=PreflightOutcome.RETRY_LATER,
            target_status=CustodyTxStatus.QUEUED,
            error=error,
        )
    return PreflightDecision(outcome=PreflightOutcome.AWAITING_CLEAR, error=error)


class CustodyTxExecutor:
    """Durable per-chain executor for custody-EOA-signed transactions."""

    def __init__(
        self,
        accounting_service: CustodyTxAccountingProtocol,
        state_dir: str = DEFAULT_STATE_DIR,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        receipt_timeout_seconds: int = DEFAULT_RECEIPT_TIMEOUT_SECONDS,
        receipt_probe_interval: int = DEFAULT_RECEIPT_PROBE_INTERVAL,
        receipt_stuck_deadline_seconds: float = DEFAULT_RECEIPT_STUCK_DEADLINE_SECONDS,
        self_heal_interval_passes: int = _SELF_HEAL_INTERVAL_PASSES,
        same_tick_promotion_budget: int = _SAME_TICK_PROMOTION_BUDGET,
        chain_ids: tuple[int, ...] = EXECUTOR_CHAIN_IDS,
    ) -> None:
        self._accounting = accounting_service
        self._state_dir = Path(state_dir)
        # 0o700 is owner-only: directory contains files with raw signed txs
        # replayable by any reader, so world/group access would defeat the
        # purpose. exist_ok=True can silently accept a pre-existing dir with
        # looser perms; the explicit chmod below tightens unconditionally.
        self._state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)  # nosemgrep
        try:
            os.chmod(self._state_dir, 0o700)  # nosemgrep
        except OSError:
            logger.warning("Custody-tx state dir %s: could not chmod to 0o700", self._state_dir)
        self._poll_interval = poll_interval_seconds
        self._receipt_timeout = receipt_timeout_seconds
        self._receipt_probe_interval = receipt_probe_interval
        self._receipt_stuck_deadline = receipt_stuck_deadline_seconds
        self._self_heal_interval_passes = self_heal_interval_passes
        self._same_tick_promotion_budget = same_tick_promotion_budget
        self._chain_ids = tuple(chain_ids)

        self._running = False
        self._chain_tasks: Dict[int, asyncio.Task] = {}
        # Signalled when a record reaches a terminal state; not load-bearing
        # for production correctness.
        self._resolution_events: Dict[str, asyncio.Event] = {}
        # web3.py rebinds every function in the ABI on each `contract(...)`
        # call (~760µs for ROFL_BRIDGE_ABI). Preflights re-run per tick over
        # an immutable (chain, route) pair, so cache the bound contract.
        self._bridge_contract_cache: Dict[tuple[int, str], Any] = {}
        # Per-(chain_id, evm_nonce) lock guarding load→mutate→save for an
        # owner-authorized clear, closing the TOCTOU against a concurrent
        # self-heal promotion.
        self._clear_locks: Dict[str, asyncio.Lock] = {}
        # Watches Sapphire for owner clear signals; started by start().
        self._clear_watcher: Optional[CustodyTxClearWatcher] = None
        # Last-emitted {status: count} snapshot per chain so the blocking-count
        # metric only fires when the picture changes (not every 1 Hz tick).
        self._blocking_snapshot: Dict[int, Dict[str, int]] = {}

    @staticmethod
    def _record_key(chain_id: int, evm_nonce: int) -> str:
        return f"{chain_id}_{evm_nonce}"

    def _record_path(self, chain_id: int, evm_nonce: int) -> Path:
        return self._state_dir / f"custody_tx_{chain_id}_{evm_nonce}.json"

    def _clear_lock(self, chain_id: int, nonce: int) -> asyncio.Lock:
        return self._clear_locks.setdefault(self._record_key(chain_id, nonce), asyncio.Lock())

    @staticmethod
    def _emit_metric(name: str, **labels: Any) -> None:
        suffix = " ".join(f"{k}={v}" for k, v in labels.items())
        logger.info("custody_tx_metric name=%s %s", name, suffix)

    async def enqueue(self, request: CustodyTxRequest) -> str:
        """Persist a request as QUEUED and return its record key.

        Persistence happens BEFORE this returns — the caller can rely on the
        record surviving a crash immediately on return. Idempotent at the
        ``(chain_id, evm_nonce)`` key: a second enqueue for the same slot
        returns the existing record's key and never overwrites disk state.
        """
        if request.chain_id not in self._chain_ids:
            raise ValueError(
                f"Chain {request.chain_id} is not managed by this executor "
                f"(managed: {sorted(self._chain_ids)})"
            )

        key = self._record_key(request.chain_id, request.evm_nonce)
        existing = self._load_record(request.chain_id, request.evm_nonce)
        if existing is not None:
            # Two callers raced for the same nonce, or a catch-up record
            # arrived before the live enqueue. Honor the existing on-disk
            # record without overwriting state.
            self._resolution_events.setdefault(key, asyncio.Event())
            logger.info(
                "Custody tx enqueue idempotent: chain=%d nonce=%d existing status=%s",
                request.chain_id,
                request.evm_nonce,
                existing.status.value,
            )
            return key

        custody_address = await self._accounting.get_custody_address()
        contract_address = str(self._accounting.contract_address)

        record = CustodyTxRecord(
            chain_id=request.chain_id,
            accounting_contract_address=contract_address,
            evm_sender=custody_address,
            evm_nonce=request.evm_nonce,
            kind=request.kind,
            id=request.id,
            signed_tx_hex=_signed_tx_to_hex(request.signed_tx),
            route_address=request.route_address,
            max_gas_cost=request.max_gas_cost,
            withdrawal_index=request.withdrawal_index,
            to_address=request.to_address,
            amount=request.amount,
        )
        self._save_record(record)
        self._resolution_events.setdefault(key, asyncio.Event())

        logger.info(
            "Custody tx enqueued: chain=%d nonce=%d kind=%s id=%s",
            record.chain_id,
            record.evm_nonce,
            record.kind.value,
            record.id,
        )
        return key

    async def verify_startup_gas_balances(self) -> None:
        """Fail loud if the custody EOA cannot fund pending txs on a managed chain.

        Threshold is the existing `MIN_WITHDRAWAL_GAS_BALANCE` env (settings
        attribute `min_withdrawal_gas_balance`); same value applied to ETH on
        Base Sepolia and ROSE on Sapphire.
        """
        custody_address = await self._accounting.get_custody_address()
        threshold = int(self._accounting.settings.min_withdrawal_gas_balance)
        problems: List[str] = []
        for chain_id in self._chain_ids:
            try:
                w3 = await self._accounting._get_chain_web3(chain_id)
                balance = await w3.eth.get_balance(custody_address)
            except Exception as exc:
                problems.append(
                    f"chain {chain_id} ({CHAIN_NAMES.get(chain_id, '?')}): "
                    f"balance lookup failed — {exc}"
                )
                continue
            if balance < threshold:
                problems.append(
                    f"chain {chain_id} ({CHAIN_NAMES.get(chain_id, '?')}): "
                    f"custody balance {balance} < threshold {threshold}"
                )
        if problems:
            raise CustodyTxStartupError(
                "Custody EOA gas-balance check failed: " + "; ".join(problems)
            )
        logger.info(
            "Custody EOA gas-balance check passed on chains: %s",
            ", ".join(str(c) for c in self._chain_ids),
        )

    async def reconcile_on_startup(self) -> None:
        """Load every persisted record and reconcile each against the chain.

        BROADCAST records get their receipt looked up by `tx_hash`. Records
        whose `tx_hash` is missing (writer crashed before persist) get
        reconciled by sender + `evm_nonce`: if the chain has already mined
        a transaction at that nonce we promote the record to SUCCESS so the
        executor doesn't try to re-broadcast a new tx into a slot that's
        already filled. Terminal records are loaded but left alone.
        """
        records = self._load_all_records()
        if not records:
            return

        logger.info("Reconciling %d custody-tx record(s) on startup", len(records))
        for record in records:
            self._resolution_events.setdefault(
                self._record_key(record.chain_id, record.evm_nonce), asyncio.Event()
            )
            if record.status in TERMINAL_STATUSES:
                continue
            if record.status == CustodyTxStatus.BROADCAST and record.tx_hash:
                await self._reconcile_by_tx_hash(record)
            elif record.status == CustodyTxStatus.BROADCAST and not record.tx_hash:
                # writer crashed between status flip and tx_hash persist
                await self._reconcile_by_sender_nonce(record)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for chain_id in self._chain_ids:
            self._chain_tasks[chain_id] = asyncio.create_task(
                self._chain_loop(chain_id), name=f"custody-tx-{chain_id}"
            )
        logger.info(
            "Custody-tx executor started for chains: %s",
            ", ".join(str(c) for c in self._chain_ids),
        )
        # The clear watcher is best-effort: a start failure (e.g. no Sapphire
        # reader) must not take down the executor — the chain loops run without it.
        try:
            self._clear_watcher = CustodyTxClearWatcher(
                self._accounting,
                self,
                state_dir=self._state_dir,
                sapphire_chain_id=int(self._accounting.settings.sapphire_chain_id),
            )
            await self._clear_watcher.start()
        except Exception:
            logger.warning("Custody-tx clear watcher failed to start — continuing without it")
            self._clear_watcher = None

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._clear_watcher is not None:
            await self._clear_watcher.stop()
            self._clear_watcher = None
        for task in self._chain_tasks.values():
            task.cancel()
        for task in self._chain_tasks.values():
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Custody-tx chain task exited with error")
        self._chain_tasks.clear()
        logger.info("Custody-tx executor stopped")

    async def wait_for_resolution(
        self, key: str, timeout: Optional[float] = None
    ) -> CustodyTxRecord:
        """Block until the record reaches a terminal status, then return it.

        Used by tests and synchronous callers that want to await broadcast
        completion. Production code should generally enqueue and move on.
        """
        event = self._resolution_events.setdefault(key, asyncio.Event())
        if timeout is not None:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        else:
            await event.wait()
        chain_id_str, nonce_str = key.split("_", 1)
        record = self._load_record(int(chain_id_str), int(nonce_str))
        if record is None:
            raise RuntimeError(f"Record {key} resolution-signalled but not on disk")
        return record

    def get_record(self, chain_id: int, evm_nonce: int) -> Optional[CustodyTxRecord]:
        return self._load_record(chain_id, evm_nonce)

    def get_records_for_chain(self, chain_id: int) -> List[CustodyTxRecord]:
        return sorted(
            self._load_all_records(chain_id=chain_id),
            key=lambda r: r.evm_nonce,
        )

    def has_any_pending_xrose_burn(self) -> tuple[bool, list[str]]:
        """Return (has_any, ids) for XROSE_BURN records in non-terminal status.

        A non-terminal XROSE_BURN carries the route address it was signed
        against; rotating ``roflBridgeAddress`` while such a record exists
        would let the broadcast race against a stale destination.
        """
        ids: list[str] = []
        for record in self._load_all_records():
            if record.kind != CustodyTxKind.XROSE_BURN:
                continue
            if record.status in TERMINAL_STATUSES:
                continue
            ids.append(record.id)
        return (bool(ids), ids)

    async def load_from_onchain_reservations(self) -> int:
        """Scan on-chain reservation sources and create missing QUEUED records.

        Pulls from two sources:
        1. The withdrawal queue (`withdrawals[i]`) — resolved entries whose
           `txIdentifier` exposes the destination chain + nonce.
        2. `BridgeBurnReserved` events — bridge burns that have a nonce
           reserved on Sapphire but may not have been broadcast yet.

        Returns the number of new records inserted. Idempotent — already-known
        records are skipped, never overwritten.
        """
        inserted = 0
        contract = self._get_reader_contract_or_none()
        if contract is None:
            return 0

        inserted += await self._catchup_withdrawal_queue(contract)
        inserted += await self._catchup_bridge_burn_reservations(contract)
        if inserted:
            logger.info(
                "Catch-up created %d custody-tx record(s) from on-chain reservations",
                inserted,
            )
        return inserted

    def _get_reader_contract_or_none(self) -> Optional[Any]:
        try:
            return self._accounting._get_reader_contract()
        except Exception:
            logger.exception("Custody-tx catch-up: reader contract unavailable")
            return None

    async def _catchup_withdrawal_queue(self, contract: Any) -> int:
        """Walk the withdrawal queue and enqueue any resolved bridge withdrawal
        whose record is missing from disk.

        Only bridge withdrawals are seeded from this path. Non-bridge
        withdrawals are caught up by ``WithdrawalProcessor._broadcast_missing_for_chain``
        which knows how to read the destination chain from the token registry —
        information the executor cannot derive from ``txIdentifier`` alone.
        """
        try:
            total = await contract.functions.withdrawalCount().call()
        except Exception:
            logger.exception("Catch-up: withdrawalCount() read failed")
            return 0

        sapphire_chain_id = int(self._accounting.settings.sapphire_chain_id)
        bridge_chain_ids = destination_chain_ids(self._accounting.settings)
        custody_address = await self._accounting.get_custody_address()
        inserted = 0
        for index in range(int(total)):
            try:
                entry = await contract.functions.withdrawals(index).call()
            except Exception:
                logger.exception("Catch-up: withdrawals(%d) read failed", index)
                continue
            to_address = entry[1]
            amount = entry[2]
            resolved = entry[5]
            tx_identifier = entry[6]
            if not resolved or not tx_identifier:
                continue

            decoded = self._decode_bridge_tx_identifier(tx_identifier)
            if decoded is None:
                continue
            chain_id, evm_nonce, kind, route_address, max_gas_cost = decoded
            if chain_id not in self._chain_ids:
                continue
            if self._load_record(chain_id, evm_nonce) is not None:
                continue

            # If the destination chain's custody nonce has already advanced past
            # this slot, an earlier incarnation broadcast at that nonce. Local
            # state was lost (wipe / fresh disk), so the previous outcome can no
            # longer be reconstructed without a chain indexer. Mark FAILED_FINAL
            # — chain-loop skips past it without blocking later nonces, and the
            # record stays on disk for forensic correlation against the on-chain
            # tx history.
            try:
                w3 = await self._accounting._get_chain_web3(chain_id)
                mined_nonce = await w3.eth.get_transaction_count(custody_address, "latest")
            except Exception as exc:
                logger.warning(
                    "Catch-up: nonce probe failed for chain=%d index=%d: %s",
                    chain_id,
                    index,
                    exc,
                )
                continue
            if int(mined_nonce) > evm_nonce:
                stale = await self._build_catchup_record(
                    chain_id=chain_id,
                    evm_nonce=evm_nonce,
                    kind=kind,
                    id=str(index),
                    signed_tx=b"",
                    route_address=route_address,
                    max_gas_cost=max_gas_cost,
                    withdrawal_index=index,
                    to_address=str(to_address),
                    amount=int(amount),
                )
                stale.status = CustodyTxStatus.FAILED_FINAL
                stale.error = (
                    f"nonce {evm_nonce} burned on-chain (custody nonce={int(mined_nonce)}) "
                    "before executor restart — outcome unreconstructable from local state"
                )
                self._save_record(stale)
                inserted += 1
                logger.warning(
                    "Catch-up: marked withdrawal #%d FAILED_FINAL "
                    "(chain=%d nonce=%d already burned)",
                    index,
                    chain_id,
                    evm_nonce,
                )
                continue

            # Sapphire defers signing — the preflight regenerates per attempt.
            try:
                if chain_id in bridge_chain_ids:
                    signed_tx = await self._accounting.resolve_bridge_withdrawal(index)
                elif chain_id == sapphire_chain_id:
                    signed_tx = b""
                else:
                    logger.warning(
                        "Catch-up: unsupported bridge destination chain=%d for index %d",
                        chain_id,
                        index,
                    )
                    continue
            except Exception as exc:
                logger.warning(
                    "Catch-up: resolveBridgeWithdrawal(%d) re-fetch failed: %s", index, exc
                )
                continue

            record = await self._build_catchup_record(
                chain_id=chain_id,
                evm_nonce=evm_nonce,
                kind=kind,
                id=str(index),
                signed_tx=signed_tx,
                route_address=route_address,
                max_gas_cost=max_gas_cost,
                withdrawal_index=index,
                to_address=str(to_address),
                amount=int(amount),
            )
            self._save_record(record)
            inserted += 1
            logger.info(
                "Catch-up: queued bridge withdrawal #%d at chain=%d nonce=%d kind=%s",
                index,
                chain_id,
                evm_nonce,
                kind.value,
            )
        return inserted

    async def _catchup_bridge_burn_reservations(self, contract: Any) -> int:
        """Replay `BridgeBurnReserved` events for chains we manage.

        Best-effort: any failure is logged and skipped — the durable-disk
        path is still authoritative; this only fills gaps after a restart.
        """
        try:
            reservations = await self._accounting.list_bridge_burn_reservations()
        except AttributeError:
            return 0
        except Exception:
            logger.exception("Catch-up: BridgeBurnReserved event scan failed")
            return 0

        inserted = 0
        for res in reservations:
            if res.chain_id not in self._chain_ids:
                continue
            if self._load_record(res.chain_id, res.nonce) is not None:
                continue

            try:
                signed_tx = await contract.functions.generateBridgeBurnTransfer(
                    res.deposit_id
                ).call()
            except Exception as exc:
                logger.warning(
                    "Catch-up: generateBridgeBurnTransfer(%s) failed: %s",
                    _to_hex(res.deposit_id),
                    exc,
                )
                continue

            record = await self._build_catchup_record(
                chain_id=res.chain_id,
                evm_nonce=res.nonce,
                kind=CustodyTxKind.XROSE_BURN,
                id=_to_hex(res.deposit_id),
                signed_tx=signed_tx,
                route_address=str(res.bridge),
                amount=int(res.amount),
            )
            self._save_record(record)
            inserted += 1
            logger.info(
                "Catch-up: queued bridge burn at chain=%d nonce=%d depositId=%s",
                res.chain_id,
                res.nonce,
                record.id,
            )
        return inserted

    def _decode_bridge_tx_identifier(
        self,
        tx_identifier: bytes,
    ) -> Optional[tuple[int, int, CustodyTxKind, str, int]]:
        """Decode a bridge-asset withdrawal's txIdentifier.

        Bridge txIdentifiers are encoded as
        ``abi.encode(uint256 destChainId, uint64 destTxNonce, address route, uint256 maxGasCost)``
        — exactly 128 bytes. Non-bridge withdrawals carry
        ``abi.encode(uint64 nonce)`` (32 bytes) and are deliberately not
        handled here: their destination chain lives in the token registry,
        not the txIdentifier, so the upstream withdrawal-processor catch-up
        owns that branch.

        Returns ``None`` (rather than raising) for any payload that does not
        match the bridge layout so callers can skip silently.
        """
        from src.services.accounting_contract import _decode_bridge_tx_identifier as _shared

        if not tx_identifier or len(tx_identifier) != 128:
            return None
        try:
            dest_chain_id, dest_tx_nonce, route, max_gas_cost = _shared(tx_identifier)
        except Exception:
            return None
        sapphire_chain_id = int(self._accounting.settings.sapphire_chain_id)
        kind = (
            CustodyTxKind.SAPPHIRE_RELEASE
            if dest_chain_id == sapphire_chain_id
            else CustodyTxKind.BASE_MINT
        )
        return dest_chain_id, dest_tx_nonce, kind, str(route), int(max_gas_cost)

    async def _build_catchup_record(
        self,
        chain_id: int,
        evm_nonce: int,
        kind: CustodyTxKind,
        id: str,
        signed_tx: Any,
        route_address: Optional[str] = None,
        max_gas_cost: Optional[int] = None,
        withdrawal_index: Optional[int] = None,
        to_address: Optional[str] = None,
        amount: Optional[int] = None,
    ) -> CustodyTxRecord:
        custody_address = await self._accounting.get_custody_address()
        return CustodyTxRecord(
            chain_id=chain_id,
            accounting_contract_address=str(self._accounting.contract_address),
            evm_sender=custody_address,
            evm_nonce=evm_nonce,
            kind=kind,
            id=id,
            signed_tx_hex=_signed_tx_to_hex(signed_tx),
            route_address=route_address,
            max_gas_cost=max_gas_cost,
            withdrawal_index=withdrawal_index,
            to_address=to_address,
            amount=amount,
        )

    async def _self_heal_blocked(self, chain_id: int) -> bool:
        """Re-inspect every BLOCKING_STATUSES record and promote any whose root
        cause has resolved on-chain. Returns True iff ≥1 record left
        BLOCKING_STATUSES.

        Uses only the existing positive-evidence paths: bridge kinds re-run
        duplicate-id recovery (event match → MARK_RECOVERED), everything else
        re-reconciles its receipt. SUCCESS is still gated on receipt status==1
        (or a verified foreign-event match), so a sweep can never spuriously
        promote a record. Each record is reconciled in isolation — a single
        record's failure is logged and skipped so it can't abort the sweep.
        """
        promoted_any = False
        for record in self.get_records_for_chain(chain_id):
            # Cheap pre-filter on the snapshot so we don't lock every record.
            if record.status not in BLOCKING_STATUSES:
                continue
            key = self._record_key(record.chain_id, record.evm_nonce)
            # Hold the same per-slot lock an owner clear takes, and reload inside
            # it: a concurrent BurnNonce→BURNING_NONCE flip or a clear must not be
            # clobbered by a stale-snapshot SUCCESS write (and vice-versa).
            async with self._clear_lock(record.chain_id, record.evm_nonce):
                fresh = self._load_record(record.chain_id, record.evm_nonce)
                if fresh is None or fresh.status not in BLOCKING_STATUSES:
                    continue
                before = fresh.status
                try:
                    if fresh.kind in (CustodyTxKind.BASE_MINT, CustodyTxKind.XROSE_BURN):
                        decision = await self._attempt_duplicate_id_recovery(fresh)
                        if (
                            decision is not None
                            and decision.outcome == PreflightOutcome.MARK_RECOVERED
                        ):
                            self._mark_recovered(fresh, decision, pin_tx_hash=False)
                        # AWAITING_CLEAR / None / no-match → leave blocked (idempotent).
                    elif fresh.tx_hash:
                        await self._reconcile_by_tx_hash(fresh)
                    else:
                        await self._reconcile_by_sender_nonce(fresh)
                except Exception:
                    logger.exception("Custody-tx %s: self-heal pass errored — leaving blocked", key)
                    continue
                if fresh.status not in BLOCKING_STATUSES:
                    promoted_any = True
                    logger.info(
                        "Custody-tx %s: self-heal promoted %s -> %s",
                        key,
                        before.value,
                        fresh.status.value,
                    )
        return promoted_any

    async def _run_self_heal_pass(self, chain_id: int) -> None:
        if await self._self_heal_blocked(chain_id):
            # Drain freshly-unblocked records this tick, bounded so one chain
            # can't monopolise the cooperative scheduler. Yield between drains so
            # other chain loops keep their slots.
            for _ in range(self._same_tick_promotion_budget):
                await self._process_next_for_chain(chain_id)
                await asyncio.sleep(0)

    def _emit_blocking_count_metric(self, chain_id: int, records: List[CustodyTxRecord]) -> None:
        """Emit a per-status blocking count, but only when the snapshot changed,
        so a steady backlog doesn't rewrite the metric on every 1 Hz tick."""
        snapshot: Dict[str, int] = {}
        for record in records:
            if record.status in BLOCKING_STATUSES:
                snapshot[record.status.value] = snapshot.get(record.status.value, 0) + 1
        if snapshot == self._blocking_snapshot.get(chain_id):
            return
        self._blocking_snapshot[chain_id] = snapshot
        for status, count in snapshot.items():
            self._emit_metric(
                "custody_tx.blocking.count", chain_id=chain_id, status=status, count=count
            )
        if not snapshot:
            self._emit_metric(
                "custody_tx.blocking.count", chain_id=chain_id, status="none", count=0
            )

    async def _apply_clear_action(
        self,
        chain_id: int,
        nonce: int,
        action: ClearAction,
        vouched_tx_hash: Any,
    ) -> str:
        """Apply one owner-authorized custody-tx clear under the per-status,
        per-kind allowlist. Returns the verdict the watcher uses to decide
        whether the clear is consumed.

        Verdict vocabulary:
        - ``applied`` — the action mutated the record.
        - ``refused_status`` — record absent-from-blocking or an allowlist
          refusal that can never become valid (consume).
        - ``refused_kind`` — unknown action (consume).
        - ``refused_proof`` — MarkSuccess proof deterministically failed
          (consume).
        - ``deferred`` — a transient / not-yet-determinable condition the same
          action will eventually clear (keep retrying).

        The watcher has already cross-checked the on-chain ``clearAppliedHash``;
        this method owns the off-chain record mutation. It acquires the per-slot
        lock and re-loads the record inside it (compare-and-swap) so a concurrent
        self-heal promotion cannot be clobbered.
        """
        key = self._record_key(chain_id, nonce)
        async with self._clear_lock(chain_id, nonce):
            record = self._load_record(chain_id, nonce)
            if record is None or record.status not in BLOCKING_STATUSES:
                status = record.status.value if record is not None else "missing"
                logger.info(
                    "Custody-tx %s: clear %s refused — record not blocking (status=%s)",
                    key,
                    action.name,
                    status,
                )
                self._emit_metric(
                    "custody_tx.clear.applied_total",
                    chain_id=chain_id,
                    action=action.name,
                    verdict="refused_status",
                )
                return "refused_status"

            before = record.status.value
            has_broadcast = bool(record.broadcast_hashes)
            gas_cap = record.status == CustodyTxStatus.AWAITING_CLEAR_GAS_CAP

            verdict = await self._dispatch_clear_action(
                record, action, vouched_tx_hash, has_broadcast=has_broadcast, gas_cap=gas_cap
            )

            after = self._load_record(chain_id, nonce)
            logger.info(
                "Custody-tx %s: clear %s verdict=%s status %s -> %s",
                key,
                action.name,
                verdict,
                before,
                after.status.value if after is not None else "missing",
            )
            self._emit_metric(
                "custody_tx.clear.applied_total",
                chain_id=chain_id,
                action=action.name,
                verdict=verdict,
            )
            return verdict

    async def _dispatch_clear_action(
        self,
        record: CustodyTxRecord,
        action: ClearAction,
        vouched_tx_hash: Any,
        *,
        has_broadcast: bool,
        gas_cap: bool,
    ) -> str:
        """Enforce the allowlist and apply the action. Returns the metric verdict."""
        key = self._record_key(record.chain_id, record.evm_nonce)

        if action == ClearAction.REQUEUE:
            if gas_cap:
                logger.critical(
                    "Custody-tx %s: Requeue refused on AWAITING_CLEAR_GAS_CAP "
                    "(the cap is the user's EIP-712 signature)",
                    key,
                )
                return "refused_status"
            record.error = None
            record.retry_count = 0
            record.stuck_since = None
            self._mark_status(record, CustodyTxStatus.QUEUED)
            return "applied"

        if action == ClearAction.ABANDON:
            if not has_broadcast:
                on_chain_advanced = await self._on_chain_nonce_advanced(record)
                if not on_chain_advanced:
                    # Precondition not yet met: abandoning now would wedge the
                    # nonce floor. Defer — the same Abandon becomes valid once
                    # the on-chain nonce advances past this slot.
                    logger.info(
                        "Custody-tx %s: Abandon deferred — never broadcast and on-chain "
                        "nonce has not advanced yet",
                        key,
                    )
                    return "deferred"
            self._mark_status(record, CustodyTxStatus.FAILED_FINAL)
            return "applied"

        if action == ClearAction.MARK_SUCCESS_WITH_HASH:
            if not gas_cap and not has_broadcast:
                # AWAITING_CLEAR with no broadcast: the executor never sent a tx,
                # so there is nothing for a hash to vouch for.
                logger.critical(
                    "Custody-tx %s: MarkSuccessWithHash refused — never broadcast "
                    "(nothing the executor sent to vouch for)",
                    key,
                )
                return "refused_status"
            from src.services.custody_tx_proof import verify_mark_success

            try:
                w3 = await self._accounting._get_chain_web3(record.chain_id)
            except Exception as exc:
                # No chain handle to settle the proof against; the same clear
                # succeeds once the reader recovers.
                logger.warning(
                    "Custody-tx %s: MarkSuccessWithHash deferred — web3 unavailable: %s", key, exc
                )
                return "deferred"
            try:
                result, reason = await verify_mark_success(self, record, w3, vouched_tx_hash)
            except Exception as exc:
                logger.critical("Custody-tx %s: MarkSuccessWithHash proof raised: %s", key, exc)
                return "deferred" if is_transient_rpc_error(exc) else "refused_proof"
            if result == "deferred":
                logger.warning(
                    "Custody-tx %s: MarkSuccessWithHash deferred — proof undeterminable: %s",
                    key,
                    reason,
                )
                return "deferred"
            if result != "ok":
                logger.critical(
                    "Custody-tx %s: MarkSuccessWithHash refused — proof failed: %s", key, reason
                )
                return "refused_proof"
            record.recovered_tx_hash = _to_hex(vouched_tx_hash)
            record.error = None
            self._mark_status(record, CustodyTxStatus.SUCCESS)
            return "applied"

        if action == ClearAction.BURN_NONCE:
            if gas_cap or has_broadcast:
                logger.critical(
                    "Custody-tx %s: BurnNonce refused — only AWAITING_CLEAR with no "
                    "broadcast is eligible (a broadcast already consumed the nonce)",
                    key,
                )
                return "refused_status"
            return await self._start_nonce_burn(record)

        logger.critical("Custody-tx %s: unknown clear action %r", key, action)
        return "refused_kind"

    async def _on_chain_nonce_advanced(self, record: CustodyTxRecord) -> bool:
        try:
            w3 = await self._accounting._get_chain_web3(record.chain_id)
            mined = int(await w3.eth.get_transaction_count(record.evm_sender, "latest"))
        except Exception as exc:
            logger.warning(
                "Custody-tx %s: nonce probe failed during clear: %s",
                self._record_key(record.chain_id, record.evm_nonce),
                exc,
            )
            return False
        return mined > record.evm_nonce

    async def _start_nonce_burn(self, record: CustodyTxRecord) -> str:
        """Sign + broadcast the owner-authorized value-0 self-transfer that
        advances the custody EOA past this reserved-but-un-mineable nonce."""
        key = self._record_key(record.chain_id, record.evm_nonce)
        try:
            signed = await self._accounting.sign_nonce_burn(record.chain_id, record.evm_nonce)
        except Exception as exc:
            # GasPriceNotSet / not-burn-authorized revert, or transient RPC. Each
            # of these clears on its own (operator setGasPrice, RPC recovery), so
            # the same BurnNonce succeeds on a later pass — defer, stay blocking.
            logger.warning("Custody-tx %s: BurnNonce deferred — signNonceBurn failed: %s", key, exc)
            return "deferred"

        try:
            w3 = await self._accounting._get_chain_web3(record.chain_id)
            raw = _hex_to_bytes(_signed_tx_to_hex(signed))
            burn_hash = await w3.eth.send_raw_transaction(raw)
        except Exception as exc:
            # Leave the record AWAITING_CLEAR (still blocking) so the same clear
            # re-drives the burn on a later watcher retry / restart.
            logger.warning("Custody-tx %s: BurnNonce deferred — broadcast failed: %s", key, exc)
            return "deferred"

        # Flip to BURNING_NONCE only after the broadcast returns: a crash before
        # this leaves the record blocking and the clear idempotently re-drives it.
        record.status = CustodyTxStatus.BURNING_NONCE
        record.stuck_since = time.time()
        record.retry_count = 0
        record.error = None
        record.burn_nonce_tx_hash = _to_hex(burn_hash)
        self._save_record(record)
        logger.info("Custody-tx %s: nonce burn broadcast tx=%s", key, record.burn_nonce_tx_hash)
        return "applied"

    async def _reconcile_burn_nonce(self, record: CustodyTxRecord) -> None:
        """Drive a BURNING_NONCE record: mark FAILED_FINAL once the slot is burned.

        The slot is burned the instant the on-chain nonce advances past it,
        regardless of which broadcast hash mined — a re-broadcast (new gas → new
        hash) can leave an earlier sibling as the one that mines, so the
        nonce-advance probe is authoritative where the tracked-hash receipt poll
        is not. Carries a wall-clock fallback: a burn that never mines surfaces a
        gated CRITICAL, never silently loops. Each probe re-broadcasts so an owner
        gas bump is absorbed.
        """
        key = self._record_key(record.chain_id, record.evm_nonce)
        if not record.burn_nonce_tx_hash:
            logger.critical("Custody-tx %s: BURNING_NONCE record missing burn_nonce_tx_hash", key)
            return

        # Bump the attempt counter + start the deadline clock BEFORE the receipt
        # lookup so a receipt-RPC outage can't freeze the re-broadcast cadence /
        # CRITICAL deadline (the except branch persists and returns).
        record.retry_count = (record.retry_count or 0) + 1
        if record.stuck_since is None:
            record.stuck_since = time.time()

        try:
            w3 = await self._accounting._get_chain_web3(record.chain_id)
            receipt = await w3.eth.get_transaction_receipt(record.burn_nonce_tx_hash)
        except TransactionNotFound:
            receipt = None
        except Exception:
            logger.exception("Custody-tx %s: burn-nonce receipt lookup failed", key)
            self._save_record(record)
            return

        if receipt is None and await self._on_chain_nonce_advanced(record):
            # A sibling/earlier burn hash mined; the tracked latest hash never
            # will. The slot is burned regardless — promote, CRITICAL for forensics.
            logger.critical(
                "Custody-tx %s: nonce advanced but tracked burn hash %s has no receipt — "
                "FAILED_FINAL (a sibling burn hash mined)",
                key,
                record.burn_nonce_tx_hash,
            )
            self._mark_status(record, CustodyTxStatus.FAILED_FINAL)
            return

        if receipt is not None:
            if int(receipt["status"]) == 1:
                self._mark_status(record, CustodyTxStatus.FAILED_FINAL)
                logger.info("Custody-tx %s: nonce burn mined — FAILED_FINAL", key)
                return
            # A value-0 self-transfer should not revert, but a mined tx consumed
            # its nonce regardless — the burn's purpose (advance the stuck nonce)
            # is achieved. Mark FAILED_FINAL so downstream can drain, with a
            # CRITICAL recording the anomalous receipt for forensics.
            logger.critical(
                "Custody-tx %s: nonce burn mined with anomalous receipt status=%s — "
                "FAILED_FINAL (nonce advanced regardless)",
                key,
                receipt["status"],
            )
            self._mark_status(record, CustodyTxStatus.FAILED_FINAL)
            return

        # Not yet mined. Re-broadcast on the probe cadence REGARDLESS of the
        # deadline so a later owner setGasPrice is always absorbed and the burn
        # can never become a permanent wedge. The wall-clock deadline only gates
        # the CRITICAL escalation log.
        past_deadline = (
            record.stuck_since is not None
            and (time.time() - record.stuck_since) > self._receipt_stuck_deadline
        )
        if record.retry_count % self._receipt_probe_interval == 0:
            if past_deadline:
                logger.critical(
                    "Custody-tx %s: nonce burn unmined past %ss — operator must raise gas",
                    key,
                    self._receipt_stuck_deadline,
                )
            try:
                signed = await self._accounting.sign_nonce_burn(record.chain_id, record.evm_nonce)
                raw = _hex_to_bytes(_signed_tx_to_hex(signed))
                burn_hash = await w3.eth.send_raw_transaction(raw)
                record.burn_nonce_tx_hash = _to_hex(burn_hash)
                logger.info(
                    "Custody-tx %s: re-broadcast nonce burn tx=%s", key, record.burn_nonce_tx_hash
                )
            except Exception as exc:
                error_str = str(exc).lower()
                if "nonce too low" not in error_str and "already known" not in error_str:
                    logger.warning("Custody-tx %s: nonce burn re-broadcast failed: %s", key, exc)
        self._save_record(record)

    async def _chain_loop(self, chain_id: int) -> None:
        logger.info(
            "Custody-tx chain loop started: chain=%d (%s)",
            chain_id,
            CHAIN_NAMES.get(chain_id, "?"),
        )
        # Exponential backoff on consecutive failures so a permanent error
        # (wrong RPC URL, CorruptCustodyTxRecordError on a damaged record)
        # does not log-spam at 1 Hz forever. A single CRITICAL fires once
        # when the chain crosses a sustained-outage threshold so external
        # alerting can pick it up without parsing the per-pass exception
        # stream.
        consecutive_failures = 0
        sleep_seconds = self._poll_interval
        critical_fired = False
        pass_count = 0
        while self._running:
            try:
                pass_count += 1
                # Periodic self-heal of parked BLOCKING_STATUSES records, then
                # the steady-state single-advance pass. Both share the one
                # try/except so a self-heal or drain exception feeds the same
                # backoff + consecutive-failure accounting. A non-positive
                # interval disables self-heal (and avoids ZeroDivisionError).
                if (
                    self._self_heal_interval_passes > 0
                    and pass_count % self._self_heal_interval_passes == 0
                ):
                    await self._run_self_heal_pass(chain_id)
                await self._process_next_for_chain(chain_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                logger.exception(
                    "Custody-tx chain %d: loop pass failed (consecutive=%d)",
                    chain_id,
                    consecutive_failures,
                )
                if consecutive_failures >= _CHAIN_LOOP_CRITICAL_THRESHOLD and not critical_fired:
                    logger.critical(
                        "Custody-tx chain %d: %d consecutive pass failures — "
                        "operator intervention required",
                        chain_id,
                        consecutive_failures,
                    )
                    critical_fired = True
                sleep_seconds = min(sleep_seconds * 2, _CHAIN_LOOP_MAX_BACKOFF_SECONDS)
            else:
                consecutive_failures = 0
                sleep_seconds = self._poll_interval
                critical_fired = False
            await asyncio.sleep(sleep_seconds)
        logger.info("Custody-tx chain loop stopped: chain=%d", chain_id)

    async def _process_next_for_chain(self, chain_id: int) -> None:
        records = self.get_records_for_chain(chain_id)
        self._emit_blocking_count_metric(chain_id, records)
        prev_nonce: Optional[int] = None
        for record in records:
            # Nonce-gap guard: between any two consecutive on-disk records the
            # nonces must be strictly +1. A gap means the executor doesn't know
            # about a reservation the contract has allocated (or catch-up
            # failed). Broadcasting past a missing nonce produces a future-tx
            # that the chain will never mine, stalling later traffic forever.
            if prev_nonce is not None and record.evm_nonce != prev_nonce + 1:
                logger.critical(
                    "Custody-tx chain %d: nonce gap between %d and %d — "
                    "blocking until catch-up fills the missing reservation",
                    chain_id,
                    prev_nonce,
                    record.evm_nonce,
                )
                return
            prev_nonce = record.evm_nonce

            if record.status in (CustodyTxStatus.SUCCESS, CustodyTxStatus.FAILED_FINAL):
                continue
            if record.status in BLOCKING_STATUSES:
                # AWAITING_CLEAR(_GAS_CAP) requires explicit operator clearance.
                # Chain loop halts here so no later nonce advances.
                return
            if record.status == CustodyTxStatus.BURNING_NONCE:
                # An owner-authorized nonce burn is in flight at this slot.
                # Drive it like a pending BROADCAST: poll the burn receipt and
                # block downstream until it mines. Never falls through to the
                # RUNNABLE branch (which would re-broadcast the original
                # un-mineable tx via _broadcast_record).
                await self._reconcile_burn_nonce(record)
                refreshed = self._load_record(record.chain_id, record.evm_nonce)
                if refreshed is not None and refreshed.status == CustodyTxStatus.FAILED_FINAL:
                    # Burn mined: the original withdrawal is abandoned and the
                    # nonce advanced. Drain downstream this same pass.
                    continue
                return
            if record.status == CustodyTxStatus.BROADCAST:
                if record.tx_hash:
                    resolved = await self._reconcile_by_tx_hash(record)
                else:
                    resolved = await self._reconcile_by_sender_nonce(record)
                if not resolved:
                    # Reconcile could not promote the record (TransactionNotFound,
                    # RPC error, or nonce not yet advanced). Bump the receipt-poll
                    # counter so a permanently-dropped tx eventually escalates
                    # instead of stalling the chain loop forever.
                    await self._handle_receipt_retry(record, "reconcile-pending")
                refreshed = self._load_record(record.chain_id, record.evm_nonce)
                if refreshed is None or refreshed.status != CustodyTxStatus.SUCCESS:
                    return
                continue
            if record.status in RUNNABLE_STATUSES:
                # Absolute nonce floor. The inter-record +1 guard above only
                # anchors a record against a *previous on-disk record*; a lone
                # runnable record at a future nonce (e.g. a bridge reservation
                # seeded before the missing normal withdrawal at nonce-1 is on
                # disk) has no predecessor to fail against and would broadcast a
                # tx the chain can never mine, stalling the loop forever after
                # receipt retries. Block until catch-up fills the missing
                # nonce(s) — fail-closed, like the +1 guard.
                w3 = await self._accounting._get_chain_web3(chain_id)
                on_chain_next = await w3.eth.get_transaction_count(record.evm_sender, "latest")
                if record.evm_nonce > on_chain_next:
                    logger.critical(
                        "Custody-tx chain %d: runnable nonce %d exceeds on-chain "
                        "next nonce %d — blocking until catch-up fills the gap",
                        chain_id,
                        record.evm_nonce,
                        on_chain_next,
                    )
                    return
                await self._broadcast_record(record)
                return

    async def _preflight(self, record: CustodyTxRecord) -> PreflightDecision:
        """Kind-routed preflight dispatcher.

        Bridge records use persisted-on-record inputs (route_address,
        max_gas_cost, withdrawal_index, to_address, amount) so restart
        rebuilds the full policy gate without any in-memory closures.
        Records missing those inputs (legacy enqueues that predate the
        kind-routed preflight) fall through to ALLOW — the contract still
        enforces invariants on-chain, just without an eth_call save.
        """
        if record.kind == CustodyTxKind.BASE_MINT:
            if record.route_address is None or record.to_address is None or record.amount is None:
                return PreflightDecision(outcome=PreflightOutcome.ALLOW)
            return await self._preflight_base_mint(record)
        if record.kind == CustodyTxKind.SAPPHIRE_RELEASE:
            if record.max_gas_cost is None or record.withdrawal_index is None:
                return PreflightDecision(outcome=PreflightOutcome.ALLOW)
            return await self._preflight_sapphire_release(record)
        if record.kind == CustodyTxKind.XROSE_BURN:
            if record.route_address is None or record.amount is None:
                return PreflightDecision(outcome=PreflightOutcome.ALLOW)
            return await self._preflight_xrose_burn(record)
        return PreflightDecision(outcome=PreflightOutcome.ALLOW)

    def _get_bridge_contract(self, w3: Any, chain_id: int, route_address: str) -> Any:
        addr = Web3.to_checksum_address(route_address)
        cached = self._bridge_contract_cache.get((chain_id, addr))
        if cached is None:
            cached = w3.eth.contract(address=addr, abi=ROFL_BRIDGE_ABI)
            self._bridge_contract_cache[(chain_id, addr)] = cached
        return cached

    def _compute_withdrawal_id(self, record: CustodyTxRecord) -> bytes:
        """Mirror BridgeLib.resolveSign:
        keccak256(abi.encode(accountingProxy, sapphireChainId, index))."""
        sapphire_chain_id = int(self._accounting.settings.sapphire_chain_id)
        return keccak(
            abi_encode(
                ["address", "uint256", "uint256"],
                [
                    Web3.to_checksum_address(record.accounting_contract_address),
                    sapphire_chain_id,
                    int(record.withdrawal_index),
                ],
            )
        )

    async def _verify_recovered_bridge_event(
        self,
        record: CustodyTxRecord,
        w3: Any,
        id_bytes: bytes,
        *,
        label: str,
        mapping_attr: str,
        event_name: str,
        filter_key: str,
        match_event: Callable[[dict], Optional[str]],
    ) -> PreflightDecision:
        """Shared duplicate-id verifier. Mint and burn differ only in the
        on-chain mapping name, the event name, the indexed filter key, and
        which event args are compared against the queued record."""
        bridge = self._get_bridge_contract(w3, record.chain_id, record.route_address)
        try:
            already = await getattr(bridge.functions, mapping_attr)(id_bytes).call()
        except Exception as exc:
            return _rpc_failure_decision(exc, f"{mapping_attr} read failed: {exc}")
        if not already:
            return PreflightDecision(
                outcome=PreflightOutcome.AWAITING_CLEAR,
                error=f"AlreadyProcessed revert but {mapping_attr}[id] == False",
            )
        try:
            events = await getattr(bridge.events, event_name).get_logs(
                from_block=0, argument_filters={filter_key: id_bytes}
            )
        except Exception as exc:
            return _rpc_failure_decision(exc, f"{event_name} event lookup failed: {exc}")
        if not events:
            return PreflightDecision(
                outcome=PreflightOutcome.AWAITING_CLEAR,
                error=f"AlreadyProcessed revert but no {event_name} event found",
            )
        if len(events) > 1:
            return PreflightDecision(
                outcome=PreflightOutcome.AWAITING_CLEAR,
                error=f"{len(events)} {event_name} events for single-{label} invariant",
            )
        mismatch = match_event(events[0]["args"])
        if mismatch is not None:
            return PreflightDecision(
                outcome=PreflightOutcome.AWAITING_CLEAR,
                error=mismatch,
            )
        recovered_hash = _to_hex(events[0]["transactionHash"])
        recovered_block = int(events[0].get("blockNumber") or 0)
        signer_check = await self._verify_recovery_signer_and_nonce(record, w3, recovered_hash)
        if signer_check is not None:
            return signer_check
        return PreflightDecision(
            outcome=PreflightOutcome.MARK_RECOVERED,
            recovered_tx_hash=recovered_hash,
            recovered_block_number=recovered_block,
        )

    async def _verify_recovered_mint(
        self,
        record: CustodyTxRecord,
        w3: Any,
        withdrawal_id: bytes,
    ) -> PreflightDecision:
        """Fail closed unless the on-chain Minted event matches this record.

        A foreign caller could have minted under the same id with different
        ``to``/``amount`` — that's an inconsistency for an operator, not a
        recovery.
        """

        def match(args: dict) -> Optional[str]:
            expected_to = (record.to_address or "").lower()
            actual_to = str(args.get("to", "")).lower()
            actual_amount = args.get("amount")
            if actual_to == expected_to and actual_amount == record.amount:
                return None
            return (
                f"Minted event mismatch: expected to={record.to_address} "
                f"amount={record.amount}, got to={args.get('to')} amount={actual_amount}"
            )

        return await self._verify_recovered_bridge_event(
            record,
            w3,
            withdrawal_id,
            label="mint",
            mapping_attr="mintedWithdrawalIds",
            event_name="Minted",
            filter_key="withdrawalId",
            match_event=match,
        )

    async def _verify_recovered_burn(
        self,
        record: CustodyTxRecord,
        w3: Any,
        deposit_id: bytes,
    ) -> PreflightDecision:
        """Burn-side analogue. ``Burned`` carries no recipient, so only
        ``amount`` is matched."""

        def match(args: dict) -> Optional[str]:
            actual_amount = args.get("amount")
            if actual_amount == record.amount:
                return None
            return (
                f"Burned event mismatch: expected amount={record.amount}, "
                f"got amount={actual_amount}"
            )

        return await self._verify_recovered_bridge_event(
            record,
            w3,
            deposit_id,
            label="burn",
            mapping_attr="burnedDepositIds",
            event_name="Burned",
            filter_key="depositId",
            match_event=match,
        )

    async def _verify_recovery_signer_and_nonce(
        self,
        record: CustodyTxRecord,
        w3: Any,
        recovered_tx_hash: str,
    ) -> Optional[PreflightDecision]:
        """Return AWAITING_CLEAR if the recovered tx wasn't sent by this record's
        custody EOA at the expected nonce; None to let recovery proceed.

        ROFLBridge replay protection is id-based; nonce parity isn't enforced
        on-chain. Without this guard, a rotated `roflSigner` (or any foreign
        caller passing the to+amount match) would flip our record to SUCCESS
        while our custody EOA's nonce stayed unconsumed — the next broadcast
        would land as future-nonce and stall the chain queue.
        """
        try:
            tx = await w3.eth.get_transaction(recovered_tx_hash)
        except Exception as exc:
            return _rpc_failure_decision(
                exc, f"recovered tx lookup failed ({recovered_tx_hash}): {exc}"
            )
        if tx is None:
            return PreflightDecision(
                outcome=PreflightOutcome.AWAITING_CLEAR,
                error=f"recovered tx not found on chain: {recovered_tx_hash}",
            )
        tx_from = str(tx.get("from") or "").lower()
        expected_sender = (record.evm_sender or "").lower()
        try:
            tx_nonce = int(tx.get("nonce"))
        except (TypeError, ValueError) as exc:
            return PreflightDecision(
                outcome=PreflightOutcome.AWAITING_CLEAR,
                error=f"recovered tx nonce unreadable ({recovered_tx_hash}): {exc}",
            )
        if tx_from != expected_sender or tx_nonce != record.evm_nonce:
            return PreflightDecision(
                outcome=PreflightOutcome.AWAITING_CLEAR,
                error=(
                    f"recovered tx {recovered_tx_hash} mismatch: "
                    f"expected from={record.evm_sender} nonce={record.evm_nonce}, "
                    f"got from={tx.get('from')} nonce={tx.get('nonce')}"
                ),
            )
        return None

    async def _attempt_duplicate_id_recovery(
        self, record: CustodyTxRecord
    ) -> Optional[PreflightDecision]:
        """None signals recovery N/A — non-bridge kind, missing inputs, or
        web3 unavailable."""
        try:
            w3 = await self._accounting._get_chain_web3(record.chain_id)
        except Exception as exc:
            logger.warning(
                "Custody-tx %s: duplicate-id recovery skipped, web3 lookup failed: %s",
                self._record_key(record.chain_id, record.evm_nonce),
                exc,
            )
            return None
        if record.kind == CustodyTxKind.BASE_MINT:
            if record.withdrawal_index is None or record.route_address is None:
                return None
            return await self._verify_recovered_mint(
                record, w3, self._compute_withdrawal_id(record)
            )
        if record.kind == CustodyTxKind.XROSE_BURN:
            if record.route_address is None:
                return None
            try:
                deposit_id = bytes(HexBytes(record.id))
            except Exception:
                return None
            if len(deposit_id) != 32:
                return None
            return await self._verify_recovered_burn(record, w3, deposit_id)
        return None

    async def _preflight_base_mint(self, record: CustodyTxRecord) -> PreflightDecision:
        """eth_call ROFLBridge.mint(to, amount, withdrawalId) from custody EOA.

        withdrawalId mirrors BridgeLib.resolveSign:
        keccak256(abi.encode(accountingProxy, sapphireChainId, index)).
        AlreadyProcessed implies a prior incarnation mined this id; route
        to event-log recovery.
        """
        try:
            w3 = await self._accounting._get_chain_web3(record.chain_id)
            evm_from = await self._accounting.get_custody_address()
            withdrawal_id = self._compute_withdrawal_id(record)
            rofl_bridge = self._get_bridge_contract(w3, record.chain_id, record.route_address)
            await rofl_bridge.functions.mint(
                Web3.to_checksum_address(record.to_address),
                int(record.amount),
                withdrawal_id,
            ).call({"from": evm_from})
        except ContractLogicError as exc:
            if _extract_revert_selector(exc) == _ALREADY_PROCESSED_SELECTOR:
                return await self._verify_recovered_mint(record, w3, withdrawal_id)
            return _classify_rofl_bridge_revert(exc, record, kind_label="base mint")
        except Exception as exc:
            return _rpc_failure_decision(exc, f"base mint preflight error: {exc}")

        if record.withdrawal_index is None:
            # Legacy enqueue predating the re-sign path: no index to re-sign
            # from, so broadcast the frozen sign-time tx.
            return PreflightDecision(outcome=PreflightOutcome.ALLOW)
        # Re-sign per attempt so the tx picks up any operator gas-price change,
        # mirroring _preflight_sapphire_release. BASE_MINT is signed by the same
        # resolveBridgeWithdrawal call (the contract branches on destChainId);
        # the destination-tx nonce is frozen in the stored txIdentifier at
        # request time, so re-signing changes only gas, never record.evm_nonce.
        try:
            fresh = await self._accounting.resolve_bridge_withdrawal(int(record.withdrawal_index))
        except ContractLogicError as exc:
            return PreflightDecision(
                outcome=PreflightOutcome.AWAITING_CLEAR,
                error=f"resolve_bridge_withdrawal reverted: {exc}",
            )
        except Exception as exc:
            return _rpc_failure_decision(exc, f"resolve_bridge_withdrawal rpc failure: {exc}")
        return PreflightDecision(
            outcome=PreflightOutcome.ALLOW,
            fresh_signed_tx=bytes(fresh),
        )

    async def _preflight_xrose_burn(self, record: CustodyTxRecord) -> PreflightDecision:
        """eth_call ROFLBridge.burn(amount, depositId) from custody EOA.

        ``record.id`` is the 0x-prefixed depositId hex persisted by
        ``sweep_engine.enqueue_xrose_burn``; the contract expects ``bytes32``.
        """
        try:
            w3 = await self._accounting._get_chain_web3(record.chain_id)
            evm_from = await self._accounting.get_custody_address()
            deposit_id = bytes(HexBytes(record.id))
            if len(deposit_id) != 32:
                return PreflightDecision(
                    outcome=PreflightOutcome.AWAITING_CLEAR,
                    error=f"xrose burn depositId must be 32 bytes (got {len(deposit_id)})",
                )
            rofl_bridge = self._get_bridge_contract(w3, record.chain_id, record.route_address)
            await rofl_bridge.functions.burn(int(record.amount), deposit_id).call(
                {"from": evm_from}
            )
        except ContractLogicError as exc:
            if _extract_revert_selector(exc) == _ALREADY_PROCESSED_SELECTOR:
                return await self._verify_recovered_burn(record, w3, deposit_id)
            return _classify_rofl_bridge_revert(exc, record, kind_label="xrose burn")
        except Exception as exc:
            return _rpc_failure_decision(exc, f"xrose burn preflight error: {exc}")

        # Re-sign per attempt so the tx picks up any operator gas-price change,
        # mirroring _preflight_sapphire_release. The destination-tx nonce is
        # frozen in BridgeBurnRequest.nonce at reserve time and re-read on every
        # sign, so re-signing changes only gas, never record.evm_nonce.
        try:
            fresh = await self._accounting.generate_bridge_burn_transfer(deposit_id)
        except ContractLogicError as exc:
            return PreflightDecision(
                outcome=PreflightOutcome.AWAITING_CLEAR,
                error=f"generate_bridge_burn_transfer reverted: {exc}",
            )
        except Exception as exc:
            return _rpc_failure_decision(exc, f"generate_bridge_burn_transfer rpc failure: {exc}")
        return PreflightDecision(
            outcome=PreflightOutcome.ALLOW,
            fresh_signed_tx=bytes(fresh),
        )

    async def _preflight_sapphire_release(self, record: CustodyTxRecord) -> PreflightDecision:
        """Re-sign per attempt so the tx picks up any operator gas-price change."""
        try:
            fresh = await self._accounting.resolve_bridge_withdrawal(int(record.withdrawal_index))
        except ContractLogicError as exc:
            # The executor has no autonomous gas bump, so any resolve revert is
            # operator-only-clear. GasBudgetExceeded gets its own status so an
            # operator can tell a gas-cap block apart from a generic revert.
            if _extract_revert_selector(exc) == _GAS_BUDGET_EXCEEDED_SELECTOR:
                return PreflightDecision(
                    outcome=PreflightOutcome.AWAITING_CLEAR,
                    target_status=CustodyTxStatus.AWAITING_CLEAR_GAS_CAP,
                    error=f"resolve_bridge_withdrawal reverted GasBudgetExceeded: {exc}",
                )
            return PreflightDecision(
                outcome=PreflightOutcome.AWAITING_CLEAR,
                error=f"resolve_bridge_withdrawal reverted: {exc}",
            )
        except Exception as exc:
            return _rpc_failure_decision(exc, f"resolve_bridge_withdrawal rpc failure: {exc}")
        return PreflightDecision(
            outcome=PreflightOutcome.ALLOW,
            fresh_signed_tx=bytes(fresh),
        )

    async def _broadcast_record(self, record: CustodyTxRecord) -> None:
        key = self._record_key(record.chain_id, record.evm_nonce)
        try:
            decision = await self._preflight(record)
        except Exception as exc:
            if is_transient_rpc_error(exc):
                logger.warning(
                    "Custody-tx %s: preflight transient RPC error — staying runnable: %s",
                    key,
                    exc,
                )
                self._mark_status(record, CustodyTxStatus.QUEUED, error=str(exc))
                return
            logger.exception("Custody-tx %s: preflight raised — marking AWAITING_CLEAR", key)
            self._mark_status(record, CustodyTxStatus.AWAITING_CLEAR, error=str(exc))
            return

        if decision.outcome == PreflightOutcome.AWAITING_CLEAR:
            target = decision.target_status or CustodyTxStatus.AWAITING_CLEAR
            self._mark_status(record, target, error=decision.error)
            return

        if decision.outcome == PreflightOutcome.RETRY_LATER:
            target = decision.target_status or CustodyTxStatus.QUEUED
            if record.status != target:
                self._mark_status(record, target, error=decision.error)
            elif record.error != decision.error:
                # Constant errors (paused / limit-exhausted) skip the save —
                # otherwise every 1 Hz tick rewrites the same JSON.
                record.error = decision.error
                self._save_record(record)
            return

        if decision.outcome == PreflightOutcome.MARK_RECOVERED:
            # Preflight path mirrors the matched event into both tx_hash
            # and recovered_* — those duplicate fields are how an operator
            # tells recovery (recovered_* set) from a real broadcast (None).
            self._mark_recovered(record, decision, pin_tx_hash=True)
            logger.info(
                "Custody-tx %s: recovered duplicate-id via event log "
                "recovered_tx=%s recovered_block=%s",
                self._record_key(record.chain_id, record.evm_nonce),
                record.recovered_tx_hash,
                record.recovered_block_number,
            )
            return

        # ALLOW: swap in fresh signed tx (SAPPHIRE_RELEASE) and clear stale
        # retry-error / WAITING_FOR_GAS_CAP indicator before broadcast.
        # Only persist if something actually changed.
        changed = False
        if decision.fresh_signed_tx is not None:
            record.signed_tx_hex = _signed_tx_to_hex(decision.fresh_signed_tx)
            changed = True
        if record.error or record.status == CustodyTxStatus.WAITING_FOR_GAS_CAP:
            record.error = None
            record.status = CustodyTxStatus.QUEUED
            changed = True
        if changed:
            self._save_record(record)

        try:
            w3 = await self._accounting._get_chain_web3(record.chain_id)
            raw = _hex_to_bytes(record.signed_tx_hex)
            # Append the hash *before* broadcast so a crash between
            # `send_raw_transaction` and the post-broadcast persist below
            # (or a fresh-sign on the next retry) cannot orphan the hash
            # used by `_reconcile_by_sender_nonce`.
            attempt_hash = "0x" + keccak(raw).hex()
            if attempt_hash not in record.broadcast_hashes:
                record.broadcast_hashes.append(attempt_hash)
                self._save_record(record)
            tx_hash = await w3.eth.send_raw_transaction(raw)
        except Exception as exc:
            error_str = str(exc).lower()
            if "nonce too low" in error_str or "already known" in error_str:
                # A previous incarnation broadcast this nonce; reconcile by
                # sender+nonce to find the mined tx.
                logger.info("Custody-tx %s: broadcast says already-known; reconciling", key)
                self._mark_status(record, CustodyTxStatus.BROADCAST)
                await self._reconcile_by_sender_nonce(record)
                return
            if is_transient_rpc_error(exc):
                # Transport/server blip after web3's own retries — stay runnable
                # so the next pass re-broadcasts the same nonce.
                record.retry_count = (record.retry_count or 0) + 1
                logger.warning(
                    "Custody-tx %s: transient broadcast error — re-queueing: %s", key, exc
                )
                self._mark_status(record, CustodyTxStatus.QUEUED, error=str(exc))
                return
            # Real rejection (signature-invalid / intrinsic-gas / unknown-account).
            logger.exception("Custody-tx %s: broadcast failed", key)
            self._mark_status(record, CustodyTxStatus.AWAITING_CLEAR, error=str(exc))
            return

        record.tx_hash = _to_hex(tx_hash)
        record.status = CustodyTxStatus.BROADCAST
        try:
            self._save_record(record)
        except OSError as exc:
            # The tx is on-chain but we lost the durable receipt that the
            # post-restart reconcile path depends on. Force AWAITING_CLEAR
            # so an operator can correlate the broadcast log line with the
            # chain state instead of the executor re-broadcasting blindly.
            logger.critical(
                "Custody-tx %s: post-broadcast persist failed (tx_hash=%s): %s",
                key,
                record.tx_hash,
                exc,
            )
            self._mark_status(
                record,
                CustodyTxStatus.AWAITING_CLEAR,
                error=f"broadcast succeeded but persist failed: {exc}",
            )
            return
        logger.info(
            "Custody-tx %s: broadcast tx=%s",
            key,
            record.tx_hash,
        )

        try:
            receipt = await self._wait_for_receipt(record.chain_id, record.tx_hash)
        except asyncio.TimeoutError:
            await self._handle_receipt_retry(record, "timeout")
            return
        except Exception as exc:
            logger.exception("Custody-tx %s: receipt poll failed", key)
            await self._handle_receipt_retry(record, f"poll-error: {exc}")
            return

        await self._apply_receipt(record, receipt)

    async def _handle_receipt_retry(self, record: CustodyTxRecord, reason: str) -> None:
        """Liveness loop for a BROADCAST record whose receipt is still missing.

        Cheap passes just persist the attempt counter. Every probe-interval pass
        reads the on-chain sender nonce: if the slot advanced, reconcile promotes
        on a status==1 receipt (or escalates a foreign-burned slot); if not, the
        record is flipped back to runnable so the next pass re-runs preflight
        (re-signing at the current owner gasPrices[chainId]) and re-broadcasts,
        absorbing "already known"/"nonce too low". A SAPPHIRE_RELEASE whose
        re-sign now reverts GasBudgetExceeded escalates to AWAITING_CLEAR_GAS_CAP
        via the preflight path — the executor has no autonomous gas bump. A tx
        whose nonce never advances past the wall-clock deadline escalates to
        AWAITING_CLEAR so a dropped or under-priced tx cannot stall the chain.
        """
        key = self._record_key(record.chain_id, record.evm_nonce)
        record.retry_count = (record.retry_count or 0) + 1
        if record.stuck_since is None:
            record.stuck_since = time.time()

        # Cheap passes between probes: just persist the counter + stuck_since.
        if record.retry_count % self._receipt_probe_interval != 0:
            logger.warning(
                "Custody-tx %s: receipt %s (attempt %d)", key, reason, record.retry_count
            )
            self._save_record(record)
            return

        # Probe the on-chain nonce (the ONE allowed live nonce read besides reconcile).
        try:
            w3 = await self._accounting._get_chain_web3(record.chain_id)
            mined_count = int(await w3.eth.get_transaction_count(record.evm_sender, "latest"))
        except Exception:
            logger.exception("Custody-tx %s: nonce probe failed during receipt retry", key)
            self._save_record(record)
            return

        if mined_count > record.evm_nonce:
            # Slot advanced — our tx (or a foreign one) mined. Reconcile promotes on
            # receipt status==1, escalates on a foreign-burned slot.
            record.stuck_since = None
            self._save_record(record)
            await self._reconcile_by_sender_nonce(record)
            return

        # Nonce has NOT advanced: still pending or dropped.
        if (
            record.stuck_since is not None
            and (time.time() - record.stuck_since) > self._receipt_stuck_deadline
        ):
            logger.error(
                "Custody-tx %s: tx unmined past %ss deadline (%s) — AWAITING_CLEAR",
                key,
                self._receipt_stuck_deadline,
                reason,
            )
            self._mark_status(
                record,
                CustodyTxStatus.AWAITING_CLEAR,
                error=f"tx unmined past {self._receipt_stuck_deadline}s deadline: {reason}",
            )
            return

        # Under deadline and still pending: flip back to runnable so the next pass
        # re-runs preflight and re-broadcasts (see the docstring for the re-sign /
        # gas-cap path).
        logger.warning(
            "Custody-tx %s: re-running preflight + re-broadcast (attempt %d, %s)",
            key,
            record.retry_count,
            reason,
        )
        record.status = CustodyTxStatus.QUEUED
        self._save_record(record)

    async def _reconcile_by_tx_hash(self, record: CustodyTxRecord) -> bool:
        """Return True iff the record was promoted to a terminal status."""
        if not record.tx_hash:
            return False
        try:
            w3 = await self._accounting._get_chain_web3(record.chain_id)
            receipt = await w3.eth.get_transaction_receipt(record.tx_hash)
        except TransactionNotFound:
            return False
        except Exception:
            logger.exception(
                "Custody-tx %s: receipt lookup failed",
                self._record_key(record.chain_id, record.evm_nonce),
            )
            return False
        await self._apply_receipt(record, receipt)
        return True

    async def _reconcile_by_sender_nonce(self, record: CustodyTxRecord) -> bool:
        """Reconcile a BROADCAST record whose ``tx_hash`` was never persisted.

        Returns True iff the record was promoted to a terminal status.

        Two steps, both required to honor the "SUCCESS only on receipt
        status == 1" invariant:

        1. Probe ``eth_getTransactionCount(sender, 'latest')``. If it has not
           advanced past ``evm_nonce``, the slot is still open — leave the
           record as is so the next pass can re-broadcast. This is the ONE
           place RPC nonce reads are allowed (reconciliation only).
        2. Derive the *expected* tx hash from the persisted ``signed_tx_hex``
           (``keccak256`` of the raw bytes). Look it up. status=1 → SUCCESS;
           status=0 → AWAITING_CLEAR; not found while the nonce HAS advanced
           → AWAITING_CLEAR (something else burned the slot).

        Bumping a record to SUCCESS purely because the on-chain nonce moved
        would silently accept reverted, replaced, or reorg-vulnerable txs.
        """
        key = self._record_key(record.chain_id, record.evm_nonce)
        try:
            w3 = await self._accounting._get_chain_web3(record.chain_id)
            mined_count = await w3.eth.get_transaction_count(record.evm_sender, "latest")
        except Exception:
            logger.exception("Custody-tx %s: transaction-count probe failed", key)
            return False
        if int(mined_count) <= record.evm_nonce:
            return False

        # Candidate hashes: every attempt's `keccak(signed_tx)` was appended
        # to `broadcast_hashes` before `send_raw_transaction`. Fall back to a
        # derived hash from the current `signed_tx_hex` for legacy records
        # written before `broadcast_hashes` existed.
        candidates: list[str] = list(record.broadcast_hashes)
        if not candidates:
            try:
                raw = _hex_to_bytes(record.signed_tx_hex)
            except Exception:
                logger.exception("Custody-tx %s: signed_tx_hex unreadable — cannot reconcile", key)
                self._mark_status(
                    record,
                    CustodyTxStatus.AWAITING_CLEAR,
                    error="signed_tx_hex unreadable",
                )
                return True
            candidates.append("0x" + keccak(raw).hex())

        receipt = None
        matched_hash: Optional[str] = None
        receipt_lookup_failed = False
        for candidate in candidates:
            try:
                receipt = await w3.eth.get_transaction_receipt(candidate)
            except TransactionNotFound:
                continue
            except Exception:
                logger.exception("Custody-tx %s: receipt lookup for %s failed", key, candidate)
                receipt_lookup_failed = True
                continue
            matched_hash = candidate
            break

        if matched_hash is None:
            if receipt_lookup_failed:
                return False
            logger.error(
                "Custody-tx %s: nonce advanced to %d but no broadcast hash (%s) found — "
                "another tx burned the slot, AWAITING_CLEAR",
                key,
                mined_count,
                ", ".join(candidates) if candidates else "none",
            )
            self._mark_status(
                record,
                CustodyTxStatus.AWAITING_CLEAR,
                error=f"nonce filled by foreign tx (tried {', '.join(candidates)})",
            )
            return True

        record.tx_hash = matched_hash
        await self._apply_receipt(record, receipt)
        return True

    async def _apply_receipt(self, record: CustodyTxRecord, receipt: Any) -> None:
        status = int(receipt["status"]) if receipt is not None else 0
        block_number = int(receipt.get("blockNumber", 0)) if receipt is not None else 0
        record.receipt_status = status
        record.receipt_block_number = block_number
        if receipt is not None:
            record.gas_used = (
                int(receipt["gasUsed"]) if receipt.get("gasUsed") is not None else None
            )
            record.effective_gas_price = (
                int(receipt["effectiveGasPrice"])
                if receipt.get("effectiveGasPrice") is not None
                else None
            )

        if status != 1:
            # Race recovery: a foreign caller may have mined the same id
            # between preflight and our broadcast. Preserve the reverted
            # broadcast's tx_hash/receipt_status; the recovered event
            # lands in recovered_* so forensic state of the failed
            # broadcast survives.
            if record.kind in (CustodyTxKind.BASE_MINT, CustodyTxKind.XROSE_BURN):
                recovery = await self._attempt_duplicate_id_recovery(record)
                if recovery is not None:
                    if recovery.outcome == PreflightOutcome.MARK_RECOVERED:
                        self._mark_recovered(record, recovery, pin_tx_hash=False)
                        logger.info(
                            "Custody-tx %s: receipt status=0 recovered via event log "
                            "broadcast_tx=%s recovered_tx=%s block=%s",
                            self._record_key(record.chain_id, record.evm_nonce),
                            record.tx_hash,
                            record.recovered_tx_hash,
                            record.recovered_block_number,
                        )
                        return
                    if recovery.outcome == PreflightOutcome.AWAITING_CLEAR:
                        # Surface recovery's diagnostic (cross-check failure,
                        # mismatched event, etc.) instead of the generic
                        # "receipt status 0" — without this the foreign-mint
                        # forensics are dropped on the floor.
                        self._mark_status(
                            record,
                            CustodyTxStatus.AWAITING_CLEAR,
                            error=recovery.error,
                        )
                        return
            self._mark_status(
                record,
                CustodyTxStatus.AWAITING_CLEAR,
                error=f"receipt status {status}",
            )
            return

        if record.kind == CustodyTxKind.SAPPHIRE_RELEASE and record.max_gas_cost is not None:
            if record.gas_used is None or record.effective_gas_price is None:
                # Receipt is missing one of the cost fields. Coercing to 0 would
                # credit the full max_gas_cost as surplus and contradict the
                # fail-closed posture of the adjacent gas-cap branch. Skip the
                # accumulator entry and escalate so the operator can inspect.
                record.surplus_delta = None
                self._mark_status(
                    record,
                    CustodyTxStatus.AWAITING_CLEAR_GAS_CAP,
                    error=(
                        "receipt missing gas_used or effective_gas_price "
                        f"(gas_used={record.gas_used}, "
                        f"effective_gas_price={record.effective_gas_price})"
                    ),
                )
                return
            actual = record.gas_used * record.effective_gas_price
            if actual > int(record.max_gas_cost):
                # Contract invariant break: BridgeLib enforces
                # gas_limit*gas_price <= max_gas_cost at sign time, and
                # gas_used <= gas_limit on-chain. If we observe a higher
                # actual cost the EVM contract's gating broke — fail closed.
                self._mark_status(
                    record,
                    CustodyTxStatus.AWAITING_CLEAR_GAS_CAP,
                    error=(f"actual gas cost {actual} > max_gas_cost {record.max_gas_cost}"),
                )
                return
            record.surplus_delta = int(record.max_gas_cost) - actual

        self._mark_status(record, CustodyTxStatus.SUCCESS)

    def sapphire_release_surplus(self) -> int:
        """Sum positive ``surplus_delta`` across SAPPHIRE_RELEASE SUCCESS records.

        Reads from disk so the value is reconstructed cleanly across restarts.
        An operator-acknowledged reconcile flow can zero out individual
        records' surplus once the gas reserve has been rolled forward
        on-chain.
        """
        sapphire_chain_id = int(self._accounting.settings.sapphire_chain_id)
        total = 0
        for r in self.get_records_for_chain(sapphire_chain_id):
            if r.kind == CustodyTxKind.SAPPHIRE_RELEASE and r.status == CustodyTxStatus.SUCCESS:
                delta = r.surplus_delta or 0
                if delta > 0:
                    total += delta
        return total

    async def _wait_for_receipt(self, chain_id: int, tx_hash: str) -> Any:
        w3 = await self._accounting._get_chain_web3(chain_id)
        deadline = time.monotonic() + self._receipt_timeout
        while time.monotonic() < deadline:
            try:
                return await w3.eth.get_transaction_receipt(tx_hash)
            except TransactionNotFound:
                await asyncio.sleep(self._poll_interval)
        raise asyncio.TimeoutError(f"receipt not found within {self._receipt_timeout}s")

    def _save_record(self, record: CustodyTxRecord) -> None:
        path = self._record_path(record.chain_id, record.evm_nonce)
        tmp = path.with_suffix(".tmp")
        payload = json.dumps(record.to_dict(), indent=2)
        # 0o600 applied at create time via O_EXCL avoids the TOCTOU window
        # where a chmod-after-write briefly leaves the raw signed tx
        # world-readable. The signed tx is replayable by any holder
        # independent of the executor's policy gates.
        if tmp.exists():
            tmp.unlink()
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
                # fsync the data (and the dir, after the rename) so the rename
                # gives durability, not just atomicity: on this encrypted TDX disk
                # the post-broadcast persist must survive a crash or the tx_hash is
                # lost while the tx is on-chain.
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(str(tmp), str(path))
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        except OSError:
            # Some platforms / filesystems disallow directory fsync.
            pass
        finally:
            os.close(dir_fd)

    def _load_record(self, chain_id: int, evm_nonce: int) -> Optional[CustodyTxRecord]:
        path = self._record_path(chain_id, evm_nonce)
        if not path.exists():
            return None
        try:
            return CustodyTxRecord.from_dict(json.loads(path.read_text()))
        except Exception as exc:
            logger.critical("Corrupt custody-tx record %s — refusing to drop blocking state", path)
            raise CorruptCustodyTxRecordError(str(path)) from exc

    def _load_all_records(self, chain_id: Optional[int] = None) -> List[CustodyTxRecord]:
        # Filename layout `custody_tx_{chain_id}_{nonce}.json` lets a per-chain
        # caller skip the full state-dir scan: each chain loop polls at 1 Hz, so
        # globbing every chain's files on every tick is N×M file reads for N
        # chains × M records.
        pattern = (
            "custody_tx_[0-9]*.json" if chain_id is None else f"custody_tx_{int(chain_id)}_*.json"
        )
        records: List[CustodyTxRecord] = []
        for path in self._state_dir.glob(pattern):
            # Sidecar files such as the clear-watcher cursor
            # (`custody_tx_clear_cursor.json`) live in this same state dir and
            # share the `custody_tx_` prefix; only `custody_tx_<int>_<int>.json`
            # files are records. Skip anything else so a sidecar is never
            # mis-parsed into a fatal CorruptCustodyTxRecordError.
            if not re.fullmatch(r"custody_tx_\d+_\d+\.json", path.name):
                continue
            try:
                payload = path.read_text()
            except FileNotFoundError:
                # Raced with an unlink between glob and read; treat as absent.
                continue
            try:
                records.append(CustodyTxRecord.from_dict(json.loads(payload)))
            except Exception as exc:
                logger.critical(
                    "Corrupt custody-tx record %s — refusing to drop blocking state",
                    path,
                )
                raise CorruptCustodyTxRecordError(str(path)) from exc
        return records

    def _mark_status(
        self,
        record: CustodyTxRecord,
        status: CustodyTxStatus,
        error: Optional[str] = None,
    ) -> None:
        record.status = status
        if error is not None:
            record.error = error
        self._save_record(record)
        if status in TERMINAL_STATUSES:
            key = self._record_key(record.chain_id, record.evm_nonce)
            event = self._resolution_events.get(key)
            if event is not None:
                event.set()

    def _mark_recovered(
        self,
        record: CustodyTxRecord,
        decision: PreflightDecision,
        *,
        pin_tx_hash: bool,
    ) -> None:
        """Promote a record to SUCCESS via duplicate-id recovery.

        ``pin_tx_hash=True`` on the preflight path (no broadcast happened,
        populate tx_hash/receipt fields from the matched event).
        ``pin_tx_hash=False`` on the receipt-race path so the reverted
        broadcast's tx_hash + receipt_status stay pinned for forensics.
        """
        record.recovered_tx_hash = decision.recovered_tx_hash
        record.recovered_block_number = decision.recovered_block_number
        if pin_tx_hash:
            record.tx_hash = decision.recovered_tx_hash
            record.receipt_block_number = decision.recovered_block_number or 0
            record.receipt_status = 1
        record.error = None
        self._mark_status(record, CustodyTxStatus.SUCCESS)


_executor_instance: Optional[CustodyTxExecutor] = None


def get_custody_tx_executor(
    accounting_service: Optional[CustodyTxAccountingProtocol] = None,
) -> CustodyTxExecutor:
    global _executor_instance
    if _executor_instance is None:
        if accounting_service is None:
            from src.services.accounting_contract import get_accounting_contract_service

            accounting_service = get_accounting_contract_service()
        # Managed chains = every RPC-configured chain, so the active Sapphire
        # net (testnet 23295 or mainnet 23294) and Base are picked up from
        # config — no hardcoded chain id. Keys of chain_rpc_urls are ints.
        _executor_instance = CustodyTxExecutor(
            accounting_service,
            chain_ids=tuple(sorted(accounting_service.settings.chain_rpc_urls.keys())),
        )
    return _executor_instance


def reset_custody_tx_executor() -> None:
    """Test-only: clear the singleton."""
    global _executor_instance
    _executor_instance = None
