#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host API service for remote KlipperVault GUI clients."""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from klipper_macro_gui_service import MacroGuiService
from klipper_vault_config import load_or_create
from klipper_vault_paths import DEFAULT_CONFIG_DIR, DEFAULT_DB_PATH

_MUTATING_METHOD_NAMES: set[str] = {
    "index",
    "remove_deleted",
    "remove_inactive_version",
    "purge_all_deleted",
    "restore_version",
    "save_macro_editor_text",
    "delete_macro_source",
    "resolve_duplicates",
    "create_backup",
    "restore_backup",
    "delete_backup",
    "import_macro_share_file",
    "import_online_updates",
    "create_online_update_pull_request",
    "restart_klipper",
    "reload_dynamic_macros",
}


def _requires_api_token_for_bind(bind_host: str) -> bool:
    """Return True when host binding is network-accessible and should require auth."""
    normalized = str(bind_host or "").strip().lower()
    return normalized not in {"127.0.0.1", "localhost", "::1"}


def _payload_text(payload: dict[str, object], key: str, default: str = "") -> str:
    """Extract one payload field as stripped text preserving legacy fallback behavior."""
    return str(payload.get(key, default) or default).strip()


def _payload_int(payload: dict[str, object], key: str, default: int = 0) -> int:
    """Extract one payload field as int preserving legacy conversion behavior."""
    return int(payload.get(key, default) or default)


def _payload_float(payload: dict[str, object], key: str, default: float = 0.0) -> float:
    """Extract one payload field as float preserving legacy conversion behavior."""
    return float(payload.get(key, default) or default)


def _payload_dict(payload: dict[str, object], key: str) -> dict[str, object]:
    """Extract one payload field as dict with empty-dict fallback."""
    value = payload.get(key, {})
    return value if isinstance(value, dict) else {}


def _payload_list(payload: dict[str, object], key: str) -> list[object]:
    """Extract one payload field as list with empty-list fallback."""
    value = payload.get(key, [])
    return value if isinstance(value, list) else []


