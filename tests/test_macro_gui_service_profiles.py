from pathlib import Path
from unittest.mock import patch

from klipper_macro_gui_service import MacroGuiService


def _service(tmp_path: Path) -> MacroGuiService:
    return MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
    )


def test_save_profile_and_list_profiles(tmp_path: Path) -> None:
    service = _service(tmp_path)

    result = service.save_ssh_profile(
        profile_name="Office Printer",
        host="printer.local",
        username="pi",
        remote_config_dir="/home/pi/printer_data/config",
        moonraker_url="http://printer.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    assert result["ok"] is True
    assert result["credential_ref"] == "ssh:office-printer:password"
    assert result["secret_backend"] in {"os_keyring", "db_fallback"}

    profiles = service.list_ssh_profiles()
    assert len(profiles) == 1
    assert profiles[0]["profile_name"] == "Office Printer"
    assert profiles[0]["is_active"] is True
    assert profiles[0]["has_secret"] is True
    assert profiles[0]["secret_backend"] in {"os_keyring", "db_fallback", ""}


def test_activate_profile_switches_active_row(tmp_path: Path) -> None:
    service = _service(tmp_path)

    a = service.save_ssh_profile(
        profile_name="A",
        host="a.local",
        username="pi",
        remote_config_dir="/a/config",
        moonraker_url="http://a.local:7125",
        auth_mode="key",
    )
    b = service.save_ssh_profile(
        profile_name="B",
        host="b.local",
        username="pi",
        remote_config_dir="/b/config",
        moonraker_url="http://b.local:7125",
        auth_mode="key",
        is_active=True,
    )

    assert service.activate_ssh_profile(int(a["profile_id"]))["ok"] is True
    active = service.get_active_ssh_profile()
    assert active is not None
    assert active["profile_name"] == "A"

    assert service.activate_ssh_profile(int(b["profile_id"]))["ok"] is True
    active = service.get_active_ssh_profile()
    assert active is not None
    assert active["profile_name"] == "B"


def test_resolve_secret_metadata(tmp_path: Path) -> None:
    service = _service(tmp_path)

    save_result = service.save_ssh_profile(
        profile_name="Lab",
        host="lab.local",
        username="pi",
        remote_config_dir="/lab/config",
        moonraker_url="http://lab.local:7125",
        auth_mode="password",
        secret_value="abc",
    )

    resolved = service.resolve_ssh_secret(str(save_result["credential_ref"]))
    assert resolved["ok"] is True
    assert resolved["has_secret"] is True
    assert resolved["secret_value"] == "abc"


def test_save_profile_rejects_invalid_auth_mode(tmp_path: Path) -> None:
    service = _service(tmp_path)

    try:
        service.save_ssh_profile(
            profile_name="Bad",
            host="bad.local",
            username="pi",
            remote_config_dir="/bad/config",
            moonraker_url="http://bad.local:7125",
            auth_mode="token",
        )
    except ValueError as exc:
        assert "auth_mode" in str(exc)
    else:
        raise AssertionError("Expected ValueError for invalid auth_mode")


def test_off_printer_mode_uses_active_profile_moonraker_url(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://remote.local:8125",
        auth_mode="key",
        is_active=True,
    )

    off_printer_service = MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
        runtime_mode="off_printer",
        moonraker_base_url="http://127.0.0.1:7125",
    )

    url = off_printer_service._moonraker_url("/printer/restart")
    assert url.startswith("http://remote.local:8125")


def test_off_printer_rewrites_localhost_moonraker_url_to_remote_host(tmp_path: Path) -> None:
    service = MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
        runtime_mode="off_printer",
        moonraker_base_url="http://127.0.0.1:7125",
    )

    service.save_ssh_profile(
        profile_name="Remote",
        host="192.168.0.25",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://127.0.0.1:7125",
        auth_mode="key",
        is_active=True,
    )

    url = service._moonraker_url("/printer/restart")
    assert url.startswith("http://192.168.0.25:7125")


