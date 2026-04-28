"""ROFL client helpers for key management and transaction submission."""

import base64
import logging
from dataclasses import dataclass

import cbor2
from eth_account import Account
from oasis_rofl_client import AsyncRoflClient
from web3.types import TxParams

from src.abi.accounting import get_error_name

logger = logging.getLogger(__name__)


@dataclass
class RoflSubmissionResult:
    """Result from ROFL transaction submission."""

    submission_id: str
    ok_payload: bytes | None = None  # Raw ok payload from ROFL response (ABI-encoded return value)


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


# Dedicated key for authenticating signed view queries on Sapphire (onlyROFLQuery modifier).
# Published on-chain at startup via setRoflSignerAddress so msg.sender checks succeed.
ROFL_QUERY_SIGNER_KEY = "rofl_query_signer.key"


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

    async def get_keypair(self, key_id: str = ROFL_QUERY_SIGNER_KEY):
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

    async def submit_tx(self, tx: TxParams, encrypt: bool = False) -> RoflSubmissionResult:
        """Submit a transaction to the ROFL daemon for signing and relay.

        Args:
            tx: Transaction parameters (must include 'to', 'data', 'gas', 'value')
            encrypt: Whether to encrypt the transaction (default: False)

        Returns:
            RoflSubmissionResult: Submission ID and optional ok payload (ABI-encoded return value)

        Raises:
            ValueError: If required transaction fields are missing
            TransactionRevertedError: If the on-chain transaction reverted
            Exception: If submission fails
        """
        if "to" not in tx or "data" not in tx:
            raise ValueError("Transaction must include 'to' and 'data' fields")

        logger.info(
            "Submitting transaction via ROFL to %s with gas %s",
            tx["to"],
            tx.get("gas"),
        )

        try:
            # AsyncRoflClient.sign_submit returns decoded CBOR directly
            try:
                result = await self._client.sign_submit(tx, encrypt)
            except cbor2.CBORDecodeError as e:
                raise ValueError(f"ROFL appd returned invalid CBOR response: {e}") from e

            if "fail" in result:
                fail_info = result["fail"]
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

            if "ok" not in result:
                raise ValueError(
                    f"Invalid ROFL response: missing 'ok' key. Response keys: {list(result.keys())}"
                )

            ok_payload = result.get("ok")
            raw_payload = None
            if ok_payload is not None:
                if isinstance(ok_payload, bytes):
                    raw_payload = ok_payload
                elif isinstance(ok_payload, str):
                    raw_payload = bytes.fromhex(ok_payload.removeprefix("0x"))
                elif isinstance(ok_payload, list) and len(ok_payload) > 0:
                    raw_payload = bytes(ok_payload)

            submission_id = cbor2.dumps(result).hex()
            logger.info("ROFL submission id: %s", submission_id)
            return RoflSubmissionResult(submission_id=submission_id, ok_payload=raw_payload)

        except TransactionRevertedError:
            raise
        except Exception as e:
            logger.error("ROFL submission exception: %s", str(e), exc_info=True)
            raise
