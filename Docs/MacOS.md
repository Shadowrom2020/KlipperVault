# macOS Installation

This guide covers source installation and running KlipperVault on macOS in remote-only off_printer mode.

## Prerequisites

- macOS (Apple Silicon or Intel)
- Python 3 with venv support (python3 -m venv --help should work)
- `sudo` for user-context install steps

If `python3` is missing, install it first (for example with Homebrew):

```bash
brew install python
```

## Install From Source

From repository root:

```bash
chmod +x ./install_macos
./install_macos
```

Installer summary:

1. Detect target user (`$SUDO_USER` when available, otherwise current user)
2. Create runtime directories under `~/Library/Application Support/KlipperVault`
3. Create virtualenv (`~/klippervault-venv` by default)
4. Install dependencies from `requirements.txt`
5. Initialize runtime defaults (stored in SQLite on first app start)

## Run From Source

```bash
~/klippervault-venv/bin/python ./klipper_vault_gui.py
```

Then open:

- http://127.0.0.1:10090

Port 10090 is fixed.

## Build a Standalone Executable

macOS binaries are built locally on macOS hosts.
GitHub Actions does not build macOS artifacts.

From repository root:

```bash
./scripts/setup_dev.sh
source .venv/bin/activate
make bundle
```

Build artifacts are written under `dist/` by PyInstaller. macOS app bundles must be built on macOS.

Run the packaged app:

```bash
open dist/KlipperVault.app
```

On macOS packaged runs use native window mode (pywebview). You do not need to open a browser manually.

### Build Local macOS Release Artifact

After running `make bundle`, optionally create a local macOS release artifact:

```bash
./.venv/bin/python scripts/build_dmg_installer.py
```

This generates a macOS release artifact in `release/` for distribution.

### Optional: Package ZIP Artifact

To generate the release ZIP variant used by cross-platform packaging scripts:

```bash
./.venv/bin/python scripts/package_executable_artifact.py --platform macos-arm64
```

Use `macos-x64` for Intel builds.

## Uninstall Source Install

Remove installed artifacts manually:

```bash
rm -rf "$HOME/klippervault-venv"
rm -rf "$HOME/Library/Application Support/KlipperVault"
```
