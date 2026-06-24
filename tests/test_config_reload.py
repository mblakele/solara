"""Tests for config reload: reload_dotenv(), ConfigWatcher, LoadManager.reload_config()."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from config import (
    ConfigWatcher,
    check_restart_required,
    reload_dotenv,
)


# === reload_dotenv tests ===


class TestReloadDotenv:
    """Tests for reload_dotenv() — re-reading .env into os.environ + decouple."""

    def test_returns_empty_when_no_changes(self, tmp_path: Path) -> None:
        """No changes → empty list."""
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=bar\n")
        import os
        os.environ["FOO"] = "bar"
        try:
            result = reload_dotenv(env_file)
            assert not result
        finally:
            os.environ.pop("FOO", None)

    def test_detects_new_key(self, tmp_path: Path) -> None:
        """New key in .env that wasn't in os.environ."""
        import os
        env_file = tmp_path / ".env"
        env_file.write_text("NEW_KEY=hello\n")
        os.environ.pop("NEW_KEY", None)
        try:
            result = reload_dotenv(env_file)
            assert "NEW_KEY" in result
            assert os.environ["NEW_KEY"] == "hello"
        finally:
            os.environ.pop("NEW_KEY", None)

    def test_detects_modified_value(self, tmp_path: Path) -> None:
        """Key exists but value changed."""
        import os
        env_file = tmp_path / ".env"
        env_file.write_text("MY_KEY=old_value\n")
        os.environ["MY_KEY"] = "old_value"
        try:
            # Now change the file
            env_file.write_text("MY_KEY=new_value\n")
            result = reload_dotenv(env_file)
            assert "MY_KEY" in result
            assert os.environ["MY_KEY"] == "new_value"
        finally:
            os.environ.pop("MY_KEY", None)

    def test_ignores_unchanged_keys(self, tmp_path: Path) -> None:
        """Key with same value → not in changed list."""
        import os
        env_file = tmp_path / ".env"
        env_file.write_text("STABLE=unchanged\n")
        os.environ["STABLE"] = "unchanged"
        try:
            result = reload_dotenv(env_file)
            assert "STABLE" not in result
        finally:
            os.environ.pop("STABLE", None)

    def test_handles_missing_file(self, tmp_path: Path) -> None:
        """Missing .env file → empty list, no error."""
        result = reload_dotenv(tmp_path / "nonexistent.env")
        assert not result

    def test_handles_empty_file(self, tmp_path: Path) -> None:
        """Empty .env file → empty list."""
        env_file = tmp_path / ".env"
        env_file.write_text("")
        result = reload_dotenv(env_file)
        assert not result

    def test_handles_comments_and_blank_lines(self, tmp_path: Path) -> None:
        """Comments and blank lines are skipped."""
        import os
        env_file = tmp_path / ".env"
        env_file.write_text("# comment\n\nKEY=value\n")
        os.environ.pop("KEY", None)
        try:
            result = reload_dotenv(env_file)
            assert "KEY" in result
            assert os.environ["KEY"] == "value"
        finally:
            os.environ.pop("KEY", None)

    def test_updates_decouple_repository(self, tmp_path: Path) -> None:
        """Changed values are reflected when reading via decouple config()."""
        from decouple import config as decouple_config
        import os
        env_file = tmp_path / ".env"
        env_file.write_text("DC_TEST_KEY=old\n")
        os.environ["DC_TEST_KEY"] = "old"
        try:
            env_file.write_text("DC_TEST_KEY=new\n")
            reload_dotenv(env_file)
            # decouple reads from os.environ first, so this should work
            assert decouple_config("DC_TEST_KEY") == "new"
        finally:
            os.environ.pop("DC_TEST_KEY", None)


# === ConfigWatcher tests ===


