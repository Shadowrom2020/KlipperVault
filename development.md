# KlipperVault Development Guide

This guide collects developer-focused workflows so [README.md](README.md) stays user-oriented.

## Local Environment Setup

Recommended setup from repository root:

```bash
bash scripts/setup_dev.sh
```

What it does:

1. Optionally installs distro system dependencies (apt/dnf/pacman/zypper/apk).
2. Creates `.venv` if missing.
3. Upgrades `pip`.
4. Installs `requirements.txt` and optional dev tools.
5. Generates workspace VS Code files:
   - `.vscode/settings.json`
   - `.vscode/launch.json`

VS Code debug configs use `${workspaceFolder}/.venv/bin/python` so launch runs with the project virtualenv.

Manual setup alternative:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## Run Locally

```bash
./.venv/bin/python klipper_vault.py
```

Use a custom test config tree:

```bash
KLIPPER_CONFIG_DIR=/tmp/testcfg ./.venv/bin/python klipper_vault.py
```

## Project Layout

```text
klipper_vault.py                  Application entry point
install.sh                        Systemd + virtualenv installer
uninstall.sh                      Service removal helper
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
  klipper_macro_watcher.py        Config file watcher
  klipper_vault_config.py         klippervault.cfg handling
  klipper_vault_db.py             Shared SQLite helpers
```

## Macro Sharing Implementation Notes

Current share/import behavior:

- Export format id: `klippervault.macro-share.v1`
- Export supports selecting one or multiple latest macro identities.
- Export payload includes source printer vendor/model metadata.
- Import stores macros as latest inactive rows marked `is_new=1`.
- Imported macros default to `macros.cfg`.
- Import ensures `[include macros.cfg]` exists in `printer.cfg`.

Relevant backend APIs live in [src/klipper_macro_indexer.py](src/klipper_macro_indexer.py):

- `export_macro_share_payload`
- `import_macro_share_payload`

UI wiring for export/import dialogs and upload/download lives in [src/klipper_macro_gui.py](src/klipper_macro_gui.py).

## Checks Before Commit

Run syntax checks on changed Python files (minimum requirement):

```bash
python3 -m py_compile src/klipper_macro_gui.py
python3 -m py_compile src/klipper_macro_indexer.py src/klipper_macro_backup.py
python3 -m py_compile src/klipper_macro_gui_service.py src/klipper_macro_viewer.py
```

Run tests:

```bash
./.venv/bin/python -m pytest -q
```

Optional CI-like checks:

```bash
./.venv/bin/ruff check .
PYTHONPATH=src ./.venv/bin/mypy src klipper_vault.py --ignore-missing-imports
./.venv/bin/python -m py_compile klipper_vault.py src/*.py
PYTHONPATH=src ./.venv/bin/pytest -q
./.venv/bin/pip-audit
./.venv/bin/bandit -q -r src klipper_vault.py -s B608
```

## Coding Conventions

- Keep edits minimal and in scope.
- Preserve existing NiceGUI patterns and callback structure.
- Keep parser/indexer dependency-free where possible.
- Add schema changes through migration-safe updates in `ensure_schema()` / `ensure_backup_schema()`.
- Do not hardcode runtime port values; use config.

## Adding a New UI Action

1. Add backend behavior in [src/klipper_macro_indexer.py](src/klipper_macro_indexer.py) or [src/klipper_macro_backup.py](src/klipper_macro_backup.py).
2. Expose it in [src/klipper_macro_gui_service.py](src/klipper_macro_gui_service.py).
3. Wire UI in [src/klipper_macro_gui.py](src/klipper_macro_gui.py).
4. Guard mutating paths when printer is printing.
5. Run compile checks and tests.

## Dev Troubleshooting

VS Code debug cannot import `nicegui`:

- Ensure interpreter is `${workspaceFolder}/.venv/bin/python`.
- Ensure debug `launch.json` entries use project interpreter.
- Verify from terminal:

```bash
./.venv/bin/python -c "import nicegui; print(nicegui.__version__)"
```

Local app startup problems:

- Check `klippervault.cfg` for malformed values.
- Validate the `.venv` interpreter exists and dependencies installed.
- Read service logs (`journalctl`) if running as systemd service.
