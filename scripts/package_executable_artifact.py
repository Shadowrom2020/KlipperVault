#!/usr/bin/env python3
"""Archive the PyInstaller output into a versioned release artifact."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"
RELEASE_DIR = REPO_ROOT / "release"
VERSION_FILE = REPO_ROOT / "VERSION"
EXECUTABLE_BASENAME = "KlipperVault"


def _read_version() -> str:
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not version:
        raise RuntimeError("VERSION file is empty")
    return version


def _find_built_executable() -> Path:
    candidates = [
        DIST_DIR / f"{EXECUTABLE_BASENAME}.exe",
        DIST_DIR / EXECUTABLE_BASENAME,
        DIST_DIR / f"{EXECUTABLE_BASENAME}.app",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"No packaged executable found in {DIST_DIR}")


def _copy_payload(source: Path, staging_dir: Path) -> Path:
    destination = staging_dir / source.name
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, help="Platform label used in the archive filename")
    args = parser.parse_args()

    version = _read_version()
    built_executable = _find_built_executable()

    RELEASE_DIR.mkdir(exist_ok=True)
    archive_stem = f"KlipperVault-{version}-{args.platform}"
    staging_dir = RELEASE_DIR / archive_stem
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)

    _copy_payload(built_executable, staging_dir)

    archive_path = shutil.make_archive(str(RELEASE_DIR / archive_stem), "zip", root_dir=staging_dir)
    shutil.rmtree(staging_dir)
    print(archive_path)


if __name__ == "__main__":
    main()