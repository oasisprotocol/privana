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
from web3 import AsyncWeb3, Web3
from web3.contract import AsyncContract
from web3.middleware import SignAndSendRawMiddlewareBuilder
from web3.providers import AsyncHTTPProvider

from src.abi.accounting import ACCOUNTING_ABI
from src.abi.accounting_siwe_auth import ACCOUNTING_SIWE_AUTH_ABI
from src.clients.rofl import RoflAppdClient
from src.config import (
    CHAIN_NAMES,
    NATIVE_TOKEN_DECIMALS,
    NATIVE_TOKEN_NAMES,
    NATIVE_TOKEN_SYMBOLS,
    load_settings,
)
from src.models.types import Settings
from src.services.cache import AsyncTTLCache

logger = logging.getLogger(__name__)

# Cache TTL settings (in seconds)
_TOKEN_CONTEXT_CACHE_TTL = 3600  # 1 hour - token metadata rarely changes
_TOKEN_SYMBOL_CACHE_TTL = 3600  # 1 hour - symbols rarely change
_TOKEN_NAME_CACHE_TTL = 3600  # 1 hour - names rarely change
_TOKEN_DECIMALS_CACHE_TTL = 3600  # 1 hour - decimals never change
_TOKEN_LIST_CACHE_TTL = 300  # 5 minutes - token list rarely changes

# Cache size limits
_TOKEN_CACHE_MAXSIZE = 1000  # Token metadata cache (context + symbols)

