# Windows installation (Windows 10 and up)

This guide installs KlipperVault in remote-only `off_printer` mode on Windows 10+.

## Prerequisites

- Windows 10 or newer
- Python 3 with `venv` support
- PowerShell or Command Prompt

If Python is not installed, download it from:

- https://www.python.org/downloads/windows/

During installation, enable "Add Python to PATH".

## Install

From repository root in Command Prompt:

```bat
install_windows.bat
```

Installer summary:

1. Detect Python command (`py -3` preferred, falls back to `python`)
2. Create runtime directories under `%APPDATA%\\KlipperVault` (config) and `%LOCALAPPDATA%\\KlipperVault` (database)
3. Create virtualenv (`%USERPROFILE%\\klippervault-venv` by default)
4. Install dependencies from `requirements.txt`
5. Initialize runtime defaults (stored in SQLite on first app start)

## Run

```bat
%USERPROFILE%\\klippervault-venv\\Scripts\\python.exe klipper_vault_gui.py
```

## Uninstall

Remove installed artifacts manually:

```bat
rmdir /s /q "%USERPROFILE%\\klippervault-venv"
rmdir /s /q "%APPDATA%\\KlipperVault"
del /q "%LOCALAPPDATA%\\KlipperVault\\klipper_macros.db"
```
