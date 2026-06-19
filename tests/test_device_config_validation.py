"""Tests for device_config integrity validations.

Covers:
  - validate_telegram_devices() — ensures telegram.devices keys match plug names
"""

from __future__ import annotations

import pytest

import device_config


# =============================================================================
# Helpers
# =============================================================================


def _make_config(
    homekit_plugs: list[dict] | None = None,
    vocolinc_plugs: list[dict] | None = None,
    telegram_devices: dict[str, list[str]] | None = None,
) -> dict:
    """Build a minimal devices.json config dict for validation tests.

    Args:
        homekit_plugs: List of homekit plug entries (each with a 'name' key).
        vocolinc_plugs: List of vocolinc plug entries (each with a 'name' key).
        telegram_devices: Dict mapping device names to action lists, or None.

    Returns:
        A config dict suitable for passing to validate_telegram_devices().
    """
    config: dict = {}

    if homekit_plugs is not None or vocolinc_plugs is not None:
        plugs: dict = {}
        if homekit_plugs is not None:
            plugs["homekit"] = list(homekit_plugs)
        if vocolinc_plugs is not None:
            plugs["vocolinc"] = list(vocolinc_plugs)
        config["plugs"] = plugs

    if telegram_devices is not None:
        config["telegram"] = {"devices": telegram_devices}

    return config


# =============================================================================
# Tests — happy paths (no error raised)
# =============================================================================


class TestValidateTelegramDevicesHappyPaths:

    def test_telegram_devices_empty_dict(self):
        """Empty devices dict is valid — nothing to validate."""
        config = _make_config(telegram_devices={})
        telegram_section = config["telegram"]
        # Should not raise
        device_config.validate_telegram_devices(config, telegram_section)

    def test_telegram_devices_no_telegram_section(self):
        """No telegram section at all — nothing to validate."""
        config = _make_config()
        telegram_section = config.get("telegram")
        # Should not raise
        device_config.validate_telegram_devices(config, telegram_section)

    def test_telegram_devices_no_devices_key(self):
        """Telegram section exists but has no 'devices' key — nothing to validate."""
        config = _make_config(telegram_devices=None)
        config["telegram"] = {"bot_token": "abc", "chat_id": "123"}
        telegram_section = config["telegram"]
        # Should not raise
        device_config.validate_telegram_devices(config, telegram_section)

    def test_telegram_devices_all_match_homekit(self):
        """All telegram device keys match homekit plug names."""
        homekit = [
            {"name": "pool_pump", "accessory_id": "1", "power_watts": 1000},
            {"name": "water_heater", "accessory_id": "2", "power_watts": 4500},
        ]
        config = _make_config(
            homekit_plugs=homekit,
            telegram_devices={
                "pool_pump": ["turn_on", "turn_off"],
                "water_heater": ["turn_on"],
            },
        )
        telegram_section = config["telegram"]
        # Should not raise
        device_config.validate_telegram_devices(config, telegram_section)

    def test_telegram_devices_all_match_vocolinc(self):
        """All telegram device keys match vocolinc plug names."""
        vocolinc = [
            {"name": "floor_lamp", "device_name": "Lamp1", "power_watts": 60},
        ]
        config = _make_config(
            vocolinc_plugs=vocolinc,
            telegram_devices={
                "floor_lamp": ["turn_on"],
            },
        )
        telegram_section = config["telegram"]
        # Should not raise
        device_config.validate_telegram_devices(config, telegram_section)

    def test_telegram_devices_case_insensitive(self):
        """Telegram device names are matched case-insensitively against plug names."""
        homekit = [
            {"name": "Pool Pump", "accessory_id": "1", "power_watts": 1000},
        ]
        config = _make_config(
            homekit_plugs=homekit,
            telegram_devices={
                "pool pump": ["turn_on"],       # lowercase
                "WATER_HEATER": ["turn_off"],    # uppercase — no matching plug
            },
        )
        # Fix: add a matching water_heater
        config = _make_config(
            homekit_plugs=[
                {"name": "Pool Pump", "accessory_id": "1", "power_watts": 1000},
                {"name": "Water Heater", "accessory_id": "2", "power_watts": 4500},
            ],
            telegram_devices={
                "pool pump": ["turn_on"],          # lowercase of "Pool Pump"
                "water heater": ["turn_off"],      # lowercase of "Water Heater"
            },
        )
        telegram_section = config["telegram"]
        # Should not raise
        device_config.validate_telegram_devices(config, telegram_section)

    def test_telegram_devices_mixed_sources(self):
        """Device keys span both homekit and vocolinc plugs."""
        homekit = [
            {"name": "water_heater", "accessory_id": "1", "power_watts": 4500},
        ]
        vocolinc = [
            {"name": "floor_lamp", "device_name": "Lamp1", "power_watts": 60},
        ]
        config = _make_config(
            homekit_plugs=homekit,
            vocolinc_plugs=vocolinc,
            telegram_devices={
                "water_heater": ["turn_on", "turn_off"],
                "floor_lamp": ["turn_on"],
            },
        )
        telegram_section = config["telegram"]
        # Should not raise
        device_config.validate_telegram_devices(config, telegram_section)


