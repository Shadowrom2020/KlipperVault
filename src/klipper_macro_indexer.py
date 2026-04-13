#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Index Klipper macros from .cfg files into a SQLite database.

Designed for constrained systems (e.g. SBCs running 3D printer stacks):
- no external dependencies
- streaming line parsing
"""

from __future__ import annotations

import argparse
import configparser
import fnmatch
import glob
import hashlib
import json
import os
import posixpath
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from klipper_macro_backup import ensure_backup_schema
from klipper_vault_config_source import ConfigSource
from klipper_vault_db import open_sqlite_connection


@dataclass
class MacroRecord:
    file_path: str
    section_type: str
    macro_name: str
    line_number: int
    description: Optional[str]
    rename_existing: Optional[str]
    gcode: Optional[str]
    variables_json: str
    body_checksum: str
    is_loaded: bool = True
    is_dynamic: bool = False


_LATEST_VERSION_SUBQUERY = """
SELECT file_path, macro_name, MAX(version) AS max_version
FROM macros
GROUP BY file_path, macro_name
""".strip()

_LATEST_VERSION_WITH_COUNT_SUBQUERY = """
SELECT file_path, macro_name, MAX(version) AS max_version, COUNT(*) AS version_count
FROM macros
GROUP BY file_path, macro_name
""".strip()

_IGNORED_CFG_FILE_NAMES = {".dynamicmacros.cfg"}


def _should_index_cfg_file(path: Path) -> bool:
    """Return True when cfg file should be parsed by the indexer."""
    return path.name.lower() not in _IGNORED_CFG_FILE_NAMES


def _iter_cfg_glob_matches(path_expr: str, base_dir: Path) -> Iterable[Path]:
    """Expand one cfg path expression into matching existing .cfg files."""
    expr = str(path_expr or "").strip().strip('"').strip("'")
    if not expr:
        return

    cfg_glob = Path(expr)
    if cfg_glob.is_absolute():
        pattern = str(cfg_glob)
    else:
        # Preserve lexical paths (including symlink hops) so stored
        # file_path values remain rooted in config_dir when possible.
        pattern = str(base_dir / expr)

    for cfg_path in sorted(Path(p) for p in glob.glob(pattern)):
        if cfg_path.is_file() and cfg_path.suffix.lower() == ".cfg" and _should_index_cfg_file(cfg_path):
            yield cfg_path


def _iter_dynamicmacros_configs(value: str, base_dir: Path) -> Iterable[Path]:
    """Yield cfg files listed in a [dynamicmacros] `configs:` value."""
    for part in str(value or "").split(","):
        yield from _iter_cfg_glob_matches(part, base_dir)


def _iter_included_files(file_path: Path, config_dir: Path) -> Iterable[tuple[Path, bool]]:
    """Yield cfg files referenced by [include] and [dynamicmacros] sections.

    Each yielded tuple is ``(cfg_path, is_dynamic_source)`` where
    ``is_dynamic_source`` is True when the file came from a [dynamicmacros]
    ``configs:`` entry.
    """
    try:
        with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
            current_section_type: Optional[str] = None
            for raw_line in handle:
                line = raw_line.strip()
                if not (line.startswith("[") and line.endswith("]")):
                    if current_section_type != "dynamicmacros":
                        continue

                    pair = _parse_key_value(raw_line)
                    if not pair:
                        continue
                    key, value = pair
                    if key != "configs" or not value:
                        continue
                    for cfg in _iter_dynamicmacros_configs(value, file_path.parent):
                        yield cfg, True
                    continue

                section_name = line[1:-1].strip()
                section_type, section_arg = _section_parts(section_name)
                current_section_type = section_type

                if section_type != "include" or not section_arg:
                    continue

                for cfg in _iter_cfg_glob_matches(section_arg, file_path.parent):
                    yield cfg, False
    except FileNotFoundError:
        return


def _resolve_cfg_file_sets(config_dir: Path) -> tuple[List[Path], set[Path], set[Path]]:
    """Return (scan_order, loaded_set, dynamic_loaded_set) for cfg files.

    loaded_set contains files loaded by Klipper from printer.cfg traversal of
    [include ...] and [dynamicmacros] configs entries. scan_order also appends
    unreferenced cfg files for visibility.
    """
    root_cfg = config_dir / "printer.cfg"
    if not root_cfg.exists() or not root_cfg.is_file():
        all_cfg = sorted((p for p in _iter_cfg_files(config_dir)), key=lambda p: str(p))
        return all_cfg, {p.resolve() for p in all_cfg}, set()

    loaded: List[Path] = []
    visited: set[Path] = set()
    dynamic_loaded: set[Path] = set()

    def visit(path: Path) -> None:
        resolved = path.resolve()
        if resolved in visited:
            return
        visited.add(resolved)
        loaded.append(path)
        for included, is_dynamic_source in _iter_included_files(resolved, config_dir):
            if is_dynamic_source:
                dynamic_loaded.add(included.resolve())
            visit(included)

    visit(root_cfg)

    ordered: List[Path] = list(loaded)
    for cfg in sorted((p for p in _iter_cfg_files(config_dir)), key=lambda p: str(p)):
        if cfg.resolve() not in visited:
            ordered.append(cfg)
    return ordered, visited, dynamic_loaded


def get_cfg_load_order(config_dir: Path) -> List[Path]:
    """Resolve cfg load order starting from printer.cfg and following [include ...]."""
    ordered, _, _ = _resolve_cfg_file_sets(config_dir)
    return ordered


def _normalize_source_cfg_path(rel_path: str) -> str:
    """Normalize and validate one source-relative cfg path."""
    normalized = posixpath.normpath(str(rel_path or "").strip().replace("\\", "/").lstrip("/"))
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise ValueError(f"invalid cfg path from source: {rel_path}")
    if not normalized.lower().endswith(".cfg"):
        raise ValueError(f"cfg path must end with .cfg: {rel_path}")
    return normalized


def _list_cfg_files_from_source(config_source: ConfigSource) -> List[str]:
    """Return sorted, unique normalized .cfg paths from one config source."""
    normalized_paths: set[str] = set()
    for rel_path in config_source.list_cfg_files():
        rel_text = str(rel_path or "").strip()
        if not rel_text:
            continue
        if not rel_text.lower().endswith(".cfg"):
            continue
        normalized_paths.add(_normalize_source_cfg_path(rel_text))
    return sorted(normalized_paths)


def _resolve_source_include_paths(
    source_rel_path: str,
    include_spec: str,
    available_cfg_paths: set[str],
) -> List[str]:
    """Resolve one source include expression using source-relative glob matching."""
    clean_spec = str(include_spec or "").strip().strip('"').strip("'")
    if not clean_spec:
        return []

    if clean_spec.startswith("/"):
        include_pattern = posixpath.normpath(clean_spec.lstrip("/"))
    else:
        base_dir = posixpath.dirname(source_rel_path)
        include_pattern = posixpath.normpath(posixpath.join(base_dir, clean_spec))

    if include_pattern in {"", ".", ".."} or include_pattern.startswith("../"):
        raise FileNotFoundError(
            f"Include file '{include_pattern}' does not exist relative to '{source_rel_path}'"
        )

    matches = sorted(path for path in available_cfg_paths if fnmatch.fnmatch(path, include_pattern))
    if not matches and not glob.has_magic(include_pattern):
        raise FileNotFoundError(
            f"Include file '{include_pattern}' does not exist relative to '{source_rel_path}'"
        )
    return matches


def _iter_included_files_from_source(
    file_path: str,
    file_text: str,
    available_cfg_paths: set[str],
) -> Iterable[tuple[str, bool]]:
    """Yield cfg files referenced by [include] and [dynamicmacros] in source text."""
    current_section_type: Optional[str] = None
    for raw_line in str(file_text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1].strip()
            section_type, section_arg = _section_parts(section_name)
            current_section_type = section_type
            if section_type == "include" and section_arg:
                for cfg in _resolve_source_include_paths(file_path, section_arg, available_cfg_paths):
                    yield cfg, False
            continue

        if current_section_type != "dynamicmacros":
            continue

        pair = _parse_key_value(raw_line)
        if not pair:
            continue
        key, value = pair
        if key != "configs" or not value:
            continue
        for part in str(value).split(","):
            for cfg in _resolve_source_include_paths(file_path, part, available_cfg_paths):
                yield cfg, True


def _resolve_cfg_file_sets_from_source(config_source: ConfigSource) -> tuple[List[str], set[str], set[str]]:
    """Return (scan_order, loaded_set, dynamic_loaded_set) for cfg files from source."""
    all_cfg = _list_cfg_files_from_source(config_source)
    if not all_cfg:
        return [], set(), set()

    cfg_set = set(all_cfg)
    root_cfg = "printer.cfg"
    if root_cfg not in cfg_set:
        return all_cfg, set(all_cfg), set()

    loaded: List[str] = []
    visited: set[str] = set()
    dynamic_loaded: set[str] = set()

    def visit(rel_path: str) -> None:
        if rel_path in visited:
            return
        visited.add(rel_path)
        loaded.append(rel_path)
        file_text = config_source.read_text(rel_path)
        for included, is_dynamic_source in _iter_included_files_from_source(rel_path, file_text, cfg_set):
            if is_dynamic_source:
                dynamic_loaded.add(included)
            visit(included)

    visit(root_cfg)

    ordered = list(loaded)
    for cfg in all_cfg:
        if cfg not in visited:
            ordered.append(cfg)
    return ordered, visited, dynamic_loaded


def _iter_cfg_sections_from_text(file_text: str) -> Iterable[tuple[str, int, List[str]]]:
    """Yield cfg sections from raw cfg text as (section_name, line_number, raw_lines)."""
    current_section: Optional[str] = None
    current_section_line = 0
    current_body: List[str] = []

    for line_number, line in enumerate(str(file_text or "").splitlines(keepends=True), start=1):
        if _is_section_header_line(line):
            if current_section is not None:
                yield current_section, current_section_line, current_body
            current_section = line.strip()[1:-1].strip()
            current_section_line = line_number
            current_body = [line]
            continue

        if current_section is not None:
            current_body.append(line)

    if current_section is not None:
        yield current_section, current_section_line, current_body


def _parse_macros_from_source_text(
    rel_path: str,
    file_text: str,
    *,
    is_loaded: bool,
    is_dynamic: bool,
) -> List[MacroRecord]:
    """Parse all [gcode_macro ...] sections from source-provided cfg text."""
    results: List[MacroRecord] = []
    for section_name, section_line, body_lines in _iter_cfg_sections_from_text(file_text):
        record = _build_macro_record(
            file_path=Path(rel_path),
            base_dir=Path("."),
            section_name=section_name,
            section_line=section_line,
            body_lines=body_lines,
            is_loaded=is_loaded,
            is_dynamic=is_dynamic,
        )
        if record is not None:
            results.append(record)
    return results


def _collect_loaded_macro_records_in_order_from_source(
    config_source: ConfigSource,
    cfg_paths: set[str],
) -> tuple[List[MacroRecord], set[str], set[str]]:
    """Return loaded macro records in true Klipper parse order from a config source."""
    root_cfg = "printer.cfg"
    if root_cfg not in cfg_paths:
        return [], set(), set()

    ordered_records: List[MacroRecord] = []
    loaded_paths: set[str] = set()
    dynamic_paths: set[str] = set()

    def visit(rel_path: str, visiting: set[str], *, is_dynamic_source: bool = False) -> None:
        if rel_path in visiting:
            raise ValueError(f"Recursive include of config file '{rel_path}'")

        loaded_paths.add(rel_path)
        if is_dynamic_source:
            dynamic_paths.add(rel_path)

        visiting.add(rel_path)
        try:
            file_text = config_source.read_text(rel_path)
            for section_name, section_line, body_lines in _iter_cfg_sections_from_text(file_text):
                section_type, section_arg = _section_parts(section_name)

                if section_type == "include" and section_arg:
                    for included_path in _resolve_source_include_paths(rel_path, section_arg, cfg_paths):
                        visit(included_path, visiting)
                    continue

                if section_type == "dynamicmacros":
                    for raw_line in body_lines[1:]:
                        pair = _parse_key_value(raw_line)
                        if not pair:
                            continue
                        key, value = pair
                        if key != "configs" or not value:
                            continue
                        for part in str(value).split(","):
                            for included_path in _resolve_source_include_paths(rel_path, part, cfg_paths):
                                visit(included_path, visiting, is_dynamic_source=True)
                    continue

                record = _build_macro_record(
                    file_path=Path(rel_path),
                    base_dir=Path("."),
                    section_name=section_name,
                    section_line=section_line,
                    body_lines=body_lines,
                    is_loaded=True,
                    is_dynamic=is_dynamic_source,
                )
                if record is not None:
                    ordered_records.append(record)
        finally:
            visiting.remove(rel_path)

    visit(root_cfg, set())
    return ordered_records, loaded_paths, dynamic_paths


def _collect_macro_records_in_order_from_source(
    config_source: ConfigSource,
) -> tuple[List[MacroRecord], set[str], set[str], List[str]]:
    """Return all macro records from source with loaded macros in true parse order."""
    cfg_files = _list_cfg_files_from_source(config_source)
    cfg_set = set(cfg_files)
    loaded_records, loaded_paths, dynamic_paths = _collect_loaded_macro_records_in_order_from_source(
        config_source,
        cfg_set,
    )

    ordered_records = list(loaded_records)
    for rel_path in cfg_files:
        if rel_path in loaded_paths:
            continue
        file_text = config_source.read_text(rel_path)
        ordered_records.extend(
            _parse_macros_from_source_text(
                rel_path,
                file_text,
                is_loaded=False,
                is_dynamic=False,
            )
        )

    return ordered_records, loaded_paths, dynamic_paths, cfg_files


def _get_klipper_parse_order_from_source(config_source: ConfigSource, cfg_paths: set[str]) -> List[str]:
    """Return cfg parse order from source using Klipper include semantics."""
    root_cfg = "printer.cfg"
    if root_cfg not in cfg_paths:
        return []

    order: List[str] = []

    def _parse_file(rel_path: str, visiting: set[str]) -> None:
        if rel_path in visiting:
            raise ValueError(f"Recursive include of config file '{rel_path}'")
        visiting.add(rel_path)
        order.append(rel_path)

        try:
            data = config_source.read_text(rel_path)
        except Exception as exc:
            visiting.remove(rel_path)
            raise FileNotFoundError(f"Unable to open config file {rel_path}") from exc

        for line in data.split("\n"):
            hash_pos = line.find("#")
            if hash_pos >= 0:
                line = line[:hash_pos]
            section_match = configparser.RawConfigParser.SECTCRE.match(line)
            header = section_match and section_match.group("header")
            if header and header.startswith("include "):
                include_spec = header[8:].strip()
                for include_rel in _resolve_source_include_paths(rel_path, include_spec, cfg_paths):
                    _parse_file(include_rel, visiting)

        visiting.remove(rel_path)

    _parse_file(root_cfg, set())
    return order


def get_cfg_load_order_from_source(config_source: ConfigSource) -> List[Path]:
    """Resolve cfg load order for a generic config source."""
    ordered, _, _ = _resolve_cfg_file_sets_from_source(config_source)
    return [Path(path) for path in ordered]


def _get_klipper_parse_order(config_dir: Path) -> List[Path]:
    """Return cfg parse order using Klipper's include semantics from configfile.py."""
    root_cfg = config_dir / "printer.cfg"
    if not root_cfg.exists() or not root_cfg.is_file():
        return []

    order: List[Path] = []

    def _parse_file(filename: str, visiting: set[str]) -> None:
        path = os.path.abspath(filename)
        if path in visiting:
            raise ValueError(f"Recursive include of config file '{filename}'")
        visiting.add(path)
        order.append(Path(filename))

        try:
            data = Path(filename).read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            visiting.remove(path)
            raise FileNotFoundError(f"Unable to open config file {filename}") from exc

        for line in data.split("\n"):
            # Klipper strips only trailing '#...' prior to header detection.
            hash_pos = line.find("#")
            if hash_pos >= 0:
                line = line[:hash_pos]
            section_match = configparser.RawConfigParser.SECTCRE.match(line)
            header = section_match and section_match.group("header")
            if header and header.startswith("include "):
                include_spec = header[8:].strip()
                for include_filename in _resolve_klipper_include(filename, include_spec):
                    _parse_file(str(include_filename), visiting)

        visiting.remove(path)

    _parse_file(str(root_cfg), set())
    return order


