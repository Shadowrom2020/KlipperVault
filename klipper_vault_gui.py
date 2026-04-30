#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Primary GUI launcher for KlipperVault."""

from __future__ import annotations

import inspect
import os
import platform
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


def _is_container_runtime() -> bool:
    """Return True when running inside a containerized runtime."""
    raw = os.environ.get("KLIPPERVAULT_CONTAINER", "").strip().lower()
    if raw in {"1", "true", "yes", "on", "docker", "container"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _ui_host_binding() -> str:
    """Return the preferred bind host for the NiceGUI server."""
    return "0.0.0.0" if _is_container_runtime() else "127.0.0.1"  # nosec


def _use_native_window() -> bool:
    """Use desktop native window only in packaged non-server local runtimes."""
    # Linux native windows require system webview libraries that are not bundled.
    if platform.system() == "Linux":
        return False
    return _is_frozen_runtime() and not _is_server_mode()


def _use_save_dialog() -> bool:
    """Show save-path dialog on Windows/macOS frozen binary instead of browser download."""
    return _is_frozen_runtime() and platform.system() in {"Windows", "Darwin"}


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


def _is_benign_shutdown_exception(error: BaseException) -> bool:
    """Return True for known benign asyncio/uvicorn teardown exceptions on any platform."""
    # Ctrl-C / SIGINT raises KeyboardInterrupt through the asyncio runner on all platforms.
    if isinstance(error, KeyboardInterrupt):
        return True

    message = str(error)

    # Cross-platform: asyncio event loop closed after the server stops.
    if isinstance(error, RuntimeError) and "Event loop is closed" in message:
        return True

    # Cross-platform: pipe/socket closed during asyncio teardown (e.g. on Linux/macOS).
    if isinstance(error, ValueError) and "I/O operation on closed" in message:
        return True

    if isinstance(error, OSError):
        # Windows ProactorEventLoop teardown races (winerror 6=ERROR_INVALID_HANDLE,
        # 995=ERROR_OPERATION_ABORTED, 10038=WSAENOTSOCK).
        if platform.system() == "Windows":
            return getattr(error, "winerror", None) in {6, 995, 10038}
        # Linux/macOS: EBADF (9) or ENOTSOCK (88/38) from SelectorEventLoop teardown.
        return error.errno in {9, 38, 88}

    return False


def main() -> None:
    """Start the KlipperVault GUI runtime."""
    from klipper_macro_gui import build_ui
    from klipper_vault_config import _FIXED_WEB_UI_PORT
    from klipper_vault_i18n import t
    from nicegui import ui

    _patch_nicegui_disconnect_signature()
    _patch_nicegui_deleted_parent_slot_event_race()
    _patch_nicegui_deleted_parent_slot_exception_filter()

    # Disable NiceGUI's reloader in bundled executables to prevent
    # attempts to reload the binary executable, which causes null byte errors.
    if _is_frozen_runtime():
        os.environ["NICEGUI_AUTORELOAD"] = "False"
        os.environ["NICEGUI_RELOAD"] = "no"

    favicon_path = _bundle_root() / "assets" / "favicon.svg"
    app_version = _load_app_version()

    def build_ui_root() -> None:
        """Wrap UI building in a root function to prevent NiceGUI script mode detection."""
        build_ui(app_version=app_version, use_save_dialog=_use_save_dialog())

    use_native_window = _use_native_window()

    try:
        ui.run(
            host=_ui_host_binding(),  # nosec B104
            port=_FIXED_WEB_UI_PORT,
            title=t("Klipper Vault"),
            favicon=favicon_path,
            show=False,
            native=use_native_window,
            reload=False,
            root=build_ui_root,
        )
    except (KeyboardInterrupt, OSError, RuntimeError, ValueError) as error:
        if _is_benign_shutdown_exception(error):
            return
        raise


if __name__ in {"__main__", "__mp_main__"}:
    main()