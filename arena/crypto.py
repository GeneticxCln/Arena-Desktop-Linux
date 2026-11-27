"""At-rest protection for Arena session cookies on Linux.

Stored form is explicit::

    ARENA-SECRET-v1:<base64(12-byte nonce + AES-256-GCM ciphertext)>

Backed by a 32-byte master key kept in the Linux Secret Service keyring
(GNOME Keyring / KDE KWallet via ``secret-tool``).  In headless environments
without a keyring daemon, values fall back to plaintext guarded by owner-only
(``0600``) file permissions.  Protected values (including legacy Windows DPAPI
values from older backups) always fail closed when they cannot be decrypted.
"""

from __future__ import annotations

import base64
import binascii
import os
import shutil
import subprocess
import sys
from typing import Optional

PREFIX = "ARENA-DPAPI-v1:"
LINUX_PREFIX = "ARENA-SECRET-v1:"
LINUX_KEYRING_ENABLED = sys.platform.startswith("linux")

_SECRET_SERVICE = "arena-desktop"
_SECRET_ACCOUNT = "master-key"
_LINUX_KEY_CACHE: Optional[bytes] = None
_LINUX_KEYRING_CHECKED = False
_LAST_LOOKUP_STATUS = "missing"


def _secret_tool_lookup_status() -> tuple[Optional[bytes], str]:
    """Return ``(key_bytes, status)`` where status is ``'ok'``, ``'missing'``, or ``'error'``."""
    tool = shutil.which("secret-tool")
    if not tool:
        return None, "error"
    try:
        proc = subprocess.run(
            [tool, "lookup", "service", _SECRET_SERVICE, "account", _SECRET_ACCOUNT],
            capture_output=True, text=True, timeout=4,
        )
    except Exception:
        return None, "error"
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            raw = base64.b64decode(proc.stdout.strip(), validate=True)
            return (raw, "ok") if len(raw) == 32 else (None, "error")
        except (ValueError, binascii.Error):
            return None, "error"
    # secret-tool exits 1 with empty stderr when the entry genuinely does not exist;
    # non-empty stderr (e.g. locked keyring / D-Bus failure) must NOT trigger key rotation!
    if proc.returncode == 1 and not (proc.stderr or "").strip():
        return None, "missing"
    return None, "error"


def _secret_tool_lookup() -> Optional[bytes]:
    global _LAST_LOOKUP_STATUS
    key, status = _secret_tool_lookup_status()
    _LAST_LOOKUP_STATUS = status
    return key


def _secret_tool_store(key: bytes) -> bool:
    tool = shutil.which("secret-tool")
    if not tool:
        return False
    encoded = base64.b64encode(key).decode("ascii")
    try:
        proc = subprocess.run(
            [tool, "store", "--label", "Arena Desktop Master Key",
             "service", _SECRET_SERVICE, "account", _SECRET_ACCOUNT],
            input=encoded, capture_output=True, text=True, timeout=5,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _linux_master_key(create: bool = False) -> Optional[bytes]:
    """Return the 32-byte AES-256 master key from Linux Secret Service, or None."""
    global _LINUX_KEY_CACHE, _LINUX_KEYRING_CHECKED, _LAST_LOOKUP_STATUS
    if not LINUX_KEYRING_ENABLED:
        return None
    if _LINUX_KEY_CACHE is not None:
        return _LINUX_KEY_CACHE
    _LAST_LOOKUP_STATUS = "missing"
    existing = _secret_tool_lookup()
    if existing is not None:
        _LINUX_KEY_CACHE = existing
        _LINUX_KEYRING_CHECKED = True
        return existing
    # Never overwrite an existing key when the real keyring timed out or is locked!
    if _LAST_LOOKUP_STATUS == "error" or not create or _LINUX_KEYRING_CHECKED:
        return None
    _LINUX_KEYRING_CHECKED = True
    candidate = os.urandom(32)
    if _secret_tool_store(candidate):
        _LINUX_KEY_CACHE = candidate
        return candidate
    return None


def _linux_protect(data: bytes) -> Optional[bytes]:
    key = _linux_master_key(create=True)
    if key is None:
        return None
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, data, None)


def _linux_unprotect(data: bytes) -> bytes:
    if len(data) < 12 + 16:
        raise ValueError("invalid Linux secret ciphertext")
    key = _linux_master_key(create=False)
    if key is None:
        raise OSError("Linux Secret Service key is unavailable")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key).decrypt(data[:12], data[12:], None)


def protection_available() -> bool:
    """True if Linux Secret Service keyring encryption is currently active."""
    return bool(LINUX_KEYRING_ENABLED and _linux_master_key(create=True) is not None)


def is_protected(value: object) -> bool:
    return isinstance(value, str) and (value.startswith(LINUX_PREFIX) or value.startswith(PREFIX))


def protect_text(value: str) -> str:
    """Return an encrypted ``ARENA-SECRET-v1:`` value, or plaintext if no keyring is active."""
    text = str(value or "")
    if not text or is_protected(text):
        return text
    if LINUX_KEYRING_ENABLED:
        encrypted = _linux_protect(text.encode("utf-8"))
        if encrypted is not None:
            return LINUX_PREFIX + base64.b64encode(encrypted).decode("ascii")
    return text


def unprotect_text(value: str) -> str:
    """Decode a protected value; legacy plaintext is returned unchanged.

    Legacy Windows DPAPI blobs fail closed so ciphertext is never sent to Arena.
    """
    text = str(value or "")
    if text.startswith(LINUX_PREFIX):
        try:
            encrypted = base64.b64decode(text[len(LINUX_PREFIX):], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid Linux secret value") from exc
        return _linux_unprotect(encrypted).decode("utf-8")
    if text.startswith(PREFIX):
        raise OSError("Windows DPAPI values cannot be decrypted on Linux")
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error):
        return text
    if raw.startswith(b"DPAPI:"):
        raise OSError("Windows DPAPI values cannot be decrypted on Linux")
    return text


def decode_cookie(value: object) -> str:
    """Best-effort cookie decoding that fails closed for unreadable protected data."""
    text = str(value or "")
    try:
        return unprotect_text(text)
    except Exception:
        return ""


def encrypt(data: bytes) -> bytes:
    return protect_text(data.decode("utf-8")).encode("utf-8")


def decrypt(data: bytes) -> bytes:
    return unprotect_text(data.decode("utf-8")).encode("utf-8")
