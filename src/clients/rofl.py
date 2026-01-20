"""ROFL client helpers for key management and transaction submission."""

import logging

from eth_account import Account
from oasis_rofl_client import RoflClient
from web3.types import TxParams

logger = logging.getLogger(__name__)

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
            # Use hex-encoded CBOR data as submission_id for backwards compatibility.
            # ROFL sign_submit returns {"ok": b''} when decoded - no real tx hash.
            submission_id = result.get("data")

            if not submission_id:
                raise ValueError(f"Unexpected ROFL response payload: {result}")

            logger.info("ROFL submission id: %s", submission_id)
            return submission_id

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
