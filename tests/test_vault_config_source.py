from pathlib import Path
from typing import cast

import pytest

from klipper_vault_config_source import LocalConfigSource, SshConfigSource
from klipper_vault_ssh_transport import SshTransport


class _FakeTransport:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []
        self.removes: list[str] = []

    @staticmethod
    def _safe_remote_path(remote_path: str) -> str:
        path = str(remote_path or "").strip()
        if not path.startswith("/"):
            path = "/" + path
        return path

    def list_cfg_files(self, remote_config_dir: str) -> list[str]:
        assert remote_config_dir == "/remote/config"
        return [
            "/remote/config/printer.cfg",
            "/remote/config/macros/toolhead.cfg",
        ]

    def read_text_file(self, remote_path: str) -> str:
        return f"read:{remote_path}"

    def write_text_file_atomic(self, remote_path: str, text: str) -> None:
        self.writes.append((remote_path, text))

    def remove_file(self, remote_path: str) -> bool:
        self.removes.append(remote_path)
        return True


def test_local_config_source_list_read_write_remove(tmp_path: Path) -> None:
    root = tmp_path / "cfg"
    (root / "printer.cfg").parent.mkdir(parents=True, exist_ok=True)
    (root / "printer.cfg").write_text("[include macros/*.cfg]\n", encoding="utf-8")
    (root / "macros").mkdir(parents=True, exist_ok=True)
    (root / "macros" / "a.cfg").write_text("[gcode_macro A]\n", encoding="utf-8")

    source = LocalConfigSource(root_dir=root)

    assert source.list_cfg_files() == ["macros/a.cfg", "printer.cfg"]
    assert "include" in source.read_text("printer.cfg")

    source.write_text("macros/b.cfg", "[gcode_macro B]\n")
    assert (root / "macros" / "b.cfg").exists()

    assert source.remove("macros/b.cfg") is True
    assert source.remove("macros/b.cfg") is False


def test_local_config_source_rejects_path_escape(tmp_path: Path) -> None:
    source = LocalConfigSource(root_dir=tmp_path)
    with pytest.raises(ValueError):
        source.read_text("../escape.cfg")


def test_ssh_config_source_relative_path_mapping() -> None:
    transport = _FakeTransport()
    source = SshConfigSource(transport=cast(SshTransport, transport), remote_root="/remote/config")

    assert source.list_cfg_files() == ["macros/toolhead.cfg", "printer.cfg"]
    assert source.read_text("printer.cfg") == "read:/remote/config/printer.cfg"

    source.write_text("macros/new.cfg", "[gcode_macro NEW]\n")
    assert transport.writes == [("/remote/config/macros/new.cfg", "[gcode_macro NEW]\n")]

    assert source.remove("printer.cfg") is True
    assert transport.removes == ["/remote/config/printer.cfg"]


def test_ssh_config_source_rejects_path_escape() -> None:
    transport = _FakeTransport()
    source = SshConfigSource(transport=cast(SshTransport, transport), remote_root="/remote/config")

    with pytest.raises(ValueError):
        source.read_text("../escape.cfg")
