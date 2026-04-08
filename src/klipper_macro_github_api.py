#!/usr/bin/env python3
# Copyright (C) 2026 Juergen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""GitHub API helpers for creating branch updates and pull requests."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Callable, Iterable, Mapping
from urllib.parse import quote, urlencode, urlparse

import httpx
from pydantic import BaseModel, field_validator
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


_API_HOST = "api.github.com"
_USER_AGENT = "KlipperVault/github-pr"
_API_BASE_URL = f"https://{_API_HOST}"


def _as_int(value: object) -> int:
    """Convert dynamic API values to int with fallback to zero."""
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
            return 0
    return 0


def _git_blob_sha(content_text: str) -> str:
    """Compute Git blob SHA for UTF-8 text content."""
    content_bytes = str(content_text or "").encode("utf-8")
    prefix = f"blob {len(content_bytes)}\0".encode("utf-8")
    # Intentional non-security SHA-1 usage: Git blob IDs are defined as SHA-1
    # over "blob <len>\0<content>" and must match Git's object model exactly.
    return hashlib.sha1(prefix + content_bytes, usedforsecurity=False).hexdigest()


# Pydantic models for GitHub API responses


class CommitResult(BaseModel):
    """Typed result from commit_changed_text_files operation."""

    changed_files: int
    commit_sha: str
    created: bool

    @field_validator("changed_files", mode="before")
    @classmethod
    def validate_changed_files(cls, v: object) -> int:
        """Ensure changed_files is a non-negative integer."""
        if isinstance(v, bool):
            value = int(v)
        elif isinstance(v, int):
            value = v
        elif isinstance(v, float):
            value = int(v)
        elif isinstance(v, str):
            try:
                value = int(v)
            except ValueError:
                value = 0
        else:
            value = 0
        if value < 0:
            raise ValueError("changed_files must be non-negative")
        return value

    @field_validator("commit_sha", mode="before")
    @classmethod
    def validate_commit_sha(cls, v: object) -> str:
        """Normalize commit SHA to string."""
        return str(v or "").strip()

    def as_dict(self) -> dict[str, object]:
        """Convert to legacy dictionary contract."""
        return {
            "changed_files": self.changed_files,
            "commit_sha": self.commit_sha,
            "created": self.created,
        }


class BranchCreationResult(BaseModel):
    """Typed result from create_branch operation."""

    created: bool
    already_exists: bool
    base_sha: str
    head_branch: str
    ref: str = ""

    @field_validator("base_sha", "head_branch", "ref", mode="before")
    @classmethod
    def normalize_text_fields(cls, v: object) -> str:
        """Normalize text fields to string."""
        return str(v or "").strip()

    def as_dict(self) -> dict[str, object]:
        """Convert to legacy dictionary contract."""
        result = {
            "created": self.created,
            "already_exists": self.already_exists,
            "base_sha": self.base_sha,
            "head_branch": self.head_branch,
        }
        if self.ref:
            result["ref"] = self.ref
        return result


class CreatePullRequestResult(BaseModel):
    """Typed result from create_pull_request operation."""

    ok: bool
    existing: bool
    number: int
    url: str
    head_branch: str

    @field_validator("number", mode="before")
    @classmethod
    def validate_pr_number(cls, v: object) -> int:
        """Ensure number is a non-negative integer."""
        if isinstance(v, bool):
            value = int(v)
        elif isinstance(v, int):
            value = v
        elif isinstance(v, float):
            value = int(v)
        elif isinstance(v, str):
            try:
                value = int(v)
            except ValueError:
                value = 0
        else:
            value = 0
        if value < 0:
            raise ValueError("PR number must be non-negative")
        return value

    @field_validator("url", "head_branch", mode="before")
    @classmethod
    def normalize_url_and_branch(cls, v: object) -> str:
        """Normalize URL and branch name to string."""
        return str(v or "").strip()

    def as_dict(self) -> dict[str, object]:
        """Convert to legacy dictionary contract."""
        return {
            "ok": self.ok,
            "existing": self.existing,
            "number": self.number,
            "url": self.url,
            "head_branch": self.head_branch,
        }


