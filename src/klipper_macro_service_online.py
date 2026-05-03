#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Online update, macro share, and GitHub PR mixin for MacroGuiService.

Extracted from klipper_macro_gui_service to keep concern-specific logic
in focused, navigable modules. MacroGuiService inherits this mixin.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ValidationError, field_validator

from klipper_macro_indexer import (
    export_macro_share_payload,
    import_macro_share_payload,
    restore_macro_version,
)
from klipper_macro_online_update import (
    check_online_macro_updates,
    import_online_macro_updates,
)
from klipper_macro_online_repo_export import (
    build_printer_manifest_path,
    build_online_update_repository_artifacts,
    export_online_update_repository_zip,
)
from klipper_macro_github_api import (
    commit_changed_text_files,
    create_branch,
    create_pull_request,
    get_open_pull_request_for_head,
    load_json_file_from_branch,
)
from klipper_type_utils import to_int as _as_int
from klipper_type_utils import to_text as _as_text


def _as_list(value: object) -> list[object]:
    """Return list values unchanged and coerce everything else to an empty list."""
    return value if isinstance(value, list) else []


class PullRequestCreationResult(BaseModel):
    """Typed pull request creation payload used internally by the service."""

    created: bool
    existing: bool
    pull_request_number: int
    pull_request_url: str
    head_branch: str
    updated_files: int
    macro_count: int
    no_changes: bool
    commit_count: int

    @field_validator("pull_request_number", "updated_files", "macro_count", "commit_count", mode="before")
    @classmethod
    def validate_non_negative_int(cls, v: object) -> int:
        """Ensure all count fields are non-negative integers."""
        value = _as_int(v)
        if value < 0:
            raise ValueError("count fields must be non-negative")
        return value

    @field_validator("pull_request_url", "head_branch", mode="before")
    @classmethod
    def normalize_url_and_branch(cls, v: object) -> str:
        """Normalize URL and branch name fields."""
        return _as_text(v)

    def as_dict(self) -> dict[str, object]:
        """Convert typed PR creation payload to legacy dictionary contract."""
        return {
            "created": self.created,
            "existing": self.existing,
            "pull_request_number": self.pull_request_number,
            "pull_request_url": self.pull_request_url,
            "head_branch": self.head_branch,
            "updated_files": self.updated_files,
            "macro_count": self.macro_count,
            "no_changes": self.no_changes,
            "commit_count": self.commit_count,
        }


class ImportedUpdateItem(BaseModel):
    """Typed imported update payload used for optional activation logic."""

    identity: str = ""
    file_path: str
    macro_name: str
    version: int

    @field_validator("file_path", "macro_name", mode="before")
    @classmethod
    def validate_required_fields(cls, v: object) -> str:
        """Ensure required fields are non-empty."""
        text = _as_text(v)
        if not text:
            raise ValueError("field must not be empty")
        return text

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, v: object) -> int:
        """Ensure version is a positive integer."""
        value = _as_int(v)
        if value <= 0:
            raise ValueError("version must be a positive integer")
        return value

    @field_validator("identity", mode="before")
    @classmethod
    def normalize_identity(cls, v: object) -> str:
        """Normalize identity field."""
        return _as_text(v)


def _normalize_printer_identity(vendor: str, model: str) -> tuple[str, str]:
    """Normalize printer identity values for compatibility checks."""
    return _as_text(vendor).lower(), _as_text(model).lower()


def _prepare_pr_artifacts(
    *,
    artifacts: dict[str, object],
) -> tuple[dict[str, str], list[str], int]:
    """Normalize repository artifacts into commit-ready write/delete collections."""
    files_to_write_raw = artifacts.get("files_to_write", {})
    if not isinstance(files_to_write_raw, dict):
        raise RuntimeError("invalid export payload generated for pull request")
    files_to_write: dict[str, str] = {
        str(path): str(content)
        for path, content in files_to_write_raw.items()
    }

    files_to_delete_raw = artifacts.get("files_to_delete", [])
    if not isinstance(files_to_delete_raw, list):
        raise RuntimeError("invalid delete payload generated for pull request")
    files_to_delete = [
        _as_text(path).lstrip("/")
        for path in files_to_delete_raw
        if _as_text(path)
    ]

    manifest_payload = artifacts.get("manifest", {})
    if not isinstance(manifest_payload, dict):
        raise RuntimeError("invalid manifest payload generated for pull request")

    manifest_path = _as_text(artifacts.get("manifest_path", "")).lstrip("/")
    if not manifest_path:
        raise RuntimeError("invalid manifest path generated for pull request")
    files_to_write[manifest_path] = json.dumps(manifest_payload, indent=2, ensure_ascii=False)

    macro_count = _as_int(artifacts.get("macro_count", 0))
    return files_to_write, files_to_delete, macro_count


def _parse_imported_update_item(item: object) -> ImportedUpdateItem | None:
    """Normalize one imported update item; return None for malformed entries."""
    if not isinstance(item, dict):
        return None

    try:
        return ImportedUpdateItem(**item)
    except ValidationError:
        return None


