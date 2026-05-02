# Windows Installation (Windows 10 and up)

This guide covers source installation and running KlipperVault on Windows, including standard remote profiles and developer-mode virtual local-only profiles.

## Prerequisites

- Windows 10 or newer
- Python 3 with venv support
- PowerShell or Command Prompt

If Python is not installed, download it from:

- https://www.python.org/downloads/windows/

During installation, enable Add Python to PATH.

## Install From Source

From repository root in Command Prompt or PowerShell:

```bat
install_windows.bat
```

Installer summary:

1. Detect Python command (`py -3` preferred, falls back to `python`)
2. Create runtime directories under `%APPDATA%\\KlipperVault` (config) and `%LOCALAPPDATA%\\KlipperVault` (database)
3. Create virtualenv (`%USERPROFILE%\\klippervault-venv` by default)
4. Install dependencies from `requirements.txt`
5. Initialize runtime defaults (stored in SQLite on first app start)

## Run From Source

```bat
%USERPROFILE%\klippervault-venv\Scripts\python.exe klipper_vault_gui.py
```

Then open:

- http://127.0.0.1:10090

Port 10090 is fixed.

## Build a Standalone Executable

From repository root in Command Prompt:

```bat
py -3 -m pip install -r requirements.txt -r requirements-build.txt
py -3 scripts\build_executable.py
```

Build artifacts are written under dist\ by PyInstaller. Windows executables must be built on Windows.

Note: the `.zip` release artifact is the portable app build. Extract it anywhere and run `KlipperVault.exe` directly (no installer required).

Run the packaged binary:

```bat
dist\KlipperVault.exe
```

On Windows packaged runs use native window mode (pywebview). You do not need to open a browser manually.

### Build Inno Setup Installer (Optional)

If you have [Inno Setup 6](https://jrsoftware.org/isdl.php) installed:

```bat
py -3 -m pip install -r requirements.txt -r requirements-build.txt
py -3 scripts\build_executable.py
py -3 scripts\build_msi_installer.py
```

This generates a .exe setup file in the release\ directory. The installer includes desktop and Start Menu shortcuts.

## Uninstall Source Install

Remove installed artifacts manually:

```bat
rmdir /s /q "%USERPROFILE%\klippervault-venv"
rmdir /s /q "%APPDATA%\KlipperVault"
del /q "%LOCALAPPDATA%\KlipperVault\klipper_macros.db"
```