class TestConfigWatcher:
    """Tests for ConfigWatcher — mtime tracking and change detection."""

    def test_first_check_records_mtime(self, tmp_path: Path) -> None:
        """First call records the mtime but reports no changes."""
        devices_file = tmp_path / "devices.json"
        devices_file.write_text('{"smartmeter": {"target_wh": -50}}')
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=bar\n")

        watcher = ConfigWatcher(env_path=env_file, devices_path=devices_file)
        changes = watcher.check()
        assert not changes.devices_changed
        assert changes.env_changed is None or changes.env_changed == []

    def test_detects_devices_json_change(self, tmp_path: Path) -> None:
        """Modify devices.json → devices_changed=True."""
        devices_file = tmp_path / "devices.json"
        devices_file.write_text('{"smartmeter": {"target_wh": -50}}')
        watcher = ConfigWatcher(devices_path=devices_file)
        watcher.check()  # record initial mtime

        # Modify the file
        time.sleep(0.01)  # ensure mtime changes
        devices_file.write_text('{"smartmeter": {"target_wh": -100}}')
        changes = watcher.check()
        assert changes.devices_changed is True

    def test_no_change_when_unchanged(self, tmp_path: Path) -> None:
        """No modification → no changes detected."""
        devices_file = tmp_path / "devices.json"
        devices_file.write_text('{"smartmeter": {"target_wh": -50}}')
        watcher = ConfigWatcher(devices_path=devices_file)
        watcher.check()  # record initial mtime
        changes = watcher.check()  # no change
        assert not changes.devices_changed

    def test_detects_env_change(self, tmp_path: Path) -> None:
        """Modify .env → env_changed with changed keys."""
        import os
        env_file = tmp_path / ".env"
        env_file.write_text("WATCH_KEY=old\n")
        os.environ.pop("WATCH_KEY", None)
        try:
            watcher = ConfigWatcher(env_path=env_file)
            watcher.check()  # record initial mtime

            time.sleep(0.01)
            env_file.write_text("WATCH_KEY=new\n")
            changes = watcher.check()
            assert changes.env_changed is not None
            assert "WATCH_KEY" in changes.env_changed
        finally:
            os.environ.pop("WATCH_KEY", None)

    def test_handles_missing_files(self, tmp_path: Path) -> None:
        """Watcher works when files don't exist yet."""
        watcher = ConfigWatcher(
            env_path=tmp_path / "no_such.env",
            devices_path=tmp_path / "no_such.json",
        )
        changes = watcher.check()
        assert not changes.devices_changed
        assert changes.env_changed is None or changes.env_changed == []


# === check_restart_required tests ===


class TestCheckRestartRequired:
    """Tests for restart-required detection."""

    def test_no_restart_for_safe_keys(self) -> None:
        """LOAD_MANAGE_ENABLED, DRY_RUN, etc. don't require restart."""
        result = check_restart_required(["LOAD_MANAGE_ENABLED", "LOAD_TARGET_WH", "DRY_RUN"])
        assert not result

    def test_restart_for_tesla_credentials(self) -> None:
        """TESLA_CLIENT_ID requires restart."""
        result = check_restart_required(["TESLA_CLIENT_ID"])
        assert result == ["TESLA_CLIENT_ID"]

    def test_restart_for_mqtt_settings(self) -> None:
        """MQTT_HOST requires restart."""
        result = check_restart_required(["MQTT_HOST", "MQTT_PORT"])
        assert "MQTT_HOST" in result
        assert "MQTT_PORT" in result

    def test_restart_for_controller_type(self) -> None:
        """LOAD_PLUG_CONTROLLER requires restart."""
        result = check_restart_required(["LOAD_PLUG_CONTROLLER"])
        assert result == ["LOAD_PLUG_CONTROLLER"]

    def test_mixed_keys(self) -> None:
        """Mix of safe and restart-required keys."""
        result = check_restart_required([
            "LOAD_MANAGE_ENABLED",  # safe
            "TESLA_CLIENT_ID",      # restart
            "MQTT_HOST",            # restart
            "DRY_RUN",              # safe
        ])
        assert sorted(result) == ["MQTT_HOST", "TESLA_CLIENT_ID"]


