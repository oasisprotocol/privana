"""Sweep engine: state machine for sweeping deposit addresses to the encumbered wallet.

State machine per deposit (one record per token and deposit address, with a random
32-byte deposit_id; sweeps for the same address serialize on the per-address lock):
    (no record) → PENDING → GAS_FUNDED → SWEPT → (credit) → record deleted

    SWEPT is only set after the sweep tx is confirmed on-chain (receipt status=1).
    On failure: record stays in current state for recovery or manual investigation.
    On ROFL restart / periodic recovery:
      - SWEPT → retry credit (idempotent via DepositAlreadyProcessed).
      - GAS_FUNDED with mined sweep_tx_hash → promoted to SWEPT, then credit.
      - PENDING / GAS_FUNDED with NO sweep_tx_hash → re-run sweep from scratch
        (no broadcast tx means no nonce-collision risk).
      - GAS_FUNDED with a sweep_tx_hash the node does not know → the same signed tx
        is broadcast again; the hash is never cleared and the sweep never re-signed.
        A reverted tx → logged for manual investigation.

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
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from web3 import AsyncWeb3, Web3
from web3.exceptions import TransactionNotFound

from src.clients.rofl import TransactionRevertedError
from src.config.chain_config import (
    GAS_FUNDING_AMOUNT_WEI,
    GAS_FUNDING_HEADROOM,
    MIN_DEPOSIT_ERC20_WEI,
    MIN_DEPOSIT_NATIVE_WEI,
    MIN_SWEEP_GAS_PRICE_WEI,
    SWEEP_GAS_LIMIT_ERC20,
    SWEEP_GAS_LIMIT_NATIVE,
    get_finality_depth,
)
from src.services.l2_fee_estimator import estimate_l1_data_fee
from src.services.rpc_identity import require_verified_web3

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


# Interval for periodic retry of SWEPT records whose credit failed.
# Longer than withdrawal polling (12s) because stuck sweeps are rare edge cases.
SWEEP_RECOVERY_INTERVAL = 60


@runtime_checkable
class DepositAccountingProtocol(Protocol):
    """Interface expected by deposit processing from AccountingContractService.

    Formalizes the 8 methods used by SweepEngine and DepositProcessor.
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

    async def get_deposit_address(
        self,
        chain_type: str,
        version: int,
        siwe_token: bytes,
    ) -> str: ...

    async def is_deposit_processed(self, deposit_id: bytes) -> bool: ...


def _to_hex(value) -> str:
    """Convert bytes or str to 0x-prefixed hex string."""
    if isinstance(value, bytes):
        return "0x" + value.hex()
    return value


class BalanceClaimStatus(str, Enum):
    """Outcome of claim_balance().

    STARTED:   a new sweep record was created; the caller drives it.
    IN_FLIGHT: an unfinished sweep of this asset already exists.
    BUSY:      another sweep holds the address lock; try again shortly.
    NONE:      nothing claimable.
    """

    STARTED = "started"
    IN_FLIGHT = "in_flight"
    BUSY = "busy"
    NONE = "none"


@dataclass(frozen=True)
class BalanceClaim:
    """Result of claim_balance().

    ``amount`` is the balance seen, for the response only (the sweep decides
    its own).
    """

    status: BalanceClaimStatus
    record: Optional["SweepRecord"] = None
    amount: int = 0


DEFAULT_STATE_DIR = os.getenv("SWEEP_STATE_DIR", "/data/sweep-engine")


class SweepState(str, Enum):
    PENDING = "pending"
    GAS_FUNDED = "gas_funded"
    SWEPT = "swept"


