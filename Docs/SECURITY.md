# KlipperVault Security Model

## Overview

KlipperVault implements security at multiple layers to protect printer configurations, macros, and credentials. This document describes the security model, threat analysis, and best practices.

## Security Principles

### 1. Defense in Depth
- Multiple layers of validation (input validation, type checking, size limits)
- Secure credential storage (system keyring with plaintext fallback)
- SQL injection prevention (parameterized queries throughout)
- File access controls (relative path validation, protected cfg detection)

### 2. Fail Secure
- File operations fail safely on permission/access errors
- Import operations validated before database modifications
- SSH connection failures prevent file mutations
- Printer commands validated before execution

### 3. Minimal Privilege
- SSH credentials stored securely, never logged
- Printer profile information stored locally
- Remote config checksums validated before upload
- Protected cfg files (printer.cfg) blocked from editing

### 4. Transparency
- Users informed of import/export operations
- Connection status clearly displayed
- Configuration changes logged with user action
- Security limitations documented (see limitations section)

## Authentication & Authorization

### SSH Authentication

#### Supported Modes

1. **SSH Key Authentication** (Recommended)
   - Uses local SSH key pair (~/.ssh/id_rsa)
   - Paramiko-based implementation
   - Most secure option
   - No credential storage needed

2. **SSH Password Authentication** (Fallback)
   - System keyring storage (Linux: pass, macOS: Keychain, Windows: Credential Manager)
   - Plaintext SQLite fallback if keyring unavailable
   - ⚠️ **Warning**: Plaintext passwords are not recommended for production
   - User is warned during setup

#### Credential Flow
```
User enters SSH password
  ↓
Application attempts system keyring storage
  ├─ Success: Stored securely in OS keyring
  └─ Failure: Fallback to plaintext SQLite (with warning dialog)
  ↓
Credentials retrieved only when needed for SSH operations
  ↓
Credentials never logged, displayed, or transmitted outside SSH protocol
```

#### Best Practices
- Use SSH key authentication when possible
- If passwords required, set strong unique passwords
- Store keyring passwords securely (OS-managed)
- Do not share printer profiles containing passwords
- Rotate passwords periodically
- Disable password authentication on remote Klipper systems once key auth is set up

### Local Access Control

KlipperVault runs as the user executing the application. It respects:
- **File system permissions**: Cannot access files outside user's permissions
- **Protected files**: Actively prevents editing of `printer.cfg` (core config)
- **SSH host validation**: Validates SSH host connection before operations

## Input Validation & Sanitization

### Macro Name Validation
```python
# Macro names restricted to valid gcode_macro identifiers
# Pattern: [a-zA-Z0-9_]
```
- Validated at import time
- Prevents injection attacks in macro execution

### File Path Validation
```python
# Relative path validation to prevent directory traversal
# Rules:
# - Must be relative (not absolute)
# - Cannot contain ".." segments
# - Must be within config directory
```
- Prevents `../../../etc/passwd` style attacks
- Enforced in `_safe_import_file_path()` function

### SSH Configuration Validation
- Host: Must be valid hostname or IP
- Port: Must be 1-65535
- Username: No validation (SSH protocol handles)
- Remote path: Relative path validation applied
- Moonraker URL: Must be valid HTTP URL

### Import Payload Validation

**NEW: File Size Limit (V1.0.0)**
```python
MAX_IMPORT_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Enforced in import_macro_share_payload()
if payload_size > MAX_IMPORT_FILE_SIZE:
    raise ValueError("Import file too large")
```

**Rationale**: Prevents out-of-memory attacks via maliciously large share files

**Validation Chain**:
1. File size check (10 MB limit)
2. Format validation (must be valid JSON)
3. Macro count validation (must have macros)
4. Macro section parsing (must be valid gcode_macro)
5. Database insertion (transactional, rollback on error)

### SQL Injection Prevention
All database queries use parameterized statements:
```python
# ✅ SAFE: Parameterized query
cursor.execute("SELECT * FROM macros WHERE macro_name = ?", (user_input,))

# ❌ DANGEROUS: String interpolation (NOT used in KlipperVault)
cursor.execute(f"SELECT * FROM macros WHERE macro_name = '{user_input}'")
```

Enforced via `open_sqlite_connection()` context manager.

## Protected Operations

### Blocked Modifications

1. **printer.cfg** - Core printer configuration
   - Status: **BLOCKED** - Cannot be edited or deleted
   - Reason: Core Klipper configuration
   - Error Message: "Cannot modify protected file: printer.cfg"
   - Code Location: `_cfg_is_protected()` check

2. **Macro Deletion** - Permanent removal
   - Status: **Allowed** - Removes from active config
   - Safety: Soft delete to version history (can restore)
   - User Confirmation: Required via dialog
   - Code: `delete_macro()` with confirmation

