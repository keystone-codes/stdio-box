"""Key derivation and AES-256-GCM sealing for profile secrets."""

import hashlib
import hmac
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from stdio_box.store.errors import SecretUnreadable

INFO_PROFILE_SECRETS = "stdio-box/profile-secrets/v1"
INFO_KEY_CHECK = "stdio-box/key-check/v1"
INFO_FORM_CSRF = "stdio-box/form-csrf/v1"
INFO_AUDIT_SUB = "stdio-box/audit-sub/v1"

_KEY_CHECK_MESSAGE = b"stdio-box key check"
_NONCE_BYTES = 12
_DERIVED_BYTES = 32


def derive(store_key: str, salt: bytes, info: str) -> bytes:
    """HKDF-SHA256, 32 bytes. Different `info` values yield different keys."""
    return HKDF(
        algorithm=SHA256(),
        length=_DERIVED_BYTES,
        salt=salt,
        info=info.encode("utf-8"),
    ).derive(store_key.encode("utf-8"))


def seal(key: bytes, profile_id: str, version: int, values: dict) -> tuple[bytes, bytes]:
    """Encrypt `values`. The nonce is 12 random bytes. AAD binds id and version."""
    nonce = os.urandom(_NONCE_BYTES)
    aad = _aad(profile_id, version)
    plaintext = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return nonce, ciphertext


def open_(key: bytes, profile_id: str, version: int, nonce: bytes, ciphertext: bytes) -> dict:
    """Decrypt a secret row. A wrong key, nonce, or AAD raises SecretUnreadable."""
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _aad(profile_id, version))
    except InvalidTag as exc:
        raise SecretUnreadable from exc
    loaded = json.loads(plaintext.decode("utf-8"))
    if not isinstance(loaded, dict):
        raise SecretUnreadable
    return loaded


def key_check(store_key: str, salt: bytes) -> str:
    """Hex HMAC-SHA256 of the key-check subkey over the fixed label."""
    subkey = derive(store_key, salt, INFO_KEY_CHECK)
    return hmac.new(subkey, _KEY_CHECK_MESSAGE, hashlib.sha256).hexdigest()


def key_id(check_hex: str) -> str:
    """First 8 characters of a key_check hex digest."""
    return check_hex[:8]


def sub_hash(audit_key: bytes, sub: str) -> str:
    """First 16 hex characters of HMAC-SHA256(audit subkey, sub)."""
    return hmac.new(audit_key, sub.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def _aad(profile_id: str, version: int) -> bytes:
    return f"{profile_id}|{version}".encode("utf-8")
