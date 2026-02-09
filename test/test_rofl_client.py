"""Tests for RoflAppdClient wrapper."""

import base64
import unittest
from unittest.mock import MagicMock, patch

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


class TestRoflAppdClient(unittest.TestCase):
    """Test cases for RoflAppdClient wrapper."""

    def setUp(self):
        """Reset singleton before each test."""
        from src.clients.rofl import RoflAppdClient

        RoflAppdClient._instance = None

    @patch("oasis_rofl_client.common.get_tx_payload")
    @patch("src.clients.rofl.RoflClient")
    def test_submit_tx_success(self, mock_rofl_client_class, mock_get_tx_payload):
        """Test that submit_tx correctly handles successful transaction."""
        from src.clients.rofl import RoflAppdClient

        # Setup mock - successful response with "ok" in CBOR
        mock_client = MagicMock()
        success_cbor = cbor2.dumps({"ok": b""})
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": success_cbor.hex()}
        mock_client._appd_request.return_value = mock_response
        mock_rofl_client_class.return_value = mock_client
        mock_get_tx_payload.return_value = {"mock": "payload"}

        # Create client and submit tx
        client = RoflAppdClient()
        tx: TxParams = {
            "to": "0x0987654321098765432109876543210987654321",
            "data": "0xabcdef",
            "gas": 100000,
            "value": 0,
        }
        result = client.submit_tx(tx, encrypt=False)

        # Verify - returns the hex-encoded CBOR as submission_id
        self.assertEqual(result, success_cbor.hex())

    @patch("oasis_rofl_client.common.get_tx_payload")
    @patch("src.clients.rofl.RoflClient")
    def test_submit_tx_reverted_raises_error(self, mock_rofl_client_class, mock_get_tx_payload):
        """Test that submit_tx raises TransactionRevertedError on revert."""
        from src.clients.rofl import RoflAppdClient, TransactionRevertedError

        # Setup mock - failed response with "fail" in CBOR
        mock_client = MagicMock()
        # InvalidSignature() selector
        revert_reason = base64.b64encode(bytes.fromhex("8baa579f")).decode()
        fail_cbor = cbor2.dumps(
            {"fail": {"code": 8, "module": "evm", "message": f"reverted: {revert_reason}"}}
        )
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": fail_cbor.hex()}
        mock_client._appd_request.return_value = mock_response
        mock_rofl_client_class.return_value = mock_client
        mock_get_tx_payload.return_value = {"mock": "payload"}

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
            client.submit_tx(tx, encrypt=False)

        error = ctx.exception
        self.assertEqual(error.code, 8)
        self.assertEqual(error.module, "evm")
        self.assertIn("InvalidSignature", str(error))

    @patch("src.clients.rofl.RoflClient")
    def test_submit_tx_requires_to_and_data(self, mock_rofl_client_class):
        """Test that submit_tx raises ValueError without 'to' or 'data'."""
        from src.clients.rofl import RoflAppdClient

        mock_client = MagicMock()
        mock_rofl_client_class.return_value = mock_client

        client = RoflAppdClient()

        # Missing 'to'
        with self.assertRaises(ValueError):
            client.submit_tx({"data": "0x123"})

        # Missing 'data'
        with self.assertRaises(ValueError):
            client.submit_tx({"to": "0x123"})

    @patch("oasis_rofl_client.common.get_tx_payload")
    @patch("src.clients.rofl.RoflClient")
    def test_submit_tx_missing_data_in_response(self, mock_rofl_client_class, mock_get_tx_payload):
        """Test that submit_tx raises ValueError when response has no data."""
        from src.clients.rofl import RoflAppdClient

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {}  # No "data" field
        mock_client._appd_request.return_value = mock_response
        mock_rofl_client_class.return_value = mock_client
        mock_get_tx_payload.return_value = {"mock": "payload"}

        client = RoflAppdClient()
        tx: TxParams = {
            "to": "0x0987654321098765432109876543210987654321",
            "data": "0xabcdef",
        }

        with self.assertRaises(ValueError) as ctx:
            client.submit_tx(tx)

        self.assertIn("Unexpected ROFL response", str(ctx.exception))

    @patch("oasis_rofl_client.common.get_tx_payload")
    @patch("src.clients.rofl.RoflClient")
    def test_submit_tx_non_cbor_data(self, mock_rofl_client_class, mock_get_tx_payload):
        """Test that submit_tx handles non-CBOR data gracefully."""
        from src.clients.rofl import RoflAppdClient

        mock_client = MagicMock()
        # Return hex data that isn't valid CBOR
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": "deadbeef"}
        mock_client._appd_request.return_value = mock_response
        mock_rofl_client_class.return_value = mock_client
        mock_get_tx_payload.return_value = {"mock": "payload"}

        client = RoflAppdClient()
        tx: TxParams = {
            "to": "0x0987654321098765432109876543210987654321",
            "data": "0xabcdef",
        }

        # Should return the data as-is (not raise error)
        result = client.submit_tx(tx)
        self.assertEqual(result, "deadbeef")

    @patch("oasis_rofl_client.common.get_tx_payload")
    @patch("src.clients.rofl.RoflClient")
    def test_submit_tx_unexpected_cbor_structure_logs_warning(
        self, mock_rofl_client_class, mock_get_tx_payload
    ):
        """Test that submit_tx logs a warning for unexpected CBOR structures."""
        from src.clients.rofl import RoflAppdClient

        mock_client = MagicMock()
        # Return CBOR with unexpected structure (neither "ok" nor "fail")
        unexpected_cbor = cbor2.dumps({"error": "something went wrong", "code": 123})
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": unexpected_cbor.hex()}
        mock_client._appd_request.return_value = mock_response
        mock_rofl_client_class.return_value = mock_client
        mock_get_tx_payload.return_value = {"mock": "payload"}

        client = RoflAppdClient()
        tx: TxParams = {
            "to": "0x0987654321098765432109876543210987654321",
            "data": "0xabcdef",
        }

        # Should return data but log a warning (we verify by checking no exception raised)
        with patch("src.clients.rofl.logger") as mock_logger:
            client.submit_tx(tx)
            # Verify warning was logged
            mock_logger.warning.assert_called_once()
            warning_call = mock_logger.warning.call_args
            self.assertIn("Unexpected CBOR response structure", warning_call[0][0])

    @patch("src.clients.rofl.RoflClient")
    def test_get_keypair(self, mock_rofl_client_class):
        """Test that get_keypair generates correct keypair."""
        from src.clients.rofl import RoflAppdClient

        # Mock key generation - return a valid private key (32 bytes hex)
        mock_client = MagicMock()
        mock_client.generate_key.return_value = (
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        mock_rofl_client_class.return_value = mock_client

        client = RoflAppdClient()
        private_key, public_address = client.get_keypair("test_key")

        # Verify
        self.assertTrue(private_key.startswith("0x"))
        self.assertTrue(public_address.startswith("0x"))
        self.assertEqual(len(public_address), 42)  # 0x + 40 hex chars
        mock_client.generate_key.assert_called_once_with("test_key")

    @patch("src.clients.rofl.RoflClient")
    def test_singleton_pattern(self, mock_rofl_client_class):
        """Test that RoflAppdClient is a singleton."""
        from src.clients.rofl import RoflAppdClient

        mock_client = MagicMock()
        mock_rofl_client_class.return_value = mock_client

        client1 = RoflAppdClient()
        client2 = RoflAppdClient()

        self.assertIs(client1, client2)
        # RoflClient should only be instantiated once
        self.assertEqual(mock_rofl_client_class.call_count, 1)


if __name__ == "__main__":
    unittest.main()
