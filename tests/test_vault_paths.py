import importlib
import os


def _reload_paths_module():
    module = importlib.import_module("klipper_vault_paths")
    return importlib.reload(module)


def test_standard_defaults_use_user_config_and_share_dirs(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    module = _reload_paths_module()

    expected_config_suffix = os.path.join(".config", "klippervault")
    expected_db_suffix = os.path.join(".local", "share", "klippervault", "klipper_macros.db")
    assert module.DEFAULT_CONFIG_DIR.endswith(expected_config_suffix)
    assert module.DEFAULT_DB_PATH.endswith(expected_db_suffix)


def test_runtime_mode_is_fixed_to_standard(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")

    module = _reload_paths_module()

    assert module._runtime_mode() == "standard"


def test_xdg_overrides_are_used_on_linux(monkeypatch, tmp_path):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    xdg_config = tmp_path / "xdg-config"
    xdg_data = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))

    module = _reload_paths_module()

    assert module.DEFAULT_CONFIG_DIR == str((xdg_config / "klippervault").resolve())
    assert module.DEFAULT_DB_PATH == str((xdg_data / "klippervault" / "klipper_macros.db").resolve())


def test_legacy_klippervault_env_overrides_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setenv("KLIPPERVAULT_CONFIG_DIR", str(tmp_path / "legacy-config"))
    monkeypatch.setenv("KLIPPERVAULT_DB_PATH", str(tmp_path / "legacy-db" / "legacy.db"))

    module = _reload_paths_module()

    assert module.DEFAULT_CONFIG_DIR.endswith(os.path.join(".config", "klippervault"))
    assert module.DEFAULT_DB_PATH.endswith(os.path.join(".local", "share", "klippervault", "klipper_macros.db"))


def test_windows_standalone_frozen_uses_executable_local_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    exe_path = tmp_path / "KlipperVault.exe"
    exe_path.write_text("stub", encoding="utf-8")
    monkeypatch.setattr("sys.executable", str(exe_path))
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    module = _reload_paths_module()

    assert module.DEFAULT_CONFIG_DIR == str((tmp_path / "data" / "config").resolve())
    assert module.DEFAULT_DB_PATH == str((tmp_path / "data" / "klipper_macros.db").resolve())


def test_windows_installer_marker_keeps_appdata_behavior(monkeypatch, tmp_path):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    exe_path = tmp_path / "KlipperVault.exe"
    exe_path.write_text("stub", encoding="utf-8")
    (tmp_path / ".klippervault_installed").write_text("installed\n", encoding="utf-8")
    appdata = tmp_path / "Roaming"
    localappdata = tmp_path / "Local"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    monkeypatch.setattr("sys.executable", str(exe_path))
    monkeypatch.setattr("sys.frozen", True, raising=False)

    module = _reload_paths_module()

    assert module.DEFAULT_CONFIG_DIR == str((appdata / "KlipperVault").resolve())
    assert module.DEFAULT_DB_PATH == str((localappdata / "KlipperVault" / "klipper_macros.db").resolve())


def test_windows_non_frozen_keeps_appdata_behavior(monkeypatch, tmp_path):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    appdata = tmp_path / "Roaming"
    localappdata = tmp_path / "Local"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    monkeypatch.setattr("sys.frozen", False, raising=False)

    module = _reload_paths_module()

    assert module.DEFAULT_CONFIG_DIR == str((appdata / "KlipperVault").resolve())
    assert module.DEFAULT_DB_PATH == str((localappdata / "KlipperVault" / "klipper_macros.db").resolve())