def test_test_active_ssh_connection_uses_profile_and_secret(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://remote.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    with patch("klipper_macro_gui_service.SshTransport.test_connection", return_value={
        "ok": True,
        "output": "klippervault-ssh-ok",
        "error": "",
        "elapsed_ms": 42,
    }):
        result = service.test_active_ssh_connection()

    assert result["ok"] is True
    assert result["profile_name"] == "Remote"
    assert result["host"] == "remote.local"
    assert result["elapsed_ms"] == 42


def test_list_active_remote_cfg_files_returns_count_and_files(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://remote.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    with patch(
        "klipper_macro_gui_service.MacroGuiService.resolve_ssh_secret",
        return_value={"ok": True, "has_secret": True, "secret_value": "pw123", "backend": "db_fallback"},
    ), patch(
        "klipper_macro_gui_service.SshTransport.list_cfg_files",
        return_value=["/remote/config/printer.cfg", "/remote/config/macros.cfg"],
    ):
        result = service.list_active_remote_cfg_files()

    assert result["ok"] is True
    assert result["count"] == 2
    assert result["files"] == ["/remote/config/printer.cfg", "/remote/config/macros.cfg"]


def test_off_printer_index_syncs_remote_before_index(tmp_path: Path) -> None:
    service = MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
        runtime_mode="off_printer",
    )
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://remote.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    with (
        patch(
            "klipper_macro_gui_service.SshTransport.list_cfg_files",
            return_value=["/remote/config/printer.cfg", "/remote/config/macros.cfg"],
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.read_text_file",
            side_effect=["[include macros.cfg]\n", "[gcode_macro TEST]\ngcode:\n  RESPOND MSG=ok\n"],
        ),
    ):
        result = service.index()

    assert result["cfg_files_scanned"] == 2
    assert "remote_sync" in result
    assert result["remote_sync"]["synced_files"] == 2
    runtime_dir = Path(str(result.get("runtime_config_dir", "")))
    assert (runtime_dir / "printer.cfg").exists()
    assert (runtime_dir / "macros.cfg").exists()


def test_off_printer_cfg_loading_overview_reads_active_remote_source(tmp_path: Path) -> None:
    service = MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
        runtime_mode="off_printer",
    )
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://remote.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    with (
        patch(
            "klipper_macro_gui_service.SshTransport.list_cfg_files",
            return_value=["/remote/config/printer.cfg", "/remote/config/macros.cfg"],
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.read_text_file",
            side_effect=lambda remote_path: {
                "/remote/config/printer.cfg": "[include macros.cfg]\n",
                "/remote/config/macros.cfg": "[gcode_macro TEST]\ngcode:\n  RESPOND MSG=ok\n",
            }[str(remote_path)],
        ),
    ):
        overview = service.load_cfg_loading_overview()

    assert [row["file_path"] for row in overview["klipper_order"]] == ["printer.cfg", "macros.cfg"]
    assert overview["klipper_macro_count"] == 1


def test_off_printer_index_syncs_with_tilde_remote_root(tmp_path: Path) -> None:
    service = MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
        runtime_mode="off_printer",
    )
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="~/printer_data/config",
        moonraker_url="http://remote.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    with (
        patch(
            "klipper_macro_gui_service.SshTransport.list_cfg_files",
            return_value=["/home/pi/printer_data/config/printer.cfg"],
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.read_text_file",
            return_value="[include macros.cfg]\n",
        ),
        patch(
            "klipper_macro_gui_service.run_indexing_from_source",
            return_value={"cfg_files_scanned": 1, "macros_inserted": 0, "macros_unchanged": 0},
        ),
    ):
        result = service.index()

    assert result["cfg_files_scanned"] == 1
    assert "remote_sync" in result
    assert result["remote_sync"]["synced_files"] == 1
    runtime_dir = Path(str(result.get("runtime_config_dir", "")))
    assert (runtime_dir / "printer.cfg").exists()


