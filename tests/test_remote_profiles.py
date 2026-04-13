from pathlib import Path

from klipper_vault_remote_profiles import (
    SshHostProfile,
    get_active_ssh_host_profile,
    get_credential_backend,
    get_fallback_secret,
    list_ssh_host_profiles,
    set_active_ssh_host_profile,
    set_credential_backend,
    set_fallback_secret,
    upsert_ssh_host_profile,
)


def test_upsert_and_list_profiles(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"

    created_id = upsert_ssh_host_profile(
        db_path,
        SshHostProfile(
            profile_name="lab-printer",
            host="192.168.1.50",
            port=22,
            username="pi",
            remote_config_dir="/home/pi/printer_data/config",
            moonraker_url="http://192.168.1.50:7125",
            auth_mode="password",
            credential_ref="ssh:lab-printer:password",
            is_active=True,
        ),
    )

    profiles = list_ssh_host_profiles(db_path)
    assert len(profiles) == 1
    assert profiles[0].id == created_id
    assert profiles[0].profile_name == "lab-printer"
    assert profiles[0].is_active is True


def test_switch_active_profile(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    id_a = upsert_ssh_host_profile(
        db_path,
        SshHostProfile(
            profile_name="printer-a",
            host="a.local",
            username="pi",
            remote_config_dir="/a/config",
            moonraker_url="http://a.local:7125",
            is_active=True,
        ),
    )
    id_b = upsert_ssh_host_profile(
        db_path,
        SshHostProfile(
            profile_name="printer-b",
            host="b.local",
            username="pi",
            remote_config_dir="/b/config",
            moonraker_url="http://b.local:7125",
            is_active=False,
        ),
    )

    assert set_active_ssh_host_profile(db_path, id_b) is True
    assert set_active_ssh_host_profile(db_path, id_a + id_b + 999) is False

    active = get_active_ssh_host_profile(db_path)
    assert active is not None
    assert active.id == id_b
    assert active.profile_name == "printer-b"


def test_credential_backend_and_fallback_storage(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    set_credential_backend(
        db_path,
        credential_ref="ssh:lab:password",
        secret_type="password",
        backend="db_fallback",
    )
    set_fallback_secret(
        db_path,
        credential_ref="ssh:lab:password",
        secret_value="super-secret",
    )

    assert get_credential_backend(db_path, "ssh:lab:password") == "db_fallback"
    assert get_fallback_secret(db_path, "ssh:lab:password") == "super-secret"
