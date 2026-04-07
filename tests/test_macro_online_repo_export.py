import json
from pathlib import Path
from unittest.mock import patch
import zipfile

from klipper_macro_indexer import run_indexing
from klipper_macro_online_repo_export import export_online_update_repository_zip


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
