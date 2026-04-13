# Standalone Executable Features & Benefits

This document summarizes what changed with the introduction of standalone executables for KlipperVault.

## For End Users

### Easy Installation (No Technical Knowledge Required)

- **Windows**: Download `.exe` installer, double-click, done
- **macOS**: Download `.dmg`, drag to Applications
- **Linux**: Download `.AppImage`, make executable, run

No need to install Python, create virtualenvs, or understand pip.

### Native Application Experience

- Appears in Applications menu and search
- Desktop shortcuts and taskbar icons
- Start Menu entries (Windows)
- Double-clickable from file browser
- Auto-launches web browser to localhost:10090

### Automatic Updates

- Config and database automatically preserved when upgrading
- No manual migration steps
- Version history intact

## For Developers

### Source Installation Still Supported

Developers can still clone the repo and run from source:

```bash
git clone https://github.com/Shadowrom2020/KlipperVault.git
cd KlipperVault
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 klipper_vault_gui.py
```

Nothing changed for source-based workflows.

### Custom Builds

Developers can build custom executables with PyInstaller:

```bash
make bundle
```

This creates `dist/KlipperVault` (Linux), `dist/KlipperVault.exe` (Windows), or `dist/KlipperVault.app` (macOS).

## Build Artifacts

### What's Included in Packages

All built artifacts include:

- Standalone Python interpreter (no system Python needed)
- All runtime dependencies (NiceGUI, paramiko, keyring, Babel, etc.)
- Compiled translation files (.mo files for en, de, fr)
- Bundled assets (favicon, icons)
- Version metadata

### Size Reference

- Windows installer: ~80-100MB
- macOS DMG: ~100-120MB
- Linux AppImage: ~100-130MB
- ZIP archives: ~75-95MB

Larger than a simple script, but includes everything needed to run.

## Platform-Specific Details

### Windows (.exe installer via Inno Setup)

- Creates Start Menu shortcuts
- Optional Desktop shortcut
- Uninstaller available in Control Panel
- Registry entries for file associations (optional)
- Preserves config at `%APPDATA%\KlipperVault`
- Database at `%LOCALAPPDATA%\KlipperVault`

### macOS (.dmg disk image)

- Drag-and-drop installation
- `.app` bundle with macOS integration
- Icon visible in Applications folder
- Spotlight searchable
- Preserves config at `~/Library/Application Support/KlipperVault`
- Gatekeeper safe (requires notarization in future releases)

### Linux (.AppImage)

- Single executable file — no installation needed
- Mountable as a virtual filesystem
- Integrated `.desktop` file for app menus
- No system-level integration required
- Works on any glibc-based Linux distro

## Security & Trust

### Code Signing (Roadmap)

- **Windows**: Signed with Sectigo or DigiCert certificate (planned)
- **macOS**: Signed and notarized (removes Gatekeeper warnings) (planned)
- **Linux**: GPG signatures for verification (supported now)

See [RELEASE_SIGNING.md](../RELEASE_SIGNING.md) for details.

### Data & Privacy

No changes to KlipperVault's privacy model:

- All connections are to your printer via SSH/Moonraker
- Config and database stored locally on your machine
- No cloud sync, telemetry, or external calls
- Same as source installation

## CI/CD & Automation

### GitHub Actions Integration

Automatic builds triggered on:

- Tag push (e.g., `git tag -a v0.4.0`)
- Manual workflow dispatch

Matrix build creates:

- Linux executable (x64)
- Windows executable (x64)
- macOS executables (Intel x64 + Apple Silicon arm64)

All packaged as:

- Native installers (if tools available): .exe, .dmg, .AppImage
- ZIP fallback archives (always available)

### Release Automation

Tagged releases automatically:

- Build on all four platforms
- Package as native installers
- Smoke-test executables
- Publish to GitHub Releases

One `git tag` triggers everything.

## Backward Compatibility

### Existing Installations

- Source virtualenv installations continue to work unchanged
- Can upgrade to executables without data loss
- Config and database are platform-agnostic

### Database & Config Format

- Same SQLite database format
- Same config directory structure
- Same `VERSION` and `assets/` layout
- No migration required

## Known Limitations

### Platform Differences

- Windows SmartScreen may warn on first run (will improve with code signing)
- macOS requires manual security approval (will improve with notarization)
- Linux AppImage requires `*.AppImage.asc` for verification (security best practice)

### Deployment Scenarios

- Cannot easily run as a system service (use source install with systemd)
- Cannot easily customize dependencies (use source install and pip)
- Cannot be distributed via distro package managers yet (Linux only)

## Future Improvements

- [ ] Code signing + notarization for secure distribution
- [ ] Homebrew tap for macOS (`brew install klippervault`)
- [ ] Linux distro packages (DEB, RPM)
- [ ] Auto-update mechanism (check for new releases at startup)
- [ ] Tray icon / taskbar integration (platform-specific)
- [ ] Drag-and-drop macro import from file browser
- [ ] Custom favicon/branding for enterprise deployments

## Troubleshooting

See [INSTALLATION_GUIDE.md](../INSTALLATION_GUIDE.md) for common issues and solutions.

---

**Questions?** Open an issue on [GitHub](https://github.com/Shadowrom2020/KlipperVault/issues).
