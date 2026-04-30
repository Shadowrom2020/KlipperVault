"""Pytest configuration and shared fixtures.

Forces keyring to use the null (no-op) backend during tests so that tests
never block on D-Bus / GNOME Keyring / macOS Keychain.  This must happen
before any module that imports ``keyring`` is loaded, which is why it sits at
module level rather than inside a fixture.
"""

import os

# Tell the keyring library to skip OS integration entirely.  Without this,
# tests that create a CredentialStore hang for several seconds per call while
# waiting for a GNOME Keyring / D-Bus SecretService authentication prompt.
os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")

import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def _stub_printer_identity_detection():
    """Stub out SSH-based printer identity detection to prevent timeouts.

    ``_try_detect_printer_identity`` tries to SSH into the configured printer
    host (e.g. 'remote.local') to read freedi.cfg / printer.cfg.  In tests
    that host doesn't exist and each TCP connect attempt takes ~15 s before
    timing out.  Patching the method to a no-op keeps tests fast.
    """
    with patch(
        "klipper_macro_service_profiles.PrinterProfileMixin._try_detect_printer_identity",
    ):
        yield
