"""Tests for the FDC deposit verifier service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config.fdc_config import FDCConfig
from src.services.fdc_deposit_verifier import (
    FDCDepositVerifier,
    FDCVerificationError,
    FDCVerifiedDeposit,
    _to_bytes32_string,
)


@pytest.fixture
def fdc_config():
    return FDCConfig(
        coston2_rpc_url="http://localhost:8545",
        coston2_private_key="0x" + "ab" * 32,
    )


def _mock_proof_data(
    tx_hash: str = "0x" + "aa" * 32,
    source_address: str = "0x1111111111111111111111111111111111111111",
    receiving_address: str = "0x2222222222222222222222222222222222222222",
    value: str = "1000000000000000000",
    status: str = "1",
    events: list | None = None,
) -> dict:
    """Build a mock DA layer proof response."""
    return {
        "proof": ["0x" + "cc" * 32, "0x" + "dd" * 32],
        "response": {
            "attestationType": "0x" + "00" * 32,
            "sourceId": "0x" + "00" * 32,
            "votingRound": "100",
            "lowestUsedTimestamp": "1700000000",
            "requestBody": {
                "transactionHash": tx_hash,
                "requiredConfirmations": "1",
                "provideInput": True,
                "listEvents": True,
                "logIndices": [],
            },
            "responseBody": {
                "blockNumber": "12345",
                "timestamp": "1700000000",
                "sourceAddress": source_address,
                "isDeployment": False,
                "receivingAddress": receiving_address,
                "value": value,
                "input": "0x",
                "status": status,
                "events": events or [],
            },
        },
    }


class TestToBytes32String:
    def test_short_string(self):
        result = _to_bytes32_string("testETH")
        assert result.startswith("0x")
        assert len(result) == 66  # 0x + 64 hex chars
        assert result == "0x" + "testETH".encode().hex().ljust(64, "0")

    def test_too_long_string(self):
        with pytest.raises(ValueError, match="too long"):
            _to_bytes32_string("a" * 33)


class TestFDCDepositVerifier:
    """Test the full verification flow with mocked externals."""

    @pytest.fixture
    def verifier(self, fdc_config):
        with patch("src.services.fdc_deposit_verifier.Web3") as mock_web3_cls:
            mock_web3 = MagicMock()
            mock_web3_cls.return_value = mock_web3
            mock_web3_cls.HTTPProvider.return_value = MagicMock()
            mock_web3_cls.to_checksum_address.side_effect = lambda x: x
            mock_web3_cls.to_bytes.side_effect = lambda hexstr: bytes.fromhex(
                hexstr[2:] if hexstr.startswith("0x") else hexstr
            )
            mock_web3_cls.from_wei.return_value = "0.001"
            mock_web3.eth.contract.return_value = MagicMock()
            yield FDCDepositVerifier(fdc_config)

    @pytest.mark.asyncio
    async def test_verify_deposit_unsupported_chain(self, verifier):
        with pytest.raises(FDCVerificationError, match="Unsupported chain ID"):
            await verifier.verify_deposit(chain_id=99999, tx_hash="0x" + "aa" * 32)

    @pytest.mark.asyncio
    async def test_verify_deposit_full_flow(self, verifier):
        """Test the happy path with all steps mocked."""
        tx_hash = "0x" + "aa" * 32
        proof_data = _mock_proof_data(tx_hash=tx_hash)

        verifier._prepare_request = AsyncMock(return_value="0xencoded")
        verifier._submit_request = AsyncMock(return_value=100)
        verifier._wait_for_proof = AsyncMock(return_value=proof_data)
        verifier._verify_on_coston2 = AsyncMock(return_value=True)

        result = await verifier.verify_deposit(chain_id=11155111, tx_hash=tx_hash)

        assert isinstance(result, FDCVerifiedDeposit)
        assert result.tx_hash == tx_hash
        assert result.chain_id == 11155111
        assert result.source_address == "0x1111111111111111111111111111111111111111"
        assert result.value == 1000000000000000000
        assert result.status == 1

        verifier._prepare_request.assert_awaited_once()
        verifier._submit_request.assert_awaited_once_with("0xencoded")
        verifier._wait_for_proof.assert_awaited_once()
        verifier._verify_on_coston2.assert_awaited_once_with(proof_data)

    @pytest.mark.asyncio
    async def test_verify_deposit_verification_fails(self, verifier):
        """Test that verification failure raises error."""
        verifier._prepare_request = AsyncMock(return_value="0xencoded")
        verifier._submit_request = AsyncMock(return_value=100)
        verifier._wait_for_proof = AsyncMock(return_value=_mock_proof_data())
        verifier._verify_on_coston2 = AsyncMock(return_value=False)

        with pytest.raises(FDCVerificationError, match="verification failed"):
            await verifier.verify_deposit(chain_id=11155111, tx_hash="0x" + "bb" * 32)

    @pytest.mark.asyncio
    async def test_verify_deposit_with_events(self, verifier):
        """Test ERC-20 deposit with Transfer events."""
        events = [
            {
                "logIndex": 0,
                "emitterAddress": "0x1234567890abcdef1234567890abcdef12345678",
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x" + "00" * 12 + "1111111111111111111111111111111111111111",
                    "0x" + "00" * 12 + "2222222222222222222222222222222222222222",
                ],
                "data": "0x" + hex(100_000_000)[2:].zfill(64),
                "removed": False,
            }
        ]
        proof_data = _mock_proof_data(events=events, value="0", status="1")

        verifier._prepare_request = AsyncMock(return_value="0xencoded")
        verifier._submit_request = AsyncMock(return_value=100)
        verifier._wait_for_proof = AsyncMock(return_value=proof_data)
        verifier._verify_on_coston2 = AsyncMock(return_value=True)

        result = await verifier.verify_deposit(chain_id=11155111, tx_hash="0x" + "cc" * 32)

        assert result.status == 1
        assert len(result.events) == 1
        assert result.events[0]["emitterAddress"] == "0x1234567890abcdef1234567890abcdef12345678"


class TestExtractErc20Amount:
    """Test the ERC-20 amount extraction helper on DepositListener."""

    DEPOSIT_ADDR = "0x2222222222222222222222222222222222222222"

    def test_extracts_transfer_amount(self):
        from src.services.deposit_listener import DepositListener

        events = [
            {
                "emitterAddress": "0xAbCdEf1234567890abcdef1234567890AbCdEf12",
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x" + "00" * 12 + "1111111111111111111111111111111111111111",
                    "0x" + "00" * 12 + "2222222222222222222222222222222222222222",
                ],
                "data": "0x" + hex(500_000)[2:].zfill(64),
            }
        ]

        amount = DepositListener._extract_erc20_amount(
            events, "0xAbCdEf1234567890abcdef1234567890AbCdEf12", self.DEPOSIT_ADDR
        )
        assert amount == 500_000

    def test_returns_none_when_no_transfer(self):
        from src.services.deposit_listener import DepositListener

        amount = DepositListener._extract_erc20_amount([], "0x1234", self.DEPOSIT_ADDR)
        assert amount is None

    def test_ignores_events_from_wrong_contract(self):
        from src.services.deposit_listener import DepositListener

        events = [
            {
                "emitterAddress": "0x0000000000000000000000000000000000000001",
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x" + "00" * 12 + "1111111111111111111111111111111111111111",
                    "0x" + "00" * 12 + "2222222222222222222222222222222222222222",
                ],
                "data": "0x" + hex(100)[2:].zfill(64),
            }
        ]

        amount = DepositListener._extract_erc20_amount(events, "0x1234", self.DEPOSIT_ADDR)
        assert amount is None

    def test_rejects_transfer_to_wrong_recipient(self):
        """F1: Transfer to a different address must not be credited."""
        from src.services.deposit_listener import DepositListener

        wrong_recipient = "0x" + "00" * 12 + "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        events = [
            {
                "emitterAddress": "0xAbCdEf1234567890abcdef1234567890AbCdEf12",
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x" + "00" * 12 + "1111111111111111111111111111111111111111",
                    wrong_recipient,
                ],
                "data": "0x" + hex(500_000)[2:].zfill(64),
            }
        ]

        amount = DepositListener._extract_erc20_amount(
            events, "0xAbCdEf1234567890abcdef1234567890AbCdEf12", self.DEPOSIT_ADDR
        )
        assert amount is None

    def test_slices_data_to_32_bytes(self):
        """F2: Extra bytes in data field must not inflate the amount."""
        from src.services.deposit_listener import DepositListener

        # 32 bytes of amount (500000) + 32 bytes of garbage
        amount_hex = hex(500_000)[2:].zfill(64)
        garbage_hex = "ff" * 32
        data = "0x" + amount_hex + garbage_hex

        events = [
            {
                "emitterAddress": "0xAbCdEf1234567890abcdef1234567890AbCdEf12",
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x" + "00" * 12 + "1111111111111111111111111111111111111111",
                    "0x" + "00" * 12 + "2222222222222222222222222222222222222222",
                ],
                "data": data,
            }
        ]

        amount = DepositListener._extract_erc20_amount(
            events, "0xAbCdEf1234567890abcdef1234567890AbCdEf12", self.DEPOSIT_ADDR
        )
        assert amount == 500_000
