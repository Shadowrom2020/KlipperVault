#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""SSH/SFTP transport helpers for off-printer KlipperVault mode."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import PurePosixPath
import posixpath
import time

import paramiko


@dataclass
class SshConnectionConfig:
    """Connection settings resolved from active SSH profile + credential store."""

    host: str
    port: int
    username: str
    auth_mode: str
    secret_value: str
    timeout_seconds: float = 8.0


class SshTransport:
    """Lightweight SSH/SFTP wrapper for remote Klipper config access."""

    def __init__(self, config: SshConnectionConfig) -> None:
        self._config = config

    def _connect(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        auth_mode = str(self._config.auth_mode or "").strip().lower()
        secret = str(self._config.secret_value or "").strip()
        kwargs: dict[str, object] = {
            "hostname": self._config.host,
            "port": int(self._config.port),
            "username": self._config.username,
            "timeout": max(float(self._config.timeout_seconds), 1.0),
        }

        if auth_mode == "password":
            kwargs["password"] = secret
            kwargs["look_for_keys"] = False
            kwargs["allow_agent"] = False
        else:
            if not secret:
                raise ValueError("Missing SSH key path for key-based authentication")
            key_path = os.path.expanduser(secret)
            kwargs["key_filename"] = key_path
            kwargs["look_for_keys"] = False
            kwargs["allow_agent"] = False

        client.connect(**kwargs)
        return client

    def _safe_remote_path(self, remote_path: str) -> str:
        """Normalize remote path using POSIX semantics and basic home expansion."""
        raw_path = str(remote_path or "").strip()
        if raw_path == "~":
            raw_path = f"/home/{self._config.username}"
        elif raw_path.startswith("~/"):
            raw_path = f"/home/{self._config.username}/{raw_path[2:]}"

        normalized = str(PurePosixPath(raw_path or "/"))
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        return normalized

    def test_connection(self) -> dict[str, object]:
        """Open SSH session and run a minimal command for connectivity validation."""
        started = time.time()
        client = self._connect()
        try:
            stdin, stdout, stderr = client.exec_command("echo klippervault-ssh-ok")
            _ = stdin
            output = str(stdout.read().decode("utf-8", errors="ignore")).strip()
            error_output = str(stderr.read().decode("utf-8", errors="ignore")).strip()
            return {
                "ok": output == "klippervault-ssh-ok",
                "output": output,
                "error": error_output,
                "elapsed_ms": int((time.time() - started) * 1000),
            }
        finally:
            client.close()

    def list_cfg_files(self, remote_config_dir: str) -> list[str]:
        """List .cfg files below remote config directory using SFTP recursion."""
        client = self._connect()
        try:
            root = self._safe_remote_path(remote_config_dir)
            with client.open_sftp() as sftp:
                discovered: list[str] = []
                stack = [root]
                while stack:
                    current = stack.pop()
                    for entry in sftp.listdir_attr(current):
                        child_path = posixpath.join(current, entry.filename)
                        mode = int(entry.st_mode)
                        if mode & 0o170000 == 0o040000:
                            stack.append(child_path)
                            continue
                        if entry.filename.lower().endswith(".cfg"):
                            discovered.append(child_path)
                discovered.sort()
                return discovered
        finally:
            client.close()

    def read_text_file(self, remote_path: str) -> str:
        """Read one remote file as UTF-8 text (with replacement on decode)."""
        client = self._connect()
        try:
            with client.open_sftp() as sftp:
                with sftp.file(self._safe_remote_path(remote_path), "rb") as remote_file:
                    return remote_file.read().decode("utf-8", errors="replace")
        finally:
            client.close()

    def write_text_file_atomic(self, remote_path: str, text: str) -> None:
        """Write one remote file atomically using temp file + rename in same dir."""
        client = self._connect()
        try:
            target = self._safe_remote_path(remote_path)
            parent_dir = posixpath.dirname(target)
            temp_path = posixpath.join(parent_dir, f".kv_tmp_{int(time.time() * 1000)}")
            payload = text.encode("utf-8")
            with client.open_sftp() as sftp:
                with sftp.file(temp_path, "wb") as remote_file:
                    remote_file.write(payload)
                sftp.rename(temp_path, target)
        finally:
            client.close()

    def remove_file(self, remote_path: str) -> bool:
        """Remove one remote file and return True when deletion happened."""
        client = self._connect()
        try:
            target = self._safe_remote_path(remote_path)
            with client.open_sftp() as sftp:
                try:
                    sftp.remove(target)
                    return True
                except OSError:
                    return False
        finally:
            client.close()