def test_off_printer_save_pushes_local_file_to_remote(tmp_path: Path) -> None:
    service = MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
        runtime_mode="off_printer",
    )
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://remote.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    with (
        patch(
            "klipper_macro_gui_service.save_macro_edit",
            return_value={"file_path": "macros.cfg", "macro_name": "TEST", "operation": "replaced"},
        ),
        patch("klipper_macro_gui_service.SshTransport.write_text_file_atomic") as write_mock,
    ):
        runtime_dir = service.get_runtime_config_dir()
        (runtime_dir / "macros.cfg").parent.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "macros.cfg").write_text("[gcode_macro TEST]\n", encoding="utf-8")
        result = service.save_macro_editor_text("macros.cfg", "TEST", "[gcode_macro TEST]\n")

    write_mock.assert_called_once()
    called_remote_path = str(write_mock.call_args[0][0])
    assert called_remote_path == "/remote/config/macros.cfg"
    assert result["remote_synced"] is True
    assert result["remote_path"] == "/remote/config/macros.cfg"


def test_delete_ssh_profile_removes_row(tmp_path: Path) -> None:
    service = _service(tmp_path)
    saved = service.save_ssh_profile(
        profile_name="ToDelete",
        host="delete.local",
        username="pi",
        remote_config_dir="/delete/config",
        moonraker_url="http://delete.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=False,
    )

    deleted = service.delete_ssh_profile(int(saved["profile_id"]))
    assert deleted["ok"] is True

    profiles = service.list_ssh_profiles()
    assert profiles == []


def test_delete_active_ssh_profile_clears_active_state(tmp_path: Path) -> None:
    service = _service(tmp_path)
    saved = service.save_ssh_profile(
        profile_name="ActiveDelete",
        host="active.local",
        username="pi",
        remote_config_dir="/active/config",
        moonraker_url="http://active.local:7125",
        auth_mode="key",
        is_active=True,
    )

    deleted = service.delete_ssh_profile(int(saved["profile_id"]))
    assert deleted["ok"] is True
    assert deleted["was_active"] is True
    assert service.get_active_ssh_profile() is None


def test_ensure_printer_profile_for_ssh_profile_creates_and_activates(tmp_path: Path) -> None:
    service = _service(tmp_path)
    saved = service.save_ssh_profile(
        profile_name="Workshop",
        host="workshop.local",
        username="pi",
        remote_config_dir="~/printer_data/config",
        moonraker_url="http://workshop.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    result = service.ensure_printer_profile_for_ssh_profile(
        ssh_profile_id=int(saved["profile_id"]),
        profile_name="Workshop",
        activate=True,
    )
    assert result["ok"] is True

    active = service.get_active_printer_profile()
    assert active is not None
    assert active["ssh_profile_id"] == int(saved["profile_id"])


def test_save_active_ssh_profile_auto_creates_active_printer_profile(tmp_path: Path) -> None:
    service = _service(tmp_path)

    saved = service.save_ssh_profile(
        profile_name="AutoMapped",
        host="auto.local",
        username="pi",
        remote_config_dir="~/printer_data/config",
        moonraker_url="http://auto.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    assert saved["ok"] is True
    active_printer = service.get_active_printer_profile()
    assert active_printer is not None
    assert active_printer["ssh_profile_id"] == int(saved["profile_id"])


def test_activate_ssh_profile_switches_active_printer_profile(tmp_path: Path) -> None:
    service = _service(tmp_path)

    left = service.save_ssh_profile(
        profile_name="Left",
        host="left.local",
        username="pi",
        remote_config_dir="~/printer_data/config",
        moonraker_url="http://left.local:7125",
        auth_mode="key",
        is_active=True,
    )
    right = service.save_ssh_profile(
        profile_name="Right",
        host="right.local",
        username="pi",
        remote_config_dir="~/printer_data/config",
        moonraker_url="http://right.local:7125",
        auth_mode="key",
        is_active=False,
    )

    assert left["ok"] is True
    assert right["ok"] is True

    activated = service.activate_ssh_profile(int(right["profile_id"]))
    assert activated["ok"] is True

    active_printer = service.get_active_printer_profile()
    assert active_printer is not None
    assert active_printer["ssh_profile_id"] == int(right["profile_id"])


def test_off_printer_restore_backup_syncs_and_prunes_remote_cfg(tmp_path: Path) -> None:
    service = MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
        runtime_mode="off_printer",
    )
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://remote.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    runtime_dir = service.get_runtime_config_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "printer.cfg").write_text("[include macros.cfg]\n", encoding="utf-8")
    (runtime_dir / "macros.cfg").write_text("[gcode_macro TEST]\n", encoding="utf-8")

    with (
        patch(
            "klipper_macro_gui_service.restore_macro_backup",
                return_value={
                    "backup_id": 7,
                    "restored_cfg_files": 2,
                    "removed_cfg_files": 1,
                    "touched_cfg_files": ["printer.cfg", "macros.cfg"],
                },
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.list_cfg_files",
            return_value=[
                "/remote/config/printer.cfg",
                "/remote/config/macros.cfg",
                "/remote/config/obsolete.cfg",
            ],
        ),
        patch("klipper_macro_gui_service.SshTransport.write_text_file_atomic") as write_mock,
        patch("klipper_macro_gui_service.SshTransport.remove_file", return_value=True) as remove_mock,
    ):
        result = service.restore_backup(7)

    assert result["remote_synced"] is True
    remote_sync = result["remote_sync"]
    assert remote_sync["uploaded_files"] == 1
    assert remote_sync["blocked_files"] == 1
    assert remote_sync["blocked_by_protected_file"] is True
    assert write_mock.call_count == 1
    remove_mock.assert_not_called()


