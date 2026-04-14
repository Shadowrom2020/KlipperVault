#!/usr/bin/env python3
"""Generate a Windows MSI installer using Inno Setup."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ISS_TEMPLATE = REPO_ROOT / "klippervault.iss"
DIST_DIR = REPO_ROOT / "dist"
RELEASE_DIR = REPO_ROOT / "release"


def _read_version() -> str:
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not version:
        raise RuntimeError("VERSION file is empty")
    return version


def main() -> None:
    """Build Windows MSI using Inno Setup."""
    version = _read_version()
    
    if not ISS_TEMPLATE.exists():
        print(f"ERROR: Inno Setup script not found at {ISS_TEMPLATE}")
        sys.exit(1)
    
    if not DIST_DIR.exists():
        print(f"ERROR: Built executable not found at {DIST_DIR}")
        sys.exit(1)
    
    # Find iscc.exe: check PATH first, then well-known installation directories
    _which = shutil.which("iscc")
    iscc_paths = [
        Path(_which) if _which else None,
        Path("C:/Program Files (x86)/Inno Setup 6/iscc.exe"),
        Path("C:/Program Files/Inno Setup 6/iscc.exe"),
        Path(os.environ.get("INNO_SETUP_PATH", "")) / "iscc.exe" if os.environ.get("INNO_SETUP_PATH") else None,
    ]
    
    iscc = None
    for candidate in iscc_paths:
        if candidate and candidate.exists():
            iscc = candidate
            break
    
    if not iscc:
        print("ERROR: iscc.exe not found. Install Inno Setup 6 from https://jrsoftware.org/isdl.php")
        sys.exit(1)
    
    output_dir = RELEASE_DIR / "inno-setup-output"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    app_exe_path = DIST_DIR / "KlipperVault.exe"
    setup_env = os.environ.copy()
    setup_env["KV_VERSION"] = version
    setup_env["KV_APP_DIR"] = str(app_exe_path)
    setup_env["KV_OUTPUT_DIR"] = str(output_dir)

    # Inno Setup preprocessor symbols must be passed via /D flags.
    subprocess.run(
        [
            str(iscc),
            f"/DKV_VERSION={version}",
            f"/DKV_APP_DIR={app_exe_path}",
            f"/DKV_OUTPUT_DIR={output_dir}",
            str(ISS_TEMPLATE),
        ],
        env=setup_env,
        check=True,
    )
    
    msi_file = output_dir / f"KlipperVault-{version}-windows-x64.exe"
    if msi_file.exists():
        print(str(msi_file))


if __name__ == "__main__":
    main()
