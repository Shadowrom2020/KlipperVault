import importlib
import os


def _reload_paths_module():
    module = importlib.import_module("klipper_vault_paths")
    return importlib.reload(module)


def test_off_printer_defaults_use_user_config_and_share_dirs(monkeypatch):
    monkeypatch.delenv("KLIPPERVAULT_CONFIG_DIR", raising=False)
    monkeypatch.delenv("KLIPPERVAULT_DB_PATH", raising=False)
    monkeypatch.setenv("KLIPPERVAULT_RUNTIME_MODE", "off_printer")

    module = _reload_paths_module()

    expected_config_suffix = os.path.join(".config", "klippervault")
    expected_db_suffix = os.path.join(".local", "share", "klippervault", "klipper_macros.db")
    assert module.DEFAULT_CONFIG_DIR.endswith(expected_config_suffix)
    assert module.DEFAULT_DB_PATH.endswith(expected_db_suffix)


def test_on_printer_defaults_use_printer_data_paths(monkeypatch):
    monkeypatch.delenv("KLIPPERVAULT_CONFIG_DIR", raising=False)
    monkeypatch.delenv("KLIPPERVAULT_DB_PATH", raising=False)
    monkeypatch.setenv("KLIPPERVAULT_RUNTIME_MODE", "on_printer")

    module = _reload_paths_module()

    assert module.DEFAULT_CONFIG_DIR.endswith(os.path.join("printer_data", "config"))
    assert module.DEFAULT_DB_PATH.endswith(os.path.join("printer_data", "db", "klipper_macros.db"))


def test_env_overrides_take_priority(monkeypatch, tmp_path):
    custom_config = tmp_path / "custom-config"
    custom_db = tmp_path / "custom-db" / "vault.db"
    monkeypatch.setenv("KLIPPERVAULT_RUNTIME_MODE", "off_printer")
    monkeypatch.setenv("KLIPPERVAULT_CONFIG_DIR", str(custom_config))
    monkeypatch.setenv("KLIPPERVAULT_DB_PATH", str(custom_db))

    module = _reload_paths_module()

    assert module.DEFAULT_CONFIG_DIR == str(custom_config.resolve())
    assert module.DEFAULT_DB_PATH == str(custom_db.resolve())
