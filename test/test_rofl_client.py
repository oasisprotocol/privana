"""Tests for RoflAppdClient wrapper."""

import base64
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import cbor2
from web3.types import TxParams


class TestDecodeRevertReason(unittest.TestCase):
    """Test cases for _decode_revert_reason function."""

    def test_decode_known_error_selector(self):
        """Test decoding a known error selector (InvalidSignature)."""
        from src.clients.rofl import _decode_revert_reason

        # InvalidSignature() selector is 0x8baa579f
        raw_message = base64.b64encode(bytes.fromhex("8baa579f")).decode()
        result = _decode_revert_reason(raw_message)
        self.assertEqual(result, "InvalidSignature")

    def test_decode_insufficient_balance_error(self):
        """Test decoding InsufficientBalance error."""
        from src.clients.rofl import _decode_revert_reason

        # InsufficientBalance() selector is 0xf4d678b8 (from ABI)
        raw_message = base64.b64encode(bytes.fromhex("f4d678b8")).decode()
        result = _decode_revert_reason(raw_message)
        self.assertEqual(result, "InsufficientBalance")

    def test_decode_unknown_selector(self):
        """Test decoding an unknown error selector."""
        from src.clients.rofl import _decode_revert_reason

        # Unknown selector
        raw_message = base64.b64encode(bytes.fromhex("deadbeef")).decode()
        result = _decode_revert_reason(raw_message)
        self.assertIn("unknown", result.lower())
        self.assertIn("0xdeadbeef", result)

    def test_decode_none_message(self):
        """Test decoding None message."""
        from src.clients.rofl import _decode_revert_reason

        result = _decode_revert_reason(None)
        self.assertEqual(result, "unknown")

    def test_decode_invalid_base64(self):
        """Test decoding invalid base64 returns the raw message."""
        from src.clients.rofl import _decode_revert_reason

        result = _decode_revert_reason("not-valid-base64!!!")
        self.assertEqual(result, "not-valid-base64!!!")


class TestTransactionRevertedError(unittest.TestCase):
    """Test cases for TransactionRevertedError exception."""

    def test_error_attributes(self):
        """Test that error attributes are set correctly."""
        from src.clients.rofl import TransactionRevertedError

        error = TransactionRevertedError(
            "Test error",
            code=8,
            module="evm",
            error_name="InvalidSignature",
        )

        self.assertEqual(str(error), "Test error")
        self.assertEqual(error.code, 8)
        self.assertEqual(error.module, "evm")
        self.assertEqual(error.error_name, "InvalidSignature")


