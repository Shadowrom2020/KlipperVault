# macOS installation

This guide installs KlipperVault in remote-only `off_printer` mode on macOS.

## Prerequisites

- macOS (Apple Silicon or Intel)
- Python 3 with `venv` support (`python3 -m venv --help` should work)
- `sudo` for user-context install steps

If `python3` is missing, install it first (for example with Homebrew):

```bash
brew install python
```

## Install

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

## Run

```bash
~/klippervault-venv/bin/python ./klipper_vault_gui.py
```

## Build a Standalone Executable

From repository root:

```bash
python3 -m pip install -r requirements.txt -r requirements-build.txt
make bundle
```

Build artifacts are written under `dist/` by PyInstaller. macOS app bundles must be built on macOS.

### Build DMG Installer (Automatic)

After running `make bundle`, you can optionally create a DMG disk image:

```bash
python3 scripts/build_dmg_installer.py
```

This generates a `.dmg` file in the `release/` directory that is ready for distribution.

## Uninstall

Remove installed artifacts manually:

```bash
rm -rf "$HOME/klippervault-venv"
rm -rf "$HOME/Library/Application Support/KlipperVault"
```