def get_cfg_loading_overview(config_dir: Path) -> Dict[str, object]:
    """Return a simple overview of cfg and macro parse order for Klipper."""
    klipper_order = _get_klipper_parse_order(config_dir)
    loaded_macro_records, _, _ = _collect_loaded_macro_records_in_order(config_dir)

    rows: List[Dict[str, object]] = []
    for idx, cfg_path in enumerate(klipper_order, start=1):
        try:
            display_path = str(cfg_path.relative_to(config_dir))
        except ValueError:
            display_path = str(cfg_path)
        rows.append({"order": idx, "file_path": display_path})

    macro_rows: List[Dict[str, object]] = []
    for idx, record in enumerate(loaded_macro_records, start=1):
        macro_rows.append(
            {
                "order": idx,
                "macro_name": record.macro_name,
                "file_path": record.file_path,
                "line_number": record.line_number,
            }
        )

    return {
        "klipper_order": rows,
        "klipper_count": len(rows),
        "klipper_macro_order": macro_rows,
        "klipper_macro_count": len(macro_rows),
    }


def get_cfg_loading_overview_from_source(config_source: ConfigSource) -> Dict[str, object]:
    """Return cfg parse overview for a generic config source."""
    cfg_paths = set(_list_cfg_files_from_source(config_source))
    klipper_order = _get_klipper_parse_order_from_source(config_source, cfg_paths)
    loaded_macro_records, _, _ = _collect_loaded_macro_records_in_order_from_source(config_source, cfg_paths)

    rows: List[Dict[str, object]] = []
    for idx, rel_path in enumerate(klipper_order, start=1):
        rows.append({"order": idx, "file_path": rel_path})

    macro_rows: List[Dict[str, object]] = []
    for idx, record in enumerate(loaded_macro_records, start=1):
        macro_rows.append(
            {
                "order": idx,
                "macro_name": record.macro_name,
                "file_path": record.file_path,
                "line_number": record.line_number,
            }
        )

    return {
        "klipper_order": rows,
        "klipper_count": len(rows),
        "klipper_macro_order": macro_rows,
        "klipper_macro_count": len(macro_rows),
    }


def _iter_cfg_files(config_dir: Path) -> Iterable[Path]:
    """Yield all cfg files under config_dir recursively."""
    visited_dirs: set[Path] = set()
    for root, dirs, files in os.walk(config_dir, followlinks=True):
        root_path = Path(root)
        root_real = root_path.resolve()
        if root_real in visited_dirs:
            dirs[:] = []
            continue
        visited_dirs.add(root_real)

        # Prune directory recursion loops caused by symlink cycles.
        kept_dirs: List[str] = []
        for dir_name in sorted(dirs):
            dir_real = (root_path / dir_name).resolve()
            if dir_real in visited_dirs:
                continue
            kept_dirs.append(dir_name)
        dirs[:] = kept_dirs

        for file_name in sorted(files):
            cfg_path = Path(root) / file_name
            if file_name.lower().endswith(".cfg") and _should_index_cfg_file(cfg_path):
                yield cfg_path


