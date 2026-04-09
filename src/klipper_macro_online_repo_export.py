#!/usr/bin/env python3
# Copyright (C) 2026 Juergen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build repository-ready online update bundles from local active macros."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

from pydantic import BaseModel, field_validator

from klipper_macro_indexer import load_macro_list, macro_row_to_section_text
from klipper_repo_url_utils import build_raw_githubusercontent_url


def _as_int(value: object, default: int = 0) -> int:
    """Convert dynamic values to int with a safe fallback."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _normalize_component(value: str) -> str:
    """Normalize vendor/model components for folder paths."""
    normalized = str(value or "").strip().lower()
    if not normalized:
        raise ValueError("vendor/model must not be empty")
    return re.sub(r"[^a-z0-9._-]+", "-", normalized)


def _safe_macro_file_name(macro_name: str) -> str:
    """Return a stable file-safe name for a macro JSON file."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(macro_name or "").strip())
    return f"{cleaned or 'macro'}.json"


def _macro_relative_path(vendor: str, model: str, macro_name: str) -> str:
    """Build one normalized repository-relative path for a macro payload file."""
    return f"{vendor}/{model}/{_safe_macro_file_name(macro_name)}"


def _macro_version(indexed_at_raw: object, fallback_ts: int) -> str:
    """Derive stable YYYY-MM-DD version text from one macro timestamp value."""
    indexed_at = _as_int(indexed_at_raw, default=fallback_ts)
    return datetime.fromtimestamp(indexed_at, tz=timezone.utc).strftime("%Y-%m-%d")


