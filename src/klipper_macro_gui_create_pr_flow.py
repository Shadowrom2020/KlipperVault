#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared create-PR UI flow helpers for the NiceGUI frontend."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from nicegui import ui

from klipper_macro_gui_state import UIState
from klipper_type_utils import to_int as _to_int
from klipper_vault_i18n import t

_WidgetT = TypeVar("_WidgetT")


def _require_widget(widget: _WidgetT | None, *, name: str) -> _WidgetT:
    """Return initialized UI widget reference or raise with clear context."""
    if widget is None:
        raise RuntimeError(f"UI control '{name}' is not initialized")
    return widget


def collect_create_pr_inputs(state: UIState) -> dict[str, str]:
    """Collect normalized pull-request form values from dialog controls."""
    repo_url_input = _require_widget(state.pr_repo_url_input, name="pr_repo_url_input")
    base_branch_input = _require_widget(state.pr_base_branch_input, name="pr_base_branch_input")
    head_branch_input = _require_widget(state.pr_head_branch_input, name="pr_head_branch_input")
    title_input = _require_widget(state.pr_title_input, name="pr_title_input")
    body_input = _require_widget(state.pr_body_input, name="pr_body_input")
    token_input = _require_widget(state.pr_token_input, name="pr_token_input")

    return {
        "repo_url": str(repo_url_input.value or "").strip(),
        "base_branch": str(base_branch_input.value or "").strip(),
        "head_branch": str(head_branch_input.value or "").strip(),
        "title": str(title_input.value or "").strip(),
        "body": str(body_input.value or "").strip(),
        "token": str(token_input.value or "").strip(),
    }


def validate_create_pr_inputs(*, printer_profile_missing: bool, inputs: dict[str, str]) -> str | None:
    """Return create-PR validation error message or None when valid."""
    if printer_profile_missing:
        return t("Set printer vendor/model before creating a pull request.")

    required_values = (
        inputs.get("repo_url", ""),
        inputs.get("base_branch", ""),
        inputs.get("head_branch", ""),
        inputs.get("title", ""),
        inputs.get("token", ""),
    )
    if not all(required_values):
        return t("Repository URL, branches, title, and token are required.")
    return None


def set_create_pr_status_from_result(state: UIState, result: dict[str, object]) -> None:
    """Set user-facing status message for create-PR result payload."""
    status_label = _require_widget(state.status_label, name="status_label")
    pr_number = _to_int(result.get("pull_request_number", 0))
    pr_url = str(result.get("pull_request_url", "")).strip()
    updated_files = _to_int(result.get("updated_files", 0))
    macro_count = _to_int(result.get("macro_count", 0))
    commit_count = _to_int(result.get("commit_count", 0))

    if bool(result.get("no_changes", False)):
        message = t("No macro changes detected for pull request. PR was not created.")
        status_label.set_text(message)
        ui.notify(message, type="warning")
        return

    if bool(result.get("existing", False)):
        status_label.set_text(
            t(
                "Open pull request already exists (#{number}): {url}",
                number=pr_number,
                url=pr_url or "-",
            )
        )
        return

    status_label.set_text(
        t(
            "Created pull request #{number} with {files} updated file(s), {commits} commit(s), for {count} macro(s): {url}",
            number=pr_number,
            files=updated_files,
            commits=commit_count,
            count=macro_count,
            url=pr_url or "-",
        )
    )


def begin_create_pr_request(state: UIState, refresh_progress_ui: Callable[[], None]) -> None:
    """Set UI state to create-PR in-progress mode."""
    confirm_button = _require_widget(state.confirm_create_pr_button, name="confirm_create_pr_button")
    error_label = _require_widget(state.create_pr_error_label, name="create_pr_error_label")
    status_label = _require_widget(state.status_label, name="status_label")

    state.create_pr_in_progress = True
    state.create_pr_progress_current = 0
    state.create_pr_progress_total = 1
    confirm_button.set_enabled(False)
    error_label.set_text("")
    status_label.set_text(t("Creating GitHub pull request..."))
    refresh_progress_ui()


def set_create_pr_request_failure(state: UIState, refresh_progress_ui: Callable[[], None], exc: Exception) -> None:
    """Set UI state and labels for create-PR request failure."""
    error_label = _require_widget(state.create_pr_error_label, name="create_pr_error_label")
    status_label = _require_widget(state.status_label, name="status_label")
    confirm_button = _require_widget(state.confirm_create_pr_button, name="confirm_create_pr_button")

    state.create_pr_in_progress = False
    refresh_progress_ui()
    error_label.set_text(t("Create PR failed: {error}", error=exc))
    status_label.set_text(t("Create PR failed: {error}", error=exc))
    confirm_button.set_enabled(True)


def finish_create_pr_request(
    state: UIState,
    refresh_progress_ui: Callable[[], None],
    result: dict[str, object],
) -> None:
    """Finalize create-PR request UI and render result status."""
    confirm_button = _require_widget(state.confirm_create_pr_button, name="confirm_create_pr_button")
    dialog = _require_widget(state.create_pr_dialog, name="create_pr_dialog")

    state.create_pr_in_progress = False
    refresh_progress_ui()
    confirm_button.set_enabled(True)
    dialog.close()
    set_create_pr_status_from_result(state, result)
