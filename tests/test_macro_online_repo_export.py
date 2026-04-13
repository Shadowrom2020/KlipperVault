import json
from pathlib import Path
from typing import cast
from unittest.mock import patch
import zipfile

from klipper_macro_indexer import run_indexing
from klipper_macro_online_repo_export import (
    build_online_update_repository_artifacts,
    export_online_update_repository_zip,
    _sha256,
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
    def _fake_urlopen(request, timeout=12.0):
        url = request.full_url
        payload = url_to_payload.get(url)
        if payload is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return _FakeResponse(payload)

    return _fake_urlopen


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _manifest_dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value)


def _manifest_entries(value: object) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], value)


def _path_list(value: object) -> list[str]:
    return cast(list[str], value)


def _text_map(value: object) -> dict[str, str]:
    return cast(dict[str, str], value)


def test_export_online_update_repository_zip_contains_manifest_and_active_macros(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = tmp_path / "db" / "macros.db"

    _write(
        config_dir / "printer.cfg",
        """
[gcode_macro PRINT_START]
gcode:
  G28

[gcode_macro PRINT_END]
gcode:
  M84
""".lstrip(),
    )

    run_indexing(config_dir, db_path)

    out_zip = tmp_path / "bundle.zip"
    result = export_online_update_repository_zip(
        db_path=db_path,
        out_file=out_zip,
        source_vendor="Voron",
        source_model="Trident",
        now_ts=1_775_560_000,
    )

    assert result["macro_count"] == 2
    assert out_zip.exists()

    with zipfile.ZipFile(out_zip, "r") as archive:
        members = set(archive.namelist())
        assert "updates/manifest.json" in members
        assert "README.md" not in members
        assert "docs/manifest-spec.md" not in members
        assert "voron/trident/PRINT_END.json" in members
        assert "voron/trident/PRINT_START.json" in members

        manifest = json.loads(archive.read("updates/manifest.json").decode("utf-8"))
        assert manifest["manifest_version"] == "1"
        assert len(manifest["macros"]) == 2
        for entry in manifest["macros"]:
            assert entry["vendor"] == "voron"
            assert entry["model"] == "trident"
            assert entry["path"].startswith("voron/trident/")
            assert entry["checksum_sha256"]


def test_export_online_update_repository_zip_merges_remote_manifest(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = tmp_path / "db" / "macros.db"

    _write(
        config_dir / "printer.cfg",
        """
[gcode_macro PRINT_START]
gcode:
  G28
""".lstrip(),
    )
    run_indexing(config_dir, db_path)

    repo_url = "https://github.com/example/online-repo"
    repo_ref = "main"
    manifest_path = "updates/manifest.json"
    manifest_url = "https://raw.githubusercontent.com/example/online-repo/main/updates/manifest.json"
    remote_manifest = {
        "manifest_version": "1",
        "generated_at": 111,
        "extra_meta": {"keep": True},
        "macros": [
            {
                "vendor": "voron",
                "model": "trident",
                "macro_name": "PRINT_START",
                "path": "voron/trident/PRINT_START.json",
                "version": "old",
                "checksum_sha256": "oldsum",
            },
            {
                "vendor": "ratrig",
                "model": "v-core-3",
                "macro_name": "PRINT_START",
                "path": "ratrig/v-core-3/PRINT_START.json",
                "version": "keep",
                "checksum_sha256": "keepsum",
            },
        ],
    }

    out_zip = tmp_path / "bundle-merged.zip"
    with patch("klipper_macro_online_repo_export.urlopen", _fake_urlopen_factory({manifest_url: remote_manifest})):
        export_online_update_repository_zip(
            db_path=db_path,
            out_file=out_zip,
            source_vendor="Voron",
            source_model="Trident",
            repo_url=repo_url,
            repo_ref=repo_ref,
            manifest_path=manifest_path,
            now_ts=1_775_560_000,
        )

    with zipfile.ZipFile(out_zip, "r") as archive:
        manifest = json.loads(archive.read("updates/manifest.json").decode("utf-8"))

    assert manifest["extra_meta"] == {"keep": True}
    assert manifest["generated_at"] == 1_775_560_000
    assert len(manifest["macros"]) == 2

    voron_entry = next(
        entry
        for entry in manifest["macros"]
        if entry["vendor"] == "voron" and entry["model"] == "trident" and entry["macro_name"] == "PRINT_START"
    )
    assert voron_entry["version"] != "old"
    assert voron_entry["checksum_sha256"] != "oldsum"

    ratrig_entry = next(
        entry
        for entry in manifest["macros"]
        if entry["vendor"] == "ratrig" and entry["model"] == "v-core-3"
    )
    assert ratrig_entry["version"] == "keep"
    assert ratrig_entry["checksum_sha256"] == "keepsum"


def test_build_artifacts_skips_unchanged_macros_in_files_to_write(tmp_path: Path) -> None:
    """Macros whose section_text matches the existing manifest checksum must not appear
    in files_to_write and must not advance generated_at."""
    config_dir = tmp_path / "config"
    db_path = tmp_path / "db" / "macros.db"

    section = "[gcode_macro PRINT_START]\ngcode:\n  G28\n"
    _write(config_dir / "printer.cfg", section)
    run_indexing(config_dir, db_path)

    # Compute the same checksum the function would compute.
    from klipper_macro_indexer import macro_row_to_section_text, load_macro_list
    macros = load_macro_list(db_path, limit=1)
    real_section_text = macro_row_to_section_text(macros[0])
    real_checksum = _sha256(real_section_text)

    existing_manifest: dict = {
        "manifest_version": "1",
        "generated_at": 999,
        "macros": [
            {
                "vendor": "voron",
                "model": "trident",
                "macro_name": "PRINT_START",
                "path": "voron/trident/PRINT_START.json",
                "version": "2025-01-01",
                "checksum_sha256": real_checksum,
            }
        ],
    }

    result = build_online_update_repository_artifacts(
        db_path=db_path,
        source_vendor="Voron",
        source_model="Trident",
        existing_manifest=existing_manifest,
        now_ts=1_775_560_000,
    )

    # File must not be written because content is identical.
    assert result["files_to_write"] == {}
    # generated_at must not be advanced because nothing changed.
    manifest = _manifest_dict(result["manifest"])
    assert manifest.get("generated_at") == 999
    # The manifest entry version should be preserved.
    entries = _manifest_entries(manifest["macros"])
    assert len(entries) == 1
    assert entries[0]["version"] == "2025-01-01"


def test_build_artifacts_includes_changed_macro_and_updates_generated_at(tmp_path: Path) -> None:
    """A macro whose checksum differs from the manifest entry must appear in files_to_write
    and generated_at must be updated to now_ts."""
    config_dir = tmp_path / "config"
    db_path = tmp_path / "db" / "macros.db"

    _write(config_dir / "printer.cfg", "[gcode_macro PRINT_START]\ngcode:\n  G28\n")
    run_indexing(config_dir, db_path)

    existing_manifest: dict = {
        "manifest_version": "1",
        "generated_at": 999,
        "macros": [
            {
                "vendor": "voron",
                "model": "trident",
                "macro_name": "PRINT_START",
                "path": "voron/trident/PRINT_START.json",
                "version": "2025-01-01",
                "checksum_sha256": "stale-checksum",  # intentionally wrong
            }
        ],
    }

    result = build_online_update_repository_artifacts(
        db_path=db_path,
        source_vendor="Voron",
        source_model="Trident",
        existing_manifest=existing_manifest,
        now_ts=1_775_560_000,
    )

    # File must be included because content changed.
    assert "voron/trident/PRINT_START.json" in _text_map(result["files_to_write"])
    # generated_at must be updated.
    manifest = _manifest_dict(result["manifest"])
    assert manifest.get("generated_at") == 1_775_560_000
    # Version must differ from the stale "old" value.
    entries = _manifest_entries(manifest["macros"])
    assert entries[0]["version"] != "2025-01-01"


def test_build_artifacts_marks_deleted_macro_files_and_prunes_manifest(tmp_path: Path) -> None:
    """When a macro is removed locally, artifacts must include a file deletion and
    remove the entry from manifest for the selected vendor/model."""
    config_dir = tmp_path / "config"
    db_path = tmp_path / "db" / "macros.db"

    _write(
        config_dir / "printer.cfg",
        """
[gcode_macro PRINT_END]
gcode:
  M84
""".lstrip(),
    )
    run_indexing(config_dir, db_path)

    existing_manifest: dict = {
        "manifest_version": "1",
        "generated_at": 111,
        "macros": [
            {
                "vendor": "voron",
                "model": "trident",
                "macro_name": "PRINT_START",
                "path": "voron/trident/PRINT_START.json",
                "version": "2025-01-01",
                "checksum_sha256": "old-start",
            },
            {
                "vendor": "voron",
                "model": "trident",
                "macro_name": "PRINT_END",
                "path": "voron/trident/PRINT_END.json",
                "version": "2025-01-01",
                "checksum_sha256": "old-end",
            },
        ],
    }

    result = build_online_update_repository_artifacts(
        db_path=db_path,
        source_vendor="Voron",
        source_model="Trident",
        existing_manifest=existing_manifest,
        now_ts=1_775_560_000,
    )

    assert "voron/trident/PRINT_START.json" in _path_list(result["files_to_delete"])
    manifest = _manifest_dict(result["manifest"])
    manifest_entries = _manifest_entries(manifest["macros"])
    assert len(manifest_entries) == 1
    assert manifest_entries[0]["macro_name"] == "PRINT_END"
    assert manifest.get("generated_at") == 1_775_560_000