class _HostApiState:
    """Mutable host API runtime state shared by request handlers."""

    def __init__(
        self,
        *,
        service: MacroGuiService,
        config_dir: Path,
        api_token: str,
    ) -> None:
        self.service = service
        self.config_dir = config_dir
        self.api_token = api_token
        self.operation_lock = threading.RLock()
        self.last_index_result: dict[str, object] = {}
        self.last_index_at: int | None = None
        self.index_error: str = ""
        self._rpc_methods = {
            name: getattr(self.service, name)
            for name in dir(self.service)
            if not name.startswith("_") and callable(getattr(self.service, name))
        }
        self._jobs_lock = threading.Lock()
        self._jobs: dict[str, dict[str, object]] = {}
        self._events_condition = threading.Condition()
        self._events: list[dict[str, object]] = []
        self._event_seq = 0

    def stop(self) -> None:
        """No-op retained for symmetric server shutdown handling."""
        return

    def run_startup_index(self) -> dict[str, object] | None:
        """Run one startup index pass so API data is fresh when the service comes up."""
        try:
            return self.run_index(trigger="startup")
        except Exception as exc:
            self.index_error = str(exc)
            return None

        self._watcher_thread = threading.Thread(target=_loop, name="klippervault-watcher", daemon=True)
        self._watcher_thread.start()

    def run_index(self, *, trigger: str) -> dict[str, object]:
        """Run one index operation with state tracking."""
        with self.operation_lock:
            result = self.service.index()
            self.last_index_result = {
                "trigger": trigger,
                **result,
            }
            self.last_index_at = int(time.time())
            self.index_error = ""
            self.publish_event(
                "index.completed",
                {
                    "trigger": trigger,
                    "result": result,
                    "last_index_at": self.last_index_at,
                },
            )
            return result

    def publish_event(self, event_type: str, payload: dict[str, object]) -> dict[str, object]:
        """Publish one server event for SSE subscribers and polling clients."""
        with self._events_condition:
            self._event_seq += 1
            event = {
                "id": self._event_seq,
                "type": str(event_type),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            }
            self._events.append(event)
            if len(self._events) > 500:
                self._events = self._events[-500:]
            self._events_condition.notify_all()
        return event

    def get_events_after(self, event_id: int) -> list[dict[str, object]]:
        """Return all events newer than a given event id."""
        with self._events_condition:
            return [event for event in self._events if int(event.get("id", 0)) > event_id]

    def wait_for_events_after(self, event_id: int, timeout: float) -> list[dict[str, object]]:
        """Wait for events newer than *event_id* or until timeout expires."""
        with self._events_condition:
            existing = [event for event in self._events if int(event.get("id", 0)) > event_id]
            if existing:
                return existing
            self._events_condition.wait(timeout=max(float(timeout), 0.1))
            return [event for event in self._events if int(event.get("id", 0)) > event_id]

    def create_index_job(self, *, trigger: str) -> dict[str, object]:
        """Schedule an index job and return lightweight job metadata."""
        return self.create_job(
            job_type="index",
            trigger=trigger,
            runner=lambda _report: self.run_index(trigger=trigger),
        )

    def create_job(
        self,
        *,
        job_type: str,
        trigger: str,
        runner,
    ) -> dict[str, object]:
        """Schedule one background job and return lightweight job metadata."""
        job_id = str(uuid4())
        now_ts = int(time.time())
        job: dict[str, object] = {
            "job_id": job_id,
            "type": str(job_type),
            "trigger": str(trigger),
            "status": "queued",
            "created_at": now_ts,
            "started_at": None,
            "finished_at": None,
            "result": {},
            "error": "",
            "progress_current": 0,
            "progress_total": 1,
        }
        with self._jobs_lock:
            self._jobs[job_id] = job
        self.publish_event(
            "job.queued",
            {
                "job_id": job_id,
                "job_type": str(job_type),
                "trigger": str(trigger),
            },
        )

        def _run() -> None:
            started_at = int(time.time())
            with self._jobs_lock:
                stored_job = self._jobs.get(job_id)
                if stored_job is None:
                    return
                stored_job["status"] = "running"
                stored_job["started_at"] = started_at
            self.publish_event(
                "job.running",
                {
                    "job_id": job_id,
                    "job_type": str(job_type),
                    "trigger": str(trigger),
                },
            )

            def _report_progress(current: int, total: int) -> None:
                with self._jobs_lock:
                    active_job = self._jobs.get(job_id)
                    if active_job is None:
                        return
                    active_job["progress_current"] = max(int(current), 0)
                    active_job["progress_total"] = max(int(total), 1)
                self.publish_event(
                    "job.progress",
                    {
                        "job_id": job_id,
                        "job_type": str(job_type),
                        "current": max(int(current), 0),
                        "total": max(int(total), 1),
                    },
                )

            try:
                result = runner(_report_progress)
                finished_at = int(time.time())
                with self._jobs_lock:
                    stored_job = self._jobs.get(job_id)
                    if stored_job is None:
                        return
                    stored_job["status"] = "completed"
                    stored_job["result"] = result
                    stored_job["finished_at"] = finished_at
                    stored_job["error"] = ""
                    stored_job["progress_current"] = stored_job.get("progress_total", 1)
                self.publish_event(
                    "job.completed",
                    {
                        "job_id": job_id,
                        "job_type": str(job_type),
                        "result": result,
                    },
                )
            except Exception as exc:
                finished_at = int(time.time())
                with self._jobs_lock:
                    stored_job = self._jobs.get(job_id)
                    if stored_job is None:
                        return
                    stored_job["status"] = "failed"
                    stored_job["result"] = {}
                    stored_job["finished_at"] = finished_at
                    stored_job["error"] = str(exc)
                self.publish_event(
                    "job.failed",
                    {
                        "job_id": job_id,
                        "job_type": str(job_type),
                        "error": str(exc),
                    },
                )

        threading.Thread(target=_run, name=f"klippervault-job-{job_id[:8]}", daemon=True).start()
        return job

    def get_job(self, job_id: str) -> dict[str, object] | None:
        """Return a copy of one job status payload."""
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            return dict(job) if isinstance(job, dict) else None

    def call_method(self, method: str, args: list[object], kwargs: dict[str, object]) -> object:
        """Invoke a whitelisted MacroGuiService method."""
        fn = self._rpc_methods.get(method)
        if fn is None:
            raise ValueError(f"unknown RPC method: {method}")

        if method in _MUTATING_METHOD_NAMES:
            with self.operation_lock:
                return fn(*args, **kwargs)
        return fn(*args, **kwargs)


