import json
from pathlib import Path
from unittest.mock import patch

from klipper_macro_online_update import (
    check_online_macro_updates,
    import_online_macro_updates,
)


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def _fake_urlopen_factory(url_to_payload: dict[str, object]):
    def _fake_urlopen(request, timeout=10.0):
        url = request.full_url
        payload = url_to_payload.get(url)
        if payload is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return _FakeResponse(payload)

    return _fake_urlopen


def test_online_update_check_and_import_cycle(tmp_path: Path) -> None:
    db_path = tmp_path / "db" / "macros.db"
    repo_url = "https://github.com/example/klipper-macros"
    repo_ref = "main"
    manifest_path = "updates/manifest.json"

    manifest_url = (
        "https://raw.githubusercontent.com/example/klipper-macros/main/updates/manifest.json"
    )
    macro_url = (
        "https://raw.githubusercontent.com/example/klipper-macros/main/voron/trident/PRINT_START.json"
    )

    section_text = (
        "[gcode_macro PRINT_START]\n"
        "description: Start print\n"
        "gcode:\n"
        "  G28\n"
        "  M117 Ready\n"
    )

    url_to_payload = {
        manifest_url: {
            "format": "klippervault.online.v1",
            "macros": [
                {
                    "vendor": "voron",
                    "model": "trident",
                    "macro_name": "PRINT_START",
                    "path": "voron/trident/PRINT_START.json",
                    "version": "2026-04-07",
                }
            ],
        },
        macro_url: {
            "macro_name": "PRINT_START",
            "source_file_path": "macros.cfg",
            "section_text": section_text,
        },
    }

    with patch("klipper_macro_online_update.urlopen", _fake_urlopen_factory(url_to_payload)):
        check_result = check_online_macro_updates(
            db_path,
            repo_url=repo_url,
            manifest_path=manifest_path,
            repo_ref=repo_ref,
            source_vendor="voron",
            source_model="trident",
        )

    assert check_result["checked"] == 1
    assert check_result["changed"] == 1
    updates = check_result["updates"]
    assert isinstance(updates, list)
    assert len(updates) == 1

    import_result = import_online_macro_updates(
        db_path,
        updates=updates,
        repo_url=repo_url,
        repo_ref=repo_ref,
    )
    assert import_result["imported"] == 1

    with patch("klipper_macro_online_update.urlopen", _fake_urlopen_factory(url_to_payload)):
        second_check = check_online_macro_updates(
            db_path,
            repo_url=repo_url,
            manifest_path=manifest_path,
            repo_ref=repo_ref,
            source_vendor="voron",
            source_model="trident",
        )

    assert second_check["checked"] == 1
    assert second_check["changed"] == 0
    assert second_check["unchanged"] == 1
