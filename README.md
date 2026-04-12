# KlipperVault

![KlipperVault logo](assets/logo.svg)

KlipperVault is a lightweight web UI for managing Klipper `gcode_macro` definitions with version history, safe editing workflows, backup/restore, duplicate handling, and Mainsail integration.

**[📸 View UI Overview with Screenshots](overview.md)**

## Overview

KlipperVault scans your Klipper config tree, indexes every `[gcode_macro ...]` section in SQLite, and presents the results in a NiceGUI interface.

It supports both local printer-host operation and off-printer operation via SSH/SFTP profile connections, with safer macro maintenance than editing raw `.cfg` files alone.

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
- systemd
- For `on_printer` mode: Klipper config directory (typically `~/printer_data/config`)
- For `off_printer` mode: SSH access to target host config directory and Moonraker URL for that host/profile

Dependencies are split by runtime profile:

- Printer host profile: [requirements-printer.txt](requirements-printer.txt)
- Full GUI profile: [requirements.txt](requirements.txt)

Off-printer credential storage:

- SSH profile metadata is stored in the KlipperVault SQLite database.
- SSH secrets use OS key storage (keyring) when available.
- If no usable keyring backend is present, secrets fall back to encrypted-at-rest SQLite storage.

## Default Paths

- Runtime mode default: `off_printer`
- Off-printer defaults:
  - Config directory: `~/.config/klippervault`
  - Database: `~/.local/share/klippervault/klipper_macros.db`
- On-printer defaults:
  - Config directory: `~/printer_data/config`
  - Database: `~/printer_data/db/klipper_macros.db`
- App config path default (installer): `~/printer_data/config/klippervault.cfg`
- Default HTTP port: `10090`
- Default Moonraker URL: `http://127.0.0.1:7125`

## Configuration

KlipperVault creates `klippervault.cfg` on first start if it does not exist.

```ini
[vault]
version_history_size: 5
port: 10090
runtime_mode: off_printer
ui_language: en
printer_vendor:
printer_model:
online_update_repo_url:
online_update_manifest_path: updates/manifest.json
online_update_ref: main
developer: false
enable_remote_api: false
api_bind_host: 127.0.0.1
api_port: 10091
api_token:
remote_api_url:
remote_api_token:
```

- `version_history_size`: max stored versions per macro
- `port`: web UI port
- `runtime_mode`: runtime behavior (`auto`, `on_printer`, `off_printer`)
- `ui_language`: UI language (`en`, `de`)
- `printer_vendor`: optional printer vendor shown in UI and exported share metadata
- `printer_model`: optional printer model shown in UI and exported share metadata
- `online_update_repo_url`: optional GitHub URL for macro update repository
- `online_update_manifest_path`: path to manifest file inside the update repository (default: `updates/manifest.json`)
- `online_update_ref`: branch, tag, or commit SHA for update checks (default: `main`)
- `developer`: enable developer features (default: `false`) — see [Macro Developer Guide](Macro_Developer.md)
- `enable_remote_api`: allow host API service for remote GUI clients (default: `false`)
- `api_bind_host`: host API bind address (default: `127.0.0.1`)
- `api_port`: host API port (default: `10091`)
- `api_token`: bearer token required by host API when `api_bind_host` is not localhost
- `remote_api_url`: optional remote API URL used by GUI mode (for example: `http://printer-host.local:10091`)
- `remote_api_token`: optional bearer token used by GUI mode for remote API auth

Environment overrides:

- `KLIPPERVAULT_RUNTIME_MODE`
- `KLIPPERVAULT_CONFIG_DIR`
- `KLIPPERVAULT_DB_PATH`

If vendor/model are missing, KlipperVault prompts for them and writes them back to config.

## Installation

From repository root:

```bash
sudo ./install.sh
```

Installer summary (GUI/off-printer default):

1. Detect target user
2. Migrate legacy installs (`klippervault-venv`, old GUI service/nav entries) when found
3. Create virtualenv (`~/klippervault-venv` by default)
4. Install dependencies from `requirements.txt` (GUI/off-printer profile)
5. Write and enable `klipper-vault.service` (default)
6. Host API service is optional and disabled by default (`INSTALL_HOST_API_SERVICE=0`)

Legacy printer-host focused install can still be enabled explicitly by setting:

- `INSTALL_HOST_API_SERVICE=1`
- `KLIPPERVAULT_RUNTIME_MODE=on_printer` (or `auto`)

Uninstall:

