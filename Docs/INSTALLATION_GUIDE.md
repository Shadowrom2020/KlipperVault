# KlipperVault Installation Guide

KlipperVault provides multiple installation methods suited for different use cases.

## Quick Start: Recommended (Windows, Linux, macOS)

The easiest way to get started is using the standalone executable or native installer for your platform.

### Windows

1. Download `KlipperVault-X.X.X-windows-x64.exe` from the [latest release](https://github.com/Shadowrom2020/KlipperVault/releases)
2. Run the installer
3. KlipperVault will launch automatically and appear in your Start Menu

### macOS

1. Follow the macOS build instructions in [MacOS.md](MacOS.md)
2. Build the app locally on macOS (`make bundle`)
3. Launch **KlipperVault.app** from `dist/` or move it to **Applications**

### Linux

1. Download `KlipperVault-X.X.X-linux-x64.AppImage` from the [latest release](https://github.com/Shadowrom2020/KlipperVault/releases)
2. Make it executable: `chmod +x KlipperVault-X.X.X-linux-x64.AppImage`
3. Run it: `./KlipperVault-X.X.X-linux-x64.AppImage`

Or extract from ZIP archive:

1. Download `KlipperVault-X.X.X-linux-x64.zip` from releases
2. Unzip: `unzip KlipperVault-X.X.X-linux-x64.zip`
3. Run: `./KlipperVault`

## For Developers: Source Installation

If you want to modify KlipperVault or use the latest development version:

- [Linux Installation](Linux.md)
- [macOS Installation](MacOS.md)
- [Windows Installation](Windows.md)

## Uninstall

### Windows

- Use Control Panel → Programs → Uninstall a program
- Select KlipperVault and click Uninstall
- This removes the app and Start Menu shortcuts

Config and database are preserved in `%APPDATA%\KlipperVault` and `%LOCALAPPDATA%\KlipperVault\klipper_macros.db`

### macOS

- Drag KlipperVault.app from Applications to the Trash
- Or use Finder → Applications → KlipperVault, then press Command+Delete

Config and database are preserved in `~/Library/Application Support/KlipperVault/`

### Linux

1. Delete the AppImage or extracted executable
2. Optionally remove config: `rm -rf ~/.config/klippervault`
3. Optionally remove database: `rm -rf ~/.local/share/klippervault`

## Troubleshooting

### Executable won't start (Windows)

If you see "SmartScreen protected your PC":

1. Click "More info"
2. Click "Run anyway"

Windows is warning because the executable is not yet signed by Microsoft. This is normal for new releases.

### Executable won't start (macOS)

If you see ""KlipperVault" cannot be opened":

1. Open System Preferences → Security & Privacy → General
2. Click "Open Anyway" next to KlipperVault
3. Confirm "Open"

This happens because the app is not yet notarized by Apple. Code signing and notarization are planned for future releases.

### Port already in use

If you see "Address already in use" on startup:

1. Check if another instance is running: `lsof -i :10090` (Linux/macOS) or `netstat -ano | findstr :10090` (Windows)
2. Close the other instance using port 10090
3. Restart KlipperVault (port 10090 is fixed)

### Database corruption or config reset

If KlipperVault won't start or keeps resetting settings:

1. Close KlipperVault
2. Back up your database:
   - **Windows**: `%LOCALAPPDATA%\KlipperVault\klipper_macros.db`
   - **macOS**: `~/Library/Application Support/KlipperVault/klipper_macros.db`
   - **Linux**: `~/.local/share/klippervault/klipper_macros.db`
3. Delete the database file and restart (a fresh database will be created)
4. Reconfigure your printer profile

## Upgrading

### From Executable to Latest Version

Simply download the newest release and run it. Your config and database persist automatically.

### From Source Installation to Executable

1. Backup your database (see Troubleshooting above)
2. Download the executable for your platform
3. Run the new version
4. The app will use your existing database and configuration

## Getting Help

- [GitHub Issues](https://github.com/Shadowrom2020/KlipperVault/issues) — report bugs
- [README.md](../README.md) — feature overview
- [Macro Developer Guide](Macro_Developer.md) — for macro publishing
