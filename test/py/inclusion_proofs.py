from eth_utils import keccak, to_bytes
from eth_typing import HexStr
from typing import Union
from web3 import Web3
import rlp
from trie import HexaryTrie


def get_mapping_storage_slot(mapping_key: Union[bytes, HexStr], mapping_slot: Union[bytes, HexStr]) -> str:
    mapping_key_bytes = to_bytes(hexstr=mapping_key) if isinstance(mapping_key, str) else mapping_key
    mapping_slot_bytes = to_bytes(hexstr=mapping_slot) if isinstance(mapping_slot, str) else mapping_slot

    padded_key = mapping_key_bytes.rjust(32, b'\x00')
    padded_slot = mapping_slot_bytes.rjust(32, b'\x00')

    return Web3.to_hex(keccak(padded_key + padded_slot))


def get_rpc_uint(number: int) -> str:
    return hex(number)


def get_rlp_uint(number: int) -> bytes:
    if number == 0:
        return b'\x80'

    hex_value = hex(number)[2:]
    if len(hex_value) % 2 != 0:
        hex_value = '0' + hex_value

    value_bytes = bytes.fromhex(hex_value)
    return rlp.encode(value_bytes)


def get_tx_inclusion_proof(provider: Web3, block_number: int, tx_index: int) -> dict:
    raw_block = provider.provider.make_request("debug_getRawBlock", [get_rpc_uint(block_number)])

    if 'error' in raw_block:
        raise Exception(f"RPC Error: {raw_block['error']}")

    raw_block_hex = raw_block['result']
    block_rlp = rlp.decode(bytes.fromhex(raw_block_hex[2:]))

    block_header = block_rlp[0]
    raw_transactions = block_rlp[1]

    trie = HexaryTrie(db={})

    for i, raw_transaction in enumerate(raw_transactions):
        key = get_rlp_uint(i)
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

    proof = trie.get_proof(get_rlp_uint(tx_index))
    proof_hex = []
    for node in proof:
        if isinstance(node, list):
            proof_hex.append(Web3.to_hex(rlp.encode(node)))
        else:
            proof_hex.append(Web3.to_hex(node))

    return {
        "rlpBlockHeader": Web3.to_hex(rlp.encode(block_header)),
        "proof": proof_hex
    }
