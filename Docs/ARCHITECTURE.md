# KlipperVault Architecture

## Overview

KlipperVault is a lightweight Klipper macro vault with the following core responsibilities:
- Fast `.cfg` parsing and macro indexing into SQLite
- Versioned macro history with backup/restore capabilities
- NiceGUI-based browsing, comparison, and management interface
- Moonraker API integration for printer status and macro execution
- SSH-based remote printer connection support

Last verified against repository state: 2026-04-22.

## High-Level Architecture

```
┌─────────────────────────────────────────────────────┐
│                   klipper_vault_gui.py              │
│              (Primary GUI launcher & entry)         │
└──────────────────┬──────────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        │                     │
┌───────▼──────────────────┐  │
│   klipper_macro_gui.py   │  │
│  (NiceGUI frontend)      │  │
│  ~3900 lines             │  │
│  - UI layout & theming   │  │
│  - Event handlers        │  │
│  - State management      │  │
└───────┬──────────────────┘  │
        │                     │
        │            ┌────────▼─────────────────────┐
        │            │ klipper_vault_config.py      │
        │            │ (Settings & DB initialization)
        │            │ Manages vault_cfg, version   │
        │            │ history size, theme, language│
        │            └──────────────────────────────┘
        │
        │
┌───────▼─────────────────────────────────────────────────────────────┐
│   klipper_macro_gui_service.py  +  three mixin modules              │
│   MacroGuiService(PrinterProfileMixin, BackupRestoreMixin,          │
│                   OnlineUpdateMixin)                                │
│   ~354 lines core + ~1815 lines in mixins                           │
│   - Core: __init__, indexing, macro ops, shared helpers             │
│   - PrinterProfileMixin: profiles, SSH, Moonraker                   │
│   - BackupRestoreMixin:  backup/restore dispatch                    │
│   - OnlineUpdateMixin:   share, online updates, PR creation         │
└───────┬─────────────────────────────────────────────────────────────┘
        │
        ├─────────────────────────────────┬─────────────────┬──────────────┐
        │                                 │                 │              │
┌───────▼──────────────┐  ┌───────────────▼──┐  ┌───────────▼───┐  ┌───────▼───┐
│ klipper_macro_       │  │ klipper_macro_   │  │ klipper_macro_│  │ klipper_  │
│ indexer.py           │  │ backup.py        │  │ online*.py    │  │ macro_    │
│                      │  │                  │  │               │  │ explainer │
│ Parsing & indexing   │  │ Backup/restore   │  │ Updates & PR  │  │ .py       │
│ - Parse cfg files    │  │ - create_backup()│  │ operations    │  │           │
│ - Extract macros     │  │ - load_backup()  │  │ - check_*()   │  │ Explains  │
│ - DB insertion       │  │ - restore_*()    │  │ - import_*()  │  │ gcode     │
│ - Import/export      │  │                  │  │ - create_pr() │  │ commands  │
│ - Section parsing    │  │ ~735 lines       │  │               │  │           │
│                      │  │                  │  │ ~1100 lines   │  │ ~1900     │
│ ~2500 lines          │  │                  │  │               │  │ lines     │
└───────┬──────────────┘  └────────┬─────────┘  └───────┬───────┘  └───────────┘
        │                          │                    │
        └──────────────────────────┼────────────────────┘
                                   │
                ┌──────────────────▼──────────────────┐
                │  klipper_vault_db.py                │
                │  (SQLite connection & schema)       │
                │  ~31 lines                          │
                │  - Connection pooling               │
                │  - Table initialization             │
                │  - Pragmas for performance          │
                └─────────────────────────────────────┘
```

## Module Responsibilities

### Frontend Layer

#### `klipper_vault_gui.py` (Entry Point)
- **Purpose**: Primary launcher and NiceGUI initialization
- **Key Functions**:
  - `main()` - Starts runtime bootstrap and delegates UI construction
  - Helper utilities for theming, mode detection, shutdown handling
  - Event loop management and error handling
- **Size**: ~252 lines
- **Closure Scope**: Contains launcher/runtime wiring; primary UI state lives in `src/klipper_macro_gui.py`
- **Dependencies**: NiceGUI, UIState container

#### `klipper_macro_gui.py` (UI Builder)
- **Purpose**: NiceGUI frontend implementation
- **Responsibilities**:
  - UI layout and theming (`build_ui()` main function)
  - State management via `UIState` container
  - Event handler registration (~150+ handlers)
  - Modal dialog definitions (backup, restore, import, export, PR creation)
  - List rendering with filtering, sorting, pagination
  - Printer profile management UI
  - Macro viewer integration
