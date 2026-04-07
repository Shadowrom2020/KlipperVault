# KlipperVault

![KlipperVault logo](assets/logo.svg)

KlipperVault is a lightweight web UI for managing Klipper `gcode_macro` definitions with version history, safe editing workflows, backup/restore, duplicate handling, and Mainsail integration.

**[📸 View UI Overview with Screenshots](overview.md)**

## Overview

KlipperVault scans your Klipper config tree, indexes every `[gcode_macro ...]` section in SQLite, and presents the results in a NiceGUI interface.

It is built for Klipper systems that keep configuration in `~/printer_data/config` and want safer macro maintenance than editing raw `.cfg` files alone.

## Key Features

- Automatic macro version history (changes are stored only when content differs).
- Active/inactive and loaded/not-loaded state tracking across include chains.
- Dynamic macro awareness for configs loaded via `[dynamicmacros]` `configs:` entries.
- Dynamic macro status badge (`Dynamic`) and dedicated `Reload Dynamic Macros` action.
- Duplicate macro detection with guided conflict resolution.
- In-place macro editing with write-back to cfg files.
- Backup and restore of both indexed rows and cfg snapshots.
- Macro sharing workflow:
  - Export one or multiple macros into a portable share JSON file.
  - Attach source printer vendor/model metadata.
  - Import via file upload as inactive `NEW` entries for review first.
  - Imported macros default to `macros.cfg`; include is ensured in `printer.cfg`.
- Online macro updates from GitHub repositories:
  - Check for updates from optional GitHub-hosted update repository.
  - Import updates as new inactive versions for selective activation.
  - Export local macros as repository bundle (developer mode only).
- Moonraker print-state safety gates for mutating actions.
- Optional script explanation panel with macro-to-macro cross-links.

Dynamic Macros project:
- https://github.com/3DCoded/DynamicMacros

## Requirements

- Linux
- Python 3 with `venv` support
- systemd
- Klipper config directory (typically `~/printer_data/config`)
- Moonraker reachable at `http://127.0.0.1:7125` (default)

Dependencies are defined in [requirements.txt](requirements.txt).

## Default Paths

- Config directory: `~/printer_data/config`
- Database: `~/printer_data/db/klipper_macros.db`
- App config: `~/printer_data/config/klippervault.cfg`
- Default HTTP port: `10090`
- Default Moonraker URL: `http://127.0.0.1:7125`

## Configuration

KlipperVault creates `klippervault.cfg` on first start if it does not exist.

```ini
[vault]
version_history_size: 5
port: 10090
ui_language: en
printer_vendor:
printer_model:
online_update_repo_url:
online_update_manifest_path: updates/manifest.json
online_update_ref: main
developer: false
```

- `version_history_size`: max stored versions per macro
- `port`: web UI port
- `ui_language`: UI language (`en`, `de`)
- `printer_vendor`: optional printer vendor shown in UI and exported share metadata
- `printer_model`: optional printer model shown in UI and exported share metadata
- `online_update_repo_url`: optional GitHub URL for macro update repository (e.g., `https://github.com/owner/macros`)
- `online_update_manifest_path`: path to manifest file inside the update repository (default: `updates/manifest.json`)
- `online_update_ref`: branch, tag, or commit SHA for update checks (default: `main`)
- `developer`: enable developer features including export of local macros to ZIP bundles (default: `false`)

If vendor/model are missing, KlipperVault prompts for them and writes them back to config.

## Installation

From repository root:

```bash
sudo ./install.sh
```

Installer summary:

1. Detect target user
2. Create virtualenv
3. Install Python dependencies
4. Write and enable `klipper-vault.service`
5. Add/update KlipperVault entry in Mainsail `.theme/navi.json`

Uninstall:

```bash
sudo ./uninstall.sh
sudo ./uninstall.sh --remove-venv
```

## Running

Manual run:

```bash
./.venv/bin/python klipper_vault.py
```

Service management:

```bash
sudo systemctl restart klipper-vault.service
sudo systemctl status klipper-vault.service
sudo journalctl -u klipper-vault.service -f
```

## Usage

Typical flow:

1. Open KlipperVault.
2. Click `Scan macros`.
3. Select a macro and review details/history.
4. Edit latest non-deleted version.
5. For dynamic macros, editing is allowed even while printing.
6. Use `Reload Dynamic Macros` to apply dynamic-macro changes without a full Klipper restart.
7. Save and re-index.

Share/import flow:

1. Click `Export macros`.
2. Select one or more macros.
3. Confirm export to trigger direct download of a share JSON file.
4. On another system, click `Import macros` and upload that JSON file.
5. Review imported `NEW` inactive entries, then enable individually.

Online updates flow:

1. Configure `online_update_repo_url` and optional `online_update_manifest_path`, `online_update_ref` in `klippervault.cfg`.
2. Click `Check for updates` to fetch the manifest and compare local macros against remote versions.
3. Review available updates in the dialog; select which ones to activate.
4. Click `Import updates` to add new versions; activate selectively or defer.
5. Updated macros appear as `NEW` inactive versions for review before enabling.

Developer mode (export repository bundle):

1. Set `developer: true` in `klippervault.cfg`.
2. Click `Macro actions` → `Export Update Repo ZIP` to download an archive.
3. The ZIP contains `updates/manifest.json` and `vendor/model/macro.json` structure ready for upload to your update repository.

Compatibility behavior:

- Share files carry source printer vendor/model.
- Import warns when source printer metadata is unknown or differs from local printer metadata.
- Online updates use checksum comparison to detect changes; only changed macros appear in the update list.

## Safety Model

When Moonraker reports `printing`, KlipperVault blocks most mutating actions (import/export/backup/restore/duplicate resolution), pauses watcher writes, and shows a warning.

Exception for dynamic macros:
- Dynamic macros remain editable while printing.
- `Reload Dynamic Macros` remains available while printing and triggers Klipper command `DYNAMIC_MACRO` via Moonraker.

## Troubleshooting

App does not start:

- Check `klippervault.cfg` port value.
- Confirm virtualenv and dependencies were installed.
- Check service logs with `journalctl`.

No macros found:

- Verify config files exist under `~/printer_data/config`.
- Check `printer.cfg` includes and file readability.
- Trigger a manual scan.

Editing is disabled:

- Confirm printer is not actively printing.
- Verify Moonraker connectivity.
- Ensure selected row is latest and non-deleted.

## Developer Docs

Developer setup, architecture, checks, and contribution guidance are in [development.md](development.md).

## License

KlipperVault is licensed under GPL-3.0-or-later. See [LICENSE](LICENSE).