class _HostApiHandler(BaseHTTPRequestHandler):
    """HTTP handler for the host API server."""

    server: "_HostApiServer"

    def log_message(self, format: str, *args: object) -> None:
        """Silence default stdlib request logging to keep service output concise."""
        return

    @property
    def state(self) -> _HostApiState:
        """Shortcut to shared server state."""
        return self.server.state

    def _read_json(self) -> dict[str, object]:
        """Read request JSON body into a dictionary."""
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request JSON must be an object")
        return payload

    def _write_json(self, status: int, payload: dict[str, object]) -> None:
        """Write JSON response payload."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _is_authorized(self) -> bool:
        """Validate optional bearer token auth."""
        required_token = self.state.api_token
        if not required_token:
            return True

        header = str(self.headers.get("Authorization", "")).strip()
        if not header.startswith("Bearer "):
            return False
        provided_token = header[len("Bearer ") :].strip()
        return bool(provided_token) and provided_token == required_token

    def _deny_unauthorized(self) -> None:
        """Send unauthorized response."""
        self._write_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})

    def do_GET(self) -> None:  # noqa: N802
        """Serve GET endpoints."""
        if not self._is_authorized():
            self._deny_unauthorized()
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/v1/health":
            self._write_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "klippervault-host-api",
                    "last_index_at": self.state.last_index_at,
                    "last_index_result": self.state.last_index_result,
                    "index_error": self.state.index_error,
                    "auto_index_mode": "startup-and-manual",
                },
            )
            return

        if path == "/api/v1/events":
            raw_last_event_id = str(query.get("last_event_id", ["0"])[0]).strip()
            try:
                last_event_id = max(int(raw_last_event_id), 0)
            except ValueError:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid last_event_id"})
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            # Send an initial hello event to make stream readiness explicit.
            initial_event = self.state.publish_event(
                "stream.hello",
                {
                    "service": "klippervault-host-api",
                },
            )
            try:
                if int(initial_event.get("id", 0)) > last_event_id:
                    self.wfile.write(f"id: {int(initial_event['id'])}\n".encode("utf-8"))
                    self.wfile.write(f"event: {str(initial_event['type'])}\n".encode("utf-8"))
                    self.wfile.write(f"data: {json.dumps(initial_event, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last_event_id = int(initial_event["id"])

                while True:
                    events = self.state.wait_for_events_after(last_event_id, timeout=15.0)
                    if not events:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue

                    for event in events:
                        event_id = int(event.get("id", 0))
                        self.wfile.write(f"id: {event_id}\n".encode("utf-8"))
                        self.wfile.write(f"event: {str(event.get('type', 'message'))}\n".encode("utf-8"))
                        self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        last_event_id = max(last_event_id, event_id)
            except (BrokenPipeError, ConnectionResetError):
                return
            return

        if path == "/api/v1/dashboard":
            limit = 500
            offset = 0
            try:
                limit_raw = query.get("limit", ["500"])[0]
                offset_raw = query.get("offset", ["0"])[0]
                limit = max(1, int(str(limit_raw)))
                offset = max(0, int(str(offset_raw)))
            except ValueError:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid limit/offset"})
                return

            stats, macros = self.state.service.load_dashboard(limit=limit, offset=offset)
            self._write_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "stats": stats,
                    "macros": macros,
                },
            )
            return

        if path == "/api/v1/macros/versions":
            file_path = str(query.get("file_path", [""])[0]).strip()
            macro_name = str(query.get("macro_name", [""])[0]).strip()
            if not file_path or not macro_name:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "file_path and macro_name are required"},
                )
                return
            versions = self.state.service.load_versions(file_path, macro_name)
            self._write_json(HTTPStatus.OK, {"ok": True, "versions": versions})
            return

        if path == "/api/v1/backups":
            backups = self.state.service.list_backups()
            self._write_json(HTTPStatus.OK, {"ok": True, "backups": backups})
            return

        if path.startswith("/api/v1/backups/") and path.endswith("/items"):
            backup_id_text = path.removeprefix("/api/v1/backups/").removesuffix("/items").strip("/")
            try:
                backup_id = int(backup_id_text)
            except ValueError:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid backup id"})
                return
            items = self.state.service.load_backup_contents(backup_id)
            self._write_json(HTTPStatus.OK, {"ok": True, "items": items})
            return

        if path == "/api/v1/printer/status":
            status = self.state.service.query_printer_status(timeout=1.5)
            self._write_json(HTTPStatus.OK, {"ok": True, "status": status})
            return

        if path == "/api/v1/duplicates":
            groups = self.state.service.list_duplicates()
            self._write_json(HTTPStatus.OK, {"ok": True, "groups": groups})
            return

        if path == "/api/v1/cfg-loading-overview":
            overview = self.state.service.load_cfg_loading_overview()
            self._write_json(HTTPStatus.OK, {"ok": True, "overview": overview})
            return

        if path.startswith("/api/v1/jobs/"):
            job_id = path.removeprefix("/api/v1/jobs/").strip()
            if not job_id:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "job id is required"})
                return
            job = self.state.get_job(job_id)
            if job is None:
                self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "job not found"})
                return
            self._write_json(HTTPStatus.OK, {"ok": True, "job": job})
            return

        self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        """Serve POST endpoints."""
        if not self._is_authorized():
            self._deny_unauthorized()
            return

        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/v1/index":
            trigger = "manual"
            try:
                payload = self._read_json()
                trigger = _payload_text(payload, "trigger", "manual") or "manual"
            except Exception:
                trigger = "manual"
            job = self.state.create_index_job(trigger=trigger)
            self._write_json(HTTPStatus.ACCEPTED, {"ok": True, "job": job})
            return

        if path == "/api/v1/jobs/online-check":
            try:
                payload = self._read_json()
                repo_url = _payload_text(payload, "repo_url")
                manifest_path = _payload_text(payload, "manifest_path", "updates/manifest.json") or "updates/manifest.json"
                repo_ref = _payload_text(payload, "repo_ref", "main") or "main"
                source_vendor = _payload_text(payload, "source_vendor")
                source_model = _payload_text(payload, "source_model")
                if not repo_url:
                    raise ValueError("repo_url is required")

                job = self.state.create_job(
                    job_type="online-check",
                    trigger="remote-gui",
                    runner=lambda report: self.state.service.check_online_updates(
                        repo_url=repo_url,
                        manifest_path=manifest_path,
                        repo_ref=repo_ref,
                        source_vendor=source_vendor,
                        source_model=source_model,
                        progress_callback=report,
                    ),
                )
            except Exception as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            self._write_json(HTTPStatus.ACCEPTED, {"ok": True, "job": job})
            return

        if path == "/api/v1/jobs/create-pr":
            try:
                payload = self._read_json()
                source_vendor = _payload_text(payload, "source_vendor")
                source_model = _payload_text(payload, "source_model")
                repo_url = _payload_text(payload, "repo_url")
                base_branch = _payload_text(payload, "base_branch")
                head_branch = _payload_text(payload, "head_branch")
                manifest_path = _payload_text(payload, "manifest_path", "updates/manifest.json") or "updates/manifest.json"
                github_token = _payload_text(payload, "github_token")
                pull_request_title = _payload_text(payload, "pull_request_title")
                pull_request_body = _payload_text(payload, "pull_request_body")
                if not repo_url:
                    raise ValueError("repo_url is required")

                job = self.state.create_job(
                    job_type="create-pr",
                    trigger="remote-gui",
                    runner=lambda report: self.state.service.create_online_update_pull_request(
                        source_vendor=source_vendor,
                        source_model=source_model,
                        repo_url=repo_url,
                        base_branch=base_branch,
                        head_branch=head_branch,
                        manifest_path=manifest_path,
                        github_token=github_token,
                        pull_request_title=pull_request_title,
                        pull_request_body=pull_request_body,
                        progress_callback=report,
                    ),
                )
            except Exception as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            self._write_json(HTTPStatus.ACCEPTED, {"ok": True, "job": job})
            return

        if path.startswith("/api/v1/actions/"):
            action = path.removeprefix("/api/v1/actions/").strip()
            try:
                payload = self._read_json()
            except Exception as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            try:
                result = self._run_action(action, payload)
            except Exception as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            self.state.publish_event(
                "action.completed",
                {
                    "action": action,
                },
            )

            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
            return

        if path == "/api/v1/share/export":
            try:
                payload = self._read_json()
                identities_raw = _payload_list(payload, "identities")
                identities: list[tuple[str, str]] = []
                for item in identities_raw:
                    if not isinstance(item, (list, tuple)) or len(item) != 2:
                        continue
                    file_path = str(item[0] or "").strip()
                    macro_name = str(item[1] or "").strip()
                    if file_path and macro_name:
                        identities.append((file_path, macro_name))

                source_vendor = _payload_text(payload, "source_vendor")
                source_model = _payload_text(payload, "source_model")
                share_payload = self.state.service.export_macro_share_payload_data(
                    identities=identities,
                    source_vendor=source_vendor,
                    source_model=source_model,
                )
                macros = share_payload.get("macros", [])
                macro_count = len(macros) if isinstance(macros, list) else 0
            except Exception as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            self._write_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "payload": share_payload,
                    "macro_count": macro_count,
                },
            )
            return

        if path == "/api/v1/share/import":
            try:
                payload = self._read_json()
                share_payload = _payload_dict(payload, "payload")
                target_vendor = _payload_text(payload, "target_vendor")
                target_model = _payload_text(payload, "target_model")
                result = self.state.service.import_macro_share_payload_data(
                    payload=share_payload,
                    target_vendor=target_vendor,
                    target_model=target_model,
                )
            except Exception as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            self._write_json(HTTPStatus.OK, {"ok": True, "result": result})
            return

        if path == "/api/v1/online-update/export-zip":
            try:
                payload = self._read_json()
                source_vendor = _payload_text(payload, "source_vendor")
                source_model = _payload_text(payload, "source_model")
                repo_url = _payload_text(payload, "repo_url")
                repo_ref = _payload_text(payload, "repo_ref", "main") or "main"
                manifest_path = _payload_text(payload, "manifest_path", "updates/manifest.json") or "updates/manifest.json"
                with tempfile.NamedTemporaryFile(prefix="klippervault-online-update-", suffix=".zip", delete=False) as temp_zip:
                    temp_zip_path = Path(temp_zip.name)

                export_result = self.state.service.export_online_update_repository_zip(
                    out_file=temp_zip_path,
                    source_vendor=source_vendor,
                    source_model=source_model,
                    repo_url=repo_url,
                    repo_ref=repo_ref,
                    manifest_path=manifest_path,
                )
                zip_bytes = temp_zip_path.read_bytes()
                encoded_zip = base64.b64encode(zip_bytes).decode("ascii")
            except Exception as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            finally:
                try:
                    if "temp_zip_path" in locals() and temp_zip_path.exists():
                        temp_zip_path.unlink(missing_ok=True)
                except Exception:
                    pass

            self._write_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "result": {
                        "macro_count": int(export_result.get("macro_count", 0) or 0),
                        "zip_base64": encoded_zip,
                    },
                },
            )
            return

        if path != "/api/v1/rpc":
            self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return

        try:
            payload = self._read_json()
            method = _payload_text(payload, "method")
            args = _payload_list(payload, "args")
            kwargs = _payload_dict(payload, "kwargs")
            result = self.state.call_method(method, args, kwargs)
        except Exception as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        self._write_json(HTTPStatus.OK, {"ok": True, "result": result})

    def _run_action(self, action: str, payload: dict[str, object]) -> object:
        """Execute one typed action endpoint."""
        handlers: dict[str, Any] = {
            "remove_deleted": lambda: self.state.service.remove_deleted(
                _payload_text(payload, "file_path"),
                _payload_text(payload, "macro_name"),
            ),
            "remove_inactive_version": lambda: self.state.service.remove_inactive_version(
                _payload_text(payload, "file_path"),
                _payload_text(payload, "macro_name"),
                _payload_int(payload, "version", 0),
            ),
            "purge_all_deleted": lambda: self.state.service.purge_all_deleted(),
            "restore_version": lambda: self.state.service.restore_version(
                _payload_text(payload, "file_path"),
                _payload_text(payload, "macro_name"),
                _payload_int(payload, "version", 0),
            ),
            "save_macro_editor_text": lambda: self.state.service.save_macro_editor_text(
                _payload_text(payload, "file_path"),
                _payload_text(payload, "macro_name"),
                _payload_text(payload, "section_text"),
            ),
            "delete_macro_source": lambda: self.state.service.delete_macro_source(
                _payload_text(payload, "file_path"),
                _payload_text(payload, "macro_name"),
            ),
            "resolve_duplicates": lambda: self.state.service.resolve_duplicates(
                keep_choices={
                    str(k): str(v)
                    for k, v in _payload_dict(payload, "keep_choices").items()
                },
                duplicate_groups=[
                    row
                    for row in _payload_list(payload, "duplicate_groups")
                    if isinstance(row, dict)
                ],
            ),
            "create_backup": lambda: self.state.service.create_backup(_payload_text(payload, "name")),
            "restore_backup": lambda: self.state.service.restore_backup(_payload_int(payload, "backup_id", 0)),
            "delete_backup": lambda: self.state.service.delete_backup(_payload_int(payload, "backup_id", 0)),
            "restart_klipper": lambda: self.state.service.restart_klipper(_payload_float(payload, "timeout", 3.0)),
            "reload_dynamic_macros": lambda: self.state.service.reload_dynamic_macros(_payload_float(payload, "timeout", 3.0)),
            "send_mainsail_notification": lambda: self.state.service.send_mainsail_notification(
                message=_payload_text(payload, "message"),
                title=_payload_text(payload, "title", "KlipperVault") or "KlipperVault",
                timeout=_payload_float(payload, "timeout", 3.0),
            ),
            "import_online_updates": lambda: self.state.service.import_online_updates(
                updates=[
                    row
                    for row in _payload_list(payload, "updates")
                    if isinstance(row, dict)
                ],
                activate_identities=[
                    str(value)
                    for value in _payload_list(payload, "activate_identities")
                ],
                repo_url=_payload_text(payload, "repo_url"),
                repo_ref=_payload_text(payload, "repo_ref"),
            ),
        }
        handler = handlers.get(action)
        if handler is None:
            raise ValueError(f"unknown action: {action}")
        return handler()


class _HostApiServer(ThreadingHTTPServer):
    """Threading HTTP server carrying shared host API state."""

    def __init__(self, server_address: tuple[str, int], state: _HostApiState) -> None:
        super().__init__(server_address, _HostApiHandler)
        self.state = state


def run_host_api_service(
    *,
    config_dir: Path | None = None,
    db_path: Path | None = None,
    bind_host: str | None = None,
    bind_port: int | None = None,
    api_token: str | None = None,
) -> None:
    """Run the host API service until interrupted."""
    resolved_config_dir = (config_dir or Path(DEFAULT_CONFIG_DIR)).expanduser().resolve()
    resolved_db_path = (db_path or Path(DEFAULT_DB_PATH)).expanduser().resolve()
    vault_cfg = load_or_create(resolved_config_dir)

    effective_bind_host = str(bind_host or vault_cfg.api_bind_host).strip() or "127.0.0.1"
    effective_bind_port = int(bind_port or vault_cfg.api_port)
    effective_api_token = str(api_token if api_token is not None else vault_cfg.api_token).strip()
    if _requires_api_token_for_bind(effective_bind_host) and not effective_api_token:
        raise ValueError("api_token is required when binding host API to non-localhost addresses")

    service = MacroGuiService(
        db_path=resolved_db_path,
        config_dir=resolved_config_dir,
        version_history_size=vault_cfg.version_history_size,
    )

    state = _HostApiState(
        service=service,
        config_dir=resolved_config_dir,
        api_token=effective_api_token,
    )
    state.run_startup_index()

    server = _HostApiServer((effective_bind_host, effective_bind_port), state)
    print(
        f"[KlipperVault] host-api listening on {effective_bind_host}:{effective_bind_port} (token={'set' if effective_api_token else 'disabled'})",
        flush=True,
    )

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        state.stop()
        server.server_close()
