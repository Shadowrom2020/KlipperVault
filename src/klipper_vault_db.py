#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared SQLite helpers for KlipperVault modules."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Callable, Iterable, Iterator


@contextmanager
def open_sqlite_connection(
    db_path: Path,
    *,
    ensure_schema: Callable[[sqlite3.Connection], None] | None = None,
    pragmas: Iterable[str] | None = None,
) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with optional pragma and schema setup."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        if pragmas:
            for pragma in pragmas:
                conn.execute(pragma)
        if ensure_schema is not None:
            ensure_schema(conn)
        yield conn
    finally:
        conn.close()