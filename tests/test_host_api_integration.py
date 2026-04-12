import threading
import time
from pathlib import Path

import httpx

from klipper_vault_host_api import _HostApiServer, _HostApiState


class _IntegrationDummyService:
    def __init__(self) -> None:
        self._index_calls = 0

    def index(self) -> dict[str, int]:
        self._index_calls += 1
        return {"macros_inserted": self._index_calls, "cfg_files_scanned": 1}

    def load_dashboard(self, *, limit: int = 500, offset: int = 0):
        return (
            {"total_macros": 1, "distinct_cfg_files": 1, "deleted_macros": 0},
            [{"macro_name": "PRINT_START", "file_path": "printer.cfg", "version": 1}],
        )

    def load_versions(self, file_path: str, macro_name: str):
        return [{"file_path": file_path, "macro_name": macro_name, "version": 1}]

    def list_backups(self):
        return []

    def load_backup_contents(self, backup_id: int):
        return []

    def query_printer_status(self, timeout: float = 1.5):
        return {"connected": True, "state": "ready", "is_printing": False, "is_busy": False}

    def list_duplicates(self):
        return []

    def load_cfg_loading_overview(self):
        return {"klipper_count": 1, "klipper_macro_count": 1}


def _start_server(api_token: str) -> tuple[_HostApiServer, str, threading.Thread]:
    state = _HostApiState(
        service=_IntegrationDummyService(),
        config_dir=Path("/tmp"),
        api_token=api_token,
    )
    server = _HostApiServer(("127.0.0.1", 0), state)
    host, port = server.server_address
    base_url = f"http://{host}:{port}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    return server, base_url, thread


def test_host_api_http_endpoints_with_auth() -> None:
    server, base_url, _thread = _start_server(api_token="secret")
    try:
        unauthorized = httpx.get(f"{base_url}/api/v1/health", timeout=5.0)
        assert unauthorized.status_code == 401

        headers = {"Authorization": "Bearer secret"}
        health = httpx.get(f"{base_url}/api/v1/health", headers=headers, timeout=5.0)
        assert health.status_code == 200
        assert health.json()["ok"] is True

        dashboard = httpx.get(f"{base_url}/api/v1/dashboard", headers=headers, timeout=5.0)
        assert dashboard.status_code == 200
        body = dashboard.json()
        assert body["ok"] is True
        assert isinstance(body["macros"], list)
    finally:
        server.shutdown()
        server.server_close()


def test_host_api_index_job_and_event_stream() -> None:
    server, base_url, _thread = _start_server(api_token="")
    try:
        index_start = httpx.post(f"{base_url}/api/v1/index", json={"trigger": "test"}, timeout=5.0)
        assert index_start.status_code == 202
        job_id = str(index_start.json()["job"]["job_id"])

        for _ in range(60):
            job_response = httpx.get(f"{base_url}/api/v1/jobs/{job_id}", timeout=5.0)
            assert job_response.status_code == 200
            job_payload = job_response.json()["job"]
            if job_payload["status"] == "completed":
                assert int(job_payload["result"]["cfg_files_scanned"]) == 1
                break
            time.sleep(0.05)
        else:
            raise AssertionError("index job did not complete")

        with httpx.stream("GET", f"{base_url}/api/v1/events", params={"last_event_id": 0}, timeout=5.0) as stream:
            lines = []
            for line in stream.iter_lines():
                if line is None:
                    continue
                lines.append(str(line))
                if "event:" in str(line):
                    break
            assert any(text.startswith("event:") for text in lines)
    finally:
        server.shutdown()
        server.server_close()
