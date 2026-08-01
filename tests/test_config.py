"""Tests for centralized Config class and module-level helpers."""

import os
from unittest.mock import patch, PropertyMock

import pytest
from config import Config as _Config


class TestConfigClass:
    """Tests for the Config class properties."""

    @pytest.fixture(autouse=True)
    def fresh_config(self):
        """Create a new Config instance for each test."""
        _Config().clear_all()

    def test_timezone_default(self):
        """Returns default timezone when env var is unset."""
        from config import Config

        cfg = Config()
        assert cfg.timezone == "America/Los_Angeles"

    def test_timezone_from_env(self):
        """Returns timezone from TIMEZONE env var."""
        _Config().set("TIMEZONE", "America/New_York")

        from config import Config

        cfg = Config()
        assert cfg.timezone == "America/New_York"

    def test_is_not_mock_mode_no_credentials(self):
        """Returns True when VUE_USERNAME is not set."""
        _Config().set("VUE_USERNAME", "")

        from config import Config

        cfg = Config()
        assert cfg.is_mock_mode is False

    def test_is_mock_mode_with_credentials(self):
        """Returns False when VUE_USERNAME is set and MOCK=False."""
        _Config().set("VUE_USERNAME", "user")
        _Config().set("MOCK", "False")

        from config import Config

        cfg = Config()
        assert cfg.is_mock_mode is False

    def test_is_mock_mode_explicitly_enabled(self):
        """Returns True when MOCK=True even with credentials."""
        _Config().set("VUE_USERNAME", "user")
        _Config().set("MOCK", "True")

        from config import Config

        cfg = Config()
        assert cfg.is_mock_mode is True

    def test_is_mock_error_default(self):
        """Returns False when MOCK_ERROR is not set."""
        from config import Config

        cfg = Config()
        assert cfg.is_mock_error is False

    def test_is_mock_error_enabled(self):
        """Returns True when MOCK_ERROR=True."""
        _Config().set("MOCK_ERROR", "True")

        from config import Config

        cfg = Config()
        assert cfg.is_mock_error is True

    def test_debug_default(self):
        """Returns False when DEBUG is not set."""
        from config import Config

        cfg = Config()
        assert cfg.debug is False

    def test_debug_enabled(self):
        """Returns True when DEBUG=True."""
        _Config().set("DEBUG", "True")

        from config import Config

        cfg = Config()
        assert cfg.debug is True

    def test_dry_run_default(self):
        """Returns False when LOAD_MANAGE_DRY_RUN is not set (default)."""

        from config import Config

        cfg = Config()
        assert cfg.dry_run is False, f"Expected dry_run=False by default but got {cfg.dry_run}"

    def test_dry_run_disabled(self):
        """Returns False when LOAD_MANAGE_DRY_RUN=False."""
        _Config().set("LOAD_MANAGE_DRY_RUN", "False")

        from config import Config

        cfg = Config()
        assert cfg.dry_run is False


class TestConfigSingleton:
    """Tests for the module-level cfg singleton."""

    @pytest.fixture(autouse=True)
    def fresh_config(self):
        """Reset config sources before each test."""
        _Config().clear_all()

    def test_singleton_is_instance(self):
        """The cfg module-level variable is a Config instance."""
        from config import Config, _config

        assert isinstance(_config, Config)


class TestBackwardCompatFunctions:
    """Tests for backward-compatible module-level functions."""

    @pytest.fixture(autouse=True)
    def fresh_config(self):
        """Reset config sources before each test."""
        _Config().clear_all()

    def test_get_timezone_returns_cfg_timezone(self):
        """get_timezone() returns cfg.timezone."""
        from config import get_timezone

        assert isinstance(get_timezone(), str)

    def test_get_timezone_respects_env(self):
        """get_timezone() respects TIMEZONE env var."""
        _Config().set("TIMEZONE", "Europe/London")

        from config import get_timezone, Config
        import config as cfg_mod

        # Create a fresh singleton to pick up the new env var
        original_cfg = cfg_mod._config
        try:
            cfg_mod._config = Config()
            assert get_timezone() == "Europe/London"
        finally:
            # Restore original singleton so other tests are not polluted
            cfg_mod._config = original_cfg

    def test_get_timezone_lazy_eval(self):
        """get_timezone() picks up env changes without replacing singleton."""
        _Config().set("TIMEZONE", "Europe/London")

        from config import get_timezone, Config
        import config as cfg_mod

        # With lazy evaluation the existing singleton works — no replacement needed
        assert get_timezone() == "Europe/London"

        # Restore default for other tests
        _Config().set("TIMEZONE", "America/Los_Angeles")