# === LoadManager.reload_config tests ===


class TestLoadManagerReloadConfig:
    """Tests for LoadManager.reload_config() — devices.json hot-reload."""

    @pytest.fixture()
    def lm(self) -> MagicMock:
        """Create a mock LoadManager with realistic attributes."""
        from load_models import PlugConfig
        from load_nbc import GapMinder

        mgr = MagicMock()
        mgr.target_wh = -50
        mgr.nbc_device = "EM1-001"
        mgr.tesla_config = None
        mgr._telegram_devices = None
        mgr._telegram_alert_on_auth_error = True
        mgr.engine = GapMinder(hysteresis_wh=50)
        mgr.plugs = {
            "pool_pump": PlugConfig(
                name="pool_pump",
                accessory_id="192.168.1.100",
                power_watts=1500.0,
                priority=1,
            ),
        }
        mgr.plug_ctrl = MagicMock()
        mgr.plug_ctrl.plugs = mgr.plugs
        mgr.sentinel_names = frozenset()
        mgr._cfg = MagicMock()
        mgr._cfg.timezone = "America/Los_Angeles"
        return mgr

    def test_no_changes_when_devices_unchanged(self, lm: MagicMock) -> None:
        """No devices.json change → empty list."""
        from load_manager import LoadManager

        with patch("device_config.get_target_wh", return_value=-50), \
             patch("device_config.get_smartmeter_device", return_value="EM1-001"), \
             patch("device_config.get_telegram_config", return_value=None), \
             patch("load_manager.load_plugs_from_file", return_value=lm.plugs), \
             patch("load_manager.load_vocolinc_plugs_from_file", return_value={}), \
             patch("load_manager.load_tesla_config", return_value=None):
            changes = LoadManager.reload_config(lm)
            assert not changes

    def test_detects_target_wh_change(self, lm: MagicMock) -> None:
        """target_wh changed in devices.json → detected."""
        from load_manager import LoadManager

        with patch("device_config.get_target_wh", return_value=-100), \
             patch("device_config.get_smartmeter_device", return_value="EM1-001"), \
             patch("device_config.get_telegram_config", return_value=None), \
             patch("load_manager.load_plugs_from_file", return_value=lm.plugs), \
             patch("load_manager.load_vocolinc_plugs_from_file", return_value={}), \
             patch("load_manager.load_tesla_config", return_value=None):
            changes = LoadManager.reload_config(lm)
            assert any("target_wh" in c for c in changes)
            assert lm.target_wh == -100

    def test_detects_nbc_device_change(self, lm: MagicMock) -> None:
        """nbc_device changed → detected."""
        from load_manager import LoadManager

        with patch("device_config.get_target_wh", return_value=-50), \
             patch("device_config.get_smartmeter_device", return_value="EM2-999"), \
             patch("device_config.get_telegram_config", return_value=None), \
             patch("load_manager.load_plugs_from_file", return_value=lm.plugs), \
             patch("load_manager.load_vocolinc_plugs_from_file", return_value={}), \
             patch("load_manager.load_tesla_config", return_value=None):
            changes = LoadManager.reload_config(lm)
            assert any("nbc_device" in c for c in changes)
            assert lm.nbc_device == "EM2-999"

    def test_detects_new_plug(self, lm: MagicMock) -> None:
        """New plug added to devices.json → detected."""
        from load_manager import LoadManager
        from load_models import PlugConfig

        new_plugs = {
            "pool_pump": PlugConfig(
                name="pool_pump", accessory_id="192.168.1.100",
                power_watts=1500.0, priority=1,
            ),
            "water_heater": PlugConfig(
                name="water_heater", accessory_id="192.168.1.101",
                power_watts=4500.0, priority=2,
            ),
        }
        with patch("device_config.get_target_wh", return_value=-50), \
             patch("device_config.get_smartmeter_device", return_value="EM1-001"), \
             patch("device_config.get_telegram_config", return_value=None), \
             patch("load_manager.load_plugs_from_file", return_value=new_plugs), \
             patch("load_manager.load_vocolinc_plugs_from_file", return_value={}), \
             patch("load_manager.load_tesla_config", return_value=None):
            changes = LoadManager.reload_config(lm)
            assert any("plugs" in c for c in changes)
            assert "water_heater" in lm.plugs

    def test_updates_gapminder_on_tesla_config_change(self, lm: MagicMock) -> None:
        """Tesla config changed → GapMinder updated."""
        from load_manager import LoadManager
        from load_models import TeslaConfig

        new_tesla = TeslaConfig(
            client_id="new_id",
            client_secret="new_secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="5YJ3E1EA1NF000001",
            charge_amps_min=8,
            charge_amps_max=32,
        )
        with patch("device_config.get_target_wh", return_value=-50), \
             patch("device_config.get_smartmeter_device", return_value="EM1-001"), \
             patch("device_config.get_telegram_config", return_value=None), \
             patch("load_manager.load_plugs_from_file", return_value=lm.plugs), \
             patch("load_manager.load_vocolinc_plugs_from_file", return_value={}), \
             patch("load_manager.load_tesla_config", return_value=new_tesla):
            changes = LoadManager.reload_config(lm)
            assert any("tesla" in c for c in changes)
            assert lm.tesla_config == new_tesla

    def test_updates_telegram_devices_whitelist(self, lm: MagicMock) -> None:
        """Telegram devices whitelist change → detected."""
        from load_manager import LoadManager

        tg_config = {
            "alert_on_auth_error": False,
            "devices": {"pool_pump": ["turn_on", "turn_off"]},
        }
        with patch("device_config.get_target_wh", return_value=-50), \
             patch("device_config.get_smartmeter_device", return_value="EM1-001"), \
             patch("device_config.get_telegram_config", return_value=tg_config), \
             patch("load_manager.load_plugs_from_file", return_value=lm.plugs), \
             patch("load_manager.load_vocolinc_plugs_from_file", return_value={}), \
             patch("load_manager.load_tesla_config", return_value=None):
            changes = LoadManager.reload_config(lm)
            assert any("telegram" in c for c in changes)
            assert lm._telegram_devices == {"pool_pump": {"turn_on", "turn_off"}}
            assert lm._telegram_alert_on_auth_error is False

    def test_reload_calls_device_config_reload(self, lm: MagicMock) -> None:
        """reload_config() calls device_config.reload() first."""
        from load_manager import LoadManager

        with patch("device_config.reload") as mock_reload, \
             patch("device_config.get_target_wh", return_value=-50), \
             patch("device_config.get_smartmeter_device", return_value="EM1-001"), \
             patch("device_config.get_telegram_config", return_value=None), \
             patch("load_manager.load_plugs_from_file", return_value=lm.plugs), \
             patch("load_manager.load_vocolinc_plugs_from_file", return_value={}), \
             patch("load_manager.load_tesla_config", return_value=None):
            LoadManager.reload_config(lm)
            mock_reload.assert_called_once()

    def test_plug_ctrl_plugs_updated(self, lm: MagicMock) -> None:
        """Controller's plugs dict is updated on change."""
        from load_manager import LoadManager
        from load_models import PlugConfig

        new_plugs = {
            "pool_pump": PlugConfig(
                name="pool_pump", accessory_id="192.168.1.100",
                power_watts=2000.0, priority=1,
            ),
        }
        with patch("device_config.get_target_wh", return_value=-50), \
             patch("device_config.get_smartmeter_device", return_value="EM1-001"), \
             patch("device_config.get_telegram_config", return_value=None), \
             patch("load_manager.load_plugs_from_file", return_value=new_plugs), \
             patch("load_manager.load_vocolinc_plugs_from_file", return_value={}), \
             patch("load_manager.load_tesla_config", return_value=None):
            LoadManager.reload_config(lm)
            assert lm.plug_ctrl.plugs == new_plugs
