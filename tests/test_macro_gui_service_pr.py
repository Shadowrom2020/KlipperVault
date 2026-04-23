from contextlib import ExitStack
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

from klipper_macro_gui_service import MacroGuiService


def _service() -> MacroGuiService:
    return MacroGuiService(
        db_path=Path("/tmp/test.db"),
        config_dir=Path("/tmp/config"),
        version_history_size=5,
        moonraker_base_url="http://moonraker.local:7125",
    )


def _create_pr(service: MacroGuiService, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "source_vendor": "Voron",
        "source_model": "Trident",
        "repo_url": "https://github.com/example/repo",
        "base_branch": "main",
        "head_branch": "feature/macro-update",
        "manifest_path": "updates/manifest.json",
        "github_token": "token",
        "pull_request_title": "Update macros",
        "pull_request_body": "Body",
    }
    payload.update(overrides)
    return service.create_online_update_pull_request(**cast(Any, payload))


def _run_create_pr_with_common_patches(
    service: MacroGuiService,
    *,
    artifacts: dict[str, object],
    changed_files: int,
) -> tuple[dict[str, object], Mock, Mock]:
    with ExitStack() as stack:
        stack.enter_context(patch("klipper_macro_service_online.get_open_pull_request_for_head", return_value=None))
        stack.enter_context(
            patch(
                "klipper_macro_service_online.load_json_file_from_branch",
                return_value={"manifest_version": "1", "macros": []},
            )
        )
        stack.enter_context(
            patch(
                "klipper_macro_service_online.build_online_update_repository_artifacts",
                return_value=artifacts,
            )
        )
        stack.enter_context(patch("klipper_macro_service_online.create_branch", return_value={"already_exists": False}))
        commit_mock = stack.enter_context(
            patch(
                "klipper_macro_service_online.commit_changed_text_files",
                return_value={"changed_files": changed_files, "commit_sha": "abc123", "created": changed_files > 0},
            )
        )
        pr_mock = stack.enter_context(
            patch(
                "klipper_macro_service_online.create_pull_request",
                return_value={"existing": False, "number": 99, "url": "https://github.com/example/repo/pull/99"},
            )
        )

        result = _create_pr(service)
        return result, cast(Mock, commit_mock), cast(Mock, pr_mock)


def test_create_online_update_pull_request_returns_existing_open_pr() -> None:
    service = _service()

    with patch(
        "klipper_macro_service_online.get_open_pull_request_for_head",
        return_value={"number": 17, "html_url": "https://github.com/example/repo/pull/17"},
    ):
        result = service.create_online_update_pull_request(
            source_vendor="Voron",
            source_model="Trident",
            repo_url="https://github.com/example/repo",
            base_branch="main",
            head_branch="feature/macro-update",
            manifest_path="updates/manifest.json",
            github_token="token",
            pull_request_title="Update macros",
            pull_request_body="Body",
        )

    assert result["created"] is False
    assert result["existing"] is True
    assert result["pull_request_number"] == 17


def test_create_online_update_pull_request_happy_path() -> None:
    service = _service()

    artifacts = {
        "manifest": {"manifest_version": "1", "macros": []},
        "manifest_path": "updates/manifest.json",
        "files_to_write": {"voron/trident/PRINT_START.json": "{}"},
        "files_to_delete": ["voron/trident/OLD_MACRO.json"],
        "macro_count": 1,
        "source_vendor": "voron",
        "source_model": "trident",
    }

    result, commit_mock, _pr_mock = _run_create_pr_with_common_patches(
        service,
        artifacts=artifacts,
        changed_files=2,
    )

    assert commit_mock.call_count == 1
    assert commit_mock.call_args.kwargs.get("deleted_files") == ["voron/trident/OLD_MACRO.json"]
    assert result["created"] is True
    assert result["existing"] is False
    assert result["pull_request_number"] == 99
    assert result["macro_count"] == 1
    assert result["commit_count"] == 1


def test_create_online_update_pull_request_no_changes_skips_pr() -> None:
    service = _service()

    artifacts = {
        "manifest": {"manifest_version": "1", "macros": []},
        "manifest_path": "updates/manifest.json",
        "files_to_write": {"voron/trident/PRINT_START.json": "{}"},
        "files_to_delete": [],
        "macro_count": 1,
        "source_vendor": "voron",
        "source_model": "trident",
    }

    result, _commit_mock, pr_mock = _run_create_pr_with_common_patches(
        service,
        artifacts=artifacts,
        changed_files=0,
    )

    assert pr_mock.call_count == 0
    assert result["created"] is False
    assert result["no_changes"] is True
    assert result["commit_count"] == 0


