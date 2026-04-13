# Linux installation

This guide installs KlipperVault in remote-only `off_printer` mode on Linux.

## Prerequisites

- Linux host
- Python 3 with `venv`
- `sudo` (recommended) for privilege escalation when needed

## Install

From repository root:

```bash
sudo ./install.sh
```

Installer summary (GUI/off-printer default):

1. Detect target user
2. Create runtime directories under `~/.config/klippervault` and `~/.local/share/klippervault`
3. Create virtualenv (`~/klippervault-venv` by default)
4. Install dependencies from `requirements.txt`
5. Initialize runtime defaults (stored in SQLite on first app start)

## Uninstall

```bash
./uninstall.sh
./uninstall.sh --remove-venv --remove-config --remove-db
```

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

Build artifacts are written under `dist/` by PyInstaller. Linux binaries must be built on Linux.

### Build AppImage Installer (Optional)

If you have `appimagetool` installed:

```bash
python3 -m pip install -r requirements.txt -r requirements-build.txt
make bundle
python3 scripts/build_appimage_installer.py
```

This generates an `.AppImage` file in the `release/` directory. See [AppImageKit](https://github.com/AppImage/AppImageKit) for installation instructions.
