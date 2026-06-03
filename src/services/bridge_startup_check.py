"""Startup-time bridge invariant checks.

Runs once from the FastAPI lifespan, after pinned-config validation
and before any processor starts. Each check raises
``BridgeStartupCheckError`` with a precise, actionable message; the
first failure aborts startup.

Pure config (env/settings shape and pinned locks) is delegated to
``src.config.bridge_validation.validate_bridge_settings``. On-chain
invariants (Sapphire reads + Base code/owner/limits/signer parity)
live here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from web3 import Web3
from web3.constants import ADDRESS_ZERO

from src.abi.rofl_bridge import ROFL_BRIDGE_ABI
from src.abi.xrose import XROSE_ABI
from src.config.bridge_validation import (
    destination_chain_ids,
    validate_bridge_settings,
)
from src.config.tokens import get_rose_token_id
from src.models.types import Settings

if TYPE_CHECKING:
    from src.services.accounting_contract import AccountingContractService

logger = logging.getLogger(__name__)


class BridgeStartupCheckError(RuntimeError):
    """A bridge startup invariant failed; do not start processors."""


def _checksum(addr: str) -> ChecksumAddress:
    return Web3.to_checksum_address(addr)


def _is_empty_code(code: bytes | HexBytes) -> bool:
    """Treat both ``b""`` and ``HexBytes('0x')`` as 'no code'.

    Different async web3 drivers disagree on which empty form they
    return for an address with no deployed bytecode; accept both.
    """
    return code in (b"", HexBytes("0x"))


async def verify_bridge_runtime(
    service: "AccountingContractService",
    settings: Settings,
) -> None:
    """Refuse to start processors if any bridge invariant is broken.

    Order: pinned-config first (cheap, no I/O), then Sapphire reads
    against the Accounting proxy, then Base reads against xROSE and
    ROFLBridge. Each check raises ``BridgeStartupCheckError`` with a
    unique message identifying which invariant failed and what was
    observed.
    """
    validate_bridge_settings(settings)

    reader = service._get_reader_contract()

    proxy = _checksum(settings.accounting_contract_address)
    if _checksum(reader.address) != proxy:
        raise BridgeStartupCheckError(
            f"Accounting reader bound to {reader.address} but "
            f"settings.accounting_contract_address is {proxy}; bridge "
            "selectors must be encoded against the proxy."
        )

    # Defensive: if a future change drops the BridgeModule artifact
    # from the merged ABI, every bridge call would silently route to
    # the proxy fallback and fail at runtime instead of at startup.
    fn_names = {
        name
        for item in reader.abi
        if item.get("type") == "function" and (name := item.get("name")) is not None
    }
    for required in (
        "bridgeModule",
        "roflBridgeAddress",
        "requestBridgeWithdrawal",
        "ROSE_TOKEN_ID",
    ):
        if required not in fn_names:
            raise BridgeStartupCheckError(
                f"Accounting ABI is missing bridge selector {required!r}; "
                "BridgeModule artifact may be absent from the merged ABI."
            )

    token_id = await get_rose_token_id(service)
    if len(token_id) != 32 or token_id == b"\x00" * 32:
        raise BridgeStartupCheckError(
            f"Accounting.ROSE_TOKEN_ID returned {HexBytes(token_id).to_0x_hex()}; "
            "bridge token is not registered on the proxy."
        )

    module_address = await reader.functions.bridgeModule().call()
    if not isinstance(module_address, str) or module_address.lower() == ADDRESS_ZERO.lower():
        raise BridgeStartupCheckError(
            f"Accounting.bridgeModule() returned {module_address!r}; "
            "BridgeModule was never set on the proxy."
        )
    module_checksum = _checksum(module_address)
    if module_checksum == proxy:
        raise BridgeStartupCheckError(
            f"Accounting.bridgeModule() returned the proxy address {proxy}; "
            "the BridgeModule implementation must be a distinct contract."
        )
    if service.reader_w3 is None:
        raise BridgeStartupCheckError(
            "Sapphire reader is not initialised; SAPPHIRE_RPC_URL must be "
            "set for bridge startup checks."
        )
    module_code = await service.reader_w3.eth.get_code(module_checksum)
    if _is_empty_code(module_code):
        raise BridgeStartupCheckError(
            f"Accounting.bridgeModule()={module_checksum} has no code on "
            "Sapphire; the BridgeModule implementation is not deployed."
        )

    expected_rofl_bridge = _checksum(settings.rofl_bridge_address)
    expected_xrose = _checksum(settings.xrose_address)

    custody_evm = await reader.functions.evmAddress().call()
    custody_checksum = _checksum(custody_evm)

    for chain_id in sorted(destination_chain_ids(settings)):
        on_chain_rofl_bridge = await reader.functions.roflBridgeAddress(chain_id).call()
        # Startup does not raise on a route mismatch: route rotation is owned
        # by the in-TEE reconciler, which fails closed while inbound xROSE is
        # in flight. A strict raise here would block restarts during a rotation.
        if on_chain_rofl_bridge.lower() == ADDRESS_ZERO.lower():
            logger.info(
                "Accounting.roflBridgeAddress(%d)=0x0; bridge route reconciler "
                "will bootstrap to %s on first tick.",
                chain_id,
                expected_rofl_bridge,
            )
        elif _checksum(on_chain_rofl_bridge) != expected_rofl_bridge:
            logger.warning(
                "Accounting.roflBridgeAddress(%d)=%s but ROFL_BRIDGE_ADDRESS=%s; "
                "bridge route reconciler will gate rotation until in-flight inbound drains.",
                chain_id,
                on_chain_rofl_bridge,
                expected_rofl_bridge,
            )

        dest_w3 = await service._get_chain_web3(chain_id)

        xrose_code = await dest_w3.eth.get_code(expected_xrose)
        if _is_empty_code(xrose_code):
            raise BridgeStartupCheckError(
                f"XROSE_ADDRESS={expected_xrose} has no code on chain "
                f"{chain_id}; xROSE is not deployed."
            )

        bridge_code = await dest_w3.eth.get_code(expected_rofl_bridge)
        if _is_empty_code(bridge_code):
            raise BridgeStartupCheckError(
                f"ROFL_BRIDGE_ADDRESS={expected_rofl_bridge} has no code "
                f"on chain {chain_id}; ROFLBridge is not deployed."
            )

        # Bind handles after the code checks so a missing deployment surfaces
        # as "no code" rather than as a confusing ABI decode error.
        xrose = dest_w3.eth.contract(address=expected_xrose, abi=XROSE_ABI)
        rofl_bridge = dest_w3.eth.contract(address=expected_rofl_bridge, abi=ROFL_BRIDGE_ABI)

        on_chain_signer = await rofl_bridge.functions.roflSigner().call()
        if _checksum(on_chain_signer) != custody_checksum:
            raise BridgeStartupCheckError(
                f"ROFLBridge.roflSigner()={on_chain_signer} on chain {chain_id} "
                f"but Accounting.evmAddress()={custody_checksum} on Sapphire; "
                "the bridge cannot authenticate custody operations."
            )

        minting_limit = await xrose.functions.mintingMaxLimitOf(expected_rofl_bridge).call()
        if int(minting_limit) != settings.bridge_mint_limit_wei:
            raise BridgeStartupCheckError(
                f"XRose.mintingMaxLimitOf({expected_rofl_bridge})={minting_limit} "
                f"on chain {chain_id} but BRIDGE_MINT_LIMIT_WEI={settings.bridge_mint_limit_wei}."
            )

        burning_limit = await xrose.functions.burningMaxLimitOf(expected_rofl_bridge).call()
        if int(burning_limit) != settings.bridge_burn_limit_wei:
            raise BridgeStartupCheckError(
                f"XRose.burningMaxLimitOf({expected_rofl_bridge})={burning_limit} "
                f"on chain {chain_id} but BRIDGE_BURN_LIMIT_WEI={settings.bridge_burn_limit_wei}."
            )

    logger.info(
        "Bridge runtime checks OK: rose_token_id=%s, bridgeModule=%s, "
        "roflBridge=%s, xrose=%s, custody=%s",
        HexBytes(token_id).to_0x_hex(),
        module_checksum,
        expected_rofl_bridge,
        expected_xrose,
        custody_checksum,
    )