def _parse_key_value(line: str) -> Optional[tuple[str, str]]:
    """Parse one cfg key/value line using ':' or '=' separators."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if ":" in line:
        key, value = line.split(":", 1)
        return key.strip().lower(), value.strip()
    if "=" in line:
        key, value = line.split("=", 1)
        return key.strip().lower(), value.strip()
    return None


def _section_parts(section_name: str) -> tuple[str, Optional[str]]:
    """Split section name into (section_type, optional_argument)."""
    parts = section_name.strip().split(None, 1)
    if not parts:
        return "", None
    section_type = parts[0].lower()
    section_arg = parts[1].strip() if len(parts) > 1 else None
    return section_type, section_arg


def _is_section_header_line(line: str) -> bool:
    """Return True only for real Klipper section header lines.

    Section headers must start at column 0 and be bracketed, e.g.
    [gcode_macro PRINT_START]. This avoids truncating gcode content that
    contains bracket-like text inside macro bodies.
    """
    if not line or line[0] in {" ", "\t"}:
        return False
    stripped = line.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return False
    inner = stripped[1:-1].strip()
    if not inner:
        return False
    if "[" in inner or "]" in inner:
        return False

    # Only treat lines as section headers when the section type looks like a
    # real Klipper section name, e.g. gcode_macro, delayed_gcode, printer.
    section_type, _ = _section_parts(inner)
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", section_type):
        return False
    return True


def _make_checksum(text: str) -> str:
    """Return SHA256 checksum for section body text."""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _normalize_gcode_for_match(gcode: Optional[str]) -> str:
    """Normalize gcode text for semantic version matching.

    Restore writes may normalize macro blocks to two-space indentation.
    Treat that representation as equivalent to previously indexed content.
    """
    if not gcode:
        return ""

    lines = str(gcode).splitlines()
    non_empty = [line for line in lines if line.strip()]
    if non_empty and all(line.startswith("  ") for line in non_empty):
        lines = [line[2:] if line.startswith("  ") else line for line in lines]
    return "\n".join(lines)


def _gcode_equivalent(left: Optional[str], right: Optional[str]) -> bool:
    """Return True when gcode differs only by normalization-safe indentation."""
    if left == right:
        return True
    return _normalize_gcode_for_match(left) == _normalize_gcode_for_match(right)


def _is_trailing_gcode_comment_or_blank(line: str) -> bool:
    """Return True for gcode lines that should be trimmed at block end."""
    stripped = line.strip()
    return not stripped or stripped.startswith("#") or stripped.startswith(";")


def _relativize_cfg_path(file_path: Path, base_dir: Path) -> str:
    """Return a stable stored path for one cfg file."""
    rel_candidate = os.path.relpath(str(file_path), str(base_dir))
    if rel_candidate == ".." or rel_candidate.startswith(f"..{os.sep}"):
        return str(file_path.resolve())
    return rel_candidate


def _build_macro_record(
    file_path: Path,
    base_dir: Path,
    section_name: str,
    section_line: int,
    body_lines: List[str],
    *,
    is_loaded: bool,
    is_dynamic: bool,
) -> Optional[MacroRecord]:
    """Build one MacroRecord from a parsed cfg section body."""
    section_type, section_arg = _section_parts(section_name)
    if section_type != "gcode_macro" or not section_arg:
        return None

    description: Optional[str] = None
    rename_existing: Optional[str] = None
    variables: Dict[str, str] = {}
    in_gcode_block = False
    gcode_lines: List[str] = []

    for line in body_lines[1:]:
        if in_gcode_block:
            gcode_lines.append(line.rstrip("\n"))
            continue

        pair = _parse_key_value(line)
        if not pair:
            continue

        key, value = pair
        if key == "gcode":
            in_gcode_block = True
            if value:
                gcode_lines.append(value)
        elif key == "description":
            description = value or None
        elif key == "rename_existing":
            rename_existing = value or None
        elif key.startswith("variable_"):
            variables[key[len("variable_") :]] = value

    while gcode_lines and _is_trailing_gcode_comment_or_blank(gcode_lines[-1]):
        gcode_lines.pop()
    gcode_text = "\n".join(gcode_lines).rstrip("\n") if gcode_lines else None

    return MacroRecord(
        file_path=_relativize_cfg_path(file_path, base_dir),
        section_type=section_type,
        macro_name=section_arg,
        line_number=section_line,
        description=description,
        rename_existing=rename_existing,
        gcode=gcode_text,
        variables_json=json.dumps(variables, separators=(",", ":"), sort_keys=True),
        body_checksum=_make_checksum("".join(body_lines)),
        is_loaded=is_loaded,
        is_dynamic=is_dynamic,
    )


def _iter_cfg_sections(file_path: Path) -> Iterable[tuple[str, int, List[str]]]:
    """Yield cfg sections as ``(section_name, line_number, raw_lines)``."""
    current_section: Optional[str] = None
    current_section_line = 0
    current_body: List[str] = []

    with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line_number, line in enumerate(handle, start=1):
            if _is_section_header_line(line):
                if current_section is not None:
                    yield current_section, current_section_line, current_body

                current_section = line.strip()[1:-1].strip()
                current_section_line = line_number
                current_body = [line]
                continue

            if current_section is not None:
                current_body.append(line)

    if current_section is not None:
        yield current_section, current_section_line, current_body


def _resolve_klipper_include(source_filename: str, include_spec: str) -> List[Path]:
    """Resolve one Klipper include expression to sorted cfg paths."""
    dirname = os.path.dirname(source_filename)
    include_glob = os.path.join(dirname, include_spec.strip())
    include_filenames = glob.glob(include_glob)
    if not include_filenames and not glob.has_magic(include_glob):
        raise FileNotFoundError(f"Include file '{include_glob}' does not exist")
    return [Path(name) for name in sorted(include_filenames)]


def _collect_loaded_macro_records_in_order(
    config_dir: Path,
) -> tuple[List[MacroRecord], set[Path], set[Path]]:
    """Return loaded macro records in true Klipper parse order."""
    root_cfg = config_dir / "printer.cfg"
    if not root_cfg.exists() or not root_cfg.is_file():
        return [], set(), set()

    ordered_records: List[MacroRecord] = []
    loaded_resolved: set[Path] = set()
    dynamic_resolved: set[Path] = set()

    def visit(file_path: Path, visiting: set[str], *, is_dynamic_source: bool = False) -> None:
        absolute_path = os.path.abspath(str(file_path))
        if absolute_path in visiting:
            raise ValueError(f"Recursive include of config file '{file_path}'")

        resolved_path = file_path.resolve()
        loaded_resolved.add(resolved_path)
        if is_dynamic_source:
            dynamic_resolved.add(resolved_path)

        visiting.add(absolute_path)
        try:
            for section_name, section_line, body_lines in _iter_cfg_sections(file_path):
                section_type, section_arg = _section_parts(section_name)

                if section_type == "include" and section_arg:
                    for included_path in _resolve_klipper_include(str(file_path), section_arg):
                        visit(included_path, visiting)
                    continue

                if section_type == "dynamicmacros":
                    for raw_line in body_lines[1:]:
                        pair = _parse_key_value(raw_line)
                        if not pair:
                            continue
                        key, value = pair
                        if key != "configs" or not value:
                            continue
                        for included_path in _iter_dynamicmacros_configs(value, file_path.parent):
                            visit(included_path, visiting, is_dynamic_source=True)
                    continue

                record = _build_macro_record(
                    file_path=file_path,
                    base_dir=config_dir,
                    section_name=section_name,
                    section_line=section_line,
                    body_lines=body_lines,
                    is_loaded=True,
                    is_dynamic=is_dynamic_source,
                )
                if record is not None:
                    ordered_records.append(record)
        finally:
            visiting.remove(absolute_path)

    visit(root_cfg, set())
    return ordered_records, loaded_resolved, dynamic_resolved


def _collect_macro_records_in_order(config_dir: Path) -> tuple[List[MacroRecord], set[Path], set[Path]]:
    """Return all macro records with loaded macros in true parse order."""
    loaded_records, loaded_resolved, dynamic_resolved = _collect_loaded_macro_records_in_order(config_dir)
    ordered_records = list(loaded_records)

    for cfg_path in sorted((p for p in _iter_cfg_files(config_dir)), key=lambda p: str(p)):
        if cfg_path.resolve() in loaded_resolved:
            continue
        ordered_records.extend(
            parse_macros_from_cfg(
                cfg_path,
                config_dir,
                is_loaded=False,
                is_dynamic=False,
            )
        )

    return ordered_records, loaded_resolved, dynamic_resolved


def _build_macro_load_order_map(config_dir: Path) -> Dict[tuple[str, str, int], int]:
    """Return global macro parse-order indices keyed by latest macro identity."""
    ordered_records, _, _ = _collect_macro_records_in_order(config_dir)
    order_map: Dict[tuple[str, str, int], int] = {}
    for idx, record in enumerate(ordered_records):
        order_map[(os.path.normpath(record.file_path), record.macro_name, record.line_number)] = idx
    return order_map


def _build_macro_load_order_map_from_source(config_source: ConfigSource) -> Dict[tuple[str, str, int], int]:
    """Return macro parse-order indices for a generic config source."""
    ordered_records, _, _, _ = _collect_macro_records_in_order_from_source(config_source)
    order_map: Dict[tuple[str, str, int], int] = {}
    for idx, record in enumerate(ordered_records):
        order_map[(os.path.normpath(record.file_path), record.macro_name, record.line_number)] = idx
    return order_map


def parse_macros_from_cfg(
    file_path: Path,
    base_dir: Path,
    *,
    is_loaded: bool = True,
    is_dynamic: bool = False,
) -> List[MacroRecord]:
    """Parse all [gcode_macro ...] sections from one cfg file."""
    results: List[MacroRecord] = []
    for section_name, section_line, body_lines in _iter_cfg_sections(file_path):
        record = _build_macro_record(
            file_path=file_path,
            base_dir=base_dir,
            section_name=section_name,
            section_line=section_line,
            body_lines=body_lines,
            is_loaded=is_loaded,
            is_dynamic=is_dynamic,
        )
        if record is not None:
            results.append(record)
    return results


# Default history limit. Also serves as the fallback for CLI invocations that
# don't load a VaultConfig; the GUI overrides this via vault_cfg.version_history_size.
_MAX_VERSIONS = 5


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Ensure main macro and backup schemas exist with migration safety."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(macros)")}

    # v1→v2 migration: the original schema lacked a `version` column.
    # Drop the table and let CREATE TABLE rebuild it from scratch.
    rebuilt = False
    if existing_cols and "version" not in existing_cols:
        conn.execute("DROP TABLE macros")
        conn.execute("DROP INDEX IF EXISTS idx_macros_name")
        rebuilt = True

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS macros (
            id          INTEGER PRIMARY KEY,
            printer_profile_id INTEGER NOT NULL DEFAULT 1,
            file_path   TEXT    NOT NULL,
            section_type TEXT   NOT NULL,
            macro_name  TEXT    NOT NULL,
            line_number INTEGER NOT NULL,
            description TEXT,
            rename_existing TEXT,
            gcode       TEXT,
            variables_json TEXT NOT NULL,
            body_checksum  TEXT NOT NULL,
            is_active   INTEGER NOT NULL DEFAULT 0,
            runtime_macro_name TEXT,
            renamed_from TEXT,
            is_deleted  INTEGER NOT NULL DEFAULT 0,
            is_loaded   INTEGER NOT NULL DEFAULT 1,
            is_dynamic  INTEGER NOT NULL DEFAULT 0,
            is_new      INTEGER NOT NULL DEFAULT 0,
            source_vendor TEXT NOT NULL DEFAULT '',
            source_model  TEXT NOT NULL DEFAULT '',
            import_source TEXT NOT NULL DEFAULT '',
            remote_repo_url TEXT,
            remote_ref      TEXT,
            remote_path     TEXT,
            remote_version  TEXT,
            version     INTEGER NOT NULL,
            indexed_at  INTEGER NOT NULL
        )
        """
    )

    # v2→v3: add is_active column to existing rows.
    # Skip when we just rebuilt from scratch — CREATE TABLE already includes it.
    if not rebuilt and existing_cols and "is_active" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0")
    # v3→v4: add is_deleted marker for macros removed from cfg files.
    if not rebuilt and existing_cols and "is_deleted" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0")
    # v4→v5: preserve rename_existing and runtime command names.
    if not rebuilt and existing_cols and "rename_existing" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN rename_existing TEXT")
    if not rebuilt and existing_cols and "runtime_macro_name" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN runtime_macro_name TEXT")
    if not rebuilt and existing_cols and "renamed_from" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN renamed_from TEXT")
    # v5→v6: track whether a macro lives in cfg files loaded by Klipper.
    if not rebuilt and existing_cols and "is_loaded" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN is_loaded INTEGER NOT NULL DEFAULT 1")
    # v6→v7: mark imported rows as new for quick discovery in the UI.
    if not rebuilt and existing_cols and "is_new" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN is_new INTEGER NOT NULL DEFAULT 0")
    # v7→v8: track whether a loaded macro came from [dynamicmacros] configs.
    if not rebuilt and existing_cols and "is_dynamic" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN is_dynamic INTEGER NOT NULL DEFAULT 0")
    # v8→v9: attach source identity/import metadata for share/online imports.
    if not rebuilt and existing_cols and "source_vendor" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN source_vendor TEXT NOT NULL DEFAULT ''")
    if not rebuilt and existing_cols and "source_model" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN source_model TEXT NOT NULL DEFAULT ''")
    if not rebuilt and existing_cols and "import_source" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN import_source TEXT NOT NULL DEFAULT ''")
    if not rebuilt and existing_cols and "remote_repo_url" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN remote_repo_url TEXT")
    if not rebuilt and existing_cols and "remote_ref" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN remote_ref TEXT")
    if not rebuilt and existing_cols and "remote_path" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN remote_path TEXT")
    if not rebuilt and existing_cols and "remote_version" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN remote_version TEXT")
    # v10→v11: store macro history scoped to one printer profile id.
    if not rebuilt and existing_cols and "printer_profile_id" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN printer_profile_id INTEGER NOT NULL DEFAULT 1")

    conn.execute(
        """
        UPDATE macros
        SET runtime_macro_name = COALESCE(NULLIF(TRIM(runtime_macro_name), ''), macro_name)
        WHERE runtime_macro_name IS NULL OR TRIM(runtime_macro_name) = ''
        """
    )
    conn.execute("DROP INDEX IF EXISTS idx_macros_version")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_macros_version_profile "
        "ON macros(printer_profile_id, file_path, macro_name, version)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_macros_name ON macros(macro_name)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_macros_printer_profile "
        "ON macros(printer_profile_id, macro_name, indexed_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_macros_source_identity "
        "ON macros(source_vendor, source_model, macro_name, indexed_at DESC)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS macro_online_update_state (
            id INTEGER PRIMARY KEY,
            source_vendor TEXT NOT NULL,
            source_model TEXT NOT NULL,
            macro_name TEXT NOT NULL,
            remote_repo_url TEXT NOT NULL,
            remote_ref TEXT,
            remote_path TEXT NOT NULL,
            remote_version TEXT,
            remote_checksum TEXT NOT NULL,
            update_available INTEGER NOT NULL DEFAULT 1,
            last_checked INTEGER NOT NULL,
            UNIQUE(source_vendor, source_model, macro_name, remote_repo_url, remote_path)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_macro_online_update_state_identity "
        "ON macro_online_update_state(source_vendor, source_model, macro_name)"
    )

    ensure_backup_schema(conn)