@dataclass
class SweepRecord:
    """Persistent state for a sweep in progress."""

    deposit_address: str
    chain_id: int
    state: SweepState
    beneficiary: str
    chain_type: str
    version: int
    amount: Optional[int] = None  # populated when the sweep is complete
    token_id_hex: str = ""  # hex-encoded bytes32
    token_address: Optional[str] = None  # None for native
    deposit_id_hex: str = ""  # hex-encoded bytes32
    source_tx_hash: str = ""  # deposit tx on source chain
    deposit_index: int = 0
    sweep_tx: Optional[str] = None  # signed raw sweep tx, rebroadcast if the node loses it
    sweep_tx_hash: Optional[str] = None  # used for convenience
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

    @classmethod
    def from_dict(cls, data: dict) -> "SweepRecord":
        data = dict(data)
        data["state"] = SweepState(data["state"])
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


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
    ):
        self._accounting = accounting_service
        self._chain_rpc_urls = chain_rpc_urls
        self._state_dir = Path(state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._web3_cache: Dict[int, AsyncWeb3] = {}
        # Per-address lock: prevents concurrent sweeps for the same deposit address
        self._address_locks: Dict[str, asyncio.Lock] = {}
        # Global lock: serializes gas tank nonce reads across all concurrent sweeps
        self._gas_tank_lock = asyncio.Lock()
        # Recovery loop state
        self._recovery_running = False
        self._recovery_task: Optional[asyncio.Task] = None

    def _get_address_lock(self, deposit_address: str, chain_id: int) -> asyncio.Lock:
        """Get or create the per-address lock for concurrent sweep protection."""
        key = f"{deposit_address.lower()}_{chain_id}"
        if key not in self._address_locks:
            self._address_locks[key] = asyncio.Lock()
        return self._address_locks[key]

    def _get_web3(self, chain_id: int) -> AsyncWeb3:
        """Get the chain's startup-verified client (see `rpc_identity`).

        Sweeps broadcast signed transactions, so serving an excluded chain would move
        funds on a chain the signature was never meant for.
        """
        return require_verified_web3(chain_id, self._chain_rpc_urls, self._web3_cache)

    def _recheck_served(self, chain_id: int) -> None:
        """Raise if re-verification dropped ``chain_id`` while a sweep held its client.

        A sweep keeps one client across the gas-tank lock and receipt waits. Checked
        before each broadcast and before a receipt promotes the record to SWEPT, in
        the sweep and in reconciliation: a receipt from a dropped endpoint would
        otherwise credit the deposit and delete the record. Raising leaves the record
        for the next recovery pass.
        """
        self._get_web3(chain_id)

    async def _get_safe_gas_price(self, w3: AsyncWeb3, chain_id: int) -> int:
        """Gas price safe from underpricing: max of 1.25x baseFeePerGas,
        eth_gasPrice (stale/low on L2s), and a per-chain floor.

        Legacy txs have no fee-bump path, so 1.25x buys one max-full block of
        base-fee drift before inclusion. The floor covers a partial signal;
        with none at all we raise rather than broadcast a price that may never
        mine and wedge the shared gas-tank lock.
        """
        latest_block = await w3.eth.get_block("latest")
        base_fee = latest_block.get("baseFeePerGas") or 0  # explicit null on some RPCs
        rpc_gas_price = await w3.eth.gas_price
        if base_fee == 0 and rpc_gas_price == 0:
            raise ValueError(f"chain {chain_id}: RPC returned no gas price signal")
        gas_price = max(base_fee * 5 // 4, rpc_gas_price, MIN_SWEEP_GAS_PRICE_WEI[chain_id])
        logger.debug(
            "Gas price: chain=%d base_fee=%d rpc=%d chosen=%d",
            chain_id,
            base_fee,
            rpc_gas_price,
            gas_price,
        )
        return gas_price

    def _record_path(self, deposit_id_hex: str) -> Path:
        # deposit_id is the unique 32-byte id, so one file per deposit: two
        # deposits to the same address can never clobber each other's state.
        # Concurrency stays with the per-address asyncio lock, not the file key.
        # Strict validation keeps caller-supplied ids from escaping the
        # state dir via path separators in the filename.
        key = deposit_id_hex.lower().removeprefix("0x")
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError(f"deposit_id_hex must be 32 bytes of hex, got {deposit_id_hex!r}")
        return self._state_dir / f"sweep_{key}.json"

    def _save_record(self, record: SweepRecord) -> None:
        path = self._record_path(record.deposit_id_hex)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record.to_dict(), indent=2))
        os.replace(str(tmp), str(path))

    def _delete_record(self, deposit_id_hex: str) -> None:
        self._record_path(deposit_id_hex).unlink(missing_ok=True)

    def get_record(self, token_id_hex: str, deposit_address: str) -> Optional[SweepRecord]:
        """The unfinished sweep of one token at one deposit address, if any."""
        for path in self._state_dir.glob("sweep_*.json"):
            try:
                record = SweepRecord.from_dict(json.loads(path.read_text()))
            except Exception:
                continue  # load_incomplete_sweeps quarantines corrupt files
            if (
                record.token_id_hex.lower() == token_id_hex.lower()
                and record.deposit_address.lower() == deposit_address.lower()
            ):
                return record
        return None

    def get_record_by_deposit_id(self, deposit_id_hex: str) -> Optional[SweepRecord]:
        """Look up a SweepRecord by deposit_id_hex (used by polling endpoint).

        Malformed ids read as not-found rather than raising: this sits on the
        status-polling path, where a bad id is a client error, not ours.
        """
        try:
            path = self._record_path(deposit_id_hex)
        except ValueError:
            return None
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return SweepRecord.from_dict(data)

    def cleanup_record(self, deposit_id_hex: str) -> None:
        """Delete a sweep record by deposit_id (public API for DepositProcessor)."""
        try:
            self._delete_record(deposit_id_hex)
        except ValueError:
            return

    def persist_error(self, deposit_id_hex: str, error: str) -> None:
        """Mark a sweep record as failed (public API for DepositProcessor).

        Skip if a sweep tx was already broadcast: the record is still
        recoverable (reconciliation promotes a mined tx to SWEPT), so it
        must not be flagged as terminally errored.
        """
        record = self.get_record_by_deposit_id(deposit_id_hex)
        if record is None:
            return
        if record.sweep_tx_hash:
            logger.warning(
                "Skipping error persist after sweep tx broadcast: deposit_id=%s sweep_tx=%s error=%s",
                deposit_id_hex,
                record.sweep_tx_hash,
                error,
            )
            return
        record.error = error
        self._save_record(record)

    async def _get_balance(self, w3: AsyncWeb3, token_address: Optional[str], holder: str) -> int:
        """Balance of ``holder``; ``token_address`` None is the native balance."""
        if token_address is None:
            return await w3.eth.get_balance(holder)
        return await self._get_erc20_balance(w3, token_address, holder)

    async def claim_balance(
        self,
        *,
        deposit_address: str,
        beneficiary: str,
        chain_type: str,
        version: int,
        chain_id: int,
        token_address: Optional[str],  # None for the native token
        token_id: bytes,
    ) -> BalanceClaim:
        """Schedule a sweep of the whole balance of one asset at a deposit address."""
        if self._get_address_lock(deposit_address, chain_id).locked():
            # A sweep (maybe of another token) holds the address; waiting for it would
            # keep the request open for the whole sweep.
            return BalanceClaim(BalanceClaimStatus.BUSY)

        w3 = self._get_web3(chain_id)
        sweepable = await self._get_balance(w3, token_address, deposit_address)

        # No await from here to the save, so no other claim can schedule the same asset meanwhile.
        existing = self.get_record(_to_hex(token_id), deposit_address)
        if existing is not None:
            if not existing.error or existing.sweep_tx_hash:
                return BalanceClaim(BalanceClaimStatus.IN_FLIGHT, existing)
            # Failed before any sweep tx existed: nothing on-chain to orphan, clean it up and create a new one below.
            self._delete_record(existing.deposit_id_hex)

        minimums = MIN_DEPOSIT_NATIVE_WEI if token_address is None else MIN_DEPOSIT_ERC20_WEI
        if sweepable == 0 or sweepable < minimums.get(chain_id, 0):
            return BalanceClaim(BalanceClaimStatus.NONE, amount=sweepable)

        record = SweepRecord(
            deposit_address=deposit_address,
            chain_id=chain_id,
            state=SweepState.PENDING,
            beneficiary=beneficiary,
            chain_type=chain_type,
            version=version,
            token_id_hex=_to_hex(token_id),
            token_address=token_address,
            deposit_id_hex=_to_hex(os.urandom(32)),
        )
        self._save_record(record)
        return BalanceClaim(BalanceClaimStatus.STARTED, record, amount=sweepable)

    async def sweep(self, deposit_id: bytes) -> None:
        """Execute the full sweep of a deposit: gas fund → sweep → confirm → credit."""
        deposit_id_hex = _to_hex(deposit_id)
        record = self.get_record_by_deposit_id(deposit_id_hex)
        if record is None:
            return
        lock = self._get_address_lock(record.deposit_address, record.chain_id)
        async with lock:
            # Re-read under the lock: a record another driver already settled is gone, and
            # one that already holds a sweep tx is left to reconciliation.
            record = self.get_record_by_deposit_id(deposit_id_hex)
            if record is None or record.state == SweepState.SWEPT or record.sweep_tx_hash:
                return
            deposit_address = record.deposit_address
            chain_id = record.chain_id
            w3 = self._get_web3(chain_id)
            is_native = record.token_address is None

            try:
                gas_price = await self._get_safe_gas_price(w3, chain_id)

                # Step 1: Fund the gas budget once per claim; the operator pays the fee.
                # Over-fund the expected sweep cost: the sweep re-prices after
                # funding confirms, so exact funding could fall short and cost a
                # second round; excess is dust. Capped per chain.
                l1_data_fee = await estimate_l1_data_fee(w3, chain_id, is_erc20=not is_native)
                gas_limit = SWEEP_GAS_LIMIT_NATIVE if is_native else SWEEP_GAS_LIMIT_ERC20
                gas_amount = min(
                    gas_limit * gas_price * GAS_FUNDING_HEADROOM + l1_data_fee,
                    GAS_FUNDING_AMOUNT_WEI[chain_id],
                )

                if not record.gas_funding_tx_hash:
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

                        self._recheck_served(chain_id)
                        gas_tx_hash = await w3.eth.send_raw_transaction(gas_tx)

                        gas_tx_hash_hex = _to_hex(gas_tx_hash)
                        record.gas_funding_tx_hash = gas_tx_hash_hex
                        record.state = SweepState.GAS_FUNDED
                        self._save_record(record)

                        logger.info(
                            "Gas funding broadcast: chain=%d tx=%s", chain_id, gas_tx_hash_hex
                        )

                        gas_receipt = await self._wait_for_receipt(w3, gas_tx_hash)
                        if gas_receipt["status"] != 1:
                            raise ValueError(f"Gas funding tx reverted: {gas_tx_hash_hex}")

                # Step 2: Sweep (re-read gas price — may have changed). The amount is decided
                # now: the whole balance, native leaving the gas budget behind to pay the fee.
                amount = await self._get_balance(w3, record.token_address, deposit_address)
                if is_native:
                    amount -= gas_amount
                if amount <= 0:
                    self._delete_record(deposit_id_hex)
                    raise ValueError(
                        f"Nothing to sweep at {deposit_address} on chain {chain_id}; claim again"
                    )
                gas_price = await self._get_safe_gas_price(w3, chain_id)

                nonce = await w3.eth.get_transaction_count(deposit_address)

                if is_native:
                    signed_tx = await self._accounting.generate_sweep_native(
                        beneficiary=record.beneficiary,
                        chain_type=record.chain_type,
                        version=record.version,
                        chain_id=chain_id,
                        amount=amount,
                        nonce=nonce,
                        gas_price=gas_price,
                    )
                else:
                    signed_tx = await self._accounting.generate_sweep_erc20(
                        beneficiary=record.beneficiary,
                        chain_type=record.chain_type,
                        version=record.version,
                        chain_id=chain_id,
                        token_address=record.token_address,
                        amount=amount,
                        nonce=nonce,
                        gas_price=gas_price,
                    )

                record.amount = amount
                record.sweep_tx = _to_hex(signed_tx)
                record.sweep_tx_hash = _to_hex(Web3.keccak(signed_tx))
                self._save_record(record)

                logger.info("Sweep broadcasting: chain=%d tx=%s", chain_id, record.sweep_tx_hash)
                self._recheck_served(chain_id)
                sweep_tx_hash = await w3.eth.send_raw_transaction(signed_tx)
                logger.info("Sweep broadcast: chain=%d tx=%s", chain_id, _to_hex(sweep_tx_hash))

                receipt = await self._wait_for_final_receipt(w3, sweep_tx_hash, chain_id)
                if receipt["status"] != 1:
                    raise ValueError(f"Sweep tx reverted: {record.sweep_tx_hash}")

                self._recheck_served(chain_id)
                record.state = SweepState.SWEPT
                self._save_record(record)

                # Step 3: Credit
                try:
                    await self._idempotent_credit(
                        beneficiary=record.beneficiary,
                        token_id=bytes.fromhex(record.token_id_hex.removeprefix("0x")),
                        amount=amount,
                        deposit_id=deposit_id,
                    )
                except Exception as exc:
                    logger.exception(
                        "Credit failed after successful sweep for %s on chain %d",
                        deposit_address,
                        chain_id,
                    )
                    raise SweepCreditPendingError(record.deposit_id_hex, exc) from exc

                logger.info(
                    "Deposit credited after sweep: beneficiary=%s amount=%d",
                    record.beneficiary,
                    amount,
                )

                # Only clean up on full success — partial failures (sweep ok, credit failed)
                # leave the record for recovery
                self._delete_record(record.deposit_id_hex)

            except SweepCreditPendingError:
                raise
            except Exception:
                logger.exception("Sweep failed for %s on chain %d", deposit_address, chain_id)
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

    def load_incomplete_sweeps(self) -> list[SweepRecord]:
        """Load all non-idle sweep records (for restart recovery).

        Migrates legacy address-keyed files (sweep_<address>_<chain>.json) to
        the deposit_id key, unlinking the old path so it can't resurrect on
        the next restart.
        """
        records = []
        # Materialize the glob before renaming inside the loop: a lazy
        # iterator could yield a just-migrated file a second time.
        for path in sorted(self._state_dir.glob("sweep_*.json")):
            try:
                data = json.loads(path.read_text())
                record = SweepRecord.from_dict(data)
                expected = self._record_path(record.deposit_id_hex)
                if path != expected:
                    if expected.exists():
                        # A deposit_id-keyed copy already exists; it was
                        # written by newer code, so the legacy file is the
                        # duplicate to drop — and the kept copy is loaded by
                        # its own glob entry, so don't double-count it here.
                        path.unlink()
                        continue
                    path.rename(expected)
                records.append(record)
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
        """Promote a record to SWEPT if its sweep tx is already mined and final on-chain.

        Closes the crash window between sweep broadcast and the state flip:
        sweep persists sweep_tx_hash *before* flipping state
        to SWEPT (see the "persist tx hash for crash recovery" save). A crash
        during _wait_for_receipt would otherwise leave the record as GAS_FUNDED
        with a valid, possibly-mined tx hash — and without reconciliation the
        funds would be swept but never credited.

        Safe: only inspects an existing tx hash, never re-signs or re-broadcasts,
        so the nonce-collision concern that blocks re-sweeping does not apply.
        Mutates record in place and persists when promoted; leaves it untouched
        on any failure so the next pass can try again.
        """
        if record.state == SweepState.SWEPT:
            return
        if not record.sweep_tx_hash:
            return

        try:
            w3 = self._get_web3(record.chain_id)
            latest = (await w3.eth.get_block("latest"))["number"]
            receipt = await w3.eth.get_transaction_receipt(record.sweep_tx_hash)
            self._recheck_served(record.chain_id)
            if latest - receipt["blockNumber"] < get_finality_depth(record.chain_id):
                return  # Mined but not final yet: a later pass credits it.
        except TransactionNotFound:
            # The node does not know the tx. Send the same signed tx again: it keeps its hash,
            # amount and nonce, so it cannot collide with another sweep, and a node that
            # already has it just says so. Clearing the hash and re-signing could credit
            # nothing if the first tx mines after all.
            if record.sweep_tx:
                try:
                    self._recheck_served(record.chain_id)
                    await w3.eth.send_raw_transaction(
                        bytes.fromhex(record.sweep_tx.removeprefix("0x"))
                    )
                    logger.warning(
                        "Reconcile: rebroadcast sweep tx %s for %s on chain %d",
                        record.sweep_tx_hash,
                        record.deposit_address,
                        record.chain_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Reconcile: rebroadcast of sweep tx %s failed: %s",
                        record.sweep_tx_hash,
                        exc,
                    )
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
            record.state = SweepState.SWEPT
            self._save_record(record)
            logger.warning(
                "Reconcile: sweep tx %s mined during crash window — promoted to SWEPT for %s on chain %d",
                record.sweep_tx_hash,
                record.deposit_address,
                record.chain_id,
            )
        else:
            logger.error(
                "Reconcile: sweep tx %s reverted on chain for %s on chain %d — manual investigation required",
                record.sweep_tx_hash,
                record.deposit_address,
                record.chain_id,
            )

    def _persist_resume_error(self, record: SweepRecord, exc: Exception) -> None:
        """Flag a resumable record as errored so a user retry can cleanup and restart.

        Skip if a sweep tx was broadcast mid-attempt: user retry would then
        cleanup_record and orphan the pending tx. Re-read disk — the local
        record is stale after _resume_sweep_from_pending ran.
        """
        current = self.get_record_by_deposit_id(record.deposit_id_hex)
        if current is None or current.sweep_tx_hash:
            return
        current.error = str(exc)
        self._save_record(current)

    async def _resume_sweep_from_pending(self, record: SweepRecord) -> None:
        """Re-run sweep for a PENDING/GAS_FUNDED record with no broadcast sweep tx.

        Safe because no tx was broadcast: no nonce-collision risk, and the sweep re-reads
        the record under the lock, so one another driver already settled is skipped.
        """
        deposit_id = bytes.fromhex(record.deposit_id_hex.removeprefix("0x"))

        await self.sweep(deposit_id)

    async def resume_incomplete_sweeps(self) -> None:
        """Resume incomplete sweeps after ROFL restart.

        Called from lifespan startup. Each record is first reconciled on-chain
        (GAS_FUNDED with a mined sweep_tx_hash → promoted to SWEPT), then:
        - SWEPT: retry creditDeposit (idempotent via DepositAlreadyProcessed).
        - PENDING/GAS_FUNDED with no sweep_tx_hash: re-run sweep from scratch.
        - Else (sweep_tx_hash set, unmined/dropped/reverted): log, skip —
          the deposit-address nonce may still be encumbered.
        """
        records = self.load_incomplete_sweeps()
        if not records:
            return

        logger.info("Found %d incomplete sweep(s) to resume", len(records))

        for record in records:
            await self._reconcile_sweep_tx(record)

        succeeded = 0
        failed = 0
        for record in records:
            try:
                if record.state == SweepState.SWEPT:
                    # Sweep confirmed but credit didn't complete — retry credit
                    token_id = bytes.fromhex(record.token_id_hex.removeprefix("0x"))
                    deposit_id = bytes.fromhex(record.deposit_id_hex.removeprefix("0x"))

                    await self._idempotent_credit(
                        beneficiary=record.beneficiary,
                        token_id=token_id,
                        amount=record.amount,
                        deposit_id=deposit_id,
                    )
                    self._delete_record(record.deposit_id_hex)
                    succeeded += 1
                    logger.info(
                        "Recovered sweep for %s on chain %d — credit completed",
                        record.deposit_address,
                        record.chain_id,
                    )
                elif not record.sweep_tx_hash:
                    await self._resume_sweep_from_pending(record)
                    succeeded += 1
                    logger.info(
                        "Resumed sweep from state=%s for %s on chain %d",
                        record.state.value,
                        record.deposit_address,
                        record.chain_id,
                    )
                else:
                    # The sweep tx was broadcast: reconciliation credits it once it is final
                    # (and rebroadcasts it if the node lost it), so it is never re-signed.
                    # A reverted tx is reported by the reconciliation.
                    logger.info(
                        "Sweep tx %s in state %s for %s on chain %d is not credited yet - the recovery loop checks it again",
                        record.sweep_tx_hash,
                        record.state.value,
                        record.deposit_address,
                        record.chain_id,
                    )
            except SweepCreditPendingError:
                # Record is SWEPT on disk; next pass retries credit. Don't flag as errored.
                succeeded += 1
                logger.warning(
                    "Resume: credit pending for %s on chain %d — recovery loop will retry",
                    record.deposit_address,
                    record.chain_id,
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
                "Sweep recovery: %d/%d succeeded, %d failed — uncredited deposits need attention",
                succeeded,
                len(records),
                failed,
            )

    # ------------------------------------------------------------------
    # Periodic recovery loop — retries SWEPT records that failed credit
    # ------------------------------------------------------------------

    async def _run_recovery_loop(self) -> None:
        """Periodically retry credit for SWEPT records.

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
                await self._reconcile_sweep_tx(record)

            # sweep_tx_hash is persisted BEFORE the SWEPT flip, so a non-SWEPT record with
            # sweep_tx_hash set is a broadcast sweep that reconciliation has not credited yet.
            swept = [r for r in records if r.state == SweepState.SWEPT]
            resumable = [r for r in records if r.state != SweepState.SWEPT and not r.sweep_tx_hash]
            waiting = [r for r in records if r.state != SweepState.SWEPT and r.sweep_tx_hash]

            for record in waiting:
                logger.info(
                    "Recovery loop: sweep tx %s in state %s for %s on chain %d is not credited yet",
                    record.sweep_tx_hash,
                    record.state.value,
                    record.deposit_address,
                    record.chain_id,
                )

            for record in resumable:
                try:
                    await self._resume_sweep_from_pending(record)
                    logger.info(
                        "Recovery loop: resumed sweep from state=%s for %s on chain %d",
                        record.state.value,
                        record.deposit_address,
                        record.chain_id,
                    )
                except SweepCreditPendingError:
                    logger.warning(
                        "Recovery loop: credit pending for %s on chain %d — next pass will retry",
                        record.deposit_address,
                        record.chain_id,
                    )
                except Exception as exc:
                    self._persist_resume_error(record, exc)
                    logger.exception(
                        "Recovery loop: resume failed for %s on chain %d — will retry in %ds",
                        record.deposit_address,
                        record.chain_id,
                        SWEEP_RECOVERY_INTERVAL,
                    )

            if not swept:
                continue

            logger.info("Recovery loop: retrying credit for %d SWEPT record(s)", len(swept))
            for record in swept:
                try:
                    token_id = bytes.fromhex(record.token_id_hex.removeprefix("0x"))
                    deposit_id = bytes.fromhex(record.deposit_id_hex.removeprefix("0x"))
                    await self._idempotent_credit(
                        beneficiary=record.beneficiary,
                        token_id=token_id,
                        amount=record.amount,
                        deposit_id=deposit_id,
                    )
                    self._delete_record(record.deposit_id_hex)
                    logger.info(
                        "Recovery loop: credit completed for %s on chain %d",
                        record.deposit_address,
                        record.chain_id,
                    )
                except Exception:
                    logger.exception(
                        "Recovery loop: credit retry failed for %s on chain %d — will retry in %ds",
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

    async def _wait_for_final_receipt(self, w3: AsyncWeb3, tx_hash, chain_id: int):
        """Poll for the receipt of ``tx_hash`` until its block is ``finality_depth`` deep.

        The credit that follows a sweep cannot be undone, so it must not rest on a block
        that can still be reorged out: with the sweep final, the deposit it spends is too.
        """
        depth = get_finality_depth(chain_id)
        deadline = time.time() + 120 + depth * 12
        last_error = None
        while time.time() < deadline:
            try:
                latest = (await w3.eth.get_block("latest"))["number"]
                receipt = await w3.eth.get_transaction_receipt(tx_hash)
                if receipt is not None and latest - receipt["blockNumber"] >= depth:
                    return receipt
            except (ConnectionError, TimeoutError, OSError, ValueError, TransactionNotFound) as exc:
                last_error = exc
                logger.warning("RPC error polling receipt for %s: %s", tx_hash, exc)
            await asyncio.sleep(2)
        msg = f"Transaction {tx_hash} not final within the timeout"
        if last_error:
            msg += f" (last RPC error: {last_error})"
        raise TimeoutError(msg)

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