def test_off_printer_blocks_printer_cfg_macro_edit(tmp_path: Path) -> None:
    service = MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
        runtime_mode="off_printer",
    )
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://remote.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    try:
        service.save_macro_editor_text("printer.cfg", "TEST", "[gcode_macro TEST]\n")
    except ValueError as exc:
        assert "read-only" in str(exc).lower()
    else:
        raise AssertionError("Expected protected printer.cfg edit to be blocked")


def test_off_printer_duplicate_resolve_upload_error_includes_file_context(tmp_path: Path) -> None:
    service = MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
        runtime_mode="off_printer",
    )
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://remote.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    runtime_dir = service.get_runtime_config_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "macros.cfg").write_text("[gcode_macro TEST]\n", encoding="utf-8")

    with (
        patch(
            "klipper_macro_gui_service.resolve_duplicate_macros",
            return_value={"removed_sections": 1, "touched_files": ["macros.cfg"]},
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.write_text_file_atomic",
            side_effect=RuntimeError("Failure"),
        ),
    ):
        try:
            service.resolve_duplicates(
                keep_choices={"TEST": "macros.cfg"},
                duplicate_groups=[{"macro_name": "TEST", "entries": [{"file_path": "macros.cfg"}]}],
            )
        except RuntimeError as exc:
            text = str(exc)
            assert "macros.cfg" in text
            assert "Failure" in text
        else:
            raise AssertionError("Expected duplicate resolve upload to raise RuntimeError")


def test_off_printer_save_rejects_remote_conflict_after_sync(tmp_path: Path) -> None:
    service = MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
        runtime_mode="off_printer",
    )
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://remote.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    # Initial sync stores remote baseline checksum.
    with (
        patch(
            "klipper_macro_gui_service.SshTransport.list_cfg_files",
            return_value=["/remote/config/macros.cfg"],
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.read_text_file",
            return_value="[gcode_macro TEST]\ngcode:\n  RESPOND MSG=remote-old\n",
        ),
        patch(
            "klipper_macro_gui_service.run_indexing_from_source",
            return_value={"cfg_files_scanned": 1, "macros_inserted": 0, "macros_unchanged": 1},
        ),
    ):
        service.index()

    runtime_dir = service.get_runtime_config_dir()
    (runtime_dir / "macros.cfg").write_text("[gcode_macro TEST]\n", encoding="utf-8")

    with (
        patch(
            "klipper_macro_gui_service.save_macro_edit",
            return_value={"file_path": "macros.cfg", "macro_name": "TEST", "operation": "replaced"},
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.read_text_file",
            return_value="[gcode_macro TEST]\ngcode:\n  RESPOND MSG=remote-new\n",
        ),
    ):
        try:
            service.save_macro_editor_text("macros.cfg", "TEST", "[gcode_macro TEST]\n")
        except RuntimeError as exc:
            assert "Remote cfg conflict" in str(exc)
            assert "macros.cfg" in str(exc)
        else:
            raise AssertionError("Expected remote conflict to abort upload")