def _promote_existing_version_to_latest(
    conn: sqlite3.Connection,
    printer_profile_id: int,
    file_path: str,
    macro_name: str,
    from_version: int,
    to_version: int,
) -> None:
    """Move one historical version to latest without creating a new row.

    Keeps row count unchanged by shifting intermediate version numbers down.
    """
    if from_version >= to_version:
        return

    versions = conn.execute(
        """
        SELECT version
        FROM macros
        WHERE printer_profile_id = ? AND file_path = ? AND macro_name = ?
        """,
        (int(printer_profile_id), file_path, macro_name),
    ).fetchall()
    if not versions:
        return
    existing_versions = {int(row[0]) for row in versions}
    if from_version not in existing_versions or to_version not in existing_versions:
        return

    # Shift all rows for this macro to a disjoint temporary range, then remap
    # in a single statement. This avoids transient UNIQUE collisions.
    max_abs_version = max(abs(int(row[0])) for row in versions)
    offset = max_abs_version + 1000
    shifted_from = from_version + offset
    shifted_to = to_version + offset

    conn.execute("SAVEPOINT promote_version")
    try:
        conn.execute(
            """
            UPDATE macros
            SET version = version + ?
            WHERE printer_profile_id = ? AND file_path = ? AND macro_name = ?
            """,
            (offset, int(printer_profile_id), file_path, macro_name),
        )
        conn.execute(
            """
            UPDATE macros
            SET version = CASE
                WHEN version = ? THEN ?
                WHEN version > ? AND version <= ? THEN version - ? - 1
                ELSE version - ?
            END
            WHERE printer_profile_id = ? AND file_path = ? AND macro_name = ?
            """,
            (
                shifted_from,
                to_version,
                shifted_from,
                shifted_to,
                offset,
                offset,
                int(printer_profile_id),
                file_path,
                macro_name,
            ),
        )
        conn.execute("RELEASE SAVEPOINT promote_version")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO SAVEPOINT promote_version")
        conn.execute("RELEASE SAVEPOINT promote_version")
        raise


