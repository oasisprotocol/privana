"""Blockchain service for interacting with Base mainnet."""

import logging
from typing import Optional
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

logger = logging.getLogger(__name__)


class Blockchain:
    """Service for blockchain interactions."""
    
    def __init__(self, rpc_url: str = "https://mainnet.base.org"):
        """
        Initialize blockchain service.
        
        Args:
            rpc_url: Base mainnet RPC URL
        """
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        
        if not self.w3.is_connected():
            raise ConnectionError(f"Failed to connect to RPC: {rpc_url}")
        
        logger.info(f"Connected to Base mainnet at {rpc_url}")
        logger.info(f"Chain ID: {self.w3.eth.chain_id}")
        logger.info(f"Latest block: {self.w3.eth.block_number}")
    
    def is_valid_address(self, address: str) -> bool:
        """
        Check if address is valid.
        
        Args:
            address: Ethereum address
            
        Returns:
            True if valid, False otherwise
        """
        return Web3.is_address(address)
    
    def get_checksum_address(self, address: str) -> Optional[str]:
        """
        Convert address to checksum format.
        
        Args:
            address: Ethereum address
            
        Returns:
            Checksum address or None if invalid
        """
        if not self.is_valid_address(address):
            return None
        return Web3.to_checksum_address(address)


blockchain: Optional[Blockchain] = None


def get_blockchain() -> Blockchain:
    """
    Get or create blockchain instance.
    
    Returns:
        Blockchain instance
    """
    global blockchain
    if blockchain is None:
        blockchain = Blockchain()
    return blockchain
