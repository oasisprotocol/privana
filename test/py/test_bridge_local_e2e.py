"""Local two-chain ROSE bridge end-to-end test, driven from Python.

Exercises the real ``AccountingContractService`` against a real ``Accounting``
proxy on sapphire-localnet (real Sapphire precompiles + the bundled rofl-appd)
and real ``XRose`` + ``ROFLBridge`` on a local Base chain (anvil, chainId 84532).
Nothing is mocked: ``onlyROFL`` writes go through the localnet appd over TCP, and
the ``onlyROFLQuery`` signing reads go through the sapphirepy-wrapped confidential
reader — including ``generateBridgeBurnTransfer``, a signed query a pure
``@oasisprotocol/sapphire-paratime`` reader cannot produce (it only encrypts
eth_calls, it does not sign them). The resolved mint and the generated burn are
actually broadcast to anvil and their on-chain effects asserted.

The contracts are deployed + owner-wired by ``solidity/scripts/bridge-local-deploy.ts``
(which writes the address manifest this test reads); the ``onlyROFL`` wiring and
every bridge flow are driven here. Run via ``scripts/bridge-local-e2e.sh``.

Self-skips unless localnet (:8545), the appd (:8549), anvil Base (:8546), and the
deploy manifest are all present, so the normal ``make test`` suite stays green.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
import rlp
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from hexbytes import HexBytes
from web3 import AsyncWeb3, Web3
from web3.providers import AsyncHTTPProvider

SAPPHIRE_URL = os.getenv("SAPPHIRE_LOCALNET_URL", "http://127.0.0.1:8545")
APPD_URL = os.getenv("ROFL_APPD_URL", "http://127.0.0.1:8549")
BASE_URL = os.getenv("BASE_LOCAL_RPC_URL", "http://127.0.0.1:8546")
MANIFEST_PATH = os.getenv("BRIDGE_LOCAL_MANIFEST", "solidity/deployments/bridge-local.json")

BASE_CHAIN_ID = 84532
# anvil default mnemonic account[1] — a well-known dev key, not a credential.
ANVIL_DEPLOYER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
ANVIL_USER_BASE = "0x000000000000000000000000000000000000bEEF"  # Base mint recipient

# The appd must be reached over TCP (the unix socket can't cross the Docker VM
# boundary on macOS); RoflAppdClient honors ROFL_APPD_URL at construction.
os.environ.setdefault("ROFL_APPD_URL", APPD_URL)

BRIDGE_WITHDRAW_TYPES = {
    "BridgeWithdraw": [
        {"name": "userAddress", "type": "address"},
        {"name": "toAddress", "type": "address"},
        {"name": "destChainId", "type": "uint256"},
        {"name": "routeAddress", "type": "address"},
        {"name": "amount", "type": "uint256"},
        {"name": "maxGasCost", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
    ]
}


def _fresh_id(tag: str) -> bytes:
    """A unique 32-byte depositId per run.

    ``creditDeposit`` / ``reserveBridgeBurn`` are permanently idempotent on their
    id, so fixed ids would make a second run against the same (persisted) contract
    revert ``DepositAlreadyProcessed``. Salting keeps the e2e re-runnable without a
    redeploy.
    """
    return Web3.keccak(tag.encode() + os.urandom(16))


def _signed_tx_nonce(raw: bytes) -> int:
    """Decode the nonce baked into a Sapphire-signed destination tx.

    Authoritative for anvil_setNonce alignment — unlike reading the contract's
    shared nonce counter, it can't see a lagged value. Sapphire's EIP155Signer
    emits legacy txs; the typed-tx branch is defensive.
    """
    raw = bytes(raw)
    if raw and raw[0] >= 0xC0:  # legacy RLP list: [nonce, gasPrice, ...]
        return int.from_bytes(rlp.decode(raw)[0], "big")
    return int.from_bytes(rlp.decode(raw[1:])[1], "big")  # EIP-2718: [chainId, nonce, ...]


def _probe_skip_reason() -> str | None:
    """Return why the live e2e can't run, or None if all prerequisites are up."""
    if not Path(MANIFEST_PATH).is_file():
        return f"deploy manifest not found at {MANIFEST_PATH} (run the deploy script first)"
    try:
        resp = httpx.get(f"{APPD_URL}/rofl/v1/app/id", timeout=2.0)
        if not resp.text.strip().startswith("rofl1"):
            return f"rofl-appd at {APPD_URL} did not return an app id"
    except Exception:
        return f"rofl-appd not reachable at {APPD_URL}"
    for url, want, name in (
        (SAPPHIRE_URL, None, "sapphire-localnet"),
        (BASE_URL, BASE_CHAIN_ID, "Base anvil"),
    ):
        try:
            resp = httpx.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
                timeout=2.0,
            )
            chain_id = int(resp.json()["result"], 16)
        except Exception:
            return f"{name} not reachable at {url}"
        if want is not None and chain_id != want:
            return f"{name} at {url} has chainId {chain_id}, expected {want}"
    return None