def test_send_mainsail_notification_posts_action_notification() -> None:
    service = _service()

    class _Response:
        status_code = 200
        reason_phrase = "OK"
        text = '{"result":"ok"}'

        @staticmethod
        def json() -> dict[str, str]:
            return {"result": "ok"}

    captured: dict[str, object] = {}

    def _mock_post(url: str, *, json: dict[str, str], timeout: float) -> _Response:
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _Response()

    with patch("klipper_macro_service_profiles.httpx.post", side_effect=_mock_post):
        result = service.send_mainsail_notification(message="2 updates available", title="KlipperVault")

    assert str(captured["url"]).endswith("/printer/gcode/script")
    assert 'action:notification KlipperVault: 2 updates available' in str(captured["json"])
    assert result["ok"] is True
    assert result["status"] == 200


def test_send_mainsail_notification_rejects_invalid_moonraker_url() -> None:
    service = MacroGuiService(
        db_path=Path("/tmp/test.db"),
        config_dir=Path("/tmp/config"),
        version_history_size=5,
        moonraker_base_url="ftp://127.0.0.1:7125",
    )

    try:
        service.send_mainsail_notification(message="test")
    except ValueError as exc:
        assert "Moonraker URL must use http/https." in str(exc)
    else:
        raise AssertionError("Expected ValueError for invalid Moonraker URL")


def test_restart_klipper_posts_restart_endpoint() -> None:
    service = _service()

    class _Response:
        status_code = 200
        reason_phrase = "OK"
        text = '{"result":"ok"}'

        @staticmethod
        def json() -> dict[str, str]:
            return {"result": "ok"}

    captured: dict[str, object] = {}

    def _mock_post(url: str, *, timeout: float) -> _Response:
        captured["url"] = url
        captured["timeout"] = timeout
        return _Response()

    with patch("klipper_macro_service_profiles.httpx.post", side_effect=_mock_post):
        result = service.restart_klipper()

    assert str(captured["url"]).endswith("/printer/restart")
    assert result["ok"] is True
    assert result["status"] == 200
    assert result["restart_method"] == "endpoint"


def test_restart_klipper_falls_back_to_gcode_restart_when_endpoint_fails() -> None:
    service = _service()

    class _Response:
        def __init__(self, status_code: int, text: str) -> None:
            self.status_code = status_code
            self.reason_phrase = "OK" if status_code < 400 else "Not Found"
            self.text = text

        def json(self) -> dict[str, object]:
            if self.status_code >= 400:
                return {"error": {"message": "Not Found"}}
            return {"result": "ok"}

    calls: list[tuple[str, object | None]] = []

    def _mock_post(url: str, *, timeout: float, json: dict[str, object] | None = None) -> _Response:
        _ = timeout
        calls.append((url, json))
        if str(url).endswith("/printer/restart"):
            return _Response(404, '{"error":{"message":"Not Found"}}')
        if str(url).endswith("/printer/gcode/script"):
            return _Response(200, '{"result":"ok"}')
        return _Response(500, "")

    with patch("klipper_macro_service_profiles.httpx.post", side_effect=_mock_post):
        result = service.restart_klipper()

    assert len(calls) == 2
    assert str(calls[0][0]).endswith("/printer/restart")
    assert str(calls[1][0]).endswith("/printer/gcode/script")
    assert calls[1][1] == {"script": "RESTART"}
    assert result["ok"] is True
    assert result["status"] == 200
    assert result["restart_method"] == "gcode_script"


def test_reload_dynamic_macros_posts_dynamic_macro_script() -> None:
    service = _service()

    class _Response:
        status_code = 200
        reason_phrase = "OK"
        text = '{"result":"ok"}'

        @staticmethod
        def json() -> dict[str, str]:
            return {"result": "ok"}

    captured: dict[str, object] = {}

    def _mock_post(url: str, *, json: dict[str, str], timeout: float) -> _Response:
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _Response()

    with patch("klipper_macro_service_profiles.httpx.post", side_effect=_mock_post):
        result = service.reload_dynamic_macros()

    assert str(captured["url"]).endswith("/printer/gcode/script")
    assert captured["json"] == {"script": "DYNAMIC_MACRO"}
    assert result["ok"] is True
    assert result["status"] == 200


