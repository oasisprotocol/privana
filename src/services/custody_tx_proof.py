"""Per-kind proof that refuses to mark a ``MarkSuccessWithHash`` clear
SUCCESS unless the vouched on-chain tx matches the record's effect.

Bridge kinds reuse the duplicate-id verifiers; ``SAPPHIRE_RELEASE``
checks the tx shape; ``NORMAL_WITHDRAWAL`` is refused.
"""

from __future__ import annotations

from typing import Any

from hexbytes import HexBytes

from src.config.bridge import SAPPHIRE_RELEASE_GAS_LIMIT
from src.services.custody_tx_executor import PreflightOutcome, is_transient_rpc_error


def _same_hash(a: Any, b: Any) -> bool:
    try:
        return HexBytes(a) == HexBytes(b)
    except Exception:
        return False


def _same_address(a: Any, b: Any) -> bool:
    na, nb = str(a or "").lower(), str(b or "").lower()
    return bool(na) and na == nb


async def verify_mark_success(
    executor: Any,
    record: Any,
    w3: Any,
    vouched_tx_hash: Any,
) -> tuple[str, str]:
    """Return ``(result, reason)`` with ``result`` in {ok, refused, deferred}."""
    kind = record.kind.value

    if kind == "base_mint":
        if record.withdrawal_index is None:
            return ("refused", "base_mint record missing withdrawal_index")
        decision = await executor._verify_recovered_mint(
            record, w3, executor._compute_withdrawal_id(record)
        )
        return _mark_recovered_gate(decision, vouched_tx_hash, label="Minted")

    if kind == "xrose_burn":
        try:
            deposit_id = bytes(HexBytes(record.id))
        except Exception:
            return ("refused", f"xrose burn id unreadable: {record.id!r}")
        if len(deposit_id) != 32:
            return ("refused", f"xrose burn depositId must be 32 bytes (got {len(deposit_id)})")
        decision = await executor._verify_recovered_burn(record, w3, deposit_id)
        return _mark_recovered_gate(decision, vouched_tx_hash, label="Burned")

    if kind == "sapphire_release":
        return await _verify_sapphire_release(record, w3, vouched_tx_hash)

    if kind == "normal_withdrawal":
        return (
            "refused",
            "MarkSuccessWithHash refused for NORMAL_WITHDRAWAL: "
            "destination-tx shape is not reconstructable",
        )

    return ("refused", f"MarkSuccessWithHash unsupported for kind={kind}")


def _mark_recovered_gate(decision: Any, vouched_tx_hash: Any, *, label: str) -> tuple[str, str]:
    """Bridge kinds: accept only a MARK_RECOVERED whose matched tx hash equals
    the vouched hash. A RETRY_LATER (transient RPC inside the verifier) defers;
    any other outcome or a hash mismatch refuses."""
    if decision.outcome == PreflightOutcome.MARK_RECOVERED:
        if not _same_hash(decision.recovered_tx_hash, vouched_tx_hash):
            return (
                "refused",
                f"{label} recovered tx {decision.recovered_tx_hash} != vouched {vouched_tx_hash!r}",
            )
        return ("ok", f"{label} event matched at vouched tx")
    if decision.outcome == PreflightOutcome.RETRY_LATER:
        return ("deferred", decision.error or f"{label} verifier transient failure")
    return ("refused", decision.error or f"{label} verifier did not recover ({decision.outcome})")


async def _verify_sapphire_release(record: Any, w3: Any, vouched_tx_hash: Any) -> tuple[str, str]:
    if (
        record.amount is None
        or record.max_gas_cost is None
        or record.to_address is None
        or record.evm_sender is None
        or record.evm_nonce is None
    ):
        return ("refused", "sapphire release record missing required field for shape check")

    try:
        tx = await w3.eth.get_transaction(vouched_tx_hash)
        receipt = await w3.eth.get_transaction_receipt(vouched_tx_hash)
    except Exception as exc:
        if is_transient_rpc_error(exc):
            return ("deferred", f"transient: vouched tx lookup failed: {exc}")
        return ("refused", f"vouched tx lookup failed: {exc}")

    if tx is None or receipt is None:
        return ("refused", f"vouched tx not found: {vouched_tx_hash!r}")

    if int(receipt["status"]) != 1:
        return ("refused", f"vouched tx reverted (receipt status={receipt['status']})")

    if not _same_address(tx.get("to"), record.to_address):
        return ("refused", f"vouched tx to={tx.get('to')} != record to={record.to_address}")

    expected_value = int(record.amount) - int(record.max_gas_cost)
    if int(tx.get("value")) != expected_value:
        return (
            "refused",
            f"vouched tx value={tx.get('value')} != amount-max_gas_cost={expected_value}",
        )

    data = tx.get("input")
    if data is None:
        data = tx.get("data")
    if bytes(HexBytes(data or b"")):
        return (
            "refused",
            f"vouched tx carries calldata (expected empty): {HexBytes(data).to_0x_hex()}",
        )

    if int(tx.get("gas")) != int(SAPPHIRE_RELEASE_GAS_LIMIT):
        return ("refused", f"vouched tx gas={tx.get('gas')} != {SAPPHIRE_RELEASE_GAS_LIMIT}")

    if not _same_address(tx.get("from"), record.evm_sender):
        return ("refused", f"vouched tx from={tx.get('from')} != record sender={record.evm_sender}")

    if int(tx.get("nonce")) != int(record.evm_nonce):
        return ("refused", f"vouched tx nonce={tx.get('nonce')} != record nonce={record.evm_nonce}")

    return ("ok", "sapphire release tx shape matched")
