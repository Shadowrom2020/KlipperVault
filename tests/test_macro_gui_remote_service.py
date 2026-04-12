from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from klipper_macro_gui_remote_service import RemoteMacroGuiService


class _Response:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = "{}"

    def json(self) -> dict[str, object]:
        return self._payload


def test_query_health_sends_bearer_token() -> None:
    service = RemoteMacroGuiService(
        base_url="http://printer-host.local:10091",
        api_token="secret-token",
    )
    captured: dict[str, object] = {}

    def _mock_request(*, method: str, url: str, headers: dict[str, str], json=None, params=None, timeout: float):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        return _Response({"ok": True, "service": "klippervault-host-api"})

    with patch("klipper_macro_gui_remote_service.httpx.request", side_effect=_mock_request):
        payload = service.query_health()

    assert payload["ok"] is True
    assert captured["method"] == "GET"
    assert str(captured["url"]).endswith("/api/v1/health")
    assert captured["headers"]["Authorization"] == "Bearer secret-token"


def test_load_latest_for_file_returns_first_version() -> None:
    service = RemoteMacroGuiService(base_url="http://printer-host.local:10091")

    def _mock_request(*, method: str, url: str, headers: dict[str, str], json=None, params=None, timeout: float):
        assert method == "GET"
        assert str(url).endswith("/api/v1/macros/versions")
        return _Response(
            {
                "ok": True,
                "versions": [
                    {"file_path": "printer.cfg", "macro_name": "PRINT_START", "version": 5},
                    {"file_path": "printer.cfg", "macro_name": "PRINT_START", "version": 4},
                ],
            }
        )

    with patch("klipper_macro_gui_remote_service.httpx.request", side_effect=_mock_request):
        latest = service.load_latest_for_file("PRINT_START", "printer.cfg")

    assert latest is not None
    assert latest["version"] == 5


def test_index_polls_until_completed() -> None:
    service = RemoteMacroGuiService(base_url="http://printer-host.local:10091", index_timeout=10.0)
    calls = {"jobs": 0}

    def _mock_request(*, method: str, url: str, headers: dict[str, str], json=None, params=None, timeout: float):
        path = str(url)
        if method == "POST" and path.endswith("/api/v1/index"):
            return _Response({"ok": True, "job": {"job_id": "job-1"}})
        if method == "GET" and path.endswith("/api/v1/jobs/job-1"):
            calls["jobs"] += 1
            if calls["jobs"] == 1:
                return _Response({"ok": True, "job": {"status": "running", "result": {}, "error": ""}})
            return _Response(
                {
                    "ok": True,
                    "job": {
                        "status": "completed",
                        "result": {"macros_inserted": 3, "cfg_files_scanned": 4},
                        "error": "",
                    },
                }
            )
        raise AssertionError(f"Unexpected request: {method} {path}")

    with patch("klipper_macro_gui_remote_service.httpx.request", side_effect=_mock_request):
        result = service.index()

    assert result["macros_inserted"] == 3
    assert calls["jobs"] >= 2


def test_check_online_updates_reports_progress() -> None:
    service = RemoteMacroGuiService(base_url="http://printer-host.local:10091", index_timeout=10.0)
    progress: list[tuple[int, int]] = []

    def _mock_request(*, method: str, url: str, headers: dict[str, str], json=None, params=None, timeout: float):
        path = str(url)
        if method == "POST" and path.endswith("/api/v1/jobs/online-check"):
            return _Response({"ok": True, "job": {"job_id": "job-online"}})
        if method == "GET" and path.endswith("/api/v1/jobs/job-online"):
            if not progress:
                return _Response(
                    {
                        "ok": True,
                        "job": {
                            "status": "running",
                            "progress_current": 1,
                            "progress_total": 3,
                            "result": {},
                            "error": "",
                        },
                    }
                )
            return _Response(
                {
                    "ok": True,
                    "job": {
                        "status": "completed",
                        "progress_current": 3,
                        "progress_total": 3,
                        "result": {"checked": 3, "changed": 1, "updates": []},
                        "error": "",
                    },
                }
            )
        raise AssertionError(f"Unexpected request: {method} {path}")

    with patch("klipper_macro_gui_remote_service.httpx.request", side_effect=_mock_request):
        result = service.check_online_updates(
            repo_url="https://github.com/example/repo",
            manifest_path="updates/manifest.json",
            repo_ref="main",
            source_vendor="Voron",
            source_model="Trident",
            progress_callback=lambda current, total: progress.append((current, total)),
        )

    assert result["checked"] == 3
    assert progress[0] == (1, 3)
    assert progress[-1] == (3, 3)


