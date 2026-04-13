from pathlib import Path

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
    assert config.developer is False
    assert config.printer_profile_prompt_required is True
    assert not (tmp_path / "klippervault.cfg").exists()


def test_load_or_create_migrates_legacy_cfg_once(tmp_path: Path) -> None:
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

    migrated = load_or_create(tmp_path, db_path)
    assert migrated.version_history_size == 7
    assert migrated.port == 10100
    assert migrated.ui_language == "de"
    assert migrated.printer_vendor == "RatRig"
    assert migrated.printer_model == "V-Core 3"
    assert migrated.online_update_repo_url == "https://github.com/example/macros"
    assert migrated.online_update_manifest_path == "vault/manifest.json"
    assert migrated.online_update_ref == "stable"
    assert migrated.developer is True

    cfg_path.unlink()
    reloaded = load_or_create(tmp_path, db_path)
    assert reloaded.version_history_size == 7
    assert reloaded.port == 10100
    assert reloaded.ui_language == "de"
    assert reloaded.developer is True


def test_load_or_create_applies_clamps_and_fallbacks(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    cfg_path = tmp_path / "klippervault.cfg"
    cfg_path.write_text(
        "[vault]\nversion_history_size: 0\nport: 70000\nui_language: DE\n",
        encoding="utf-8",
    )

    config = load_or_create(tmp_path, db_path)

    assert config.version_history_size == 1
    assert config.port == 10090
    assert config.ui_language == "de"
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
            developer=True,
        ),
        db_path,
    )

    config = load_or_create(tmp_path, db_path)
    assert config.version_history_size == 9
    assert config.port == 12000
    assert config.ui_language == "fr"
    assert config.printer_vendor == "Voron"
    assert config.printer_model == "Trident"
    assert config.online_update_repo_url == "https://github.com/example/repo"
    assert config.online_update_manifest_path == "updates/custom.json"
    assert config.online_update_ref == "dev"
    assert config.developer is True
    assert config.runtime_mode == "off_printer"


def test_default_db_path_is_stable_when_not_provided(tmp_path: Path) -> None:
    save(tmp_path, VaultConfig(version_history_size=11))
    config = load_or_create(tmp_path)
    assert config.version_history_size == 11
