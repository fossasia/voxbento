import base64
import hashlib
import logging

from cryptography.fernet import Fernet, MultiFernet

logger = logging.getLogger(__name__)

_fernet = None


def get_fernet() -> MultiFernet:
    global _fernet
    if _fernet is None:
        # Local import required to avoid circular dependency
        from portal.config import settings

        keys_str = settings.api_key_encryption_key
        if not keys_str or keys_str == "change-this-encryption-key-in-production":
            raise RuntimeError("API_KEY_ENCRYPTION_KEY must be set securely and changed from the default value.")

        fernets = []
        for key_str in keys_str.split(","):
            key_str = key_str.strip()
            if not key_str:
                continue
            if len(key_str) < 32:
                raise RuntimeError("Each encryption key must be at least 32 characters long.")

            # Derive a 32-byte urlsafe base64 string from the encryption key
            key = hashlib.sha256(key_str.encode()).digest()
            fernets.append(Fernet(base64.urlsafe_b64encode(key)))

        if not fernets:
            raise RuntimeError("No valid encryption keys found.")

        _fernet = MultiFernet(fernets)
    return _fernet


def encrypt_val(val: str | None) -> str | None:
    if not val:
        return None
    return get_fernet().encrypt(val.encode()).decode()


def decrypt_val(val: str | None) -> str | None:
    if not val:
        return None
    try:
        return get_fernet().decrypt(val.encode()).decode()
    except Exception as e:
        logger.exception("Failed to decrypt API key.")
        raise ValueError(
            "Failed to decrypt API key. This may indicate a corrupted database entry or an incorrect API_KEY_ENCRYPTION_KEY."
        ) from e
