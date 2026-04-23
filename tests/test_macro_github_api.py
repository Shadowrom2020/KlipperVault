import base64
from unittest.mock import patch

from klipper_macro_github_api import (
    _git_blob_sha,
    commit_changed_text_files,
    create_branch,
    create_pull_request,
    get_open_pull_request_for_head,
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


def test_create_branch_returns_already_exists_when_reference_conflicts() -> None:
    with patch("klipper_macro_github_api.get_branch_sha", return_value="base123"):
        with patch("klipper_macro_github_api._request_json", side_effect=RuntimeError("Reference already exists")):
            result = create_branch(
                repo_url="https://github.com/example-org/example-repo",
                token="token",
                base_branch="main",
                head_branch="feature/branch",
            )

    assert result["created"] is False
    assert result["already_exists"] is True
    assert result["base_sha"] == "base123"
    assert result["head_branch"] == "feature/branch"


def test_get_open_pull_request_for_head_returns_first_open_match() -> None:
    pulls_payload = [
        {"number": 7, "html_url": "https://github.com/example-org/example-repo/pull/7"},
        {"number": 8, "html_url": "https://github.com/example-org/example-repo/pull/8"},
    ]
    with patch("klipper_macro_github_api._request_json", return_value=(200, pulls_payload)):
        result = get_open_pull_request_for_head(
            repo_url="https://github.com/example-org/example-repo",
            token="token",
            head_branch="feature/branch",
        )

    assert result == pulls_payload[0]


def test_load_json_file_from_branch_rejects_non_object_root() -> None:
    encoded = base64.b64encode(b"[]").decode("ascii")
    with patch(
        "klipper_macro_github_api._request_json",
        return_value=(200, {"encoding": "base64", "content": encoded}),
    ):
        try:
            load_json_file_from_branch(
                repo_url="https://github.com/example-org/example-repo",
                token="token",
                branch="main",
                file_path="updates/manifest.json",
            )
        except RuntimeError as exc:
            assert "root must be an object" in str(exc)
        else:
            raise AssertionError("Expected RuntimeError for non-object manifest root")


def test_commit_changed_text_files_returns_noop_when_content_matches_head() -> None:
    unchanged_content = "{}"
    existing_sha = _git_blob_sha(unchanged_content)
    progress: list[tuple[int, int]] = []

    with patch("klipper_macro_github_api.get_branch_sha", return_value="head123"):
        with patch("klipper_macro_github_api._get_commit", return_value={"tree": {"sha": "tree123"}}):
            with patch(
                "klipper_macro_github_api._get_tree_blob_shas",
                return_value={"updates/manifest.json": existing_sha},
            ):
                result = commit_changed_text_files(
                    repo_url="https://github.com/example-org/example-repo",
                    token="token",
                    branch="main",
                    files={"updates/manifest.json": unchanged_content},
                    deleted_files=[],
                    commit_message="No changes",
                    progress_callback=lambda c, t: progress.append((c, t)),
                )

    assert result["created"] is False
    assert result["changed_files"] == 0
    assert result["commit_sha"] == ""
    assert progress
    assert progress[-1][0] == progress[-1][1]


def test_commit_changed_text_files_commits_changed_and_deleted_files() -> None:
    with patch("klipper_macro_github_api.get_branch_sha", return_value="head123"):
        with patch("klipper_macro_github_api._get_commit", return_value={"tree": {"sha": "tree123"}}):
            with patch(
                "klipper_macro_github_api._get_tree_blob_shas",
                return_value={
                    "updates/manifest.json": "oldsha",
                    "updates/remove-me.json": "removeold",
                },
            ):
                with patch("klipper_macro_github_api._create_blob", return_value="blob123"):
                    with patch("klipper_macro_github_api._create_tree", return_value="tree456"):
                        with patch("klipper_macro_github_api._create_commit", return_value="commit789"):
                            with patch("klipper_macro_github_api._update_branch_ref") as update_ref:
                                result = commit_changed_text_files(
                                    repo_url="https://github.com/example-org/example-repo",
                                    token="token",
                                    branch="main",
                                    files={"updates/manifest.json": '{"manifest_version":"1"}'},
                                    deleted_files=["updates/remove-me.json"],
                                    commit_message="Apply updates",
                                )

    update_ref.assert_called_once()
    assert result["created"] is True
    assert result["changed_files"] == 2
    assert result["commit_sha"] == "commit789"