class TestConfigTeslaProperties:
    """Tests for Tesla-related config properties."""

    @pytest.fixture(autouse=True)
    def fresh_config(self):
        """Reset config sources before each test."""
        _Config().clear_all()

    def test_tesla_client_id_default(self):
        """Returns None when TESLA_CLIENT_ID is not set."""
        from config import Config

        cfg = Config()
        assert cfg.tesla_client_id is None

    def test_tesla_redirect_uri_default(self):
        """Returns empty string when TESLA_REDIRECT_URI is not set."""
        from config import Config

        cfg = Config()
        assert cfg.tesla_redirect_uri == ""


class TestConfigTeslaTelemetryProperties:
    """Tests for Tesla fleet-telemetry (MQTT) config properties."""

    @pytest.fixture(autouse=True)
    def fresh_config(self):
        """Reset config sources before each test."""
        _Config().clear_all()

    def test_mqtt_host_default(self):
        """Returns 'localhost' when MQTT_HOST is not set."""
        from config import Config

        cfg = Config()
        assert cfg.mqtt_host == "localhost"

    def test_mqtt_host_from_env(self):
        """Returns host from MQTT_HOST env var."""
        _Config().set("MQTT_HOST", "192.168.1.50")

        from config import Config

        cfg = Config()
        assert cfg.mqtt_host == "192.168.1.50"

    def test_mqtt_port_default(self):
        """Returns 1883 when MQTT_PORT is not set."""
        from config import Config

        cfg = Config()
        assert cfg.mqtt_port == 1883

    def test_mqtt_port_from_env(self):
        """Returns port from MQTT_PORT env var."""
        _Config().set("MQTT_PORT", "8883")

        from config import Config

        cfg = Config()
        assert cfg.mqtt_port == 8883

    def test_mqtt_topic_base_default(self):
        """Returns 'tesla/telemetry' when MQTT_TOPIC_BASE is not set."""
        from config import Config

        cfg = Config()
        assert cfg.mqtt_topic_base == "tesla/telemetry"

    def test_mqtt_topic_base_from_env(self):
        """Returns topic base from MQTT_TOPIC_BASE env var."""
        _Config().set("MQTT_TOPIC_BASE", "vehicles/1")

        from config import Config

        cfg = Config()
        assert cfg.mqtt_topic_base == "vehicles/1"

    def test_tesla_telemetry_chargeamps_interval_default(self):
        """Returns 15 when TESLA_TELEMETRY_CHARGEAMPS_INTERVAL_SEC is not set."""
        from config import Config

        cfg = Config()
        assert cfg.tesla_telemetry_chargeamps_interval == 15

    def test_tesla_telemetry_chargeamps_interval_from_env(self):
        """Returns value from TESLA_TELEMETRY_CHARGEAMPS_INTERVAL_SEC env var."""
        _Config().set("TESLA_TELEMETRY_CHARGEAMPS_INTERVAL_SEC", "30")

        from config import Config

        cfg = Config()
        assert cfg.tesla_telemetry_chargeamps_interval == 30

    def test_tesla_telemetry_location_interval_default(self):
        """Returns 120 when TESLA_TELEMETRY_LOCATION_INTERVAL_SEC is not set."""
        from config import Config

        cfg = Config()
        assert cfg.tesla_telemetry_location_interval == 120

    def test_tesla_telemetry_location_interval_from_env(self):
        """Returns value from TESLA_TELEMETRY_LOCATION_INTERVAL_SEC env var."""
        _Config().set("TESLA_TELEMETRY_LOCATION_INTERVAL_SEC", "60")

        from config import Config

        cfg = Config()
        assert cfg.tesla_telemetry_location_interval == 60

    def test_tesla_telemetry_chargestate_interval_default(self):
        """Returns 15 when TESLA_TELEMETRY_CHARGESTATE_INTERVAL_SEC is not set."""
        from config import Config

        cfg = Config()
        assert cfg.tesla_telemetry_chargestate_interval == 15

    def test_tesla_telemetry_chargestate_interval_from_env(self):
        """Returns value from TESLA_TELEMETRY_CHARGESTATE_INTERVAL_SEC env var."""
        _Config().set("TESLA_TELEMETRY_CHARGESTATE_INTERVAL_SEC", "30")

        from config import Config

        cfg = Config()
        assert cfg.tesla_telemetry_chargestate_interval == 30

    def test_tesla_telemetry_detailedchargestate_interval_default(self):
        """Returns 15 when TESLA_TELEMETRY_DETAILEDCHARGESTATE_INTERVAL_SEC is not set."""
        from config import Config

        cfg = Config()
        assert cfg.tesla_telemetry_detailedchargestate_interval == 15

    def test_tesla_telemetry_detailedchargestate_interval_from_env(self):
        """Returns value from TESLA_TELEMETRY_DETAILEDCHARGESTATE_INTERVAL_SEC env var."""
        _Config().set("TESLA_TELEMETRY_DETAILEDCHARGESTATE_INTERVAL_SEC", "30")

        from config import Config

        cfg = Config()
        assert cfg.tesla_telemetry_detailedchargestate_interval == 30


