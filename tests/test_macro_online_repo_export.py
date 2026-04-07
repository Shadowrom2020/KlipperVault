import json
from pathlib import Path
import zipfile

from klipper_macro_indexer import run_indexing
from klipper_macro_online_repo_export import export_online_update_repository_zip


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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