def index_macros(
    conn: sqlite3.Connection, records: List[MacroRecord], now_ts: int,
    printer_profile_id: int = 1,
    max_versions: int = _MAX_VERSIONS,
) -> tuple[int, int, int]:
    """Insert a new version only when parsed macro content truly changed.

    Returns (inserted, unchanged, dynamic_inserted).
    """
    inserted = 0
    unchanged = 0
    dynamic_inserted = 0
    for rec in records:
        versions = conn.execute(
            """
            SELECT
                version,
                description,
                rename_existing,
                gcode,
                variables_json,
                section_type,
                line_number,
                body_checksum
            FROM macros
            WHERE printer_profile_id = ? AND file_path = ? AND macro_name = ?
            ORDER BY version ASC
            """,
            (int(printer_profile_id), rec.file_path, rec.macro_name),
        ).fetchall()

        latest = versions[-1] if versions else None

        if latest:
            latest_version = int(latest[0])
            stored_description = latest[1]
            stored_rename_existing = latest[2]
            stored_gcode = latest[3]
            stored_variables_json = str(latest[4])
            stored_section_type = str(latest[5])
            stored_line_number = int(latest[6])
            stored_body_checksum = str(latest[7])
            parsed_description = rec.description
            parsed_rename_existing = rec.rename_existing
            parsed_gcode = rec.gcode
            parsed_variables_json = rec.variables_json

            # If the current cfg body exactly matches a historical version,
            # reactivate that version instead of keeping a newer equivalent row
            # or inserting a duplicate version.
            matched_checksum_version: Optional[int] = None
            for row in versions:
                if str(row[7]) == rec.body_checksum:
                    matched_checksum_version = int(row[0])
                    break

            if matched_checksum_version is not None and matched_checksum_version != latest_version:
                _promote_existing_version_to_latest(
                    conn=conn,
                    printer_profile_id=int(printer_profile_id),
                    file_path=rec.file_path,
                    macro_name=rec.macro_name,
                    from_version=matched_checksum_version,
                    to_version=latest_version,
                )
                conn.execute(
                    """
                    UPDATE macros
                    SET line_number = ?,
                        body_checksum = ?,
                        is_new = 0,
                        indexed_at = ?
                    WHERE file_path = ? AND macro_name = ? AND version = ?
                      AND printer_profile_id = ?
                    """,
                    (
                        rec.line_number,
                        rec.body_checksum,
                        now_ts,
                        rec.file_path,
                        rec.macro_name,
                        latest_version,
                        int(printer_profile_id),
                    ),
                )
                unchanged += 1
                continue

            content_changed = (
                stored_description != parsed_description
                or stored_rename_existing != parsed_rename_existing
                or not _gcode_equivalent(stored_gcode, parsed_gcode)
                or stored_variables_json != parsed_variables_json
                or stored_section_type != rec.section_type
            )

            if not content_changed:
                # Keep newest row metadata in sync without creating a new version.
                # This avoids version churn from non-semantic changes, while still
                # tracking the latest scan timestamp and parser normalization.
                if (
                    stored_body_checksum != rec.body_checksum
                    or stored_line_number != rec.line_number
                ):
                    conn.execute(
                        """
                        UPDATE macros
                        SET line_number = ?,
                            body_checksum = ?,
                            is_new = 0,
                            indexed_at = ?
                        WHERE file_path = ? AND macro_name = ? AND version = ?
                          AND printer_profile_id = ?
                        """,
                        (
                            rec.line_number,
                            rec.body_checksum,
                            now_ts,
                            rec.file_path,
                            rec.macro_name,
                            latest_version,
                            int(printer_profile_id),
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE macros
                        SET indexed_at = ?
                                    ,is_new = 0
                        WHERE file_path = ? AND macro_name = ? AND version = ?
                                                    AND printer_profile_id = ?
                        """,
                                                (now_ts, rec.file_path, rec.macro_name, latest_version, int(printer_profile_id)),
                    )
                unchanged += 1
                continue

            # If parsed content equals an older stored version, reactivate it by
            # promoting that row to latest instead of inserting a new version.
            matched_version: Optional[int] = None
            for row in versions:
                row_version = int(row[0])
                row_description = row[1]
                row_rename_existing = row[2]
                row_gcode = row[3]
                row_variables_json = str(row[4])
                row_section_type = str(row[5])
                if (
                    row_description == parsed_description
                    and row_rename_existing == parsed_rename_existing
                    and _gcode_equivalent(row_gcode, parsed_gcode)
                    and row_variables_json == parsed_variables_json
                    and row_section_type == rec.section_type
                ):
                    matched_version = row_version
                    break

            if matched_version is not None:
                _promote_existing_version_to_latest(
                    conn=conn,
                    printer_profile_id=int(printer_profile_id),
                    file_path=rec.file_path,
                    macro_name=rec.macro_name,
                    from_version=matched_version,
                    to_version=latest_version,
                )
                conn.execute(
                    """
                    UPDATE macros
                    SET line_number = ?,
                        body_checksum = ?,
                        is_new = 0,
                        indexed_at = ?
                    WHERE file_path = ? AND macro_name = ? AND version = ?
                      AND printer_profile_id = ?
                    """,
                    (
                        rec.line_number,
                        rec.body_checksum,
                        now_ts,
                        rec.file_path,
                        rec.macro_name,
                        latest_version,
                        int(printer_profile_id),
                    ),
                )
                unchanged += 1
                continue

            # Parsed content changed compared to the newest known version.
            # Store a new version row.

        new_version = (int(latest[0]) + 1) if latest else 1
        conn.execute(
            """
            INSERT INTO macros (
                printer_profile_id,
                file_path, section_type, macro_name, line_number,
                description, rename_existing, gcode, variables_json, body_checksum, is_active,
                runtime_macro_name, renamed_from, is_loaded, is_dynamic, is_new,
                version, indexed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(printer_profile_id),
                rec.file_path,
                rec.section_type,
                rec.macro_name,
                rec.line_number,
                rec.description,
                rec.rename_existing,
                rec.gcode,
                rec.variables_json,
                rec.body_checksum,
                0,
                rec.macro_name,
                None,
                1 if rec.is_loaded else 0,
                1 if rec.is_dynamic else 0,
                0,
                new_version,
                now_ts,
            ),
        )
        # Keep only the last max_versions versions; delete anything older.
        conn.execute(
            """
            DELETE FROM macros
                        WHERE printer_profile_id = ? AND file_path = ? AND macro_name = ?
              AND version <= (
                SELECT MAX(version) - ? FROM macros
                                WHERE printer_profile_id = ? AND file_path = ? AND macro_name = ?
              )
            """,
                        (
                                int(printer_profile_id),
                                rec.file_path,
                                rec.macro_name,
                                max_versions,
                                int(printer_profile_id),
                                rec.file_path,
                                rec.macro_name,
                        ),
        )
        inserted += 1
        if rec.is_dynamic:
            dynamic_inserted += 1

    # Mark latest rows as deleted when their macro is no longer present on disk,
    # and stamp whether the source cfg is currently loaded by Klipper.
    seen_identity_status = {
        (rec.file_path, rec.macro_name): (bool(rec.is_loaded), bool(rec.is_dynamic))
        for rec in records
    }
    latest_rows = conn.execute(
        """
        SELECT file_path, macro_name, MAX(version) AS max_version
        FROM macros
        WHERE printer_profile_id = ?
        GROUP BY file_path, macro_name
        """,
        (int(printer_profile_id),),
    ).fetchall()
    for file_path, macro_name, max_version in latest_rows:
        identity = (str(file_path), str(macro_name))
        is_seen = identity in seen_identity_status
        row_loaded, row_dynamic = seen_identity_status.get(identity, (False, False))
        is_loaded = 1 if row_loaded else 0
        is_dynamic = 1 if row_dynamic else 0
        conn.execute(
            """
            UPDATE macros
            SET is_deleted = CASE
                    WHEN ? = 1 THEN 0
                    WHEN is_new = 1 AND TRIM(import_source) != '' THEN 0
                    ELSE 1
                END,
                is_loaded = ?,
                is_dynamic = ?,
                is_new = CASE WHEN ? = 1 THEN 0 ELSE is_new END
            WHERE file_path = ? AND macro_name = ? AND version = ?
              AND printer_profile_id = ?
            """,
            (
                1 if is_seen else 0,
                is_loaded,
                is_dynamic,
                1 if is_seen else 0,
                str(file_path),
                str(macro_name),
                int(max_version),
                int(printer_profile_id),
            ),
        )

    # Determine active runtime command mapping by cfg loading order.
    # A later [gcode_macro X] overrides command X. With rename_existing: Y,
    # the previous X definition becomes callable as Y.
    runtime_target_by_name: Dict[str, tuple[str, str, str]] = {}
    for rec in records:
        if not rec.is_loaded:
            continue
        prev_target = runtime_target_by_name.get(rec.macro_name.lower())
        rename_target = str(rec.rename_existing or "").strip()
        if rename_target and prev_target is not None:
            runtime_target_by_name[rename_target.lower()] = (
                prev_target[0],
                prev_target[1],
                rename_target,
            )
        runtime_target_by_name[rec.macro_name.lower()] = (
            rec.file_path,
            rec.macro_name,
            rec.macro_name,
        )

    conn.execute(
        """
        UPDATE macros
        SET is_active = 0,
            runtime_macro_name = macro_name,
            renamed_from = NULL
        WHERE printer_profile_id = ?
        """
        ,(int(printer_profile_id),)
    )

    runtime_names_by_identity: Dict[tuple[str, str], list[str]] = {}
    for file_path, macro_name, runtime_name in runtime_target_by_name.values():
        key = (file_path, macro_name)
        runtime_names = runtime_names_by_identity.setdefault(key, [])
        if runtime_name not in runtime_names:
            runtime_names.append(runtime_name)

    for (file_path, macro_name), runtime_names in runtime_names_by_identity.items():
        selected_runtime = next(
            (name for name in runtime_names if name.lower() == macro_name.lower()),
            sorted(runtime_names, key=lambda name: name.lower())[0],
        )
        renamed_from = macro_name if selected_runtime.lower() != macro_name.lower() else None
        conn.execute(
            """
            UPDATE macros
            SET is_active = 1,
                runtime_macro_name = ?,
                renamed_from = ?
            WHERE file_path = ? AND macro_name = ?
                            AND is_deleted = 0
                            AND is_loaded = 1
                                                        AND printer_profile_id = ?
              AND version = (
                SELECT MAX(version)
                FROM macros
                                WHERE printer_profile_id = ? AND file_path = ? AND macro_name = ?
              )
            """,
                        (
                                selected_runtime,
                                renamed_from,
                                file_path,
                                macro_name,
                                int(printer_profile_id),
                                int(printer_profile_id),
                                file_path,
                                macro_name,
                        ),
        )

    return inserted, unchanged, dynamic_inserted


def run_indexing(
    config_dir: Path,
    db_path: Path,
    max_versions: int = _MAX_VERSIONS,
    printer_profile_id: int = 1,
    progress_callback: Callable[[int, int], None] | None = None,
) -> Dict[str, object]:
    """Index cfg files into SQLite and return a small run summary."""
    if progress_callback is not None:
        progress_callback(0, 3)

    if not config_dir.exists() or not config_dir.is_dir():
        raise FileNotFoundError(f"config directory not found: {config_dir}")

    cfg_files, _, _ = _resolve_cfg_file_sets(config_dir)
    if progress_callback is not None:
        progress_callback(1, 3)
    all_records, _, _ = _collect_macro_records_in_order(config_dir)
    if progress_callback is not None:
        progress_callback(2, 3)
    cfg_count = len(cfg_files)

    now_ts = int(time.time())
    with open_sqlite_connection(
        db_path,
        ensure_schema=ensure_schema,
        pragmas=("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"),
    ) as conn:
        inserted, unchanged, dynamic_inserted = index_macros(
            conn,
            all_records,
            now_ts,
            printer_profile_id=int(printer_profile_id),
            max_versions=max_versions,
        )
        conn.commit()

    if progress_callback is not None:
        progress_callback(3, 3)

    return {
        "cfg_files_scanned": cfg_count,
        "macros_inserted": inserted,
        "dynamic_macros_inserted": dynamic_inserted,
        "macros_unchanged": unchanged,
        "db_path": str(db_path),
    }


def run_indexing_from_source(
    config_source: ConfigSource,
    db_path: Path,
    max_versions: int = _MAX_VERSIONS,
    printer_profile_id: int = 1,
    progress_callback: Callable[[int, int], None] | None = None,
) -> Dict[str, object]:
    """Index cfg files from a generic config source into SQLite."""
    if progress_callback is not None:
        progress_callback(0, 4)

    cfg_files, _, _ = _resolve_cfg_file_sets_from_source(config_source)
    if progress_callback is not None:
        progress_callback(1, 4)

    all_records, _, _, _ = _collect_macro_records_in_order_from_source(config_source)
    if progress_callback is not None:
        progress_callback(2, 4)

    now_ts = int(time.time())
    with open_sqlite_connection(
        db_path,
        ensure_schema=ensure_schema,
        pragmas=("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"),
    ) as conn:
        inserted, unchanged, dynamic_inserted = index_macros(
            conn,
            all_records,
            now_ts,
            printer_profile_id=int(printer_profile_id),
            max_versions=max_versions,
        )
        conn.commit()

    if progress_callback is not None:
        progress_callback(4, 4)

    return {
        "cfg_files_scanned": len(cfg_files),
        "macros_inserted": inserted,
        "dynamic_macros_inserted": dynamic_inserted,
        "macros_unchanged": unchanged,
        "db_path": str(db_path),
    }


_SHARE_FORMAT = "klippervault.macro-share.v1"


def export_macro_share_payload(
    db_path: Path,
    identities: List[tuple[str, str]],
    source_vendor: str,
    source_model: str,
    now_ts: Optional[int] = None,
) -> Dict[str, object]:
    """Build a portable macro-share payload for selected latest macros."""
    selected = {(str(file_path), str(macro_name)) for file_path, macro_name in identities}
    if not selected:
        raise ValueError("no macros selected for export")

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        rows = conn.execute(
            f"""
            SELECT
                m.file_path,
                m.macro_name,
                m.section_type,
                m.description,
                m.rename_existing,
                m.gcode,
                m.variables_json
            FROM macros AS m
            INNER JOIN (
                {_LATEST_VERSION_SUBQUERY}
            ) AS latest
                ON m.file_path = latest.file_path
               AND m.macro_name = latest.macro_name
               AND m.version = latest.max_version
            ORDER BY m.macro_name COLLATE NOCASE ASC, m.file_path ASC
            """
        ).fetchall()

    exported_macros: List[Dict[str, object]] = []
    for file_path, macro_name, section_type, description, rename_existing, gcode, variables_json in rows:
        identity = (str(file_path), str(macro_name))
        if identity not in selected:
            continue
        section_text = macro_row_to_section_text(
            {
                "section_type": str(section_type or "gcode_macro"),
                "macro_name": str(macro_name),
                "description": description,
                "rename_existing": rename_existing,
                "gcode": gcode,
                "variables_json": str(variables_json),
            }
        )
        exported_macros.append(
            {
                "macro_name": str(macro_name),
                "source_file_path": str(file_path),
                "section_text": section_text,
            }
        )

    if not exported_macros:
        raise ValueError("none of the selected macros were found")

    ts = int(now_ts) if now_ts is not None else int(time.time())
    return {
        "format": _SHARE_FORMAT,
        "exported_at": ts,
        "source_printer": {
            "vendor": str(source_vendor or "").strip(),
            "model": str(source_model or "").strip(),
        },
        "macros": exported_macros,
    }


def _safe_import_file_path(source_file_path: str, macro_name: str) -> str:
    """Return default cfg path used for imported macros."""
    return "macros.cfg"


def _printer_cfg_includes_macros_cfg(printer_cfg: Path) -> bool:
    """Return True when printer.cfg already includes macros.cfg."""
    try:
        lines = printer_cfg.read_text(encoding="utf-8", errors="ignore").splitlines()
    except FileNotFoundError:
        return False

    for line in lines:
        stripped = line.strip()
        if not (stripped.startswith("[") and stripped.endswith("]")):
            continue
        inner = stripped[1:-1].strip()
        section_type, section_arg = _section_parts(inner)
        if section_type != "include" or not section_arg:
            continue
        include_expr = str(section_arg).strip().strip('"').strip("'")
        include_expr = include_expr.removeprefix("./")
        if Path(include_expr).name.lower() == "macros.cfg":
            return True
    return False


def _ensure_printer_cfg_includes_macros_cfg(config_dir: Path) -> None:
    """Ensure printer.cfg exists and includes macros.cfg."""
    printer_cfg = config_dir / "printer.cfg"
    if _printer_cfg_includes_macros_cfg(printer_cfg):
        return

    try:
        content = printer_cfg.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        content = ""

    content = content.rstrip("\n")
    include_line = "[include macros.cfg]"
    new_content = f"{content}\n\n{include_line}\n" if content else f"{include_line}\n"
    printer_cfg.parent.mkdir(parents=True, exist_ok=True)
    printer_cfg.write_text(new_content, encoding="utf-8")


def import_macro_share_payload(
    db_path: Path,
    payload: Dict[str, object],
    now_ts: Optional[int] = None,
    printer_profile_id: int = 1,
) -> Dict[str, object]:
    """Store imported macros as inactive latest rows marked as new."""
    if str(payload.get("format", "")).strip() != _SHARE_FORMAT:
        raise ValueError("unsupported macro share file format")

    macros_raw = payload.get("macros", [])
    if not isinstance(macros_raw, list) or not macros_raw:
        raise ValueError("macro share file contains no macros")

    imported_rows: List[tuple] = []
    ts = int(now_ts) if now_ts is not None else int(time.time())
    for item in macros_raw:
        if not isinstance(item, dict):
            continue
        section_text = str(item.get("section_text", ""))
        parsed = _parse_macro_section_text(section_text)
        macro_name = str(parsed.get("macro_name", "")).strip()
        if not macro_name:
            continue
        file_path = _safe_import_file_path(str(item.get("source_file_path", "")), macro_name)
        body_checksum = _make_checksum(str(parsed.get("section_text", section_text)))
        imported_rows.append(
            (
                file_path,
                str(parsed.get("section_type", "gcode_macro")),
                macro_name,
                1,
                parsed.get("description"),
                parsed.get("rename_existing"),
                parsed.get("gcode"),
                str(parsed.get("variables_json", "{}")),
                body_checksum,
                ts,
            )
        )

    if not imported_rows:
        raise ValueError("macro share file contains no valid gcode macros")

    source_printer = payload.get("source_printer", {})
    source_vendor = ""
    source_model = ""
    if isinstance(source_printer, dict):
        source_vendor = str(source_printer.get("vendor", "")).strip()
        source_model = str(source_printer.get("model", "")).strip()

    inserted = 0
    with open_sqlite_connection(
        db_path,
        ensure_schema=ensure_schema,
        pragmas=("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"),
    ) as conn:
        for (
            file_path,
            section_type,
            macro_name,
            line_number,
            description,
            rename_existing,
            gcode,
            variables_json,
            body_checksum,
            indexed_at,
        ) in imported_rows:
            latest_row = conn.execute(
                """
                SELECT MAX(version)
                FROM macros
                WHERE file_path = ? AND macro_name = ?
                """,
                (file_path, macro_name),
            ).fetchone()
            latest_version = int(latest_row[0]) if latest_row and latest_row[0] is not None else 0
            new_version = latest_version + 1
            conn.execute(
                """
                INSERT INTO macros (
                    printer_profile_id,
                    file_path,
                    section_type,
                    macro_name,
                    line_number,
                    description,
                    rename_existing,
                    gcode,
                    variables_json,
                    body_checksum,
                    is_active,
                    runtime_macro_name,
                    renamed_from,
                    is_deleted,
                    is_loaded,
                    is_dynamic,
                    is_new,
                    source_vendor,
                    source_model,
                    import_source,
                    version,
                    indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(printer_profile_id),
                    file_path,
                    section_type,
                    macro_name,
                    line_number,
                    description,
                    rename_existing,
                    gcode,
                    variables_json,
                    body_checksum,
                    0,
                    macro_name,
                    None,
                    0,
                    0,
                    0,
                    1,
                    source_vendor,
                    source_model,
                    "share",
                    new_version,
                    indexed_at,
                ),
            )
            inserted += 1
        conn.commit()

    return {
        "imported": inserted,
        "source_vendor": source_vendor,
        "source_model": source_model,
    }


def load_stats(db_path: Path, printer_profile_id: int | None = None) -> Dict[str, object]:
    """Return aggregate stats for the stats panel."""
    if not db_path.exists():
        return {
            "total_macros": 0,
            "deleted_macros": 0,
            "distinct_macro_names": 0,
            "distinct_cfg_files": 0,
            "latest_update_ts": None,
            "macros_per_file": [],
        }

    latest_subquery = _LATEST_VERSION_SUBQUERY
    latest_args: tuple[object, ...] = ()
    if printer_profile_id is not None:
        latest_subquery = """
        SELECT file_path, macro_name, MAX(version) AS max_version
        FROM macros
        WHERE printer_profile_id = ?
        GROUP BY file_path, macro_name
        """.strip()
        latest_args = (int(printer_profile_id),)

    row_filter = ""
    row_args: tuple[object, ...] = ()
    if printer_profile_id is not None:
        row_filter = " AND m.printer_profile_id = ?"
        row_args = (int(printer_profile_id),)

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        # Count distinct latest, non-deleted macros to reflect current cfg state.
        total_macros = int(conn.execute(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT m.file_path, m.macro_name
                FROM macros AS m
                INNER JOIN (
                    {latest_subquery}
                ) AS latest
                    ON m.file_path = latest.file_path
                   AND m.macro_name = latest.macro_name
                   AND m.version = latest.max_version
                WHERE m.is_deleted = 0
                {row_filter}
            )
            """
            , latest_args + row_args
        ).fetchone()[0])
        deleted_macros = int(conn.execute(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT m.file_path, m.macro_name
                FROM macros AS m
                INNER JOIN (
                    {latest_subquery}
                ) AS latest
                    ON m.file_path = latest.file_path
                   AND m.macro_name = latest.macro_name
                   AND m.version = latest.max_version
                WHERE m.is_deleted = 1
                {row_filter}
            )
            """
            , latest_args + row_args
        ).fetchone()[0])
        distinct_macro_names = int(conn.execute(
            f"""
            SELECT COUNT(DISTINCT m.macro_name)
            FROM macros AS m
            INNER JOIN (
                {latest_subquery}
            ) AS latest
                ON m.file_path = latest.file_path
               AND m.macro_name = latest.macro_name
               AND m.version = latest.max_version
            WHERE m.is_deleted = 0
            {row_filter}
            """
            , latest_args + row_args
        ).fetchone()[0])
        distinct_runtime_macro_names = int(conn.execute(
            f"""
            SELECT COUNT(DISTINCT COALESCE(NULLIF(TRIM(m.runtime_macro_name), ''), m.macro_name))
            FROM macros AS m
            INNER JOIN (
                {latest_subquery}
            ) AS latest
                ON m.file_path = latest.file_path
               AND m.macro_name = latest.macro_name
               AND m.version = latest.max_version
            WHERE m.is_deleted = 0
            {row_filter}
            """
            , latest_args + row_args
        ).fetchone()[0])
        distinct_cfg_files = int(conn.execute(
            f"""
            SELECT COUNT(DISTINCT m.file_path)
            FROM macros AS m
            INNER JOIN (
                {latest_subquery}
            ) AS latest
                ON m.file_path = latest.file_path
               AND m.macro_name = latest.macro_name
               AND m.version = latest.max_version
            WHERE m.is_deleted = 0
            {row_filter}
            """
            , latest_args + row_args
        ).fetchone()[0])
        if printer_profile_id is None:
            latest_update_ts = conn.execute("SELECT MAX(indexed_at) FROM macros").fetchone()[0]
        else:
            latest_update_ts = conn.execute(
                "SELECT MAX(indexed_at) FROM macros WHERE printer_profile_id = ?",
                (int(printer_profile_id),),
            ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT m.file_path, COUNT(DISTINCT m.macro_name) AS macro_count
            FROM macros AS m
            INNER JOIN (
                {latest_subquery}
            ) AS latest
                ON m.file_path = latest.file_path
               AND m.macro_name = latest.macro_name
               AND m.version = latest.max_version
            WHERE m.is_deleted = 0
            {row_filter}
            GROUP BY m.file_path
            ORDER BY macro_count DESC, m.file_path ASC
            LIMIT 20
            """
            , latest_args + row_args
        ).fetchall()
        macros_per_file = [
            {"file_path": str(file_path), "macro_count": int(macro_count)}
            for file_path, macro_count in rows
        ]

    return {
        "total_macros": total_macros,
        "deleted_macros": deleted_macros,
        "distinct_macro_names": distinct_macro_names,
        "distinct_runtime_macro_names": distinct_runtime_macro_names,
        "distinct_cfg_files": distinct_cfg_files,
        "latest_update_ts": int(latest_update_ts) if latest_update_ts is not None else None,
        "macros_per_file": macros_per_file,
    }


