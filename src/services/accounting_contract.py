"""Service for building and submitting Accounting contract transactions."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from sapphirepy import sapphire
from web3 import Web3
from web3.contract import Contract
from web3.middleware import SignAndSendRawMiddlewareBuilder

from src.abi.accounting import ACCOUNTING_ABI
from src.abi.accounting_siwe_auth import ACCOUNTING_SIWE_AUTH_ABI
from src.clients.rofl import RoflAppdClient
from src.config import CHAIN_NAMES, ERC20_TOKENS, NATIVE_TOKEN_SYMBOLS, load_settings
from src.models.types import Settings

logger = logging.getLogger(__name__)


def _ensure_hex(value: str) -> str:
    if value.startswith("0x"):
        return value
    return "0x" + value


def _to_checksum(address: str) -> ChecksumAddress:
    return Web3.to_checksum_address(address)


def _to_prefixed_hex(value: Any) -> str:
    return HexBytes(value).to_0x_hex().lower()


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
            Web3(Web3.HTTPProvider(self.sapphire_rpc_url)) if self.sapphire_rpc_url else None
        )
        self.contract_reader: Optional[Contract] = (
            self.reader_w3.eth.contract(address=self.contract_address, abi=ACCOUNTING_ABI)
            if self.reader_w3
            else None
        )
        self._confidential_reader_w3: Optional[Web3] = None
        self._confidential_contract_reader: Optional[Contract] = None
        self._siwe_auth_address: Optional[ChecksumAddress] = None
        self._siwe_auth_reader: Optional[Contract] = None
        self._confidential_siwe_auth_reader: Optional[Contract] = None
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

    def _submit(self, data: bytes, value: int = 0, gas: Optional[int] = None) -> SubmissionResult:
        tx = self._build_tx(data, value=value, gas=gas)
        submission_id = self.rofl_client.submit_tx(tx)
        return SubmissionResult(submission_id=submission_id, status="submitted")

    def _require_address(self, value: str, field: str) -> ChecksumAddress:
        if not isinstance(value, str) or not Web3.is_address(value):
            raise ValueError(f"Invalid {field} provided")
        return _to_checksum(value)

    def _require_positive(self, value: Any, field: str, allow_zero: bool = False) -> int:
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

    def _require_hex(self, value: str, field: str, expected_len: Optional[int] = None) -> HexBytes:
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
            self._optional_hex(request_payload.get("rlp_block_header"), "rlp_block_header"),
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
            self._optional_hex(request_payload.get("receipt_index_rlp"), "receipt_index_rlp"),
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
            raise ValueError("SAPPHIRE_RPC_URL must be configured to perform withdrawal operations")
        return self.contract_reader

    def _get_siwe_auth_address(self) -> ChecksumAddress:
        if self._siwe_auth_address is not None:
            return self._siwe_auth_address

        contract_reader = self._get_reader_contract()
        auth_address = contract_reader.functions.siweAuth().call()
        if not isinstance(auth_address, str) or not Web3.is_address(auth_address):
            raise ValueError(
                "Invalid SIWE auth contract address returned by the Accounting contract"
            )

        self._siwe_auth_address = _to_checksum(auth_address)
        return self._siwe_auth_address

    def _get_siwe_auth_reader_contract(self) -> Contract:
        if self._siwe_auth_reader is not None:
            return self._siwe_auth_reader

        if self.reader_w3 is None:
            raise ValueError("SAPPHIRE_RPC_URL must be configured to read SIWE auth settings")

        auth_address = self._get_siwe_auth_address()
        self._siwe_auth_reader = self.reader_w3.eth.contract(
            address=auth_address,
            abi=ACCOUNTING_SIWE_AUTH_ABI,
        )
        return self._siwe_auth_reader

    def _get_confidential_reader_contract(self) -> Contract:
        if self._confidential_contract_reader is not None:
            return self._confidential_contract_reader

        if not self.sapphire_rpc_url:
            raise ValueError("SAPPHIRE_RPC_URL must be configured to perform confidential reads")

        # Uses SAPPHIRE_VIEW_PRIVATE_KEY if set; otherwise tries ROFL appd keypair.
        private_key = os.getenv("SAPPHIRE_VIEW_PRIVATE_KEY")
        if private_key:
            private_key = _ensure_hex(private_key)
        else:
            # In ROFL deployments, use the app-managed service key for signing view calls.
            try:
                private_key, _ = self.rofl_client.get_keypair()
            except Exception as exc:  # pragma: no cover - depends on ROFL runtime
                raise ValueError(
                    "SAPPHIRE_VIEW_PRIVATE_KEY is not set and ROFL appd key is unavailable"
                ) from exc

        account: LocalAccount = Account.from_key(private_key)

        w3 = Web3(Web3.HTTPProvider(self.sapphire_rpc_url))
        w3.middleware_onion.add(SignAndSendRawMiddlewareBuilder.build(account))
        wrapped_w3 = sapphire.wrap(w3, account)

        self._confidential_reader_w3 = wrapped_w3
        self._confidential_contract_reader = wrapped_w3.eth.contract(
            address=self.contract_address, abi=ACCOUNTING_ABI
        )
        return self._confidential_contract_reader

    def _get_confidential_siwe_auth_reader_contract(self) -> Contract:
        if self._confidential_siwe_auth_reader is not None:
            return self._confidential_siwe_auth_reader

        # Ensure the confidential reader is initialized.
        self._get_confidential_reader_contract()
        if self._confidential_reader_w3 is None:
            raise ValueError("Confidential reader is not initialized")

        auth_address = self._get_siwe_auth_address()
        self._confidential_siwe_auth_reader = self._confidential_reader_w3.eth.contract(
            address=auth_address,
            abi=ACCOUNTING_SIWE_AUTH_ABI,
        )
        return self._confidential_siwe_auth_reader

    def _get_chain_timestamp(self) -> int:
        if self._confidential_reader_w3 is None:
            raise ValueError("Confidential reader is not initialized")
        return int(self._confidential_reader_w3.eth.get_block("latest")["timestamp"])

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

    def _check_destination_balance(self, chain_id: int, is_native: bool, amount: int) -> None:
        """Check that evmAddress has enough native balance on the destination chain for gas."""
        chain_w3 = self._get_chain_web3(chain_id)
        evm_address = self._get_deposit_address()
        balance = chain_w3.eth.get_balance(evm_address)

        required = self.settings.min_withdrawal_gas_balance
        if is_native:
            required += amount

        if balance < required:
            chain_name = CHAIN_NAMES.get(chain_id, f"chain {chain_id}")
            raise ValueError(
                f"Insufficient native balance on {chain_name}. "
                f"EVM address {evm_address} has {balance} wei, needs at least {required} wei."
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
                        "type": "function",
                    }
                ]
                token_contract = chain_w3.eth.contract(address=context.token_address, abi=erc20_abi)
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
            transaction_data["data"] = "0x" + function_selector + padded_address + padded_amount
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

    def modify_lock(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        lock_id = self._require_positive(payload["lock_id"], "lock_id")
        amount = self._require_positive(payload["amount"], "amount", allow_zero=True)
        new_expiry = self._require_positive(payload["new_expiry"], "new_expiry")
        signature = self._require_hex(payload["signature"], "signature")

        fn = self.contract.functions.modifyLock(
            user,
            lock_id,
            amount,
            new_expiry,
            signature,
        )
        return self._submit(fn._encode_transaction_data())

    def transfer_funds(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        to_addr = self._require_address(payload["to_address"], "to_address")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        amount = self._require_positive(payload["amount"], "amount")
        nonce = self._require_positive(payload["nonce"], "nonce", allow_zero=True)
        signature = self._require_hex(payload["signature"], "signature")

        # Validate transfer nonce matches on-chain state
        contract_reader = self._get_reader_contract()
        expected_nonce = contract_reader.functions.transferNonces(user).call()
        if nonce != expected_nonce:
            raise ValueError(
                f"Transfer nonce mismatch: got {nonce}, expected {expected_nonce}. "
                f"The nonce may already have been used by another request."
            )

        fn = self.contract.functions.transferBalance(
            user,
            to_addr,
            token,
            amount,
            nonce,
            signature,
        )
        return self._submit(fn._encode_transaction_data())

    def transfer_locked_funds(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        lock_id = self._require_positive(payload["lock_id"], "lock_id")
        to_addr = self._require_address(payload["to_address"], "to_address")
        amount = self._require_positive(payload["amount"], "amount")
        signature = self._require_hex(payload["signature"], "signature")

        fn = self.contract.functions.transferFromLock(
            user,
            to_addr,
            lock_id,
            amount,
            signature,
        )
        return self._submit(fn._encode_transaction_data())

    def unlock_funds(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        lock_id = self._require_positive(payload["lock_id"], "lock_id")

        fn = self.contract.functions.unlockSingleLock(
            user,
            lock_id,
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
        return self._submit(fn._encode_transaction_data(), gas=3_000_000)  # leave 3m gas limit

    def request_withdrawal(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        amount = self._require_positive(payload["amount"], "amount")
        nonce = self._require_positive(payload["nonce"], "nonce", allow_zero=True)
        signature = self._require_hex(payload["signature"], "signature")

        # Validate withdrawal nonce matches on-chain state
        contract_reader = self._get_reader_contract()
        expected_nonce = contract_reader.functions.withdrawalNonces(user).call()
        if nonce != expected_nonce:
            raise ValueError(
                f"Withdrawal nonce mismatch: got {nonce}, expected {expected_nonce}. "
                f"The nonce may already have been used by another request."
            )

        # Validate token and destination chain before on-chain submission
        context = self._get_token_context(token)
        if not context.chain_id:
            raise ValueError(
                f"Token {token.hex()} has invalid chain_id (0). "
                f"Cannot process withdrawal - token not properly registered."
            )

        # Validate we have RPC configured for destination chain
        if context.chain_id not in self.settings.chain_rpc_urls:
            raise ValueError(
                f"No RPC URL configured for chain_id {context.chain_id}. "
                f"Cannot process withdrawal - destination chain not supported."
            )

        # Verify the broadcaster has enough native balance on the destination chain
        self._check_destination_balance(context.chain_id, context.is_native, amount)

        fn = self.contract.functions.requestWithdrawal(
            user,
            token,
            amount,
            nonce,
            signature,
        )

        submission_id = self.rofl_client.submit_tx(self._build_tx(fn._encode_transaction_data()))

        detail_parts = [f"chain_id={context.chain_id}"]
        if context.token_address:
            detail_parts.append(f"token_address={context.token_address}")
        detail = "; ".join(detail_parts)

        return SubmissionResult(submission_id=submission_id, status="submitted", detail=detail)

    def resolve_withdrawal(self, index: int) -> SubmissionResult:
        """Submit resolveWithdrawal transaction via ROFL."""
        fn = self.contract.functions.resolveWithdrawal(index)
        return self._submit(fn._encode_transaction_data())

    def get_withdrawal(self, index: int) -> Dict[str, Any]:
        contract_reader = self._get_reader_contract()
        result = contract_reader.functions.withdrawals(index).call()

        return {
            "index": index,
            "user_address": result[0],
            "amount": str(result[1]),
            "block_number": result[2],
            "token_id": "0x" + result[3].hex(),
            "resolved": result[4],
            "tx_identifier": "0x" + result[5].hex() if result[5] else "0x",
        }

    def get_pending_withdrawals(self, user_address: str) -> Dict[str, Any]:
        checksum_user = self._require_address(user_address, "user_address")
        contract_reader = self._get_reader_contract()

        pending = []
        index = 0
        max_iterations = 10000

        while index < max_iterations:
            try:
                result = contract_reader.functions.withdrawals(index).call()
                withdrawal_user = result[0]
                resolved = result[4]

                if withdrawal_user.lower() == checksum_user.lower() and not resolved:
                    pending.append(
                        {
                            "index": index,
                            "user_address": result[0],
                            "amount": str(result[1]),
                            "block_number": result[2],
                            "token_id": "0x" + result[3].hex(),
                            "resolved": result[4],
                            "tx_identifier": "0x" + result[5].hex() if result[5] else "0x",
                        }
                    )
                index += 1
            except Exception:
                break

        return {
            "user_address": checksum_user,
            "pending_withdrawals": pending,
        }

    def get_all_pending_withdrawals(self, user_address: Optional[str] = None) -> Dict[str, Any]:
        """Get all pending (unresolved) withdrawals.

        Args:
            user_address: Optional filter by user address
        """
        contract_reader = self._get_reader_contract()
        current_block = self.reader_w3.eth.block_number if self.reader_w3 else 0

        checksum_user = None
        if user_address:
            checksum_user = self._require_address(user_address, "user_address")

        pending = []

        # Get total withdrawal count from contract
        try:
            total_withdrawals = contract_reader.functions.withdrawalCount().call()
        except Exception as e:
            logger.error(f"Failed to get withdrawal count: {e}")
            return {"pending": [], "current_block": current_block}

        for index in range(total_withdrawals):
            try:
                result = contract_reader.functions.withdrawals(index).call()

                withdrawal_user = result[0]
                resolved = result[4]

                # Skip if already resolved
                if resolved:
                    continue

                # Apply user filter
                if checksum_user and withdrawal_user.lower() != checksum_user.lower():
                    continue

                block_number = result[2]
                token_id_bytes = result[3]

                # Get chain_id for this token
                token_hex = HexBytes(token_id_bytes)
                try:
                    context = self._get_token_context(token_hex)
                    chain_id = context.chain_id
                except Exception as e:
                    logger.warning(
                        f"Withdrawal #{index}: unknown/invalid token 0x{token_id_bytes.hex()} - {e}"
                    )
                    chain_id = None

                pending.append(
                    {
                        "index": index,
                        "user_address": withdrawal_user,
                        "amount": result[1],
                        "token_id": "0x" + token_id_bytes.hex(),
                        "block_number": block_number,
                        "can_resolve": current_block - block_number >= 1,
                        "chain_id": chain_id,
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to read withdrawal {index}: {e}")

        return {
            "pending": pending,
            "current_block": current_block,
        }

    def unlock_all_expired_locks(self, payload: Dict) -> SubmissionResult:
        """Unlock all expired locks for a user."""
        user = self._require_address(payload["user_address"], "user_address")

        fn = self.contract.functions.unlockAllExpiredLocks(user)
        return self._submit(fn._encode_transaction_data())

    def get_token_info(self, token_id: str) -> Dict[str, Any]:
        token_hex = self._require_hex(token_id, "token_id", expected_len=32)
        contract_reader = self._get_reader_contract()

        token_type, token_data = contract_reader.functions.tokens(bytes(token_hex)).call()

        token_type_names = {0: "NativeEVM", 1: "ERC20"}

        result = {
            "token_id": token_id.lower(),
            "token_type": token_type,
            "token_type_name": token_type_names.get(token_type, "Unknown"),
            "data": "0x" + token_data.hex() if token_data else "0x",
        }

        if token_type == 0 and token_data:
            chain_id = int.from_bytes(token_data[:32], byteorder="big")
            result["chain_id"] = chain_id
            result["chain_name"] = self.chain_names.get(chain_id, f"Chain {chain_id}")
        elif token_type == 1 and len(token_data) >= 52:
            chain_id = int.from_bytes(token_data[:32], byteorder="big")
            token_address = "0x" + token_data[32:52].hex()
            result["chain_id"] = chain_id
            result["chain_name"] = self.chain_names.get(chain_id, f"Chain {chain_id}")
            result["token_address"] = Web3.to_checksum_address(token_address)

        return result

    def get_withdrawal_nonce(self, user_address: str) -> Dict[str, Any]:
        """Get the current withdrawal nonce for a user."""
        checksum_user = self._require_address(user_address, "user_address")
        contract_reader = self._get_reader_contract()
        nonce = contract_reader.functions.withdrawalNonces(checksum_user).call()
        return {
            "user_address": checksum_user,
            "nonce": nonce,
        }

    def get_transfer_nonce(self, user_address: str) -> Dict[str, Any]:
        """Get the current transfer nonce for a user."""
        checksum_user = self._require_address(user_address, "user_address")
        contract_reader = self._get_reader_contract()
        nonce = contract_reader.functions.transferNonces(checksum_user).call()
        return {
            "user_address": checksum_user,
            "nonce": nonce,
        }

    def get_relay_nonce(self, user_address: str) -> Dict[str, Any]:
        checksum_user = self._require_address(user_address, "user_address")
        contract_reader = self._get_reader_contract()
        nonce = contract_reader.functions.relayNonces(checksum_user).call()
        return {
            "user_address": checksum_user,
            "nonce": nonce,
        }

    def get_relay_result(self, relay_id: int) -> bytes:
        contract_reader = self._get_reader_contract()
        return contract_reader.functions.relayResults(relay_id).call()

    def get_next_relay_id(self) -> int:
        contract_reader = self._get_reader_contract()
        return contract_reader.functions.nextRelayId().call()

    def clear_relay_result(self, relay_id: int) -> SubmissionResult:
        fn = self.contract.functions.clearRelayResult(relay_id)
        return self._submit(fn._encode_transaction_data())

    def relay_execute(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        chain_id = self._require_positive(payload["chain_id"], "chain_id")
        to = self._require_address(payload["to"], "to")
        value = self._require_positive(payload["value"], "value", allow_zero=True)
        data = self._require_hex(payload["data"], "data") if payload.get("data", "0x") != "0x" else HexBytes("0x")
        gas_limit = self._require_positive(payload["gas_limit"], "gas_limit")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        amount = self._require_positive(payload["amount"], "amount", allow_zero=True)
        nonce = self._require_positive(payload["nonce"], "nonce", allow_zero=True)
        signature = self._require_hex(payload["signature"], "signature")

        contract_reader = self._get_reader_contract()
        expected_nonce = contract_reader.functions.relayNonces(user).call()
        if nonce != expected_nonce:
            raise ValueError(
                f"Relay nonce mismatch: got {nonce}, expected {expected_nonce}. "
                f"The nonce may already have been used by another request."
            )

        fn = self.contract.functions.executeRelay(
            user,
            chain_id,
            to,
            value,
            bytes(data),
            gas_limit,
            bytes(token),
            amount,
            nonce,
            bytes(signature),
        )
        return self._submit(fn._encode_transaction_data())

    def get_siwe_domain(self) -> Dict[str, Any]:
        contract_reader = self._get_siwe_auth_reader_contract()
        domain = contract_reader.functions.domain().call()
        return {"domain": domain}

    @staticmethod
    def _parse_signature_rsv(signature: str) -> tuple[bytes, bytes, int]:
        sig = HexBytes(_ensure_hex(signature))
        if len(sig) != 65:
            raise ValueError("SIWE signature must be 65 bytes")
        r = bytes(sig[0:32])
        s = bytes(sig[32:64])
        v = int(sig[64])
        if v < 27:
            v += 27
        if v not in (27, 28):
            raise ValueError(f"Invalid signature recovery parameter v={v}")
        return (r, s, v)

    def siwe_login(self, siwe_message: str, signature: str) -> Dict[str, Any]:
        contract_reader = self._get_confidential_siwe_auth_reader_contract()
        r, s, v = self._parse_signature_rsv(signature)
        token = contract_reader.functions.login(siwe_message, (r, s, v)).call()
        return {"token": _to_prefixed_hex(token)}

    def get_balance(self, user_address: str, token_id: str, siwe_token: bytes) -> Dict[str, Any]:
        checksum_user = self._require_address(user_address, "user_address")
        token_hex = self._require_hex(token_id, "token_id", expected_len=32)

        contract_reader = self._get_confidential_reader_contract()
        balance = contract_reader.functions.balanceOf(
            checksum_user, bytes(token_hex), siwe_token
        ).call()

        context = self._get_token_context(token_hex)
        return {
            "user_address": checksum_user,
            "token_id": _to_prefixed_hex(token_hex),
            "balance": str(balance),
            "token_symbol": self._get_token_symbol(token_hex),
            "chain_id": str(context.chain_id),
        }

    def get_batch_balances(
        self, user_address: str, token_ids_raw: list[str], siwe_token: bytes
    ) -> Dict[str, Any]:
        user = self._require_address(user_address, "user_address")
        if not isinstance(token_ids_raw, list) or len(token_ids_raw) == 0:
            raise ValueError("token_ids must be a non-empty array")

        token_ids: list[HexBytes] = [
            self._require_hex(token_id, "token_id", expected_len=32) for token_id in token_ids_raw
        ]

        contract_reader = self._get_confidential_reader_contract()

        response_balances = []
        for token_id in token_ids:
            balance = contract_reader.functions.balanceOf(user, bytes(token_id), siwe_token).call()
            context = self._get_token_context(token_id)
            response_balances.append(
                {
                    "token_id": _to_prefixed_hex(token_id),
                    "balance": str(balance),
                    "token_symbol": self._get_token_symbol(token_id),
                    "chain_id": str(context.chain_id),
                }
            )

        return {"user_address": user, "balances": response_balances}

    def _lock_to_info(self, user: ChecksumAddress, lock: Any, now: int) -> Dict[str, Any]:
        lock_id, service_id, token_id, amount, expiry = lock
        return {
            "lock_id": int(lock_id),
            "user_address": user,
            "service_address": Web3.to_checksum_address(service_id),
            "token_id": _to_prefixed_hex(token_id),
            "amount": int(amount),
            "expiry": int(expiry),
            "is_expired": now >= int(expiry),
        }

    def get_locked_funds(
        self,
        user_address: str,
        service_address: Optional[str],
        siwe_token: bytes,
    ) -> Dict[str, Any]:
        user = self._require_address(user_address, "user_address")
        service = (
            self._require_address(service_address, "service_address") if service_address else None
        )

        contract_reader = self._get_confidential_reader_contract()

        locks: list[Any]
        # API private reads are user-token scoped; `service_address` here is an output filter.
        # Backend services that need service-authenticated reads should query
        # getServiceLocks(...) directly on the contract using Sapphire authenticated
        # view calls (for Python wrappers: signed query with empty token parameter).
        all_locks = contract_reader.functions.getUserLocks(user, siwe_token).call()
        if service is None:
            locks = all_locks
        else:
            locks = [lock for lock in all_locks if lock[1].lower() == service.lower()]

        now = self._get_chain_timestamp()

        lock_infos = [self._lock_to_info(user, lock, now) for lock in locks]
        total_locked = sum(info["amount"] for info in lock_infos)

        return {
            "user_address": user,
            "service_address": service,
            "locks": lock_infos,
            "total_locked": int(total_locked),
        }

    def get_expired_locks(self, user_address: str, siwe_token: bytes) -> Dict[str, Any]:
        user = self._require_address(user_address, "user_address")
        contract_reader = self._get_confidential_reader_contract()

        now = self._get_chain_timestamp()
        all_locks = contract_reader.functions.getUserLocks(user, siwe_token).call()
        expired_locks = [lock for lock in all_locks if now >= int(lock[4])]
        lock_infos = [self._lock_to_info(user, lock, now) for lock in expired_locks]
        return {"user_address": user, "expired_locks": lock_infos}

    def get_total_locked_balance(
        self, user_address: str, token_id: str, siwe_token: bytes
    ) -> Dict[str, Any]:
        user = self._require_address(user_address, "user_address")
        token_hex = self._require_hex(token_id, "token_id", expected_len=32)

        contract_reader = self._get_confidential_reader_contract()
        locks = contract_reader.functions.getUserLocks(user, siwe_token).call()
        total_locked = sum(int(lock[3]) for lock in locks if HexBytes(lock[2]) == token_hex)

        return {
            "user_address": user,
            "token_id": _to_prefixed_hex(token_hex),
            "total_locked": str(total_locked),
        }


_service_instance: Optional[AccountingContractService] = None


def get_accounting_contract_service() -> AccountingContractService:
    """Return singleton service instance."""

    global _service_instance
    if _service_instance is None:
        _service_instance = AccountingContractService()
    return _service_instance