- **Dependencies**: 
  - `klipper_macro_gui_service.MacroGuiService`
  - `klipper_macro_gui_state.UIState`
  - `klipper_macro_gui_logic` (filtering, sorting helpers)
  - `klipper_macro_viewer.MacroViewer`
  - `klipper_macro_compare.MacroCompareView`
- **Size**: ~3934 lines

#### `klipper_macro_gui_state.py` (State Container)
- **Purpose**: Centralized state management for UI
- **Key Class**: `UIState` dataclass
- **Contains**:
  - Cached macro data (`cached_macros`, `cached_duplicate_names`)
  - Printer state (`printer_is_printing`, `printer_state`, `printer_is_busy`)
  - UI element references (buttons, dialogs, lists, inputs)
  - Pagination state (`list_page_index`, `list_page_size`)
  - Duplicate wizard state
  - SSH and printer profile state
- **Pattern**: Closure capture - GUI callbacks access via closure

### Service Layer

#### `klipper_macro_gui_service.py` (Service Core)
- **Purpose**: Slim core of `MacroGuiService`; inherits the three mixin classes
- **Class**: `MacroGuiService(PrinterProfileMixin, BackupRestoreMixin, OnlineUpdateMixin)`
- **Responsibilities**:
  - Shared `__init__` — establishes all runtime state (`_db_path`, `_active_printer_profile_id`, `_credential_store`, etc.)
  - Macro indexing methods: `index()`, `load_cfg_loading_overview()`, `load_dashboard()`, `load_versions()`, `save_macro_editor_text()`, `remove_deleted()`, `purge_all_deleted()`, `restore_version()`, `resolve_duplicates()`, `list_duplicates()`
  - Shared helpers: `_emit_operation_progress()`, `_resolve_runtime_config_dir()`, `_text_checksum()`, `_require_non_empty()`
- **Size**: ~354 lines

#### `klipper_macro_service_profiles.py` (Printer Profile Mixin)
- **Class**: `PrinterProfileMixin`
- **Responsibilities**:
  - Printer profile CRUD: `list_printer_profiles()`, `activate_printer_profile()`, `save_printer_profile()`, `delete_printer_profile()`
  - SSH profile management: `save_ssh_profile()`, `delete_ssh_profile()`, `test_active_ssh_connection()`
  - Remote cfg sync: `sync_active_remote_cfg_to_local()`, `_push_local_cfg_file_to_active_remote()`, `save_config_to_remote()`
  - Moonraker integration: `query_printer_status()`, `restart_klipper()`, `send_mainsail_notification()`
  - Pydantic models: `MoonrakerStatusResult`, `MoonrakerCommandResult`
- **Size**: ~1204 lines

#### `klipper_macro_service_backup.py` (Backup/Restore Mixin)
- **Class**: `BackupRestoreMixin`
- **Responsibilities**:
  - `create_backup()`, `list_backups()`, `load_backup_contents()`, `restore_backup()`, `delete_backup()`
- **Size**: ~84 lines

#### `klipper_macro_service_online.py` (Online Update Mixin)
- **Class**: `OnlineUpdateMixin`
- **Responsibilities**:
  - Macro share: `export_macro_share_file()`, `import_macro_share_file()`
  - Online updates: `check_online_updates()`, `import_online_updates()`
  - GitHub PR workflow: `export_online_update_repository_zip()`, `create_online_update_pull_request()`
  - Pydantic models: `PullRequestCreationResult`, `ImportedUpdateItem`
- **Size**: ~527 lines

### Business Logic Layer

#### `klipper_macro_indexer.py` (Macro Parsing & Indexing)
- **Purpose**: Parse cfg files, extract macros, insert into SQLite
- **Key Functions**:
  - `run_indexing_from_source()` - Main indexing orchestration (~150 lines)
  - `_parse_macro_section_text()` - Parse gcode_macro section headers
  - `import_macro_share_payload()` - Import shared macros from zip file
  - `export_macro_share_payload()` - Export macros to shareable zip
  - `get_cfg_loading_overview()` - Determine macro load order
  - `load_macro_list()` - Fetch macros from DB with filters
  - `load_macro_versions()` - Get version history for one macro
  - `load_stats()` - Count macros, files, etc.
