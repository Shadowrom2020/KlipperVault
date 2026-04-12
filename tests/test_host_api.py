from pathlib import Path
import threading
import time

from klipper_vault_host_api import _HostApiState, _requires_api_token_for_bind


class _DummyService:
    def index(self) -> dict[str, int]:
        return {"macros_inserted": 1}


def test_requires_api_token_for_bind_localhost_false() -> None:
    assert _requires_api_token_for_bind("127.0.0.1") is False
    assert _requires_api_token_for_bind("localhost") is False
    assert _requires_api_token_for_bind("::1") is False


def test_requires_api_token_for_bind_network_true() -> None:
    assert _requires_api_token_for_bind("0.0.0.0") is True
    assert _requires_api_token_for_bind("192.168.1.10") is True


def test_host_api_state_create_job_success() -> None:
    state = _HostApiState(
        service=_DummyService(),
        config_dir=Path("/tmp"),
        api_token="",
    )

    job = state.create_job(
        job_type="unit",
        trigger="test",
        runner=lambda report: ({"ok": True, "progress": report(1, 1)}),
    )

    assert job["status"] in {"queued", "running", "completed"}
    job_id = str(job["job_id"])

    # Wait briefly for background thread completion.
    for _ in range(100):
        stored = state.get_job(job_id)
        assert stored is not None
        if stored["status"] == "completed":
            assert stored["error"] == ""
            assert isinstance(stored["result"], dict)
            return
        time.sleep(0.01)

    raise AssertionError("job did not complete in time")


def test_host_api_state_create_job_failure() -> None:
    state = _HostApiState(
        service=_DummyService(),
        config_dir=Path("/tmp"),
        api_token="",
    )

    job = state.create_job(
        job_type="unit",
        trigger="test",
        runner=lambda report: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    job_id = str(job["job_id"])
    for _ in range(100):
        stored = state.get_job(job_id)
        assert stored is not None
        if stored["status"] == "failed":
            assert "boom" in str(stored["error"])
            return
        time.sleep(0.01)

    raise AssertionError("failed job did not reach failed status in time")


def test_host_api_state_publish_and_get_events() -> None:
    state = _HostApiState(
        service=_DummyService(),
        config_dir=Path("/tmp"),
        api_token="",
    )

    event = state.publish_event("unit.test", {"value": 1})
    assert int(event["id"]) >= 1
    events = state.get_events_after(0)
    assert len(events) >= 1
    assert events[-1]["type"] == "unit.test"


def test_host_api_state_wait_for_events_after_unblocks() -> None:
    state = _HostApiState(
        service=_DummyService(),
        config_dir=Path("/tmp"),
        api_token="",
    )

    def _publisher() -> None:
        time.sleep(0.05)
        state.publish_event("unit.wait", {"ready": True})

    publisher = threading.Thread(target=_publisher, daemon=True)
    publisher.start()
    events = state.wait_for_events_after(0, timeout=1.0)
    assert any(str(event.get("type", "")) == "unit.wait" for event in events)


def test_host_api_state_run_startup_index_records_result() -> None:
    state = _HostApiState(
        service=_DummyService(),
        config_dir=Path("/tmp"),
        api_token="",
    )

    result = state.run_startup_index()

    assert isinstance(result, dict)
    assert result == {"macros_inserted": 1}
    assert state.last_index_result == {"trigger": "startup", "macros_inserted": 1}
    assert isinstance(state.last_index_at, int)
    assert state.index_error == ""
