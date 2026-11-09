"""CLI parsing tests for ``--provision-fleet-telemetry``.

The provisioning command takes two required positionals (server hostname and
telemetry CA file), one optional positional (server port), and a required
``--tesla-vehicle-command-proxy`` option.  The proxy CA is optional.
"""

from pathlib import Path

import pytest

from app import _parse_provision_args
from config import Config


def _ca_file(tmp_path: Path, name: str = "ca.pem") -> Path:
    """Write a placeholder CA file and return its path."""
    path = tmp_path / name
    path.write_text("-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----")
    return path


def _cfg(interval: int = 15) -> Config:
    return Config(
        overrides={
            "TESLA_TELEMETRY_DETAILEDCHARGESTATE_INTERVAL_SEC": str(interval),
        }
    )


def test_provision_cli_requires_proxy_url(tmp_path):
    """The proxy URL option is required; argparse exits 2 without it."""
    ca = _ca_file(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        _parse_provision_args(["telemetry.example.com", str(ca)], cfg=_cfg())
    assert exc_info.value.code == 2


def test_provision_cli_parses_proxy_url(tmp_path):
    """A valid HTTP proxy URL lands in the dataclass."""
    ca = _ca_file(tmp_path)
    config = _parse_provision_args(
        [
            "telemetry.example.com",
            str(ca),
            "--tesla-vehicle-command-proxy",
            "http://127.0.0.1:4444",
        ],
        cfg=_cfg(),
    )
    assert config.vehicle_command_proxy_url == "http://127.0.0.1:4444"


def test_provision_cli_parses_proxy_ca(tmp_path):
    """A supplied proxy CA path lands in the dataclass."""
    ca = _ca_file(tmp_path)
    proxy_ca = _ca_file(tmp_path, "proxy-ca.pem")
    config = _parse_provision_args(
        [
            "telemetry.example.com",
            str(ca),
            "--tesla-vehicle-command-proxy",
            "https://localhost:4444",
            "--tesla-vehicle-command-proxy-ca",
            str(proxy_ca),
        ],
        cfg=_cfg(),
    )
    assert config.vehicle_command_proxy_ca_file == proxy_ca


def test_provision_cli_rejects_missing_proxy_ca(tmp_path):
    """A proxy CA path that does not exist is a usage error."""
    ca = _ca_file(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        _parse_provision_args(
            [
                "telemetry.example.com",
                str(ca),
                "--tesla-vehicle-command-proxy",
                "https://localhost:4444",
                "--tesla-vehicle-command-proxy-ca",
                str(tmp_path / "missing.pem"),
            ],
            cfg=_cfg(),
        )
    assert exc_info.value.code == 2


def test_provision_cli_rejects_bad_proxy_scheme(tmp_path):
    """Proxy URLs must use http or https."""
    ca = _ca_file(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        _parse_provision_args(
            [
                "telemetry.example.com",
                str(ca),
                "--tesla-vehicle-command-proxy",
                "ftp://localhost:4444",
            ],
            cfg=_cfg(),
        )
    assert exc_info.value.code == 2


def test_provision_cli_defaults_server_port(tmp_path):
    """The server port defaults to 4443."""
    ca = _ca_file(tmp_path)
    config = _parse_provision_args(
        [
            "telemetry.example.com",
            str(ca),
            "--tesla-vehicle-command-proxy",
            "http://127.0.0.1:4444",
        ],
        cfg=_cfg(),
    )
    assert config.server_port == 4443


def test_provision_cli_parses_positional_port(tmp_path):
    """An explicit positional port overrides the default."""
    ca = _ca_file(tmp_path)
    config = _parse_provision_args(
        [
            "telemetry.example.com",
            str(ca),
            "4445",
            "--tesla-vehicle-command-proxy",
            "http://127.0.0.1:4444",
        ],
        cfg=_cfg(),
    )
    assert config.server_port == 4445


def test_provision_config_carries_proxy_fields(tmp_path):
    """The parsed config carries both proxy fields and the interval."""
    ca = _ca_file(tmp_path)
    proxy_ca = _ca_file(tmp_path, "proxy-ca.pem")
    config = _parse_provision_args(
        [
            "telemetry.example.com",
            str(ca),
            "--tesla-vehicle-command-proxy",
            "https://localhost:4444",
            "--tesla-vehicle-command-proxy-ca",
            str(proxy_ca),
        ],
        cfg=_cfg(interval=30),
    )
    assert config.server_hostname == "telemetry.example.com"
    assert config.ca_file_path == ca
    assert config.vehicle_command_proxy_url == "https://localhost:4444"
    assert config.vehicle_command_proxy_ca_file == proxy_ca
    assert config.detailedchargestate_interval_sec == 30
