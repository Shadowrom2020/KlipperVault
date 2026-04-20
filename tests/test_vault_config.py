from pathlib import Path
import sqlite3

from klipper_vault_config import VaultConfig, load_or_create, save


def test_load_or_create_bootstraps_defaults_in_db(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    config = load_or_create(tmp_path, db_path)

    assert config.version_history_size == 5
    assert config.port == 10090
    assert config.runtime_mode == "off_printer"
    assert config.ui_language == "en"
    assert config.printer_vendor == ""
    assert config.printer_model == ""
    assert config.online_update_repo_url == "https://github.com/Shadowrom2020/KlipperVault-Online-Updates"
    assert config.online_update_manifest_path == "updates/manifest.json"
    assert config.online_update_ref == "main"
    assert config.theme_mode == "auto"
    assert config.developer is False
    assert config.printer_profile_prompt_required is True
    assert not (tmp_path / "klippervault.cfg").exists()


def test_load_or_create_ignores_legacy_cfg_file(tmp_path: Path) -> None:
    cfg_path = tmp_path / "klippervault.cfg"
    db_path = tmp_path / "vault.db"
    cfg_path.write_text(
        """
[vault]
version_history_size: 7
port: 10100
runtime_mode: off_printer
ui_language: de
printer_vendor: RatRig
printer_model: V-Core 3
online_update_repo_url: https://github.com/example/macros
online_update_manifest_path: vault/manifest.json
online_update_ref: stable
developer: true
""".strip()
        + "\n",
        encoding="utf-8",
    )

    loaded = load_or_create(tmp_path, db_path)
    assert loaded.version_history_size == 5
    assert loaded.port == 10090
    assert loaded.ui_language == "en"
    assert loaded.printer_vendor == ""
    assert loaded.printer_model == ""
    assert loaded.theme_mode == "auto"
    assert loaded.developer is False


def test_load_or_create_applies_clamps_and_fallbacks(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE vault_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL)")
        conn.execute("INSERT INTO vault_settings(key, value, updated_at) VALUES ('version_history_size', '0', 1)")
        conn.execute("INSERT INTO vault_settings(key, value, updated_at) VALUES ('port', '70000', 1)")
        conn.execute("INSERT INTO vault_settings(key, value, updated_at) VALUES ('ui_language', 'DE', 1)")
        conn.execute("INSERT INTO vault_settings(key, value, updated_at) VALUES ('theme_mode', 'invalid', 1)")
        conn.commit()

    config = load_or_create(tmp_path, db_path)

    assert config.version_history_size == 1
    assert config.port == 10090
    assert config.ui_language == "de"
    assert config.theme_mode == "auto"
    assert config.runtime_mode == "off_printer"


def test_save_persists_settings_in_db(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    save(
        tmp_path,
        VaultConfig(
            version_history_size=9,
            port=12000,
            runtime_mode="off_printer",
            ui_language="fr",
            printer_vendor="Voron",
            printer_model="Trident",
            online_update_repo_url="https://github.com/example/repo",
            online_update_manifest_path="updates/custom.json",
            online_update_ref="dev",
            theme_mode="dark",
            developer=True,
        ),
        db_path,
    )

    config = load_or_create(tmp_path, db_path)
    assert config.version_history_size == 9
    assert config.port == 10090
    assert config.ui_language == "fr"
    assert config.printer_vendor == "Voron"
    assert config.printer_model == "Trident"
    assert config.online_update_repo_url == "https://github.com/example/repo"
    assert config.online_update_manifest_path == "updates/custom.json"
    assert config.online_update_ref == "dev"
    assert config.theme_mode == "dark"
    assert config.developer is True
    assert config.runtime_mode == "off_printer"


def test_default_db_path_is_stable_when_not_provided(tmp_path: Path) -> None:
    save(tmp_path, VaultConfig(version_history_size=11))
    config = load_or_create(tmp_path)
    assert config.version_history_size == 11
