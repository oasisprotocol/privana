"""Tests for token store."""

import concurrent.futures
import time

import jwt

import src.auth.jwt_keys as jwt_keys
import src.auth.jwt_service as jwt_service
import src.auth.token_store as token_store_module
from src.auth.token_store import TokenStore

TEST_ADDRESS = "0x0000000000000000000000000000000000000001"


class TestTokenStorePersistence:
    """Tests for token store disk persistence."""

    def test_persists_to_disk(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that token store persists data to disk."""
        storage_dir = tmp_path / "token_store_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))

        token_store_module._token_store_instance = None
        store = TokenStore()

        store.store_refresh_token("test-jti", TEST_ADDRESS, 9999999999.0)

        # diskcache creates a directory with cache.db
        assert storage_dir.exists()

        # Close and reset singleton, create new instance
        store.close()
        token_store_module._token_store_instance = None
        store2 = TokenStore()

        result = store2.get_refresh_token("test-jti")
        assert result is not None
        assert result[0] == TEST_ADDRESS
        store2.close()


class TestAtomicOperations:
    """Tests for atomic token operations."""

    def test_consume_is_atomic(self, reset_auth_singletons, disable_rofl_keys):
        """Test that refresh token consumption is atomic (TOCTOU protection)."""
        from src.auth.jwt_service import JWTService

        jwt_svc = JWTService()
        refresh_token = jwt_svc.create_refresh_token(TEST_ADDRESS)

        payload = jwt.decode(refresh_token, options={"verify_signature": False})
        jti = payload["jti"]

        store = TokenStore()
        address = store.consume_refresh_token(jti)
        assert address == TEST_ADDRESS

        # Second consumption should fail
        assert store.consume_refresh_token(jti) is None

    def test_concurrent_consumption_only_one_succeeds(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that concurrent refresh token consumption only allows one to succeed.

        This verifies the fix for the TOCTOU race condition where two concurrent
        refresh requests could both use the same refresh token.
        """
        storage_dir = tmp_path / "concurrent_refresh_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        token_store_module._token_store_instance = None
        jwt_keys._jwt_key_manager_instance = None
        jwt_service._jwt_service_instance = None

        store = TokenStore()
        test_jti = "concurrent-test-jti"
        test_address = "0x1234567890123456789012345678901234567890"

        store.store_refresh_token(test_jti, test_address, 9999999999.0)

        results = []

        def try_consume():
            result = store.consume_refresh_token(test_jti)
            results.append(result)
            return result

        num_threads = 10
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(try_consume) for _ in range(num_threads)]
            concurrent.futures.wait(futures)

        success_count = sum(1 for r in results if r == test_address)
        none_count = sum(1 for r in results if r is None)

        assert success_count == 1, f"Expected 1 success, got {success_count}"
        assert none_count == num_threads - 1, f"Expected {num_threads - 1} failures"


class TestNonceOperations:
    """Tests for SIWE nonce operations (API-level replay protection)."""

    def test_generate_and_consume_nonce(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test basic nonce generation and consumption."""
        storage_dir = tmp_path / "nonce_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        token_store_module._token_store_instance = None

        store = TokenStore()
        nonce = store.generate_nonce()

        assert nonce is not None
        assert len(nonce) > 0

        # Nonce should be valid
        assert store.is_nonce_valid(nonce) is True

        # Consume the nonce
        assert store.consume_nonce(nonce) is True

        # Nonce should no longer be valid after consumption
        assert store.is_nonce_valid(nonce) is False
        assert store.consume_nonce(nonce) is False

    def test_nonce_replay_rejected(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that nonce replay (second use) is rejected."""
        storage_dir = tmp_path / "nonce_replay_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        token_store_module._token_store_instance = None

        store = TokenStore()
        nonce = store.generate_nonce()

        # First use should succeed
        assert store.consume_nonce(nonce) is True

        # Second use (replay) should fail
        assert store.consume_nonce(nonce) is False

    def test_nonce_expiration(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that expired nonces are rejected."""
        storage_dir = tmp_path / "nonce_expiry_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        # Set very short expiry for testing
        monkeypatch.setenv("SIWE_NONCE_EXPIRY_SECONDS", "1")
        token_store_module._token_store_instance = None

        store = TokenStore()
        nonce = store.generate_nonce()

        # Nonce should be valid immediately
        assert store.is_nonce_valid(nonce) is True

        # Wait for expiration
        time.sleep(1.5)

        # Nonce should now be invalid
        assert store.is_nonce_valid(nonce) is False
        assert store.consume_nonce(nonce) is False

    def test_same_address_gets_same_nonce(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that same address gets existing nonce instead of new one."""
        storage_dir = tmp_path / "nonce_client_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        token_store_module._token_store_instance = None

        store = TokenStore()
        address1 = "0x1234567890123456789012345678901234567890"

        nonce1 = store.generate_nonce(client_id=address1)
        nonce2 = store.generate_nonce(client_id=address1)

        # Same address should get same nonce
        assert nonce1 == nonce2

        # Different address should get different nonce
        address2 = "0x0987654321098765432109876543210987654321"
        nonce3 = store.generate_nonce(client_id=address2)
        assert nonce3 != nonce1

    def test_address_gets_new_nonce_after_consumption(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that address gets new nonce after previous one is consumed."""
        storage_dir = tmp_path / "nonce_consumed_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        token_store_module._token_store_instance = None

        store = TokenStore()
        address = "0x1234567890123456789012345678901234567890"

        nonce1 = store.generate_nonce(client_id=address)
        assert store.consume_nonce(nonce1) is True

        # After consumption, address should get a new nonce
        nonce2 = store.generate_nonce(client_id=address)
        assert nonce2 != nonce1

    def test_concurrent_nonce_consumption_only_one_succeeds(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that concurrent nonce consumption only allows one to succeed.

        This verifies the atomic nonce consumption for replay protection.
        """
        storage_dir = tmp_path / "concurrent_nonce_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        token_store_module._token_store_instance = None

        store = TokenStore()
        nonce = store.generate_nonce()

        results = []

        def try_consume():
            result = store.consume_nonce(nonce)
            results.append(result)
            return result

        num_threads = 10
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(try_consume) for _ in range(num_threads)]
            concurrent.futures.wait(futures)

        success_count = sum(1 for r in results if r is True)
        failure_count = sum(1 for r in results if r is False)

        assert success_count == 1, f"Expected 1 success, got {success_count}"
        assert failure_count == num_threads - 1, f"Expected {num_threads - 1} failures"

    def test_nonce_persists_to_disk(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that nonces persist to disk across store instances."""
        storage_dir = tmp_path / "nonce_persist_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        token_store_module._token_store_instance = None

        store = TokenStore()
        nonce = store.generate_nonce()

        # Verify storage directory exists (diskcache creates it)
        assert storage_dir.exists()

        # Close and reset singleton, create new instance
        store.close()
        token_store_module._token_store_instance = None
        store2 = TokenStore()

        # Nonce should still be valid
        assert store2.is_nonce_valid(nonce) is True
        assert store2.consume_nonce(nonce) is True
        store2.close()