def parse_github_repo(repo_url: str) -> tuple[str, str]:
    """Parse GitHub repository URL and return (owner, repo)."""
    parsed = urlparse(str(repo_url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("online update repository URL must use http/https")
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise ValueError("online update repository URL must point to github.com")

    path_parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(path_parts) < 2:
        raise ValueError("online update repository URL must include owner/repo")

    return path_parts[0], path_parts[1]


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.2, min=0.2, max=1.5),
    retry=retry_if_exception_type(httpx.RequestError),
)
def _perform_https_request(
    *,
    method: str,
    path: str,
    timeout: float,
    headers: dict[str, str],
    payload: str,
) -> tuple[int, str, str]:
    """Perform one HTTPS request with bounded retry for transient transport errors."""
    response = httpx.request(
        method=method.upper(),
        url=f"{_API_BASE_URL}{path}",
        content=payload,
        headers=headers,
        timeout=timeout,
    )
    return response.status_code, (response.reason_phrase or ""), response.text


def _request_json(
    *,
    method: str,
    path: str,
    token: str,
    timeout: float,
    body: Mapping[str, object] | None = None,
    accepted_status: Iterable[int] = (200,),
) -> tuple[int, object]:
    """Perform one authenticated GitHub API request and return status + parsed JSON body."""
    clean_token = str(token or "").strip()
    if not clean_token:
        raise ValueError("GitHub token is required")

    payload = ""
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {clean_token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": _USER_AGENT,
    }
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"

    status_code, _reason, raw_payload = _perform_https_request(
        method=method,
        path=path,
        timeout=timeout,
        headers=headers,
        payload=payload,
    )

    if raw_payload:
        try:
            parsed_payload: object = json.loads(raw_payload)
        except ValueError:
            parsed_payload = {}
    else:
        parsed_payload = {}

    if status_code not in set(accepted_status):
        if isinstance(parsed_payload, dict):
            message = str(parsed_payload.get("message", "")).strip()
        else:
            message = ""
        raise RuntimeError(message or f"GitHub API request failed with status {status_code}")

    return status_code, parsed_payload


def get_branch_sha(repo_url: str, token: str, branch: str, timeout: float = 8.0) -> str:
    """Return commit SHA for one branch."""
    owner, repo = parse_github_repo(repo_url)
    clean_branch = str(branch or "").strip()
    if not clean_branch:
        raise ValueError("base branch is required")

    path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/ref/heads/{quote(clean_branch, safe='')}"
    _status, payload = _request_json(method="GET", path=path, token=token, timeout=timeout, accepted_status=(200,))
    if not isinstance(payload, dict):
        raise RuntimeError(f"failed to resolve branch '{clean_branch}'")
    sha = str(payload.get("object", {}).get("sha", "")).strip()
    if not sha:
        raise RuntimeError(f"failed to resolve branch '{clean_branch}'")
    return sha


def _get_commit(repo_url: str, token: str, commit_sha: str, timeout: float = 8.0) -> dict[str, object]:
    """Load one commit object from GitHub git database API."""
    owner, repo = parse_github_repo(repo_url)
    clean_commit_sha = str(commit_sha or "").strip()
    if not clean_commit_sha:
        raise ValueError("commit sha is required")

    path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/commits/{quote(clean_commit_sha, safe='')}"
    _status, payload = _request_json(method="GET", path=path, token=token, timeout=timeout, accepted_status=(200,))
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected GitHub response while loading commit")
    return payload


def _get_tree_blob_shas(repo_url: str, token: str, tree_sha: str, timeout: float = 8.0) -> dict[str, str]:
    """Return mapping path -> blob sha for a tree (recursive)."""
    owner, repo = parse_github_repo(repo_url)
    clean_tree_sha = str(tree_sha or "").strip()
    if not clean_tree_sha:
        raise ValueError("tree sha is required")

    path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/trees/{quote(clean_tree_sha, safe='')}?recursive=1"
    _status, payload = _request_json(method="GET", path=path, token=token, timeout=timeout, accepted_status=(200,))
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected GitHub response while loading tree")

    tree_items = payload.get("tree", [])
    if not isinstance(tree_items, list):
        return {}

    out: dict[str, str] = {}
    for item in tree_items:
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "")) != "blob":
            continue
        path_value = str(item.get("path", "")).strip()
        sha_value = str(item.get("sha", "")).strip()
        if path_value and sha_value:
            out[path_value] = sha_value
    return out


def _create_blob(repo_url: str, token: str, content_text: str, timeout: float = 8.0) -> str:
    """Create one git blob and return its sha."""
    owner, repo = parse_github_repo(repo_url)
    path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/blobs"
    body: dict[str, object] = {
        "content": str(content_text or ""),
        "encoding": "utf-8",
    }
    _status, payload = _request_json(
        method="POST",
        path=path,
        token=token,
        timeout=timeout,
        body=body,
        accepted_status=(201,),
    )
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected GitHub response while creating blob")
    blob_sha = str(payload.get("sha", "")).strip()
    if not blob_sha:
        raise RuntimeError("failed to create blob")
    return blob_sha


