from unittest.mock import MagicMock, patch

import paramiko  # type: ignore[import-untyped]

from klipper_vault_ssh_transport import (
    SshConnectionConfig,
    SshTransport,
    _ensure_host_trusted,
    _known_hosts_path,
)


class _FakeSftpFile:
    def __init__(self, content: bytes = b"") -> None:
        self._content = content
        self.writes: list[bytes] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return self._content

    def write(self, payload: bytes) -> int:
        self.writes.append(payload)
        return len(payload)


class _FakeSftp:
    def __init__(self) -> None:
        self.listing: dict[str, list[object]] = {}
        self.rename_should_fail = False
        self.renames: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self.files: dict[tuple[str, str], _FakeSftpFile] = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def listdir_attr(self, path: str):
        return self.listing.get(path, [])

    def file(self, path: str, mode: str) -> _FakeSftpFile:
        key = (path, mode)
        if key not in self.files:
            self.files[key] = _FakeSftpFile()
        return self.files[key]

    def rename(self, src: str, dst: str) -> None:
        self.renames.append((src, dst))
        if self.rename_should_fail:
            raise OSError("Failure")

    def remove(self, path: str) -> None:
        self.removed.append(path)


class _DirEntry:
    def __init__(self, filename: str, is_dir: bool) -> None:
        self.filename = filename
        self.st_mode = 0o040000 if is_dir else 0o100644


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


def test_ensure_host_trusted_skips_when_already_known(tmp_path) -> None:
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


def test_ensure_host_trusted_tofu_for_unknown_host(tmp_path) -> None:
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


def test_ensure_host_trusted_non_standard_port_formats_correctly(tmp_path) -> None:
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


def test_list_cfg_files_recurses_and_returns_sorted_paths() -> None:
    transport = SshTransport(
        SshConnectionConfig(
            host="example.local",
            port=22,
            username="pi",
            auth_mode="password",
            secret_value="pw",
        )
    )

    fake_sftp = _FakeSftp()
    fake_sftp.listing = {
        "/home/pi/printer_data/config": [
            _DirEntry("printer.cfg", is_dir=False),
            _DirEntry("extras", is_dir=True),
            _DirEntry("notes.txt", is_dir=False),
        ],
        "/home/pi/printer_data/config/extras": [
            _DirEntry("macros.cfg", is_dir=False),
            _DirEntry("README.md", is_dir=False),
        ],
    }

    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp

    with patch.object(SshTransport, "_connect", return_value=fake_client):
        files = transport.list_cfg_files("~/printer_data/config")

    assert files == [
        "/home/pi/printer_data/config/extras/macros.cfg",
        "/home/pi/printer_data/config/printer.cfg",
    ]


def test_write_text_file_atomic_falls_back_to_direct_write_on_rename_error() -> None:
    transport = SshTransport(
        SshConnectionConfig(
            host="example.local",
            port=22,
            username="pi",
            auth_mode="password",
            secret_value="pw",
        )
    )

    fake_sftp = _FakeSftp()
    fake_sftp.rename_should_fail = True
    fake_client = MagicMock()
    fake_client.open_sftp.return_value = fake_sftp

    with patch.object(SshTransport, "_connect", return_value=fake_client):
        transport.write_text_file_atomic("~/printer_data/config/printer.cfg", "[gcode_macro TEST]\n")

    assert fake_sftp.renames
    temp_path, target_path = fake_sftp.renames[0]
    assert target_path == "/home/pi/printer_data/config/printer.cfg"

    tmp_writes = fake_sftp.files[(temp_path, "wb")].writes
    dst_writes = fake_sftp.files[(target_path, "wb")].writes
    assert tmp_writes
    assert dst_writes
    assert tmp_writes[0] == dst_writes[0]
    assert temp_path in fake_sftp.removed
