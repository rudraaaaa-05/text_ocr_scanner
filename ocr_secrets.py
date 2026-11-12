from __future__ import annotations

import logging
import os
import sys
from typing import Optional

SERVICE = "ocr-scanner"
DISABLE_ENV = "OCR_SCANNER_KEYRING"   # set to "off" to skip the OS store (tests, headless boxes)
WINDOWS_BLOB_LIMIT = 2560            # CRED_MAX_CREDENTIAL_BLOB_SIZE in bytes; keyring stores UTF-16
log = logging.getLogger("ocr_scanner")


class SecretStoreError(Exception):
    """A readable, secret-free description of why the credential store could not be used."""


def _scrub(text: str, secret: str = "") -> str:
    """Remove the secret (and each comma-separated part of it) from an error text."""
    text = str(text)
    parts = [secret] + [p.strip() for p in secret.replace("\n", ",").split(",")] if secret else []
    for part in sorted({p for p in parts if len(p) >= 4}, key=len, reverse=True):
        text = text.replace(part, "***")
    return " ".join(text.split())[:200]


def _describe(exc: Exception, secret: str = "") -> str:
    detail = _scrub(exc, secret)
    return f"{exc.__class__.__name__}: {detail}" if detail else exc.__class__.__name__


class SecretStore:
    """Thin wrapper over the keyring API. `backend` is anything with get_password / set_password /
    delete_password (the `keyring` module itself, or a fake in tests)."""

    def __init__(self, backend=None):
        self._backend = backend
        self._checked = backend is not None   # an injected backend is trusted and never re-discovered
        self._problem: Optional[str] = None

    # availability

    def problem(self) -> Optional[str]:
        """None when the store is usable, otherwise why not. A working store is remembered; a missing one is
        looked for again on the next call, so a keyring started later is picked up on the next settings reload."""
        if self._checked:
            return self._problem
        why = self._discover()
        if why is None:
            self._checked, self._problem = True, None
        return why

    def _discover(self) -> Optional[str]:
        if os.environ.get(DISABLE_ENV, "").strip().lower() in ("off", "0", "false", "no"):
            return f"disabled by the {DISABLE_ENV} environment variable"
        try:
            import keyring
        except ImportError:
            return "the 'keyring' package is not installed (pip install keyring)"
        try:
            kr = keyring.get_keyring()
        except Exception as exc:  # backend discovery can fail in many platform specific ways
            return f"no credential store could be opened ({_describe(exc)})"
        module = (kr.__class__.__module__ or "").lower()
        if module.startswith(("keyring.backends.fail", "keyring.backends.null")) or module.startswith("keyrings.alt"):
            return ("no secure credential store was found "
                    "(on Linux, run a Secret Service provider such as GNOME Keyring, KWallet or KeePassXC)")
        log.info("Credential store: %s", kr.__class__.__name__)
        self._backend = keyring
        return None

    @property
    def available(self) -> bool:
        return self.problem() is None

    @staticmethod
    def _user(provider_id: str) -> str:
        return f"api-keys:{provider_id}"

    def _need(self):
        why = self.problem()
        if why:
            raise SecretStoreError(f"Secure key storage is unavailable: {why}.")
        return self._backend

    # operations

    def get(self, provider_id: str) -> Optional[str]:
        backend = self._need()
        try:
            return backend.get_password(SERVICE, self._user(provider_id))
        except Exception as exc:  # keyring raises backend specific errors (locked, dismissed prompt, D-Bus down)
            raise SecretStoreError(f"Could not read the saved key for '{provider_id}' ({_describe(exc)}).") from None

    def set(self, provider_id: str, value: str) -> None:
        backend = self._need()
        if sys.platform.startswith("win") and len(value.encode("utf-16-le")) > WINDOWS_BLOB_LIMIT:
            raise SecretStoreError(f"The keys for '{provider_id}' are too long for Windows Credential Manager "
                                   f"(limit about {WINDOWS_BLOB_LIMIT // 2} characters). Use fewer keys.")
        try:
            backend.set_password(SERVICE, self._user(provider_id), value)
        except Exception as exc:
            raise SecretStoreError(f"Could not store the key for '{provider_id}' ({_describe(exc, value)}).") from None

    def delete(self, provider_id: str) -> None:
        """Missing entries are not an error."""
        backend = self._need()
        try:
            backend.delete_password(SERVICE, self._user(provider_id))
        except Exception as exc:
            if exc.__class__.__name__ == "PasswordDeleteError":  # keyring's "nothing to delete"
                return
            raise SecretStoreError(f"Could not delete the key for '{provider_id}' ({_describe(exc)}).") from None

    def set_verified(self, provider_id: str, value: str) -> None:
        """Store and read back. Raises unless the stored value is identical."""
        self.set(provider_id, value)
        if self.get(provider_id) != value:
            raise SecretStoreError(f"The key for '{provider_id}' could not be read back after saving it.")


_default: Optional[SecretStore] = None


def default_store() -> SecretStore:
    global _default
    if _default is None:
        _default = SecretStore()
    return _default