3. **Config Upload** - Remote file modification
   - Status: **Protected** - Checksum validation required
   - Mechanism: Detect remote changes before uploading
   - Conflict Detection: Compare local vs remote checksums
   - Error: "Remote cfg conflict - sync and retry"
   - Code: `_remote_conflict_message()` check

## Printer Command Execution

### Restart Klipper
- **Blocked During**: Print in progress
- **Requires**: User confirmation
- **Mechanism**: Uses Moonraker REST API
- **Error Handling**: Connection failure → user notification
- **Log**: Operation logged with timestamp

### Reload Dynamic Macros
- **Blocked During**: Print in progress
- **Mechanism**: [gcode_shell_command] execution via Moonraker
- **Validation**: Only available if feature enabled in printer config
- **Error Handling**: Timeout → user notification

### Macro Execution
- **Not Implemented**: KlipperVault does not execute macros
- **User Must**: Open Mainsail/Fluidd for macro execution
- **Reason**: Execution context and print state management outside scope

## Data Storage Security

### SQLite Database
**Location**: `~/.local/share/klippervault/vault.db` (Linux/macOS) or `%APPDATA%\klippervault\vault.db` (Windows)

**Protection**:
- File ownership: User only (mode 0o600)
- ⚠️ Not encrypted - relies on OS file system encryption
- Contains: Macros, backups, printer profiles, **SSH credentials** (when keyring unavailable)
- WAL mode: Enables concurrent access with minimal locking

**Sensitive Data in Database**:
- SSH passwords (plaintext fallback - ⚠️ Warning: Not recommended)
- SSH private key paths
- Macro content (source code)

**Best Practices**:
- Use full-disk encryption (BitLocker, FileVault, dm-crypt)
- Don't share backup files containing SSH credentials
- Use SSH keys instead of passwords
- Regularly backup database (includes macro history)

### File System
**Config Directory**: `~/.config/klippervault/` (Linux/macOS)
**Contains**:
- Application settings (vault_config.json)
- Cached data
- SSH key references

**Protection**: Standard user file permissions

## Network Security

### SSH Transport
- **Protocol**: SSH 2.0 via Paramiko
- **Ciphers**: OS system ciphers (negotiated with server)
- **Authentication**: Key or password (see Authentication section)
- **Port Forwarding**: None (direct file operations)

### HTTP/HTTPS
- **Moonraker API**: 
  - Supports both HTTP and HTTPS
  - Use HTTPS in production
  - URL format: `https://printer.local:7125`
- **GitHub API**:
  - HTTPS required
  - Token-based authentication
  - Rate limits: 60 requests/hour (unauthenticated), higher with token

### Credential Transmission
- **SSH**: Transmitted via SSH protocol only (encrypted)
- **GitHub Token**: Sent via HTTPS headers
- **Moonraker**: API key sent via HTTPS (if required)
- **Never**: Logged to console or files

## Threat Analysis

### High-Risk Threats

#### 1. Local File System Access
**Threat**: Attacker with local file system access to user home directory
**Impact**: High - Can read database, SSH credentials, macros
**Mitigation**:
- Use full-disk encryption
- Restrict user account access
- Don't share home directory
**Status**: Accepted risk (scope of OS security)

#### 2. SSH Credential Compromise
**Threat**: SSH password leaked via plaintext fallback storage
**Impact**: High - Unauthorized access to remote Klipper
**Mitigation**:
- Use SSH keys instead of passwords
- Store passwords in system keyring only
- Rotate passwords if exposed
- Monitor SSH logs on remote system
**Status**: Documented, user-mitigated

#### 3. Malicious Macro Code
**Threat**: Imported macros contain harmful gcode
**Impact**: Medium - Could damage printer (wrong moves, heat, etc.)
**Mitigation**:
- Review imported macros before activation
- Only import from trusted sources
- Test in safe conditions
**Status**: User responsibility

### Medium-Risk Threats

#### 4. Remote Code Execution via SSH
**Threat**: Attacker controls remote Klipper system
**Impact**: Medium - Can execute gcode commands
**Mitigation**:
- Use authenticated SSH (key-based)
- Firewall remote systems
- Monitor network access
**Status**: Accepted (network security scope)

#### 5. Man-in-the-Middle (MITM) on SSH
**Threat**: Network attacker intercepts SSH connection
**Impact**: Medium - Could intercept credentials, execute commands
**Mitigation**:
- SSH uses encrypted protocol (resistant to MITM)
- Host key verification (first-time connection warning)
- Use VPN or trusted networks
**Status**: Mitigated by SSH protocol

#### 6. Large File Denial of Service (DoS)
**Threat**: Attacker creates 1GB import file to crash app
**Impact**: Medium - Out-of-memory, application crash
**Mitigation**:
- **NEW**: File size limit (10 MB) in V1.0.0
- Application continues on parse failure
- User notified with clear error
**Status**: **RESOLVED** (10 MB limit added)

