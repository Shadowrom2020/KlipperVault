# KlipperVault Development Guide

This guide collects developer-focused workflows so [README.md](../README.md) stays user-oriented.

Scope: this file covers extending and maintaining KlipperVault itself (code, architecture, tests, and tooling).
For online update publishing workflows (Developer menu, Export Update Zip, Create Pull Request), use [Macro_Developer.md](Macro_Developer.md).

## Local Environment Setup

Recommended setup from repository root:

```bash
bash scripts/setup_dev.sh
```

Default toolchain target:

- Python runtime is managed via `pyenv`.
- Setup script resolves and installs the latest available `3.13.x` on each run.
- Pin an exact patch only when needed: `PYENV_PYTHON_VERSION=<version> ./scripts/setup_dev.sh`.

What it does:

1. Optionally installs distro system dependencies (apt/dnf/pacman/zypper/apk).
2. Creates `.venv` if missing.
3. Upgrades `pip`.
4. Installs `requirements.txt` and optional dev tools.
5. Generates workspace VS Code files:
   - `.vscode/settings.json`
   - `.vscode/launch.json`

VS Code debug configs use `${workspaceFolder}/.venv/bin/python` so launch runs with the project virtualenv.

Generated debug configurations include full app workflows:

- `Python: KlipperVault GUI (off_printer debug)`

Manual setup alternative:

```bash
pyenv install -s 3.13.3
~/.pyenv/versions/3.13.3/bin/python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## Run Locally

```bash
./.venv/bin/python klipper_vault_gui.py
```

Notes:

- In `off_printer` mode, indexing mirrors remote cfg files into a local OS-standard data directory before scan.
- Mutating operations (edit/delete/restore/duplicate resolve/backup restore) sync changed cfg content back to remote over SSH.
- Manual/startup scan is intentionally blocked until an active SSH profile with credentials is configured.

## Project Layout

```text
klipper_vault_gui.py              Primary GUI entry point wrapper
install.sh                        Remote-host virtualenv installer
uninstall.sh                      Remote-host uninstall helper
VERSION                           App version string
src/
  klipper_macro_gui.py            NiceGUI page and UI wiring
  klipper_macro_viewer.py         Macro viewer and explanation wiring
  klipper_macro_editor.py         Reusable inline macro editor
  klipper_macro_explainer.py      G-code explanation heuristics
  klipper_macro_explainer_view.py Reusable explanation panel and macro popup
  klipper_macro_gui_service.py    Service layer for UI actions
  klipper_macro_indexer.py        Parser, indexer, versioning, cfg rewrites
  klipper_macro_backup.py         Backup and restore support
  klipper_macro_compare.py        Version compare UI
  klipper_macro_compare_core.py   Diff logic used by compare dialog
  klipper_vault_ssh_transport.py  SSH/SFTP read-write transport for off_printer mode
  klipper_vault_remote_profiles.py SQLite profile metadata and credential index tables
  klipper_vault_secret_store.py   Keyring-first secret storage with DB fallback
  klipper_vault_config.py         SQLite-backed app settings handling
  klipper_vault_paths.py          Runtime-aware default config/db path resolution
  klipper_vault_db.py             Shared SQLite helpers