def _create_tree(
    repo_url: str,
    token: str,
    base_tree_sha: str,
    entries: list[dict[str, object]],
    timeout: float = 8.0,
) -> str:
    """Create one git tree from base tree and file entries."""
    owner, repo = parse_github_repo(repo_url)
    path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/trees"
    body: dict[str, object] = {
        "base_tree": str(base_tree_sha or ""),
        "tree": entries,
    }
    _status, payload = _request_json(
        method="POST",
        path=path,
        token=token,
        timeout=timeout,
        body=body,
        accepted_status=(201,),
    )
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected GitHub response while creating tree")
    tree_sha = str(payload.get("sha", "")).strip()
    if not tree_sha:
        raise RuntimeError("failed to create tree")
    return tree_sha


def _create_commit(
    repo_url: str,
    token: str,
    message: str,
    tree_sha: str,
    parent_sha: str,
    timeout: float = 8.0,
) -> str:
    """Create one git commit and return its sha."""
    owner, repo = parse_github_repo(repo_url)
    path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/commits"
    body: dict[str, object] = {
        "message": str(message or "Update macros"),
        "tree": str(tree_sha or ""),
        "parents": [str(parent_sha or "")],
    }
    _status, payload = _request_json(
        method="POST",
        path=path,
        token=token,
        timeout=timeout,
        body=body,
        accepted_status=(201,),
    )
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected GitHub response while creating commit")
    commit_sha = str(payload.get("sha", "")).strip()
    if not commit_sha:
        raise RuntimeError("failed to create commit")
    return commit_sha


def _update_branch_ref(
    repo_url: str,
    token: str,
    branch: str,
    commit_sha: str,
    timeout: float = 8.0,
) -> None:
    """Move branch ref to a new commit sha."""
    owner, repo = parse_github_repo(repo_url)
    clean_branch = str(branch or "").strip()
    clean_commit_sha = str(commit_sha or "").strip()
    if not clean_branch:
        raise ValueError("branch is required")
    if not clean_commit_sha:
        raise ValueError("commit sha is required")

    path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/refs/heads/{quote(clean_branch, safe='')}"
    body: dict[str, object] = {
        "sha": clean_commit_sha,
        "force": False,
    }
    _request_json(
        method="PATCH",
        path=path,
        token=token,
        timeout=timeout,
        body=body,
        accepted_status=(200,),
    )


def commit_changed_text_files(
    *,
    repo_url: str,
    token: str,
    branch: str,
    files: dict[str, str],
    deleted_files: list[str] | None = None,
    commit_message: str,
    timeout: float = 8.0,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, object]:
    """Create one commit for changed/removed files only and return commit details."""
    clean_branch = str(branch or "").strip()
    if not clean_branch:
        raise ValueError("branch is required")
    clean_deleted_files = [
        str(path or "").strip().lstrip("/")
        for path in (deleted_files or [])
        if str(path or "").strip()
    ]
    if not files and not clean_deleted_files:
        result = CommitResult(changed_files=0, commit_sha="", created=False)
        return result.as_dict()

    def _report(current: int, total: int) -> None:
        if progress_callback is not None:
            progress_callback(max(0, int(current)), max(1, int(total)))

    total_steps = max(len(files) + len(clean_deleted_files), 1) + 6
    step = 0
    _report(step, total_steps)

    head_commit_sha = get_branch_sha(repo_url, token, clean_branch, timeout=timeout)
    step += 1
    _report(step, total_steps)

    head_commit = _get_commit(repo_url, token, head_commit_sha, timeout=timeout)
    tree_payload = head_commit.get("tree", {})
    if not isinstance(tree_payload, dict):
        raise RuntimeError("failed to resolve tree for branch head")
    base_tree_sha = str(tree_payload.get("sha", "")).strip()
    if not base_tree_sha:
        raise RuntimeError("failed to resolve tree for branch head")
    step += 1
    _report(step, total_steps)

    existing_blob_shas = _get_tree_blob_shas(repo_url, token, base_tree_sha, timeout=timeout)
    step += 1
    _report(step, total_steps)

    changed_items: list[tuple[str, str]] = []
    for file_path, content_text in sorted(files.items()):
        clean_path = str(file_path or "").strip().lstrip("/")
        if not clean_path:
            continue
        desired_sha = _git_blob_sha(content_text)
        current_sha = existing_blob_shas.get(clean_path, "")
        if desired_sha != current_sha:
            changed_items.append((clean_path, content_text))

    changed_write_paths = {path for path, _ in changed_items}
    changed_deletes: list[str] = []
    for file_path in sorted(set(clean_deleted_files)):
        if not file_path or file_path in changed_write_paths:
            continue
        if file_path in existing_blob_shas:
            changed_deletes.append(file_path)

    if not changed_items and not changed_deletes:
        _report(total_steps, total_steps)
        result = CommitResult(changed_files=0, commit_sha="", created=False)
        return result.as_dict()

    tree_entries: list[dict[str, object]] = []
    for file_path, content_text in changed_items:
        blob_sha = _create_blob(repo_url, token, content_text, timeout=timeout)
        tree_entries.append(
            {
                "path": file_path,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            }
        )
        step += 1
        _report(step, total_steps)

    for file_path in changed_deletes:
        tree_entries.append(
            {
                "path": file_path,
                "mode": "100644",
                "type": "blob",
                "sha": None,
            }
        )
        step += 1
        _report(step, total_steps)

    new_tree_sha = _create_tree(
        repo_url=repo_url,
        token=token,
        base_tree_sha=base_tree_sha,
        entries=tree_entries,
        timeout=timeout,
    )
    step += 1
    _report(step, total_steps)

    commit_sha = _create_commit(
        repo_url=repo_url,
        token=token,
        message=commit_message,
        tree_sha=new_tree_sha,
        parent_sha=head_commit_sha,
        timeout=timeout,
    )
    step += 1
    _report(step, total_steps)

    _update_branch_ref(
        repo_url=repo_url,
        token=token,
        branch=clean_branch,
        commit_sha=commit_sha,
        timeout=timeout,
    )
    _report(total_steps, total_steps)

    result = CommitResult(
        changed_files=len(changed_items) + len(changed_deletes),
        commit_sha=commit_sha,
        created=True,
    )
    return result.as_dict()


