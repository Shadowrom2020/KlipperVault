from unittest.mock import MagicMock, patch

import paramiko
import pytest

from klipper_vault_ssh_transport import (
    SshConnectionConfig,
    SshTransport,
    _ensure_host_trusted,
    _known_hosts_path,
)


def test_safe_remote_path_normalization() -> None:
    transport = SshTransport(
        SshConnectionConfig(
            host="example.local",
            port=22,
            username="pi",
            auth_mode="password",
            secret_value="pw",
        )
    )

    assert transport._safe_remote_path("/home/pi/printer_data/config") == "/home/pi/printer_data/config"
    assert transport._safe_remote_path("home/pi/printer_data/config") == "/home/pi/printer_data/config"
    assert transport._safe_remote_path("~/printer_data/config") == "/home/pi/printer_data/config"


def test_known_hosts_path_is_inside_config_dir() -> None:
    path = _known_hosts_path()
    assert path.name == "known_hosts"
    assert "klippervault" in str(path).lower()


def test_ensure_host_trusted_skips_when_already_known(tmp_path: pytest.FixtureRequest) -> None:
    """When the host key is already stored, no Transport connection is opened."""
    kh_file = tmp_path / "known_hosts"
    # Write a minimal RSA dummy key entry so lookup succeeds
    dummy_key = paramiko.RSAKey.generate(1024)
    host_keys = paramiko.HostKeys()
    host_keys.add("printer.local", dummy_key.get_name(), dummy_key)
    host_keys.save(str(kh_file))

    with patch("klipper_vault_ssh_transport._known_hosts_path", return_value=kh_file):
        with patch("klipper_vault_ssh_transport.paramiko.Transport") as mock_transport_cls:
            _ensure_host_trusted("printer.local", 22, 5.0)
            mock_transport_cls.assert_not_called()


def test_ensure_host_trusted_tofu_for_unknown_host(tmp_path: pytest.FixtureRequest) -> None:
    """When the host is unknown, the key is fetched and saved to known_hosts."""
    kh_file = tmp_path / "known_hosts"
    dummy_key = paramiko.RSAKey.generate(1024)

    mock_transport = MagicMock()
    mock_transport.get_remote_server_key.return_value = dummy_key

    with patch("klipper_vault_ssh_transport._known_hosts_path", return_value=kh_file):
        with patch("klipper_vault_ssh_transport.paramiko.Transport", return_value=mock_transport):
            _ensure_host_trusted("newhost.local", 22, 5.0)

    mock_transport.start_client.assert_called_once()
    mock_transport.get_remote_server_key.assert_called_once()
    mock_transport.close.assert_called()

    assert kh_file.exists()
    saved = paramiko.HostKeys()
    saved.load(str(kh_file))
    assert saved.lookup("newhost.local") is not None


def test_ensure_host_trusted_non_standard_port_formats_correctly(tmp_path: pytest.FixtureRequest) -> None:
    """Non-standard ports should use bracket notation in known_hosts."""
    kh_file = tmp_path / "known_hosts"
    dummy_key = paramiko.RSAKey.generate(1024)

    mock_transport = MagicMock()
    mock_transport.get_remote_server_key.return_value = dummy_key

    with patch("klipper_vault_ssh_transport._known_hosts_path", return_value=kh_file):
        with patch("klipper_vault_ssh_transport.paramiko.Transport", return_value=mock_transport):
            _ensure_host_trusted("printer.local", 2222, 5.0)

    saved = paramiko.HostKeys()
    saved.load(str(kh_file))
    assert saved.lookup("[printer.local]:2222") is not None