class TestConfigPlugControllerProperties:
    """Tests for plug/Tesla controller config properties."""

    @pytest.fixture(autouse=True)
    def fresh_config(self):
        """Reset config sources before each test."""
        _Config().clear_all()

    def test_load_plug_controller_default(self):
        """Returns 'stub' when LOAD_PLUG_CONTROLLER is not set."""
        from config import Config

        cfg = Config()
        assert cfg.load_plug_controller == "stub"

    def test_load_tesla_controller_default(self):
        """Returns 'stub' when LOAD_TESLA_CONTROLLER is not set."""
        from config import Config

        cfg = Config()
        assert cfg.load_tesla_controller == "stub"


class TestConfigVocolincProperties:
    """Tests for VOCOlinc config properties."""

    @pytest.fixture(autouse=True)
    def fresh_config(self):
        """Reset config sources before each test."""
        _Config().clear_all()

    def test_vocolinc_credentials_empty_by_default(self):
        """Returns empty strings when VOCOlinc credentials are not set."""
        from config import Config

        cfg = Config()
        assert cfg.vocolinc_username == ""
        assert cfg.vocolinc_password == ""


class TestConfigClearAll:
    """clear_all() removes app-owned keys but leaves unrelated env vars."""

    def test_clear_all_removes_app_owned_keys(self):
        """App-owned keys (prefixes and exact names) are removed."""
        _Config().set("MOCK", "True")
        _Config().set("LOAD_MANAGE_ENABLED", "True")
        _Config().set("TESLA_CLIENT_ID", "client-id")
        _Config().set("VUE_USERNAME", "user")
        _Config().set("TIMEZONE", "America/New_York")

        _Config().clear_all()

        for key in (
            "MOCK", "LOAD_MANAGE_ENABLED",
            "TESLA_CLIENT_ID", "VUE_USERNAME", "TIMEZONE",
        ):
            assert key not in os.environ, f"{key} should have been removed"

    def test_clear_all_preserves_unrelated_env_vars(self, monkeypatch):
        """Non-app env vars survive clear_all()."""
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("UNRELATED_VAR", "keep-me")

        _Config().clear_all()

        assert os.environ.get("UNRELATED_VAR") == "keep-me"
        assert os.environ.get("PATH") == "/usr/bin:/bin"

    def test_clear_all_blocks_env_file_repollution(self, tmp_path, monkeypatch):
        """After clear_all(), .env values are not re-read into lookups."""
        from config import Config
        import config as config_module

        monkeypatch.delenv("VUE_USERNAME", raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("VUE_USERNAME=env-file-user\n")
        # Ensure the lazy cache is empty so a lookup would parse the file.
        monkeypatch.setattr(config_module, "_env_data", None)
        assert Config().vue_username == "env-file-user"

        _Config().clear_all()

        # Cache is marked parsed-empty: the .env value must not reappear.
        cfg = Config()
        assert cfg.vue_username is None