def test_off_printer_save_allows_upload_when_remote_unchanged(tmp_path: Path) -> None:
    service = MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
        runtime_mode="off_printer",
    )
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://remote.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    with (
        patch(
            "klipper_macro_gui_service.SshTransport.list_cfg_files",
            return_value=["/remote/config/macros.cfg"],
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.read_text_file",
            return_value="[gcode_macro TEST]\ngcode:\n  RESPOND MSG=remote-old\n",
        ),
        patch(
            "klipper_macro_gui_service.run_indexing_from_source",
            return_value={"cfg_files_scanned": 1, "macros_inserted": 0, "macros_unchanged": 1},
        ),
    ):
        service.index()

    runtime_dir = service.get_runtime_config_dir()
    (runtime_dir / "macros.cfg").write_text("[gcode_macro TEST]\n", encoding="utf-8")

    with (
        patch(
            "klipper_macro_gui_service.save_macro_edit",
            return_value={"file_path": "macros.cfg", "macro_name": "TEST", "operation": "replaced"},
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.read_text_file",
            return_value="[gcode_macro TEST]\ngcode:\n  RESPOND MSG=remote-old\n",
        ),
        patch("klipper_macro_gui_service.SshTransport.write_text_file_atomic") as write_mock,
    ):
        result = service.save_macro_editor_text("macros.cfg", "TEST", "[gcode_macro TEST]\n")

    write_mock.assert_called_once()
    assert result["remote_synced"] is True


def test_off_printer_tree_sync_rejects_remote_conflict_after_sync(tmp_path: Path) -> None:
    service = MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
        runtime_mode="off_printer",
    )
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://remote.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    with (
        patch(
            "klipper_macro_gui_service.SshTransport.list_cfg_files",
            return_value=["/remote/config/macros.cfg"],
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.read_text_file",
            return_value="[gcode_macro TEST]\ngcode:\n  RESPOND MSG=remote-old\n",
        ),
        patch(
            "klipper_macro_gui_service.run_indexing_from_source",
            return_value={"cfg_files_scanned": 1, "macros_inserted": 0, "macros_unchanged": 1},
        ),
    ):
        service.index()

    runtime_dir = service.get_runtime_config_dir()
    (runtime_dir / "macros.cfg").write_text("[gcode_macro TEST]\n", encoding="utf-8")

    with (
        patch(
            "klipper_macro_gui_service.SshTransport.read_text_file",
            return_value="[gcode_macro TEST]\ngcode:\n  RESPOND MSG=remote-new\n",
        ),
        patch("klipper_macro_gui_service.SshTransport.write_text_file_atomic") as write_mock,
    ):
        try:
            service._sync_local_cfg_tree_to_active_remote(prune_remote_missing=False)
        except RuntimeError as exc:
            assert "Remote cfg conflict" in str(exc)
            assert "macros.cfg" in str(exc)
        else:
            raise AssertionError("Expected tree sync conflict to abort upload")

    write_mock.assert_not_called()


def test_off_printer_tree_sync_rejects_new_remote_file_prune(tmp_path: Path) -> None:
    service = MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
        runtime_mode="off_printer",
    )
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://remote.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    with (
        patch(
            "klipper_macro_gui_service.SshTransport.list_cfg_files",
            return_value=["/remote/config/macros.cfg"],
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.read_text_file",
            return_value="[gcode_macro TEST]\ngcode:\n  RESPOND MSG=remote-old\n",
        ),
        patch(
            "klipper_macro_gui_service.run_indexing_from_source",
            return_value={"cfg_files_scanned": 1, "macros_inserted": 0, "macros_unchanged": 1},
        ),
    ):
        service.index()

    runtime_dir = service.get_runtime_config_dir()
    (runtime_dir / "macros.cfg").write_text("[gcode_macro TEST]\n", encoding="utf-8")

    with (
        patch(
            "klipper_macro_gui_service.SshTransport.list_cfg_files",
            return_value=["/remote/config/macros.cfg", "/remote/config/new.cfg"],
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.read_text_file",
            return_value="[gcode_macro TEST]\ngcode:\n  RESPOND MSG=remote-old\n",
        ),
        patch("klipper_macro_gui_service.SshTransport.write_text_file_atomic"),
        patch("klipper_macro_gui_service.SshTransport.remove_file") as remove_mock,
    ):
        try:
            service._sync_local_cfg_tree_to_active_remote(prune_remote_missing=True)
        except RuntimeError as exc:
            assert "appeared after last sync" in str(exc)
            assert "new.cfg" in str(exc)
        else:
            raise AssertionError("Expected tree prune conflict for new remote cfg file")

    remove_mock.assert_not_called()


