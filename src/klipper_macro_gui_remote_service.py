#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Remote GUI service adapter for talking to the host API."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Callable
import time

import httpx


class RemoteMacroGuiService:
    """Network-backed replacement for MacroGuiService with the same public methods."""

    def __init__(
        self,
        *,
        base_url: str,
        api_token: str = "",
        timeout: float = 60.0,
        index_timeout: float = 300.0,
    ) -> None:
        clean_base_url = str(base_url or "").strip().rstrip("/")
        if not clean_base_url:
            raise ValueError("remote API base URL is required")

        self._base_url = clean_base_url
        self._timeout = max(float(timeout), 1.0)
        self._index_timeout = max(float(index_timeout), self._timeout)
        self._api_token = str(api_token or "").strip()

    def _headers(self) -> dict[str, str]:
        """Build API request headers, including optional bearer token."""
        headers = {"Content-Type": "application/json"}
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        return headers

    def _request_json(
        self,
        *,
        method: str,
        path: str,
        json_body: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
        timeout: float | None = None,
    ) -> dict[str, object]:
        """Perform one API request and return JSON object payload."""
        url = f"{self._base_url}{path}"
        request_timeout = self._timeout if timeout is None else max(float(timeout), 1.0)

        try:
            response = httpx.request(
                method=method,
                url=url,
                headers=self._headers(),
                json=json_body,
                params=params,
                timeout=request_timeout,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Remote API request failed: {exc}") from exc

        try:
            payload = response.json() if response.text else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError("Remote API returned invalid JSON") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("Remote API returned an invalid payload")

        if response.status_code >= 400 or not bool(payload.get("ok", False)):
            detail = str(payload.get("error", "Remote API request failed")).strip()
            if detail:
                raise RuntimeError(detail)
            raise RuntimeError(f"Remote API returned HTTP {response.status_code}")
        return payload

    def load_dashboard(self, *, limit: int = 500, offset: int = 0) -> tuple[dict[str, object], list[dict[str, object]]]:
        """Load dashboard stats and macros from typed API endpoint."""
        payload = self._request_json(
            method="GET",
            path="/api/v1/dashboard",
            params={"limit": max(int(limit), 1), "offset": max(int(offset), 0)},
        )
        stats = payload.get("stats", {})
        macros = payload.get("macros", [])
        stats_dict = stats if isinstance(stats, dict) else {}
        macro_rows = macros if isinstance(macros, list) else []
        return stats_dict, [row for row in macro_rows if isinstance(row, dict)]

    def load_versions(self, file_path: str, macro_name: str) -> list[dict[str, object]]:
        """Load macro versions from typed API endpoint."""
        payload = self._request_json(
            method="GET",
            path="/api/v1/macros/versions",
            params={
                "file_path": str(file_path or ""),
                "macro_name": str(macro_name or ""),
            },
        )
        versions = payload.get("versions", [])
        if not isinstance(versions, list):
            return []
        return [row for row in versions if isinstance(row, dict)]

    def load_latest_for_file(self, macro_name: str, file_path: str) -> dict[str, object] | None:
        """Load latest macro row by reusing typed versions endpoint."""
        versions = self.load_versions(file_path, macro_name)
        return versions[0] if versions else None

    def list_backups(self) -> list[dict[str, object]]:
        """Load backups list from typed API endpoint."""
        payload = self._request_json(method="GET", path="/api/v1/backups")
        backups = payload.get("backups", [])
        if not isinstance(backups, list):
            return []
        return [row for row in backups if isinstance(row, dict)]

    def load_backup_contents(self, backup_id: int) -> list[dict[str, object]]:
        """Load backup contents from typed API endpoint."""
        payload = self._request_json(method="GET", path=f"/api/v1/backups/{int(backup_id)}/items")
        items = payload.get("items", [])
        if not isinstance(items, list):
            return []
        return [row for row in items if isinstance(row, dict)]

    def query_printer_status(self, timeout: float = 2.0) -> dict[str, object]:
        """Load printer status from typed API endpoint."""
        payload = self._request_json(method="GET", path="/api/v1/printer/status", timeout=timeout)
        status = payload.get("status", {})
        return status if isinstance(status, dict) else {}

    def load_cfg_loading_overview(self) -> dict[str, object]:
        """Load cfg parse overview from typed API endpoint."""
        payload = self._request_json(method="GET", path="/api/v1/cfg-loading-overview")
        overview = payload.get("overview", {})
        return overview if isinstance(overview, dict) else {}

    def list_duplicates(self) -> list[dict[str, object]]:
        """Load duplicate macro groups from typed API endpoint."""
        payload = self._request_json(method="GET", path="/api/v1/duplicates")
        groups = payload.get("groups", [])
        if not isinstance(groups, list):
            return []
        return [row for row in groups if isinstance(row, dict)]

    def index(self) -> dict[str, object]:
        """Trigger host-side index job and block until completion for GUI parity."""
        payload = self._request_json(method="POST", path="/api/v1/index", json_body={"trigger": "remote-gui"})
        job = payload.get("job", {})
        if not isinstance(job, dict):
            raise RuntimeError("Remote API returned invalid index job payload")

        job_id = str(job.get("job_id", "")).strip()
        if not job_id:
            raise RuntimeError("Remote API did not return an index job id")

        deadline = time.monotonic() + self._index_timeout
        while True:
            job_payload = self._request_json(method="GET", path=f"/api/v1/jobs/{job_id}")
            status_block = job_payload.get("job", {})
            if not isinstance(status_block, dict):
                raise RuntimeError("Remote API returned invalid job status payload")

            status = str(status_block.get("status", "")).strip().lower()
            if status == "completed":
                result = status_block.get("result", {})
                return result if isinstance(result, dict) else {}
            if status == "failed":
                error_text = str(status_block.get("error", "Indexing failed")).strip()
                raise RuntimeError(error_text or "Indexing failed")

            if time.monotonic() >= deadline:
                raise RuntimeError("Indexing timed out while waiting for host service")
            time.sleep(0.5)

    def _action(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        """Call one typed action endpoint and return result payload."""
        response = self._request_json(
            method="POST",
            path=f"/api/v1/actions/{action}",
            json_body=payload,
        )
        result = response.get("result", {})
        return result if isinstance(result, dict) else {}

    def _run_job_with_polling(
        self,
        *,
        start_path: str,
        start_payload: dict[str, object],
        progress_callback: Callable[[int, int], None] | None,
        timeout: float,
    ) -> dict[str, object]:
        """Start one job endpoint and wait for completion while reporting progress."""
        start_response = self._request_json(
            method="POST",
            path=start_path,
            json_body=start_payload,
            timeout=max(timeout, self._timeout),
        )
        job = start_response.get("job", {})
        if not isinstance(job, dict):
            raise RuntimeError("Remote API returned invalid job payload")
        job_id = str(job.get("job_id", "")).strip()
        if not job_id:
            raise RuntimeError("Remote API did not return a job id")

        deadline = time.monotonic() + max(float(timeout), self._timeout)
        while True:
            job_payload = self._request_json(method="GET", path=f"/api/v1/jobs/{job_id}")
            status_block = job_payload.get("job", {})
            if not isinstance(status_block, dict):
                raise RuntimeError("Remote API returned invalid job status payload")

            current = int(status_block.get("progress_current", 0) or 0)
            total = max(int(status_block.get("progress_total", 1) or 1), 1)
            if progress_callback is not None:
                progress_callback(current, total)

            status = str(status_block.get("status", "")).strip().lower()
            if status == "completed":
                result = status_block.get("result", {})
                return result if isinstance(result, dict) else {}
            if status == "failed":
                error_text = str(status_block.get("error", "Job failed")).strip()
                raise RuntimeError(error_text or "Job failed")

            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out while waiting for host job completion")
            time.sleep(0.5)

    def remove_deleted(self, file_path: str, macro_name: str) -> dict[str, object]:
        """Remove deleted macro rows via typed action endpoint."""
        return self._action("remove_deleted", {"file_path": file_path, "macro_name": macro_name})

    def remove_inactive_version(self, file_path: str, macro_name: str, version: int) -> dict[str, object]:
        """Remove one inactive macro version via typed action endpoint."""
        return self._action(
            "remove_inactive_version",
            {"file_path": file_path, "macro_name": macro_name, "version": int(version)},
        )

    def purge_all_deleted(self) -> dict[str, object]:
        """Purge all deleted macros via typed action endpoint."""
        return self._action("purge_all_deleted", {})

    def restore_version(self, file_path: str, macro_name: str, version: int) -> dict[str, object]:
        """Restore macro version via typed action endpoint."""
        return self._action(
            "restore_version",
            {"file_path": file_path, "macro_name": macro_name, "version": int(version)},
        )

    def save_macro_editor_text(self, file_path: str, macro_name: str, section_text: str) -> dict[str, object]:
        """Save macro edit via typed action endpoint."""
        return self._action(
            "save_macro_editor_text",
            {
                "file_path": file_path,
                "macro_name": macro_name,
                "section_text": section_text,
            },
        )

    def delete_macro_source(self, file_path: str, macro_name: str) -> dict[str, object]:
        """Delete macro source via typed action endpoint."""
        return self._action("delete_macro_source", {"file_path": file_path, "macro_name": macro_name})

    def resolve_duplicates(
        self,
        keep_choices: dict[str, str],
        duplicate_groups: list[dict[str, object]],
    ) -> dict[str, object]:
        """Resolve duplicates via typed action endpoint."""
        return self._action(
            "resolve_duplicates",
            {
                "keep_choices": keep_choices,
                "duplicate_groups": duplicate_groups,
            },
        )

    def create_backup(self, name: str) -> dict[str, object]:
        """Create backup via typed action endpoint."""
        return self._action("create_backup", {"name": name})

    def restore_backup(self, backup_id: int) -> dict[str, object]:
        """Restore backup via typed action endpoint."""
        return self._action("restore_backup", {"backup_id": int(backup_id)})

    def delete_backup(self, backup_id: int) -> dict[str, object]:
        """Delete backup via typed action endpoint."""
        return self._action("delete_backup", {"backup_id": int(backup_id)})

    def restart_klipper(self, timeout: float = 3.0) -> dict[str, object]:
        """Restart Klipper via typed action endpoint."""
        return self._action("restart_klipper", {"timeout": float(timeout)})

    def reload_dynamic_macros(self, timeout: float = 3.0) -> dict[str, object]:
        """Reload dynamic macros via typed action endpoint."""
        return self._action("reload_dynamic_macros", {"timeout": float(timeout)})

    def send_mainsail_notification(
        self,
        *,
        message: str,
        title: str = "KlipperVault",
        timeout: float = 3.0,
    ) -> dict[str, object]:
        """Send Mainsail notification via typed action endpoint."""
        return self._action(
            "send_mainsail_notification",
            {
                "message": message,
                "title": title,
                "timeout": float(timeout),
            },
        )

    def import_online_updates(
        self,
        *,
        updates: list[dict[str, object]],
        activate_identities: list[str],
        repo_url: str,
        repo_ref: str,
    ) -> dict[str, object]:
        """Import online updates via typed action endpoint."""
        return self._action(
            "import_online_updates",
            {
                "updates": updates,
                "activate_identities": activate_identities,
                "repo_url": repo_url,
                "repo_ref": repo_ref,
            },
        )

    def check_online_updates(
        self,
        *,
        repo_url: str,
        manifest_path: str,
        repo_ref: str,
        source_vendor: str,
        source_model: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, object]:
        """Check online updates using host-side background job with polling."""
        return self._run_job_with_polling(
            start_path="/api/v1/jobs/online-check",
            start_payload={
                "repo_url": repo_url,
                "manifest_path": manifest_path,
                "repo_ref": repo_ref,
                "source_vendor": source_vendor,
                "source_model": source_model,
            },
            progress_callback=progress_callback,
            timeout=self._index_timeout,
        )

    def create_online_update_pull_request(
        self,
        *,
        source_vendor: str,
        source_model: str,
        repo_url: str,
        base_branch: str,
        head_branch: str,
        manifest_path: str,
        github_token: str,
        pull_request_title: str,
        pull_request_body: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, object]:
        """Create pull request using host-side background job with polling."""
        return self._run_job_with_polling(
            start_path="/api/v1/jobs/create-pr",
            start_payload={
                "source_vendor": source_vendor,
                "source_model": source_model,
                "repo_url": repo_url,
                "base_branch": base_branch,
                "head_branch": head_branch,
                "manifest_path": manifest_path,
                "github_token": github_token,
                "pull_request_title": pull_request_title,
                "pull_request_body": pull_request_body,
            },
            progress_callback=progress_callback,
            timeout=max(self._index_timeout, 900.0),
        )

    def export_online_update_repository_zip(
        self,
        *,
        out_file: Path,
        source_vendor: str,
        source_model: str,
        repo_url: str,
        repo_ref: str,
        manifest_path: str,
    ) -> dict[str, object]:
        """Export online update repository zip via typed endpoint and write local file."""
        response = self._request_json(
            method="POST",
            path="/api/v1/online-update/export-zip",
            json_body={
                "source_vendor": source_vendor,
                "source_model": source_model,
                "repo_url": repo_url,
                "repo_ref": repo_ref,
                "manifest_path": manifest_path,
            },
            timeout=max(self._index_timeout, 600.0),
        )
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise RuntimeError("Remote API returned invalid export zip payload")
        zip_base64 = str(result.get("zip_base64", "") or "").strip()
        if not zip_base64:
            raise RuntimeError("Remote API returned empty zip payload")
        zip_bytes = base64.b64decode(zip_base64.encode("ascii"))

        resolved_out_file = Path(out_file).expanduser().resolve()
        resolved_out_file.parent.mkdir(parents=True, exist_ok=True)
        resolved_out_file.write_bytes(zip_bytes)
        return {
            "file_path": str(resolved_out_file),
            "macro_count": int(result.get("macro_count", 0) or 0),
        }

    def export_macro_share_file(
        self,
        identities: list[tuple[str, str]],
        source_vendor: str,
        source_model: str,
        out_file: Path,
    ) -> dict[str, object]:
        """Export macros via typed share endpoint and write local output file."""
        response = self._request_json(
            method="POST",
            path="/api/v1/share/export",
            json_body={
                "identities": [[str(file_path), str(macro_name)] for file_path, macro_name in identities],
                "source_vendor": source_vendor,
                "source_model": source_model,
            },
            timeout=self._index_timeout,
        )
        payload = response.get("payload", {})
        if not isinstance(payload, dict):
            raise RuntimeError("Remote API returned invalid share export payload")
        macro_count = int(response.get("macro_count", 0) or 0)

        resolved_out_file = Path(out_file).expanduser().resolve()
        resolved_out_file.parent.mkdir(parents=True, exist_ok=True)
        resolved_out_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return {
            "file_path": str(resolved_out_file),
            "macro_count": macro_count,
        }

    def import_macro_share_file(
        self,
        import_file: Path,
        target_vendor: str,
        target_model: str,
    ) -> dict[str, object]:
        """Import local share file by sending payload data to the host API."""
        resolved_file = Path(import_file).expanduser().resolve()
        payload_raw = json.loads(resolved_file.read_text(encoding="utf-8"))
        payload_data = payload_raw if isinstance(payload_raw, dict) else {}

        response = self._request_json(
            method="POST",
            path="/api/v1/share/import",
            json_body={
                "payload": payload_data,
                "target_vendor": target_vendor,
                "target_model": target_model,
            },
            timeout=self._index_timeout,
        )
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise RuntimeError("Remote API returned invalid share import result")
        return {
            **result,
            "file_path": str(resolved_file),
        }

    def query_health(self) -> dict[str, object]:
        """Return health metadata from the host API."""
        return self._request_json(method="GET", path="/api/v1/health")

    def stream_events(
        self,
        *,
        on_event: Callable[[dict[str, object]], None],
        stop_requested: Callable[[], bool] | None = None,
        last_event_id: int = 0,
    ) -> int:
        """Consume host SSE events and invoke callback for each parsed event.

        Returns the last seen event id.
        """
        current_last_event_id = max(int(last_event_id), 0)
        while True:
            if stop_requested is not None and stop_requested():
                return current_last_event_id

            try:
                with httpx.stream(
                    "GET",
                    f"{self._base_url}/api/v1/events",
                    headers=self._headers(),
                    params={"last_event_id": current_last_event_id},
                    timeout=None,
                ) as response:
                    if response.status_code >= 400:
                        raise RuntimeError(f"Event stream returned HTTP {response.status_code}")

                    pending_id: int | None = None
                    pending_event_type: str | None = None
                    pending_data_lines: list[str] = []

                    for line in response.iter_lines():
                        if stop_requested is not None and stop_requested():
                            return current_last_event_id

                        if line is None:
                            continue
                        text = str(line)
                        if text == "":
                            if pending_data_lines:
                                try:
                                    parsed = json.loads("\n".join(pending_data_lines))
                                except json.JSONDecodeError:
                                    parsed = {}
                                if isinstance(parsed, dict):
                                    event_payload = parsed
                                else:
                                    event_payload = {"data": parsed}
                                if pending_id is not None:
                                    event_payload["id"] = pending_id
                                    current_last_event_id = max(current_last_event_id, pending_id)
                                if pending_event_type is not None:
                                    event_payload["type"] = pending_event_type
                                on_event(event_payload)
                            pending_id = None
                            pending_event_type = None
                            pending_data_lines = []
                            continue

                        if text.startswith(":"):
                            continue
                        if text.startswith("id:"):
                            raw_id = text[3:].strip()
                            try:
                                pending_id = int(raw_id)
                            except ValueError:
                                pending_id = None
                            continue
                        if text.startswith("event:"):
                            pending_event_type = text[6:].strip()
                            continue
                        if text.startswith("data:"):
                            pending_data_lines.append(text[5:].strip())
            except Exception:
                if stop_requested is not None and stop_requested():
                    return current_last_event_id
                time.sleep(2.0)
