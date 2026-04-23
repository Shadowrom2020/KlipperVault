# Release Notes Template

Use this template for GitHub Releases triggered by version tags (`v*`).

---

## KlipperVault vX.Y.Z

Release date: YYYY-MM-DD

### Highlights

- [ ] Major feature or improvement #1
- [ ] Major feature or improvement #2
- [ ] Key fix #1

### Added

- Item
- Item

### Changed

- Item
- Item

### Fixed

- Item
- Item

### Installers and Artifacts

Recommended: use the native installer for your platform.

- Windows installer: `KlipperVault-X.Y.Z-windows-x64.exe`
- macOS local build instructions: [Docs/MacOS.md](https://github.com/Shadowrom2020/KlipperVault/blob/main/Docs/MacOS.md)
- Linux AppImage: `KlipperVault-X.Y.Z-linux-x64.AppImage`
- Portable archives: `KlipperVault-X.Y.Z-<platform>.zip`

If an installer is unavailable for a platform in this release, use the ZIP artifact.

### Upgrade Notes

- Existing config and database are preserved.
- Source-based installs continue to work.
- If you run from source, no migration is required.

See [INSTALLATION_GUIDE.md](https://github.com/Shadowrom2020/KlipperVault/blob/main/INSTALLATION_GUIDE.md) for install details.

### Security and Signing

- Windows code signing: [signed / unsigned]
- macOS notarization: [notarized / not notarized]
- Linux signature: [signed / unsigned]

Verification command for Linux signatures:

`gpg --verify KlipperVault-*.AppImage.asc`

### Known Issues

- Issue + workaround
- Issue + workaround

### Contributors

- @username - contribution
- @username - contribution

### Documentation

- [Installation Guide](https://github.com/Shadowrom2020/KlipperVault/blob/main/INSTALLATION_GUIDE.md)
- [Development Guide](https://github.com/Shadowrom2020/KlipperVault/blob/main/development.md)
- [Release Signing Guide](https://github.com/Shadowrom2020/KlipperVault/blob/main/RELEASE_SIGNING.md)

---

## Release Manager Checklist

- [ ] `VERSION` updated
- [ ] Tag created and pushed (example: `vX.Y.Z`)
- [ ] `build-executables` workflow green for all matrix targets
- [ ] Artifacts uploaded to GitHub Release
- [ ] At least one artifact smoke-tested per platform
- [ ] Signing/notarization status accurately documented
- [ ] Notes reviewed for correctness and clarity