# Note: Balance and user locks are not cached because SIWE token must be
# validated on each request. In the future, if SIWE validation moves to
# the API layer, caching could be added here for performance.


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
        self.contract = self.w3.eth.contract(address=self.contract_address, abi=ACCOUNTING_ABI)
        self.sapphire_rpc_url = self.settings.sapphire_rpc_url

        self.reader_w3: Optional[AsyncWeb3] = (
            AsyncWeb3(AsyncHTTPProvider(self.sapphire_rpc_url)) if self.sapphire_rpc_url else None
        )
        self.contract_reader: Optional[AsyncContract] = (
            self.reader_w3.eth.contract(address=self.contract_address, abi=ACCOUNTING_ABI)
            if self.reader_w3
            else None
        )

        self._confidential_reader_w3: Optional[AsyncWeb3] = None
        self._confidential_contract_reader: Optional[AsyncContract] = None
        self._deposit_address: Optional[str] = None
        self._siwe_domain: Optional[str] = None
        self._siwe_auth_address: Optional[ChecksumAddress] = None
        self._siwe_auth_reader: Optional[AsyncContract] = None
        self._confidential_siwe_auth_reader: Optional[AsyncContract] = None

        self.rofl_client = RoflAppdClient()
        self.chain_rpc_urls: Dict[int, str] = dict(self.settings.chain_rpc_urls)
        self._chain_web3: Dict[int, AsyncWeb3] = {}
        self.default_token_symbol = "ETH"
        self.chain_names = CHAIN_NAMES

        # Async TTL caches for concurrent access
        self._token_context_cache: AsyncTTLCache[str, TokenContext] = AsyncTTLCache(
            maxsize=_TOKEN_CACHE_MAXSIZE, ttl=_TOKEN_CONTEXT_CACHE_TTL
        )
        self._token_symbol_cache: AsyncTTLCache[str, Optional[str]] = AsyncTTLCache(
            maxsize=_TOKEN_CACHE_MAXSIZE, ttl=_TOKEN_SYMBOL_CACHE_TTL
        )
        self._token_name_cache: AsyncTTLCache[str, Optional[str]] = AsyncTTLCache(
            maxsize=_TOKEN_CACHE_MAXSIZE, ttl=_TOKEN_NAME_CACHE_TTL
        )
        self._token_decimals_cache: AsyncTTLCache[str, Optional[int]] = AsyncTTLCache(
            maxsize=_TOKEN_CACHE_MAXSIZE, ttl=_TOKEN_DECIMALS_CACHE_TTL
        )
        self._token_list_cache: AsyncTTLCache[str, list[Dict[str, Any]]] = AsyncTTLCache(
            maxsize=1, ttl=_TOKEN_LIST_CACHE_TTL
        )

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

    async def _submit(
        self, data: bytes, value: int = 0, gas: Optional[int] = None
    ) -> SubmissionResult:
        tx = self._build_tx(data, value=value, gas=gas)
        await self.rofl_client.submit_tx(tx)
        return SubmissionResult(status="submitted")

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

    async def _get_deposit_address(self) -> str:
        """Fetch the deposit address from the contract (cached)."""
        if self._deposit_address is None:
            contract_reader = self._get_reader_contract()
            self._deposit_address = await contract_reader.functions.evmAddress().call()
        return self._deposit_address

    def _get_reader_contract(self) -> AsyncContract:
        if self.contract_reader is None:
            raise ValueError("SAPPHIRE_RPC_URL must be configured to perform withdrawal operations")
        return self.contract_reader

    async def _get_siwe_auth_address(self) -> ChecksumAddress:
        if self._siwe_auth_address is not None:
            return self._siwe_auth_address

        contract_reader = self._get_reader_contract()
        auth_address = await contract_reader.functions.siweAuth().call()
        if not isinstance(auth_address, str) or not Web3.is_address(auth_address):
            raise ValueError(
                "Invalid SIWE auth contract address returned by the Accounting contract"
            )

        self._siwe_auth_address = _to_checksum(auth_address)
        return self._siwe_auth_address

    async def _get_siwe_auth_reader_contract(self) -> AsyncContract:
        if self._siwe_auth_reader is not None:
            return self._siwe_auth_reader

        if self.reader_w3 is None:
            raise ValueError("SAPPHIRE_RPC_URL must be configured to read SIWE auth settings")

        auth_address = await self._get_siwe_auth_address()
        self._siwe_auth_reader = self.reader_w3.eth.contract(
            address=auth_address,
            abi=ACCOUNTING_SIWE_AUTH_ABI,
        )
        return self._siwe_auth_reader

    async def _get_confidential_reader_contract(self) -> AsyncContract:
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
                private_key, _ = await self.rofl_client.get_keypair()
            except Exception as exc:  # pragma: no cover - depends on ROFL runtime
                raise ValueError(
                    "SAPPHIRE_VIEW_PRIVATE_KEY is not set and ROFL appd key is unavailable"
                ) from exc

        account: LocalAccount = Account.from_key(private_key)

        w3 = AsyncWeb3(AsyncHTTPProvider(self.sapphire_rpc_url))
        w3.middleware_onion.add(SignAndSendRawMiddlewareBuilder.build(account))
        wrapped_w3 = sapphire.wrap(w3, account)

        self._confidential_reader_w3 = wrapped_w3
        self._confidential_contract_reader = wrapped_w3.eth.contract(
            address=self.contract_address, abi=ACCOUNTING_ABI
        )
        return self._confidential_contract_reader

    async def _get_confidential_siwe_auth_reader_contract(self) -> AsyncContract:
        if self._confidential_siwe_auth_reader is not None:
            return self._confidential_siwe_auth_reader

        # Ensure the confidential reader is initialized.
        await self._get_confidential_reader_contract()
        if self._confidential_reader_w3 is None:
            raise ValueError("Confidential reader is not initialized")

        auth_address = await self._get_siwe_auth_address()
        self._confidential_siwe_auth_reader = self._confidential_reader_w3.eth.contract(
            address=auth_address,
            abi=ACCOUNTING_SIWE_AUTH_ABI,
        )
        return self._confidential_siwe_auth_reader

    async def _get_chain_timestamp(self) -> int:
        if self._confidential_reader_w3 is None:
            raise ValueError("Confidential reader is not initialized")
        block = await self._confidential_reader_w3.eth.get_block("latest")
        return int(block["timestamp"])

    async def _get_chain_web3(self, chain_id: int) -> AsyncWeb3:
        if chain_id in self._chain_web3:
            return self._chain_web3[chain_id]

        rpc_url = self.chain_rpc_urls.get(chain_id)
        if not rpc_url:
            raise ValueError(f"No RPC endpoint configured for chain ID {chain_id}")

        web3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        connected = await web3.is_connected()
        if not connected:
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

    async def _send_raw_transaction(self, chain_id: int, raw_tx: Any) -> str:
        chain_w3 = await self._get_chain_web3(chain_id)
        raw_bytes = self._as_raw_tx_bytes(raw_tx)
        tx_hash = await chain_w3.eth.send_raw_transaction(raw_bytes)
        return HexBytes(tx_hash).hex()

    async def _get_token_context(self, token: HexBytes) -> TokenContext:
        return await self._token_context_cache.get_or_set_async(
            token.hex(), lambda: self._fetch_token_context(token)
        )

    async def _fetch_token_context(self, token: HexBytes) -> TokenContext:
        """Fetch token context from contract (uncached)."""
        contract = self._get_reader_contract()
        token_type, token_data = await contract.functions.tokens(bytes(token)).call()

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

    async def fetch_registered_erc20_tokens(self) -> Dict[int, Dict[str, str]]:
        """
        Fetch all registered ERC20 tokens from contract.

        Returns a mapping of chain_id -> {token_address: token_id} for ERC20 tokens.
        This allows the deposit listener to dynamically discover which tokens to watch.
        """
        contract = self._get_reader_contract()
        if self.reader_w3 is None:
            raise ValueError("SAPPHIRE_RPC_URL must be configured to fetch registered tokens")

        erc20_tokens: Dict[int, Dict[str, str]] = {}

        token_ids = await contract.functions.getRegisteredTokens().call()
        logger.info(f"Fetched {len(token_ids)} registered tokens from contract")

        for token_id in token_ids:
            try:
                # Fetch token data from contract
                # tokenType: 0 = NativeEVM, 1 = ERC20
                stored_type, token_data = await contract.functions.tokens(token_id).call()

                # Only process ERC20 tokens (tokenType == 1)
                if stored_type != 1 or len(token_data) < 52:
                    continue

                # Decode: first 32 bytes = chain_id, next 20 bytes = token address
                chain_id = int.from_bytes(token_data[:32], byteorder="big")
                token_address = "0x" + token_data[32:52].hex()
                token_address = Web3.to_checksum_address(token_address)
                token_id_hex = Web3.to_hex(token_id)

                if chain_id not in erc20_tokens:
                    erc20_tokens[chain_id] = {}

                erc20_tokens[chain_id][token_address] = token_id_hex
                logger.info(
                    f"Discovered registered ERC20 token: chain={chain_id}, "
                    f"address={token_address}, tokenId={token_id_hex}"
                )

            except Exception as e:
                logger.warning(f"Failed to decode token {Web3.to_hex(token_id)}: {e}")
                continue

        return erc20_tokens

    async def _check_destination_balance(self, chain_id: int, is_native: bool, amount: int) -> None:
        """Check that evmAddress has enough native balance on the destination chain for gas."""
        chain_w3 = await self._get_chain_web3(chain_id)
        evm_address = await self._get_deposit_address()
        balance = await chain_w3.eth.get_balance(evm_address)

        required = self.settings.min_withdrawal_gas_balance
        if is_native:
            required += amount

        if balance < required:
            chain_name = CHAIN_NAMES.get(chain_id, f"chain {chain_id}")
            raise ValueError(
                f"Insufficient native balance on {chain_name}. "
                f"EVM address {evm_address} has {balance} wei, needs at least {required} wei."
            )

    async def _get_token_symbol(self, token: HexBytes) -> Optional[str]:
        return await self._token_symbol_cache.get_or_set_async(
            token.hex(), lambda: self._fetch_token_symbol(token)
        )

    async def _fetch_token_symbol(self, token: HexBytes) -> Optional[str]:
        """Fetch token symbol from config or chain (uncached)."""
        context = await self._get_token_context(token)

        if context.is_native:
            return NATIVE_TOKEN_SYMBOLS.get(context.chain_id, "ETH")

        if context.token_address:
            try:
                chain_w3 = await self._get_chain_web3(context.chain_id)
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
                return await token_contract.functions.symbol().call()
            except Exception:
                return None

        return None

    async def _get_token_name(self, token: HexBytes) -> Optional[str]:
        return await self._token_name_cache.get_or_set_async(
            token.hex(), lambda: self._fetch_token_name(token)
        )

    async def _fetch_token_name(self, token: HexBytes) -> Optional[str]:
        """Fetch token name from config or chain (uncached)."""
        context = await self._get_token_context(token)

        if context.is_native:
            return NATIVE_TOKEN_NAMES.get(context.chain_id, "Ether")

        if context.token_address:
            try:
                chain_w3 = await self._get_chain_web3(context.chain_id)
                erc20_abi = [
                    {
                        "constant": True,
                        "inputs": [],
                        "name": "name",
                        "outputs": [{"name": "", "type": "string"}],
                        "type": "function",
                    }
                ]
                token_contract = chain_w3.eth.contract(address=context.token_address, abi=erc20_abi)
                return await token_contract.functions.name().call()
            except Exception:
                return None

        return None

    async def _get_token_decimals(self, token: HexBytes) -> Optional[int]:
        return await self._token_decimals_cache.get_or_set_async(
            token.hex(), lambda: self._fetch_token_decimals(token)
        )

    async def _fetch_token_decimals(self, token: HexBytes) -> Optional[int]:
        """Fetch token decimals from config or chain (uncached)."""
        context = await self._get_token_context(token)

        if context.is_native:
            return NATIVE_TOKEN_DECIMALS.get(context.chain_id, 18)

        if context.token_address:
            try:
                chain_w3 = await self._get_chain_web3(context.chain_id)
                erc20_abi = [
                    {
                        "constant": True,
                        "inputs": [],
                        "name": "decimals",
                        "outputs": [{"name": "", "type": "uint8"}],
                        "type": "function",
                    }
                ]
                token_contract = chain_w3.eth.contract(address=context.token_address, abi=erc20_abi)
                return await token_contract.functions.decimals().call()
            except Exception:
                return None

        return None

    async def deposit_quote(self, user_address: str, token_id: str, amount: int) -> Dict[str, Any]:
        """Generate a deposit quote with transaction details for UI usage."""

        checksum_user = self._require_address(user_address, "user_address")
        token_hex = self._require_hex(token_id, "token_id", expected_len=32)
        token_norm = token_hex.hex()
        deposit_address = await self._get_deposit_address()

        context = await self._get_token_context(token_hex)

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
            "amount": str(amount),
            "deposit_address": deposit_address,
            "transaction": transaction_data,
            "instructions": instructions,
        }

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    async def lock_funds(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        service = self._require_address(payload["service_address"], "service_address")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        amount = self._require_positive(payload["amount"], "amount")
        expiry = self._require_positive(payload["expiry"], "expiry")
        nonce = self._require_positive(payload["nonce"], "nonce", allow_zero=True)
        signature = self._require_hex(payload["signature"], "signature")

        contract_reader = self._get_reader_contract()
        expected_nonce = await contract_reader.functions.createLockNonces(user).call()
        if nonce != expected_nonce:
            raise ValueError(
                f"createLock nonce mismatch: got {nonce}, expected {expected_nonce}. "
                f"The nonce may already have been used by another request."
            )

        fn = self.contract.functions.createLock(
            user,
            service,
            token,
            amount,
            expiry,
            nonce,
            signature,
        )
        return await self._submit(fn._encode_transaction_data())

    async def modify_lock(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        lock_id = self._require_positive(payload["lock_id"], "lock_id")
        amount = self._require_positive(payload["amount"], "amount", allow_zero=True)
        new_expiry = self._require_positive(payload["new_expiry"], "new_expiry")
        nonce = self._require_positive(payload["nonce"], "nonce", allow_zero=True)
        signature = self._require_hex(payload["signature"], "signature")

        contract_reader = self._get_reader_contract()
        expected_nonce = await contract_reader.functions.modifyLockNonces(user).call()
        if nonce != expected_nonce:
            raise ValueError(
                f"modifyLock nonce mismatch: got {nonce}, expected {expected_nonce}. "
                f"The nonce may already have been used by another request."
            )

        fn = self.contract.functions.modifyLock(
            user,
            lock_id,
            amount,
            new_expiry,
            nonce,
            signature,
        )
        return await self._submit(fn._encode_transaction_data())

    async def transfer_funds(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        to_addr = self._require_address(payload["to_address"], "to_address")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        amount = self._require_positive(payload["amount"], "amount")
        nonce = self._require_positive(payload["nonce"], "nonce", allow_zero=True)
        signature = self._require_hex(payload["signature"], "signature")

        # Validate transfer nonce matches on-chain state
        contract_reader = self._get_reader_contract()
        expected_nonce = await contract_reader.functions.transferNonces(user).call()
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
        return await self._submit(fn._encode_transaction_data())

    async def transfer_locked_funds(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        lock_id = self._require_positive(payload["lock_id"], "lock_id")
        to_addr = self._require_address(payload["to_address"], "to_address")
        amount = self._require_positive(payload["amount"], "amount")
        service = self._require_address(payload["service_address"], "service_address")
        nonce = self._require_positive(payload["nonce"], "nonce", allow_zero=True)
        signature = self._require_hex(payload["signature"], "signature")

        contract_reader = self._get_reader_contract()
        expected_nonce = await contract_reader.functions.transferLockedNonces(service).call()
        if nonce != expected_nonce:
            raise ValueError(
                f"transferFromLock nonce mismatch: got {nonce}, expected {expected_nonce}. "
                f"The nonce may already have been used by another request."
            )

        fn = self.contract.functions.transferFromLock(
            user,
            to_addr,
            lock_id,
            amount,
            nonce,
            signature,
        )
        return await self._submit(fn._encode_transaction_data())

    async def unlock_funds(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        lock_id = self._require_positive(payload["lock_id"], "lock_id")

        fn = self.contract.functions.unlockSingleLock(
            user,
            lock_id,
        )
        return await self._submit(fn._encode_transaction_data())

    async def include_deposit(self, payload: Dict) -> SubmissionResult:
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
        return await self._submit(
            fn._encode_transaction_data(), gas=3_000_000
        )  # leave 3m gas limit

    async def request_withdrawal(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        amount = self._require_positive(payload["amount"], "amount")
        nonce = self._require_positive(payload["nonce"], "nonce", allow_zero=True)
        signature = self._require_hex(payload["signature"], "signature")

        # Validate withdrawal nonce matches on-chain state
        contract_reader = self._get_reader_contract()
        expected_nonce = await contract_reader.functions.withdrawalNonces(user).call()
        if nonce != expected_nonce:
            raise ValueError(
                f"Withdrawal nonce mismatch: got {nonce}, expected {expected_nonce}. "
                f"The nonce may already have been used by another request."
            )

        # Validate token and destination chain before on-chain submission
        context = await self._get_token_context(token)
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
        await self._check_destination_balance(context.chain_id, context.is_native, amount)

        fn = self.contract.functions.requestWithdrawal(
            user,
            token,
            amount,
            nonce,
            signature,
        )

        await self.rofl_client.submit_tx(self._build_tx(fn._encode_transaction_data()))

        detail_parts = [f"chain_id={context.chain_id}"]
        if context.token_address:
            detail_parts.append(f"token_address={context.token_address}")
        detail = "; ".join(detail_parts)

        return SubmissionResult(status="submitted", detail=detail)

    async def resolve_withdrawal(self, index: int) -> SubmissionResult:
        """Submit resolveWithdrawal transaction via ROFL."""
        fn = self.contract.functions.resolveWithdrawal(index)
        return await self._submit(fn._encode_transaction_data())

    async def get_withdrawal(self, index: int) -> Dict[str, Any]:
        contract_reader = self._get_reader_contract()
        result = await contract_reader.functions.withdrawals(index).call()

        return {
            "index": index,
            "user_address": result[0],
            "amount": str(result[1]),
            "block_number": result[2],
            "token_id": "0x" + result[3].hex(),
            "resolved": result[4],
            "tx_identifier": "0x" + result[5].hex() if result[5] else "0x",
        }

    async def get_pending_withdrawals(self, user_address: str) -> Dict[str, Any]:
        checksum_user = self._require_address(user_address, "user_address")
        contract_reader = self._get_reader_contract()

        pending = []
        index = 0
        max_iterations = 10000

        while index < max_iterations:
            try:
                result = await contract_reader.functions.withdrawals(index).call()
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

    async def get_all_pending_withdrawals(
        self, user_address: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get all pending (unresolved) withdrawals.

        Args:
            user_address: Optional filter by user address
        """
        contract_reader = self._get_reader_contract()
        current_block = await self.reader_w3.eth.block_number if self.reader_w3 else 0

        checksum_user = None
        if user_address:
            checksum_user = self._require_address(user_address, "user_address")

        pending = []

        # Get total withdrawal count from contract
        try:
            total_withdrawals = await contract_reader.functions.withdrawalCount().call()
        except Exception as e:
            logger.error(f"Failed to get withdrawal count: {e}")
            return {"pending": [], "current_block": current_block}

        for index in range(total_withdrawals):
            try:
                result = await contract_reader.functions.withdrawals(index).call()

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
                    context = await self._get_token_context(token_hex)
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
                        "amount": str(result[1]),
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

    async def unlock_all_expired_locks(self, payload: Dict) -> SubmissionResult:
        """Unlock all expired locks for a user."""
        user = self._require_address(payload["user_address"], "user_address")

        fn = self.contract.functions.unlockAllExpiredLocks(user)
        return await self._submit(fn._encode_transaction_data())

    async def get_token_info(self, token_id: str) -> Dict[str, Any]:
        token_hex = self._require_hex(token_id, "token_id", expected_len=32)
        contract_reader = self._get_reader_contract()

        token_type, token_data = await contract_reader.functions.tokens(bytes(token_hex)).call()

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

        result["symbol"] = await self._get_token_symbol(token_hex)
        result["name"] = await self._get_token_name(token_hex)
        result["decimals"] = await self._get_token_decimals(token_hex)

        return result

    async def list_all_tokens(self) -> list[Dict[str, Any]]:
        """List all registered tokens from the contract (cached for 5 minutes)."""
        return await self._token_list_cache.get_or_set_async("all", self._fetch_all_tokens)

    async def _fetch_all_tokens(self) -> list[Dict[str, Any]]:
        """Fetch all registered tokens from the contract (uncached)."""
        contract_reader = self._get_reader_contract()
        token_ids = await contract_reader.functions.getRegisteredTokens().call()

        results = []
        for token_id in token_ids:
            token_id_hex = Web3.to_hex(token_id)
            try:
                info = await self.get_token_info(token_id_hex)
                results.append(info)
            except Exception:
                logger.warning("Failed to fetch info for token %s", token_id_hex)
        return results

    async def get_withdrawal_nonce(self, user_address: str) -> Dict[str, Any]:
        """Get the current withdrawal nonce for a user."""
        checksum_user = self._require_address(user_address, "user_address")
        contract_reader = self._get_reader_contract()
        nonce = await contract_reader.functions.withdrawalNonces(checksum_user).call()
        return {
            "user_address": checksum_user,
            "nonce": nonce,
        }

    async def get_transfer_nonce(self, user_address: str) -> Dict[str, Any]:
        """Get the current transfer nonce for a user."""
        checksum_user = self._require_address(user_address, "user_address")
        contract_reader = self._get_reader_contract()
        nonce = await contract_reader.functions.transferNonces(checksum_user).call()
        return {
            "user_address": checksum_user,
            "nonce": nonce,
        }

    async def get_lock_nonce(self, user_address: str) -> Dict[str, Any]:
        """Get the current createLock nonce for a user."""
        checksum_user = self._require_address(user_address, "user_address")
        contract_reader = self._get_reader_contract()
        nonce = await contract_reader.functions.createLockNonces(checksum_user).call()
        return {"user_address": checksum_user, "nonce": nonce}

    async def get_modify_lock_nonce(self, user_address: str) -> Dict[str, Any]:
        """Get the current modifyLock nonce for a user."""
        checksum_user = self._require_address(user_address, "user_address")
        contract_reader = self._get_reader_contract()
        nonce = await contract_reader.functions.modifyLockNonces(checksum_user).call()
        return {"user_address": checksum_user, "nonce": nonce}

    async def get_transfer_locked_nonce(self, service_address: str) -> Dict[str, Any]:
        """Get the current transferFromLock nonce for a service."""
        checksum_service = self._require_address(service_address, "service_address")
        contract_reader = self._get_reader_contract()
        nonce = await contract_reader.functions.transferLockedNonces(checksum_service).call()
        return {"service_address": checksum_service, "nonce": nonce}

    async def get_siwe_domain(self) -> Dict[str, Any]:
        if self._siwe_domain is None:
            contract_reader = await self._get_siwe_auth_reader_contract()
            self._siwe_domain = await contract_reader.functions.domain().call()
        return {"domain": self._siwe_domain}

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

    async def siwe_login(self, siwe_message: str, signature: str) -> Dict[str, Any]:
        contract_reader = await self._get_confidential_siwe_auth_reader_contract()
        r, s, v = self._parse_signature_rsv(signature)
        token = await contract_reader.functions.login(siwe_message, (r, s, v)).call()
        return {"token": _to_prefixed_hex(token)}

    async def set_auth_token_enc_key(self, enc_key: bytes) -> SubmissionResult:
        """Set the AuthToken encryption key in the contract.

        This is called during startup to sync the ROFL-derived encryption key
        with the contract. The contract verifies the ROFL origin.

        Args:
            enc_key: 32-byte Deoxys-II encryption key.

        Returns:
            Submission result with transaction ID.
        """
        if len(enc_key) != 32:
            raise ValueError(f"Encryption key must be 32 bytes, got {len(enc_key)}")

        # Function selector: keccak256("setAuthTokenEncKey(bytes32)")[:4]
        selector = Web3.keccak(text="setAuthTokenEncKey(bytes32)")[:4]
        # enc_key is already 32 bytes, just concatenate
        data = selector + enc_key

        siwe_auth_address = await self._get_siwe_auth_address()

        # Verify ROFL app ID matches before attempting to set key
        siwe_auth_reader = await self._get_siwe_auth_reader_contract()
        contract_rofl_app_id = await siwe_auth_reader.functions.roflAppId().call()
        our_rofl_app_id = await self.rofl_client._client.get_app_id()
        logger.info(
            "ROFL app ID check - contract expects: %s, we are: %s",
            contract_rofl_app_id.hex()
            if isinstance(contract_rofl_app_id, bytes)
            else contract_rofl_app_id,
            our_rofl_app_id,
        )

        # Compute and log the expected key hash for verification
        expected_key_hash = Web3.keccak(enc_key)
        logger.info(
            "Syncing AuthToken encryption key to SIWE auth contract at %s (key prefix: %s..., expected hash: %s)",
            siwe_auth_address,
            enc_key[:4].hex(),
            expected_key_hash.hex(),
        )

        # Submit via ROFL
        tx = {
            "to": siwe_auth_address,
            "value": 0,
            "gas": self.gas_limit,
            "data": Web3.to_hex(data),
        }

        await self.rofl_client.submit_tx(tx, encrypt=True)
        logger.info("AuthToken encryption key submitted to contract at %s", siwe_auth_address)

        return SubmissionResult(status="submitted")

    async def get_balance(
        self, user_address: str, token_id: str, siwe_token: bytes
    ) -> Dict[str, Any]:
        checksum_user = self._require_address(user_address, "user_address")
        token_hex = self._require_hex(token_id, "token_id", expected_len=32)
        balance = await self._fetch_balance(token_hex, siwe_token)
        context = await self._get_token_context(token_hex)
        return {
            "user_address": checksum_user,
            "token_id": _to_prefixed_hex(token_hex),
            "balance": str(balance),
            "token_symbol": await self._get_token_symbol(token_hex),
            "chain_id": str(context.chain_id),
        }

    async def _fetch_balance(self, token: HexBytes, siwe_token: bytes) -> int:
        contract_reader = await self._get_confidential_reader_contract()
        return await contract_reader.functions.balanceOf(bytes(token), siwe_token).call()

    async def get_batch_balances(
        self, user_address: str, token_ids_raw: list[str], siwe_token: bytes
    ) -> Dict[str, Any]:
        user = self._require_address(user_address, "user_address")
        if not isinstance(token_ids_raw, list) or len(token_ids_raw) == 0:
            raise ValueError("token_ids must be a non-empty array")

        token_ids: list[HexBytes] = [
            self._require_hex(token_id, "token_id", expected_len=32) for token_id in token_ids_raw
        ]

        response_balances = []
        for token_id in token_ids:
            balance = await self._fetch_balance(token_id, siwe_token)
            context = await self._get_token_context(token_id)
            response_balances.append(
                {
                    "token_id": _to_prefixed_hex(token_id),
                    "balance": str(balance),
                    "token_symbol": await self._get_token_symbol(token_id),
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
            "amount": str(int(amount)),
            "expiry": int(expiry),
            "is_expired": now >= int(expiry),
        }

    async def _fetch_user_locks(self, siwe_token: bytes) -> list[Any]:
        contract_reader = await self._get_confidential_reader_contract()
        return await contract_reader.functions.getUserLocks(siwe_token).call()

    async def get_locked_funds(
        self,
        user_address: str,
        service_address: Optional[str],
        siwe_token: bytes,
    ) -> Dict[str, Any]:
        user = self._require_address(user_address, "user_address")
        service = (
            self._require_address(service_address, "service_address") if service_address else None
        )

        locks: list[Any]
        # API private reads are user-token scoped; `service_address` here is an output filter.
        # Backend services that need service-authenticated reads should query
        # getServiceLocks(...) directly on the contract using Sapphire authenticated
        # view calls (for Python wrappers: signed query with empty token parameter).
        all_locks = await self._fetch_user_locks(siwe_token)
        if service is None:
            locks = all_locks
        else:
            locks = [lock for lock in all_locks if lock[1].lower() == service.lower()]

        now = await self._get_chain_timestamp()

        lock_infos = [self._lock_to_info(user, lock, now) for lock in locks]
        total_locked = sum(int(info["amount"]) for info in lock_infos)

        return {
            "user_address": user,
            "service_address": service,
            "locks": lock_infos,
            "total_locked": str(total_locked),
        }

    async def get_expired_locks(self, user_address: str, siwe_token: bytes) -> Dict[str, Any]:
        user = self._require_address(user_address, "user_address")

        now = await self._get_chain_timestamp()
        all_locks = await self._fetch_user_locks(siwe_token)
        expired_locks = [lock for lock in all_locks if now >= int(lock[4])]
        lock_infos = [self._lock_to_info(user, lock, now) for lock in expired_locks]
        return {"user_address": user, "expired_locks": lock_infos}

    async def get_total_locked_balance(
        self, user_address: str, token_id: str, siwe_token: bytes
    ) -> Dict[str, Any]:
        user = self._require_address(user_address, "user_address")
        token_hex = self._require_hex(token_id, "token_id", expected_len=32)

        locks = await self._fetch_user_locks(siwe_token)
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
