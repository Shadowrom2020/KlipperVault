# KlipperVault Macro Developer Guide

This guide is for developers who want to maintain and distribute macro collections using KlipperVault's developer features.

## Overview

KlipperVault provides tools for:
- **Exporting** local macros as update repository bundles (ZIP format)
- **Publishing** macro updates to GitHub via pull requests

## Setting Up a Macro Repository

### 1. Create a GitHub Repository

Create a new GitHub repository to host your macro collection:

```bash
# Example: macros repository
Repository Name: klipper-macros
Description: Collection of Klipper gcode macros for community use
```

### 2. Repository Structure

Your repository should follow this structure:

```
klipper-macros/
├── README.md                    # Repository overview
├── [vendor]/[model]/
│   ├── manifest.json            # Used by update checks, ZIP export, and PR publishing
│   ├── [MACRO_NAME_A].json
│   ├── [MACRO_NAME_B].json
│   └── ...
└── CONTRIBUTING.md             # Contributors guide (optional)
```

Notes:
- KlipperVault now uses one manifest per printer at [vendor]/[model]/manifest.json for all online workflows.

### 3. Initial Commit

Start with a basic README and one starter printer manifest:

```bash
git init
echo "# Klipper Macros" > README.md

# Example printer-local manifest used by all online update workflows
mkdir -p voron/trident
echo '{"generated_at": null, "macros": []}' > voron/trident/manifest.json

git add README.md voron/trident/manifest.json
git commit -m "Initial commit"
git push -u origin main
```

## Configuring KlipperVault

### UI Menu Map

Current toolbar/menu locations for developer workflows:

- **Developer** menu (visible only when `developer: true`):
   - `Create Virtual Printer`
   - `Export Update Zip`
   - `Create Pull Request`
   - `Import macro.cfg`

Page behavior:
- On the printer selection page, only `Create Virtual Printer` is shown in the Developer menu.
- After connecting to a profile and entering the macro workspace, all developer actions are shown.

### Virtual Printers (Developer Mode)

Use virtual printers when you want to maintain macro repositories for hardware you do not physically own.

Behavior:
- Created from `Developer -> Create Virtual Printer`.
- Auto-activated after creation.
- Local-only mode: no SSH connection or Moonraker connectivity is required.
- Online update checks, ZIP export, and PR publishing still use the selected vendor/model and per-printer manifest path.
- Remote `Save Config` upload is disabled for virtual printer profiles.

Selection-page card behavior:
- Virtual printer cards do not show an edit action.
- All printer cards include a delete action with confirmation.
- Confirmed deletion removes the profile and its associated printer-scoped vault data from the database.

### 1. Enable Developer Mode

In the app, open `Macro actions -> Settings`, then enable `Developer mode` and save.

### 2. Configure Repository URL

In `Macro actions -> Settings`, set:

- `Online update repository URL`: `https://github.com/your-username/klipper-macros`
- `Online update reference`: `main`

- **online_update_repo_url**: Full GitHub repository URL (HTTPS)
- **online_update_ref**: Branch, tag, or commit SHA for macro updates

Important:
- Manifest paths are derived automatically from active printer vendor/model as [vendor]/[model]/manifest.json.

### 3. Set Printer Vendor/Model

Set the active printer profile in `Manage printer connections` so vendor/model are available.

This ensures your exported macros are placed in `[vendor]/[model]` subdirectories.

## Publishing Macros via Pull Request

### 1. Generate GitHub Access Token

A personal access token is required to create pull requests programmatically.

**Steps:**

1. Go to GitHub Settings → **Developer settings** → **Personal access tokens**
2. Click **Tokens (classic)** (or **Fine-grained tokens** for better security)
3. Click **Generate new token**

**Required Permissions (Classic Token):**
- ✓ `repo` (Full repository control)
- ✓ `workflow` (Update GitHub Action workflows) — optional

**Recommended Permissions (Fine-grained Token):**
- ✓ `contents: read and write` (Read and write repository contents)
- ✓ `pull-requests: read and write` (Read and write pull requests)