- **Size**: ~2508 lines
- **Algorithms**:
  - Streaming line-by-line parsing (memory-efficient)
  - Macro section detection and extraction
  - Variable JSON parsing and storage
  - Duplicate macro detection
  - Load order resolution from include chain
- **Complexity**: ⚠️ Refactoring candidate - split into parsing, validation, DB insertion phases
- **Security**: Now includes file size limit check (10 MB max import)

#### `klipper_macro_backup.py` (Backup/Restore)
- **Purpose**: Snapshot macros, version history management
- **Key Functions**:
  - `create_macro_backup()` - Create named backup snapshot
  - `list_macro_backups()` - Fetch backup list for printer profile
  - `load_backup_items()` - Load macros from one backup
  - `restore_macro_backup()` - Restore entire backup to cfg files
  - `delete_macro_backup()` - Remove backup and history
- **Size**: ~735 lines
- **Schema**: `macro_backups` and `macro_backup_items` tables

#### `klipper_macro_explainer.py` (Macro Documentation)
- **Purpose**: Generate human-readable explanations of gcode commands
- **Implementation**: ~1889 lines with command explanation helpers
- **Pattern**: Dynamic dispatch via command name lookup
- **Used By**: Macro viewer to show command descriptions
- **Refactoring Opportunity**: Data-driven approach for command explanation definitions

#### `klipper_macro_online_update.py` (Online Macro Updates)
- **Purpose**: Check for and import online macro updates
- **Key Functions**:
  - `check_online_macro_updates()` - Compare local vs online versions
  - `import_online_macro_updates()` - Import selected updates from remote
- **Size**: ~700 lines
- **Pattern**: Good separation of concerns

#### `klipper_macro_online_repo_export.py` (GitHub Export)
- **Purpose**: Build and export macro repository for GitHub
- **Used By**: PR creation workflow
- **Key Functions**:
  - `build_online_update_repository_artifacts()` - Create repo structure
  - `export_online_update_repository_zip()` - Generate zip for PR

#### `klipper_macro_indexer_queries.py` (Indexer Query Layer)
- **Purpose**: Isolate SQLite read/query concerns from parser and mutation paths
- **Key Functions**:
  - `load_stats()` - Dashboard aggregates and latest-index timestamp
  - `load_macro_list()` - Latest-version macro list payload with load-order enrichment
  - `load_macro_versions()` - Version history rows for one macro
  - `load_duplicate_macro_groups()` - Duplicate groups for resolver workflows

### Configuration & Storage Layer

#### `klipper_vault_config.py` (App Settings)
- **Purpose**: Load and save application configuration
- **Contents**:
  - UI language preference
  - Theme mode (dark/light/auto)
  - Version history retention size
  - Developer mode flag
- **Storage**: SQLite `vault_settings` table
- **Pattern**: In-memory VaultConfig object loaded at startup

#### `klipper_vault_db.py` (Database Connection)
- **Purpose**: SQLite connection management and schema initialization
- **Pattern**: Context manager for automatic connection cleanup
- **Features**:
  - WAL mode (Write-Ahead Logging) for concurrent access
  - PRAGMA optimizations for performance
  - Schema versioning and migration support
- **Size**: ~31 lines (clean, focused)

