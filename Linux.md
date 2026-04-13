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
