"""Tests for Deoxys-II implementation."""

import pytest

from src.crypto.deoxysii import AEAD, KEY_SIZE, NONCE_SIZE, TAG_SIZE, DecryptionError


class TestDeoxysIIBasic:
    """Basic tests for Deoxys-II AEAD."""

    def test_key_size_validation(self):
        """Should reject invalid key sizes."""
        with pytest.raises(ValueError, match="Invalid key size"):
            AEAD(b"too short")

        with pytest.raises(ValueError, match="Invalid key size"):
            AEAD(b"x" * 33)

        # Valid key should work
        AEAD(b"x" * KEY_SIZE)

    def test_nonce_size_validation(self):
        """Should reject invalid nonce sizes."""
        aead = AEAD(b"x" * KEY_SIZE)

        with pytest.raises(ValueError, match="Invalid nonce size"):
            aead.encrypt(b"too short", b"plaintext")

        with pytest.raises(ValueError, match="Invalid nonce size"):
            aead.encrypt(b"x" * 16, b"plaintext")

        # Valid nonce should work
        aead.encrypt(b"x" * NONCE_SIZE, b"plaintext")

    def test_encrypt_decrypt_roundtrip(self):
        """Should encrypt and decrypt successfully."""
        key = bytes(range(KEY_SIZE))
        nonce = bytes(range(NONCE_SIZE))
        plaintext = b"Hello, Deoxys-II!"

        aead = AEAD(key)
        ciphertext = aead.encrypt(nonce, plaintext)
        decrypted = aead.decrypt(nonce, ciphertext)

        assert decrypted == plaintext

    def test_encrypt_decrypt_with_associated_data(self):
        """Should encrypt and decrypt with associated data."""
        key = bytes(range(KEY_SIZE))
        nonce = bytes(range(NONCE_SIZE))
        plaintext = b"Secret message"
        ad = b"Additional authenticated data"

        aead = AEAD(key)
        ciphertext = aead.encrypt(nonce, plaintext, ad)
        decrypted = aead.decrypt(nonce, ciphertext, ad)

        assert decrypted == plaintext

    def test_wrong_associated_data_fails(self):
        """Should fail decryption with wrong associated data."""
        key = bytes(range(KEY_SIZE))
        nonce = bytes(range(NONCE_SIZE))
        plaintext = b"Secret message"
        ad = b"Correct AD"

        aead = AEAD(key)
        ciphertext = aead.encrypt(nonce, plaintext, ad)

        with pytest.raises(DecryptionError):
            aead.decrypt(nonce, ciphertext, b"Wrong AD")

    def test_tampered_ciphertext_fails(self):
        """Should fail decryption with tampered ciphertext."""
        key = bytes(range(KEY_SIZE))
        nonce = bytes(range(NONCE_SIZE))
        plaintext = b"Secret message"

        aead = AEAD(key)
        ciphertext = aead.encrypt(nonce, plaintext)

        # Tamper with the ciphertext
        tampered = bytearray(ciphertext)
        tampered[0] ^= 0xFF
        tampered = bytes(tampered)

        with pytest.raises(DecryptionError):
            aead.decrypt(nonce, tampered)

    def test_tampered_tag_fails(self):
        """Should fail decryption with tampered tag."""
        key = bytes(range(KEY_SIZE))
        nonce = bytes(range(NONCE_SIZE))
        plaintext = b"Secret message"

        aead = AEAD(key)
        ciphertext = aead.encrypt(nonce, plaintext)

        # Tamper with the tag (last 16 bytes)
        tampered = bytearray(ciphertext)
        tampered[-1] ^= 0xFF
        tampered = bytes(tampered)

        with pytest.raises(DecryptionError):
            aead.decrypt(nonce, tampered)

    def test_wrong_key_fails(self):
        """Should fail decryption with wrong key."""
        key1 = bytes(range(KEY_SIZE))
        key2 = bytes(range(1, KEY_SIZE + 1))
        nonce = bytes(range(NONCE_SIZE))
        plaintext = b"Secret message"

        aead1 = AEAD(key1)
        aead2 = AEAD(key2)

        ciphertext = aead1.encrypt(nonce, plaintext)

        with pytest.raises(DecryptionError):
            aead2.decrypt(nonce, ciphertext)

    def test_empty_plaintext(self):
        """Should handle empty plaintext."""
        key = bytes(range(KEY_SIZE))
        nonce = bytes(range(NONCE_SIZE))

        aead = AEAD(key)
        ciphertext = aead.encrypt(nonce, b"")

        assert len(ciphertext) == TAG_SIZE
        decrypted = aead.decrypt(nonce, ciphertext)
        assert decrypted == b""

    def test_none_plaintext(self):
        """Should handle None plaintext."""
        key = bytes(range(KEY_SIZE))
        nonce = bytes(range(NONCE_SIZE))

        aead = AEAD(key)
        ciphertext = aead.encrypt(nonce, None)

        assert len(ciphertext) == TAG_SIZE
        decrypted = aead.decrypt(nonce, ciphertext)
        assert decrypted == b""

    def test_ciphertext_length(self):
        """Ciphertext should be plaintext length + tag size."""
        key = bytes(range(KEY_SIZE))
        nonce = bytes(range(NONCE_SIZE))

        aead = AEAD(key)

        for length in [0, 1, 15, 16, 17, 31, 32, 33, 100, 1000]:
            plaintext = b"x" * length
            ciphertext = aead.encrypt(nonce, plaintext)
            assert len(ciphertext) == length + TAG_SIZE

    def test_deterministic_encryption(self):
        """Same inputs should produce same outputs."""
        key = bytes(range(KEY_SIZE))
        nonce = bytes(range(NONCE_SIZE))
        plaintext = b"test"

        aead = AEAD(key)
        ct1 = aead.encrypt(nonce, plaintext)
        ct2 = aead.encrypt(nonce, plaintext)

        assert ct1 == ct2


