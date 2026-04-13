# KlipperVault

![KlipperVault logo](assets/logo.svg)

KlipperVault is a lightweight web UI for managing Klipper `gcode_macro` definitions with version history, safe editing workflows, backup/restore, duplicate handling, and Mainsail integration.

**[📸 View UI Overview with Screenshots](overview.md)**

## Deprecation Notice: On-Printer Runtime Removed

Support for running KlipperVault directly on the printer host has been removed.

- Removed behavior: local/on-printer runtime mode and local printer-host execution workflow.
- Supported behavior: remote-only `off_printer` mode using SSH/SFTP + Moonraker API.

If you still have an older on-printer installation, remove it to avoid port conflicts,
stale services, and accidental execution of unsupported code paths.

### Uninstall Old On-Printer Versions (Detailed)

Run the helper script from the repository root on the printer host (for example via SSH):

```bash
chmod +x ./remove.sh
./remove.sh
```

What `remove.sh` handles automatically:

1. Stops and disables legacy services (`klippervault.service`, `klipper-vault.service`).
2. Removes legacy service unit files from `/etc/systemd/system`.
3. Removes old app directories and virtual environments.
4. Removes legacy on-printer config/database artifacts.
5. Removes old launcher shortcuts in the installer user's home directory.
6. Prints a verification summary of any remaining paths.

Optional script modes:

```bash
# Preview actions without applying changes
./remove.sh --dry-run

# Keep old launcher shortcuts, remove everything else
./remove.sh --keep-shortcuts
```

After cleanup, use this repository from a remote PC/server only, then follow the normal installation and remote profile setup flow.

## Overview

KlipperVault runs remotely on a PC/server, syncs Klipper cfg files over SSH/SFTP, indexes every `[gcode_macro ...]` section in SQLite, and presents the results in a NiceGUI interface.

All printer interaction is remote-only via SSH/SFTP for config files and Moonraker HTTP API for printer state/actions.

## What's New

- Startup online update check now runs automatically when `online_update_repo_url` is configured.
- When updates are found during startup checks, KlipperVault posts a Mainsail notification through Moonraker.
- Developer publishing actions are available in a dedicated top-level `Developer` toolbar menu:
  - `Export Update Zip`
  - `Create Pull Request`

## Key Features

- Automatic macro version history (changes are stored only when content differs).
- Active/inactive and loaded/not-loaded state tracking across include chains.
- Dynamic macro awareness for configs loaded via `[dynamicmacros]` `configs:` entries.
- Dynamic macro status badge (`Dynamic`) and dedicated `Reload Dynamic Macros` action.
- Duplicate macro detection with guided conflict resolution.
- Loading-order overview showing Klipper file parse order and macro-level inline include order.
- In-place macro editing with write-back to cfg files.
- Backup and restore of both indexed rows and cfg snapshots.
- Macro sharing workflow:
  - Export one or multiple macros into a portable share JSON file.
  - Attach source printer vendor/model metadata.
  - Import via file upload as inactive `NEW` entries for review first.
  - Imported macros default to `macros.cfg`; include is ensured in `printer.cfg`.
- Online macro updates from GitHub repositories:
  - Check for updates from optional GitHub-hosted update repository.
  - Run an automatic startup update check when a repository is configured.
  - Import updates as new inactive versions for selective activation.
  - **Developer mode**: Create pull requests to publish macros to repositories, export local macros as repository bundles.
    - See [**Macro Developer Guide**](Macro_Developer.md) for setup instructions.
- Moonraker print-state safety gates for mutating actions.
- Optional script explanation panel with macro-to-macro cross-links.

Dynamic Macros project:
- https://github.com/3DCoded/DynamicMacros

## Requirements

- Linux
- Python 3 with `venv` support
- SSH access to target host config directory
- Moonraker URL for target host/profile

Primary dependency profile:

- GUI + remote workflows: [requirements.txt](requirements.txt)

Off-printer credential storage:

- SSH profile metadata is stored in the KlipperVault SQLite database.
- SSH secrets use OS key storage (keyring) when available.
- If no usable keyring backend is present, secrets fall back to encrypted-at-rest SQLite storage.

## Default Paths

- Runtime mode: `off_printer`
- Config directory: `~/.config/klippervault`
- Database: `~/.local/share/klippervault/klipper_macros.db`
- Default HTTP port: `10090`
- Moonraker URL comes from the active SSH profile.

## Configuration

KlipperVault stores application settings in the SQLite database and exposes them in-app:

1. Open `Macro actions`.
2. Click `Settings`.
3. Save changes from the dialog.

- `version_history_size`: max stored versions per macro
- `port`: web UI port
- `runtime_mode`: fixed to `off_printer`
- `ui_language`: UI language (`en`, `de`, `fr`)
- `online_update_repo_url`: optional GitHub URL for macro update repository
- `online_update_manifest_path`: path to manifest file inside the update repository (default: `updates/manifest.json`)
- `online_update_ref`: branch, tag, or commit SHA for update checks (default: `main`)
- `developer`: enable developer features (default: `false`) — see [Macro Developer Guide](Macro_Developer.md)
Environment overrides:

- `KLIPPERVAULT_RUNTIME_MODE`
- `KLIPPERVAULT_CONFIG_DIR`
- `KLIPPERVAULT_DB_PATH`

Port, UI language, and developer mode changes require app restart to take full effect.

## Installation

From repository root:

```bash
sudo ./install.sh
```

Installer summary (GUI/off-printer default):

1. Detect target user
2. Create runtime directories under `~/.config/klippervault` and `~/.local/share/klippervault`
3. Create virtualenv (`~/klippervault-venv` by default)
4. Install dependencies from `requirements.txt`
5. Initialize runtime defaults (stored in SQLite on first app start)

Uninstall:

```bash
./uninstall.sh
./uninstall.sh --remove-venv --remove-config --remove-db
```

## Running

Manual run:

```bash
./.venv/bin/python klipper_vault.py
```

Off-printer mode run (explicit):

```bash
KLIPPERVAULT_RUNTIME_MODE=off_printer \
KLIPPERVAULT_CONFIG_DIR=$HOME/.config/klippervault \
KLIPPERVAULT_DB_PATH=$HOME/.local/share/klippervault/klipper_macros.db \
./.venv/bin/python klipper_vault_gui.py
```

## Usage

Typical off-printer flow:

1. Open KlipperVault.
2. In `off_printer` mode, open `Manage SSH profiles`, save one profile, and activate it.
3. Click `Test SSH profile`.
4. Click `Scan macros`.
5. Select a macro and review details/history.
6. Edit latest non-deleted version.
7. For dynamic macros, editing is allowed even while printing.
8. Use `Reload Dynamic Macros` to apply dynamic-macro changes without a full Klipper restart.
9. Save and re-index.

Loading-order inspection:

1. Open `Macro actions`.
2. Click `Loading order overview`.
3. Review file and macro parse order to confirm include sequencing and macro override precedence.

Share/import flow:

1. Click `Export macros`.
2. Select one or more macros.
3. Confirm export to trigger direct download of a share JSON file.
4. On another system, click `Import macros` and upload that JSON file.
5. Review imported `NEW` inactive entries, then enable individually.

Online updates flow:

1. Configure `online_update_repo_url` and optional `online_update_manifest_path`, `online_update_ref` in `Macro actions -> Settings`.
2. Click `Check for updates` to fetch the manifest and compare local macros against remote versions.
3. Review available updates in the dialog; select which ones to activate.
4. Click `Import updates` to add new versions; activate selectively or defer.
5. Updated macros appear as `NEW` inactive versions for review before enabling.

Developer mode (publish and export update artifacts):

1. Enable `Developer mode` in `Macro actions -> Settings`.
2. Use the top-level `Developer` toolbar menu.
3. Click `Export Update Zip` to download an update ZIP for review or manual distribution.
4. Click `Create Pull Request` to publish active macros directly to the configured GitHub repository.
5. See [**Macro Developer Guide**](Macro_Developer.md) for repository setup, token creation, and publishing details.

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

- Check `Macro actions -> Settings` web UI port value.
- Confirm virtualenv and dependencies were installed.
- Check service logs with `journalctl`.

No macros found:

- In `off_printer` mode, verify an active printer connection exists, credentials are set, and `Test printer connection` succeeds.
- Check `printer.cfg` includes and file readability.
- Trigger a manual scan.

Editing is disabled:

- Confirm printer is not actively printing.
- Verify Moonraker connectivity.
- Ensure selected row is latest and non-deleted.

## Developer Docs

Developer setup, architecture, checks, and contribution guidance are in [development.md](development.md).

## Honorable people that helped me to build this:
@[triadterm](https://github.com/triadterm) - Thanks for being an early adopter and testing this

## License

KlipperVault is licensed under GPL-3.0-or-later. See [LICENSE](LICENSE).
