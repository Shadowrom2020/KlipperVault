# KlipperVault Agent Hints

## Project Layout
- Entry point: `klipper_vault.py`
- Source modules: `src/`
- Main UI: `src/klipper_macro_gui.py`
- Parser/indexer: `src/klipper_macro_indexer.py`
- Backup logic: `src/klipper_macro_backup.py`

## Runtime and Config
- App settings are stored in SQLite table `vault_settings`.
- UI port comes from `vault_settings.port`.
- Version history retention comes from `vault_settings.version_history_size`.
- App version is read from repository file `VERSION`.

## Common Workflows
- Launch UI: `python3 klipper_vault.py`
- Syntax check touched files: `python3 -m py_compile <file1> <file2> ...`
- Re-index macros from UI: "Scan macros" button.
- Create backups from UI: "Backup" button in top toolbar.

## UI Behavior Notes
- Left panel lists macros with status badges.
- Center panel shows selected macro details and version compare.
- Right panel lists backups with actions:
  - View contents (magnify icon)
  - Restore (restore icon)
  - Delete (trash icon)

## Implementation Notes
- Keep parser dependency-free and lightweight.
- Avoid changing DB schema names unless adding a migration path.
- `ensure_schema()` in indexer is called by most DB read paths; schema additions should remain backward-safe.
- Backup table migrations live in `ensure_backup_schema()`.

## Change Safety Checklist
1. Update imports if files are moved under `src/`.
2. Keep `klipper_vault.py` as the single runtime launcher.
3. After parser/indexer changes, run a compile check and at least one quick parse smoke test.
4. After GUI changes, run compile check and confirm callback wiring paths.