### Low-Risk Threats

#### 7. Macro Name Injection
**Threat**: Macro name with special characters injected into SQL
**Impact**: Low - Parameterized queries prevent injection
**Mitigation**:
- All queries use parameterized statements
- Macro names validated before use
**Status**: Mitigated

#### 8. Directory Traversal
**Threat**: Path like `../../etc/passwd` escapes config directory
**Impact**: Low - Path validation prevents escape
**Mitigation**:
- Relative path validation in `_safe_import_file_path()`
- Protected file detection
**Status**: Mitigated

#### 9. User Interface Spoofing
**Threat**: Attacker modifies UI to fake success/failure
**Impact**: Low - Local application, only visible to user
**Status**: Accepted (physical security scope)

## Incident Response

### If SSH Credentials Are Compromised

1. **Immediate Actions**:
   - Rotate SSH password on remote system
   - Delete SSH profile from KlipperVault
   - If key-based auth, regenerate key pair

2. **Investigation**:
   - Check remote system logs for unauthorized access
   - Monitor printer for unauthorized macro execution
   - Check if database file was accessed

3. **Prevention**:
   - Use SSH key authentication instead of password
   - Use system keyring for password storage
   - Enable SSH key-based auth only on remote

### If Database File Is Compromised

1. **Immediate Actions**:
   - Regenerate SSH profiles
   - Rotate all sensitive passwords
   - Check printer configuration in remote system

2. **Recovery**:
   - Database can be deleted (will be re-indexed)
   - Backups preserved in separate tables
   - No data loss of macro history

3. **Prevention**:
   - Use full-disk encryption
   - Restrict home directory access
   - Backup database to encrypted storage

### If Macro Code Is Malicious

1. **Immediate Actions**:
   - Do not activate dangerous macros
   - Delete imported macros
   - Inspect gcode before running

2. **Recovery**:
   - Restore printer.cfg from backup
   - Use version history to revert changes
   - Reset macros to last known-good state

## Compliance & Standards

### Data Protection
- No personal data collection
- No telemetry or analytics
- User data stored locally only
- No cloud synchronization

### Encryption
- No end-to-end encryption (user responsibility)
- SSH: Encrypted via SSH protocol
- Database: Relies on OS-level encryption
- Transport: HTTPS for HTTP APIs

### Audit
- No built-in audit logging
- File modification tracked by version history
- User actions visible in UI (confirmation dialogs)
- Consider external monitoring for production

## Future Security Improvements (Post-1.0)

### High Priority
1. **Database encryption** - Encrypt vault.db at rest
2. **Audit logging** - Full operation audit trail
3. **Configuration signing** - Detect modified remote configs
4. **Rate limiting** - Prevent brute-force SSH attacks

### Medium Priority
5. **Two-factor authentication** - For SSH key management
6. **Configuration backups** - Encrypted off-site storage
7. **Macro signing** - Verify macro authorship/integrity
8. **RBAC** - Role-based access control for multi-user setups

### Lower Priority
9. **Vault encryption** - Encrypt sensitive fields in DB
10. **API authentication** - If KlipperVault exposed as service
11. **Certificate pinning** - HTTPS server certificate validation
12. **Macro sandboxing** - Restrict macro capabilities

## Security Reporting

### Vulnerability Disclosure
To report security vulnerabilities:
1. **Do not** create public issues on GitHub
2. Email security details to project maintainer
3. Include proof-of-concept if possible
4. Allow reasonable time for fix before disclosure

### Security Updates
- Monitor releases for security patches
- Update to latest version promptly
- Subscribe to announcements for critical fixes

## Testing & Validation

### Security Testing Performed
- ✅ SQL injection tests (parameterized queries)
- ✅ File path traversal tests (relative path validation)
- ✅ File size limit tests (10 MB import limit)
- ✅ SSH connection validation
- ⚠️ Penetration testing (not performed - external testing recommended)
- ⚠️ Cryptography review (not performed - uses system keyring/SSH)

### Recommended External Testing
- Professional security audit before production use
- Penetration testing of SSH implementation
- Code review for injection vulnerabilities
- Cryptography review of credential storage

## Conclusion

KlipperVault implements multi-layered security appropriate for a local macro management application. The primary threat model focuses on:
1. Local file system access control (OS responsibility)
2. SSH credential management (system keyring with fallback)
3. Input validation (parameterized queries, path validation)
4. Protected operations (confirmation dialogs, file path restrictions)

Users should follow best practices for SSH authentication and use system encryption for additional protection of sensitive data. The application is suitable for single-user local use; multi-user or network-exposed deployments would require additional security controls.
