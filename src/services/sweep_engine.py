"""Sweep engine: state machine for sweeping deposit addresses to the encumbered wallet.

State machine per deposit (records keyed by the unique 32-byte deposit_id;
sweeps for the same address serialize on the per-address lock):
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

from src.clients.rofl import TransactionRevertedError
from src.config.chain_config import GAS_FUNDING_AMOUNT_WEI
from src.services.l2_fee_estimator import estimate_l1_data_fee
from src.services.rpc_identity import verified_web3

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

    Formalizes the 10 methods used by SweepEngine and DepositProcessor.
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

    async def get_token_id(self, chain_id: int, token_address: str | None) -> bytes: ...

    async def get_deposit_address(
        self,
        chain_type: str,
        version: int,
        siwe_token: bytes,
    ) -> str: ...

    async def is_token_registered(self, token_id: bytes) -> bool: ...

    async def is_deposit_processed(self, deposit_id: bytes) -> bool: ...


def _to_hex(value) -> str:
    """Convert bytes or str to 0x-prefixed hex string."""
    if isinstance(value, bytes):
        return "0x" + value.hex()
    return value


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
    amount: int = 0
    token_id_hex: str = ""  # hex-encoded bytes32
    token_address: Optional[str] = None  # None for native
    deposit_id_hex: str = ""  # hex-encoded bytes32
    source_tx_hash: str = ""  # deposit tx on source chain
    deposit_index: int = 0
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
        # Track gas funding tx hashes to exclude from deposit verification
        self._gas_funding_tx_hashes: Set[str] = set()
        # Recovery loop state
        self._recovery_running = False
        self._recovery_task: Optional[asyncio.Task] = None

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
        """Get the verified client for a chain.

        Startup narrows the served chains to endpoints that proved their chain ID
        (see `rpc_identity`). Sweeps broadcast signed transactions, so serving an
        excluded chain would move funds on a chain the signature was not meant
        for; refusing outright is the safe half of that trade.
        """
        if chain_id not in self._web3_cache:
            w3 = verified_web3(chain_id, self._chain_rpc_urls)
            if w3 is None:
                raise ValueError(f"No verified RPC endpoint for chain {chain_id}")
            self._web3_cache[chain_id] = w3
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
                self._delete_record(record.deposit_id_hex)

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
                self._delete_record(record.deposit_id_hex)

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

    def load_incomplete_sweeps(self) -> list[SweepRecord]:
        """Load all non-idle sweep records (for restart recovery).

        Also re-populates gas_funding_tx_hashes from persisted records
        to prevent gas funding txs from being claimed as deposits after restart.

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
                if record.gas_funding_tx_hash:
                    self._gas_funding_tx_hashes.add(record.gas_funding_tx_hash.lower())
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
        if record.state == SweepState.SWEPT:
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

        Safe because no tx was broadcast: no nonce-collision risk, and
        sweep_native/sweep_erc20 short-circuit on balance=0 + is_deposit_processed
        if the original task resolved it concurrently.
        """
        token_id = bytes.fromhex(record.token_id_hex.removeprefix("0x"))
        deposit_id = bytes.fromhex(record.deposit_id_hex.removeprefix("0x"))

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
                    # Unmined or reverted sweep tx still owns the deposit-address
                    # nonce — re-sweeping risks collision.
                    failed += 1
                    logger.warning(
                        "Incomplete sweep in state %s with pending sweep_tx=%s for %s on chain %d — requires manual investigation",
                        record.state.value,
                        record.sweep_tx_hash,
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

            # sweep_tx_hash is persisted BEFORE the SWEPT flip, so a non-SWEPT
            # record with sweep_tx_hash set means the deposit nonce is encumbered.
            swept = [r for r in records if r.state == SweepState.SWEPT]
            resumable = [r for r in records if r.state != SweepState.SWEPT and not r.sweep_tx_hash]
            stuck = [r for r in records if r.state != SweepState.SWEPT and r.sweep_tx_hash]

            for record in stuck:
                logger.warning(
                    "Recovery loop: stuck sweep in state %s with pending sweep_tx=%s for %s on chain %d — requires manual investigation",
                    record.state.value,
                    record.sweep_tx_hash,
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
