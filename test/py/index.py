import os
import traceback
from web3 import Web3
from inclusion_proofs import get_tx_inclusion_proof


def main():
    rpc_url = os.getenv("RPC_URL", "https://eth1.lava.build")
    block_number = int(os.getenv("BLOCK_NUMBER", "23488800"))
    tx_index = int(os.getenv("TX_INDEX", "1"))

    w3 = Web3(Web3.HTTPProvider(rpc_url))

    try:
        result = get_tx_inclusion_proof(w3, block_number, tx_index)
        print("RLP Block Header:", result["rlpBlockHeader"])
        print("Proof:", result["proof"])
    except Exception as error:
        print("Error:", error)
        traceback.print_exc()
        exit(1)


if __name__ == "__main__":
    main()
