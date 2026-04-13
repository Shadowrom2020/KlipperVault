#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Legacy on-printer entrypoint deprecation helper.

This script performs startup cleanup for deprecated on-printer installations:
- remove KlipperVault update-manager section from Moonraker config
- send a Mainsail deprecation notification via Moonraker
- stop legacy KlipperVault systemd service
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404
import sys
from pathlib import Path
from urllib import error, request


README_URL = "https://github.com/Shadowrom2020/KlipperVault/blob/main/README.md"
MOONRAKER_URL_DEFAULT = "http://127.0.0.1:7125"


def _home_dir() -> Path:
    """Return effective install-user home directory."""
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or ""
    if user:
        return Path(os.path.expanduser(f"~{user}")).resolve()
    return Path.home().resolve()


def _moonraker_config_candidates(home_dir: Path) -> list[Path]:
    """Return possible Moonraker config file locations."""
    configured = os.environ.get("MOONRAKER_CONFIG_PATH", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser().resolve())
    candidates.extend(
        [
            (home_dir / "printer_data" / "config" / "moonraker.conf").resolve(),
            (home_dir / "printer_data" / "config" / "moonraker.cfg").resolve(),
            Path("/etc/moonraker.conf"),
        ]
    )

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        if path not in seen:
            unique.append(path)
            seen.add(path)
    return unique


def _remove_update_manager_klippervault_section(config_path: Path) -> bool:
    """Remove [update_manager ...klippervault...] section from Moonraker config."""
    if not config_path.exists() or not config_path.is_file():
        return False

    content = config_path.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)

    section_header_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    kept: list[str] = []
    in_removed_section = False
    removed_any = False

    for line in lines:
        header_match = section_header_re.match(line)
        if header_match:
            section_name = header_match.group(1).strip().lower()
            remove_section = "update_manager" in section_name and "klippervault" in section_name
            if remove_section:
                in_removed_section = True
                removed_any = True
                continue
            in_removed_section = False

        if not in_removed_section:
            kept.append(line)

    if not removed_any:
        return False

    backup_path = config_path.with_suffix(config_path.suffix + ".bak_klippervault")
    try:
        if not backup_path.exists():
            backup_path.write_text(content, encoding="utf-8")
    except OSError:
        # Non-fatal: continue with write attempt.
        pass

    config_path.write_text("".join(kept), encoding="utf-8")
    return True


def _send_mainsail_deprecation_notification() -> None:
    """Send deprecation notification to Mainsail via Moonraker gcode API."""
    moonraker_url = os.environ.get("KLIPPERVAULT_MOONRAKER_URL", MOONRAKER_URL_DEFAULT).strip() or MOONRAKER_URL_DEFAULT
    payload_message = (
        "Running KlipperVault on printer is deprecated and has been removed. "
        f"Use remote mode only. See {README_URL}"
    )
    escaped = payload_message.replace("\\", "\\\\").replace('"', '\\"')
    gcode = f'RESPOND TYPE=command MSG="action:notification KlipperVault: {escaped}"'

    body = json.dumps({"script": gcode}).encode("utf-8")
    endpoint = moonraker_url.rstrip("/") + "/printer/gcode/script"
    req = request.Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=3.0):
            pass
    except (error.URLError, error.HTTPError, TimeoutError):
        # Non-fatal: service should still be stopped even when Moonraker is unavailable.
        pass


def _stop_legacy_service() -> None:
    """Stop legacy KlipperVault systemd service names."""
    for service_name in ("klippervault.service", "klipper-vault.service"):
        try:
            subprocess.run(["systemctl", "stop", service_name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # nosec B603
        except OSError:
            # Non-fatal: service manager may be unavailable in some environments.
            pass


def main() -> None:
    """Run on-printer deprecation cleanup actions and stop legacy service."""
    home = _home_dir()
    removed_any = False
    for moonraker_cfg in _moonraker_config_candidates(home):
        try:
            removed = _remove_update_manager_klippervault_section(moonraker_cfg)
        except OSError:
            removed = False
        removed_any = removed_any or removed

    if removed_any:
        print("[KlipperVault] Removed Moonraker update-manager entry for KlipperVault.", flush=True)
    else:
        print("[KlipperVault] No Moonraker update-manager KlipperVault entry found.", flush=True)

    _send_mainsail_deprecation_notification()
    print("[KlipperVault] Sent Mainsail deprecation notification (best effort).", flush=True)

    _stop_legacy_service()
    print("[KlipperVault] Requested stop for legacy KlipperVault service.", flush=True)


if __name__ in {"__main__", "__mp_main__"}:
    main()