def load_macro_list(
    db_path: Path,
    limit: int = 1000,
    offset: int = 0,
    config_dir: Path | None = None,
    config_source: ConfigSource | None = None,
    include_macro_body: bool = True,
    printer_profile_id: int | None = None,
) -> List[Dict[str, object]]:
    """Return latest version row for each macro (list view payload).

    When *config_dir* or *config_source* is given, each row is enriched with a
    ``load_order_index`` reflecting the true macro-level parse order Klipper
    would use, including nested includes spliced inline. Macros not present in
    the current config traversal receive index 999999 and sort to the end.
    """
    if not db_path.exists():
        return []

    latest_subquery = _LATEST_VERSION_WITH_COUNT_SUBQUERY
    latest_args: tuple[object, ...] = ()
    if printer_profile_id is not None:
        latest_subquery = """
        SELECT file_path, macro_name, MAX(version) AS max_version, COUNT(*) AS version_count
        FROM macros
        WHERE printer_profile_id = ?
        GROUP BY file_path, macro_name
        """.strip()
        latest_args = (int(printer_profile_id),)

    load_order_map: Dict[tuple[str, str, int], int] = {}
    if config_source is not None:
        try:
            load_order_map = _build_macro_load_order_map_from_source(config_source)
        except (FileNotFoundError, OSError, ValueError):
            pass
    elif config_dir is not None:
        try:
            load_order_map = _build_macro_load_order_map(config_dir)
        except (FileNotFoundError, OSError, ValueError):
            pass

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        if include_macro_body:
            body_columns = "m.gcode, m.variables_json"
        else:
            body_columns = "NULL AS gcode, '{}' AS variables_json"

        rows = conn.execute(
            f"""
            SELECT
                m.macro_name,
                m.file_path,
                m.version,
                m.indexed_at,
                m.line_number,
                m.description,
                m.rename_existing,
                {body_columns},
                m.is_active,
                m.runtime_macro_name,
                m.renamed_from,
                m.is_deleted,
                m.is_loaded,
                m.is_dynamic,
                                EXISTS(
                                        SELECT 1
                                        FROM macros AS pending
                                        WHERE pending.file_path = m.file_path
                                            AND pending.macro_name = m.macro_name
                                            AND pending.is_new = 1
                                            AND (? IS NULL OR pending.printer_profile_id = ?)
                                ) AS has_new_version,
                cnt.version_count
            FROM macros AS m
            INNER JOIN (
                {latest_subquery}
            ) AS cnt
                ON m.file_path = cnt.file_path
               AND m.macro_name = cnt.macro_name
               AND m.version = cnt.max_version
            WHERE (? IS NULL OR m.printer_profile_id = ?)
            ORDER BY m.macro_name COLLATE NOCASE ASC, m.file_path ASC
            LIMIT ?
            OFFSET ?
            """,
            latest_args + (printer_profile_id, printer_profile_id, printer_profile_id, printer_profile_id, limit, offset),
        ).fetchall()

    return [
        {
            "macro_name": str(macro_name),
            "file_path": str(file_path),
            "version": int(version),
            "indexed_at": int(indexed_at),
            "line_number": int(line_number),
            "description": description,
            "rename_existing": rename_existing,
            "gcode": gcode,
            "variables_json": str(variables_json),
            "is_active": bool(is_active),
            "runtime_macro_name": str(runtime_macro_name or macro_name),
            "renamed_from": renamed_from,
            "display_name": str(runtime_macro_name or macro_name),
            "is_deleted": bool(is_deleted),
            "is_loaded": bool(is_loaded),
            "is_dynamic": bool(is_dynamic),
            "is_new": bool(has_new_version),
            "version_count": int(version_count),
            "load_order_index": load_order_map.get(
                (os.path.normpath(str(file_path)), str(macro_name), int(line_number)),
                999999,
            ),
        }
        for (
            macro_name,
            file_path,
            version,
            indexed_at,
            line_number,
            description,
            rename_existing,
            gcode,
            variables_json,
            is_active,
            runtime_macro_name,
            renamed_from,
            is_deleted,
            is_loaded,
            is_dynamic,
            has_new_version,
            version_count,
        ) in rows
    ]


