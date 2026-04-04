# KlipperVault

![KlipperVault logo](assets/logo.svg)

KlipperVault is a lightweight web UI for managing Klipper `gcode_macro` definitions with version history, backup and restore workflows, duplicate detection, and Mainsail integration.

It is designed for Klipper systems that keep their printer configuration under `~/printer_data/config` and want a safer workflow around macro maintenance than editing raw `.cfg` files alone.

## Overview

KlipperVault scans your Klipper config tree, indexes every `[gcode_macro ...]` section a database, and presents the results in a web interface.

The application focuses on a few operational goals:

1. Preserve macro history automatically whenever a macro changes.
2. Show the effective state of macros across multiple included `.cfg` files.
3. Help resolve duplicate macro definitions safely.
4. Allow backup and restore of macro state.
5. Integrate cleanly into a Mainsail-based Klipper setup.
6. Prevent risky edits while the printer is actively printing.

## Key Features

### Macro indexing and version history

- Recursively scans `.cfg` files under the Klipper config directory.
- Follows `printer.cfg` include order so active and overridden macros are identified correctly.
- Stores macro versions in SQLite only when content actually changes.
- Tracks deleted macros, inactive overrides, and the latest indexed state.

### Macro viewer and comparison

- Browse indexed macros by name and file.
- Filter by active or inactive state.
- Highlight duplicate macro names.
- Compare historical versions of the same macro.
- Jump from an inactive macro to the active overriding definition.

### In-place macro editing

- Edit the latest non-deleted version of a macro directly from the web UI.
- Save writes the edited section back to the correct `.cfg` file.
- A new version is created automatically on the next index pass when the content actually changed.
- The editor uses a syntax-highlighted code editor when the NiceGUI runtime supports CodeMirror.

### Script explanation and macro cross-links

- Experimental: The macro explainer is an early development feature and may be inaccurate.
- Explains common g-code and Klipper commands in plain language directly in the macro viewer.
- Recognizes macro-to-macro calls and lists referenced macros in an explanation panel.
- Opens referenced macros from a popup so users can follow the script flow across multiple macros.

### Backup and restore

- Create named backups of the current macro state.
- Backup includes both indexed macro rows and `.cfg` file snapshots.
- View backup contents before restoring.
- Restore backup state back to config files and database.

### Duplicate resolution

- Detects duplicate macro names across files.
- Provides a guided workflow to choose which definition to keep.
- Creates a pre-resolution backup before modifying files.

### Moonraker print-state safety

- Queries Moonraker to determine whether the printer is currently printing.
- Disables macro editing and other mutating actions while printing.
- Pauses the config file watcher during active prints.
- Shows a warning dialog in the UI when edits are blocked by printer state.

### Mainsail integration

- The installer adds KlipperVault to Mainsail's left navigation using `.theme/navi.json`.
- The sidebar link points to the configured KlipperVault web UI port.

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
  klipper_macro_watcher.py        Config file watcher
  klipper_vault_config.py         klippervault.cfg handling
```

## Requirements

### Runtime

- Linux
- Python 3 with `venv` support
- systemd
- A Klipper config directory, typically `~/printer_data/config`
- Moonraker reachable at `http://127.0.0.1:7125` by default

### Python dependencies

Current Python dependencies are defined in `requirements.txt`.

At the time of writing, the project depends on:

- `nicegui>=2.0.0`

## Default Paths

KlipperVault assumes the following defaults unless you override them:

- Config directory: `~/printer_data/config`
- Database: `~/printer_data/db/klipper_macros.db`
- App config: `~/printer_data/config/klippervault.cfg`
- Default HTTP port: `10090`
- Default Moonraker URL: `http://127.0.0.1:7125`

## Configuration

KlipperVault creates `klippervault.cfg` on first start if it does not already exist.

Default config:

```ini
[vault]
version_history_size: 5
port: 10090
```

### Settings

