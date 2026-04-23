# Code Signing and Release Preparation

This guide documents how to sign and notarize KlipperVault executables for secure distribution across platforms.

## macOS: Signing and Notarization

macOS Gatekeeper will warn or block unsigned executables. To distribute via GitHub Releases or direct download, sign and notarize your builds.

### Prerequisites

- Apple Developer Account (for signing certificate and notarization)
- `codesign` and `notarytool` (included in Xcode Command Line Tools)

### Setup: Create Signing Certificate

1. Go to [Apple Developer Account](https://developer.apple.com/account)
2. Under "Certificates, Identifiers & Profiles", create a new "Developer ID Application" certificate
3. Download the certificate and import it into your Keychain

### Sign the macOS App Bundle

```bash
# After building the .app bundle with make bundle
codesign --deep --force --verify --verbose --sign "Developer ID Application" \
  dist/KlipperVault.app
```

Verify the signature:

```bash
codesign --verify --verbose dist/KlipperVault.app
```

### Sign and Package the macOS Artifact

```bash
# After building the local macOS release artifact (see Docs/MacOS.md)
MACOS_ARTIFACT="$(ls release/KlipperVault-* | head -n 1)"
codesign --force --sign "Developer ID Application" "$MACOS_ARTIFACT"
```

### Notarize the macOS Artifact (Required for distribution)

```bash
# Submit the artifact for notarization
MACOS_ARTIFACT="$(ls release/KlipperVault-* | head -n 1)"
notarytool submit "$MACOS_ARTIFACT" --apple-id "your-apple-id@example.com" \
  --password "@keychain:app-specific-password" --team-id "XXXXX"

# Check notarization status (use the request ID from submit output)
notarytool info <request-id> --apple-id "your-apple-id@example.com" \
  --password "@keychain:app-specific-password" --team-id "XXXXX"

# When complete, staple the notarization to the artifact
xcrun stapler staple "$MACOS_ARTIFACT"
```

### CI Integration (Optional)

To automate signing in GitHub Actions, store your Apple ID and app-specific password as repository secrets:

```yaml
- name: Sign and Notarize macOS Artifact (macOS only)
  if: runner.os == 'macOS' && startsWith(github.ref, 'refs/tags/')
  env:
    APPLE_ID: ${{ secrets.APPLE_ID }}
    APPLE_PASSWORD: ${{ secrets.APPLE_PASSWORD }}
    APPLE_TEAM_ID: ${{ secrets.APPLE_TEAM_ID }}
  run: |
    codesign --deep --force --sign "$APPLE_TEAM_ID" dist/KlipperVault.app
    python3 scripts/build_dmg_installer.py
    MACOS_ARTIFACT="$(ls release/KlipperVault-* | head -n 1)"
    codesign --force --sign "$APPLE_TEAM_ID" "$MACOS_ARTIFACT"
    notarytool submit "$MACOS_ARTIFACT" --apple-id "$APPLE_ID" \
      --password "$APPLE_PASSWORD" --team-id "$APPLE_TEAM_ID" --wait
```

## Windows: Code Signing

Windows SmartScreen and many security tools will show warnings for unsigned executables. Use Sectigo or DigiCert code signing certificates.

### Prerequisites

- Code signing certificate (from Sectigo, DigiCert, or similar)
- `signtool` (included in Windows SDK)

### Sign the Executable

```bash
signtool sign /f certificate.pfx /p YOUR_PASSWORD /fd SHA256 \
  /tr http://timestamp.sectigo.com /td SHA256 \
  dist/KlipperVault.exe
```

### Sign the Installer

```bash
signtool sign /f certificate.pfx /p YOUR_PASSWORD /fd SHA256 \
  /tr http://timestamp.sectigo.com /td SHA256 \
  release/KlipperVault-*.exe
```

Verify the signature:

```bash
signtool verify /pa release/KlipperVault-*.exe
```

### CI Integration (Optional)

Store your code signing certificate as a repository secret and decode it in CI:

```yaml
- name: Sign Windows Installer
  if: runner.os == 'Windows' && startsWith(github.ref, 'refs/tags/')
  env:
    CERTIFICATE: ${{ secrets.WINDOWS_CODESIGN_CERT }}
    CERTIFICATE_PASSWORD: ${{ secrets.WINDOWS_CODESIGN_PASSWORD }}
  run: |
    # Decode base64-encoded certificate
    [System.Convert]::FromBase64String($env:CERTIFICATE) | Set-Content cert.pfx -AsByteStream
    
    # Sign installer
    & "C:\Program Files (x86)\Windows Kits\10\bin\x64\signtool.exe" sign `
      /f cert.pfx /p $env:CERTIFICATE_PASSWORD /fd SHA256 `
      /tr http://timestamp.sectigo.com /td SHA256 `
      release/KlipperVault-*.exe
    
    Remove-Item cert.pfx
```

## Linux: GPG Signatures

Linux users typically verify GPG signatures. Sign your AppImage and installer packages.

### Prerequisites

- GPG key pair (create with `gpg --gen-key` if needed)

### Sign the AppImage

```bash
gpg --detach-sign --armor release/KlipperVault-*.AppImage
```

This creates a `.asc` signature file alongside the AppImage.

### Publish GPG Public Key

```bash
gpg --export -a "your-name" > klippervault-gpg-key.asc
```

Include this file in your GitHub Release notes so users can verify signatures:

```bash
# Verify an AppImage
gpg --verify KlipperVault-*.AppImage.asc KlipperVault-*.AppImage
```

## Release Checklist

Before publishing a release:

- [ ] Increment version in `VERSION` file
- [ ] Update `CHANGELOG.md` with release notes
- [ ] Test packaged builds locally on all platforms
- [ ] Sign Windows executable and installer
- [ ] Sign and notarize macOS release artifact
- [ ] GPG-sign Linux AppImage
- [ ] Push tag (e.g., `git tag -a v0.3.1 -m "Release 0.3.1"`)
- [ ] Verify GitHub Actions build-executables workflow completes
- [ ] Download and test at least one artifact from each platform
- [ ] Review release notes before publishing to GitHub Releases
- [ ] Pin release digest or tag in any third-party package repositories

## References

- [Apple Developer: Code Signing](https://developer.apple.com/support/code-signing/)
- [Apple Developer: Notarizing macOS Software](https://developer.apple.com/documentation/notaryapi)
- [Microsoft: SignTool.exe Code Signing](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool)
- [GNU Privacy Guard: GPG Manual](https://gnupg.org/documentation/manuals.html)
