# Release Notes Template

Use this template when creating a GitHub Release for KlipperVault.

---

## KlipperVault X.X.X — Release Notes

### ✨ What's New

- **Feature name**: Brief description
- **Feature name**: Brief description
- **Bug fix**: Fixed issue #123

### 🔄 Changes

- Changed behavior of X
- Improved Y performance by Z%
- Updated translation for language

### 🐛 Bug Fixes

- Fixed crash when doing X
- Fixed incorrect Y display in Z scenario
- Fixed SSH connection timeout handling

### 📦 Installation

**Recommended**: Download the executable for your platform below — no Python installation needed.

- **Windows**: `KlipperVault-X.X.X-windows-x64.exe` — Inno Setup installer with Desktop shortcut
- **macOS**: `KlipperVault-X.X.X-macos.dmg` — Drag-and-drop disk image for Intel and Apple Silicon
- **Linux**: `KlipperVault-X.X.X-linux-x64.AppImage` — Universal desktop application image

Alternatively, extract from ZIP archive if you prefer a portable version without the installer.

### 🔐 Code Signing

- **Windows executable signed** with [certificate authority]
- **macOS app notarized** — safe to open without security warnings
- **Linux AppImage GPG-signed** — verify with `gpg --verify KlipperVault-*.AppImage.asc`

GPG public key: [link to key in repo]

### 📋 Migration from Source Installation

If upgrading from a virtualenv-based installation:

1. Download the executable for your platform
2. Run it — your existing config and database are automatically preserved
3. No data loss; all settings and macro history remain intact

See [INSTALLATION_GUIDE.md](https://github.com/Shadowrom2020/KlipperVault/blob/main/INSTALLATION_GUIDE.md) for details.

### 🙏 Contributors

- @username — feature or fix description
- @username — feature or fix description

### 📚 Documentation

- [Installation Guide](https://github.com/Shadowrom2020/KlipperVault/blob/main/INSTALLATION_GUIDE.md)
- [Macro Developer Guide](https://github.com/Shadowrom2020/KlipperVault/blob/main/Macro_Developer.md)
- [Release Signing (Code Signing & Notarization)](https://github.com/Shadowrom2020/KlipperVault/blob/main/RELEASE_SIGNING.md)

### ⚠️Known Issues

- Issue description and workaround
- Planned fix in version X.X.X+1

---

## Checklist Before Publishing

- [ ] Version incremented in `VERSION` file
- [ ] CHANGELOG.md updated with release notes
- [ ] All platform executables built and tested  
- [ ] Windows executable signed with certificate
- [ ] macOS app signed and notarized
- [ ] Linux AppImage GPG-signed
- [ ] GitHub Actions build-executables workflow completed successfully
- [ ] Downloaded and tested at least one artifact per platform
- [ ] Release notes formatted and ready
- [ ] Contributors credited appropriately

## Notes for Release Manager

- Consider security implications of any new dependencies
- Verify that no credentials or secrets are accidentally included in bundles
- Test on a clean machine to simulate real user experience
- Announce release in relevant community channels (Klipper Discord, etc.)