def test_off_printer_resolve_duplicates_rejects_remote_conflict_after_sync(tmp_path: Path) -> None:
    service = MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
        runtime_mode="off_printer",
    )
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://remote.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    with (
        patch(
            "klipper_macro_gui_service.SshTransport.list_cfg_files",
            return_value=["/remote/config/macros.cfg"],
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.read_text_file",
            return_value="[gcode_macro TEST]\ngcode:\n  RESPOND MSG=remote-old\n",
        ),
        patch(
            "klipper_macro_gui_service.run_indexing_from_source",
            return_value={"cfg_files_scanned": 1, "macros_inserted": 0, "macros_unchanged": 1},
        ),
    ):
        service.index()

    runtime_dir = service.get_runtime_config_dir()
    (runtime_dir / "macros.cfg").write_text("[gcode_macro TEST]\n", encoding="utf-8")

    with (
        patch(
            "klipper_macro_gui_service.resolve_duplicate_macros",
            return_value={"removed_sections": 1, "touched_files": ["macros.cfg"]},
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.read_text_file",
            return_value="[gcode_macro TEST]\ngcode:\n  RESPOND MSG=remote-new\n",
        ),
        patch("klipper_macro_gui_service.SshTransport.write_text_file_atomic") as write_mock,
    ):
        try:
            service.resolve_duplicates(
                keep_choices={"TEST": "macros.cfg"},
                duplicate_groups=[{"macro_name": "TEST", "entries": [{"file_path": "macros.cfg"}]}],
            )
        except RuntimeError as exc:
            assert "Remote cfg conflict" in str(exc)
            assert "macros.cfg" in str(exc)
        else:
            raise AssertionError("Expected duplicate resolution to abort on remote conflict")

    write_mock.assert_not_called()


def test_off_printer_restore_backup_rejects_remote_conflict_after_sync(tmp_path: Path) -> None:
    service = MacroGuiService(
        db_path=tmp_path / "vault.db",
        config_dir=tmp_path / "config",
        version_history_size=5,
        runtime_mode="off_printer",
    )
    service.save_ssh_profile(
        profile_name="Remote",
        host="remote.local",
        username="pi",
        remote_config_dir="/remote/config",
        moonraker_url="http://remote.local:7125",
        auth_mode="password",
        secret_value="pw123",
        is_active=True,
    )

    with (
        patch(
            "klipper_macro_gui_service.SshTransport.list_cfg_files",
            return_value=["/remote/config/macros.cfg"],
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.read_text_file",
            return_value="[gcode_macro TEST]\ngcode:\n  RESPOND MSG=remote-old\n",
        ),
        patch(
            "klipper_macro_gui_service.run_indexing_from_source",
            return_value={"cfg_files_scanned": 1, "macros_inserted": 0, "macros_unchanged": 1},
        ),
    ):
        service.index()

    runtime_dir = service.get_runtime_config_dir()
    (runtime_dir / "macros.cfg").write_text("[gcode_macro TEST]\n", encoding="utf-8")

    with (
        patch(
            "klipper_macro_gui_service.restore_macro_backup",
            return_value={
                "backup_id": 7,
                "restored_cfg_files": 1,
                "removed_cfg_files": 0,
                "touched_cfg_files": ["macros.cfg"],
            },
        ),
        patch(
            "klipper_macro_gui_service.SshTransport.read_text_file",
            return_value="[gcode_macro TEST]\ngcode:\n  RESPOND MSG=remote-new\n",
        ),
        patch("klipper_macro_gui_service.SshTransport.write_text_file_atomic") as write_mock,
    ):
        try:
            service.restore_backup(7)
        except RuntimeError as exc:
            assert "Remote cfg conflict" in str(exc)
            assert "macros.cfg" in str(exc)
        else:
            raise AssertionError("Expected restore backup to abort on remote conflict")

    write_mock.assert_not_called()
