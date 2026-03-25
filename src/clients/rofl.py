"""ROFL client helpers for key management and transaction submission."""

import base64
import logging

from eth_account import Account
from oasis_rofl_client import AsyncRoflClient
from web3.types import TxParams

from src.abi.accounting import get_error_name

logger = logging.getLogger(__name__)


class TransactionRevertedError(Exception):
    """Raised when an on-chain transaction reverts."""

    def __init__(
        self,
        message: str,
        code: int | None = None,
        module: str | None = None,
        error_name: str | None = None,
    ):
        self.code = code
        self.module = module
        self.error_name = error_name
        super().__init__(message)


def _decode_revert_reason(raw_message: str | None) -> str:
    """Try to decode a revert reason from base64-encoded error selector.

    The raw_message typically looks like "reverted: <base64>" where <base64>
    contains the error selector and any additional data.
    """
    if not raw_message:
        return "unknown"

    # Strip common prefixes
    message = raw_message
    if message.startswith("reverted: "):
        message = message[len("reverted: ") :]
    elif message.startswith("reverted:"):
        message = message[len("reverted:") :]

    try:
        decoded = base64.b64decode(message)
        if len(decoded) >= 4:
            selector = bytes(decoded[:4])
            error_name = get_error_name(selector)
            if error_name:
                return error_name
            return f"unknown (selector: 0x{selector.hex()})"
        return "unknown (no selector)"
    except Exception:
        return raw_message


ACCOUNTING_SERVICE_KEY = "accounting_service.key"


class RoflAppdClient:
    """Singleton class for interacting with ROFL app daemon.

    This class provides a singleton interface to the ROFL app daemon for key
    management and transaction signing operations. It uses the official
    oasis-rofl-client library with async support.
    """

    _instance = None

    def __new__(cls):
        """Create or return the singleton instance.

        Returns:
            RoflAppdClient: The singleton instance
        """
        if cls._instance is None:
            cls._instance = super(RoflAppdClient, cls).__new__(cls)
            cls._instance._client = AsyncRoflClient()
        return cls._instance

    async def get_keypair(self, key_id: str = ACCOUNTING_SERVICE_KEY):
        """Generate a secp256k1 keypair using ROFL's key management.

        Args:
            key_id (str): A unique identifier for the key

        Returns:
            tuple: A tuple containing:
                - private_key (str): The private key in hex format with '0x' prefix
                - public_address (str): The Ethereum address derived from the private key

        Raises:
            ValueError: If key generation fails
            Exception: If communication with ROFL daemon fails
        """
        try:
            logger.info(f"Generating keypair with ID: {key_id}")

            key = await self._client.generate_key(key_id)

            if not key:
                raise ValueError("Failed to generate key")

            private_key = "0x" + key

            account = Account.from_key(private_key)
            public_address = account.address

            logger.info(f"Generated keypair with public address: {public_address}")

            return private_key, public_address

        except Exception as e:
            logger.error(f"Error generating keypair: {e}")
            raise

    async def submit_tx(self, tx: TxParams, encrypt: bool = False) -> None:
        """Submit a transaction to the ROFL daemon for signing and relay.

        Args:
            tx: Transaction parameters (must include 'to', 'data', 'gas', 'value')
            encrypt: Whether to encrypt the transaction (default: False)

        Raises:
            ValueError: If required transaction fields are missing
            TransactionRevertedError: If the on-chain transaction reverted
        """
        if "to" not in tx or "data" not in tx:
            raise ValueError("Transaction must include 'to' and 'data' fields")

        logger.info("Submitting transaction via ROFL to %s", tx["to"])

        result = await self._client.sign_submit(tx, encrypt)
        logger.info("ROFL transaction result: %s", result)

        if "fail" in result:
            fail_info = result["fail"]
            code = fail_info.get("code")
            module = fail_info.get("module")
            raw_message = fail_info.get("message")
            error_name = _decode_revert_reason(raw_message)

            error_msg = f"Transaction reverted: {error_name}"
            if module:
                error_msg += f" (module: {module}, code: {code})"

            logger.error(
                "Transaction reverted: error=%s, module=%s, code=%s",
                error_name,
                module,
                code,
            )
            raise TransactionRevertedError(
                error_msg, code=code, module=module, error_name=error_name
            )

        if "ok" not in result:
            raise ValueError("Invalid ROFL response: missing 'ok' key")


async def get_keypair(key_id: str = ACCOUNTING_SERVICE_KEY):
    """Get a keypair using the RoflAppdClient.

    Args:
        key_id (str, optional): A unique identifier for the key. Defaults to ACCOUNTING_SERVICE_KEY.

    Returns:
        tuple: A tuple containing (private_key, public_address)

    Raises:
        Exception: If key generation fails
    """
    return await RoflAppdClient().get_keypair(key_id)