def create_branch(repo_url: str, token: str, base_branch: str, head_branch: str, timeout: float = 8.0) -> dict[str, object]:
    """Create a new branch from base branch SHA."""
    owner, repo = parse_github_repo(repo_url)
    clean_head = str(head_branch or "").strip()
    clean_base = str(base_branch or "").strip()
    if not clean_head:
        raise ValueError("head branch is required")
    if not clean_base:
        raise ValueError("base branch is required")
    if clean_head == clean_base:
        raise ValueError("head branch must differ from base branch")

    base_sha = get_branch_sha(repo_url, token, clean_base, timeout=timeout)
    path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/refs"
    body = {
        "ref": f"refs/heads/{clean_head}",
        "sha": base_sha,
    }

    try:
        _status, payload = _request_json(
            method="POST",
            path=path,
            token=token,
            timeout=timeout,
            body=body,
            accepted_status=(201,),
        )
        if not isinstance(payload, dict):
            payload = {}
    except RuntimeError as exc:
        message = str(exc)
        if "Reference already exists" in message:
            result = BranchCreationResult(
                created=False,
                already_exists=True,
                base_sha=base_sha,
                head_branch=clean_head,
            )
            return result.as_dict()
        raise

    result = BranchCreationResult(
        created=True,
        already_exists=False,
        base_sha=base_sha,
        head_branch=clean_head,
        ref=payload.get("ref", ""),
    )
    return result.as_dict()


def get_open_pull_request_for_head(
    repo_url: str,
    token: str,
    head_branch: str,
    timeout: float = 8.0,
) -> dict[str, object] | None:
    """Return open PR for owner:head branch when present."""
    owner, repo = parse_github_repo(repo_url)
    clean_head = str(head_branch or "").strip()
    if not clean_head:
        return None

    query = urlencode({"state": "open", "head": f"{owner}:{clean_head}"})
    path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls?{query}"
    _status, payload = _request_json(method="GET", path=path, token=token, timeout=timeout, accepted_status=(200,))

    if isinstance(payload, list):
        pulls = payload
    else:
        pulls = []
    if not pulls:
        return None

    first = pulls[0]
    if not isinstance(first, dict):
        return None
    return first


