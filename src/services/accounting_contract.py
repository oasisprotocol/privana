"""Service for building and submitting Accounting contract transactions."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from eth_abi import decode as _abi_decode
from eth_abi.exceptions import DecodingError
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from sapphirepy import sapphire
from web3 import AsyncWeb3, Web3
from web3.constants import ADDRESS_ZERO
from web3.contract import AsyncContract
from web3.exceptions import BadFunctionCallOutput, ContractLogicError
from web3.middleware import SignAndSendRawMiddlewareBuilder
from web3.providers import AsyncHTTPProvider

from src.abi.accounting import ACCOUNTING_ABI
from src.abi.accounting_history import ACCOUNTING_HISTORY_ABI
from src.abi.accounting_siwe_auth import ACCOUNTING_SIWE_AUTH_ABI
from src.clients.rofl import ROFL_QUERY_SIGNER_KEY, RoflAppdClient
from src.config import (
    CHAIN_NAMES,
    NATIVE_TOKEN_DECIMALS,
    NATIVE_TOKEN_NAMES,
    NATIVE_TOKEN_SYMBOLS,
    load_settings,
)
from src.config.tokens import get_rose_token_id as _get_rose_token_id
from src.models.accounting import (
    HISTORY_KIND_WIRE_NAMES,
    HistoryKind,
    parse_chain_type,
)
from src.models.types import Settings
from src.services.cache import AsyncTTLCache
from src.utils.eth_logs import paginated_get_logs

logger = logging.getLogger(__name__)

# History payload field lengths (bytes), matching `abi.encodePacked` on-chain.
_HISTORY_TOKEN_ID_LEN = 32
_HISTORY_AMOUNT_LEN = 32
_HISTORY_DEPOSIT_ID_LEN = 32
_HISTORY_ADDRESS_LEN = 20
_HISTORY_DEPOSIT_PAYLOAD_LEN = _HISTORY_TOKEN_ID_LEN + _HISTORY_AMOUNT_LEN + _HISTORY_DEPOSIT_ID_LEN
_HISTORY_COUNTERPARTY_PAYLOAD_LEN = (
    _HISTORY_TOKEN_ID_LEN + _HISTORY_AMOUNT_LEN + _HISTORY_ADDRESS_LEN
)
_HISTORY_PAIRED_TRANSFER_PAYLOAD_LEN = _HISTORY_COUNTERPARTY_PAYLOAD_LEN + _HISTORY_ADDRESS_LEN
_INT256_MIN = -(2**255)
_INT256_MAX = 2**255 - 1
_HISTORY_MODULE_ID = HexBytes(Web3.keccak(text="privana.accounting.historyModule.v1")).to_0x_hex()

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

BRIDGE_BURN_RESERVATION_LOOKBACK_BLOCKS = int(
    os.getenv("BRIDGE_BURN_RESERVATION_LOOKBACK_BLOCKS", "10000")
)

_BRIDGE_BURN_LOOKBACK_UNSET: Any = object()


def _ensure_hex(value: str) -> str:
    if value.startswith("0x"):
        return value
    return "0x" + value


def _to_checksum(address: str) -> ChecksumAddress:
    return Web3.to_checksum_address(address)


def _to_prefixed_hex(value: Any) -> str:
    return HexBytes(value).to_0x_hex().lower()


def _decode_bridge_tx_identifier(
    tx_identifier: bytes,
) -> tuple[int, int, ChecksumAddress, int]:
    """Decode a BridgeAsset withdrawal's txIdentifier.

    Returns ``(dest_chain_id, dest_tx_nonce, route_address, max_gas_cost)`` per
    the on-chain encoding ``abi.encode(uint256, uint64, address, uint256)``.
    ``route_address`` is returned EIP-55 checksummed.
    """
    dest_chain_id, dest_tx_nonce, route_address, max_gas_cost = _abi_decode(
        ["uint256", "uint64", "address", "uint256"], tx_identifier
    )
    return (
        int(dest_chain_id),
        int(dest_tx_nonce),
        Web3.to_checksum_address(route_address),
        int(max_gas_cost),
    )


@dataclass
class SubmissionResult:
    """Plain DTO for transaction submission results."""

    submission_id: str
    status: str
    detail: Optional[str] = None


@dataclass
class TokenContext:
    """Derived metadata needed to craft withdrawal transactions."""

    chain_id: Optional[int]
    token_address: Optional[ChecksumAddress]
    is_native: bool
    is_bridge_asset: bool = False


@dataclass(frozen=True)
class BridgeBurnReservation:
    """Decoded ``BridgeBurnReserved`` event row.

    Returned by the paginated event scan in ``list_bridge_burn_reservations``;
    chain filtering happens in Python because the event indexes only
    ``depositId``. Single-deposit lookups should read canonical state via
    ``get_bridge_burn_nonce`` instead.
    """

    deposit_id: bytes
    chain_id: int
    bridge: ChecksumAddress
    amount: int
    nonce: int


class AccountingContractService:
    """Encapsulates Accounting contract interactions."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or load_settings()
        self.w3 = Web3()
        self.chain_id = self.settings.sapphire_chain_id
        self.gas_limit = self.settings.accounting_gas_limit
        self.contract_address = _to_checksum(self.settings.accounting_contract_address)
        self.history_module_address = _to_checksum(ADDRESS_ZERO)
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
        self._confidential_history_contract_reader: Optional[AsyncContract] = None
        self._history_module_validated = False
        self._deposit_address: Optional[str] = None
        self._siwe_domain: Optional[str] = None
        self._siwe_auth_address: Optional[ChecksumAddress] = None
        self._siwe_auth_reader: Optional[AsyncContract] = None
        self._confidential_siwe_auth_reader: Optional[AsyncContract] = None
        self._eip712_domain: Optional[Dict[str, Any]] = None

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
        rofl_result = await self.rofl_client.submit_tx(tx)
        return SubmissionResult(submission_id=rofl_result.submission_id, status="submitted")

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

    async def _get_eip712_domain(self) -> Dict[str, Any]:
        if self._eip712_domain is not None:
            return self._eip712_domain

        contract_reader = self._get_reader_contract()
        domain = await contract_reader.functions.eip712Domain().call()
        self._eip712_domain = {
            "name": domain[1],
            "version": domain[2],
            "chainId": int(domain[3]),
            "verifyingContract": _to_checksum(domain[4]),
        }
        return self._eip712_domain

    async def _recover_eip712_signer(
        self,
        primary_type: str,
        message_types: list[Dict[str, str]],
        message: Dict[str, Any],
        signature: HexBytes,
    ) -> ChecksumAddress:
        domain = await self._get_eip712_domain()
        try:
            signable = encode_typed_data(
                domain_data=domain,
                message_types={primary_type: message_types},
                message_data=message,
            )
            signer = Account.recover_message(signable, signature=signature)
        except Exception as exc:
            raise ValueError("Invalid EIP-712 signature") from exc

        return _to_checksum(signer)

    async def _validate_user_nonce(
        self, user: ChecksumAddress, nonce: int, nonce_fn: str, label: str
    ) -> None:
        contract_reader = self._get_reader_contract()
        expected_nonce = await getattr(contract_reader.functions, nonce_fn)(user).call()
        if nonce != expected_nonce:
            raise ValueError(
                f"{label} nonce mismatch: got {nonce}, expected {expected_nonce}. "
                f"The nonce may already have been used by another request."
            )

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

    def _set_history_module_address(self, module_address: ChecksumAddress) -> None:
        self.history_module_address = module_address
        self._confidential_history_contract_reader = None

    async def _validate_history_module_address(self, module_address: ChecksumAddress) -> None:
        if module_address == _to_checksum(ADDRESS_ZERO):
            raise ValueError("AccountingHistoryModule is not configured")
        if self.reader_w3 is None:
            raise ValueError(
                "SAPPHIRE_RPC_URL must be configured to read AccountingHistoryModule settings"
            )

        code = await self.reader_w3.eth.get_code(module_address)
        if len(code) == 0:
            raise ValueError("AccountingHistoryModule address has no contract code")

        history_reader = self.reader_w3.eth.contract(
            address=module_address,
            abi=ACCOUNTING_HISTORY_ABI,
        )
        try:
            module_id = HexBytes(await history_reader.functions.MODULE_ID().call()).to_0x_hex()
        except (BadFunctionCallOutput, ContractLogicError, DecodingError) as exc:
            raise ValueError("Could not read AccountingHistoryModule MODULE_ID") from exc
        if module_id != _HISTORY_MODULE_ID:
            raise ValueError("AccountingHistoryModule has unexpected module id")

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
            # In ROFL deployments, use the dedicated query-signer key so msg.sender on signed
            # queries matches the address published on-chain via setRoflSignerAddress.
            try:
                private_key, _ = await self.rofl_client.get_keypair(ROFL_QUERY_SIGNER_KEY)
            except Exception as exc:  # pragma: no cover - depends on ROFL runtime
                raise ValueError(
                    "SAPPHIRE_VIEW_PRIVATE_KEY is not set and ROFL appd key is unavailable"
                ) from exc

        account: LocalAccount = Account.from_key(private_key)

        w3 = AsyncWeb3(AsyncHTTPProvider(self.sapphire_rpc_url))
        w3.middleware_onion.add(SignAndSendRawMiddlewareBuilder.build(account))
        wrapped_w3 = sapphire.wrap(w3, account)
        wrapped_w3.eth.default_account = account.address

        self._confidential_reader_w3 = wrapped_w3
        self._confidential_contract_reader = wrapped_w3.eth.contract(
            address=self.contract_address, abi=ACCOUNTING_ABI
        )
        return self._confidential_contract_reader

    async def _resolve_history_module_address(self) -> ChecksumAddress:
        contract_reader = self._get_reader_contract()
        module_address = _to_checksum(await contract_reader.functions.historyModule().call())

        if module_address != self.history_module_address:
            await self._validate_history_module_address(module_address)
            self._set_history_module_address(module_address)
            self._history_module_validated = True
            return module_address

        if not getattr(self, "_history_module_validated", False):
            await self._validate_history_module_address(module_address)
            self._history_module_validated = True

        return module_address

    async def _get_confidential_history_reader_contract(self) -> AsyncContract:
        await self._resolve_history_module_address()
        if self._confidential_history_contract_reader is not None:
            return self._confidential_history_contract_reader

        await self._get_confidential_reader_contract()
        if self._confidential_reader_w3 is None:
            raise ValueError("Confidential reader is not initialized")

        self._confidential_history_contract_reader = self._confidential_reader_w3.eth.contract(
            address=self.contract_address,
            abi=ACCOUNTING_HISTORY_ABI,
        )
        return self._confidential_history_contract_reader

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

    async def _get_token_context(self, token: HexBytes) -> TokenContext:
        return await self._token_context_cache.get_or_set_async(
            token.hex(), lambda: self._fetch_token_context(token)
        )

    async def _fetch_token_context(self, token: HexBytes) -> TokenContext:
        """Fetch token context from contract (uncached)."""
        contract = self._get_reader_contract()
        token_type, token_data = await contract.functions.tokens(bytes(token)).call()

        chain_id: Optional[int]
        token_address: Optional[ChecksumAddress]
        is_bridge_asset = False

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
        elif token_type == 2:
            # BridgeAsset (e.g. ROSE): the on-chain `data` is just the UTF-8
            # symbol — there is no canonical chain_id or contract address. The
            # destination chain for a bridge withdrawal is decoded from the
            # withdrawal's `txIdentifier` at grouping time; callers must NOT
            # use `chain_id=None` here for per-chain routing.
            chain_id = None
            token_address = None
            is_native = False
            is_bridge_asset = True
        else:
            raise ValueError(f"Unsupported token type: {token_type}")

        return TokenContext(
            chain_id=chain_id,
            token_address=token_address,
            is_native=is_native,
            is_bridge_asset=is_bridge_asset,
        )

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

    # ------------------------------------------------------------------
    # Per-user deposit address operations
    # ------------------------------------------------------------------

    async def resolve_address_from_token(self, siwe_token: bytes) -> str:
        """Decrypt a SIWE token on-chain and return the authenticated user address."""
        contract = await self._get_confidential_siwe_auth_reader_contract()
        address = await contract.functions.authSender(siwe_token).call()
        return Web3.to_checksum_address(address)

    async def get_deposit_address(self, chain_type: str, version: int, siwe_token: bytes) -> str:
        """Call getDepositAddress with SIWE token to derive the user's deposit address."""
        contract = await self._get_confidential_reader_contract()
        chain_type_enum = parse_chain_type(chain_type)
        address = await contract.functions.getDepositAddress(
            chain_type_enum.value, version, siwe_token
        ).call()
        return Web3.to_checksum_address(address)

    async def get_rose_token_id(self) -> bytes:
        """Read and cache Accounting.ROSE_TOKEN_ID via the shared token helper."""
        return await _get_rose_token_id(self)

    async def get_custody_address(self) -> str:
        """Return the custody EOA address used by sweep destinations."""
        return Web3.to_checksum_address(await self._get_deposit_address())

    async def credit_deposit(
        self,
        beneficiary: str,
        token_id: bytes,
        amount: int,
        deposit_id: bytes,
    ) -> SubmissionResult:
        """Credit a deposit to a beneficiary via ROFL."""
        user = self._require_address(beneficiary, "beneficiary")
        fn = self.contract.functions.creditDeposit(
            user,
            token_id,
            amount,
            deposit_id,
        )
        return await self._submit(fn._encode_transaction_data())

    async def generate_sweep_native(
        self,
        beneficiary: str,
        chain_type: str,
        version: int,
        chain_id: int,
        amount: int,
        nonce: int,
        gas_price: int,
    ) -> bytes:
        """Generate a signed sweep tx for native tokens. Returns raw signed tx bytes."""
        user = self._require_address(beneficiary, "beneficiary")
        chain_type_enum = parse_chain_type(chain_type)
        contract = await self._get_confidential_reader_contract()
        signed_tx = await contract.functions.generateSweepNativeTransfer(
            user, chain_type_enum.value, version, chain_id, amount, nonce, gas_price
        ).call()
        return signed_tx

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
    ) -> bytes:
        """Generate a signed sweep tx for ERC-20 tokens. Returns raw signed tx bytes."""
        user = self._require_address(beneficiary, "beneficiary")
        token_addr = self._require_address(token_address, "token_address")
        chain_type_enum = parse_chain_type(chain_type)
        contract = await self._get_confidential_reader_contract()
        signed_tx = await contract.functions.generateSweepERC20Transfer(
            user, chain_type_enum.value, version, chain_id, token_addr, amount, nonce, gas_price
        ).call()
        return signed_tx

    async def generate_gas_funding_tx(
        self,
        to_deposit_address: str,
        chain_id: int,
        gas_amount: int,
        gas_tank_nonce: int,
        gas_price: int,
    ) -> bytes:
        """Generate a signed gas funding tx. Returns raw signed tx bytes."""
        to_addr = self._require_address(to_deposit_address, "to_deposit_address")
        contract = await self._get_confidential_reader_contract()
        signed_tx = await contract.functions.generateGasFundingTx(
            to_addr, chain_id, gas_amount, gas_tank_nonce, gas_price
        ).call()
        return signed_tx

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
    ) -> bytes:
        """Sign an ERC20 sweep whose recipient is the configured ROFLBridge route.

        Unlike ``generate_sweep_erc20`` (which targets the custody EOA), this
        helper routes the transfer to ``roflBridgeAddress[84532]`` so the bridge
        contract can call ``burn`` against its own balance afterwards.
        """
        user = self._require_address(beneficiary, "beneficiary")
        token_addr = self._require_address(token_address, "token_address")
        chain_type_enum = parse_chain_type(chain_type)
        contract = await self._get_confidential_reader_contract()
        signed_tx = await contract.functions.generateSweepERC20TransferToBridge(
            user,
            chain_type_enum.value,
            version,
            chain_id,
            token_addr,
            amount,
            source_chain_nonce,
            gas_price,
        ).call()
        return signed_tx

    async def reserve_bridge_burn(
        self,
        deposit_id: bytes,
        chain_id: int,
        bridge: str,
        amount: int,
    ) -> SubmissionResult:
        """Reserve a custody-EOA burn nonce on Sapphire for ``deposit_id``.

        Submitted through ROFL. The reservation lands in the
        ``bridgeBurnRequests[depositId]`` mapping and emits
        ``BridgeBurnReserved(depositId, chainId, bridge, amount, nonce)``;
        ``get_bridge_burn_nonce`` reads the canonical mapping value via the
        ``getBridgeBurnRequest`` view on ``BridgeModule``.
        """
        if len(deposit_id) != 32:
            raise ValueError("deposit_id must be 32 bytes")
        bridge_addr = self._require_address(bridge, "bridge")
        amount_int = self._require_positive(amount, "amount")
        fn = self.contract.functions.reserveBridgeBurn(
            deposit_id, chain_id, bridge_addr, amount_int
        )
        return await self._submit(fn._encode_transaction_data())

    async def set_rofl_bridge(self, chain_id: int, bridge: str) -> SubmissionResult:
        """Write ``Accounting.roflBridgeAddress[chain_id] = bridge`` via ROFL.

        The on-chain setter is intentionally minimal — it writes and emits
        ``RoflBridgeUpdated``. Route-change safety (drain in-flight inbound
        before rotating) lives off-chain in the reconciler, because the
        ``onlyROFL`` gate already makes the TEE the only writer.
        """
        bridge_addr = self._require_address(bridge, "bridge")
        fn = self.contract.functions.setRoflBridge(chain_id, bridge_addr)
        return await self._submit(fn._encode_transaction_data())

    async def get_bridge_burn_nonce(self, deposit_id: bytes) -> int:
        """Recover the reserved EVM nonce for ``deposit_id`` from canonical state.

        Reads the ``BridgeBurnRequest`` struct via the typed getter on
        BridgeModule (routed through the Accounting proxy fallback). O(1) on
        the contract side and unaffected by RPC log-range limits (Sapphire
        confidential VM caps ``eth_getLogs`` at 100 blocks).

        Raises ``ValueError`` if no reservation exists for ``deposit_id`` —
        the natural signal that the reservation transaction has not been mined
        and a bounded retry should follow.
        """
        if len(deposit_id) != 32:
            raise ValueError("deposit_id must be 32 bytes")
        contract = self._get_reader_contract()
        _chain_id, _bridge, _amount, nonce, exists = await contract.functions.getBridgeBurnRequest(
            deposit_id
        ).call()
        if not exists:
            raise ValueError(f"No BridgeBurnRequest reservation for depositId={deposit_id.hex()}")
        return int(nonce)

    async def list_bridge_burn_reservations(
        self,
        chain_id: Optional[int] = None,
        *,
        lookback_blocks: Any = _BRIDGE_BURN_LOOKBACK_UNSET,
    ) -> list[BridgeBurnReservation]:
        """Return ``BridgeBurnReserved`` events, newest last; bounded by
        ``lookback_blocks`` (defaults to the module constant; ``None`` =
        scan from genesis).
        """
        contract = self._get_reader_contract()
        if self.reader_w3 is None:
            raise ValueError("SAPPHIRE_RPC_URL must be configured to list bridge burn reservations")
        if lookback_blocks is _BRIDGE_BURN_LOOKBACK_UNSET:
            lookback_blocks = BRIDGE_BURN_RESERVATION_LOOKBACK_BLOCKS
        head = await self.reader_w3.eth.block_number
        from_block = 0 if lookback_blocks is None else max(0, head - int(lookback_blocks))
        events = await paginated_get_logs(
            contract.events.BridgeBurnReserved,
            from_block=from_block,
            to_block=head,
        )
        reservations: list[BridgeBurnReservation] = []
        for event in events:
            args = event["args"]
            event_chain_id = int(args["chainId"])
            if chain_id is not None and event_chain_id != chain_id:
                continue
            reservations.append(
                BridgeBurnReservation(
                    deposit_id=bytes(args["depositId"]),
                    chain_id=event_chain_id,
                    bridge=Web3.to_checksum_address(args["bridge"]),
                    amount=int(args["amount"]),
                    nonce=int(args["nonce"]),
                )
            )
        return reservations

    async def generate_bridge_burn_transfer(self, deposit_id: bytes) -> bytes:
        """Sign the ``ROFLBridge.burn(amount, depositId)`` transaction.

        Reads the stored ``BridgeBurnRequest`` and reverts ``BridgeBurnNotFound``
        until ``reserve_bridge_burn`` has been mined. The signed payload comes
        out of the delegated ``BridgeModule.generateBridgeBurnTransfer`` and is
        ready to be handed to the custody-tx executor.
        """
        if len(deposit_id) != 32:
            raise ValueError("deposit_id must be 32 bytes")
        contract = await self._get_confidential_reader_contract()
        signed_tx = await contract.functions.generateBridgeBurnTransfer(deposit_id).call()
        return signed_tx

    async def resolve_bridge_withdrawal(self, index: int) -> bytes:
        """Sign a bridge-withdrawal destination-chain tx via the delegated module.

        State-idempotent: only the first call emits ``WithdrawalResolved``. The
        signed bytes can differ between calls if ``gasPrices[destChainId]`` was
        updated in between.
        """
        contract = await self._get_confidential_reader_contract()
        signed_tx = await contract.functions.resolveBridgeWithdrawal(index).call()
        return signed_tx

    async def sign_nonce_burn(self, chain_id: int, nonce: int) -> bytes:
        """Sign a no-op self-transfer to advance the custody EOA past a stuck nonce.

        The signed payload is a custody-EOA-to-itself transfer with value 0 at the
        given ``nonce``, used to mine through a reserved-but-un-mineable slot. The
        contract reverts unless the owner first authorized that slot via
        ``clearCustodyTx(..., ClearAction.BurnNonce, 0)``, and reverts
        ``GasPriceNotSet`` if ``gasPrices[chainId] == 0``.
        """
        contract = await self._get_confidential_reader_contract()
        signed_tx = await contract.functions.signNonceBurn(chain_id, nonce).call()
        return signed_tx

    async def submit_resolve_bridge_withdrawal(self, index: int) -> SubmissionResult:
        """Submit resolveBridgeWithdrawal via ROFL (BridgeAsset variant of resolveWithdrawal)."""
        fn = self.contract.functions.resolveBridgeWithdrawal(index)
        return await self._submit(fn._encode_transaction_data())

    async def get_gas_price(self, chain_id: int) -> int:
        """Read ``gasPrices[chain_id]`` from the Accounting contract."""
        contract = self._get_reader_contract()
        return int(await contract.functions.gasPrices(chain_id).call())

    async def get_clear_applied_hash(self, chain_id: int, nonce: int) -> bytes:
        """Read the first-clear-wins coordination hash for a custody-tx clear.

        Returns the bytes32 stored for the clear at ``(chain_id, nonce)``. An
        all-zero bytes32 means no clear has been applied for that slot.
        """
        contract = self._get_reader_contract()
        value = await contract.functions.clearAppliedHash(chain_id, nonce).call()
        return bytes(value)

    # ------------------------------------------------------------------
    # Deposit verification helpers
    # ------------------------------------------------------------------

    async def get_gas_tank_address(self) -> str:
        """Read gasTankAddress from the contract (public getter)."""
        contract = self._get_reader_contract()
        address = await contract.functions.gasTankAddress().call()
        return Web3.to_checksum_address(address)

    async def get_token_id(self, chain_id: int, token_address: str | None) -> bytes:
        """Compute tokenId by calling the contract's encode + hash functions.

        Avoids abi.encode vs abi.encodePacked mismatch by delegating to the contract.
        """
        contract = self._get_reader_contract()
        if token_address is None:
            data = await contract.functions.encodeEVMNativeTokenData(chain_id).call()
            token_id = await contract.functions.getTokenId((0, data)).call()
        else:
            addr = Web3.to_checksum_address(token_address)
            data = await contract.functions.encodeEVMErc20TokenData(chain_id, addr).call()
            token_id = await contract.functions.getTokenId((1, data)).call()
        return token_id

    async def is_deposit_processed(self, deposit_id: bytes) -> bool:
        """Check if a deposit has already been processed on-chain.

        Used for idempotency: /deposits/check pre-checks this before sweeping.
        """
        contract = self._get_reader_contract()
        return await contract.functions.processedDeposits(deposit_id).call()

    async def get_rofl_bridge_address(self, chain_id: int) -> str:
        contract = self._get_reader_contract()
        return await contract.functions.roflBridgeAddress(chain_id).call()

    async def is_token_registered(self, token_id: bytes) -> bool:
        """Check if a token is registered on-chain.

        Fail-fast check before sweeping — avoids wasting a sweep
        transaction on a token that creditDeposit would reject.
        """
        contract = self._get_reader_contract()
        _token_type, data = await contract.functions.tokens(bytes(token_id)).call()
        return len(data) > 0

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    async def lock_funds(self, payload: Dict) -> SubmissionResult:
        service = self._require_address(payload["service_address"], "service_address")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        amount = self._require_positive(payload["amount"], "amount")
        expiry = self._require_positive(payload["expiry"], "expiry")
        nonce = self._require_positive(payload["nonce"], "nonce", allow_zero=True)
        signature = self._require_hex(payload["signature"], "signature")
        user = await self._recover_eip712_signer(
            "Lock",
            [
                {"name": "serviceAddress", "type": "address"},
                {"name": "tokenId", "type": "bytes32"},
                {"name": "amount", "type": "uint256"},
                {"name": "expiry", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
            ],
            {
                "serviceAddress": service,
                "tokenId": _to_prefixed_hex(token),
                "amount": amount,
                "expiry": expiry,
                "nonce": nonce,
            },
            signature,
        )
        await self._validate_user_nonce(user, nonce, "createLockNonces", "createLock")

        fn = self.contract.functions.createLock(
            service,
            token,
            amount,
            expiry,
            nonce,
            signature,
        )
        return await self._submit(fn._encode_transaction_data())

    async def modify_lock(self, payload: Dict) -> SubmissionResult:
        lock_id = self._require_positive(payload["lock_id"], "lock_id")
        amount = self._require_positive(payload["amount"], "amount", allow_zero=True)
        new_expiry = self._require_positive(payload["new_expiry"], "new_expiry")
        nonce = self._require_positive(payload["nonce"], "nonce", allow_zero=True)
        signature = self._require_hex(payload["signature"], "signature")
        user = await self._recover_eip712_signer(
            "ModifyLock",
            [
                {"name": "lockId", "type": "uint256"},
                {"name": "amount", "type": "uint256"},
                {"name": "newExpiry", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
            ],
            {
                "lockId": lock_id,
                "amount": amount,
                "newExpiry": new_expiry,
                "nonce": nonce,
            },
            signature,
        )
        await self._validate_user_nonce(user, nonce, "modifyLockNonces", "modifyLock")

        fn = self.contract.functions.modifyLock(
            lock_id,
            amount,
            new_expiry,
            nonce,
            signature,
        )
        return await self._submit(fn._encode_transaction_data())

    async def transfer_funds(self, payload: Dict) -> SubmissionResult:
        to_addr = self._require_address(payload["to_address"], "to_address")
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        amount = self._require_positive(payload["amount"], "amount")
        nonce = self._require_positive(payload["nonce"], "nonce", allow_zero=True)
        signature = self._require_hex(payload["signature"], "signature")
        user = await self._recover_eip712_signer(
            "Transfer",
            [
                {"name": "toAddress", "type": "address"},
                {"name": "tokenId", "type": "bytes32"},
                {"name": "amount", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
            ],
            {
                "toAddress": to_addr,
                "tokenId": _to_prefixed_hex(token),
                "amount": amount,
                "nonce": nonce,
            },
            signature,
        )
        await self._validate_user_nonce(user, nonce, "transferNonces", "Transfer")

        fn = self.contract.functions.transferBalance(
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

    async def withdraw_from_lock(
        self, payload: Dict, user_address: str, siwe_token: bytes
    ) -> SubmissionResult:
        user = self._require_address(user_address, "user_address")
        to_addr = self._require_address(payload["to_address"], "to_address")
        lock_id = self._require_positive(payload["lock_id"], "lock_id")
        amount = self._require_positive(payload["amount"], "amount")
        nonce = self._require_positive(payload["nonce"], "nonce", allow_zero=True)
        signature = self._require_hex(payload["signature"], "signature")

        if str(to_addr).lower() == ADDRESS_ZERO:
            raise ValueError("to_address must not be the zero address")

        user_locks = await self._fetch_user_locks(siwe_token)
        lock = next((entry for entry in user_locks if int(entry[0]) == lock_id), None)
        if lock is None:
            raise ValueError(f"lock_id {lock_id} not found for user {user}")

        token = HexBytes(lock[2])
        context = await self._get_token_context(token)

        if context.chain_id not in self.settings.chain_rpc_urls:
            raise ValueError(
                f"No RPC URL configured for chain_id {context.chain_id}. "
                f"Cannot process withdrawal - destination chain not supported."
            )

        await self._check_destination_balance(context.chain_id, context.is_native, amount)

        fn = self.contract.functions.withdrawFromLock(
            user,
            to_addr,
            lock_id,
            amount,
            nonce,
            signature,
        )
        result = await self._submit(fn._encode_transaction_data())

        detail_parts = [f"chain_id={context.chain_id}"]
        if context.token_address:
            detail_parts.append(f"token_address={context.token_address}")
        result.detail = "; ".join(detail_parts)

        return result

    async def unlock_funds(self, payload: Dict) -> SubmissionResult:
        user = self._require_address(payload["user_address"], "user_address")
        lock_id = self._require_positive(payload["lock_id"], "lock_id")

        fn = self.contract.functions.unlockSingleLock(
            user,
            lock_id,
        )
        return await self._submit(fn._encode_transaction_data())

    async def request_withdrawal(self, payload: Dict) -> SubmissionResult:
        token = self._require_hex(payload["token_id"], "token_id", expected_len=32)
        amount = self._require_positive(payload["amount"], "amount")
        nonce = self._require_positive(payload["nonce"], "nonce", allow_zero=True)
        signature = self._require_hex(payload["signature"], "signature")
        user = await self._recover_eip712_signer(
            "Withdraw",
            [
                {"name": "tokenId", "type": "bytes32"},
                {"name": "amount", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
            ],
            {
                "tokenId": _to_prefixed_hex(token),
                "amount": amount,
                "nonce": nonce,
            },
            signature,
        )
        await self._validate_user_nonce(user, nonce, "withdrawalNonces", "Withdrawal")

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
            token,
            amount,
            nonce,
            signature,
        )

        rofl_result = await self.rofl_client.submit_tx(
            self._build_tx(fn._encode_transaction_data())
        )

        detail_parts = [f"chain_id={context.chain_id}"]
        if context.token_address:
            detail_parts.append(f"token_address={context.token_address}")
        detail = "; ".join(detail_parts)

        return SubmissionResult(
            submission_id=rofl_result.submission_id, status="submitted", detail=detail
        )

    async def request_bridge_withdrawal(self, payload: Dict) -> SubmissionResult:
        """Submit ``requestBridgeWithdrawal`` via ROFL."""
        user = self._require_address(payload["user_address"], "user_address")
        to = self._require_address(payload["to_address"], "to_address")
        dest_chain_id = self._require_positive(payload["dest_chain_id"], "dest_chain_id")
        route = self._require_address(payload["route_address"], "route_address")
        amount = self._require_positive(payload["amount"], "amount")
        max_gas_cost = self._require_positive(
            payload["max_gas_cost"], "max_gas_cost", allow_zero=True
        )
        user_nonce = self._require_positive(payload["user_nonce"], "user_nonce", allow_zero=True)
        signature = self._require_hex(payload["signature"], "signature")

        fn = self.contract.functions.requestBridgeWithdrawal(
            user, to, dest_chain_id, route, amount, max_gas_cost, user_nonce, signature
        )
        rofl_result = await self.rofl_client.submit_tx(
            self._build_tx(fn._encode_transaction_data())
        )
        detail = f"destChainId={dest_chain_id}; routeAddress={route}"
        return SubmissionResult(
            submission_id=rofl_result.submission_id, status="submitted", detail=detail
        )

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
            "to_address": result[1],
            "amount": str(result[2]),
            "block_number": result[3],
            "token_id": "0x" + result[4].hex(),
            "resolved": result[5],
            "tx_identifier": "0x" + result[6].hex() if result[6] else "0x",
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
                resolved = result[5]

                if withdrawal_user.lower() == checksum_user.lower() and not resolved:
                    pending.append(
                        {
                            "index": index,
                            "user_address": result[0],
                            "to_address": result[1],
                            "amount": str(result[2]),
                            "block_number": result[3],
                            "token_id": "0x" + result[4].hex(),
                            "resolved": result[5],
                            "tx_identifier": "0x" + result[6].hex() if result[6] else "0x",
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
                resolved = result[5]

                # Skip if already resolved
                if resolved:
                    continue

                # Apply user filter
                if checksum_user and withdrawal_user.lower() != checksum_user.lower():
                    continue

                block_number = result[3]
                token_id_bytes = result[4]
                tx_identifier = result[6]

                # Get chain_id for this token. BridgeAsset records intentionally
                # carry chain_id=None on the TokenContext — the real destination
                # chain is encoded inside the per-withdrawal txIdentifier and
                # decoded here so per-chain grouping downstream sees a real id.
                token_hex = HexBytes(token_id_bytes)
                try:
                    context = await self._get_token_context(token_hex)
                except Exception as e:
                    logger.warning(
                        f"Withdrawal #{index}: unknown/invalid token 0x{token_id_bytes.hex()} - {e}"
                    )
                    context = None

                record: Dict[str, Any] = {
                    "index": index,
                    "user_address": withdrawal_user,
                    "to_address": result[1],
                    "amount": str(result[2]),
                    "token_id": "0x" + token_id_bytes.hex(),
                    "block_number": block_number,
                    "can_resolve": current_block - block_number >= 1,
                    "chain_id": context.chain_id if context else None,
                }

                if context is not None and context.is_bridge_asset:
                    try:
                        (
                            dest_chain_id,
                            dest_tx_nonce,
                            route_address,
                            max_gas_cost,
                        ) = _decode_bridge_tx_identifier(tx_identifier)
                    except Exception as e:
                        logger.warning(
                            f"Withdrawal #{index}: malformed bridge txIdentifier "
                            f"0x{tx_identifier.hex() if tx_identifier else ''} - {e}"
                        )
                        continue
                    record["chain_id"] = dest_chain_id
                    record["dest_tx_nonce"] = dest_tx_nonce
                    record["route_address"] = route_address
                    record["max_gas_cost"] = max_gas_cost
                    record["is_bridge_asset"] = True

                pending.append(record)
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

    async def get_rofl_signer_address(self) -> str:
        """Read the currently published ROFL signer address from the contract."""
        contract = self._get_reader_contract()
        address = await contract.functions.roflSignerAddress().call()
        return Web3.to_checksum_address(address)

    async def set_rofl_signer_address(self, new_signer: str) -> SubmissionResult:
        """Publish the ROFL-derived signer address on-chain via a ROFL-authenticated tx."""
        checksum = self._require_address(new_signer, "new_signer")
        fn = self.contract.functions.setRoflSignerAddress(checksum)
        return await self._submit(fn._encode_transaction_data())

    async def set_auth_token_enc_key(self, enc_key: bytes) -> None:
        """Sync the AuthToken encryption key to the AccountingSiweAuth contract via ROFL."""
        if len(enc_key) != 32:
            raise ValueError(f"Encryption key must be 32 bytes, got {len(enc_key)}")
        auth_address = await self._get_siwe_auth_address()
        # ABI-encode setAuthTokenEncKey(bytes32)
        fn_selector = Web3.keccak(text="setAuthTokenEncKey(bytes32)")[:4]
        data = fn_selector + enc_key
        tx = {
            "to": auth_address,
            "value": 0,
            "gas": self.gas_limit,
            "data": Web3.to_hex(data),
        }
        await self.rofl_client.submit_tx(tx, encrypt=True)

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

    @staticmethod
    def _decode_history_payload(
        kind: HistoryKind, payload: Any, owner_address: Optional[str] = None
    ) -> Dict[str, Any]:
        payload_bytes = bytes(HexBytes(payload))
        match kind:
            case HistoryKind.Deposit:
                expected_len = _HISTORY_DEPOSIT_PAYLOAD_LEN
                tail_field = "deposit_id"
            case (
                HistoryKind.Withdraw
                | HistoryKind.CreateLock
                | HistoryKind.ModifyLock
                | HistoryKind.UnlockLock
            ):
                expected_len = _HISTORY_COUNTERPARTY_PAYLOAD_LEN
                tail_field = "counterparty"
            case HistoryKind.TransferFromLock | HistoryKind.TransferBalance:
                expected_len = None
                tail_field = "transfer_pair"
            case _:
                raise ValueError(f"Unsupported history kind {kind.name}")

        if expected_len is not None and len(payload_bytes) != expected_len:
            raise ValueError(
                f"History payload for {kind.name} must be "
                f"{expected_len} bytes, got {len(payload_bytes)} bytes"
            )
        if expected_len is None and len(payload_bytes) not in (
            _HISTORY_COUNTERPARTY_PAYLOAD_LEN,
            _HISTORY_PAIRED_TRANSFER_PAYLOAD_LEN,
        ):
            raise ValueError(
                f"History payload for {kind.name} must be "
                f"{_HISTORY_COUNTERPARTY_PAYLOAD_LEN} or "
                f"{_HISTORY_PAIRED_TRANSFER_PAYLOAD_LEN} bytes, got {len(payload_bytes)} bytes"
            )

        decoded: Dict[str, Any] = {
            "token_id": _to_prefixed_hex(payload_bytes[:_HISTORY_TOKEN_ID_LEN]),
            "amount": str(
                int.from_bytes(
                    payload_bytes[
                        _HISTORY_TOKEN_ID_LEN : _HISTORY_TOKEN_ID_LEN + _HISTORY_AMOUNT_LEN
                    ],
                    "big",
                )
            ),
            "counterparty": None,
            "from_address": None,
            "to_address": None,
            "deposit_id": None,
        }
        tail = payload_bytes[_HISTORY_TOKEN_ID_LEN + _HISTORY_AMOUNT_LEN :]
        if tail_field == "deposit_id":
            decoded["deposit_id"] = _to_prefixed_hex(tail)
        elif tail_field == "transfer_pair" and len(tail) == 2 * _HISTORY_ADDRESS_LEN:
            from_address = Web3.to_checksum_address(tail[:_HISTORY_ADDRESS_LEN])
            to_address = Web3.to_checksum_address(tail[_HISTORY_ADDRESS_LEN:])
            owner = Web3.to_checksum_address(owner_address) if owner_address else None
            decoded["from_address"] = from_address
            decoded["to_address"] = to_address
            if owner == to_address:
                decoded["counterparty"] = from_address
            elif owner == from_address:
                decoded["counterparty"] = to_address
        else:
            decoded["counterparty"] = Web3.to_checksum_address(tail)
        return decoded

    @staticmethod
    def _unknown_history_entry(timestamp: int) -> Dict[str, Any]:
        return {
            "kind": "unknown",
            "timestamp": timestamp,
            "token_id": None,
            "amount": None,
            "counterparty": None,
            "from_address": None,
            "to_address": None,
            "deposit_id": None,
            "chain_id": None,
        }

    async def _history_entry_to_dict(
        self, entry: Any, owner_address: Optional[str] = None
    ) -> Dict[str, Any]:
        kind, timestamp, payload = entry
        timestamp_int = int(timestamp)
        try:
            kind_enum = HistoryKind(int(kind))
            decoded = self._decode_history_payload(kind_enum, payload, owner_address)
        except ValueError as exc:
            logger.warning("History entry decode failed (timestamp=%d): %s", timestamp_int, exc)
            return self._unknown_history_entry(timestamp_int)

        try:
            token_hex = HexBytes(decoded["token_id"])
            context = await self._get_token_context(token_hex)
            chain_id: Optional[int] = int(context.chain_id)
        except ValueError as exc:
            logger.warning(
                "History token context lookup failed (timestamp=%d): %s", timestamp_int, exc
            )
            chain_id = None

        return {
            "kind": HISTORY_KIND_WIRE_NAMES[kind_enum],
            "timestamp": timestamp_int,
            **decoded,
            "chain_id": chain_id,
        }

    async def get_history(
        self, offset: int, limit: int, siwe_token: bytes, user_address: str
    ) -> Dict[str, Any]:
        """Fetch one page of history. Page 0 is the oldest; pass ``offset=-1`` for the newest page."""
        if offset < _INT256_MIN or offset > _INT256_MAX:
            raise ValueError("offset must fit int256")
        if limit < 0:
            raise ValueError("limit must be >= 0")
        owner_address = self._require_address(user_address, "user_address")

        contract_reader = await self._get_confidential_history_reader_contract()
        entries, total = await contract_reader.functions.getHistory(
            offset, limit, siwe_token
        ).call()

        history = await asyncio.gather(
            *(self._history_entry_to_dict(entry, owner_address) for entry in entries)
        )
        return {
            "history": list(history),
            "total": int(total),
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