- `version_history_size`: Maximum stored versions per macro.
- `port`: Port used by the KlipperVault web UI.

## Installation

### Recommended install

From the repository root:

```bash
sudo ./install.sh
```

The installer will:

1. Detect the target user.
2. Create a Python virtual environment.
3. Install Python dependencies.
4. Write and enable `klipper-vault.service`.
5. Add a KlipperVault entry to Mainsail's left navigation.

### Installer behavior

The install script uses these important defaults:

- Virtual environment: `$HOME/klippervault-venv`
- Service name: `klipper-vault.service`
- Mainsail nav file: `~/printer_data/config/.theme/navi.json`

### Supported installer overrides

You can override parts of the install process via environment variables:

- `PYTHON_BIN`
- `VENV_DIR`
- `KLIPPER_CONFIG_DIR`
- `VAULT_CFG_PATH`
- `MAINSAIL_THEME_DIR`
- `MAINSAIL_NAV_FILE`
- `MAINSAIL_NAV_TITLE`
- `MAINSAIL_NAV_TARGET`
- `MAINSAIL_NAV_POSITION`
- `MAINSAIL_NAV_HREF`

Example:

```bash
sudo MAINSAIL_NAV_HREF=http://printer.local:10090 ./install.sh
```

## Running Manually

If you want to launch the app without the installer:

```bash
python3 klipper_vault.py
```

The app reads the configured UI port from `klippervault.cfg` and starts the NiceGUI server on `127.0.0.1`.

## Service Management

### Restart the service

```bash
sudo systemctl restart klipper-vault.service
```

### Check service status

```bash
sudo systemctl status klipper-vault.service
```

### Follow logs

```bash
sudo journalctl -u klipper-vault.service -f
```

## Uninstall

Remove the service but keep the virtual environment:

```bash
sudo ./uninstall.sh
```

Remove the service and the virtual environment:

```bash
sudo ./uninstall.sh --remove-venv
```

## Mainsail Integration

After installation, KlipperVault is added to Mainsail's sidebar via a `navi.json` entry inside Mainsail's `.theme` directory.

By default the generated navigation link:

- Uses title `KlipperVault`
- Opens in a new tab
- Uses sidebar position `85`
- Points at the configured KlipperVault UI port

If the entry already exists, the installer updates it instead of adding a duplicate.

## Usage

### Typical workflow

1. Open KlipperVault from Mainsail or via its direct URL.
2. Click `Scan macros` to refresh the database from the current config tree.
3. Select a macro from the left panel.
4. Review its current definition, historical versions, and status.
5. Edit the latest version if needed.
6. Save and let KlipperVault re-index the config to record the new version.

### Editing rules

KlipperVault allows editing only when:

1. The printer is not actively printing.
2. The selected macro is not deleted.
3. The selected row is the latest stored version for that macro identity.

### Duplicate handling

When duplicate macro names are detected, the top toolbar exposes a duplicate warning action. The duplicate workflow lets you inspect entries, compare keep targets, and apply a chosen resolution.

### Backups

Use the `Backup` action from the top toolbar to store a named snapshot before risky edits or cleanup operations.

## Moonraker Integration

KlipperVault queries Moonraker using the object query endpoint for `print_stats`.

Default endpoint:

```text
http://127.0.0.1:7125/printer/objects/query?print_stats=state,message
```

To override the Moonraker base URL, set:

```bash
export MOONRAKER_BASE_URL=http://your-host:7125
```

Then start KlipperVault normally.

## Safety Model

KlipperVault is intentionally conservative around live printer activity.

While Moonraker reports `printing`:

- File watcher polling is paused.
- Macro editing is disabled.
- Backup, restore, duplicate resolution, and other mutating actions are blocked.
- The UI warns the user that the printer is actively printing.

## Development

### Environment setup

The quickest way to get a local environment ready is:

```bash
bash scripts/setup_dev.sh
```

