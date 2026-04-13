#!/usr/bin/env python3
"""Package Linux executable into AppImage installer."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"
RELEASE_DIR = REPO_ROOT / "release"


def _read_version() -> str:
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not version:
        raise RuntimeError("VERSION file is empty")
    return version


def main() -> None:
    """Build Linux AppImage installer."""
    version = _read_version()
    
    executable = DIST_DIR / "KlipperVault"
    if not executable.exists():
        print(f"ERROR: Linux executable not found at {executable}")
        sys.exit(1)
    
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    appdir = RELEASE_DIR / f"KlipperVault-{version}.AppDir"
    appimage_path = RELEASE_DIR / f"KlipperVault-{version}-linux-x64.AppImage"
    
    # Create AppDir structure
    if appdir.exists():
        shutil.rmtree(appdir)
    appdir.mkdir(parents=True)
    
    app_usr = appdir / "usr" / "bin"
    app_usr.mkdir(parents=True)
    
    # Copy executable
    shutil.copy2(executable, app_usr / "klippervault")
    (app_usr / "klippervault").chmod(0o755)
    
    # Create AppRun script
    apprun = appdir / "AppRun"
    apprun.write_text(
        "#!/bin/sh\n"
        'HERE="$(cd "$(dirname "$0")" && pwd)"\n'
        'exec "$HERE/usr/bin/klippervault" "$@"\n',
        encoding="utf-8",
    )
    apprun.chmod(0o755)
    
    # Create .desktop file
    desktop_file = appdir / "klippervault.desktop"
    desktop_file.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=KlipperVault\n"
        "Exec=klippervault\n"
        "Icon=klippervault\n"
        "Categories=Utility;\n"
        "Comment=KlipperVault macro vault management\n"
        "StartupNotify=true\n",
        encoding="utf-8",
    )
    
    # Copy icon if available
    favicon = REPO_ROOT / "assets" / "favicon.svg"
    if favicon.exists():
        shutil.copy2(favicon, appdir / "klippervault.svg")
    
    # Try to find appimagetool
    appimagetool = shutil.which("appimagetool")
    if not appimagetool:
        print("ERROR: appimagetool not found. Install it from https://github.com/AppImage/AppImageKit/releases")
        sys.exit(1)
    
    # Build AppImage
    try:
        subprocess.run(
            [appimagetool, str(appdir), str(appimage_path)],
            check=True,
            capture_output=True,
            env={**os.environ, "APPIMAGE_EXTRACT_AND_RUN": "1"},
        )
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Failed to create AppImage: {e.stderr.decode()}")
        sys.exit(1)
    finally:
        # Clean up AppDir
        if appdir.exists():
            shutil.rmtree(appdir)
    
    # Make AppImage executable
    appimage_path.chmod(0o755)
    print(str(appimage_path))


if __name__ == "__main__":
    main()
