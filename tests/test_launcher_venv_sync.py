import subprocess
from pathlib import Path

import klipper_vault_gui as klipper_vault


def test_container_runtime_enabled_by_env(monkeypatch) -> None:
    monkeypatch.setenv("KLIPPERVAULT_CONTAINER", "1")

    assert klipper_vault._is_container_runtime() is True


def test_container_runtime_disabled_by_env(monkeypatch) -> None:
    monkeypatch.setenv("KLIPPERVAULT_CONTAINER", "0")

    assert klipper_vault._is_container_runtime() is False


def test_container_runtime_detected_by_container_marker(monkeypatch) -> None:
    monkeypatch.delenv("KLIPPERVAULT_CONTAINER", raising=False)
    original_exists = klipper_vault.Path.exists

    def _fake_exists(path: Path) -> bool:
        if str(path) == "/.dockerenv":
            return True
        return original_exists(path)

    monkeypatch.setattr(klipper_vault.Path, "exists", _fake_exists)

    assert klipper_vault._is_container_runtime() is True


def test_ui_host_binding_uses_loopback_locally(monkeypatch) -> None:
    monkeypatch.setattr(klipper_vault, "_is_container_runtime", lambda: False)

    assert klipper_vault._ui_host_binding() == "127.0.0.1"


def test_ui_host_binding_uses_all_interfaces_in_container(monkeypatch) -> None:
    monkeypatch.setattr(klipper_vault, "_is_container_runtime", lambda: True)

    assert klipper_vault._ui_host_binding() == "0.0.0.0"


def test_is_benign_shutdown_exception_for_keyboard_interrupt() -> None:
    assert klipper_vault._is_benign_shutdown_exception(KeyboardInterrupt()) is True


def test_is_benign_shutdown_exception_for_known_winerrors(monkeypatch) -> None:
    monkeypatch.setattr(klipper_vault.platform, "system", lambda: "Windows")

    error = OSError(995, "The I/O operation has been aborted")
    error.winerror = 995  # type: ignore[attr-defined]
    assert klipper_vault._is_benign_shutdown_exception(error) is True


def test_is_benign_shutdown_exception_for_event_loop_closed_windows(monkeypatch) -> None:
    monkeypatch.setattr(klipper_vault.platform, "system", lambda: "Windows")

    error = RuntimeError("Event loop is closed")
    assert klipper_vault._is_benign_shutdown_exception(error) is True


def test_is_benign_shutdown_exception_for_event_loop_closed_linux(monkeypatch) -> None:
    monkeypatch.setattr(klipper_vault.platform, "system", lambda: "Linux")

    error = RuntimeError("Event loop is closed")
    assert klipper_vault._is_benign_shutdown_exception(error) is True


def test_is_benign_shutdown_exception_for_event_loop_closed_macos(monkeypatch) -> None:
    monkeypatch.setattr(klipper_vault.platform, "system", lambda: "Darwin")

    error = RuntimeError("Event loop is closed")
    assert klipper_vault._is_benign_shutdown_exception(error) is True


def test_is_benign_shutdown_exception_for_io_closed_pipe_linux(monkeypatch) -> None:
    monkeypatch.setattr(klipper_vault.platform, "system", lambda: "Linux")

    error = ValueError("I/O operation on closed file")
    assert klipper_vault._is_benign_shutdown_exception(error) is True


def test_is_benign_shutdown_exception_for_ebadf_linux(monkeypatch) -> None:
    import errno

    monkeypatch.setattr(klipper_vault.platform, "system", lambda: "Linux")

    error = OSError(errno.EBADF, "Bad file descriptor")
    assert klipper_vault._is_benign_shutdown_exception(error) is True


def test_is_benign_shutdown_exception_unrelated_error_not_suppressed(monkeypatch) -> None:
    monkeypatch.setattr(klipper_vault.platform, "system", lambda: "Linux")

    error = RuntimeError("Something went wrong")
    assert klipper_vault._is_benign_shutdown_exception(error) is False


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
    monkeypatch.setenv("KLIPPERVAULT_REQUIREMENTS_FILE", "requirements-custom.txt")

    assert klipper_vault._requirements_path() == tmp_path / "requirements-custom.txt"


def test_stamp_path_changes_per_requirements_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KLIPPERVAULT_REQUIREMENTS_FILE", "requirements-custom.txt")
    monkeypatch.setattr(klipper_vault.sys, "executable", str(tmp_path / "python"))

    stamp_path = klipper_vault._venv_requirements_stamp_path()

    assert stamp_path.name == ".klippervault_requirements-custom.txt.sha256"
