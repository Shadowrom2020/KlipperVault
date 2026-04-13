#!/usr/bin/env python3
# Copyright (C) 2026 Juergen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Config source abstraction for local cache and SSH-backed cfg trees."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from klipper_vault_ssh_transport import SshTransport


class ConfigSource:
	"""Minimal read/write contract for cfg trees."""

	def list_cfg_files(self) -> list[str]:
		"""Return all cfg files as source-relative POSIX paths."""
		raise NotImplementedError

	def read_text(self, relative_path: str) -> str:
		"""Read one cfg file from the source."""
		raise NotImplementedError

	def write_text(self, relative_path: str, text: str) -> None:
		"""Write one cfg file in the source."""
		raise NotImplementedError

	def remove(self, relative_path: str) -> bool:
		"""Remove one cfg file from the source."""
		raise NotImplementedError


@dataclass(frozen=True)
class LocalConfigSource(ConfigSource):
	"""Local filesystem-backed config source."""

	root_dir: Path

	def _safe_local_path(self, relative_path: str) -> Path:
		rel = str(relative_path or "").strip().replace("\\", "/").lstrip("/")
		target = (self.root_dir / rel).resolve()
		root = self.root_dir.resolve()
		target_str = str(target)
		root_str = str(root)
		if target_str != root_str and not target_str.startswith(root_str + "/"):
			raise ValueError(f"Path escapes config root: {relative_path}")
		return target

	def list_cfg_files(self) -> list[str]:
		root = self.root_dir.resolve()
		if not root.exists():
			return []
		files = [
			str(path.resolve().relative_to(root)).replace("\\", "/")
			for path in root.rglob("*.cfg")
			if path.is_file()
		]
		files.sort()
		return files

	def read_text(self, relative_path: str) -> str:
		return self._safe_local_path(relative_path).read_text(encoding="utf-8")

	def write_text(self, relative_path: str, text: str) -> None:
		target = self._safe_local_path(relative_path)
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_text(text, encoding="utf-8")

	def remove(self, relative_path: str) -> bool:
		target = self._safe_local_path(relative_path)
		if not target.exists():
			return False
		target.unlink()
		return True


@dataclass(frozen=True)
class SshConfigSource(ConfigSource):
	"""SSH/SFTP-backed config source with relative-path normalization."""

	transport: SshTransport
	remote_root: str

	def _root(self) -> PurePosixPath:
		return PurePosixPath(self.transport._safe_remote_path(self.remote_root))

	@staticmethod
	def _clean_relative_path(relative_path: str) -> PurePosixPath:
		rel = str(relative_path or "").strip().replace("\\", "/").lstrip("/")
		if rel in {"", "."}:
			raise ValueError("relative_path must not be empty")
		candidate = PurePosixPath(rel)
		if ".." in candidate.parts:
			raise ValueError(f"Path escapes config root: {relative_path}")
		return candidate

	def _relative_from_remote(self, remote_path: str) -> str:
		root = self._root()
		candidate = PurePosixPath(remote_path)
		try:
			return str(candidate.relative_to(root))
		except ValueError as exc:
			raise ValueError(f"Remote path escapes config root: {remote_path}") from exc

	def _remote_path(self, relative_path: str) -> str:
		root = self._root()
		rel = self._clean_relative_path(relative_path)
		return str(root / rel)

	def list_cfg_files(self) -> list[str]:
		remote_files = self.transport.list_cfg_files(str(self._root()))
		rel_files = [self._relative_from_remote(path) for path in remote_files]
		rel_files.sort()
		return rel_files

	def read_text(self, relative_path: str) -> str:
		return self.transport.read_text_file(self._remote_path(relative_path))

	def write_text(self, relative_path: str, text: str) -> None:
		self.transport.write_text_file_atomic(self._remote_path(relative_path), text)

	def remove(self, relative_path: str) -> bool:
		return self.transport.remove_file(self._remote_path(relative_path))