def _sha256(text: str) -> str:
    """Compute SHA-256 checksum for UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ManifestEntry(BaseModel):
    """Validated manifest macro entry with normalized vendor/model."""

    vendor: str
    model: str
    macro_name: str
    path: str
    version: str = ""
    checksum_sha256: str = ""

    @field_validator("vendor", "model", mode="before")
    @classmethod
    def normalize_component_field(cls, v: object) -> str:
        """Normalize vendor/model using path-safe rules."""
        return _normalize_component(str(v or ""))

    @field_validator("macro_name", "path", mode="before")
    @classmethod
    def validate_required_fields(cls, v: object) -> str:
        """Validate required fields are non-empty."""
        text = str(v or "").strip()
        if not text:
            raise ValueError("field must not be empty")
        return text


def _normalize_manifest_entry(entry: object) -> dict[str, object] | None:
    """Return normalized manifest macro entry or None when invalid."""
    if not isinstance(entry, dict):
        return None
    try:
        validated = ManifestEntry(**entry)
        return validated.model_dump()
    except Exception:
        return None


def _normalize_manifest_entries(entries_raw: object) -> list[dict[str, object]]:
    """Normalize one manifest entries collection, dropping invalid rows."""
    if not isinstance(entries_raw, list):
        return []

    out: list[dict[str, object]] = []
    for entry in entries_raw:
        normalized_entry = _normalize_manifest_entry(entry)
        if normalized_entry is not None:
            out.append(normalized_entry)
    return out


def _build_raw_github_url(repo_url: str, repo_ref: str, file_path: str) -> str:
    """Build raw.githubusercontent URL for one file."""
    return build_raw_githubusercontent_url(
        repo_url,
        repo_ref=repo_ref,
        file_path=file_path,
        invalid_scheme_error="online update repository URL must use http/https",
        invalid_host_error="online update repository URL must point to github.com",
        invalid_path_error="online update repository URL must include owner/repo",
        empty_path_error="manifest path must not be empty",
    )


def _load_remote_manifest(repo_url: str, repo_ref: str, manifest_path: str) -> dict[str, object]:
    """Load manifest JSON from remote repository."""
    request = Request(
        _build_raw_github_url(repo_url, repo_ref, manifest_path),
        headers={"User-Agent": "KlipperVault/online-repo-export"},
    )
    try:
        with urlopen(request, timeout=12.0) as response:  # nosec:B310
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} while downloading manifest.json") from exc
    except URLError as exc:
        raise RuntimeError(f"network error while downloading manifest.json: {exc.reason}") from exc

    try:
        loaded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid manifest.json in repository: {exc}") from exc

    if not isinstance(loaded, dict):
        raise RuntimeError("invalid manifest.json in repository: root must be an object")
    return loaded


def build_online_update_repository_artifacts(
    *,
    db_path: Path,
    source_vendor: str,
    source_model: str,
    repo_url: str | None = None,
    repo_ref: str | None = None,
    manifest_path: str = "updates/manifest.json",
    now_ts: int | None = None,
    existing_manifest: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build manifest and file payloads for online-update repository publishing.

    Only files whose section_text checksum differs from the existing manifest entry
    are included in files_to_write.  The version field and generated_at are likewise
    only updated when content actually changed, so re-running on an unchanged macro
    set produces an identical blob and triggers no spurious PR commit.
    """
    vendor = _normalize_component(source_vendor)
    model = _normalize_component(source_model)
    timestamp = int(now_ts) if now_ts is not None else int(datetime.now(tz=timezone.utc).timestamp())

    macros = load_macro_list(db_path, limit=100000, include_macro_body=True)
    active_macros = [
        macro
        for macro in macros
        if bool(macro.get("is_active", False)) and not bool(macro.get("is_deleted", False))
    ]

    # Load the manifest first so existing checksums are available during the macro loop.
    manifest: dict[str, object]
    clean_repo_url = str(repo_url or "").strip()
    clean_repo_ref = str(repo_ref or "main").strip() or "main"
    clean_manifest_path = str(manifest_path or "updates/manifest.json").strip() or "updates/manifest.json"

    if existing_manifest is not None:
        manifest = dict(existing_manifest)
    elif clean_repo_url:
        manifest = _load_remote_manifest(clean_repo_url, clean_repo_ref, clean_manifest_path)
    else:
        manifest = {"manifest_version": "1", "macros": []}

    existing_entries = _normalize_manifest_entries(manifest.get("macros", []))

    # Lookup: (vendor, model, macro_name) -> existing manifest entry for checksum comparison.
    existing_by_key: dict[tuple[str, str, str], dict[str, object]] = {
        (str(e.get("vendor", "")), str(e.get("model", "")), str(e.get("macro_name", ""))): e
        for e in existing_entries
    }

    macro_entries: list[dict[str, object]] = []
    files_to_write: dict[str, str] = {}
    files_to_delete: list[str] = []
    any_content_changed = False

    for macro in active_macros:
        macro_name = str(macro.get("macro_name", "")).strip()
        if not macro_name:
            continue

        section_text = macro_row_to_section_text(macro)
        relative_path = _macro_relative_path(vendor, model, macro_name)
        checksum = _sha256(section_text)

        existing_entry = existing_by_key.get((vendor, model, macro_name))
        existing_checksum = str(existing_entry.get("checksum_sha256", "")) if existing_entry else ""
        existing_version = str(existing_entry.get("version", "")) if existing_entry else ""

        if existing_checksum and existing_checksum == checksum and existing_version:
            # Content unchanged: keep the existing published version; skip the file write.
            version = existing_version
        else:
            # New or changed macro: derive version from its last-indexed timestamp.
            version = _macro_version(macro.get("indexed_at", timestamp), timestamp)
            files_to_write[relative_path] = json.dumps(
                {
                    "macro_name": macro_name,
                    "source_file_path": "macros.cfg",
                    "version": version,
                    "section_text": section_text,
                },
                indent=2,
                ensure_ascii=False,
            )
            any_content_changed = True

        macro_entries.append(
            {
                "vendor": vendor,
                "model": model,
                "macro_name": macro_name,
                "path": relative_path,
                "version": version,
                "checksum_sha256": checksum,
            }
        )

    exported_keyed = {
        (vendor, model, str(entry["macro_name"])): entry
        for entry in macro_entries
    }
    merged_entries: list[dict[str, object]] = []
    for entry in existing_entries:
        key = (str(entry.get("vendor", "")), str(entry.get("model", "")), str(entry.get("macro_name", "")))
        if key[0] == vendor and key[1] == model and key not in exported_keyed:
            deleted_path = str(entry.get("path", "")).strip().lstrip("/")
            if deleted_path:
                files_to_delete.append(deleted_path)
                any_content_changed = True
            continue
        replacement = exported_keyed.pop(key, None)
        merged_entries.append(replacement if replacement is not None else entry)
    merged_entries.extend(exported_keyed.values())

    manifest["manifest_version"] = str(manifest.get("manifest_version", "1") or "1")
    # Only advance generated_at when content actually changed so the manifest blob SHA
    # remains stable across no-op runs and does not trigger spurious commits.
    if any_content_changed:
        manifest["generated_at"] = timestamp
    manifest["macros"] = sorted(
        merged_entries,
        key=lambda entry: (
            str(entry.get("vendor", "")).lower(),
            str(entry.get("model", "")).lower(),
            str(entry.get("macro_name", "")).lower(),
            str(entry.get("path", "")).lower(),
        ),
    )

    return {
        "manifest": manifest,
        "manifest_path": clean_manifest_path,
        "files_to_write": files_to_write,
        "files_to_delete": sorted(set(files_to_delete)),
        "macro_count": len(macro_entries),
        "source_vendor": vendor,
        "source_model": model,
    }


