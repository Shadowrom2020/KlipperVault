from pathlib import Path
from unittest.mock import patch

from klipper_macro_gui_service import MacroGuiService


def _service() -> MacroGuiService:
    return MacroGuiService(
        db_path=Path("/tmp/test.db"),
        config_dir=Path("/tmp/config"),
        version_history_size=5,
    )


def test_create_online_update_pull_request_returns_existing_open_pr() -> None:
    service = _service()

    with patch(
        "klipper_macro_gui_service.get_open_pull_request_for_head",
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

    with patch("klipper_macro_gui_service.get_open_pull_request_for_head", return_value=None):
        with patch("klipper_macro_gui_service.load_json_file_from_branch", return_value={"manifest_version": "1", "macros": []}):
            with patch("klipper_macro_gui_service.build_online_update_repository_artifacts", return_value=artifacts):
                with patch("klipper_macro_gui_service.create_branch", return_value={"already_exists": False}):
                    with patch(
                        "klipper_macro_gui_service.commit_changed_text_files",
                        return_value={"changed_files": 2, "commit_sha": "abc123", "created": True},
                    ) as commit_mock:
                        with patch(
                            "klipper_macro_gui_service.create_pull_request",
                            return_value={"existing": False, "number": 99, "url": "https://github.com/example/repo/pull/99"},
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

    with patch("klipper_macro_gui_service.get_open_pull_request_for_head", return_value=None):
        with patch("klipper_macro_gui_service.load_json_file_from_branch", return_value={"manifest_version": "1", "macros": []}):
            with patch("klipper_macro_gui_service.build_online_update_repository_artifacts", return_value=artifacts):
                with patch("klipper_macro_gui_service.create_branch", return_value={"already_exists": False}):
                    with patch(
                        "klipper_macro_gui_service.commit_changed_text_files",
                        return_value={"changed_files": 0, "commit_sha": "", "created": False},
                    ):
                        with patch("klipper_macro_gui_service.create_pull_request") as pr_mock:
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

    assert pr_mock.call_count == 0
    assert result["created"] is False
    assert result["no_changes"] is True
    assert result["commit_count"] == 0
