"""Tests for device_config module — JSON-based device configuration."""

from pathlib import Path
from unittest.mock import patch

import pytest
import device_config


@pytest.fixture(autouse=True)
def clear_cache():
    """Ensure each test starts with a fresh cache."""
    device_config.reload()
    yield
    # Clean up: restore empty cache after test
    device_config.reload()


# --- File loading tests ---


def test_load_missing_file_returns_defaults(tmp_path):
    """Returns defaults when devices.json doesn't exist."""
    fake_path = tmp_path / "nonexistent.json"
    with patch.object(device_config, "_DEVICES_FILE", fake_path):
        device_config.reload()

        assert device_config.get_timezone() == "America/Los_Angeles"
        assert device_config.get_smartmeter_device() == ""
        assert device_config.get_target_wh() == -50
        assert device_config.get_homekit_plugs() == []
        assert device_config.get_vocolinc_plugs() == []
        assert device_config.get_tesla_config() is None


def test_load_malformed_json_raises_error(tmp_path):
    """Raises DeviceConfigError when devices.json exists and is non-empty but invalid."""
    fake_file = tmp_path / "devices.json"
    fake_file.write_text("{invalid json content")

    with patch.object(device_config, "_DEVICES_FILE", fake_file):
        device_config.reload()
        with pytest.raises(device_config.DeviceConfigError, match="invalid JSON"):
            device_config.get_timezone()


def test_load_valid_file(tmp_path):
    """Reads all fields from a valid devices.json."""
    fake_file = tmp_path / "devices.json"
    fake_file.write_text("""{
        "timezone": "America/New_York",
        "smartmeter": {
            "device": "EM1-ABCDEF",
            "target_wh": -750
        },
        "plugs": {
            "homekit": [
                {"name": "heater", "accessory_id": "hk123",
                 "power_watts": 4500, "priority": 10}
            ],
            "vocolinc": [
                {"name": "lamp", "device_name": "LivingRoomLamp",
                 "power_watts": 60}
            ]
        },
        "tesla": {
            "vehicle_id": "5YJ3E1EA4KF123456",
            "redirect_uri": "http://localhost:8000/callback",
            "home_lat": 37.0,
            "home_lon": -122.0,
            "charge_amps_min": 10,
            "charge_amps_max": 40
        }
    }""")

    with patch.object(device_config, "_DEVICES_FILE", fake_file):
        device_config.reload()

        assert device_config.get_timezone() == "America/New_York"
        assert device_config.get_smartmeter_device() == "EM1-ABCDEF"
        assert device_config.get_target_wh() == -750

        plugs = device_config.get_homekit_plugs()
        assert len(plugs) == 1
        assert plugs[0]["name"] == "heater"

        vplugs = device_config.get_vocolinc_plugs()
        assert len(vplugs) == 1
        assert vplugs[0]["device_name"] == "LivingRoomLamp"

        tesla = device_config.get_tesla_config()
        assert tesla is not None
        assert tesla["vehicle_id"] == "5YJ3E1EA4KF123456"
        assert tesla["charge_amps_min"] == 10


def test_cache_persists_across_calls(tmp_path):
    """Data is cached after first load."""
    fake_file = tmp_path / "devices.json"
    fake_file.write_text('{"timezone": "Europe/London"}')

    with patch.object(device_config, "_DEVICES_FILE", fake_file):
        device_config.reload()
        # First call loads and caches
        tz1 = device_config.get_timezone()
        # Modify file after cache is set
        fake_file.write_text('{"timezone": "Asia/Tokyo"}')
        # Second call should return cached value
        tz2 = device_config.get_timezone()

    assert tz1 == "Europe/London"
    assert tz2 == "Europe/London"  # Still cached


def test_reload_clears_cache(tmp_path):
    """reload() forces re-read of the file."""
    fake_file = tmp_path / "devices.json"
    fake_file.write_text('{"timezone": "Europe/London"}')

    with patch.object(device_config, "_DEVICES_FILE", fake_file):
        device_config.reload()
        tz1 = device_config.get_timezone()

        # Modify file and reload
        fake_file.write_text('{"timezone": "Asia/Tokyo"}')
        device_config.reload()
        tz2 = device_config.get_timezone()

    assert tz1 == "Europe/London"
    assert tz2 == "Asia/Tokyo"


# --- Accessor defaults tests ---


def test_get_timezone_default():
    """Defaults to America/Los_Angeles when not configured."""
    with patch.object(device_config, "_DEVICES_FILE", Path("/nonexistent")):
        device_config.reload()

    assert device_config.get_timezone() == "America/Los_Angeles"


def test_get_smartmeter_device_default():
    """Returns empty string when smartmeter section is missing."""
    with patch("device_config._load", return_value={}):
        assert device_config.get_smartmeter_device() == ""


def test_get_target_wh_default():
    """Defaults to -50 when not configured."""
    with patch("device_config._load", return_value={}):
        assert device_config.get_target_wh() == -50


def test_get_homekit_plugs_default():
    """Returns empty list when no plugs configured."""
    with patch("device_config._load", return_value={}):
        assert device_config.get_homekit_plugs() == []


