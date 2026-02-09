"""ROFL client helpers for key management and transaction submission."""

import base64
import logging

import cbor2
from eth_account import Account
from oasis_rofl_client import RoflClient
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
    oasis-rofl-client library.
    """

    _instance = None

    def __new__(cls):
        """Create or return the singleton instance.

        Returns:
            RoflAppdClient: The singleton instance
        """
        if cls._instance is None:
            cls._instance = super(RoflAppdClient, cls).__new__(cls)
            cls._instance._client = RoflClient()
        return cls._instance

    def get_keypair(self, key_id: str = ACCOUNTING_SERVICE_KEY):
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

            key = self._client.generate_key(key_id)

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

    def submit_tx(self, tx: TxParams, encrypt: bool = False) -> str:
        """Submit a transaction to the ROFL daemon for signing and relay.

        Args:
            tx: Transaction parameters (must include 'to', 'data', 'gas', 'value')
            encrypt: Whether to encrypt the transaction (default: False)

        Returns:
            str: The submission ID from ROFL response

        Raises:
            ValueError: If required transaction fields are missing
            TransactionRevertedError: If the on-chain transaction reverted
            Exception: If submission fails
        """
        from oasis_rofl_client.common import ENDPOINT_TX_SIGN_SUBMIT, get_tx_payload

        if "to" not in tx or "data" not in tx:
            raise ValueError("Transaction must include 'to' and 'data' fields")

        logger.info(
            "Submitting transaction via ROFL to %s with gas %s",
            tx["to"],
            tx.get("gas"),
        )

        try:
            payload = get_tx_payload(tx, encrypt)
            response = self._client._appd_request("POST", ENDPOINT_TX_SIGN_SUBMIT, payload)
            result = response.json()
            submission_id = result.get("data")

            if not submission_id:
                raise ValueError(f"Unexpected ROFL response payload: {result}")

            # Decode CBOR to check for transaction failure
            try:
                decoded = cbor2.loads(bytes.fromhex(submission_id))
                if isinstance(decoded, dict):
                    if "fail" in decoded:
                        fail_info = decoded["fail"]
                        code = fail_info.get("code")
                        module = fail_info.get("module")
                        raw_message = fail_info.get("message")

                        # Decode the revert reason from the message
                        error_name = _decode_revert_reason(raw_message)

                        error_msg = f"Transaction reverted: {error_name}"
                        if module:
                            error_msg += f" (module: {module}, code: {code})"

                        logger.error(
                            "Transaction reverted on-chain: error=%s, module=%s, code=%s, raw=%s",
                            error_name,
                            module,
                            code,
                            raw_message,
                        )
                        raise TransactionRevertedError(
                            error_msg, code=code, module=module, error_name=error_name
                        )
                    elif "ok" not in decoded:
                        # Unknown response format - could be an error in a different format
                        logger.warning(
                            "Unexpected CBOR response structure (keys: %s): %s",
                            list(decoded.keys()),
                            decoded,
                        )
            except cbor2.CBORDecodeError:
                # Not valid CBOR, treat as opaque submission ID
                pass

            logger.info("ROFL submission id: %s", submission_id)
            return submission_id

        except TransactionRevertedError:
            raise
        except Exception as e:
            logger.error("ROFL submission exception: %s", str(e), exc_info=True)
            raise


def get_keypair(key_id: str = ACCOUNTING_SERVICE_KEY):
    """Get a keypair using the RoflAppdClient.

    Args:
        key_id (str, optional): A unique identifier for the key. Defaults to ACCOUNTING_SERVICE_KEY.

    Returns:
        tuple: A tuple containing (private_key, public_address)

    Raises:
        Exception: If key generation fails
    """
    return RoflAppdClient().get_keypair(key_id)