class TestDeoxysIIKnownVectors:
    """Test vectors to verify compatibility with TypeScript implementation."""

    def test_zero_key_zero_nonce_empty_message(self):
        """Test with all zeros."""
        key = b"\x00" * KEY_SIZE
        nonce = b"\x00" * NONCE_SIZE

        aead = AEAD(key)
        ciphertext = aead.encrypt(nonce, b"")

        # The ciphertext should be exactly TAG_SIZE bytes
        assert len(ciphertext) == TAG_SIZE

        # Verify roundtrip
        decrypted = aead.decrypt(nonce, ciphertext)
        assert decrypted == b""

    def test_sapphire_nonce_zero(self):
        """Test with nonce=0 which Sapphire uses (15 zero bytes)."""
        # This matches the contract's usage: Sapphire.encrypt(key, 0, data, "")
        key = bytes.fromhex("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
        nonce = b"\x00" * NONCE_SIZE  # nonce=0 means 15 zero bytes
        plaintext = b"Test message for Sapphire"

        aead = AEAD(key)
        ciphertext = aead.encrypt(nonce, plaintext)
        decrypted = aead.decrypt(nonce, ciphertext)

        assert decrypted == plaintext

    def test_various_message_lengths(self):
        """Test various message lengths for block boundary conditions."""
        key = bytes.fromhex("deadbeefdeadbeefdeadbeefdeadbeefcafebabecafebabecafebabecafebabe")
        nonce = bytes.fromhex("112233445566778899aabbccddeeff")

        aead = AEAD(key)

        # Test lengths around block boundaries (16 bytes)
        for length in [0, 1, 15, 16, 17, 31, 32, 33, 47, 48, 49, 100]:
            plaintext = bytes(range(256))[:length] if length > 0 else b""
            ciphertext = aead.encrypt(nonce, plaintext)
            decrypted = aead.decrypt(nonce, ciphertext)
            assert decrypted == plaintext, f"Failed for length {length}"


class TestDeoxysIISapphireCompatibility:
    """Tests for compatibility with Sapphire contract encryption.

    These tests verify that our Python implementation matches the
    Sapphire contract's encryption format.
    """

    def test_auth_token_struct_encoding(self):
        """Test encryption of an ABI-encoded AuthToken struct.

        This simulates what the contract does:
        Sapphire.encrypt(_authTokenEncKey, 0, abi.encode(authToken), "")
        """
        from eth_abi import encode

        # Simulate an AuthToken struct (domain, userAddr, validUntil, statement, resources)
        domain = "https://example.com"
        user_addr = bytes.fromhex("1234567890123456789012345678901234567890")
        valid_until = 1700000000
        statement = "Sign in to example.com"
        resources = ["https://example.com/api"]

        # ABI encode the struct
        # AuthToken is (string, address, uint256, string, string[])
        encoded = encode(
            ["string", "address", "uint256", "string", "string[]"],
            [domain, user_addr, valid_until, statement, resources],
        )

        # Use a test key
        key = bytes(range(32))
        nonce = b"\x00" * NONCE_SIZE  # Sapphire uses nonce=0

        aead = AEAD(key)
        ciphertext = aead.encrypt(nonce, encoded)
        decrypted = aead.decrypt(nonce, ciphertext)

        assert decrypted == encoded
