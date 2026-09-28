import os
from unittest.mock import patch

import pytest

import portal.crypto
from portal.crypto import decrypt_val, encrypt_val


@pytest.fixture(autouse=True)
def reset_fernet():
    # Reset the cached _fernet before each test
    portal.crypto._fernet = None
    yield
    portal.crypto._fernet = None


def test_single_key_encryption():
    with patch("portal.config.settings.api_key_encryption_key", "my-super-secret-encryption-key-32-chars!"):
        original = "sk-test-123"
        encrypted = encrypt_val(original)
        assert encrypted != original
        assert encrypted is not None

        decrypted = decrypt_val(encrypted)
        assert decrypted == original


def test_multi_key_rotation():
    key1 = "old-encryption-key-that-is-at-least-32-chars"
    key2 = "new-encryption-key-that-is-also-at-least-32-c"

    # Encrypt with key1
    with patch("portal.config.settings.api_key_encryption_key", key1):
        portal.crypto._fernet = None
        encrypted_old = encrypt_val("sk-old-key")

    # Rotate: Add key2 to front. The new active encryption key is key2.
    # It should still decrypt the old key.
    with patch("portal.config.settings.api_key_encryption_key", f"{key2},{key1}"):
        portal.crypto._fernet = None

        # Verify it can decrypt the old token
        assert decrypt_val(encrypted_old) == "sk-old-key"

        # Verify it encrypts with the new key (key2)
        encrypted_new = encrypt_val("sk-new-key")

        # New tokens should be decryptable
        assert decrypt_val(encrypted_new) == "sk-new-key"

    # Remove key1 (completed rotation). It should still decrypt the new token.
    with patch("portal.config.settings.api_key_encryption_key", key2):
        portal.crypto._fernet = None
        assert decrypt_val(encrypted_new) == "sk-new-key"

        # It should fail to decrypt the old token since key1 is gone
        with pytest.raises(ValueError, match="Failed to decrypt API key"):
            decrypt_val(encrypted_old)


def test_empty_keys_str_raises():
    with patch("portal.config.settings.api_key_encryption_key", ""):
        with pytest.raises(RuntimeError, match="API_KEY_ENCRYPTION_KEY must be set securely"):
            portal.crypto.get_fernet()


def test_default_keys_str_raises():
    with patch("portal.config.settings.api_key_encryption_key", "change-this-encryption-key-in-production"):
        with pytest.raises(RuntimeError, match="API_KEY_ENCRYPTION_KEY must be set securely"):
            portal.crypto.get_fernet()


def test_short_key_raises():
    with patch("portal.config.settings.api_key_encryption_key", "short-key"):
        with pytest.raises(RuntimeError, match="must be at least 32 characters long"):
            portal.crypto.get_fernet()


def test_empty_parts_are_skipped():
    # Empty parts like "key1,,key2" should be skipped. If no valid keys are found, it raises RuntimeError.
    with patch("portal.config.settings.api_key_encryption_key", " , ,,  "):
        with pytest.raises(RuntimeError, match="No valid encryption keys found"):
            portal.crypto.get_fernet()


def test_encrypt_val_none_or_empty():
    assert encrypt_val(None) is None
    assert encrypt_val("") is None


def test_decrypt_val_none_or_empty():
    assert decrypt_val(None) is None
    assert decrypt_val("") is None