def load_json_file_from_branch(
    repo_url: str,
    token: str,
    branch: str,
    file_path: str,
    timeout: float = 8.0,
) -> dict[str, object] | None:
    """Load JSON file contents from one branch; return None when file is missing."""
    owner, repo = parse_github_repo(repo_url)
    clean_path = str(file_path or "").strip().lstrip("/")
    clean_branch = str(branch or "").strip()
    if not clean_path:
        raise ValueError("manifest path must not be empty")
    if not clean_branch:
        raise ValueError("branch is required")

    query = urlencode({"ref": clean_branch})
    path = (
        f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/contents/"
        f"{quote(clean_path, safe='/')}?{query}"
    )

    try:
        status_code, payload = _request_json(method="GET", path=path, token=token, timeout=timeout, accepted_status=(200, 404))
        if status_code == 404:
            return None
    except RuntimeError:
        raise

    if not isinstance(payload, dict):
        raise RuntimeError("invalid manifest payload in repository")

    encoding = str(payload.get("encoding", "")).strip().lower()
    encoded_content = str(payload.get("content", ""))
    if encoding != "base64" or not encoded_content:
        raise RuntimeError("invalid manifest payload in repository")

    try:
        decoded_bytes = base64.b64decode(encoded_content, validate=False)
        parsed = json.loads(decoded_bytes.decode("utf-8"))
    except (ValueError, OSError) as exc:
        raise RuntimeError(f"invalid manifest.json in repository: {exc}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError("invalid manifest.json in repository: root must be an object")
    return parsed


def upsert_text_file(
    *,
    repo_url: str,
    token: str,
    branch: str,
    file_path: str,
    content_text: str,
    commit_message: str,
    timeout: float = 8.0,
) -> dict[str, object]:
    """Create or update one repository file on target branch."""
    owner, repo = parse_github_repo(repo_url)
    clean_path = str(file_path or "").strip().lstrip("/")
    clean_branch = str(branch or "").strip()
    if not clean_path:
        raise ValueError("file path is required")
    if not clean_branch:
        raise ValueError("branch is required")

    query = urlencode({"ref": clean_branch})
    endpoint = (
        f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/contents/"
        f"{quote(clean_path, safe='/')}"
    )
    sha: str | None = None

    try:
        status_code, existing = _request_json(
            method="GET",
            path=f"{endpoint}?{query}",
            token=token,
            timeout=timeout,
            accepted_status=(200, 404),
        )
        if status_code == 200 and isinstance(existing, dict):
            existing_sha = str(existing.get("sha", "")).strip()
            if existing_sha:
                sha = existing_sha
    except RuntimeError:
        raise

    body: dict[str, object] = {
        "message": str(commit_message or "Update file"),
        "content": base64.b64encode(str(content_text or "").encode("utf-8")).decode("ascii"),
        "branch": clean_branch,
    }
    if sha:
        body["sha"] = sha

    _status, payload = _request_json(
        method="PUT",
        path=endpoint,
        token=token,
        timeout=timeout,
        body=body,
        accepted_status=(200, 201),
    )
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected GitHub response while updating file")
    return {
        "content": payload.get("content", {}),
        "commit": payload.get("commit", {}),
    }


def create_pull_request(
    *,
    repo_url: str,
    token: str,
    base_branch: str,
    head_branch: str,
    title: str,
    body: str,
    timeout: float = 8.0,
) -> dict[str, object]:
    """Create a pull request and return normalized metadata."""
    owner, repo = parse_github_repo(repo_url)
    clean_base = str(base_branch or "").strip()
    clean_head = str(head_branch or "").strip()
    clean_title = str(title or "").strip()
    clean_body = str(body or "").strip()
    if not clean_base:
        raise ValueError("base branch is required")
    if not clean_head:
        raise ValueError("head branch is required")
    if not clean_title:
        raise ValueError("pull request title is required")

    endpoint = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls"
    request_body = {
        "title": clean_title,
        "head": clean_head,
        "base": clean_base,
        "body": clean_body,
    }

    try:
        _status, payload = _request_json(
            method="POST",
            path=endpoint,
            token=token,
            timeout=timeout,
            body=request_body,
            accepted_status=(201,),
        )
    except RuntimeError as exc:
        message = str(exc)
        if "A pull request already exists" in message:
            existing = get_open_pull_request_for_head(repo_url, token, clean_head, timeout=timeout)
            if existing is not None:
                result = CreatePullRequestResult(
                    ok=True,
                    existing=True,
                    number=_as_int(existing.get("number", 0)),
                    url=str(existing.get("html_url", "")),
                    head_branch=clean_head,
                )
                return result.as_dict()
        raise

    if not isinstance(payload, dict):
        raise RuntimeError("unexpected GitHub response while creating pull request")

    result = CreatePullRequestResult(
        ok=True,
        existing=False,
        number=_as_int(payload.get("number", 0)),
        url=str(payload.get("html_url", "")),
        head_branch=clean_head,
    )
    return result.as_dict()