```bash
sudo ./uninstall.sh
sudo ./uninstall.sh --remove-venv
```

## Running

Manual run:

```bash
./.venv/bin/python klipper_vault_gui.py
```

Off-printer mode run (explicit):

```bash
KLIPPERVAULT_RUNTIME_MODE=off_printer \
KLIPPERVAULT_CONFIG_DIR=$HOME/.config/klippervault \
KLIPPERVAULT_DB_PATH=$HOME/.local/share/klippervault/klipper_macros.db \
./.venv/bin/python klipper_vault_gui.py
```

Host API service mode (printer host):

```bash
./.venv/bin/python klipper_vault.py
```

Legacy remote GUI mode (client machine, host API compatibility):

```bash
KLIPPERVAULT_REMOTE_API_URL=http://printer-host.local:10091 \
KLIPPERVAULT_REMOTE_API_TOKEN=<token-if-configured> \
./.venv/bin/python klipper_vault_gui.py
```

Service management:

```bash
sudo systemctl restart klipper-vault.service
sudo systemctl restart klipper-vault-host-api.service
sudo systemctl status klipper-vault.service
sudo systemctl status klipper-vault-host-api.service
sudo journalctl -u klipper-vault.service -f
sudo journalctl -u klipper-vault-host-api.service -f
```

Install without host API service unit:

```bash
sudo INSTALL_HOST_API_SERVICE=0 ./install.sh
```

Install with host API service unit (optional compatibility mode):

```bash
sudo INSTALL_HOST_API_SERVICE=1 KLIPPERVAULT_RUNTIME_MODE=on_printer ./install.sh
```

## Usage

Typical flow:

1. Open KlipperVault.
2. In `off_printer` mode, open `Manage SSH profiles`, save one profile, and activate it.
3. Click `Test SSH profile`.
4. Click `Scan macros`.
5. Select a macro and review details/history.
6. Edit latest non-deleted version.
7. For dynamic macros, editing is allowed even while printing.
8. Use `Reload Dynamic Macros` to apply dynamic-macro changes without a full Klipper restart.
9. Save and re-index.

On-printer/auto flow:

1. Open KlipperVault.
2. Click `Scan macros`.
3. Select a macro and review details/history.
4. Edit latest non-deleted version.
5. For dynamic macros, editing is allowed even while printing.
6. Use `Reload Dynamic Macros` to apply dynamic-macro changes without a full Klipper restart.
7. Save and re-index.

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

1. Configure `online_update_repo_url` and optional `online_update_manifest_path`, `online_update_ref` in `klippervault.cfg`.
2. Click `Check for updates` to fetch the manifest and compare local macros against remote versions.
3. Review available updates in the dialog; select which ones to activate.
4. Click `Import updates` to add new versions; activate selectively or defer.
5. Updated macros appear as `NEW` inactive versions for review before enabling.

Developer mode (publish and export update artifacts):

1. Set `developer: true` in `klippervault.cfg`.
2. Use the top-level `Developer` toolbar menu.
3. Click `Export Update Zip` to download an update ZIP for review or manual distribution.
4. Click `Create Pull Request` to publish active macros directly to the configured GitHub repository.
5. See [**Macro Developer Guide**](Macro_Developer.md) for repository setup, token creation, and publishing details.

Compatibility behavior:

- Share files carry source printer vendor/model.
- Import warns when source printer metadata is unknown or differs from local printer metadata.
- Online updates use checksum comparison to detect changes; only changed macros appear in the update list.

Remote host API typed endpoints (legacy compatibility mode):

- `GET /api/v1/health`
- `GET /api/v1/dashboard`
- `GET /api/v1/macros/versions`
- `GET /api/v1/backups`
- `GET /api/v1/backups/{backup_id}/items`
- `GET /api/v1/printer/status`
- `GET /api/v1/duplicates`
- `GET /api/v1/cfg-loading-overview`
- `GET /api/v1/jobs/{job_id}`
- `GET /api/v1/events` (SSE stream)
- `POST /api/v1/index`
- `POST /api/v1/jobs/online-check`
- `POST /api/v1/jobs/create-pr`
- `POST /api/v1/actions/{action}`
- `POST /api/v1/share/export`
- `POST /api/v1/share/import`
- `POST /api/v1/online-update/export-zip`

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

- In `off_printer` mode, verify an active SSH profile exists, credentials are set, and `Test SSH profile` succeeds.
- In `on_printer`/`auto` mode, verify config files exist under `~/printer_data/config`.
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