def export_online_update_repository_zip(
    *,
    db_path: Path,
    out_file: Path,
    source_vendor: str,
    source_model: str,
    repo_url: str | None = None,
    repo_ref: str | None = None,
    manifest_path: str = "updates/manifest.json",
    now_ts: int | None = None,
) -> dict[str, object]:
    """Export active local macros into a zip for the online update repository."""
    vendor = _normalize_component(source_vendor)
    model = _normalize_component(source_model)
    timestamp = int(now_ts) if now_ts is not None else int(datetime.now(tz=timezone.utc).timestamp())

    macros = load_macro_list(db_path, limit=100000, include_macro_body=True)
    active_macros = [
        macro
        for macro in macros
        if bool(macro.get("is_active", False)) and not bool(macro.get("is_deleted", False))
    ]
    if not active_macros:
        raise ValueError("no active macros available for export")

    macro_entries: list[dict[str, object]] = []
    files_to_write: dict[str, str] = {}

    for macro in active_macros:
        macro_name = str(macro.get("macro_name", "")).strip()
        if not macro_name:
            continue

        section_text = macro_row_to_section_text(macro)
        relative_path = _macro_relative_path(vendor, model, macro_name)
        checksum = _sha256(section_text)

        version = _macro_version(macro.get("indexed_at", timestamp), timestamp)
        macro_payload = {
            "macro_name": macro_name,
            "source_file_path": "macros.cfg",
            "version": version,
            "section_text": section_text,
        }

        files_to_write[relative_path] = json.dumps(macro_payload, indent=2, ensure_ascii=False)
        macro_entries.append(
            {
                "vendor": vendor,
                "model": model,
                "macro_name": macro_name,
                "path": relative_path,
                "version": version,
                "checksum_sha256": checksum,
            }
        )

    if not macro_entries:
        raise ValueError("no valid active macros available for export")

    manifest: dict[str, object]
    clean_repo_url = str(repo_url or "").strip()
    clean_repo_ref = str(repo_ref or "main").strip() or "main"
    clean_manifest_path = str(manifest_path or "updates/manifest.json").strip() or "updates/manifest.json"
    if clean_repo_url:
        manifest = _load_remote_manifest(clean_repo_url, clean_repo_ref, clean_manifest_path)
    else:
        manifest = {"manifest_version": "1", "macros": []}

    existing_entries = _normalize_manifest_entries(manifest.get("macros", []))

    exported_keyed = {
        (vendor, model, str(entry["macro_name"])): entry
        for entry in macro_entries
    }
    merged_entries: list[dict[str, object]] = []
    for entry in existing_entries:
        key = (str(entry.get("vendor", "")), str(entry.get("model", "")), str(entry.get("macro_name", "")))
        replacement = exported_keyed.pop(key, None)
        merged_entries.append(replacement if replacement is not None else entry)
    merged_entries.extend(exported_keyed.values())

    manifest["manifest_version"] = str(manifest.get("manifest_version", "1") or "1")
    manifest["generated_at"] = timestamp
    manifest["macros"] = sorted(
        merged_entries,
        key=lambda entry: (
            str(entry.get("vendor", "")).lower(),
            str(entry.get("model", "")).lower(),
            str(entry.get("macro_name", "")).lower(),
            str(entry.get("path", "")).lower(),
        ),
    )

    out_path = out_file.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(clean_manifest_path.lstrip("/"), json.dumps(manifest, indent=2, ensure_ascii=False))
        for rel_path, payload in files_to_write.items():
            archive.writestr(rel_path, payload)

    return {
        "file_path": str(out_path),
        "macro_count": len(macro_entries),
        "source_vendor": vendor,
        "source_model": model,
    }
