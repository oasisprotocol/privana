"""ROFL client helpers for key management and transaction submission."""

import logging
from typing import Any, Dict

import httpx
from eth_account import Account
from web3.types import TxParams

logger = logging.getLogger(__name__)

ACCOUNTING_SERVICE_KEY = "accounting_service.key"

class RoflAppdClient:
    """Singleton class for interacting with ROFL app daemon.
    
    This class provides a singleton interface to the ROFL app daemon for key
    management and transaction signing operations. It uses a Unix domain socket
    for communication with the daemon.
    
    Attributes:
        _client (httpx.Client): HTTP client for communicating with ROFL daemon
        ROFL_SOCKET_PATH (str): Path to the ROFL daemon Unix domain socket
    """
    
    _instance = None
    ROFL_SOCKET_PATH = "/run/rofl-appd.sock"
    
    def __new__(cls):
        """Create or return the singleton instance.
        
        Returns:
            RoflAppdClient: The singleton instance
        """
        if cls._instance is None:
            cls._instance = super(RoflAppdClient, cls).__new__(cls)
            cls._instance._client = cls._create_client()
        return cls._instance
    
    @staticmethod
    def _create_client():
        """Create an HTTP client for the ROFL socket.
        
        Returns:
            httpx.Client: HTTP client configured to use the ROFL Unix domain socket
        """
        transport = httpx.HTTPTransport(uds=RoflAppdClient.ROFL_SOCKET_PATH)
        return httpx.Client(transport=transport)
    
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
            
            payload = {"key_id": key_id, "kind": "secp256k1"}
            response = self._client.post(
                "http://localhost/rofl/v1/keys/generate", 
                json=payload
            )
            response.raise_for_status()
            
            result = response.json()
            key = result.get("key")
            
            if not key:
                raise ValueError(f"Failed to generate key: {result}")
                
            private_key = "0x" + key
            
            account = Account.from_key(private_key)
            public_address = account.address
            
            logger.info(f"Generated keypair with public address: {public_address}")
            
            return private_key, public_address
            
        except Exception as e:
            logger.error(f"Error generating keypair: {e}")
            raise

    def submit_tx(self, tx: TxParams, encrypt: bool = False) -> str:
        """Submit a transaction to the ROFL daemon for signing and relay."""

        if "to" not in tx or "data" not in tx:
            raise ValueError("Transaction must include 'to' and 'data' fields")

        def _as_int(value: Any, default: int = 0) -> int:
            if value is None:
                return default
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                try:
                    return int(value, 0)
                except ValueError as exc:  # pragma: no cover - defensive branch
                    raise ValueError(f"Expected numeric value, received {value}") from exc
            if isinstance(value, bytes):
                return int.from_bytes(value, byteorder="big")
            return int(value)

        def _strip_0x(value: Any) -> str:
            if isinstance(value, bytes):
                value = value.hex()
            value_str = str(value)
            return value_str[2:] if value_str.startswith("0x") else value_str

        gas_limit = tx.get("gas") or tx.get("gasLimit") or tx.get("gas_limit")

        payload: Dict[str, Any] = {
            "tx": {
                "kind": "eth",
                "data": {
                    "gas_limit": _as_int(gas_limit),
                    "to": _strip_0x(tx["to"]),
                    "value": _as_int(tx.get("value")),
                    "data": _strip_0x(tx["data"]),
                },
            },
            "encrypt": encrypt,
        }

        logger.info(
            "Submitting transaction via ROFL to %s with gas %s",
            tx["to"],
            tx.get("gas"),
        )

        try:
            response = self._client.post(
                "http://localhost/rofl/v1/tx/sign-submit", json=payload, timeout=30.0
            )

            logger.info("ROFL response status: %s", response.status_code)

            if response.status_code != 200:
                logger.error(
                    "ROFL transaction submission failed: status=%s, response=%s",
                    response.status_code,
                    response.text
                )

            response.raise_for_status()

            result = response.json()
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
