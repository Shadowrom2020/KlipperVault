# KlipperVault

![KlipperVault logo](assets/logo.svg)

KlipperVault is a lightweight web UI for managing Klipper `gcode_macro` definitions with version history, safe editing workflows, backup/restore, duplicate handling, and Mainsail integration.

**[📸 View UI Overview with Screenshots](Docs/overview.md)**

## Getting Started

**[→ Download the standalone executable for your platform →](https://github.com/Shadowrom2020/KlipperVault/releases)**

- **Windows**: Download `.exe` installer, run it, done
- **macOS**: Build locally on macOS by following [Docs/MacOS.md](Docs/MacOS.md)
- **Linux**: Download `.AppImage`, make executable, run

No Python installation or virtualenv needed. Works across Windows 10+, macOS, and Linux.

**[Full installation guide](Docs/INSTALLATION_GUIDE.md)** — for troubleshooting, upgrading, and source installs

## Overview

KlipperVault runs remotely on a PC/server, syncs Klipper cfg files over SSH/SFTP, indexes every `[gcode_macro ...]` section in SQLite, and presents the results in a NiceGUI interface.

All printer interaction is remote-only via SSH/SFTP for config files and Moonraker HTTP API for printer state/actions.

## What's New

- **Safer edit and sync workflow**
  - Local edits are staged first; upload happens explicitly with `Save Config`.
  - While printing, check/view/import flows remain available, but mutating remote sync actions are blocked.
  - Auto-restart after upload is removed; restart remains a deliberate/manual action.

- **Improved printer/profile UX**
  - Active printer selection is in the top bar for faster context switching.
  - Printer connection actions are grouped under a dedicated `Printers` menu.
  - Backup action is now in `Macro actions` for a cleaner toolbar.

- **Online update quality-of-life**
  - Startup update check runs automatically when `online_update_repo_url` is configured.
  - If updates are found, KlipperVault can post a Mainsail notification through Moonraker.
  - Imported updates are attached as new versions within existing macro identity chains.

- **Developer publishing menu**
  - Publishing tools are grouped in a top-level `Developer` menu:
    - `Export Update Zip`
    - `Create Pull Request`

## Key Features

- **Versioned macro history**
  - Automatic history snapshots are stored only when macro content actually changes.
  - Active/inactive and loaded/not-loaded states are tracked across include chains.

- **Remote-first editing workflow**
  - Edit macros in-place with local write-back and immediate re-indexing.
  - Changes are staged locally first and uploaded explicitly with `Save Config`.
  - Restart/reload actions are surfaced intentionally (no automatic restart on upload).

- **Backup and recovery**
  - Create named backups of indexed macro rows and cfg snapshots.
  - Restore from backups with clear status feedback and post-restore re-indexing.

- **Macro sharing and review**
  - Export one or multiple macros into a portable share JSON file.
  - Include source printer vendor/model metadata for compatibility checks.
  - Import as inactive `NEW` entries so changes can be reviewed before activation.

- **Online update pipeline**
  - Compare local macros against an optional GitHub-hosted update repository.
  - Run automatic startup checks when `online_update_repo_url` is configured.
  - Import only changed macros as new versions and activate selectively.
  - Post startup update notifications to Mainsail through Moonraker.

- **Dynamic macro support**
  - Detect macros provided via `[dynamicmacros]` `configs:` entries.
  - Show dynamic status and provide a dedicated `Reload Dynamic Macros` action.

- **Conflict and visibility tooling**
  - Guided duplicate detection/resolution workflows.
  - Loading-order overview at file and macro parse levels.
  - Optional script explanation panel with macro-to-macro cross-links.

- **Developer publishing tools**
  - Top-level `Developer` menu with:
    - `Export Update Zip`
    - `Create Pull Request`
  - See [**Macro Developer Guide**](Docs/Macro_Developer.md) for setup instructions.

Dynamic Macros project:
- https://github.com/3DCoded/DynamicMacros

## Requirements

- Linux, macOS, or Windows 10+
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
- Config directory:
  - Linux: `~/.config/klippervault`
  - macOS: `~/Library/Application Support/KlipperVault`
  - Windows: `%APPDATA%\\KlipperVault`
- Database:
  - Linux: `~/.local/share/klippervault/klipper_macros.db`
  - macOS: `~/Library/Application Support/KlipperVault/klipper_macros.db`
  - Windows: `%LOCALAPPDATA%\\KlipperVault\\klipper_macros.db`
- Default HTTP port: `10090`
- Moonraker URL comes from the active SSH profile.

## Configuration

KlipperVault stores application settings in the SQLite database and exposes them in-app:

1. Click the top-right `Settings` (gear) button in the toolbar.
2. Update settings in the dialog.
3. Save changes.

- `version_history_size`: max stored versions per macro
- `port`: web UI port
- `runtime_mode`: fixed to `off_printer`
- `ui_language`: UI language (`en`, `de`, `fr`)
- `online_update_repo_url`: optional GitHub URL for macro update repository
- `online_update_manifest_path`: path to manifest file inside the update repository (default: `updates/manifest.json`)
- `online_update_ref`: branch, tag, or commit SHA for update checks (default: `main`)
- `developer`: enable developer features (default: `false`) — see [Macro Developer Guide](Docs/Macro_Developer.md)

Port, UI language, and developer mode changes require app restart to take full effect.

## Installation

**Quick Start: Use the [standalone executable or native installer](Docs/INSTALLATION_GUIDE.md)** — no Python or virtualenv needed.

For developers or source-based deployments:
- [Linux source installation](Docs/Linux.md)
- [macOS source installation](Docs/MacOS.md)
- [Windows source installation](Docs/Windows.md)

## Standalone Executable Builds

KlipperVault can also be packaged as a standalone executable for Windows, Linux, and macOS with PyInstaller.

Important constraints:

- Build on the target OS. PyInstaller does not cross-compile Windows, Linux, and macOS binaries from one host.
- The packaged build keeps the existing config and database locations for each platform.
- Packaged builds open the browser automatically on launch.

Build prerequisites:

```bash
python3 -m pip install -r requirements.txt -r requirements-build.txt
```

Build commands:

```bash
# Linux/macOS
make bundle

# Windows
py -3 scripts\\build_executable.py
```

This generates a packaged app from [klippervault.spec](klippervault.spec). CI/release automation can reuse the same spec on each platform.

Platform-native distribution artifacts:

- **Windows**: Inno Setup installer (.exe) — requires [Inno Setup 6](https://jrsoftware.org/isdl.php) to build locally
- **macOS**: app bundle and local release artifact — build locally on macOS (see [Docs/MacOS.md](Docs/MacOS.md))
- **Linux**: AppImage (.AppImage) — requires [appimagetool](https://github.com/AppImage/AppImageKit) to build locally

When tools are not available, the build produces a ZIP archive fallback containing the executable.

GitHub Actions automation:

- [build-executables.yml](.github/workflows/build-executables.yml) builds versioned Windows and Linux artifacts.
  - Installers (AppImage, Inno Setup) are built on each respective OS if tools are available.
  - Both platforms produce ZIP archives as a fallback.
- macOS binaries/installers are built manually on macOS hosts (not in GitHub Actions).
- Tagged pushes matching `v*` publish workflow-built artifacts to the GitHub release for that tag.

## Docker Deployment

See [docker.md](Docs/docker.md) for Docker build, run, persistence, networking, and upgrade instructions.

## Usage

Typical off-printer flow:

1. Open KlipperVault.
2. Open `Printers` and choose `Manage printer connections`, then save and activate a profile.
3. Open `Printers` and choose `Test printer connection`.
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

1. Configure `online_update_repo_url` and optional `online_update_manifest_path`, `online_update_ref` in `Settings` (gear button).
2. Click `Check for updates` to fetch the manifest and compare local macros against remote versions.
3. Review available updates in the dialog; select which ones to activate.
4. Click `Import updates` to add new versions; activate selectively or defer.
5. Updated macros appear as `NEW` inactive versions for review before enabling.

Developer mode (publish and export update artifacts):

1. Enable `Developer mode` in `Settings` (gear button).
2. Use the top-level `Developer` toolbar menu.
3. Click `Export Update Zip` to download an update ZIP for review or manual distribution.
4. Click `Create Pull Request` to publish active macros directly to the configured GitHub repository.
5. See [**Macro Developer Guide**](Docs/Macro_Developer.md) for repository setup, token creation, and publishing details.

Compatibility behavior:

- Share files carry source printer vendor/model.
- Import warns when source printer metadata is unknown or differs from local printer metadata.
- Online updates use checksum comparison to detect changes; only changed macros appear in the update list.

## Safety Model

When Moonraker reports `printing`, KlipperVault keeps the UI responsive for review tasks but blocks selected risk-prone actions such as `Save Config`, backup creation, and purge operations.

Local-first workflows remain available during printing (for example check/update dialogs and local import/review), while printer-impacting upload actions stay gated.

Exception for dynamic macros:
- Dynamic macros remain editable while printing.
- `Reload Dynamic Macros` remains available while printing and triggers Klipper command `DYNAMIC_MACRO` via Moonraker.

## Troubleshooting

App does not start:

- Check the `Settings` (gear) web UI port value.
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

Developer setup, architecture, checks, and contribution guidance are in [Docs/development.md](Docs/development.md).

## Honorable people that helped me to build this:
@[Agent-047185](https://github.com/Agent-047185) - Thanks for being an early adopter and testing all of my stuff!

@[triadterm](https://github.com/triadterm) - Thanks for being an early adopter and testing this

## License

KlipperVault is licensed under GPL-3.0-or-later. See [LICENSE](LICENSE).
