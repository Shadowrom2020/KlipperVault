#!/usr/bin/env python3
# Copyright (C) 2026 Juergen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared URL helpers for GitHub-hosted KlipperVault repositories."""

from __future__ import annotations

from urllib.parse import quote, urlparse


def parse_github_repo_url(
    repo_url: str,
    *,
    invalid_scheme_error: str,
    invalid_host_error: str,
    invalid_path_error: str,
) -> tuple[str, str]:
    """Parse one GitHub URL and return normalized (owner, repo)."""
    parsed = urlparse(str(repo_url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(invalid_scheme_error)
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise ValueError(invalid_host_error)

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise ValueError(invalid_path_error)

    owner = parts[0]
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]

    if not owner or not repo:
        raise ValueError(invalid_path_error)
    return owner, repo


def build_raw_githubusercontent_url(
    repo_url: str,
    *,
    repo_ref: str,
    file_path: str,
    invalid_scheme_error: str,
    invalid_host_error: str,
    invalid_path_error: str,
    empty_path_error: str,
) -> str:
    """Build one raw.githubusercontent URL for a file path in a GitHub repository."""
    owner, repo = parse_github_repo_url(
        repo_url,
        invalid_scheme_error=invalid_scheme_error,
        invalid_host_error=invalid_host_error,
        invalid_path_error=invalid_path_error,
    )

    clean_ref = str(repo_ref or "main").strip() or "main"
    clean_path = str(file_path or "").strip().lstrip("/")
    if not clean_path:
        raise ValueError(empty_path_error)

    encoded_path = "/".join(quote(part, safe="") for part in clean_path.split("/"))
    encoded_ref = quote(clean_ref, safe="")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{encoded_ref}/{encoded_path}"