def test_get_vocolinc_plugs_default():
    """Returns empty list when no vocolinc plugs configured."""
    with patch("device_config._load", return_value={
        "plugs": {"homekit": [{"name": "test"}]}
    }):
        assert device_config.get_vocolinc_plugs() == []


def test_get_tesla_config_none_when_missing():
    """Returns None when tesla section is absent."""
    with patch("device_config._load", return_value={}):
        assert device_config.get_tesla_config() is None


def test_get_tesla_config_returns_section_even_without_vehicle_id():
    """Returns the tesla section even when vehicle_id is missing.

    vehicle_id may come from env vars rather than devices.json.
    The amp limits (charge_amps_min/max) must still be readable.
    """
    with patch("device_config._load", return_value={
        "tesla": {"charge_amps_min": 5, "charge_amps_max": 24}
    }):
        result = device_config.get_tesla_config()
    assert result is not None
    assert result["charge_amps_min"] == 5
    assert result["charge_amps_max"] == 24


def test_get_tesla_config_returns_section_with_vehicle_id():
    """Returns the full tesla section when vehicle_id is present."""
    with patch("device_config._load", return_value={
        "tesla": {
            "vehicle_id": "5YJ3E1EA4KF123456",
            "home_lat": 37.0,
        }
    }):
        result = device_config.get_tesla_config()

    assert result is not None
    assert result["vehicle_id"] == "5YJ3E1EA4KF123456"
    assert result["home_lat"] == 37.0


def test_get_tesla_config_returns_section_with_empty_vehicle_id():
    """Returns the tesla section even when vehicle_id is empty string.

    The empty-vehicle_id case should be treated the same as missing —
    the section still exists and its other fields are valid.
    """
    with patch("device_config._load", return_value={
        "tesla": {"vehicle_id": "", "charge_amps_max": 32}
    }):
        result = device_config.get_tesla_config()
    assert result is not None
    assert result["charge_amps_max"] == 32


def test_get_telegram_config_returns_section_when_present():
    """Returns the telegram section when it exists."""
    telegram_data = {
        "enabled": True,
        "bot_token": "123:ABC",
        "chat_id": "-100123456",
    }
    with patch("device_config._load", return_value={"telegram": telegram_data}):
        result = device_config.get_telegram_config()
    assert result == telegram_data


def test_get_telegram_config_returns_none_when_missing():
    """Returns None when telegram section is absent."""
    with patch("device_config._load", return_value={}):
        assert device_config.get_telegram_config() is None


def test_get_telegram_config_returns_none_when_empty_section():
    """Returns None when telegram section exists but is empty dict."""
    with patch("device_config._load", return_value={"telegram": {}}):
        assert device_config.get_telegram_config() is None


# --- New public accessor tests ---


def test_has_smartmeter_true():
    """Returns True when smartmeter section with target_wh exists."""
    with patch("device_config._load", return_value={
        "smartmeter": {"device": "EM1-ABC", "target_wh": -750}
    }):
        assert device_config.has_smartmeter() is True


def test_has_smartmeter_false_no_target_wh():
    """Returns False when smartmeter exists but has no target_wh."""
    with patch("device_config._load", return_value={
        "smartmeter": {"device": "EM1-ABC"}
    }):
        assert device_config.has_smartmeter() is False


def test_has_smartmeter_false_when_missing():
    """Returns False when smartmeter section is absent."""
    with patch("device_config._load", return_value={}):
        assert device_config.has_smartmeter() is False


def test_get_all_plugs_returns_plugs_section():
    """Returns the full plugs section from devices.json."""
    plugs_data = {
        "homekit": [{"name": "heater", "accessory_id": "hk123"}],
        "vocolinc": [{"name": "lamp", "device_name": "Lamp1"}],
    }
    with patch("device_config._load", return_value={"plugs": plugs_data}):
        result = device_config.get_all_plugs()
    assert result == plugs_data


def test_get_all_plugs_returns_empty_when_missing():
    """Returns empty dict when plugs section is absent."""
    with patch("device_config._load", return_value={}):
        result = device_config.get_all_plugs()
    assert result == {}


def test_get_all_returns_full_config():
    """Returns the complete devices.json contents."""
    full_config = {
        "timezone": "America/New_York",
        "smartmeter": {"device": "EM1-ABC", "target_wh": -750},
        "plugs": {"homekit": []},
        "tesla": {"vehicle_id": "123"},
    }
    with patch("device_config._load", return_value=full_config):
        result = device_config.get_all()
    assert result == full_config


def test_load_empty_file_raises_error(tmp_path):
    """Raises an error when devices.json exists but is empty."""
    fake_file = tmp_path / "devices.json"
    fake_file.write_text("")

    with patch.object(device_config, "_DEVICES_FILE", fake_file):
        device_config.reload()
        with pytest.raises(device_config.DeviceConfigError):
            device_config.get_timezone()


def test_load_whitespace_only_file_raises_error(tmp_path):
    """Raises an error when devices.json exists but contains only whitespace."""
    fake_file = tmp_path / "devices.json"
    fake_file.write_text("   \n  \t  ")

    with patch.object(device_config, "_DEVICES_FILE", fake_file):
        device_config.reload()
        with pytest.raises(device_config.DeviceConfigError):
            device_config.get_timezone()