4. Set expiration: **90 days** or **No expiration** (your choice)
5. Copy the token (you won't see it again!)

### 2. Create Pull Request in KlipperVault

1. Open KlipperVault web UI
2. Click **Developer** menu in toolbar
3. Select **Create Pull Request**
4. Fill in the form:
   - **Repository URL**: Your macro repository URL (auto-filled from config)
   - **Base branch**: Target branch (`main`)
   - **Head branch**: Auto-generated from printer vendor/model and timestamp
   - **Pull request title**: Auto-filled with printer info (customize as needed)
   - **Pull request description**: Auto-filled (customize as needed)
   - **GitHub API token**: Paste your personal access token here

5. Click **Create PR**

**Note:** The token is **not stored** — it's used only for this request and discarded immediately.

### 3. Review & Merge PR

Once the PR is created:
1. Review the changes on GitHub
2. Check the printer-local manifest was updated correctly: `[vendor]/[model]/manifest.json`
3. Verify macros are placed in correct subdirectories: `[vendor]/[model]/`
4. Merge the PR
5. Monitor for GitHub Actions workflows (if configured)

## Storing Access Tokens Securely

Your GitHub access token is sensitive — treat it like a password.

### Option: Use a Password Manager (Recommended)

#### KeePass (Windows/Linux)

1. Open KeePass
2. Create new entry: **GitHub KlipperVault Token**
3. Username: `github.com`
4. Password: Paste your token
5. Save database with strong master password

When you need the token:
- Copy from KeePass
- Paste into KlipperVault form
- Clear clipboard after use

#### Bitwarden (Cross-platform, Cloud-backed)

1. Go to vault.bitwarden.com
2. Create new item: **Notes** or **Secure Note**
3. Save: GitHub token and repository URL
4. Set organization/collection permissions
5. Use browser extension to auto-fill when needed

#### 1Password (macOS/iOS/Windows)

1. Create new login item
2. Title: `GitHub KlipperVault`
3. Username: `github.com`
4. Password: Paste token
5. Enable **Two-Factor Authentication** on your GitHub account

### Best Practices

1. **Use a personal access token**, not your GitHub password
2. **Set short expiration** (90 days) and rotate regularly
3. **Use fine-grained tokens** for specific repository access only
4. **Enable 2FA** on your GitHub account
5. **Never share your token** with others
6. **Rotate tokens** if you suspect compromise
7. **Use a password manager** for secure storage
8. **Clear KlipperVault form** after each use (token is not stored)

## Exporting Macros as ZIP Bundle

The **Export Update Zip** feature packages your local macros for distribution.

### Use Cases

1. **Test updates locally** before pushing to GitHub
2. **Create backups** of macro repository state
3. **Review artifacts** before creating a pull request

### Steps

1. Click **Developer** menu in toolbar
2. Select **Export Update Zip**
3. ZIP file downloads with:
   - All active local macros
   - Manifest file with checksums and versions at `[vendor]/[model]/manifest.json`
   - Macro JSON files in `[vendor]/[model]/`
4. Use the ZIP for manual review or repository-side validation

## Example: Setting Up a Community Macro Repository

### Step 1: Create Repository on GitHub

```bash
# Clone your new repository
git clone https://github.com/your-username/klipper-macros.git
cd klipper-macros

# Create one example printer-local manifest for online update + PR workflows
mkdir -p voron/trident
cat > voron/trident/manifest.json << 'EOF'
{
   "generated_at": null,
   "macros": []
}
EOF

# Commit
git add voron/trident/manifest.json
git commit -m "Initialize manifest"
git push
```

### Step 2: Configure KlipperVault

In `Macro actions -> Settings`:

- Enable `Developer mode`
- Set `Online update repository URL` to `https://github.com/your-username/klipper-macros`
- Set `Online update reference` to `main`

In `Manage printer connections`:

- Ensure the active printer profile has vendor/model metadata configured

### Step 3: Export and Create PR

1. Get GitHub token from GitHub Developer settings
2. Open KlipperVault → **Developer** → **Create Pull Request**
3. Paste token
4. Create PR

### Step 4: Merge and Distribute

1. Review PR on GitHub
2. Merge to `main`
3. Confirm the repository now contains the updated manifest and macro files

## Troubleshooting

### "No macro changes detected for pull request"

**Cause:** All local macros match remote versions (by SHA-256 checksum)

**Solution:**
- Verify `printer_vendor` and `printer_model` are set
- Ensure macros have actual content changes
- Check that you're comparing against the correct remote branch

### "Pull request already exists"

**Cause:** An open PR from this branch already exists

**Solution:**
- Merge or close the existing PR
- Or use a different branch name in the PR dialog

### Token rejected / 401 Unauthorized

**Cause:** Token is expired, invalid, or missing required scopes

**Solution:**
1. Generate a new token with correct permissions
2. Verify token has `contents` and `pull-requests` scopes
3. Check token hasn't expired

### Macros not appearing in manifest

**Cause:** Macros might be inactive or filtered during export

**Solution:**
- Ensure macros are **active** in KlipperVault
- Check `printer_vendor` and `printer_model` are set
- Verify no duplicates are preventing activation

### Online updates not found for current printer

**Cause:** The active printer manifest is missing at `[vendor]/[model]/manifest.json` in the repository.

**Solution:**
- Ensure active vendor/model in KlipperVault matches the repository path exactly.
- Ensure `[vendor]/[model]/manifest.json` exists and contains entries for that printer.

## See Also

- [KlipperVault Main README](../README.md)
- [UI Overview](overview.md)
- [GitHub Personal Access Tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/creating-a-personal-access-token)
- [Fine-grained Personal Access Tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/creating-a-fine-grained-personal-access-token)
