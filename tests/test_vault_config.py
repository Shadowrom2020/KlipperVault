from pathlib import Path

from klipper_vault_config import VaultConfig, load_or_create, save


def test_load_or_create_writes_defaults_when_missing(tmp_path: Path) -> None:
    config = load_or_create(tmp_path)
    cfg_path = tmp_path / "klippervault.cfg"

    assert cfg_path.exists()
    assert config.version_history_size == 5
    assert config.port == 10090
    assert config.ui_language == "en"
    assert config.printer_vendor == ""
    assert config.printer_model == ""
    assert config.online_update_repo_url == "https://github.com/Shadowrom2020/KlipperVault-Online-Updates"
    assert config.online_update_manifest_path == "updates/manifest.json"
    assert config.online_update_ref == "main"
    assert config.developer is False
    assert config.printer_profile_prompt_required is True


def test_load_or_create_applies_clamps_and_fallbacks(tmp_path: Path) -> None:
    cfg_path = tmp_path / "klippervault.cfg"
    cfg_path.write_text(
        "[vault]\nversion_history_size: 0\nport: 70000\nui_language: DE\n",
        encoding="utf-8",
    )

    config = load_or_create(tmp_path)

    assert config.version_history_size == 1
    assert config.port == 10090
    assert config.ui_language == "de"
    assert config.printer_vendor == ""
    assert config.printer_model == ""
    assert config.online_update_repo_url == "https://github.com/Shadowrom2020/KlipperVault-Online-Updates"
    assert config.online_update_manifest_path == "updates/manifest.json"
    assert config.online_update_ref == "main"
    assert config.printer_profile_prompt_required is True
    cfg_text = cfg_path.read_text(encoding="utf-8")
    assert "online_update_repo_url: https://github.com/Shadowrom2020/KlipperVault-Online-Updates" in cfg_text
    assert "online_update_manifest_path: updates/manifest.json" in cfg_text
    assert "online_update_ref: main" in cfg_text
    assert "developer: false" in cfg_text


def test_load_or_create_reads_printer_identity_fields(tmp_path: Path) -> None:
    cfg_path = tmp_path / "klippervault.cfg"
    cfg_path.write_text(
        "[vault]\nprinter_vendor: Voron\nprinter_model: Trident\n",
        encoding="utf-8",
    )

    config = load_or_create(tmp_path)

    assert config.printer_vendor == "Voron"
    assert config.printer_model == "Trident"
    assert config.printer_profile_prompt_required is False


def test_save_persists_printer_identity_fields(tmp_path: Path) -> None:
    save(
        tmp_path,
        VaultConfig(
            version_history_size=7,
            port=10100,
            ui_language="de",
            printer_vendor="RatRig",
            printer_model="V-Core 3",
            online_update_repo_url="https://github.com/example/macros",
            online_update_manifest_path="vault/manifest.json",
            online_update_ref="stable",
        ),
    )

    config = load_or_create(tmp_path)

    assert config.version_history_size == 7
    assert config.port == 10100
    assert config.ui_language == "de"
    assert config.printer_vendor == "RatRig"
    assert config.printer_model == "V-Core 3"
    assert config.online_update_repo_url == "https://github.com/example/macros"
    assert config.online_update_manifest_path == "vault/manifest.json"
    assert config.online_update_ref == "stable"
    assert config.printer_profile_prompt_required is False


def test_load_or_create_marks_upgrade_configs_without_identity_as_prompt_required(tmp_path: Path) -> None:
    cfg_path = tmp_path / "klippervault.cfg"
    cfg_path.write_text(
        "[vault]\nversion_history_size: 5\nport: 10090\nui_language: en\n",
        encoding="utf-8",
    )

    config = load_or_create(tmp_path)

    assert config.printer_vendor == ""
    assert config.printer_model == ""
    cfg_text = cfg_path.read_text(encoding="utf-8")
    assert "online_update_repo_url: https://github.com/Shadowrom2020/KlipperVault-Online-Updates" in cfg_text
    assert "online_update_manifest_path: updates/manifest.json" in cfg_text
    assert "online_update_ref: main" in cfg_text
    assert "developer: false" in cfg_text
    assert config.printer_profile_prompt_required is True


def test_load_or_create_detects_freedi_identity_from_freedi_cfg(tmp_path: Path) -> None:
    (tmp_path / "freedi.cfg").write_text(
        "# External printer profile\nprinter_model: Moonraker X\n",
        encoding="utf-8",
    )

    config = load_or_create(tmp_path)

    assert config.printer_vendor == "freedi"
    assert config.printer_model == "Moonraker X"
    assert config.printer_profile_prompt_required is False
    assert "printer_vendor: freedi" in (tmp_path / "klippervault.cfg").read_text(encoding="utf-8")
    assert "printer_model: Moonraker X" in (tmp_path / "klippervault.cfg").read_text(encoding="utf-8")


def test_load_or_create_prefers_stored_identity_over_freedi_detection(tmp_path: Path) -> None:
    cfg_path = tmp_path / "klippervault.cfg"
    cfg_path.write_text(
        "[vault]\nprinter_vendor: Voron\nprinter_model: Trident\n",
        encoding="utf-8",
    )
    (tmp_path / "freedi.cfg").write_text(
        "printer_model: Moonraker X\n",
        encoding="utf-8",
    )

    config = load_or_create(tmp_path)

    assert config.printer_vendor == "Voron"
    assert config.printer_model == "Trident"
    assert config.printer_profile_prompt_required is False


def test_load_or_create_reads_developer_mode(tmp_path: Path) -> None:
    cfg_path = tmp_path / "klippervault.cfg"
    cfg_path.write_text(
        "[vault]\ndeveloper: true\n",
        encoding="utf-8",
    )

    config = load_or_create(tmp_path)

    assert config.developer is True


def test_save_persists_developer_mode(tmp_path: Path) -> None:
    save(
        tmp_path,
        VaultConfig(developer=True),
    )
    cfg_path = tmp_path / "klippervault.cfg"

    assert "developer: true" in cfg_path.read_text(encoding="utf-8")

    reloaded_config = load_or_create(tmp_path)
    assert reloaded_config.developer is True


def test_load_or_create_backfills_all_persisted_config_keys(tmp_path: Path) -> None:
    cfg_path = tmp_path / "klippervault.cfg"
    cfg_path.write_text(
        "[vault]\nport: 10090\n",
        encoding="utf-8",
    )

    load_or_create(tmp_path)

    cfg_text = cfg_path.read_text(encoding="utf-8")
    assert "version_history_size: 5" in cfg_text
    assert "ui_language: en" in cfg_text
    assert "printer_vendor:" in cfg_text
    assert "printer_model:" in cfg_text
    assert "online_update_repo_url: https://github.com/Shadowrom2020/KlipperVault-Online-Updates" in cfg_text
    assert "online_update_manifest_path: updates/manifest.json" in cfg_text
    assert "online_update_ref: main" in cfg_text
    assert "developer: false" in cfg_text