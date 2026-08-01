"""Regression tests: credential config resolves from a .env file.

These mirror the telegram .env tests (tests/test_telegram.py). The config
lookup chain is overrides -> os.environ -> .env file; a code path that reads
only os.environ (as load_telegram_config did before its fix) fails these
tests, so the Emporia/Vocolinc/Tesla credential paths are protected against
the same regression.
"""

import config as config_module
import pytest

from config import Config
from config_loader import load_tesla_config, load_vocolinc_credentials


def _write_env_file(monkeypatch, tmp_path, keys):
    """Point the config .env lookup at a temp file containing *keys*.

    Removes the keys from os.environ (so the .env values are visible),
    writes a .env file in tmp_path, resets the lazy .env cache, and
    chdirs to tmp_path. monkeypatch restores everything at teardown.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: pytest tmp_path fixture.
        keys: Dict of config key -> value to place in the .env file.
    """
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    content = "".join(f"{key}={value}\n" for key, value in keys.items())
    (tmp_path / ".env").write_text(content)
    monkeypatch.setattr(config_module, "_env_data", None)
    monkeypatch.chdir(tmp_path)


TESLA_ENV_KEYS = {
    "TESLA_CLIENT_ID": "env-file-client-id",
    "TESLA_CLIENT_SECRET": "env-file-client-secret",
    "TESLA_VEHICLE_ID": "env-file-vehicle-id",
    "TESLA_REDIRECT_URI": "https://env.example/callback",
}


class TestEnvFileCredentials:
    """Credential paths resolve from a .env file, not just os.environ."""

    def test_load_tesla_config_from_env_file(self, monkeypatch, tmp_path):
        """TESLA_* keys in .env produce a usable TeslaConfig."""
        _write_env_file(monkeypatch, tmp_path, TESLA_ENV_KEYS)

        tesla_config = load_tesla_config()

        assert tesla_config is not None
        assert tesla_config.client_id == TESLA_ENV_KEYS["TESLA_CLIENT_ID"]
        assert tesla_config.client_secret == TESLA_ENV_KEYS["TESLA_CLIENT_SECRET"]
        assert tesla_config.vehicle_id == TESLA_ENV_KEYS["TESLA_VEHICLE_ID"]
        assert tesla_config.redirect_uri == TESLA_ENV_KEYS["TESLA_REDIRECT_URI"]
        assert tesla_config.home_lat is None
        assert tesla_config.home_lon is None

    def test_load_vocolinc_credentials_from_env_file(self, monkeypatch, tmp_path):
        """VOCOLINC_* keys in .env produce credentials."""
        _write_env_file(
            monkeypatch,
            tmp_path,
            {
                "VOCOLINC_USERNAME": "env-file-user",
                "VOCOLINC_PASSWORD": "env-file-pass",
            },
        )

        creds = load_vocolinc_credentials()

        assert creds == ("env-file-user", "env-file-pass")

    def test_vue_credentials_from_env_file(self, monkeypatch, tmp_path):
        """VUE_* keys in .env are visible via the Config properties.

        MetricsBase.vue_init() reads exactly these properties
        (cfg.vue_username / cfg.vue_password).
        """
        _write_env_file(
            monkeypatch,
            tmp_path,
            {
                "VUE_USERNAME": "env-file-user",
                "VUE_PASSWORD": "env-file-pass",
            },
        )

        cfg = Config()

        assert cfg.vue_username == "env-file-user"
        assert cfg.vue_password == "env-file-pass"

    def test_env_var_takes_precedence_over_env_file(self, monkeypatch, tmp_path):
        """os.environ wins over the .env file for the same key."""
        monkeypatch.setenv("VUE_USERNAME", "env-var-user")
        _write_env_file(
            monkeypatch,
            tmp_path,
            {
                "VUE_PASSWORD": "env-file-pass",
            },
        )

        cfg = Config()

        assert cfg.vue_username == "env-var-user"
        assert cfg.vue_password == "env-file-pass"