class OnlineUpdateMixin:
    """Macro share file, online update, and GitHub PR operations."""

    # ------------------------------------------------------------------
    # Attributes provided by MacroGuiService at runtime
    # ------------------------------------------------------------------
    _db_path: Path
    _active_printer_profile_id: int

    def _resolve_runtime_config_dir(self) -> Path:
        raise NotImplementedError  # provided by MacroGuiService

    def _require_non_empty(self, value: str, error_message: str) -> str:
        raise NotImplementedError  # provided by MacroGuiService

    # ------------------------------------------------------------------ #
    # Macro share file import/export                                       #
    # ------------------------------------------------------------------ #

    def export_macro_share_file(
        self,
        identities: list[tuple[str, str]],
        source_vendor: str,
        source_model: str,
        out_file: Path,
    ) -> dict[str, object]:
        """Export selected latest macros to a shareable JSON file."""
        payload = self.export_macro_share_payload_data(
            identities=identities,
            source_vendor=source_vendor,
            source_model=source_model,
        )
        exported_macros = payload.get("macros", [])
        macro_count = len(exported_macros) if isinstance(exported_macros, list) else 0
        out_file = out_file.expanduser().resolve()
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return {
            "file_path": str(out_file),
            "macro_count": macro_count,
        }

    def import_macro_share_file(
        self,
        import_file: Path,
        target_vendor: str,
        target_model: str,
    ) -> dict[str, object]:
        """Import macros from a share file as new inactive rows."""
        import_file = import_file.expanduser().resolve()
        payload = json.loads(import_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("invalid macro share file")

        result = self.import_macro_share_payload_data(
            payload=payload,
            target_vendor=target_vendor,
            target_model=target_model,
        )

        return {
            **result,
            "file_path": str(import_file),
        }

    def export_macro_share_payload_data(
        self,
        *,
        identities: list[tuple[str, str]],
        source_vendor: str,
        source_model: str,
    ) -> dict[str, object]:
        """Return share-file payload without writing to disk."""
        return export_macro_share_payload(
            db_path=self._db_path,
            identities=identities,
            source_vendor=source_vendor,
            source_model=source_model,
            now_ts=int(time.time()),
        )

    def import_macro_share_payload_data(
        self,
        *,
        payload: dict[str, object],
        target_vendor: str,
        target_model: str,
    ) -> dict[str, object]:
        """Import macros from pre-loaded share payload data."""

        result = import_macro_share_payload(
            db_path=self._db_path,
            payload=payload,
            now_ts=int(time.time()),
        )

        source_vendor = _as_text(result.get("source_vendor", ""))
        source_model = _as_text(result.get("source_model", ""))
        src_vendor_norm, src_model_norm = _normalize_printer_identity(source_vendor, source_model)
        tgt_vendor_norm, tgt_model_norm = _normalize_printer_identity(target_vendor, target_model)
        printer_matches = bool(
            src_vendor_norm
            and src_model_norm
            and tgt_vendor_norm
            and tgt_model_norm
            and src_vendor_norm == tgt_vendor_norm
            and src_model_norm == tgt_model_norm
        )
        imported_count = _as_int(result.get("imported", 0))

        return {
            "imported": imported_count,
            "source_vendor": source_vendor,
            "source_model": source_model,
            "printer_matches": printer_matches,
        }

    # ------------------------------------------------------------------ #
    # Online macro updates                                                #
    # ------------------------------------------------------------------ #

    def check_online_updates(
        self,
        *,
        repo_url: str,
        repo_ref: str,
        source_vendor: str,
        source_model: str,
        manifest_path: str = "",
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, object]:
        """Check GitHub manifest for online macro updates for one printer identity."""
        return check_online_macro_updates(
            db_path=self._db_path,
            repo_url=repo_url,
            manifest_path=manifest_path,
            repo_ref=repo_ref,
            source_vendor=source_vendor,
            source_model=source_model,
            now_ts=int(time.time()),
            progress_callback=progress_callback,
        )

    def import_online_updates(
        self,
        *,
        updates: list[dict[str, object]],
        activate_identities: list[str],
        repo_url: str,
        repo_ref: str,
    ) -> dict[str, object]:
        """Import online updates and optionally activate selected imported versions."""
        import_result = import_online_macro_updates(
            db_path=self._db_path,
            updates=updates,
            repo_url=repo_url,
            repo_ref=repo_ref,
            printer_profile_id=(int(self._active_printer_profile_id) if int(self._active_printer_profile_id) > 0 else None),
            now_ts=int(time.time()),
        )

        imported_items = _as_list(import_result.get("imported_items", []))
        activate_set = {_as_text(identity) for identity in activate_identities}
        activated_count = 0
        activated_files: list[str] = []
        runtime_config_dir = self._resolve_runtime_config_dir()

        for item in imported_items:
            parsed_item = _parse_imported_update_item(item)
            if parsed_item is None or parsed_item.identity not in activate_set:
                continue

            restore_macro_version(
                db_path=self._db_path,
                config_dir=runtime_config_dir,
                file_path=parsed_item.file_path,
                macro_name=parsed_item.macro_name,
                version=parsed_item.version,
            )
            activated_count += 1
            activated_files.append(parsed_item.file_path)

        imported_count = _as_int(import_result.get("imported", 0))
        result = {
            "imported": imported_count,
            "activated": activated_count,
            "imported_items": imported_items,
        }

        if activated_files:
            result["remote_synced"] = False
            result["local_changed"] = True

        return result

    def export_online_update_repository_zip(
        self,
        *,
        out_file: Path,
        source_vendor: str,
        source_model: str,
        repo_url: str,
        repo_ref: str,
        manifest_path: str = "",
    ) -> dict[str, object]:
        """Export active local macros as a repository-ready online update zip."""
        return export_online_update_repository_zip(
            db_path=self._db_path,
            out_file=out_file,
            source_vendor=source_vendor,
            source_model=source_model,
            repo_url=repo_url,
            repo_ref=repo_ref,
            manifest_path=manifest_path,
            now_ts=int(time.time()),
        )

    # ------------------------------------------------------------------ #
    # GitHub pull request                                                 #
    # ------------------------------------------------------------------ #

    def create_online_update_pull_request(
        self,
        *,
        source_vendor: str,
        source_model: str,
        repo_url: str,
        base_branch: str,
        head_branch: str,
        github_token: str,
        pull_request_title: str,
        pull_request_body: str,
        manifest_path: str = "",
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, object]:
        """Create a GitHub pull request with exported active macro artifacts."""
        clean_repo_url = self._require_non_empty(repo_url, "online update repository URL is required")
        clean_base = self._require_non_empty(base_branch, "base branch is required")
        clean_head = self._require_non_empty(head_branch, "head branch is required")
        _ = manifest_path  # Deprecated: PR manifests are now always printer-local.
        target_manifest_path = build_printer_manifest_path(source_vendor, source_model)
        clean_token = self._require_non_empty(github_token, "GitHub token is required")
        clean_title = self._require_non_empty(pull_request_title, "pull request title is required")
        clean_body = str(pull_request_body or "").strip()

        def _report(current: int, total: int) -> None:
            if progress_callback is not None:
                progress_callback(max(int(current), 0), max(int(total), 1))

        _report(0, 6)

        existing_pr = get_open_pull_request_for_head(clean_repo_url, clean_token, clean_head)
        _report(1, 6)
        if existing_pr is not None:
            return PullRequestCreationResult(
                created=False,
                existing=True,
                pull_request_number=_as_int(existing_pr.get("number", 0)),
                pull_request_url=str(existing_pr.get("html_url", "")),
                head_branch=clean_head,
                updated_files=0,
                macro_count=0,
                no_changes=False,
                commit_count=0,
            ).as_dict()

        remote_manifest = load_json_file_from_branch(
            repo_url=clean_repo_url,
            token=clean_token,
            branch=clean_base,
            file_path=target_manifest_path,
        )
        _report(2, 6)

        artifacts = build_online_update_repository_artifacts(
            db_path=self._db_path,
            source_vendor=source_vendor,
            source_model=source_model,
            now_ts=int(time.time()),
            existing_manifest=remote_manifest,
        )
        _report(3, 6)
        files_to_write, files_to_delete, macro_count = _prepare_pr_artifacts(
            artifacts=artifacts,
        )

        branch_result = create_branch(
            repo_url=clean_repo_url,
            token=clean_token,
            base_branch=clean_base,
            head_branch=clean_head,
        )
        _report(4, 6)
        if bool(branch_result.get("already_exists", False)):
            raise RuntimeError(
                "head branch already exists on remote repository; choose a different branch name"
            )

        def _map_commit_progress(current: int, total: int) -> None:
            safe_total = max(int(total), 1)
            safe_current = max(int(current), 0)
            scaled_current = 4000 + int((safe_current / safe_total) * 1000)
            _report(scaled_current, 6000)

        commit_result = commit_changed_text_files(
            repo_url=clean_repo_url,
            token=clean_token,
            branch=clean_head,
            files=files_to_write,
            deleted_files=files_to_delete,
            commit_message=f"Update macros for {source_vendor} {source_model}",
            progress_callback=_map_commit_progress,
        )
        updated_files = _as_int(commit_result.get("changed_files", 0))

        if updated_files <= 0:
            _report(6, 6)
            return PullRequestCreationResult(
                created=False,
                existing=False,
                pull_request_number=0,
                pull_request_url="",
                head_branch=clean_head,
                updated_files=0,
                macro_count=macro_count,
                no_changes=True,
                commit_count=0,
            ).as_dict()

        pull_request_result = create_pull_request(
            repo_url=clean_repo_url,
            token=clean_token,
            base_branch=clean_base,
            head_branch=clean_head,
            title=clean_title,
            body=clean_body,
        )
        _report(6, 6)

        return PullRequestCreationResult(
            created=True,
            existing=bool(pull_request_result.get("existing", False)),
            pull_request_number=_as_int(pull_request_result.get("number", 0)),
            pull_request_url=str(pull_request_result.get("url", "")),
            head_branch=clean_head,
            updated_files=updated_files,
            macro_count=macro_count,
            no_changes=False,
            commit_count=1,
        ).as_dict()
