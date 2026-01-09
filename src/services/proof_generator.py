import logging
import time
from typing import Dict, Optional

import rlp
from eth_typing import HexStr
from trie import HexaryTrie
from web3 import Web3
from web3.types import TxReceipt

logger = logging.getLogger(__name__)


class ProofGeneratorService:

    def __init__(self, chain_rpc_urls: Dict[int, str]):
        self.chain_rpc_urls = chain_rpc_urls
        self._chain_web3: Dict[int, Web3] = {}

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
    def _get_rpc_uint(number: int) -> str:
        return hex(number)

    @staticmethod
    def _get_rlp_uint(number: int) -> bytes:
        if number == 0:
            return b'\x80'

        hex_value = hex(number)[2:]
        if len(hex_value) % 2 != 0:
            hex_value = '0' + hex_value

        value_bytes = bytes.fromhex(hex_value)
        return rlp.encode(value_bytes)

    def generate_tx_proof(
        self,
        chain_id: int,
        tx_hash: HexStr,
    ) -> Dict[str, str]:
        web3 = self._get_chain_web3(chain_id)

        tx_receipt: TxReceipt = web3.eth.get_transaction_receipt(tx_hash)
        if not tx_receipt:
            raise ValueError(f"Transaction {tx_hash} not found on chain {chain_id}")

        if tx_receipt.get("status") != 1:
            raise ValueError(f"Transaction {tx_hash} failed on chain {chain_id}")

        block_number = tx_receipt["blockNumber"]
        tx_index = tx_receipt["transactionIndex"]

        return self._generate_tx_inclusion_proof(web3, block_number, tx_index)

    def _generate_tx_inclusion_proof(
        self,
        web3: Web3,
        block_number: int,
        tx_index: int,
        max_retries: int = 3,
    ) -> Dict[str, str]:
        last_error = None

        for attempt in range(max_retries):
            try:
                raw_block = web3.provider.make_request(
                    "debug_getRawBlock",
                    [self._get_rpc_uint(block_number)]
                )

                if 'error' in raw_block:
                    error_msg = raw_block['error']
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get('message', str(error_msg))

                    if 'method not found' in str(error_msg).lower() or 'not supported' in str(error_msg).lower():
                        raise ValueError(
                            f"RPC endpoint does not support debug_getRawBlock API. "
                            f"Archive node with debug API is required. "
                            f"Error: {error_msg}"
                        )

                    last_error = Exception(f"RPC Error calling debug_getRawBlock: {error_msg}")

                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        logger.warning(
                            f"debug_getRawBlock failed (attempt {attempt + 1}/{max_retries}), "
                            f"retrying in {wait_time}s: {error_msg}"
                        )
                        time.sleep(wait_time)
                        continue

                    raise last_error

                break

            except ValueError:
                raise
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(
                        f"Proof generation failed (attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {wait_time}s: {str(e)}"
                    )
                    time.sleep(wait_time)
                    continue
                raise

        raw_block_hex = raw_block['result']
        block_rlp = rlp.decode(bytes.fromhex(raw_block_hex[2:]))

        block_header = block_rlp[0]
        raw_transactions = block_rlp[1]

        trie = HexaryTrie(db={})

        for i, raw_transaction in enumerate(raw_transactions):
            key = self._get_rlp_uint(i)
            if isinstance(raw_transaction, list):
                value = rlp.encode(raw_transaction)
            else:
                value = raw_transaction
            trie.set(key, value)

        tx_root = Web3.to_hex(trie.root_hash)
        block_header_tx_root = Web3.to_hex(block_header[4])

        if tx_root != block_header_tx_root:
            raise Exception(
                "Constructed transaction Merkle tree has a root inconsistent with the transactionsRoot in the block header"
            )

        if tx_index >= len(raw_transactions):
            raise Exception("Transaction index is outside the range of this block")

        proof = trie.get_proof(self._get_rlp_uint(tx_index))
        proof_hex = []
        for node in proof:
            if isinstance(node, list):
                proof_hex.append(Web3.to_hex(rlp.encode(node)))
            else:
                proof_hex.append(Web3.to_hex(node))

        transaction_index_rlp = Web3.to_hex(self._get_rlp_uint(tx_index))
        rlp_block_header = Web3.to_hex(rlp.encode(block_header))
        transaction_proof_stack = Web3.to_hex(rlp.encode([bytes.fromhex(p[2:]) for p in proof_hex]))

        return {
            "rlp_block_header": rlp_block_header,
            "transaction_index_rlp": transaction_index_rlp,
            "transaction_proof_stack": transaction_proof_stack,
        }

    @staticmethod
    def _parse_hex_int(value, default=0) -> int:
        if value is None:
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            return int(value, 16) if value.startswith("0x") else int(value)
        return default

    @staticmethod
    def _parse_hex_bytes(value, default=b"") -> bytes:
        if value is None:
            return default
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            hex_str = value[2:] if value.startswith("0x") else value
            return bytes.fromhex(hex_str) if hex_str else default
        return default

    @classmethod
    def _encode_receipt(cls, receipt: dict) -> bytes:
        status = cls._parse_hex_int(receipt.get("status"), 0)
        cumulative_gas = cls._parse_hex_int(receipt.get("cumulativeGasUsed"), 0)
        logs_bloom = cls._parse_hex_bytes(receipt.get("logsBloom"), b"\x00" * 256)

        logs_array = []
        for log in receipt.get("logs", []):
            address = cls._parse_hex_bytes(log.get("address"))
            topics = [cls._parse_hex_bytes(t) for t in log.get("topics", [])]
            data = cls._parse_hex_bytes(log.get("data"))
            logs_array.append([address, topics, data])

        receipt_array = [status, cumulative_gas, logs_bloom, logs_array]

        tx_type = cls._parse_hex_int(receipt.get("type"), 0)

        if tx_type == 0x7e:
            deposit_nonce = receipt.get("depositNonce")
            if deposit_nonce is not None:
                receipt_array.append(cls._parse_hex_int(deposit_nonce))

            deposit_version = receipt.get("depositReceiptVersion")
            if deposit_version is not None:
                receipt_array.append(cls._parse_hex_int(deposit_version))

        encoded = rlp.encode(receipt_array)

        if tx_type != 0:
            encoded = bytes([tx_type]) + encoded

        return encoded

    def _generate_receipt_inclusion_proof(
        self,
        web3: Web3,
        block_number: int,
        tx_index: int,
        max_retries: int = 3,
    ) -> Dict[str, str]:
        last_error = None

        for attempt in range(max_retries):
            try:
                receipts_result = web3.provider.make_request(
                    "eth_getBlockReceipts",
                    [self._get_rpc_uint(block_number)]
                )

                if 'error' in receipts_result:
                    error_msg = receipts_result['error']
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get('message', str(error_msg))

                    last_error = Exception(f"RPC Error calling eth_getBlockReceipts: {error_msg}")

                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        logger.warning(
                            f"eth_getBlockReceipts failed (attempt {attempt + 1}/{max_retries}), "
                            f"retrying in {wait_time}s: {error_msg}"
                        )
                        time.sleep(wait_time)
                        continue

                    raise last_error

                raw_block = web3.provider.make_request(
                    "debug_getRawBlock",
                    [self._get_rpc_uint(block_number)]
                )

                if 'error' in raw_block:
                    error_msg = raw_block['error']
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get('message', str(error_msg))

                    last_error = Exception(f"RPC Error calling debug_getRawBlock: {error_msg}")

                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        logger.warning(
                            f"debug_getRawBlock failed (attempt {attempt + 1}/{max_retries}), "
                            f"retrying in {wait_time}s: {error_msg}"
                        )
                        time.sleep(wait_time)
                        continue

                    raise last_error

                break

            except ValueError:
                raise
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(
                        f"Receipt proof generation failed (attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {wait_time}s: {str(e)}"
                    )
                    time.sleep(wait_time)
                    continue
                raise

        receipts = receipts_result['result']
        raw_block_hex = raw_block['result']
        block_rlp = rlp.decode(bytes.fromhex(raw_block_hex[2:]))
        block_header = block_rlp[0]

        trie = HexaryTrie(db={})
        for i, receipt in enumerate(receipts):
            key = self._get_rlp_uint(i)
            value = self._encode_receipt(receipt)
            trie.set(key, value)

        receipt_root = Web3.to_hex(trie.root_hash)
        block_header_receipt_root = Web3.to_hex(block_header[5])

        if receipt_root != block_header_receipt_root:
            raise Exception(
                f"Constructed receipts Merkle tree has a root inconsistent with the receiptsRoot in the block header. "
                f"Computed: {receipt_root}, Expected: {block_header_receipt_root}"
            )

        if tx_index >= len(receipts):
            raise Exception("Transaction index is outside the range of this block")

        proof = trie.get_proof(self._get_rlp_uint(tx_index))
        proof_hex = []
        for node in proof:
            if isinstance(node, list):
                proof_hex.append(Web3.to_hex(rlp.encode(node)))
            else:
                proof_hex.append(Web3.to_hex(node))

        receipt_index_rlp = Web3.to_hex(self._get_rlp_uint(tx_index))
        receipt_proof_stack = Web3.to_hex(rlp.encode([bytes.fromhex(p[2:]) for p in proof_hex]))

        return {
            "receipt_index_rlp": receipt_index_rlp,
            "receipt_proof_stack": receipt_proof_stack,
        }

    def generate_deposit_proofs(
        self,
        chain_id: int,
        tx_hash: HexStr,
    ) -> Dict[str, str]:
        web3 = self._get_chain_web3(chain_id)

        tx_receipt: TxReceipt = web3.eth.get_transaction_receipt(tx_hash)
        if not tx_receipt:
            raise ValueError(f"Transaction {tx_hash} not found on chain {chain_id}")

        if tx_receipt.get("status") != 1:
            raise ValueError(f"Transaction {tx_hash} failed on chain {chain_id}")

        block_number = tx_receipt["blockNumber"]
        tx_index = tx_receipt["transactionIndex"]

        tx_proof = self._generate_tx_inclusion_proof(web3, block_number, tx_index)
        receipt_proof = self._generate_receipt_inclusion_proof(web3, block_number, tx_index)

        return {
            "rlp_block_header": tx_proof["rlp_block_header"],
            "transaction_index_rlp": tx_proof["transaction_index_rlp"],
            "transaction_proof_stack": tx_proof["transaction_proof_stack"],
            "receipt_index_rlp": receipt_proof["receipt_index_rlp"],
            "receipt_proof_stack": receipt_proof["receipt_proof_stack"],
        }


_proof_generator_instance: Optional[ProofGeneratorService] = None


def get_proof_generator(chain_rpc_urls: Optional[Dict[int, str]] = None) -> ProofGeneratorService:
    global _proof_generator_instance

    if _proof_generator_instance is None:
        if chain_rpc_urls is None:
            from src.config import load_settings
            settings = load_settings()
            chain_rpc_urls = dict(settings.chain_rpc_urls)

        _proof_generator_instance = ProofGeneratorService(chain_rpc_urls)

    return _proof_generator_instance