```

## Runtime Mode

Supported `runtime_mode` value from app settings:

- `off_printer`: SSH/SFTP-driven remote config workflow.

Off-printer persistence model:

- App settings are stored in SQLite (`vault_settings`).
- SSH host profiles are stored in SQLite (`ssh_host_profiles`).
- Secret backend metadata is tracked in `credential_store_index`.
- Credential values are stored in OS keyring when available; SQLite fallback is used otherwise.

Primary APIs and modules:

- [src/klipper_macro_gui_service.py](../src/klipper_macro_gui_service.py)
- [src/klipper_vault_remote_profiles.py](../src/klipper_vault_remote_profiles.py)
- [src/klipper_vault_secret_store.py](../src/klipper_vault_secret_store.py)
- [src/klipper_vault_ssh_transport.py](../src/klipper_vault_ssh_transport.py)

## Macro Sharing Implementation Notes

Current share/import behavior:

- Export format id: `klippervault.macro-share.v1`
- Export supports selecting one or multiple latest macro identities.
- Export payload includes source printer vendor/model metadata.
- Import stores macros as latest inactive rows marked `is_new=1`.
- Imported macros default to `macros.cfg`.
- Import ensures `[include macros.cfg]` exists in `printer.cfg`.

Relevant backend APIs live in [src/klipper_macro_indexer.py](../src/klipper_macro_indexer.py):

- `export_macro_share_payload`
- `import_macro_share_payload`

UI wiring for export/import dialogs and upload/download lives in [src/klipper_macro_gui.py](../src/klipper_macro_gui.py).

## UI Translation Workflow

KlipperVault now uses a gettext-first translation workflow with Babel:

- Catalog template: `src/locales/klippervault.pot`
- Language catalogs: `src/locales/<lang>/LC_MESSAGES/klippervault.po`
- Compiled runtime catalogs: `src/locales/<lang>/LC_MESSAGES/klippervault.mo`

Refresh translation catalogs after adding or changing `t("...")` strings:

```bash
./.venv/bin/pybabel extract -F babel.ini -o src/locales/klippervault.pot src klipper_vault_gui.py
./.venv/bin/pybabel update -i src/locales/klippervault.pot -d src/locales -D klippervault
./.venv/bin/pybabel compile -d src/locales -D klippervault
```

Convenience target:

```bash
make i18n
```

Additional targets in [Makefile](../Makefile):

- `make i18n-extract`
- `make i18n-update`
- `make i18n-compile`

Migration status:

- Runtime translations are now loaded from gettext catalogs (`.mo`) only.
- Translation source of truth is now `src/locales/<lang>/LC_MESSAGES/klippervault.po`.

## Building Standalone Executables

KlipperVault can be packaged as native executables for Windows, macOS, and Linux using PyInstaller.

### Local Build

Build a standalone executable for your current platform:

```bash
make bundle
```

This creates:
- `dist/KlipperVault` (Linux)
- `dist/KlipperVault.exe` (Windows)
- `dist/KlipperVault.app` (macOS)

The executable includes:
- Python 3.13 runtime (no system Python needed)
- All dependencies (NiceGUI, paramiko, keyring, Babel, etc.)
- Translation catalogs (en, de, fr)
- Bundled assets (favicon, version metadata)

Size: ~55–60 MB executable, ~75–95 MB when packaged.

### Releasing with Executables

#### 1. Tag the Release

Create an annotated git tag on main:

```bash
git tag -a v0.4.0 -m "Release version 0.4.0"
git push origin v0.4.0
```

This triggers the automated release workflow.

#### 2. Automated Workflow

The GitHub Actions `build-executables.yml` workflow:

1. **Builds on CI-supported platforms** (parallel):
   - Linux (ubuntu-latest)
   - Windows (windows-latest)

2. **For each platform**:
   - Installs Python 3.13
   - Installs build dependencies (`requirements.txt` + `requirements-build.txt`)
   - Runs `python scripts/build_executable.py` (PyInstaller)
   - Builds native installers if tools available:
     - `.exe` (Windows via Inno Setup)
     - `.AppImage` (Linux)
   - Creates fallback ZIP archives (always available)
   - Runs smoke test: `scripts/test_packaged_executable.py`

3. **Publishes automatically**:
   - Downloads all artifacts from CI build jobs
   - Creates GitHub Release with the tag
   - Uploads all packages (installers + ZIPs + signatures)

macOS artifacts are built manually on macOS hosts and uploaded separately during release preparation.

#### 3. What Gets Published

For each platform:
- Native installer/artifact if available (`.exe`, macOS local artifact, `.AppImage`)
- ZIP archive with contents
- `.asc` signature file (GPG signed if configured)

Results appear on [Releases](https://github.com/Shadowrom2020/KlipperVault/releases).

#### 4. Release Notes

Add release notes before tagging by editing [.github/RELEASE_NOTES_TEMPLATE.md](../.github/RELEASE_NOTES_TEMPLATE.md).

The template includes:
- Features
- Bug fixes
- Breaking changes
- Installation instructions
- Documentation links

GitHub will use this as the default release description.

### Manual Workflow Dispatch

Trigger builds manually without creating a tag:

1. Go to **Actions** → **Build Executables**
2. Click **Run workflow**

This builds but does not auto-publish (no GitHub release created).

### Testing the Build Locally

```bash
# Clean previous builds
make clean

