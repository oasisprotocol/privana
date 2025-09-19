"""Service for building and submitting Accounting contract transactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from web3 import Web3
from web3.contract import Contract

from src.abi.accounting import ACCOUNTING_ABI
from src.clients.rofl import RoflAppdClient
from src.config import load_settings
from src.models.types import Settings


def _ensure_hex(value: str) -> str:
    if value.startswith("0x"):
        return value
    return "0x" + value


def _to_checksum(address: str) -> ChecksumAddress:
    return Web3.to_checksum_address(address)


@dataclass
class SubmissionResult:
    """Plain DTO for transaction submission results."""

    submission_id: str
    status: str
    detail: Optional[str] = None


@dataclass
class TokenContext:
    """Derived metadata needed to craft withdrawal transactions."""

    chain_id: int
    token_address: Optional[ChecksumAddress]
    is_native: bool


class AccountingContractService:
    """Encapsulates Accounting contract interactions."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or load_settings()
        self.w3 = Web3()
        self.chain_id = self.settings.sapphire_chain_id
        self.gas_limit = self.settings.accounting_gas_limit
        self.contract_address = _to_checksum(self.settings.accounting_contract_address)
        self.contract: Contract = self.w3.eth.contract(
            address=self.contract_address, abi=ACCOUNTING_ABI
        )
        self.sapphire_rpc_url = self.settings.sapphire_rpc_url
        self.reader_w3: Optional[Web3] = (
            Web3(Web3.HTTPProvider(self.sapphire_rpc_url))
            if self.sapphire_rpc_url
            else None
        )
        self.contract_reader: Optional[Contract] = (
            self.reader_w3.eth.contract(
                address=self.contract_address, abi=ACCOUNTING_ABI
            )
            if self.reader_w3
            else None
        )
        self.rofl_client = RoflAppdClient()
        self.chain_rpc_urls: Dict[int, str] = dict(self.settings.chain_rpc_urls)
        self._chain_web3: Dict[int, Web3] = {}

    # ------------------------------------------------------------------
    # Helper utilities
    # ------------------------------------------------------------------
    def _build_tx(self, data: bytes, value: int = 0, gas: Optional[int] = None) -> Dict:
        gas_limit = gas or self.gas_limit
        tx = {
            "to": self.contract_address,
            "value": value,
            "gas": gas_limit,
            "data": Web3.to_hex(data),
        }
        return tx

    def _submit(
        self, data: bytes, value: int = 0, gas: Optional[int] = None
    ) -> SubmissionResult:
        tx = self._build_tx(data, value=value, gas=gas)
        submission_id = self.rofl_client.submit_tx(tx)
        return SubmissionResult(submission_id=submission_id, status="submitted")

    def _require_address(self, value: str, field: str) -> ChecksumAddress:
        if not isinstance(value, str) or not Web3.is_address(value):
            raise ValueError(f"Invalid {field} provided")
        return _to_checksum(value)

    def _require_positive(
        self, value: Any, field: str, allow_zero: bool = False
    ) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be numeric") from exc

        if allow_zero:
            if parsed < 0:
                raise ValueError(f"{field} must be zero or positive")
        else:
            if parsed <= 0:
                raise ValueError(f"{field} must be positive")
        return parsed

    def _require_hex(
        self, value: str, field: str, expected_len: Optional[int] = None
    ) -> HexBytes:
        try:
            data = HexBytes(_ensure_hex(value))
        except Exception as exc:
            raise ValueError(f"Invalid hex value for {field}") from exc

        if expected_len is not None and len(data) != expected_len:
            raise ValueError(f"{field} must be {expected_len} bytes")
        return data

    def _optional_hex(self, value: Optional[str], field: str) -> bytes:
        if value in (None, ""):
            return b""
        return bytes(self._require_hex(value, field))

    def _build_tx_proof(self, request_payload: Dict[str, Optional[str]]):
        return (
            self._optional_hex(
                request_payload.get("rlp_block_header"), "rlp_block_header"
            ),
            self._optional_hex(
                request_payload.get("transaction_index_rlp"), "transaction_index_rlp"
            ),
            self._optional_hex(
                request_payload.get("transaction_proof_stack"),
                "transaction_proof_stack",
            ),
        )

    def _get_deposit_address(self) -> str:
        """Fetch or derive the ROFL-managed address for deposits."""
        _, public_address = self.rofl_client.get_keypair()
        return public_address

    def _get_reader_contract(self) -> Contract:
        if self.contract_reader is None:
            raise ValueError(
                "SAPPHIRE_RPC_URL must be configured to perform withdrawal operations"
            )
        return self.contract_reader

    def _get_chain_web3(self, chain_id: int) -> Web3:
        if chain_id in self._chain_web3:
            return self._chain_web3[chain_id]

        rpc_url = self.chain_rpc_urls.get(chain_id)
        if not rpc_url:
            raise ValueError(f"No RPC endpoint configured for chain ID {chain_id}")

        web3 = Web3(Web3.HTTPProvider(rpc_url))
        if not web3.is_connected():
            raise ValueError(f"Failed to connect to RPC endpoint for chain ID {chain_id}")

        self._chain_web3[chain_id] = web3
        return web3

    @staticmethod
    def _as_raw_tx_bytes(value: Any) -> bytes:
        if isinstance(value, HexBytes):
            return bytes(value)
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return bytes(HexBytes(_ensure_hex(value)))
        raise ValueError("Unexpected raw transaction payload type")

    def _send_raw_transaction(self, chain_id: int, raw_tx: Any) -> str:
        chain_w3 = self._get_chain_web3(chain_id)
        raw_bytes = self._as_raw_tx_bytes(raw_tx)
        tx_hash = chain_w3.eth.send_raw_transaction(raw_bytes)
        return HexBytes(tx_hash).hex()

    def _get_token_context(self, token: HexBytes) -> TokenContext:
        contract = self._get_reader_contract()
        chain_hash, token_address_bytes, _ = contract.functions.tokens(bytes(token)).call()
        chain_type, chain_identifier = contract.functions.chains(chain_hash).call()

        if chain_type != 0:
            raise ValueError("Unsupported chain type for withdrawal generation")

        identifier_bytes = bytes(chain_identifier)
        if len(identifier_bytes) == 0:
            raise ValueError("Missing chain identifier for withdrawal generation")

        chain_id = int.from_bytes(identifier_bytes, byteorder="big")

        address_bytes = bytes(token_address_bytes)
        is_native = len(address_bytes) == 0 or int.from_bytes(address_bytes, "big") == 0
        token_address: Optional[ChecksumAddress] = None
        if not is_native:
            if len(address_bytes) != 20:
                raise ValueError("Token address must be 20 bytes for ERC20 withdrawals")
            token_address = _to_checksum("0x" + address_bytes.hex())

        return TokenContext(
            chain_id=chain_id,
            token_address=token_address,
            is_native=is_native,
        )

    def deposit_quote(self, user_address: str, token_id: str) -> Dict[str, str | int]:
        """Generate a deposit quote for UI usage."""

        checksum_user = self._require_address(user_address, "user_address")
        token_hex = self._require_hex(token_id, "token_id", expected_len=32)
        token_norm = token_hex.hex()
        deposit_address = self._get_deposit_address()
        instructions = (
            "Send the desired amount to the deposit address on the source chain."
            "This address is controlled within the ROFL enclave."
        )

        return {
            "user_address": checksum_user,
            "token_id": token_norm,
            "deposit_address": deposit_address,
            "chain_id": self.chain_id,
            "instructions": instructions,
        }

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------
    def include_native_deposit(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        tx_data = self._require_hex(
            payload["evm_transaction_data"], "evm_transaction_data"
        )
        proof = self._build_tx_proof(payload)

        fn = self.contract.functions.includeEVMNativeDeposit(
            user,
            token,
            tx_data,
            proof,
        )
        return self._submit(fn._encode_transaction_data())

    def include_erc20_deposit(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        tx_data = self._require_hex(
            payload["evm_transaction_data"], "evm_transaction_data"
        )
        proof = self._build_tx_proof(payload)

        fn = self.contract.functions.includeEVMErc20Deposit(
            user,
            token,
            tx_data,
            proof,
        )
        return self._submit(fn._encode_transaction_data())

    def lock_funds(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        service = self._require_address(payload["service_address"], "service_address")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        amount = self._require_positive(payload["amount"], "amount")
        expiry = self._require_positive(payload["expiry"], "expiry")
        signature = self._require_hex(payload["signature"], "signature")

        fn = self.contract.functions.lockFunds(
            user,
            service,
            token,
            amount,
            expiry,
            signature,
        )
        return self._submit(fn._encode_transaction_data())

    def transfer_funds(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        to_addr = self._require_address(payload["to_address"], "to_address")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        amount = self._require_positive(payload["amount"], "amount")
        expiry = self._require_positive(payload["expiry"], "expiry")
        signature = self._require_hex(payload["signature"], "signature")

        fn = self.contract.functions.transferFunds(
            user,
            to_addr,
            token,
            amount,
            expiry,
            signature,
        )
        return self._submit(fn._encode_transaction_data())

    def transfer_locked_funds(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        lock_index = self._require_positive(
            payload["lock_index"], "lock_index", allow_zero=True
        )
        to_addr = self._require_address(payload["to_address"], "to_address")
        amount = self._require_positive(payload["amount"], "amount")
        signature = self._require_hex(payload["signature"], "signature")

        fn = self.contract.functions.transferLockedFunds(
            user,
            lock_index,
            to_addr,
            amount,
            signature,
        )
        return self._submit(fn._encode_transaction_data())

    def unlock_funds(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        lock_index = self._require_positive(
            payload["lock_index"], "lock_index", allow_zero=True
        )

        fn = self.contract.functions.unlockFunds(
            user,
            lock_index,
        )
        return self._submit(fn._encode_transaction_data())

    def withdraw(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        amount = self._require_positive(payload["amount"], "amount")
        signature = self._require_hex(payload["signature"], "signature")

        contract_reader = self._get_reader_contract()

        try:
            contract_reader.functions.verifyWithdrawSignature(
                user,
                bytes(token),
                amount,
                bytes(signature),
            ).call()
        except Exception as exc:  # pragma: no cover - network path
            raise ValueError("Invalid or previously used withdrawal signature") from exc

        context = self._get_token_context(token)

        if context.is_native:
            raw_tx = contract_reader.functions.generateEVMNativeWithdrawal(
                context.chain_id,
                user,
                amount,
            ).call()
        else:
            raw_tx = contract_reader.functions.generateEVMErc20Withdrawal(
                context.chain_id,
                user,
                amount,
            ).call()

        tx_hash = self._send_raw_transaction(context.chain_id, raw_tx)

        detail_parts = [f"chain_id={context.chain_id}"]
        if context.token_address:
            detail_parts.append(f"token_address={context.token_address}")
        detail = "; ".join(detail_parts)

        return SubmissionResult(submission_id=tx_hash, status="sent", detail=detail)


_service_instance: Optional[AccountingContractService] = None


def get_accounting_contract_service() -> AccountingContractService:
    """Return singleton service instance."""

    global _service_instance
    if _service_instance is None:
        _service_instance = AccountingContractService()
    return _service_instance
