"""Tests for deposit verification logic.

These tests mock the web3 RPC calls to verify the verifier's logic
without hitting a real chain.
"""

from unittest.mock import AsyncMock, patch

import pytest
from web3 import Web3

from src.services.deposit_verifier import DepositVerifier, VerifiedDeposit

DEPOSIT_ADDR = Web3.to_checksum_address("0x" + "aa" * 20)
TOKEN_ADDR = Web3.to_checksum_address("0x" + "bb" * 20)
TX_HASH = "0x" + "cc" * 32
ONE_ETH = Web3.to_wei(1, "ether")
HALF_ETH = Web3.to_wei(0.5, "ether")
HUNDRED_USDC = 100 * 10**6


@pytest.fixture
def verifier():
    chain_rpc_urls = {84532: "https://fake-rpc.example.com"}
    return DepositVerifier(chain_rpc_urls)


@pytest.mark.asyncio
async def test_verify_native_top_level_deposit(verifier):
    """Direct native ETH transfer: tx.to == depositAddress, value > 0."""
    mock_receipt = {
        "status": 1,
        "blockNumber": 100,
        "transactionHash": bytes.fromhex("cc" * 32),
        "to": DEPOSIT_ADDR,
        "logs": [],
    }
    mock_tx = {
        "to": DEPOSIT_ADDR,
        "value": ONE_ETH,
        "from": "0x" + "dd" * 20,
    }
    mock_block = {"number": 200}  # 100 confirmations

    with patch.object(verifier, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        w3.eth.get_transaction_receipt = AsyncMock(return_value=mock_receipt)
        w3.eth.get_transaction = AsyncMock(return_value=mock_tx)
        w3.eth.get_block = AsyncMock(return_value=mock_block)
        mock_get_w3.return_value = w3

        result = await verifier.verify_deposit(
            chain_id=84532,
            tx_hash=TX_HASH,
            deposit_address=DEPOSIT_ADDR,
            expected_amount=ONE_ETH,
            log_index=0,
        )

    assert isinstance(result, VerifiedDeposit)
    assert result.amount == ONE_ETH
    assert result.is_native is True
    assert result.token_address is None


@pytest.mark.asyncio
async def test_verify_erc20_deposit(verifier):
    """ERC20 Transfer event to depositAddress."""
    transfer_log = {
        "address": TOKEN_ADDR,
        "topics": [
            bytes.fromhex("ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"),
            bytes(12) + bytes.fromhex("dd" * 20),  # from
            bytes(12) + bytes.fromhex("aa" * 20),  # to = deposit addr
        ],
        "data": HUNDRED_USDC.to_bytes(32, "big"),
        "logIndex": 3,
    }
    mock_receipt = {
        "status": 1,
        "blockNumber": 100,
        "transactionHash": bytes.fromhex("cc" * 32),
        "to": "0x" + "ee" * 20,  # tx.to is a router, not token
        "logs": [transfer_log],
    }
    mock_block = {"number": 200}

    with patch.object(verifier, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        w3.eth.get_transaction_receipt = AsyncMock(return_value=mock_receipt)
        w3.eth.get_block = AsyncMock(return_value=mock_block)
        mock_get_w3.return_value = w3

        result = await verifier.verify_deposit(
            chain_id=84532,
            tx_hash=TX_HASH,
            deposit_address=DEPOSIT_ADDR,
            expected_amount=HUNDRED_USDC,
            log_index=3,
        )

    assert isinstance(result, VerifiedDeposit)
    assert result.amount == HUNDRED_USDC
    assert result.is_native is False
    assert result.token_address == TOKEN_ADDR


@pytest.mark.asyncio
async def test_verify_erc20_multi_transfer_selects_by_log_index(verifier):
    """When multiple Transfer events match, select by EVM logIndex (not position)."""
    # Two Transfer events to the deposit address, at logIndex 2 and 5
    transfer_a = {
        "address": TOKEN_ADDR,
        "topics": [
            bytes.fromhex("ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"),
            bytes(12) + bytes.fromhex("dd" * 20),
            bytes(12) + bytes.fromhex("aa" * 20),
        ],
        "data": (50 * 10**6).to_bytes(32, "big"),  # 50 USDC
        "logIndex": 2,
    }
    transfer_b = {
        "address": TOKEN_ADDR,
        "topics": [
            bytes.fromhex("ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"),
            bytes(12) + bytes.fromhex("dd" * 20),
            bytes(12) + bytes.fromhex("aa" * 20),
        ],
        "data": HUNDRED_USDC.to_bytes(32, "big"),  # 100 USDC
        "logIndex": 5,
    }
    # Unrelated log between them to ensure logIndex != position
    unrelated_log = {"topics": [], "data": b"", "logIndex": 3}
    mock_receipt = {
        "status": 1,
        "blockNumber": 100,
        "logs": [transfer_a, unrelated_log, transfer_b],
    }
    mock_block = {"number": 200}

    with patch.object(verifier, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        w3.eth.get_transaction_receipt = AsyncMock(return_value=mock_receipt)
        w3.eth.get_block = AsyncMock(return_value=mock_block)
        mock_get_w3.return_value = w3

        result = await verifier.verify_deposit(
            chain_id=84532,
            tx_hash=TX_HASH,
            deposit_address=DEPOSIT_ADDR,
            expected_amount=HUNDRED_USDC,
            log_index=5,  # select the second transfer by its actual logIndex
        )

    assert result.amount == HUNDRED_USDC
    assert result.deposit_index == 5  # actual EVM logIndex, not position


@pytest.mark.asyncio
async def test_verify_erc20_multi_transfer_invalid_log_index(verifier):
    """Reject when log_index doesn't match any Transfer event's logIndex."""
    transfer_log = {
        "address": TOKEN_ADDR,
        "topics": [
            bytes.fromhex("ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"),
            bytes(12) + bytes.fromhex("dd" * 20),
            bytes(12) + bytes.fromhex("aa" * 20),
        ],
        "data": HUNDRED_USDC.to_bytes(32, "big"),
        "logIndex": 2,
    }
    transfer_log_2 = {
        "address": TOKEN_ADDR,
        "topics": [
            bytes.fromhex("ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"),
            bytes(12) + bytes.fromhex("dd" * 20),
            bytes(12) + bytes.fromhex("aa" * 20),
        ],
        "data": HUNDRED_USDC.to_bytes(32, "big"),
        "logIndex": 7,
    }
    mock_receipt = {
        "status": 1,
        "blockNumber": 100,
        "logs": [transfer_log, transfer_log_2],
    }
    mock_block = {"number": 200}

    with patch.object(verifier, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        w3.eth.get_transaction_receipt = AsyncMock(return_value=mock_receipt)
        w3.eth.get_block = AsyncMock(return_value=mock_block)
        mock_get_w3.return_value = w3

        with pytest.raises(ValueError, match="No Transfer event with logIndex=99"):
            await verifier.verify_deposit(
                chain_id=84532,
                tx_hash=TX_HASH,
                deposit_address=DEPOSIT_ADDR,
                expected_amount=HUNDRED_USDC,
                log_index=99,  # doesn't exist
            )


@pytest.mark.asyncio
async def test_reject_failed_transaction(verifier):
    """Reverted tx should be rejected."""
    mock_receipt = {
        "status": 0,  # reverted
        "blockNumber": 100,
        "transactionHash": bytes.fromhex("cc" * 32),
        "to": DEPOSIT_ADDR,
        "logs": [],
    }

    with patch.object(verifier, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        w3.eth.get_transaction_receipt = AsyncMock(return_value=mock_receipt)
        mock_get_w3.return_value = w3

        with pytest.raises(ValueError, match="reverted"):
            await verifier.verify_deposit(
                chain_id=84532,
                tx_hash=TX_HASH,
                deposit_address=DEPOSIT_ADDR,
                expected_amount=ONE_ETH,
                log_index=0,
            )


@pytest.mark.asyncio
async def test_reject_insufficient_finality(verifier):
    """Tx without enough confirmations should be rejected."""
    mock_receipt = {
        "status": 1,
        "blockNumber": 195,  # only 5 confirmations (need 15 for Base)
        "transactionHash": bytes.fromhex("cc" * 32),
        "to": DEPOSIT_ADDR,
        "logs": [],
    }
    mock_tx = {"to": DEPOSIT_ADDR, "value": ONE_ETH, "from": "0x" + "dd" * 20}
    mock_block = {"number": 200}

    with patch.object(verifier, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        w3.eth.get_transaction_receipt = AsyncMock(return_value=mock_receipt)
        w3.eth.get_transaction = AsyncMock(return_value=mock_tx)
        w3.eth.get_block = AsyncMock(return_value=mock_block)
        mock_get_w3.return_value = w3

        with pytest.raises(ValueError, match="finality"):
            await verifier.verify_deposit(
                chain_id=84532,
                tx_hash=TX_HASH,
                deposit_address=DEPOSIT_ADDR,
                expected_amount=ONE_ETH,
                log_index=0,
            )


@pytest.mark.asyncio
async def test_reject_no_deposit_found(verifier):
    """Tx that doesn't send anything to deposit address."""
    mock_receipt = {
        "status": 1,
        "blockNumber": 100,
        "transactionHash": bytes.fromhex("cc" * 32),
        "to": "0x" + "ff" * 20,  # different address
        "logs": [],
    }
    mock_tx = {"to": "0x" + "ff" * 20, "value": ONE_ETH, "from": "0x" + "dd" * 20}
    mock_block = {"number": 200}

    with patch.object(verifier, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        w3.eth.get_transaction_receipt = AsyncMock(return_value=mock_receipt)
        w3.eth.get_transaction = AsyncMock(return_value=mock_tx)
        w3.eth.get_block = AsyncMock(return_value=mock_block)
        # Balance-delta fallback also finds nothing
        w3.eth.get_balance = AsyncMock(return_value=0)
        mock_get_w3.return_value = w3

        with pytest.raises(ValueError, match="[Nn]o deposit found"):
            await verifier.verify_deposit(
                chain_id=84532,
                tx_hash=TX_HASH,
                deposit_address=DEPOSIT_ADDR,
                expected_amount=ONE_ETH,
                log_index=0,
            )


@pytest.mark.asyncio
async def test_verify_native_internal_call_deposit(verifier):
    """ETH sent via internal call (multisig, contract forwarding) — detected by balance delta."""
    mock_receipt = {
        "status": 1,
        "blockNumber": 100,
        "transactionHash": bytes.fromhex("cc" * 32),
        "to": "0x" + "ff" * 20,  # tx.to is a multisig, not the deposit address
        "logs": [],
    }
    # Top-level tx goes to the multisig, not deposit address
    mock_tx = {"to": "0x" + "ff" * 20, "value": 0, "from": "0x" + "dd" * 20}
    mock_block = {"number": 200}

    with patch.object(verifier, "_get_web3") as mock_get_w3:
        w3 = AsyncMock()
        w3.eth.get_transaction_receipt = AsyncMock(return_value=mock_receipt)
        w3.eth.get_transaction = AsyncMock(return_value=mock_tx)
        w3.eth.get_block = AsyncMock(return_value=mock_block)
        # Balance delta: 0 before the block, 0.5 ETH after (arrived via internal call)
        w3.eth.get_balance = AsyncMock(
            side_effect=lambda addr, block_identifier: 0 if block_identifier == 99 else HALF_ETH
        )
        mock_get_w3.return_value = w3

        result = await verifier.verify_deposit(
            chain_id=84532,
            tx_hash=TX_HASH,
            deposit_address=DEPOSIT_ADDR,
            expected_amount=HALF_ETH,
            log_index=0,
        )

    assert isinstance(result, VerifiedDeposit)
    assert result.amount == HALF_ETH
    assert result.is_native is True
    assert result.token_address is None
