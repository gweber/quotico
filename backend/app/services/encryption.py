"""Field-level encryption using Fernet (AES-128-CBC) with key versioning.

The ENCRYPTION_KEY lives only in memory (env var). Even if the database
is compromised, 2FA secrets are encrypted and useless without the key.
"""

from cryptography.fernet import Fernet

from app.config import settings

# Key registry: version -> Fernet instance
# For key rotation: set ENCRYPTION_KEY to the new key, ENCRYPTION_KEY_OLD to the
# previous one.  Once all users with 2FA have logged in (lazy re-encryption),
# remove ENCRYPTION_KEY_OLD from .env.
if settings.ENCRYPTION_KEY_OLD:
    CURRENT_KEY_VERSION = 2
    _key_registry: dict[int, Fernet] = {
        1: Fernet(settings.ENCRYPTION_KEY_OLD.encode()),
        2: Fernet(settings.ENCRYPTION_KEY.encode()),
    }
else:
    CURRENT_KEY_VERSION = 1
    _key_registry: dict[int, Fernet] = {
        1: Fernet(settings.ENCRYPTION_KEY.encode()),
    }


def encrypt(plaintext: str, key_version: int = CURRENT_KEY_VERSION) -> str:
    """Encrypt a string and return base64 ciphertext."""
    f = _key_registry.get(key_version)
    if not f:
        raise ValueError(f"Unknown key version: {key_version}")
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str, key_version: int = CURRENT_KEY_VERSION) -> str:
    """Decrypt base64 ciphertext and return plaintext."""
    f = _key_registry.get(key_version)
    if not f:
        raise ValueError(f"Unknown key version: {key_version}")
    return f.decrypt(ciphertext.encode()).decode()


def needs_reencryption(stored_version: int) -> bool:
    """Check if a secret needs re-encryption with the current key."""
    return stored_version != CURRENT_KEY_VERSION


def reencrypt(ciphertext: str, old_version: int) -> tuple[str, int]:
    """Re-encrypt a secret from an old key to the current key.

    Returns (new_ciphertext, new_version).
    """
    plaintext = decrypt(ciphertext, key_version=old_version)
    new_ciphertext = encrypt(plaintext, key_version=CURRENT_KEY_VERSION)
    return new_ciphertext, CURRENT_KEY_VERSION