def test_export_online_update_repository_zip_writes_local_file() -> None:
    service = RemoteMacroGuiService(base_url="http://printer-host.local:10091")

    def _mock_request(*, method: str, url: str, headers: dict[str, str], json=None, params=None, timeout: float):
        assert method == "POST"
        assert str(url).endswith("/api/v1/online-update/export-zip")
        return _Response(
            {
                "ok": True,
                "result": {
                    "macro_count": 2,
                    "zip_base64": "UEsDBA==",
                },
            }
        )

    with TemporaryDirectory() as temp_dir:
        output_path = Path(temp_dir) / "export.zip"
        with patch("klipper_macro_gui_remote_service.httpx.request", side_effect=_mock_request):
            result = service.export_online_update_repository_zip(
                out_file=output_path,
                source_vendor="Voron",
                source_model="Trident",
                repo_url="https://github.com/example/repo",
                repo_ref="main",
                manifest_path="updates/manifest.json",
            )
            assert output_path.exists() is True
            assert output_path.read_bytes() == b"PK\x03\x04"

    assert result["macro_count"] == 2


def test_import_macro_share_file_posts_payload() -> None:
    service = RemoteMacroGuiService(base_url="http://printer-host.local:10091")
    captured: dict[str, object] = {}

    def _mock_request(*, method: str, url: str, headers: dict[str, str], json=None, params=None, timeout: float):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = json
        return _Response(
            {
                "ok": True,
                "result": {
                    "imported": 1,
                    "source_vendor": "Voron",
                    "source_model": "Trident",
                    "printer_matches": True,
                },
            }
        )

    with TemporaryDirectory() as temp_dir:
        share_file = Path(temp_dir) / "share.json"
        share_file.write_text('{"manifest_version": 1, "macros": []}', encoding="utf-8")
        with patch("klipper_macro_gui_remote_service.httpx.request", side_effect=_mock_request):
            result = service.import_macro_share_file(
                import_file=share_file,
                target_vendor="Voron",
                target_model="Trident",
            )

    assert result["imported"] == 1
    assert captured["method"] == "POST"
    assert str(captured["url"]).endswith("/api/v1/share/import")
    assert isinstance(captured["json"], dict)


def test_stream_events_parses_sse_payloads() -> None:
    service = RemoteMacroGuiService(base_url="http://printer-host.local:10091")
    received: list[dict[str, object]] = []
    stop_state = {"stop": False}

    class _FakeStreamResponse:
        status_code = 200

        @staticmethod
        def iter_lines():
            yield "id: 1"
            yield "event: job.completed"
            yield 'data: {"payload":{"ok":true}}'
            yield ""
            stop_state["stop"] = True

    class _FakeStreamContext:
        def __enter__(self):
            return _FakeStreamResponse()

        def __exit__(self, exc_type, exc, tb):
            return False

    def _mock_stream(method: str, url: str, headers: dict[str, str], params: dict[str, int], timeout=None):
        assert method == "GET"
        assert str(url).endswith("/api/v1/events")
        assert "last_event_id" in params
        return _FakeStreamContext()

    with patch("klipper_macro_gui_remote_service.httpx.stream", side_effect=_mock_stream):
        last_id = service.stream_events(
            on_event=lambda event: received.append(event),
            stop_requested=lambda: stop_state["stop"],
            last_event_id=0,
        )

    assert last_id == 1
    assert len(received) == 1
    assert received[0]["type"] == "job.completed"
    assert received[0]["payload"] == {"ok": True}