# =============================================================================
# Tests — error paths (DeviceConfigError raised)
# =============================================================================


class TestValidateTelegramDevicesErrors:

    def test_telegram_devices_unmatched_device(self):
        """One device key not found in any plug list raises DeviceConfigError."""
        homekit = [
            {"name": "water_heater", "accessory_id": "1", "power_watts": 4500},
        ]
        config = _make_config(
            homekit_plugs=homekit,
            telegram_devices={
                "water_heater": ["turn_on"],
                "UnknownDevice": ["turn_on"],
            },
        )
        telegram_section = config["telegram"]
        with pytest.raises(device_config.DeviceConfigError) as exc_info:
            device_config.validate_telegram_devices(config, telegram_section)
        assert "UnknownDevice" in str(exc_info.value)

    def test_telegram_devices_multiple_unmatched(self):
        """Two device keys not in any plug list — both listed in the error."""
        homekit = [
            {"name": "water_heater", "accessory_id": "1", "power_watts": 4500},
        ]
        config = _make_config(
            homekit_plugs=homekit,
            telegram_devices={
                "water_heater": ["turn_on"],
                "UnknownDevice1": ["turn_on"],
                "UnknownDevice2": ["turn_off"],
            },
        )
        telegram_section = config["telegram"]
        with pytest.raises(device_config.DeviceConfigError) as exc_info:
            device_config.validate_telegram_devices(config, telegram_section)
        error_str = str(exc_info.value)
        assert "UnknownDevice1" in error_str
        assert "UnknownDevice2" in error_str

    def test_telegram_devices_error_message_includes_plug_names(self):
        """Error message lists available plug names."""
        homekit = [
            {"name": "water_heater", "accessory_id": "1", "power_watts": 4500},
        ]
        config = _make_config(
            homekit_plugs=homekit,
            telegram_devices={
                "water_heater": ["turn_on"],
                "UnknownDevice": ["turn_on"],
            },
        )
        telegram_section = config["telegram"]
        with pytest.raises(device_config.DeviceConfigError) as exc_info:
            device_config.validate_telegram_devices(config, telegram_section)
        error_str = str(exc_info.value)
        assert "water_heater" in error_str

    def test_telegram_devices_all_unmatched(self):
        """All telegram device keys are unmatched — all listed."""
        config = _make_config(
            homekit_plugs=[
                {"name": "water_heater", "accessory_id": "1", "power_watts": 4500},
            ],
            telegram_devices={
                "UnknownDevice1": ["turn_on"],
                "UnknownDevice2": ["turn_off"],
            },
        )
        telegram_section = config["telegram"]
        with pytest.raises(device_config.DeviceConfigError) as exc_info:
            device_config.validate_telegram_devices(config, telegram_section)
        error_str = str(exc_info.value)
        assert "UnknownDevice1" in error_str
        assert "UnknownDevice2" in error_str

    def test_telegram_devices_empty_plugs_all_unmatched(self):
        """No plugs configured and telegram.devices is non-empty — all unmatched."""
        config = _make_config(
            telegram_devices={
                "water_heater": ["turn_on"],
            },
        )
        telegram_section = config["telegram"]
        with pytest.raises(device_config.DeviceConfigError) as exc_info:
            device_config.validate_telegram_devices(config, telegram_section)
        assert "water_heater" in str(exc_info.value)
