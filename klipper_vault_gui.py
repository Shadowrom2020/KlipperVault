#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Primary GUI launcher for KlipperVault."""

from __future__ import annotations

import hashlib
import inspect
import os
import platform
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent

# On Windows, PyInstaller's windowed (console=False) mode sets sys.stdout and
# sys.stderr to None.  Uvicorn's DefaultFormatter calls stream.isatty() during
# logging setup, which raises AttributeError: 'NoneType' has no attribute 'isatty'.
# Redirect both streams to devnull before any library imports touch them.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: WPS515
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: WPS515


def _is_frozen_runtime() -> bool:
    """Return True when running from a packaged executable."""
    return bool(getattr(sys, "frozen", False))


def _is_server_mode() -> bool:
    """Return True when runtime is explicitly configured for server mode."""
    raw = os.environ.get("KLIPPERVAULT_SERVER_MODE", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _use_native_window() -> bool:
    """Use desktop native window only in packaged non-server local runtimes."""
    # Linux native windows require system webview libraries that are not bundled.
    if platform.system() == "Linux":
        return False
    return _is_frozen_runtime() and not _is_server_mode()


def _bundle_root() -> Path:
    """Return runtime root for packaged resources or repository root."""
    if _is_frozen_runtime():
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            return Path(meipass)
    return REPO_ROOT


SRC_DIR = REPO_ROOT / "src"
if not _is_frozen_runtime() and str(SRC_DIR) not in sys.path:
    # Keep imports stable after moving all source modules under src/.
    sys.path.insert(0, str(SRC_DIR))


def _requirements_hash(requirements_path: Path) -> str:
    """Return SHA256 hash of requirements file contents."""
    return hashlib.sha256(requirements_path.read_bytes()).hexdigest()


def _requirements_file_name() -> str:
    """Return requirements file name used for runtime dependency sync."""
    return os.environ.get("KLIPPERVAULT_REQUIREMENTS_FILE", "requirements.txt").strip() or "requirements.txt"


def _requirements_path() -> Path:
    """Return resolved requirements path from environment or default."""
    configured = Path(_requirements_file_name())
    if configured.is_absolute():
        return configured
    return REPO_ROOT / configured


def _venv_requirements_stamp_path() -> Path:
    """Return per-venv stamp file used to skip redundant pip installs."""
    # Keep symlink path intact so venv python wrappers like
    # ~/.venv/bin/python -> /usr/bin/python still map back to ~/.venv.
    stamp_name = f".klippervault_{Path(_requirements_file_name()).name}.sha256"
    python_path = Path(sys.executable)
    if python_path.parent.name == "bin" and (python_path.parent.parent / "pyvenv.cfg").exists():
        return python_path.parent.parent / stamp_name
    return REPO_ROOT / stamp_name


def _auto_update_venv_enabled() -> bool:
    """Return True when startup venv sync is enabled."""
    raw = os.environ.get("KLIPPERVAULT_AUTO_UPDATE_VENV", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _log_venv_sync(message: str) -> None:
    """Emit a startup log line for venv sync decisions."""
    print(f"[KlipperVault] venv-sync: {message}", flush=True)


def _sync_venv_requirements_if_needed() -> None:
    """Install requirements into active venv when requirements.txt changed."""
    if _is_frozen_runtime():
        _log_venv_sync("skipping in packaged runtime")
        return

    if not _auto_update_venv_enabled():
        _log_venv_sync("disabled via KLIPPERVAULT_AUTO_UPDATE_VENV")
        return

    requirements_path = _requirements_path()
    if not requirements_path.exists() or not requirements_path.is_file():
        _log_venv_sync(f"{requirements_path} not found; skipping")
        return

    required_hash = _requirements_hash(requirements_path)
    stamp_path = _venv_requirements_stamp_path()

    try:
        installed_hash = stamp_path.read_text(encoding="utf-8").strip()
    except OSError:
        installed_hash = ""

    if installed_hash == required_hash:
        _log_venv_sync("requirements unchanged; skipping")
        return

    _log_venv_sync(f"requirements changed; running pip install for {requirements_path.name}")
    subprocess.run(  # nosec B603
        [sys.executable, "-m", "pip", "install", "-r", str(requirements_path)],
        cwd=str(REPO_ROOT),
        check=True,
    )

    stamp_path.write_text(required_hash + "\n", encoding="utf-8")
    _log_venv_sync("requirements sync completed")


def _load_app_version() -> str:
    """Read application version from VERSION file, with safe fallback."""
    version_path = _bundle_root() / "VERSION"
    try:
        version = version_path.read_text(encoding="utf-8").strip()
        return version or "unknown"
    except OSError:
        return "unknown"


def _patch_nicegui_disconnect_signature() -> None:
    """Adapt NiceGUI disconnect callback for newer socket.io argument shape."""
    try:
        from nicegui import nicegui as nicegui_runtime
    except Exception:
        return

    disconnect_handler = getattr(nicegui_runtime, "_on_disconnect", None)
    sio = getattr(nicegui_runtime, "sio", None)
    if disconnect_handler is None or sio is None:
        return

    try:
        parameter_count = len(inspect.signature(disconnect_handler).parameters)
    except (TypeError, ValueError):
        parameter_count = 0

    if parameter_count >= 2:
        return

    def _on_disconnect_compat(sid: str, *_: object) -> None:
        disconnect_handler(sid)

    nicegui_runtime._on_disconnect = _on_disconnect_compat
    sio.on("disconnect", _on_disconnect_compat)


def _patch_nicegui_deleted_parent_slot_event_race() -> None:
    """Ignore stale UI events that arrive after a client's element tree is removed."""
    try:
        from nicegui import events as nicegui_events
    except Exception:
        return

    if getattr(nicegui_events.handle_event, "__name__", "") == "_handle_event_compat":
        return

    def _handle_event_compat(handler, arguments) -> None:
        if handler is None:
            return
        try:
            parent_slot: Any = (
                arguments.sender.parent_slot or arguments.sender.client.layout.default_slot
                if isinstance(arguments, nicegui_events.UiEventArguments)
                else nicegui_events.nullcontext()
            )

            with parent_slot:
                if nicegui_events.helpers.expects_arguments(handler):
                    result = handler(arguments)
                else:
                    result = handler()

            if nicegui_events.helpers.should_await(result):
                nicegui_events.background_tasks.create_or_defer(
                    nicegui_events.helpers.await_with_context(result, parent_slot),
                    name=str(handler),
                )
        except RuntimeError as error:
            if "The parent slot of the element has been deleted." in str(error):
                return
            nicegui_events.core.app.handle_exception(error)
        except Exception as error:
            nicegui_events.core.app.handle_exception(error)

    nicegui_events.handle_event = _handle_event_compat


def _patch_nicegui_deleted_parent_slot_exception_filter() -> None:
    """Suppress only the known benign parent-slot teardown RuntimeError."""
    try:
        from nicegui import core as nicegui_core
    except Exception:
        return

    app_object = getattr(nicegui_core, "app", None)
    if app_object is None:
        return

    original_handle_exception = getattr(app_object, "handle_exception", None)
    if original_handle_exception is None:
        return

    if getattr(original_handle_exception, "__name__", "") == "_handle_exception_compat":
        return

    def _handle_exception_compat(error: Exception) -> None:
        if isinstance(error, RuntimeError) and str(error) == "The parent slot of the element has been deleted.":
            return
        original_handle_exception(error)

    app_object.handle_exception = _handle_exception_compat


def main() -> None:
    """Start the KlipperVault GUI runtime."""
    _sync_venv_requirements_if_needed()

    from klipper_macro_gui import build_ui
    from klipper_vault_config import _FIXED_WEB_UI_PORT
    from klipper_vault_config import load_or_create as _load_vault_config
    from klipper_vault_i18n import t
    from klipper_vault_paths import DEFAULT_CONFIG_DIR, DEFAULT_DB_PATH
    from nicegui import ui

    _patch_nicegui_disconnect_signature()
    _patch_nicegui_deleted_parent_slot_event_race()
    _patch_nicegui_deleted_parent_slot_exception_filter()

    # Disable NiceGUI's reloader in bundled executables to prevent
    # attempts to reload the binary executable, which causes null byte errors.
    if _is_frozen_runtime():
        os.environ["NICEGUI_AUTORELOAD"] = "False"
        os.environ["NICEGUI_RELOAD"] = "no"

    config_dir = Path(DEFAULT_CONFIG_DIR).expanduser().resolve()
    db_path = Path(DEFAULT_DB_PATH).expanduser().resolve()
    vault_cfg = _load_vault_config(config_dir, db_path)
    favicon_path = _bundle_root() / "assets" / "favicon.svg"
    app_version = _load_app_version()

    def build_ui_root() -> None:
        """Wrap UI building in a root function to prevent NiceGUI script mode detection."""
        build_ui(app_version=app_version)

    use_native_window = _use_native_window()

    ui.run(
        host="127.0.0.1" if use_native_window else "0.0.0.0",  # nosec B104
        port=_FIXED_WEB_UI_PORT,
        title=t("Klipper Vault"),
        dark=True,
        favicon=favicon_path,
        show=False,
        native=use_native_window,
        reload=False,
        root=build_ui_root if _is_frozen_runtime() else None,
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()