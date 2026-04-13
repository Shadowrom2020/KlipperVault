#!/usr/bin/env python3
"""Build a standalone KlipperVault executable with PyInstaller."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "klippervault.spec"


def main() -> None:
    """Run the PyInstaller build with the repository spec file."""
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", str(SPEC_PATH)],
        cwd=REPO_ROOT,
        check=True,
    )


if __name__ == "__main__":
    main()