_SKIP_REASON = _probe_skip_reason()
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        _SKIP_REASON is not None, reason=_SKIP_REASON or "bridge e2e infra not reachable"
    ),
]


def _is_transient_message(message: str) -> bool:
    message = message.lower()
    return any(
        token in message
        for token in (
            "root not found",
            "connection",
            "timeout",
            "timed out",
            "temporarily",
            "fetch",
            "reset",
            "remote end closed",
            "server disconnected",
            "502",
            "503",
        )
    )


async def _retry_transient(
    factory,
    *,
    attempts: int = 10,
    delay: float = 1.0,
    idempotent_errors: tuple[str, ...] = (),
    landed=None,
):
    """Retry transient appd/RPC transport errors without re-applying a landed write.

    Right after a fresh deploy (and intermittently after) the localnet appd
    returns HTTP 400 "root not found" until a state root is queryable — worth
    retrying. The hazard is an appd write that *mines* but whose response is then
    lost to a transient error: a blind retry would double-apply it. Two guards
    make retries safe for writes:

      * ``idempotent_errors`` — error names that, on a retry, mean the earlier
        attempt already landed (e.g. ``DepositAlreadyProcessed``) -> success.
      * ``landed`` — an async predicate checked before re-submitting, for writes
        whose retry would otherwise revert differently (e.g. a consumed nonce).

    A genuine on-chain revert (appd CBOR ``fail`` -> ``TransactionRevertedError``;
    signed read -> web3 ``ContractLogicError``) still propagates so it can't mask
    the replay or a NotAuthorizedROFL failure.
    """
    from src.clients.rofl import TransactionRevertedError

    async def _already_landed() -> bool:
        return landed is not None and await landed()

    last_err: Exception | None = None
    for _ in range(attempts):
        try:
            return await factory()
        except TransactionRevertedError as exc:
            if (exc.error_name or "") in idempotent_errors or await _already_landed():
                return None
            raise
        except httpx.HTTPError as exc:
            # Any appd HTTP error is transient *by construction* in this harness:
            # payloads are well-formed, and a genuine on-chain revert never arrives
            # as an HTTP status — it comes back as HTTP 200 with a CBOR `fail`
            # (-> TransactionRevertedError, handled above). So the only HTTP error
            # the appd produces here is the post-deploy "root not found" 4xx (and
            # transport hiccups). Retry without status inspection; if it is somehow
            # permanent it still surfaces after `attempts` rather than silently passing.
            if await _already_landed():
                return None
            last_err = exc
            await asyncio.sleep(delay)
        except Exception as exc:  # noqa: BLE001 - classify web3/aiohttp errors by message
            if not _is_transient_message(str(exc)):
                if await _already_landed():
                    return None
                raise
            last_err = exc
            await asyncio.sleep(delay)
    if await _already_landed():
        return None
    assert last_err is not None
    raise last_err


