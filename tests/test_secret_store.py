from pathlib import Path

from klipper_vault_secret_store import CredentialStore


def test_secret_store_uses_db_fallback_when_keyring_unavailable(tmp_path: Path) -> None:
    db_path = tmp_path / "vault.db"
    store = CredentialStore(db_path)

    backend = store.set_secret(
        credential_ref="ssh:office:password",
        secret_type="password",
        secret_value="pw-123",
    )

    loaded = store.get_secret(credential_ref="ssh:office:password")
    assert backend in {"os_keyring", "db_fallback"}
    assert loaded == "pw-123"


def test_secret_store_rejects_empty_reference(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "vault.db")

    try:
        store.set_secret(
            credential_ref="",
            secret_type="password",
            secret_value="pw-123",
        )
    except ValueError as exc:
        assert "credential_ref" in str(exc)
    else:
        raise AssertionError("Expected ValueError for empty credential_ref")


def test_secret_store_status_contains_platform_info(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "vault.db")
    status = store.status

    assert isinstance(status.platform_name, str)
    assert status.platform_name != ""
    assert isinstance(status.keyring_available, bool)
    assert isinstance(status.keyring_backend, str)
