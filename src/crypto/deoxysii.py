"""Deoxys-II-256-128 AEAD wrapper around sapphirepy.

This module provides an AEAD interface wrapping the official
sapphirepy DeoxysII implementation used by Sapphire contracts.

Key: 32 bytes
Nonce: 15 bytes
Tag: 16 bytes (appended to ciphertext)
"""

from typing import Optional

from sapphirepy.deoxysii import (
    KEY_SIZE,
    NONCE_SIZE,
    TAG_SIZE,
    DeoxysII,
)


class DecryptionError(Exception):
    """Raised when decryption fails (tag verification failure)."""

    pass


class AEAD:
    """Deoxys-II-256-128 AEAD implementation.

    Provides authenticated encryption with associated data using the
    Deoxys-II algorithm, which is the encryption scheme used by Sapphire.
    """

    def __init__(self, key: bytes):
        """Initialize with a 32-byte key.

        Args:
            key: 32-byte encryption key.

        Raises:
            ValueError: If key is not 32 bytes.
        """
        if len(key) != KEY_SIZE:
            raise ValueError(f"Invalid key size: expected {KEY_SIZE}, got {len(key)}")

        self._deoxys = DeoxysII(key)

    def encrypt(
        self,
        nonce: bytes,
        plaintext: Optional[bytes] = None,
        associated_data: Optional[bytes] = None,
    ) -> bytes:
        """Encrypt plaintext with optional associated data.

        Args:
            nonce: 15-byte nonce. Must be unique for each encryption with the same key.
            plaintext: Data to encrypt. Can be None or empty.
            associated_data: Additional authenticated data. Can be None or empty.

        Returns:
            Ciphertext with 16-byte tag appended.

        Raises:
            ValueError: If nonce is not 15 bytes.
        """
        if len(nonce) != NONCE_SIZE:
            raise ValueError(f"Invalid nonce size: expected {NONCE_SIZE}, got {len(nonce)}")

        if plaintext is None:
            plaintext = b""

        dst = bytearray(len(plaintext) + TAG_SIZE)
        self._deoxys.encrypt(nonce, dst, associated_data, plaintext)

        return bytes(dst)

    def decrypt(
        self, nonce: bytes, ciphertext: bytes, associated_data: Optional[bytes] = None
    ) -> bytes:
        """Decrypt ciphertext with optional associated data.

        Args:
            nonce: 15-byte nonce used during encryption.
            ciphertext: Encrypted data with 16-byte tag appended.
            associated_data: Additional authenticated data used during encryption.

        Returns:
            Decrypted plaintext.

        Raises:
            ValueError: If nonce is not 15 bytes.
            DecryptionError: If tag verification fails.
        """
        if len(nonce) != NONCE_SIZE:
            raise ValueError(f"Invalid nonce size: expected {NONCE_SIZE}, got {len(nonce)}")

        if len(ciphertext) < TAG_SIZE:
            raise DecryptionError("Ciphertext too short")

        dst = bytearray(len(ciphertext) - TAG_SIZE)
        if not self._deoxys.decrypt(nonce, dst, associated_data, ciphertext):
            # Zero out plaintext on failure
            for i in range(len(dst)):
                dst[i] = 0
            raise DecryptionError("Message authentication failure")

        return bytes(dst)
