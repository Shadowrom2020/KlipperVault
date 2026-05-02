from pathlib import Path

from klipper_vault_printer_profiles import (
    create_printer_profile,
    ensure_default_printer_profile,
    get_active_printer_profile,
    list_printer_profiles,
    set_active_printer_profile,
)
from klipper_vault_remote_profiles import SshHostProfile, upsert_ssh_host_profile


def test_ensure_default_printer_profile_creates_active_profile(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"

    profile_id = ensure_default_printer_profile(db_path)

    assert profile_id > 0
    profiles = list_printer_profiles(db_path)
    assert len(profiles) == 1
    assert profiles[0].id == profile_id
    assert profiles[0].is_active is True
    assert profiles[0].is_virtual is False


def test_set_active_printer_profile_switches_profiles(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"

    ssh_id = upsert_ssh_host_profile(
        db_path,
        SshHostProfile(
            profile_name="remote",
            host="printer.local",
            username="pi",
            remote_config_dir="~/printer_data/config",
            moonraker_url="http://printer.local:7125",
            is_active=True,
        ),
    )
    _ = ssh_id

    first_id = ensure_default_printer_profile(db_path)
    profiles = list_printer_profiles(db_path, include_archived=True)
    assert profiles

    # Create a second profile by reusing the default and inserting a new one via ensure path.
    # Keep this test focused on active profile switching behavior.
    from klipper_vault_printer_profiles import ensure_printer_profile_schema
    from klipper_vault_db import open_sqlite_connection

    with open_sqlite_connection(db_path, ensure_schema=ensure_printer_profile_schema) as conn:
        conn.execute(
            """
            INSERT INTO printer_profiles (
                profile_name, vendor, model, connection_type, ssh_profile_id,
                is_active, is_archived, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s','now'), strftime('%s','now'))
            """,
            ("Second Printer", "", "", "standard", None, 0, 0),
        )
        second_id = int(conn.execute("SELECT id FROM printer_profiles WHERE profile_name = ?", ("Second Printer",)).fetchone()[0])
        conn.commit()

    assert set_active_printer_profile(db_path, second_id) is True
    active = get_active_printer_profile(db_path)
    assert active is not None
    assert active.id == second_id
    assert active.id != first_id


def test_create_virtual_printer_profile_persists_virtual_flag(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    _ = ensure_default_printer_profile(db_path)

    virtual_id = create_printer_profile(
        db_path,
        profile_name="Virtual Voron",
        vendor="Voron",
        model="Trident",
        is_virtual=True,
        is_active=True,
    )

    active = get_active_printer_profile(db_path)
    assert active is not None
    assert active.id == virtual_id
    assert active.is_virtual is True