#### `klipper_vault_paths.py` (File Paths)
- **Purpose**: Centralized path resolution for config and data directories
- **Directories**:
  - Linux: `~/.config/klippervault/` (config), `~/.local/share/klippervault/` (data)
  - macOS: `~/Library/Application Support/klippervault/`
  - Windows: `%APPDATA%\klippervault\`

#### `klipper_vault_printer_profiles.py` (Printer Profiles)
- **Purpose**: Printer profile CRUD operations
- **Fields**: vendor, model, connection_type, SSH host/port, Moonraker URL
- **Schema**: `printer_profiles` table

#### `klipper_vault_remote_profiles.py` (SSH Profiles)
- **Purpose**: SSH connection profile management
- **Fields**: host, port, username, password (optional, fallback)
- **Schema**: `ssh_host_profiles` table
- **Pattern**: Credential store integration for secure storage

### Network & Transport Layer

#### `klipper_vault_ssh_transport.py` (SSH Implementation)
- **Purpose**: SSH/SFTP file transfer and Klipper command execution
- **Pattern**: Paramiko-based async transport with connection pooling
- **Operations**: Download cfg, upload modified cfg, execute Klipper commands
- **Error Handling**: Timeout, authentication, connection failure recovery

#### `klipper_vault_config_source.py` (Config Sources)
- **Purpose**: Abstract interface for config file access
- **Implementations**:
  - `LocalConfigSource` - Direct file system access
  - `SshConfigSource` - Remote access via SSH
- **Pattern**: Strategy pattern for flexible config access

#### `klipper_macro_github_api.py` (GitHub Integration)
- **Purpose**: GitHub API operations for PR creation
- **Operations**: Create branch, commit files, create pull request
- **Auth**: Token-based authentication
- **API**: Uses httpx for async HTTP requests

### Utilities & Helpers

#### `klipper_macro_gui_logic.py` (UI Logic)
- **Purpose**: Filtering, sorting, and list manipulation for UI
- **Functions**:
  - `filter_macros()` - Apply search, status, active/inactive filters
  - `sort_macros()` - Sort by load order, name, creation time
  - `duplicate_names_for_macros()` - Identify macros with same name
  - `macro_key()` - Unique identifier for deduplication

#### `klipper_macro_gui_timers.py` (UI Timer Wiring)
- **Purpose**: Centralize recurring NiceGUI timer registrations
- **Functions**:
  - `register_periodic_updates()` - Registers startup and recurring background refresh timers for search flush, update progress, config drift checks, and printer-card refresh

#### `klipper_macro_gui_create_pr_flow.py` (Create-PR Flow Helpers)
- **Purpose**: Keep pull-request workflow transitions outside the core UI closure
- **Functions**:
  - `collect_create_pr_inputs()` - Pulls and normalizes PR dialog values
  - `validate_create_pr_inputs()` - Validates required inputs and profile preconditions
  - `begin_create_pr_request()` / `set_create_pr_request_failure()` / `finish_create_pr_request()` - Encapsulate create-PR progress state transitions
  - `set_create_pr_status_from_result()` - Renders final PR result messaging

#### `klipper_macro_compare_core.py` (Diff Implementation)
- **Purpose**: Compute diffs between macro versions
- **Algorithm**: Unified diff format
- **Used By**: Duplicate resolution wizard, version history viewer

#### `klipper_macro_viewer.py` (Macro Display)
- **Purpose**: Render macro sections with syntax highlighting
- **Features**:
  - Syntax coloring for gcode
  - Variable display
  - Macro comparison view
  - Version history navigation

#### `klipper_type_utils.py` (Type Conversions)
- **Purpose**: Safe type conversions with defaults
- **Functions**: `to_int()`, `to_text()`, `to_dict_list()`, `cast()`

#### `klipper_vault_i18n.py` (Internationalization)
- **Purpose**: Multi-language support
- **Supported**: English, German, French (via Babel/gettext)
- **Pattern**: `t("message key")` for translation lookups

#### `klipper_vault_secret_store.py` (Credential Management)
- **Purpose**: Secure storage of SSH passwords
- **Backend**: System keyring (Linux keyctl, macOS Keychain, Windows Credential Manager)
- **Fallback**: Plaintext SQLite storage (documented, not recommended)

## Data Flow

### Indexing Flow
```
User clicks "Scan macros"
    ↓
GUI calls service.index()
    ↓
klipper_macro_indexer.run_indexing_from_source()
    ├─ Download cfg files (if SSH)
    ├─ Parse each cfg file line-by-line
    ├─ Extract [gcode_macro ...] sections
    ├─ Resolve includes and load order
    └─ Insert into SQLite
    ↓
GUI refreshes macro list from DB
```

### Backup Flow
```
User enters backup name
    ↓
GUI calls service.create_backup(name)
    ↓
klipper_macro_backup.create_macro_backup()
    ├─ Create macro_backups row
    ├─ Copy all macros to macro_backup_items
    └─ Store snapshot with timestamp
    ↓
GUI shows "Backup created"
```

### Import/Export Flow
```
User uploads share file
    ↓
GUI calls service.export_macro_share_payload(selected_macros)
    ↓
klipper_macro_indexer.export_macro_share_payload()
    ├─ Build JSON payload with macro sections
    └─ Zip into downloadable .kvmacros file
    ↓
User shares .kvmacros file
    ↓
Another user imports via "Import macros"
    ↓
GUI calls service.import_macro_share_payload(payload)
    ↓
klipper_macro_indexer.import_macro_share_payload()
    ├─ Validate file size (max 10 MB)
    ├─ Parse each macro section
    └─ Insert as "new" inactive macros
    ↓
