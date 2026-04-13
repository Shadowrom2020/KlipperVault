#!/usr/bin/env python3
"""Package macOS .app bundle into a DMG installer."""

from __future__ import annotations

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
    """Build macOS DMG installer."""
    version = _read_version()
    
    app_bundle = DIST_DIR / "KlipperVault.app"
    if not app_bundle.exists():
        print(f"ERROR: macOS app bundle not found at {app_bundle}")
        sys.exit(1)
    
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    dmg_path = RELEASE_DIR / f"KlipperVault-{version}-macos.dmg"
    
    # Create temporary staging directory
    staging = RELEASE_DIR / "dmg-staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    
    # Copy app bundle and create alias to Applications
    shutil.copytree(app_bundle, staging / "KlipperVault.app")
    (staging / "Applications").symlink_to("/Applications")
    
    # Use hdiutil to create DMG
    try:
        subprocess.run(
            [
                "hdiutil",
                "create",
                "-volname", "KlipperVault",
                "-srcdir", str(staging),
                "-ov",
                "-format", "UDZO",
                str(dmg_path),
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Failed to create DMG: {e.stderr.decode()}")
        sys.exit(1)
    finally:
        # Clean up staging directory
        if staging.exists():
            shutil.rmtree(staging)
    
    print(str(dmg_path))


if __name__ == "__main__":
    main()
