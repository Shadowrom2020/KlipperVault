from klipper_vault_ssh_transport import SshTransport, SshConnectionConfig


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
