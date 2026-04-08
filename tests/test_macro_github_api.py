import base64
from unittest.mock import patch

from klipper_macro_github_api import (
    create_pull_request,
    load_json_file_from_branch,
    parse_github_repo,
    upsert_text_file,
)


def test_parse_github_repo_extracts_owner_and_repo() -> None:
    owner, repo = parse_github_repo("https://github.com/example-org/example-repo")

    assert owner == "example-org"
    assert repo == "example-repo"


def test_load_json_file_from_branch_returns_none_for_missing_file() -> None:
    with patch("klipper_macro_github_api._request_json", return_value=(404, {})):
        payload = load_json_file_from_branch(
            repo_url="https://github.com/example-org/example-repo",
            token="token",
            branch="main",
            file_path="updates/manifest.json",
        )

    assert payload is None


def test_load_json_file_from_branch_decodes_base64_payload() -> None:
    encoded = base64.b64encode(b'{"manifest_version": "1", "macros": []}').decode("ascii")
    with patch(
        "klipper_macro_github_api._request_json",
        return_value=(200, {"encoding": "base64", "content": encoded}),
    ):
        payload = load_json_file_from_branch(
            repo_url="https://github.com/example-org/example-repo",
            token="token",
            branch="main",
            file_path="updates/manifest.json",
        )

    assert payload == {"manifest_version": "1", "macros": []}


def test_create_pull_request_returns_existing_when_duplicate_detected() -> None:
    with patch("klipper_macro_github_api._request_json", side_effect=RuntimeError("A pull request already exists")):
        with patch(
            "klipper_macro_github_api.get_open_pull_request_for_head",
            return_value={"number": 42, "html_url": "https://github.com/example-org/example-repo/pull/42"},
        ):
            result = create_pull_request(
                repo_url="https://github.com/example-org/example-repo",
                token="token",
                base_branch="main",
                head_branch="feature/branch",
                title="Update macros",
                body="Body",
            )

    assert result["ok"] is True
    assert result["existing"] is True
    assert result["number"] == 42


def test_upsert_text_file_updates_existing_file_with_sha() -> None:
    captured_bodies: list[dict[str, object]] = []

    def _fake_request_json(**kwargs):
        method = kwargs["method"]
        path = kwargs["path"]
        if method == "GET" and "contents" in path:
            return (200, {"sha": "abc123"})
        if method == "PUT":
            body = kwargs.get("body")
            assert isinstance(body, dict)
            captured_bodies.append(body)
            return (200, {"content": {"sha": "newsha"}, "commit": {"sha": "commitsha"}})
        raise AssertionError(f"Unexpected call: {method} {path}")

    with patch("klipper_macro_github_api._request_json", side_effect=_fake_request_json):
        result = upsert_text_file(
            repo_url="https://github.com/example-org/example-repo",
            token="token",
            branch="feature/branch",
            file_path="updates/manifest.json",
            content_text="{}",
            commit_message="Update manifest",
        )

    assert captured_bodies
    assert captured_bodies[0]["sha"] == "abc123"
    assert result["content"] == {"sha": "newsha"}