def load_macro_versions(
    db_path: Path,
    file_path: str,
    macro_name: str,
    printer_profile_id: int | None = None,
) -> List[Dict[str, object]]:
    """Load all stored versions for one macro, newest first."""
    if not db_path.exists():
        return []

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        rows = conn.execute(
            """
            SELECT
                macro_name,
                file_path,
                version,
                indexed_at,
                line_number,
                description,
                rename_existing,
                gcode,
                variables_json,
                is_active,
                runtime_macro_name,
                renamed_from,
                is_deleted,
                is_loaded,
                is_dynamic,
                is_new
            FROM macros
            WHERE file_path = ? AND macro_name = ?
                            AND (? IS NULL OR printer_profile_id = ?)
            ORDER BY version DESC
            """,
                        (file_path, macro_name, printer_profile_id, printer_profile_id),
        ).fetchall()

    return [
        {
            "macro_name": str(row_macro_name),
            "file_path": str(row_file_path),
            "version": int(version),
            "indexed_at": int(indexed_at),
            "line_number": int(line_number),
            "description": description,
            "rename_existing": rename_existing,
            "gcode": gcode,
            "variables_json": str(variables_json),
            "is_active": bool(is_active),
            "runtime_macro_name": str(runtime_macro_name or row_macro_name),
            "renamed_from": renamed_from,
            "display_name": str(runtime_macro_name or row_macro_name),
            "is_deleted": bool(is_deleted),
            "is_loaded": bool(is_loaded),
            "is_dynamic": bool(is_dynamic),
            "is_new": bool(is_new),
        }
        for (
            row_macro_name,
            row_file_path,
            version,
            indexed_at,
            line_number,
            description,
            rename_existing,
            gcode,
            variables_json,
            is_active,
            runtime_macro_name,
            renamed_from,
            is_deleted,
            is_loaded,
            is_dynamic,
            is_new,
        ) in rows
    ]


def load_duplicate_macro_groups(db_path: Path, printer_profile_id: Optional[int] = None) -> List[Dict[str, object]]:
    """Return duplicate macro definitions grouped by macro_name."""
    if not db_path.exists():
        return []

    where_profile = ""
    params: tuple[object, ...] = ()
    if printer_profile_id is not None:
        where_profile = "\n                  AND m.printer_profile_id = ?"
        params = (int(printer_profile_id),)

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        rows = conn.execute(
            f"""
            WITH latest AS (
                {_LATEST_VERSION_SUBQUERY}
            ), latest_rows AS (
                SELECT m.*
                FROM macros AS m
                INNER JOIN latest AS l
                    ON m.file_path = l.file_path
                   AND m.macro_name = l.macro_name
                   AND m.version = l.max_version
                WHERE m.is_deleted = 0
                  AND m.is_loaded = 1
{where_profile}
                  AND COALESCE(NULLIF(TRIM(m.runtime_macro_name), ''), m.macro_name) = m.macro_name
            ), duplicated AS (
                SELECT macro_name
                FROM latest_rows
                GROUP BY macro_name
                HAVING COUNT(*) > 1
            )
            SELECT
                m.macro_name,
                m.file_path,
                m.version,
                m.indexed_at,
                m.is_active
            FROM latest_rows AS m
            INNER JOIN duplicated AS d
                ON d.macro_name = m.macro_name
            ORDER BY m.macro_name COLLATE NOCASE ASC, m.file_path ASC
            """,
            params,
        ).fetchall()

    groups: Dict[str, List[Dict[str, object]]] = {}
    for macro_name, file_path, version, indexed_at, is_active in rows:
        key = str(macro_name)
        groups.setdefault(key, []).append(
            {
                "macro_name": key,
                "file_path": str(file_path),
                "version": int(version),
                "indexed_at": int(indexed_at),
                "is_active": bool(is_active),
            }
        )

    return [
        {"macro_name": macro_name, "entries": entries}
        for macro_name, entries in groups.items()
    ]


def _safe_cfg_path(config_dir: Path, rel_path: str) -> Path:
    """Return a cfg path safely constrained inside config_dir."""
    root = Path(os.path.abspath(str(config_dir)))
    candidate = Path(os.path.abspath(str(config_dir / rel_path)))
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"invalid cfg file path outside config directory: {rel_path}")
    return candidate


def _macro_version_to_section_text(row: tuple) -> str:
    """Build cfg section text from one stored macro version row."""
    section_type, macro_name, description, rename_existing, gcode, variables_json = row
    header_section = str(section_type or "gcode_macro").strip() or "gcode_macro"
    lines: List[str] = [f"[{header_section} {macro_name}]\n"]

    if description:
        lines.append(f"description: {description}\n")

    if rename_existing:
        lines.append(f"rename_existing: {rename_existing}\n")

    try:
        variables = json.loads(str(variables_json))
    except Exception:
        variables = {}
    if isinstance(variables, dict):
        for key in sorted(variables.keys()):
            lines.append(f"variable_{key}: {variables[key]}\n")

    if gcode:
        lines.append("gcode:\n")
        for line in str(gcode).splitlines():
            lines.append(f"{line}\n")

    return "".join(lines)


def macro_row_to_section_text(macro: Dict[str, object]) -> str:
    """Build editable cfg section text from one macro mapping."""
    return _macro_version_to_section_text(
        (
            macro.get("section_type", "gcode_macro"),
            macro.get("macro_name", ""),
            macro.get("description"),
            macro.get("rename_existing"),
            macro.get("gcode"),
            macro.get("variables_json", "{}"),
        )
    )


def _parse_macro_section_text(section_text: str) -> Dict[str, object]:
    """Parse one edited [gcode_macro ...] section from text."""
    normalized_text = str(section_text or "")
    if not normalized_text.strip():
        raise ValueError("macro text is empty")

    lines = normalized_text.splitlines(keepends=True)
    if not lines:
        raise ValueError("macro text is empty")

    header_line = lines[0]
    if not _is_section_header_line(header_line):
        raise ValueError("macro text must start with a [gcode_macro ...] header")

    header = header_line.strip()[1:-1].strip()
    section_type, section_arg = _section_parts(header)
    if section_type != "gcode_macro" or not section_arg:
        raise ValueError("only [gcode_macro ...] sections can be edited")

    description: Optional[str] = None
    rename_existing: Optional[str] = None
    variables: Dict[str, str] = {}
    current_gcode_lines: List[str] = []
    in_gcode_block = False

    for line in lines[1:]:
        if _is_section_header_line(line):
            raise ValueError("macro text must contain exactly one section")

        if in_gcode_block:
            current_gcode_lines.append(line.rstrip("\n"))
            continue

        pair = _parse_key_value(line)
        if not pair:
            continue

        key, value = pair
        if key == "gcode":
            in_gcode_block = True
            if value:
                current_gcode_lines.append(value)
        elif key == "description":
            description = value or None
        elif key == "rename_existing":
            rename_existing = value or None
        elif key.startswith("variable_"):
            variables[key[len("variable_") :]] = value

    while current_gcode_lines and _is_trailing_gcode_comment_or_blank(current_gcode_lines[-1]):
        current_gcode_lines.pop()

    return {
        "section_type": section_type,
        "macro_name": str(section_arg),
        "description": description,
        "rename_existing": rename_existing,
        "gcode": "\n".join(current_gcode_lines).rstrip("\n") if current_gcode_lines else None,
        "variables_json": json.dumps(variables, separators=(",", ":"), sort_keys=True),
        "section_text": normalized_text if normalized_text.endswith("\n") else f"{normalized_text}\n",
    }


def _replace_or_append_macro_section(cfg_file: Path, macro_name: str, section_text: str) -> str:
    """Replace all matching macro sections with one section, or append if missing."""
    try:
        lines = cfg_file.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    except FileNotFoundError:
        lines = []

    output: List[str] = []
    skipping_target = False
    removed_sections = 0
    insert_index: int | None = None

    def trim_trailing_blank_lines(lines: List[str]) -> None:
        while lines and not lines[-1].strip():
            lines.pop()

    def trim_leading_blank_lines(lines: List[str]) -> None:
        while lines and not lines[0].strip():
            del lines[0]

    for line in lines:
        if _is_section_header_line(line):
            header = line.strip()[1:-1].strip()
            section_type, section_arg = _section_parts(header)
            is_target = section_type == "gcode_macro" and str(section_arg or "") == macro_name
            if is_target:
                if insert_index is None:
                    insert_index = len(output)
                removed_sections += 1
                skipping_target = True
                continue
            skipping_target = False

        if skipping_target:
            continue
        output.append(line)

    normalized_section = section_text.rstrip("\n") + "\n"
    if insert_index is not None:
        before_lines = output[:insert_index]
        after_lines = output[insert_index:]
        trim_trailing_blank_lines(before_lines)
        trim_leading_blank_lines(after_lines)

        output = before_lines
        if output:
            output.append("\n")
        output.append(normalized_section)
        if after_lines:
            output.append("\n")
            output.extend(after_lines)
        operation = "replaced"
    else:
        trim_trailing_blank_lines(output)
        if output:
            output.append("\n")
        output.append(normalized_section)
        operation = "appended"

    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text("".join(output), encoding="utf-8")
    return operation


