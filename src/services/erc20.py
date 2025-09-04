"""ERC20 token interaction service."""

import logging
from typing import Optional
from web3 import Web3
from web3.contract import Contract
from src.abi.erc20 import ERC20_ABI

logger = logging.getLogger(__name__)


class ERC20Token:
    """ERC20 token contract interaction."""
    
    def __init__(self, w3: Web3, contract_address: str):
        """
        Initialize ERC20 token contract.
        
        Args:
            w3: Web3 instance
            contract_address: Token contract address
        """
        self.w3 = w3
        self.address = Web3.to_checksum_address(contract_address)
        self.contract: Contract = self.w3.eth.contract(
            address=self.address,
            abi=ERC20_ABI
        )
        self._decimals: Optional[int] = None
    
    @property
    def decimals(self) -> int:
        """Get token decimals (cached)."""
        if self._decimals is None:
            self._decimals = self.contract.functions.decimals().call()
        return self._decimals
    
    def get_balance(self, address: str) -> str:
        """
        Get formatted token balance.
        
        Args:
            address: User wallet address
            
        Returns:
            Balance as string with proper decimal formatting
        """
        checksum_address = Web3.to_checksum_address(address)
        raw_balance = self.contract.functions.balanceOf(checksum_address).call()
        decimals = self.decimals
        
        if raw_balance == 0:
            return "0"
        
        balance_float = raw_balance / (10 ** decimals)
        
        if balance_float == int(balance_float):
            return str(int(balance_float))
        else:
            formatted = f"{balance_float:.{decimals}f}".rstrip('0').rstrip('.')
            return formatted