This will create `.venv/` at the repository root, upgrade pip, and install all dependencies from `requirements.txt`. It is safe to re-run; it skips virtualenv creation if `.venv/` already exists.

To activate the environment manually in subsequent shells:

```bash
source .venv/bin/activate
```

Alternatively, set the environment up by hand:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Run locally

```bash
python3 klipper_vault.py
```

The app will read `~/printer_data/config/klippervault.cfg` (created on first run) and start NiceGUI on `127.0.0.1` at the configured port (default `10090`).

For a custom config location, point `KLIPPER_CONFIG_DIR` at a test directory:

```bash
KLIPPER_CONFIG_DIR=/tmp/testcfg python3 klipper_vault.py
```

### Module guide

| Module | Responsibility |
|---|---|
| `klipper_vault.py` | Entry point — reads config, calls `build_ui()`, starts NiceGUI server |
| `src/klipper_vault_config.py` | Reads and auto-creates `klippervault.cfg`; exposes typed defaults |
| `src/klipper_macro_indexer.py` | `.cfg` parser, SQLite schema, version tracking, macro save/rewrite |
| `src/klipper_macro_backup.py` | Backup creation, backup listing, restore to cfg and DB |
| `src/klipper_macro_gui_service.py` | Service layer — mediates between UI callbacks and backend modules |
| `src/klipper_macro_gui.py` | NiceGUI page, full UI wiring, print-state lock, callback registration |
| `src/klipper_macro_viewer.py` | Macro detail panel and inline editor with CodeMirror |
| `src/klipper_macro_compare.py` | Version comparison dialog UI |
| `src/klipper_macro_compare_core.py` | Diff logic used by the compare dialog |
| `src/klipper_macro_watcher.py` | Polls config directory for changes and triggers re-index |

### Compile checks

After editing Python files, verify syntax with `py_compile` before committing:

```bash
python3 -m py_compile src/klipper_macro_gui.py
python3 -m py_compile src/klipper_macro_indexer.py src/klipper_macro_backup.py
python3 -m py_compile src/klipper_macro_gui_service.py src/klipper_macro_viewer.py
```

A zero-exit compile check means no syntax errors. There is no automated test suite; the primary verification method is a compile check followed by a manual smoke test of the changed flow.

### Code conventions

- Keep edits minimal and focused. Do not refactor code outside the scope of a change.
- Preserve existing NiceGUI patterns (`ui.card`, `ui.row`, `q-` class names, `.bind_*` helpers).
- SQLite schema additions go in `ensure_schema()` or `ensure_backup_schema()` and must be backward-safe (`IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`).
- The config parser in `klipper_macro_indexer.py` is intentionally dependency-free. Do not add third-party parsing libraries.
- UI port and Moonraker base URL are runtime values read from config or environment. Never hardcode them.

### Adding a new UI action

1. Add the backend logic to `klipper_macro_indexer.py` or `klipper_macro_backup.py`.
2. Expose it via a method on `MacroGuiService` in `klipper_macro_gui_service.py`.
3. Wire the callback in `klipper_macro_gui.py` and guard on `printer_is_printing` if the action is mutating.
4. Run compile checks on all touched files.

## Troubleshooting

### The app does not start

- Check `klippervault.cfg` for an invalid port.
- Confirm the virtual environment was created successfully.
- Review systemd logs with `journalctl`.

### The Mainsail navigation item does not appear

- Confirm the install script ran successfully.
- Check `~/printer_data/config/.theme/navi.json`.
- Reload Mainsail with a hard refresh.

### Editing is disabled

- Confirm the printer is not actively printing.
- Check Moonraker connectivity.
- Make sure you are viewing the latest non-deleted version of the macro.

### No macros are found

- Confirm your config tree exists under `~/printer_data/config`.
- Verify `printer.cfg` and included `.cfg` files are readable.
- Run a manual scan from the UI.

## License

KlipperVault is licensed under the GPL-3.0-or-later license. See `LICENSE` for details.