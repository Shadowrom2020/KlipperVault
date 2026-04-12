#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cross-platform secret storage with keyring-first and SQLite fallback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import platform

from klipper_vault_remote_profiles import (
    clear_fallback_secret,
    get_credential_backend,
    get_fallback_secret,
    set_credential_backend,
    set_fallback_secret,
)

_BACKEND_OS_KEYRING = "os_keyring"
_BACKEND_DB_FALLBACK = "db_fallback"


def _detect_os_keyring() -> tuple[bool, str]:
    """Detect whether a usable keyring backend exists in this runtime."""
    try:
        import keyring  # type: ignore[import-not-found]
    except Exception:
        return False, "keyring package unavailable"

    try:
        backend = keyring.get_keyring()
        backend_name = f"{backend.__class__.__module__}.{backend.__class__.__name__}"
    except Exception as exc:
        return False, f"keyring backend load failed: {exc}"

    # keyring's fail backend means no system credential store is configured.
    if "fail" in backend_name.lower():
        return False, f"keyring backend unavailable: {backend_name}"

    return True, backend_name


@dataclass
class SecretBackendStatus:
    """Diagnostic info for selected secret backend."""

    keyring_available: bool
    keyring_backend: str
    platform_name: str


class CredentialStore:
    """Persist and retrieve secrets using OS keyring or DB fallback."""

    def __init__(self, db_path: Path, *, service_name: str = "KlipperVault") -> None:
        self._db_path = db_path
        self._service_name = service_name
        keyring_available, keyring_backend = _detect_os_keyring()
        self._status = SecretBackendStatus(
            keyring_available=keyring_available,
            keyring_backend=keyring_backend,
            platform_name=platform.system().lower(),
        )

    @property
    def status(self) -> SecretBackendStatus:
        """Return backend detection information."""
        return self._status

    def set_secret(self, *, credential_ref: str, secret_type: str, secret_value: str) -> str:
        """Save secret and return storage backend name."""
        ref = credential_ref.strip()
        if not ref:
            raise ValueError("credential_ref must not be empty")
        secret_type = secret_type.strip().lower()
        if not secret_type:
            raise ValueError("secret_type must not be empty")

        if self._status.keyring_available:
            try:
                import keyring  # type: ignore[import-not-found]

                keyring.set_password(self._service_name, ref, secret_value)
                set_credential_backend(
                    self._db_path,
                    credential_ref=ref,
                    secret_type=secret_type,
                    backend=_BACKEND_OS_KEYRING,
                )
                # Clean up stale fallback records after successful keyring write.
                clear_fallback_secret(self._db_path, ref)
                return _BACKEND_OS_KEYRING
            except Exception:
                # Fall through to DB fallback when keyring is not operational.
                pass

        set_credential_backend(
            self._db_path,
            credential_ref=ref,
            secret_type=secret_type,
            backend=_BACKEND_DB_FALLBACK,
        )
        set_fallback_secret(
            self._db_path,
            credential_ref=ref,
            secret_value=secret_value,
        )
        return _BACKEND_DB_FALLBACK

    def get_secret(self, *, credential_ref: str) -> str | None:
        """Load secret by reference using recorded backend metadata."""
        ref = credential_ref.strip()
        if not ref:
            return None

        backend = get_credential_backend(self._db_path, ref)
        if backend == _BACKEND_OS_KEYRING and self._status.keyring_available:
            try:
                import keyring  # type: ignore[import-not-found]

                value = keyring.get_password(self._service_name, ref)
                if value:
                    return value
            except Exception:
                # Fallback read for migration/recovery scenarios.
                pass

        return get_fallback_secret(self._db_path, ref)

    def delete_secret(self, *, credential_ref: str) -> None:
        """Delete secret from available backends for one credential reference."""
        ref = credential_ref.strip()
        if not ref:
            return

        if self._status.keyring_available:
            try:
                import keyring  # type: ignore[import-not-found]

                keyring.delete_password(self._service_name, ref)
            except Exception:
                # Ignore keyring deletion failures and continue with DB fallback cleanup.
                pass

        clear_fallback_secret(self._db_path, ref)
