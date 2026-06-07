from cryptography.fernet import Fernet
import base64
import hashlib

_fernet = None

def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        from portal.config import settings
        # Derive a 32-byte urlsafe base64 string from the secret key
        key = hashlib.sha256(settings.secret_key.encode()).digest()
        _fernet = Fernet(base64.urlsafe_b64encode(key))
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
    except Exception:
        return None  # Fail securely if decryption is invalid
