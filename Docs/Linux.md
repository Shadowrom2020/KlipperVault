# Linux Installation

This guide covers source installation and running KlipperVault on Linux, including standard remote profiles and developer-mode virtual local-only profiles.

## Prerequisites

- Linux host
- Python 3 with venv
- sudo (recommended) for privilege escalation when needed

## Install From Source

From repository root:

```bash
sudo ./install.sh
```

Installer summary:

1. Detect target user
2. Create runtime directories under `~/.config/klippervault` and `~/.local/share/klippervault`
3. Create virtualenv (`~/klippervault-venv` by default)
4. Install dependencies from `requirements.txt`
5. Initialize runtime defaults (stored in SQLite on first app start)

## Run From Source

From repository root:

```bash
~/klippervault-venv/bin/python ./klipper_vault_gui.py
```

On Linux, source and packaged runs use browser/server mode. Open:

- http://127.0.0.1:10090

Port 10090 is fixed.

## Uninstall Source Install

```bash
./uninstall.sh
./uninstall.sh --remove-venv --remove-config --remove-db
```

## Build a Standalone Executable

```bash
python3 -m pip install -r requirements.txt -r requirements-build.txt
make bundle
```

Build artifacts are written under dist/ by PyInstaller. Linux binaries must be built on Linux.

Run the packaged binary:

```bash
./dist/KlipperVault
```

Then open http://127.0.0.1:10090 in your browser.

### Build AppImage Installer (Optional)

If you have `appimagetool` installed:

```bash
python3 -m pip install -r requirements.txt -r requirements-build.txt
make bundle
python3 scripts/build_appimage_installer.py
```

This generates an .AppImage file in the release/ directory. See [AppImageKit](https://github.com/AppImage/AppImageKit) for installation instructions.
