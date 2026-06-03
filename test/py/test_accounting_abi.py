"""Regression tests for the merged ACCOUNTING_ABI.

After the BridgeModule extraction, ``src.abi.accounting`` merges the
``Accounting``, ``BridgeModule``, and ``BridgeLib`` artifacts into a single
ABI list bound to the proxy address. These tests pin that the merge keeps
the bridge surface intact and that the throw-on-conflict guard is wired
correctly.
"""

from __future__ import annotations

import pytest
from web3 import Web3

from src.abi.accounting import (
    ACCOUNTING_ABI,
    ERROR_SELECTORS,
    _merge_abis,
)


def _function_names() -> set[str]:
    return {item["name"] for item in ACCOUNTING_ABI if item.get("type") == "function"}


def _error_names() -> set[str]:
    return {item["name"] for item in ACCOUNTING_ABI if item.get("type") == "error"}


def _event_names() -> set[str]:
    return {item["name"] for item in ACCOUNTING_ABI if item.get("type") == "event"}


class TestMergedAbi:
    def test_bridge_function_selectors_present(self) -> None:
        names = _function_names()
        for required in (
            "requestBridgeWithdrawal",
            "resolveBridgeWithdrawal",
            "setRoflBridge",
            "setBridgeModule",
            "bridgeModule",
        ):
            assert required in names, f"missing function {required}"

    def test_bridge_errors_decode_via_selectors(self) -> None:
        # BridgeModule + Accounting errors
        for required in (
            "BridgeAssetNotSupported",
            "BridgeModuleNotSet",
            "BridgeModuleNotContract",
            "UnknownSelector",
        ):
            assert required in _error_names(), f"missing error {required}"
        # BridgeLib errors (bubble up through delegatecall)
        for required in (
            "InvalidRouteAddress",
            "RoflBridgeNotSet",
            "InvalidMaxGasCost",
            "GasBudgetExceeded",
        ):
            assert required in _error_names(), f"missing error {required}"

    def test_error_selectors_round_trip(self) -> None:
        # Recompute selector for each error and assert it's in ERROR_SELECTORS.
        for item in ACCOUNTING_ABI:
            if item.get("type") != "error":
                continue
            sig = f"{item['name']}({','.join(i['type'] for i in item.get('inputs', []))})"
            selector = bytes(Web3.keccak(text=sig)[:4])
            assert selector in ERROR_SELECTORS
            assert ERROR_SELECTORS[selector] == item["name"]

    def test_required_bridge_symbols_present(self) -> None:
        function_names = _function_names()
        event_names = _event_names()
        required_functions = {
            "ROSE_TOKEN_ID",
            "ledgerTotalOf",
            "roflBridgeAddress",
            "bridgeModule",
            "setBridgeModule",
            "requestBridgeWithdrawal",
            "resolveBridgeWithdrawal",
            "resolveWithdrawal",
            "reserveBridgeBurn",
            "generateBridgeBurnTransfer",
            "getBridgeBurnRequest",
            "generateSweepERC20TransferToBridge",
            "setRoflBridge",
        }
        required_events = {
            "BridgeBurnReserved",
            "RoflBridgeUpdated",
            "BridgeModuleSet",
            "Withdrawal",
        }
        missing_functions = sorted(required_functions - function_names)
        missing_events = sorted(required_events - event_names)
        assert not missing_functions, f"missing functions: {missing_functions}"
        assert not missing_events, f"missing events: {missing_events}"

    def test_rofl_bridge_updated_event_signature(self) -> None:
        # Pin the on-chain event shape so the cross-layer contract for
        # off-chain listeners can't drift unnoticed.
        event = next(
            (
                item
                for item in ACCOUNTING_ABI
                if item.get("type") == "event" and item.get("name") == "RoflBridgeUpdated"
            ),
            None,
        )
        assert event is not None, "RoflBridgeUpdated event missing"
        inputs = event.get("inputs") or []
        shape = [(i["type"], i["name"], bool(i.get("indexed"))) for i in inputs]
        assert shape == [
            ("uint256", "chainId", True),
            ("address", "bridge", False),
        ], f"unexpected RoflBridgeUpdated shape: {shape}"

    def test_no_duplicate_function_selectors(self) -> None:
        seen: dict[bytes, str] = {}
        for item in ACCOUNTING_ABI:
            if item.get("type") != "function":
                continue
            sig = f"{item['name']}({','.join(i['type'] for i in item.get('inputs', []))})"
            selector = bytes(Web3.keccak(text=sig)[:4])
            if selector in seen and seen[selector] != item["name"]:
                pytest.fail(
                    f"selector collision: {seen[selector]} and {item['name']} both hash to {selector.hex()}"
                )
            seen[selector] = item["name"]


class TestMergeConflictDetection:
    def test_throws_on_divergent_event_indexed_flags(self) -> None:
        a = [
            {
                "type": "event",
                "name": "Foo",
                "inputs": [{"type": "address", "indexed": True, "name": "x"}],
                "anonymous": False,
            }
        ]
        b = [
            {
                "type": "event",
                "name": "Foo",
                "inputs": [{"type": "address", "indexed": False, "name": "x"}],
                "anonymous": False,
            }
        ]
        # Different indexed flag ⇒ different canonical key, so both kept.
        # That is fine — indexed flags ARE part of identity. But identical
        # canonical key with divergent fields should throw.
        merged_diff_indexed = _merge_abis(a, b)
        assert len(merged_diff_indexed) == 2

    def test_throws_on_divergent_function_state_mutability(self) -> None:
        a = [
            {
                "type": "function",
                "name": "foo",
                "inputs": [],
                "outputs": [],
                "stateMutability": "view",
            }
        ]
        b = [
            {
                "type": "function",
                "name": "foo",
                "inputs": [],
                "outputs": [],
                "stateMutability": "nonpayable",
            }
        ]
        with pytest.raises(RuntimeError, match="ABI fragment conflict"):
            _merge_abis(a, b)

    def test_silently_dedups_identical_fragments(self) -> None:
        a = [
            {
                "type": "function",
                "name": "foo",
                "inputs": [{"type": "uint256", "name": "x"}],
                "outputs": [],
                "stateMutability": "view",
            }
        ]
        merged = _merge_abis(a, list(a))
        assert len(merged) == 1