def test_create_online_update_pull_request_rejects_invalid_files_to_write_payload() -> None:
    service = _service()

    artifacts = {
        "manifest": {"manifest_version": "1", "macros": []},
        "manifest_path": "updates/manifest.json",
        "files_to_write": [],
        "files_to_delete": [],
        "macro_count": 1,
    }

    with patch("klipper_macro_service_online.get_open_pull_request_for_head", return_value=None):
        with patch("klipper_macro_service_online.load_json_file_from_branch", return_value={"manifest_version": "1", "macros": []}):
            with patch("klipper_macro_service_online.build_online_update_repository_artifacts", return_value=artifacts):
                try:
                    _create_pr(service)
                except RuntimeError as exc:
                    assert "invalid export payload generated for pull request" in str(exc)
                else:
                    raise AssertionError("Expected RuntimeError for invalid files_to_write payload")


def test_create_online_update_pull_request_rejects_invalid_manifest_payload() -> None:
    service = _service()

    artifacts = {
        "manifest": [],
        "manifest_path": "updates/manifest.json",
        "files_to_write": {"voron/trident/PRINT_START.json": "{}"},
        "files_to_delete": [],
        "macro_count": 1,
    }

    with patch("klipper_macro_service_online.get_open_pull_request_for_head", return_value=None):
        with patch("klipper_macro_service_online.load_json_file_from_branch", return_value={"manifest_version": "1", "macros": []}):
            with patch("klipper_macro_service_online.build_online_update_repository_artifacts", return_value=artifacts):
                try:
                    _create_pr(service)
                except RuntimeError as exc:
                    assert "invalid manifest payload generated for pull request" in str(exc)
                else:
                    raise AssertionError("Expected RuntimeError for invalid manifest payload")


def test_import_online_updates_activates_selected_identities() -> None:
    service = _service()

    import_result = {
        "imported": 2,
        "imported_items": [
            {
                "identity": "printer.cfg::PRINT_START",
                "file_path": "printer.cfg",
                "macro_name": "PRINT_START",
                "version": 3,
            },
            {
                "identity": "macros.cfg::PRINT_END",
                "file_path": "macros.cfg",
                "macro_name": "PRINT_END",
                "version": 2,
            },
        ],
    }

    with patch("klipper_macro_service_online.import_online_macro_updates", return_value=import_result):
        with patch("klipper_macro_service_online.restore_macro_version") as restore_mock:
            result = service.import_online_updates(
                updates=[],
                activate_identities=["printer.cfg::PRINT_START"],
                repo_url="https://github.com/example/repo",
                repo_ref="main",
            )

    assert restore_mock.call_count == 1
    assert restore_mock.call_args.kwargs["file_path"] == "printer.cfg"
    assert restore_mock.call_args.kwargs["macro_name"] == "PRINT_START"
    assert restore_mock.call_args.kwargs["version"] == 3
    assert result["imported"] == 2
    assert result["activated"] == 1


def test_import_online_updates_skips_malformed_items() -> None:
    service = _service()

    import_result = {
        "imported": 3,
        "imported_items": [
            {
                "identity": "printer.cfg::PRINT_START",
                "file_path": "",
                "macro_name": "PRINT_START",
                "version": 3,
            },
            {
                "identity": "printer.cfg::PRINT_END",
                "file_path": "printer.cfg",
                "macro_name": "",
                "version": 2,
            },
            {
                "identity": "printer.cfg::PRIME_LINE",
                "file_path": "printer.cfg",
                "macro_name": "PRIME_LINE",
                "version": 0,
            },
        ],
    }

    with patch("klipper_macro_service_online.import_online_macro_updates", return_value=import_result):
        with patch("klipper_macro_service_online.restore_macro_version") as restore_mock:
            result = service.import_online_updates(
                updates=[],
                activate_identities=[
                    "printer.cfg::PRINT_START",
                    "printer.cfg::PRINT_END",
                    "printer.cfg::PRIME_LINE",
                ],
                repo_url="https://github.com/example/repo",
                repo_ref="main",
            )

    assert restore_mock.call_count == 0
    assert result["imported"] == 3
    assert result["activated"] == 0