# Build new executable
make bundle

# Run the executable
./dist/KlipperVault
```

or for Windows:
```powershell
.\dist\KlipperVault.exe
```

### Troubleshooting Builds

Missing dependencies for installers:

- **Windows**: Inno Setup (`choco install innosetup`)
- **macOS**: Built-in (uses .app bundle)
- **Linux**: `appimage-builder` or `appimagetool`

If installer tools are unavailable, the workflow falls back to ZIP archives.

Python syntax errors:

```bash
python3 -m py_compile src/klipper_macro_gui.py src/klipper_macro_indexer.py
```

## Checks Before Commit

Run syntax checks on changed Python files (minimum requirement):

```bash
python3 -m py_compile src/klipper_macro_gui.py
python3 -m py_compile src/klipper_macro_indexer.py src/klipper_macro_backup.py
python3 -m py_compile src/klipper_macro_gui_service.py src/klipper_macro_viewer.py
python3 -m py_compile src/klipper_vault_remote_profiles.py src/klipper_vault_secret_store.py src/klipper_vault_ssh_transport.py
```

Run tests:

```bash
./.venv/bin/python -m pytest -q
```

Optional CI-like checks:

```bash
./scripts/ci_parity.sh
```

Modes:

```bash
./scripts/ci_parity.sh --quality
./scripts/ci_parity.sh --security
```

## Coding Conventions

- Keep edits minimal and in scope.
- Preserve existing NiceGUI patterns and callback structure.
- Keep parser/indexer dependency-free where possible.
- Add schema changes through migration-safe updates in `ensure_schema()` / `ensure_backup_schema()`.
- Do not hardcode runtime port values; use config.

## Adding a New UI Action

1. Add backend behavior in [src/klipper_macro_indexer.py](../src/klipper_macro_indexer.py) or [src/klipper_macro_backup.py](../src/klipper_macro_backup.py).
2. Expose it in [src/klipper_macro_gui_service.py](../src/klipper_macro_gui_service.py).
3. Wire UI in [src/klipper_macro_gui.py](../src/klipper_macro_gui.py).
4. Guard mutating paths when printer is printing.
5. Run compile checks and tests.

## Docs Consistency Checklist

When UI or behavior changes, update docs in the same PR and verify:

- Menu/action names match current UI labels exactly.
- Config keys and defaults match `klipper_vault_config.py` and the in-app `Macro actions -> Settings` dialog.
- Feature descriptions align across [README.md](../README.md), [overview.md](overview.md), and [Macro_Developer.md](Macro_Developer.md).
- Any startup/background behavior changes are described in user-facing docs.
- Security/token handling text reflects implemented behavior only.

## Dev Troubleshooting

VS Code debug cannot import `nicegui`:

- Ensure interpreter is `${workspaceFolder}/.venv/bin/python`.
- Ensure debug `launch.json` entries use project interpreter.
- Verify from terminal:

```bash
./.venv/bin/python -c "import nicegui; print(nicegui.__version__)"
```

Local app startup problems:

- Check app settings in `Macro actions -> Settings` for malformed values.
- Validate the `.venv` interpreter exists and dependencies installed.
