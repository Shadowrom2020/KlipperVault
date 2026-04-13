import subprocess
from pathlib import Path

import klipper_vault


def test_sync_venv_requirements_skips_when_hash_matches(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KLIPPERVAULT_AUTO_UPDATE_VENV", raising=False)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("nicegui==2.0.0\n", encoding="utf-8")

    stamp = tmp_path / ".stamp"
    expected_hash = klipper_vault._requirements_hash(requirements)
    stamp.write_text(expected_hash + "\n", encoding="utf-8")

    monkeypatch.setattr(klipper_vault, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(klipper_vault, "_venv_requirements_stamp_path", lambda: stamp)

    called = {"value": False}

    def _fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        called["value"] = True
        raise AssertionError("pip install should not run when hash matches")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    klipper_vault._sync_venv_requirements_if_needed()

    assert called["value"] is False


def test_sync_venv_requirements_installs_and_writes_stamp(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KLIPPERVAULT_AUTO_UPDATE_VENV", raising=False)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("nicegui==2.0.0\n", encoding="utf-8")

    stamp = tmp_path / ".stamp"

    monkeypatch.setattr(klipper_vault, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(klipper_vault, "_venv_requirements_stamp_path", lambda: stamp)

    calls = []

    def _fake_run(cmd, cwd, check):  # type: ignore[no-untyped-def]
        calls.append((cmd, cwd, check))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    klipper_vault._sync_venv_requirements_if_needed()

    assert len(calls) == 1
    assert calls[0][0][1:4] == ["-m", "pip", "install"]
    assert calls[0][1] == str(tmp_path)
    assert calls[0][2] is True

    expected_hash = klipper_vault._requirements_hash(requirements)
    assert stamp.read_text(encoding="utf-8").strip() == expected_hash


def test_sync_venv_requirements_disabled_by_env(tmp_path: Path, monkeypatch) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("nicegui==2.0.0\n", encoding="utf-8")

    stamp = tmp_path / ".stamp"

    monkeypatch.setattr(klipper_vault, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(klipper_vault, "_venv_requirements_stamp_path", lambda: stamp)
    monkeypatch.setenv("KLIPPERVAULT_AUTO_UPDATE_VENV", "0")

    called = {"value": False}

    def _fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        called["value"] = True
        raise AssertionError("pip install should not run when disabled")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    klipper_vault._sync_venv_requirements_if_needed()

    assert called["value"] is False
    assert not stamp.exists()


def test_requirements_path_uses_env_relative_to_repo_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(klipper_vault, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("KLIPPERVAULT_REQUIREMENTS_FILE", "requirements-printer.txt")

    assert klipper_vault._requirements_path() == tmp_path / "requirements-printer.txt"


def test_stamp_path_changes_per_requirements_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KLIPPERVAULT_REQUIREMENTS_FILE", "requirements-printer.txt")
    monkeypatch.setattr(klipper_vault.sys, "executable", str(tmp_path / "python"))

    stamp_path = klipper_vault._venv_requirements_stamp_path()

    assert stamp_path.name == ".klippervault_requirements-printer.txt.sha256"