async def test_bridge_local_e2e() -> None:
    from src.abi.accounting import ACCOUNTING_ABI
    from src.abi.rofl_bridge import ROFL_BRIDGE_ABI
    from src.abi.xrose import XROSE_ABI
    from src.clients.rofl import ROFL_QUERY_SIGNER_KEY, RoflAppdClient, TransactionRevertedError
    from src.models.types import Settings
    from src.services.accounting_contract import AccountingContractService
    from src.services.rofl_signer_bootstrap import bootstrap_rofl_signer_address

    manifest = json.loads(Path(MANIFEST_PATH).read_text())
    proxy = Web3.to_checksum_address(manifest["accountingProxy"])
    custody = Web3.to_checksum_address(manifest["custodyEOA"])
    bridge_addr = Web3.to_checksum_address(manifest["roflBridge"])
    xrose_addr = Web3.to_checksum_address(manifest["xrose"])
    sapphire_chain_id = int(manifest["sapphireChainId"])
    rose_token_id = HexBytes(manifest["roseTokenId"])

    # Reach the appd over TCP: drop any cached socket-bound singleton first.
    RoflAppdClient._instance = None  # type: ignore[attr-defined]

    # The whole point of this test is that the signed queries (resolveBridgeWithdrawal,
    # generateBridgeBurnTransfer) go through the ROFL appd query-signer key.
    # _get_confidential_reader_contract prefers SAPPHIRE_VIEW_PRIVATE_KEY when set, so a
    # leaked dev-shell view key would silently bypass the appd path. Refuse to run in
    # that ambiguous state.
    assert not os.getenv("SAPPHIRE_VIEW_PRIVATE_KEY"), (
        "unset SAPPHIRE_VIEW_PRIVATE_KEY: this e2e must drive signed queries via the ROFL "
        "appd query-signer keypair, not a view key from the shell"
    )

    settings = Settings(
        accounting_contract_address=proxy,
        sapphire_chain_id=sapphire_chain_id,
        sapphire_rpc_url=SAPPHIRE_URL,
        accounting_gas_limit=3_000_000,
        chain_rpc_urls={sapphire_chain_id: SAPPHIRE_URL, BASE_CHAIN_ID: BASE_URL},
    )
    service = AccountingContractService(settings)

    # Plain (public-view) readers; the service owns the confidential/appd paths.
    sapphire = AsyncWeb3(AsyncHTTPProvider(SAPPHIRE_URL))
    accounting = sapphire.eth.contract(address=proxy, abi=ACCOUNTING_ABI)
    base = AsyncWeb3(AsyncHTTPProvider(BASE_URL))
    xrose = base.eth.contract(address=xrose_addr, abi=XROSE_ABI)
    bridge = base.eth.contract(address=bridge_addr, abi=ROFL_BRIDGE_ABI)

    async def ledger() -> int:
        return int(await accounting.functions.ledgerTotalOf(rose_token_id).call())

    async def poll_ledger(expected: int, label: str) -> None:
        last = -1
        for _ in range(30):
            last = await ledger()
            if last == expected:
                return
            await asyncio.sleep(0.5)
        assert last == expected, f"{label}: ledgerTotalOf={last}, expected {expected}"

    async def broadcast_aligned(signed_tx: bytes, nonce: int):
        # The custody EOA's anvil nonce drifts from the contract's shared
        # nonces[chainId] pool (every request/reserve allocates one, not all are
        # broadcast), so realign before sending or the tx sits behind a gap.
        await base.provider.make_request("anvil_setNonce", [custody, hex(nonce)])
        tx_hash = await base.eth.send_raw_transaction(signed_tx)
        return await base.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

    # ── Wire the onlyROFL side through the real appd ──
    await _retry_transient(lambda: service.set_rofl_bridge(BASE_CHAIN_ID, bridge_addr))
    await _retry_transient(lambda: bootstrap_rofl_signer_address(service))
    _, signer_address = await _retry_transient(
        lambda: RoflAppdClient().get_keypair(ROFL_QUERY_SIGNER_KEY)
    )

    want_bridge = bridge_addr.lower()
    want_signer = signer_address.lower()
    for _ in range(30):
        have_bridge = (await accounting.functions.roflBridgeAddress(BASE_CHAIN_ID).call()).lower()
        have_signer = (await accounting.functions.roflSignerAddress().call()).lower()
        if have_bridge == want_bridge and have_signer == want_signer:
            break
        await asyncio.sleep(0.5)
    assert (
        await accounting.functions.roflBridgeAddress(BASE_CHAIN_ID).call()
    ).lower() == want_bridge
    assert (await accounting.functions.roflSignerAddress().call()).lower() == want_signer

    domain_fields = await accounting.functions.eip712Domain().call()
    domain = {
        "name": domain_fields[1],
        "version": domain_fields[2],
        "chainId": int(domain_fields[3]),
        "verifyingContract": Web3.to_checksum_address(domain_fields[4]),
    }

    # ─────────────────────────────────────────────────────────────────────────
    # Bridge OUT: Sapphire ROSE → Base xROSE (mint broadcast on anvil)
    # ─────────────────────────────────────────────────────────────────────────
    user = Account.create()
    out_amount = Web3.to_wei(3, "ether")
    out_deposit_id = _fresh_id("e2e-out-seed")

    ledger_base = await ledger()
    await _retry_transient(
        lambda: service.credit_deposit(user.address, rose_token_id, out_amount, out_deposit_id),
        idempotent_errors=("DepositAlreadyProcessed",),
    )
    await poll_ledger(ledger_base + out_amount, "out: after credit")

    user_nonce = int(await accounting.functions.withdrawalNonces(user.address).call())
    message = {
        "userAddress": user.address,
        "toAddress": ANVIL_USER_BASE,
        "destChainId": BASE_CHAIN_ID,
        "routeAddress": bridge_addr,
        "amount": out_amount,
        "maxGasCost": 0,
        "nonce": user_nonce,
    }
    signable = encode_typed_data(
        domain_data=domain, message_types=BRIDGE_WITHDRAW_TYPES, message_data=message
    )
    signature = Web3.to_hex(Account.sign_message(signable, user.key).signature)

    index = int(await accounting.functions.withdrawalCount().call())

    async def _withdrawal_landed() -> bool:
        return int(await accounting.functions.withdrawalCount().call()) > index

    await _retry_transient(
        lambda: service.request_bridge_withdrawal(
            {
                "user_address": user.address,
                "to_address": ANVIL_USER_BASE,
                "dest_chain_id": BASE_CHAIN_ID,
                "route_address": bridge_addr,
                "amount": out_amount,
                "max_gas_cost": 0,
                "user_nonce": user_nonce,
                "signature": signature,
            }
        ),
        landed=_withdrawal_landed,
    )
    # requestBridgeWithdrawal debits the ROSE ledger back to the baseline.
    await poll_ledger(ledger_base, "out: after request")

    # Sign the Base mint via the confidential reader (resolveBridgeWithdrawal).
    signed_mint = await _retry_transient(lambda: service.resolve_bridge_withdrawal(index))

    expected_withdrawal_id = Web3.keccak(
        abi_encode(["address", "uint256", "uint256"], [proxy, sapphire_chain_id, index])
    )
    recipient_before = int(await xrose.functions.balanceOf(ANVIL_USER_BASE).call())
    supply_before = int(await xrose.functions.totalSupply().call())
    receipt = await broadcast_aligned(signed_mint, _signed_tx_nonce(signed_mint))
    assert receipt["status"] == 1, "out: mint broadcast did not succeed on Base"
    assert (
        int(await xrose.functions.balanceOf(ANVIL_USER_BASE).call())
        == recipient_before + out_amount
    )
    assert int(await xrose.functions.totalSupply().call()) == supply_before + out_amount
    assert await bridge.functions.mintedWithdrawalIds(expected_withdrawal_id).call() is True

    # ─────────────────────────────────────────────────────────────────────────
    # Bridge IN: Base xROSE burn → Sapphire ROSE credit.
    # Drives generateBridgeBurnTransfer — an onlyROFLQuery SIGNED query a
    # non-signing eth_call reader cannot produce — and broadcasts the
    # custody-signed burn to anvil.
    # ─────────────────────────────────────────────────────────────────────────
    in_amount = Web3.to_wei(2, "ether")
    in_deposit_id = _fresh_id("e2e-in-burn")

    # Seed the bridge's own xROSE balance (ROFLBridge.burn burns from itself).
    # The deploy granted the anvil deployer a mint limit for exactly this.
    deployer = Account.from_key(ANVIL_DEPLOYER_KEY)
    seed_nonce = await base.eth.get_transaction_count(deployer.address)
    seed_tx = await xrose.functions.mint(bridge_addr, in_amount).build_transaction(
        {
            "from": deployer.address,
            "nonce": seed_nonce,
            "gas": 200_000,
            "gasPrice": 0,
            "chainId": BASE_CHAIN_ID,
        }
    )
    seed_signed = deployer.sign_transaction(seed_tx)
    seed_hash = await base.eth.send_raw_transaction(seed_signed.raw_transaction)
    seed_receipt = await base.eth.wait_for_transaction_receipt(seed_hash, timeout=60)
    assert seed_receipt["status"] == 1, "in: seeding bridge xROSE failed"

    # Reserve the Base burn nonce (onlyROFL/appd) and read it back from canonical state.
    await _retry_transient(
        lambda: service.reserve_bridge_burn(in_deposit_id, BASE_CHAIN_ID, bridge_addr, in_amount)
    )
    burn_nonce = -1
    for _ in range(30):
        try:
            burn_nonce = await service.get_bridge_burn_nonce(in_deposit_id)
            break
        except ValueError:
            await asyncio.sleep(0.5)
    assert burn_nonce >= 0, "in: bridge burn reservation never materialized"

    (
        res_chain,
        res_bridge,
        res_amount,
        _res_nonce,
        res_exists,
    ) = await accounting.functions.getBridgeBurnRequest(in_deposit_id).call()
    assert res_exists is True
    assert int(res_chain) == BASE_CHAIN_ID
    assert Web3.to_checksum_address(res_bridge) == bridge_addr
    assert int(res_amount) == in_amount

    # The gap-closing step: sign ROFLBridge.burn via the onlyROFLQuery signed query.
    signed_burn = await _retry_transient(
        lambda: service.generate_bridge_burn_transfer(in_deposit_id)
    )

    bridge_bal_before = int(await xrose.functions.balanceOf(bridge_addr).call())
    supply_before = int(await xrose.functions.totalSupply().call())
    burn_receipt = await broadcast_aligned(signed_burn, _signed_tx_nonce(signed_burn))
    assert burn_receipt["status"] == 1, "in: burn broadcast did not succeed on Base"
    assert int(await xrose.functions.balanceOf(bridge_addr).call()) == bridge_bal_before - in_amount
    assert int(await xrose.functions.totalSupply().call()) == supply_before - in_amount
    assert await bridge.functions.burnedDepositIds(in_deposit_id).call() is True

    # Credit the inbound ROSE on Sapphire only after the burn confirmed.
    in_user = Account.create()
    in_credit_id = _fresh_id("e2e-in-credit")
    ledger_before = await ledger()
    await _retry_transient(
        lambda: service.credit_deposit(in_user.address, rose_token_id, in_amount, in_credit_id),
        idempotent_errors=("DepositAlreadyProcessed",),
    )
    await poll_ledger(ledger_before + in_amount, "in: after credit")

    # ─────────────────────────────────────────────────────────────────────────
    # Replay protection: a duplicate creditDeposit must revert (real appd CBOR
    # 'fail' → TransactionRevertedError) and must not move the ledger again.
    # ─────────────────────────────────────────────────────────────────────────
    replay_user = Account.create()
    replay_amount = Web3.to_wei(1, "ether")
    replay_deposit_id = _fresh_id("e2e-replay-dup")

    ledger_base = await ledger()
    await _retry_transient(
        lambda: service.credit_deposit(
            replay_user.address, rose_token_id, replay_amount, replay_deposit_id
        ),
        idempotent_errors=("DepositAlreadyProcessed",),
    )
    await poll_ledger(ledger_base + replay_amount, "replay: after first credit")

    # The duplicate must surface the real DepositAlreadyProcessed revert; transient
    # appd 400s are retried (no idempotent_errors here, so the revert propagates).
    with pytest.raises(TransactionRevertedError):
        await _retry_transient(
            lambda: service.credit_deposit(
                replay_user.address, rose_token_id, replay_amount, replay_deposit_id
            )
        )
    assert await ledger() == ledger_base + replay_amount, "replay: ledger moved on duplicate credit"

    # Close the AsyncWeb3 aiohttp sessions we opened (avoids "unclosed session"
    # noise); the service's own readers are closed too.
    for client in (sapphire, base, service.reader_w3, service._confidential_reader_w3):
        if client is not None:
            try:
                await client.provider.disconnect()
            except Exception:
                pass