def restore_macro_version(
    db_path: Path,
    config_dir: Path,
    file_path: str,
    macro_name: str,
    version: int,
    printer_profile_id: Optional[int] = None,
) -> Dict[str, object]:
    """Restore one stored macro version into its cfg file content."""
    if version <= 0:
        raise ValueError("version must be a positive integer")

    where_profile = ""
    params: tuple[object, ...]
    if printer_profile_id is not None:
        where_profile = " AND printer_profile_id = ?"
        params = (file_path, macro_name, int(version), int(printer_profile_id))
    else:
        params = (file_path, macro_name, int(version))

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        row = conn.execute(
            f"""
            SELECT section_type, macro_name, description, rename_existing, gcode, variables_json, is_new
            FROM macros
            WHERE file_path = ? AND macro_name = ? AND version = ?
            {where_profile}
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is not None and bool(int(row[6])):
            if printer_profile_id is not None:
                conn.execute(
                    """
                    UPDATE macros
                    SET is_new = 0
                    WHERE file_path = ? AND macro_name = ? AND version = ? AND printer_profile_id = ?
                    """,
                    (file_path, macro_name, int(version), int(printer_profile_id)),
                )
            else:
                conn.execute(
                    """
                    UPDATE macros
                    SET is_new = 0
                    WHERE file_path = ? AND macro_name = ? AND version = ?
                    """,
                    (file_path, macro_name, int(version)),
                )
            conn.commit()

    if row is None:
        raise ValueError("macro version not found")

    config_dir = config_dir.expanduser().resolve()
    restore_target_path = file_path
    cfg_file = _safe_cfg_path(config_dir, restore_target_path)
    section_text = _macro_version_to_section_text(row[:6])
    operation = _replace_or_append_macro_section(cfg_file, macro_name, section_text)

    if bool(int(row[6])) and Path(restore_target_path).name.lower() == "macros.cfg":
        _ensure_printer_cfg_includes_macros_cfg(config_dir)

    return {
        "file_path": restore_target_path,
        "macro_name": macro_name,
        "version": int(version),
        "operation": operation,
    }


def save_macro_edit(
    config_dir: Path,
    file_path: str,
    macro_name: str,
    section_text: str,
) -> Dict[str, object]:
    """Write an edited macro section back to its cfg file."""
    parsed = _parse_macro_section_text(section_text)
    parsed_macro_name = str(parsed.get("macro_name", ""))
    if parsed_macro_name != macro_name:
        raise ValueError("macro renaming is not supported")

    config_dir = config_dir.expanduser().resolve()
    cfg_file = _safe_cfg_path(config_dir, file_path)
    operation = _replace_or_append_macro_section(cfg_file, macro_name, str(parsed["section_text"]))
    return {
        "file_path": file_path,
        "macro_name": macro_name,
        "operation": operation,
    }


def delete_macro_from_cfg(
    config_dir: Path,
    file_path: str,
    macro_name: str,
) -> Dict[str, object]:
    """Delete one [gcode_macro ...] section from its source cfg file."""
    if not file_path or not macro_name:
        raise ValueError("missing macro identity")

    config_dir = config_dir.expanduser().resolve()
    cfg_file = _safe_cfg_path(config_dir, file_path)
    removed_sections = _remove_macro_sections_from_cfg(cfg_file, macro_name)
    return {
        "file_path": file_path,
        "macro_name": macro_name,
        "removed_sections": int(removed_sections),
    }


def remove_deleted_macro(
    db_path: Path,
    file_path: str,
    macro_name: str,
    printer_profile_id: Optional[int] = None,
) -> Dict[str, object]:
    """Permanently delete one macro identity from DB when marked as deleted."""
    where_profile = ""
    latest_params: tuple[object, ...]
    delete_params: tuple[object, ...]
    if printer_profile_id is not None:
        where_profile = " AND printer_profile_id = ?"
        latest_params = (file_path, macro_name, int(printer_profile_id))
        delete_params = (file_path, macro_name, int(printer_profile_id))
    else:
        latest_params = (file_path, macro_name)
        delete_params = (file_path, macro_name)

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        latest = conn.execute(
            f"""
            SELECT version, is_deleted
            FROM macros
            WHERE file_path = ? AND macro_name = ?
            {where_profile}
            ORDER BY version DESC
            LIMIT 1
            """,
            latest_params,
        ).fetchone()

        if latest is None:
            return {"removed": 0, "reason": "not_found"}

        if int(latest[1]) == 0:
            return {"removed": 0, "reason": "not_deleted"}

        removed = conn.execute(
            f"DELETE FROM macros WHERE file_path = ? AND macro_name = ?{where_profile}",
            delete_params,
        ).rowcount
        conn.commit()

    return {"removed": int(removed or 0), "reason": "removed"}


def remove_inactive_macro_version(
    db_path: Path,
    file_path: str,
    macro_name: str,
    version: int,
    printer_profile_id: Optional[int] = None,
) -> Dict[str, object]:
    """Permanently delete one selected inactive macro version from DB."""
    where_profile = ""
    select_params: tuple[object, ...]
    delete_params: tuple[object, ...]
    if printer_profile_id is not None:
        where_profile = " AND printer_profile_id = ?"
        select_params = (file_path, macro_name, version, int(printer_profile_id))
        delete_params = (file_path, macro_name, version, int(printer_profile_id))
    else:
        select_params = (file_path, macro_name, version)
        delete_params = (file_path, macro_name, version)

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        row = conn.execute(
            f"""
            SELECT version, is_active, is_deleted
            FROM macros
            WHERE file_path = ? AND macro_name = ? AND version = ?
            {where_profile}
            """,
            select_params,
        ).fetchone()

        if row is None:
            return {"removed": 0, "reason": "not_found"}
        if int(row[2]) != 0:
            return {"removed": 0, "reason": "deleted"}
        if int(row[1]) != 0:
            return {"removed": 0, "reason": "not_inactive"}

        removed = conn.execute(
            f"DELETE FROM macros WHERE file_path = ? AND macro_name = ? AND version = ?{where_profile}",
            delete_params,
        ).rowcount
        conn.commit()

    return {"removed": int(removed or 0), "reason": "removed"}


def remove_all_deleted_macros(db_path: Path, printer_profile_id: Optional[int] = None) -> Dict[str, object]:
    """Permanently remove all macro identities whose latest version is deleted."""
    where_profile = ""
    params: tuple[object, ...] = ()
    if printer_profile_id is not None:
        where_profile = " AND m.printer_profile_id = ?"
        params = (int(printer_profile_id),)

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        removed = conn.execute(
            f"""
            DELETE FROM macros
            WHERE (file_path, macro_name) IN (
                SELECT m.file_path, m.macro_name
                FROM macros AS m
                INNER JOIN (
                    {_LATEST_VERSION_SUBQUERY}
                ) AS latest
                    ON m.file_path = latest.file_path
                   AND m.macro_name = latest.macro_name
                   AND m.version = latest.max_version
                WHERE m.is_deleted = 1{where_profile}
            )
            """,
            params,
        ).rowcount
        conn.commit()

    return {"removed": int(removed or 0)}


def _remove_macro_sections_from_cfg(cfg_file: Path, macro_name: str) -> int:
    """Remove all [gcode_macro <macro_name>] sections from one cfg file."""
    try:
        lines = cfg_file.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    except FileNotFoundError:
        return 0

    output: List[str] = []
    skipping_target = False
    removed_sections = 0

    for line in lines:
        if _is_section_header_line(line):
            header = line.strip()[1:-1].strip()
            section_type, section_arg = _section_parts(header)
            is_target = section_type == "gcode_macro" and str(section_arg or "") == macro_name
            if is_target:
                skipping_target = True
                removed_sections += 1
                continue
            skipping_target = False

        if skipping_target:
            continue
        output.append(line)

    if removed_sections > 0:
        cfg_file.write_text("".join(output), encoding="utf-8")
    return removed_sections


def resolve_duplicate_macros(
    config_dir: Path,
    keep_choices: Dict[str, str],
    duplicate_groups: Optional[List[Dict[str, object]]] = None,
) -> Dict[str, object]:
    """Delete non-selected duplicate macro definitions from cfg files.

    keep_choices maps macro_name -> file_path (relative to config_dir) to keep.
    When duplicate_groups is provided, removals are scoped to those entries.
    """
    config_dir = config_dir.expanduser().resolve()
    removed_sections = 0
    touched_files: set[str] = set()

    if duplicate_groups is not None:
        for group in duplicate_groups:
            macro_name = str(group.get("macro_name", ""))
            if not macro_name:
                continue

            keep_file = str(keep_choices.get(macro_name, ""))
            if not keep_file:
                continue

            raw_entries = group.get("entries", [])
            entries = raw_entries if isinstance(raw_entries, list) else []
            for entry in entries:
                rel_path = str(entry.get("file_path", ""))
                if not rel_path or rel_path == keep_file:
                    continue
                cfg_file = _safe_cfg_path(config_dir, rel_path)
                removed_in_file = _remove_macro_sections_from_cfg(cfg_file, macro_name)
                if removed_in_file > 0:
                    removed_sections += removed_in_file
                    touched_files.add(rel_path)
    else:
        for macro_name, keep_file in keep_choices.items():
            group_files = [
                rel
                for rel in (str(p.relative_to(config_dir)) for p in _iter_cfg_files(config_dir))
                if rel != keep_file
            ]
            for rel_path in group_files:
                cfg_file = _safe_cfg_path(config_dir, rel_path)
                removed_in_file = _remove_macro_sections_from_cfg(cfg_file, macro_name)
                if removed_in_file > 0:
                    removed_sections += removed_in_file
                    touched_files.add(rel_path)

    return {
        "removed_sections": removed_sections,
        "touched_files": sorted(touched_files),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser for standalone indexing runs."""
    parser = argparse.ArgumentParser(
        description="Scan Klipper .cfg files for [gcode_macro ...] sections and store them in SQLite."
    )
    parser.add_argument(
        "--config-dir",
        default="~/.config/klippervault",
        help="Path to local runtime config cache directory (default: ~/.config/klippervault)",
    )
    parser.add_argument(
        "--db-path",
        default="~/.local/share/klippervault/klipper_macros.db",
        help="Path to SQLite DB file (default: ~/.local/share/klippervault/klipper_macros.db)",
    )
    return parser


def main() -> int:
    """CLI entrypoint for scanner mode."""
    args = build_arg_parser().parse_args()
    config_dir = Path(args.config_dir).expanduser().resolve()
    db_path = Path(args.db_path).expanduser().resolve()

    try:
        result = run_indexing(config_dir=config_dir, db_path=db_path)
    except FileNotFoundError:
        print(f"ERROR: config directory not found: {config_dir}")
        return 2

    print(f"Scanned cfg files   : {result['cfg_files_scanned']}")
    print(f"New versions stored : {result['macros_inserted']}")
    print(f"Unchanged (skipped) : {result['macros_unchanged']}")
    print(f"Database            : {result['db_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