class TestRoflAppdClient(unittest.IsolatedAsyncioTestCase):
    """Test cases for RoflAppdClient wrapper."""

    def setUp(self):
        """Reset singleton before each test."""
        from src.clients.rofl import RoflAppdClient

        RoflAppdClient._instance = None

    @patch("src.clients.rofl.AsyncRoflClient")
    async def test_submit_tx_success(self, mock_async_client_class):
        """Test that submit_tx correctly handles successful transaction."""
        from src.clients.rofl import RoflAppdClient

        # Setup mock - successful response with "ok"
        mock_client = MagicMock()
        mock_client.sign_submit = AsyncMock(return_value={"ok": b""})
        mock_async_client_class.return_value = mock_client

        # Create client and submit tx
        client = RoflAppdClient()
        tx: TxParams = {
            "to": "0x0987654321098765432109876543210987654321",
            "data": "0xabcdef",
            "gas": 100000,
            "value": 0,
        }
        result = await client.submit_tx(tx, encrypt=False)

        # Verify - returns RoflSubmissionResult with hex-encoded CBOR as submission_id
        expected = cbor2.dumps({"ok": b""}).hex()
        self.assertEqual(result.submission_id, expected)
        self.assertEqual(result.ok_payload, b"")

    @patch("src.clients.rofl.AsyncRoflClient")
    async def test_submit_tx_reverted_raises_error(self, mock_async_client_class):
        """Test that submit_tx raises TransactionRevertedError on revert."""
        from src.clients.rofl import RoflAppdClient, TransactionRevertedError

        # Setup mock - failed response with "fail"
        mock_client = MagicMock()
        # InvalidSignature() selector
        revert_reason = base64.b64encode(bytes.fromhex("8baa579f")).decode()
        mock_client.sign_submit = AsyncMock(
            return_value={
                "fail": {"code": 8, "module": "evm", "message": f"reverted: {revert_reason}"}
            }
        )
        mock_async_client_class.return_value = mock_client

        # Create client and submit tx
        client = RoflAppdClient()
        tx: TxParams = {
            "to": "0x0987654321098765432109876543210987654321",
            "data": "0xabcdef",
            "gas": 100000,
            "value": 0,
        }

        # Should raise TransactionRevertedError
        with self.assertRaises(TransactionRevertedError) as ctx:
            await client.submit_tx(tx, encrypt=False)

        error = ctx.exception
        self.assertEqual(error.code, 8)
        self.assertEqual(error.module, "evm")
        self.assertIn("InvalidSignature", str(error))

    @patch("src.clients.rofl.AsyncRoflClient")
    async def test_submit_tx_requires_to_and_data(self, mock_async_client_class):
        """Test that submit_tx raises ValueError without 'to' or 'data'."""
        from src.clients.rofl import RoflAppdClient

        mock_client = MagicMock()
        mock_async_client_class.return_value = mock_client

        client = RoflAppdClient()

        # Missing 'to'
        with self.assertRaises(ValueError):
            await client.submit_tx({"data": "0x123"})

        # Missing 'data'
        with self.assertRaises(ValueError):
            await client.submit_tx({"to": "0x123"})

    @patch("src.clients.rofl.AsyncRoflClient")
    async def test_submit_tx_invalid_cbor_raises_error(self, mock_async_client_class):
        """Test that submit_tx raises ValueError when response is invalid CBOR."""
        from src.clients.rofl import RoflAppdClient

        mock_client = MagicMock()
        mock_client.sign_submit = AsyncMock(side_effect=cbor2.CBORDecodeError("invalid"))
        mock_async_client_class.return_value = mock_client

        client = RoflAppdClient()
        tx: TxParams = {
            "to": "0x0987654321098765432109876543210987654321",
            "data": "0xabcdef",
        }

        with self.assertRaises(ValueError) as ctx:
            await client.submit_tx(tx)

        self.assertIn("invalid CBOR", str(ctx.exception))

    @patch("src.clients.rofl.AsyncRoflClient")
    async def test_submit_tx_missing_ok_key_raises_error(self, mock_async_client_class):
        """Test that submit_tx raises ValueError when response has no 'ok' or 'fail' key."""
        from src.clients.rofl import RoflAppdClient

        mock_client = MagicMock()
        # Return CBOR with unexpected structure (neither "ok" nor "fail")
        mock_client.sign_submit = AsyncMock(
            return_value={"error": "something went wrong", "code": 123}
        )
        mock_async_client_class.return_value = mock_client

        client = RoflAppdClient()
        tx: TxParams = {
            "to": "0x0987654321098765432109876543210987654321",
            "data": "0xabcdef",
        }

        with self.assertRaises(ValueError) as ctx:
            await client.submit_tx(tx)

        self.assertIn("missing 'ok' key", str(ctx.exception))

    @patch("src.clients.rofl.AsyncRoflClient")
    async def test_get_keypair(self, mock_async_client_class):
        """Test that get_keypair generates correct keypair."""
        from src.clients.rofl import RoflAppdClient

        # Mock key generation - return a valid private key (32 bytes hex)
        mock_client = MagicMock()
        mock_client.generate_key = AsyncMock(
            return_value="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        mock_async_client_class.return_value = mock_client

        client = RoflAppdClient()
        private_key, public_address = await client.get_keypair("test_key")

        # Verify
        self.assertTrue(private_key.startswith("0x"))
        self.assertTrue(public_address.startswith("0x"))
        self.assertEqual(len(public_address), 42)  # 0x + 40 hex chars
        mock_client.generate_key.assert_called_once_with("test_key")

    @patch("src.clients.rofl.AsyncRoflClient")
    def test_singleton_pattern(self, mock_async_client_class):
        """Test that RoflAppdClient is a singleton."""
        from src.clients.rofl import RoflAppdClient

        mock_client = MagicMock()
        mock_async_client_class.return_value = mock_client

        client1 = RoflAppdClient()
        client2 = RoflAppdClient()

        self.assertIs(client1, client2)
        # AsyncRoflClient should only be instantiated once
        self.assertEqual(mock_async_client_class.call_count, 1)


if __name__ == "__main__":
    unittest.main()