GUI shows preview, user activates
```

## Database Schema

### Core Tables

#### `macros` - Macro inventory
```sql
id              INTEGER PRIMARY KEY
file_path       TEXT - relative path to cfg file
section_type    TEXT - "gcode_macro", "gcode", etc.
macro_name      TEXT - name of macro
version         INTEGER - version number (1-indexed)
description     TEXT - [description] field
rename_existing TEXT - [rename_existing] field
gcode           TEXT - macro code block
variables_json  TEXT - serialized [variable] fields
body_checksum   TEXT - SHA256 of section text
created_at      INTEGER - unix timestamp
is_loaded       BOOLEAN - macro loaded on printer
is_dynamic      BOOLEAN - from [dynamicmacros]
printer_profile_id INTEGER - foreign key
```

#### `macro_backups` - Backup metadata
```sql
id              INTEGER PRIMARY KEY
backup_name     TEXT - user-friendly backup name
printer_profile_id INTEGER
created_at      INTEGER - unix timestamp
```

#### `macro_backup_items` - Backed up macro entries
```sql
id              INTEGER PRIMARY KEY
backup_id       INTEGER - foreign key to macro_backups
file_path       TEXT
macro_name      TEXT
version         INTEGER
body_checksum   TEXT
```

#### `printer_profiles` - Printer configurations
```sql
id              INTEGER PRIMARY KEY
profile_name    TEXT - "Default Printer", "Voron 2.4", etc.
vendor          TEXT - printer brand
model           TEXT - specific model
connection_type TEXT - "standard", "local_ssh"
ssh_profile_id  INTEGER - foreign key to ssh_host_profiles
ssh_host        TEXT - override SSH host
ssh_port        INTEGER - override SSH port
ssh_username    TEXT - override SSH user
ssh_remote_config_dir TEXT - remote config path
ssh_moonraker_url TEXT - remote Moonraker URL
ssh_auth_mode   TEXT - "password", "key"
ssh_credential_ref TEXT - keyring identifier
is_active       BOOLEAN
is_archived     BOOLEAN
```

#### `ssh_host_profiles` - SSH connection profiles
```sql
id              INTEGER PRIMARY KEY
host            TEXT - SSH hostname or IP
port            INTEGER - SSH port (default 22)
username        TEXT - SSH username
password        TEXT - fallback password (encrypted/plaintext)
moonraker_url   TEXT - Moonraker URL on remote
identify        TEXT - printer identity identifier
is_active       BOOLEAN
```

#### `vault_settings` - App configuration
```sql
key             TEXT PRIMARY KEY
value           TEXT - JSON-encoded setting
```

## Performance Characteristics

### Indexing
- **Speed**: ~0.5-2 seconds for typical Klipper config tree
- **Memory**: Streaming line parsing, minimal buffering
- **Optimization**: Include path caching, duplicate detection in-memory

### Database Queries
- **WAL mode**: Enables concurrent reader/writer
- **Pragmas**: `journal_mode=WAL`, `synchronous=NORMAL` for speed
- **Indexes**: Created on common query patterns (file_path, macro_name, version)

### UI Responsiveness
- **Pagination**: Lists load 50-200 macros per page (configurable)
- **Async**: Indexing and remote operations run in background
- **Caching**: Macro text, version history cached in memory

## Testing

### Test Files (20 test modules in `tests/`)
- `test_macro_indexer.py` - Macro parsing, indexing
- `test_macro_backup.py` - Backup/restore operations
- `test_macro_gui_logic.py` - Filtering and sorting
- `test_macro_gui_service_*.py` - Service layer operations
- `test_printer_profiles.py` - Profile management
- `test_vault_config.py` - Configuration loading
- `test_vault_i18n.py` - Internationalization
- `test_vault_paths.py` - Path resolution
- And more...

### Coverage Gaps
- ⚠️ GUI rendering in a browser runtime (NiceGUI widget rendering and client-side interactions)
- ⚠️ Full remote E2E (real SSH target + real GitHub repo in CI-safe sandbox)

## Known Limitations & Refactoring Candidates

### Current Open Architecture Work
- Browser-runtime GUI test coverage and full remote E2E coverage are still missing (see Coverage Gaps above).

## Conclusion

KlipperVault follows a clean layered architecture with clear separation of concerns:
- **UI Layer**: Handles presentation and user interaction
- **Service Layer**: Orchestrates business logic
- **Business Logic Layer**: Implements domain operations (parsing, backup, online)
- **Storage Layer**: Database, configuration, paths
- **Transport Layer**: SSH, HTTP, GitHub API

The codebase is well-organized with clear layering and a short list of targeted refactoring opportunities.
