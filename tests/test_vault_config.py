from pathlib import Path

from klipper_vault_config import load_or_create


def test_load_or_create_writes_defaults_when_missing(tmp_path: Path) -> None:
    config = load_or_create(tmp_path)
    cfg_path = tmp_path / "klippervault.cfg"

    assert cfg_path.exists()
    assert config.version_history_size == 5
    assert config.port == 10090
    assert config.ui_language == "en"


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