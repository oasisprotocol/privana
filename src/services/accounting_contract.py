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
from src.config import CHAIN_NAMES, NATIVE_TOKEN_SYMBOLS, ERC20_TOKENS, load_settings
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
        self.default_token_symbol = "ETH"
        self.chain_names = CHAIN_NAMES

    # ------------------------------------------------------------------
    # Helper utilities
    # ------------------------------------------------------------------
    def _build_tx(self, data: Any, value: int = 0, gas: Optional[int] = None) -> Dict:
        gas_limit = gas or self.gas_limit

        if isinstance(data, str):
            if data.startswith("0x"):
                data_bytes = HexBytes(data)
            else:
                data_bytes = data.encode("utf-8")
        elif isinstance(data, (bytes, bytearray, memoryview, HexBytes)):
            data_bytes = bytes(data)
        else:
            raise TypeError(f"Unsupported transaction data type: {type(data)!r}")

        tx = {
            "to": self.contract_address,
            "value": value,
            "gas": gas_limit,
            "data": Web3.to_hex(data_bytes),
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

    def _validate_proof_data(self, request_payload: Dict[str, Optional[str]]) -> None:
        required_fields = [
            "rlp_block_header",
            "transaction_index_rlp",
            "transaction_proof_stack",
            "receipt_index_rlp",
            "receipt_proof_stack",
        ]
        missing_fields = []

        for field in required_fields:
            value = request_payload.get(field)
            if value is None or value == "" or value == "0x":
                missing_fields.append(field)

        if missing_fields:
            raise ValueError(
                f"Proof data is incomplete. Missing or empty fields: {', '.join(missing_fields)}. "
                f"Proofs are required for contract validation. "
                f"Ensure ALCHEMY_API_KEY is configured and RPC endpoint supports debug_getRawBlock."
            )

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

    def _build_receipt_proof(self, request_payload: Dict[str, Optional[str]]):
        return (
            self._optional_hex(
                request_payload.get("receipt_index_rlp"), "receipt_index_rlp"
            ),
            self._optional_hex(
                request_payload.get("receipt_proof_stack"),
                "receipt_proof_stack",
            ),
        )

    def _get_deposit_address(self) -> str:
        """Fetch the deposit address from the contract."""
        contract_reader = self._get_reader_contract()
        return contract_reader.functions.evmAddress().call()

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
        token_type, token_data = contract.functions.tokens(bytes(token)).call()

        if token_type == 0:
            chain_id = int.from_bytes(token_data[:32], byteorder="big")
            if chain_id == 0:
                raise ValueError(
                    f"Token {token.hex()} is not registered in the contract. "
                    f"Please register it using the hardhat addEVMNativeToken or addEVMErc20Token task."
                )
            token_address = None
            is_native = True
        elif token_type == 1:
            chain_id = int.from_bytes(token_data[:32], byteorder="big")
            if chain_id == 0:
                raise ValueError(
                    f"Token {token.hex()} is not properly registered in the contract. "
                    f"Chain ID is 0. Please re-register using the hardhat addEVMErc20Token task."
                )
            token_address_bytes = token_data[32:52]
            token_address = _to_checksum("0x" + token_address_bytes.hex())
            is_native = False
        else:
            raise ValueError(f"Unsupported token type: {token_type}")

        return TokenContext(
            chain_id=chain_id,
            token_address=token_address,
            is_native=is_native,
        )

    def _get_token_symbol(self, token: HexBytes) -> str:
        context = self._get_token_context(token)

        if context.is_native:
            return NATIVE_TOKEN_SYMBOLS.get(context.chain_id, "ETH")

        if context.token_address:
            token_lower = context.token_address.lower()
            erc20_mapping = ERC20_TOKENS.get(context.chain_id, {})

            for addr, symbol in erc20_mapping.items():
                if addr.lower() == token_lower:
                    return symbol

            try:
                chain_w3 = self._get_chain_web3(context.chain_id)
                erc20_abi = [
                    {
                        "constant": True,
                        "inputs": [],
                        "name": "symbol",
                        "outputs": [{"name": "", "type": "string"}],
                        "type": "function"
                    }
                ]
                token_contract = chain_w3.eth.contract(
                    address=context.token_address,
                    abi=erc20_abi
                )
                return token_contract.functions.symbol().call()
            except Exception:
                return "UNKNOWN"

        return "UNKNOWN"

    def deposit_quote(self, user_address: str, token_id: str, amount: int) -> Dict[str, Any]:
        """Generate a deposit quote with transaction details for UI usage."""

        checksum_user = self._require_address(user_address, "user_address")
        token_hex = self._require_hex(token_id, "token_id", expected_len=32)
        token_norm = token_hex.hex()
        deposit_address = self._get_deposit_address()

        context = self._get_token_context(token_hex)

        transaction_data = {
            "chain_id": context.chain_id,
        }

        if context.is_native:
            transaction_data["to"] = deposit_address
            transaction_data["value"] = hex(amount)
            transaction_data["data"] = "0x"
            instructions = (
                "Send native tokens to the ROFL deposit address on the source chain. "
                "Use the provided transaction data to construct your transaction."
            )
        else:
            if context.token_address is None:
                raise ValueError("Token metadata is missing the ERC20 contract address")

            transaction_data["to"] = context.token_address
            transaction_data["value"] = "0x0"
            function_selector = "a9059cbb"
            padded_address = deposit_address[2:].lower().rjust(64, "0")
            padded_amount = hex(amount)[2:].rjust(64, "0")
            transaction_data["data"] = (
                "0x" + function_selector + padded_address + padded_amount
            )
            instructions = (
                "Call the token contract's transfer function sending funds to the ROFL deposit address. "
                "Use the provided transaction data to construct your transaction."
            )

        return {
            "user_address": checksum_user,
            "token_id": token_norm,
            "amount": amount,
            "deposit_address": deposit_address,
            "transaction": transaction_data,
            "instructions": instructions,
        }

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    def lock_funds(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        service = self._require_address(payload["service_address"], "service_address")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        amount = self._require_positive(payload["amount"], "amount")
        expiry = self._require_positive(payload["expiry"], "expiry")
        signature = self._require_hex(payload["signature"], "signature")

        fn = self.contract.functions.createLock(
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
        signature = self._require_hex(payload["signature"], "signature")

        fn = self.contract.functions.transferBalance(
            user,
            to_addr,
            token,
            amount,
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

        fn = self.contract.functions.transferFromLock(
            user,
            to_addr,
            lock_index,
            amount,
            signature,
        )
        return self._submit(fn._encode_transaction_data())

    def unlock_funds(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        lock_index = self._require_positive(
            payload["lock_index"], "lock_index", allow_zero=True
        )

        fn = self.contract.functions.unlockSingleLock(
            user,
            lock_index,
        )
        return self._submit(fn._encode_transaction_data())

    def include_deposit(self, payload: Dict) -> SubmissionResult:
        """Include a verified deposit using transaction and receipt proofs."""
        user = self._require_address(payload["user_address"], "user_address")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)

        self._validate_proof_data(payload)
        tx_proof = self._build_tx_proof(payload)
        receipt_proof = self._build_receipt_proof(payload)

        fn = self.contract.functions.creditEVMDeposit(
            user,
            token,
            tx_proof,
            receipt_proof,
        )
        return self._submit(fn._encode_transaction_data(), gas=3_000_000) # leave 3m gas limit

    def get_balance(self, user_address: str, token_id: str) -> int:
        """Get user balance for a specific token from the contract."""
        checksum_user = self._require_address(user_address, "user_address")
        token_hex = self._require_hex(token_id, "token_id", expected_len=32)

        contract_reader = self._get_reader_contract()
        balance = contract_reader.functions.balances(checksum_user, bytes(token_hex)).call()
        return balance

    def get_locked_funds(
        self, user_address: str, service_address: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get locked funds for a user, optionally filtered by service address."""
        checksum_user = self._require_address(user_address, "user_address")

        contract_reader = self._get_reader_contract()

        fund_locks = contract_reader.functions.getUserLocks(checksum_user).call()

        latest_timestamp = None
        if self.reader_w3:
            latest_block = self.reader_w3.eth.get_block("latest")
            latest_timestamp = latest_block.get("timestamp")

        locks = []
        for i, lock in enumerate(fund_locks):
            # FundLock struct: (serviceId, tokenId, amount, expiry)
            lock_info = {
                "lock_index": i,
                "user_address": checksum_user,
                "service_address": lock[0],
                "token_id": "0x" + lock[1].hex(),
                "amount": lock[2],
                "expiry": lock[3],
                "is_expired": bool(latest_timestamp is not None and lock[3] < latest_timestamp),
            }

            if service_address is None or lock[0].lower() == service_address.lower():
                locks.append(lock_info)

        total_locked = sum(lock["amount"] for lock in locks)

        response = {
            "user_address": checksum_user,
            "service_address": service_address,
            "locks": locks,
            "total_locked": total_locked
        }

        return response

    def withdraw(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        amount = self._require_positive(payload["amount"], "amount")
        signature = self._require_hex(payload["signature"], "signature")

        fn = self.contract.functions.withdraw(
            user,
            token,
            amount,
            signature,
        )

        submission_id = self.rofl_client.submit_tx(self._build_tx(fn._encode_transaction_data()))

        context = self._get_token_context(token)
        detail_parts = [f"chain_id={context.chain_id}"]
        if context.token_address:
            detail_parts.append(f"token_address={context.token_address}")
        detail = "; ".join(detail_parts)

        return SubmissionResult(submission_id=submission_id, status="submitted", detail=detail)

    def unlock_all_expired_locks(self, payload: Dict) -> SubmissionResult:
        """Unlock all expired locks for a user."""
        user = self._require_address(payload["user_address"], "user_address")

        fn = self.contract.functions.unlockAllExpiredLocks(user)
        return self._submit(fn._encode_transaction_data())

    def get_expired_locks(self, user_address: str) -> Dict[str, Any]:
        """Get all expired locks for a user."""
        checksum_user = self._require_address(user_address, "user_address")

        contract_reader = self._get_reader_contract()

        expired_locks, lock_indices = contract_reader.functions.getExpiredLocks(checksum_user).call()

        locks = []
        for i, lock in enumerate(expired_locks):
            lock_info = {
                "lock_index": lock_indices[i],
                "user_address": checksum_user,
                "service_address": lock[0],
                "token_id": "0x" + lock[1].hex(),
                "amount": lock[2],
                "expiry": lock[3],
            }
            locks.append(lock_info)

        return {
            "user_address": checksum_user,
            "expired_locks": locks,
        }

    def get_balances(self, user_address: str, token_ids: list[str]) -> Dict[str, Any]:
        """Get balances for multiple tokens for a user."""
        checksum_user = self._require_address(user_address, "user_address")

        token_hex_list = [self._require_hex(token_id, "token_id", expected_len=32) for token_id in token_ids]

        contract_reader = self._get_reader_contract()

        balances = contract_reader.functions.getBalances(
            checksum_user,
            [bytes(token) for token in token_hex_list]
        ).call()

        token_balances = []
        for i, token_id in enumerate(token_ids):
            token_hex = token_hex_list[i]
            token_symbol = self._get_token_symbol(token_hex)
            token_context = self._get_token_context(token_hex)

            token_balances.append({
                "token_id": token_id.lower(),
                "balance": str(balances[i]),
                "token_symbol": token_symbol,
                "chain_id": str(token_context.chain_id),
            })

        return {
            "user_address": checksum_user,
            "balances": token_balances,
        }

    def get_total_locked_balance(self, user_address: str, token_id: str) -> int:
        """Get total locked balance for a specific token."""
        checksum_user = self._require_address(user_address, "user_address")
        token_hex = self._require_hex(token_id, "token_id", expected_len=32)

        contract_reader = self._get_reader_contract()

        total_locked = contract_reader.functions.getTotalLockedBalance(
            checksum_user,
            bytes(token_hex)
        ).call()

        return total_locked


_service_instance: Optional[AccountingContractService] = None


def get_accounting_contract_service() -> AccountingContractService:
    """Return singleton service instance."""

    global _service_instance
    if _service_instance is None:
        _service_instance = AccountingContractService()
    return _service_instance
