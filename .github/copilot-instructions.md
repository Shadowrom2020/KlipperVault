# Copilot Instructions for KlipperVault

Use these repository-specific hints when assisting in this codebase.

## Primary Goal
Maintain a lightweight Klipper macro vault with:
- Fast `.cfg` parsing
- Versioned macro history in SQLite
- NiceGUI-based browsing, compare, backup, and restore flows
- Moonraker API integration for macro updates and printer status
- Mainsail UI integration for macro editing and management

## Codebase Facts
- Runtime starts from `klipper_vault_gui.py`.
- All source files are under `src/`.
- The GUI module builds UI only; launcher handles `main()` and `ui.run(...)`.
- Current app version is stored in `VERSION`.

## Database and Parsing
- Main macro table: `macros`.
- Backup tables: `macro_backups`, `macro_backup_items`.
- Macro parser supports full gcode blocks until next real section header.
- Trailing comment-only lines at end of gcode are trimmed before storage.

## Config Rules
- App settings are stored in SQLite table `vault_settings`.
- Runtime-critical settings include:
  - `version_history_size`
  - `port`
- Do not hardcode static UI port values in runtime logic.

## Style and Safety
- Prefer minimal, focused edits.
- Preserve existing NiceGUI and SQLite patterns.
- Add migration-safe schema updates only.
- Run `py_compile` on changed Python files before finishing.

## Helpful Commands
- `python3 klipper_vault_gui.py`
- `python3 -m py_compile src/klipper_macro_gui.py`
- `python3 -m py_compile src/klipper_macro_indexer.py src/klipper_macro_backup.py`
