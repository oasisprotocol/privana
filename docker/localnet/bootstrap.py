"""Deploy and fund a disposable Privana stack on Sapphire localnet."""

import json
import os
import re
import subprocess
from pathlib import Path

from eth_account import Account
from oasis_rofl_client import RoflClient
from web3 import Web3

DATA = Path("/data")
ARTIFACTS = Path("/privana/solidity/artifacts/contracts")
# Public test mnemonic from solidity/hardhat.config.ts.
MNEMONIC = "chimney theory present latin find behave ankle clock shadow earn suit reflect"
# solidity/tasks/localnetToken.ts deploys the token from Hardhat test account 0 at nonce 0,
# not from MNEMONIC. Also listed in .env.localnet.
TOKEN = "0x5FbDB2315678afecb367f032d93F642f64180aa3"

Account.enable_unaudited_hdwallet_features()
admin, service, user = (
    Account.from_mnemonic(MNEMONIC, account_path=f"m/44'/60'/0'/0/{i}") for i in range(3)
)
# Runs in sapphire-localnet's network namespace.
w3 = Web3(Web3.HTTPProvider("http://localhost:8545"))
rofl = RoflClient(timeout=120)


def abi(path):
    return json.loads((ARTIFACTS / path).read_text())["abi"]


def send(tx):
    tx = {
        "from": admin.address,
        "nonce": w3.eth.get_transaction_count(admin.address),
        "chainId": w3.eth.chain_id,
        "gas": 100_000,
        "gasPrice": w3.eth.gas_price,
    } | tx
    tx_hash = w3.eth.send_raw_transaction(admin.sign_transaction(tx).raw_transaction)
    if w3.eth.wait_for_transaction_receipt(tx_hash).status != 1:
        raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")


def rofl_send(contract, name, *args):
    tx = {
        "to": contract.address,
        "data": contract.encode_abi(name, args=args),
        "gas": 500_000,
        "value": 0,
    }
    result = rofl.sign_submit(tx)
    if "ok" not in result:
        raise RuntimeError(f"{name} failed: {result}")


def main():
    app_id = rofl.get_app_id().strip()
    manifest = DATA / "deployment.json"
    if manifest.exists():
        deployment = json.loads(manifest.read_text())
        if deployment["app_id"] != app_id or not w3.eth.get_code(deployment["accounting"]):
            raise SystemExit(
                "Localnet was reset: run `docker compose -f compose.localnet.yaml down -v` first"
            )
        print("Using existing localnet deployment", flush=True)
        return

    deploy = subprocess.run(
        ["npx", "hardhat", "deploy", "--network", "sapphire-localnet", "--roflappid", app_id],
        cwd="/privana/solidity",
        env=os.environ | {"SECRET_KEY": Web3.to_hex(admin.key)},
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout
    print(deploy, flush=True)
    address = re.search(r"Accounting contract address: (0x[0-9a-fA-F]{40})", deploy)[1]
    accounting = w3.eth.contract(address, abi=abi("Accounting.sol/Accounting.json"))
    token = w3.eth.contract(TOKEN, abi=abi("test/LocalnetERC20.sol/LocalnetERC20.json"))

    # Gas for withdrawals and sweeps, and tokens backing the credits below.
    evm_address = accounting.functions.evmAddress().call()
    for to in (evm_address, accounting.functions.gasTankAddress().call()):
        send({"to": to, "value": 10 * 10**18})
    send({"to": TOKEN, "data": token.encode_abi("transfer", args=[evm_address, 200_000 * 10**18])})

    # 1 is TokenType.ERC20.
    info = (1, accounting.functions.encodeEVMErc20TokenData(w3.eth.chain_id, TOKEN).call())
    rofl_send(accounting, "setTokenInfo", info)
    token_id = accounting.functions.getTokenId(info).call()
    for account in (service, user):
        deposit_id = Web3.keccak(text=f"localnet:{account.address}")
        rofl_send(
            accounting, "creditDeposit", account.address, token_id, 100_000 * 10**18, deposit_id
        )

    (DATA / "localnet.env").write_text(
        f"ACCOUNTING_CONTRACT_ADDRESS={address}\nRATE_LIMIT_EXEMPT={service.address}\n"
    )
    deployment = {
        "app_id": app_id,
        "accounting": address,
        "token": TOKEN,
        "token_id": Web3.to_hex(token_id),
        "accounts": {"admin": admin.address, "service": service.address, "user": user.address},
    }
    manifest.write_text(json.dumps(deployment, indent=2) + "\n")
    print(manifest.read_text(), flush=True)


if __name__ == "__main__":
    main()
