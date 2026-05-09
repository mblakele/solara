"""Tests for load management configuration loading, time-range parsing, and sync."""

import asyncio
from datetime import datetime, time, timedelta, timezone
from unittest.mock import patch

import pytz
import pytest
from decouple import UndefinedValueError, config

from load_manager import (
    AbstractPlugController,
    DeviceState,
    LoadManager,
    PlugConfig,
    PlugController,
    TeslaConfig,
    TeslaState,
    _parse_load_manage_enabled,
    load_plugs_from_file,
    load_tesla_config,
)
from load_models import TeslaAuthError
import device_config
from metrics import EnergyCache
from tests.helpers import _make_metrics_with_wh


# --- Config loading tests ---


def test_load_plugs_valid():
    """Loads valid plug configs."""
    with patch("device_config._load", return_value={
        "plugs": {
            "homekit": [
                {"name": "water_heater", "accessory_id": "abc123",
                 "power_watts": 4500, "role": "flexible", "priority": 10},
                {"name": "pool_pump", "accessory_id": "xyz789",
                 "power_watts": 1500, "role": "flexible", "priority": 20},
            ]
        }
    }):
        device_config.reload()
        plugs = load_plugs_from_file()

    assert len(plugs) == 2
    assert plugs["water_heater"].power_watts == 4500.0
    assert plugs["water_heater"].role == "flexible"
    assert plugs["water_heater"].priority == 10


def test_load_plugs_default_priority():
    """Defaults priority to 0."""
    with patch("device_config._load", return_value={
        "plugs": {
            "homekit": [
                {"name": "test", "accessory_id": "abc",
                 "power_watts": 1000, "role": "flexible"},
            ]
        }
    }):
        device_config.reload()
        plugs = load_plugs_from_file()

    assert plugs["test"].priority == 0


def test_load_plugs_invalid_role():
    """Skips plugs with invalid role."""
    with patch("device_config._load", return_value={
        "plugs": {
            "homekit": [
                {"name": "test", "accessory_id": "abc",
                 "power_watts": 1000, "role": "invalid"},
            ]
        }
    }):
        device_config.reload()
        plugs = load_plugs_from_file()

    assert len(plugs) == 0


def test_load_plugs_invalid_format():
    """Skips entries missing required fields."""
    with patch("device_config._load", return_value={
        "plugs": {
            "homekit": [
                {"name": "test"},  # missing accessory_id, power_watts, role
            ]
        }
    }):
        device_config.reload()
        with pytest.raises(KeyError):
            load_plugs_from_file()


def test_load_tesla_config_all_vars():
    """Loads Tesla config when all vars present."""
    config.set("TESLA_CLIENT_ID", "client-id")
    config.set("TESLA_CLIENT_SECRET", "client-secret")

    with patch("device_config._load", return_value={
        "tesla": {
            "vehicle_id": "vehicle-123",
            "redirect_uri": "http://localhost/callback",
            "home_lat": 37.7749,
            "home_lon": -122.4194,
        }
    }):
        device_config.reload()
        tesla_config = load_tesla_config()

    assert tesla_config is not None
    assert tesla_config.client_id == "client-id"
    assert tesla_config.home_lat == 37.7749


def test_load_tesla_config_missing_vars():
    """Returns None when required vars missing."""

    def mock_decouple(key, default=None, cast=str):  # type: ignore[no-untyped-def]
        if key == "TESLA_CLIENT_ID":
            return ""  # empty client_id means missing
        if default is not None:
            return default
        raise UndefinedValueError(key)

    with patch("config._decouple_config", side_effect=mock_decouple):
        config_result = load_tesla_config()

    assert config_result is None


def test_load_tesla_config_optionals():
    """Loads optional vars with defaults."""
    config.set("TESLA_CLIENT_ID", "client-id")
    config.set("TESLA_CLIENT_SECRET", "client-secret")

    with patch("device_config._load", return_value={
        "tesla": {
            "vehicle_id": "vehicle-123",
            "redirect_uri": "http://localhost/callback",
            "home_lat": 37.7749,
            "home_lon": -122.4194,
            "charge_amps_min": 8,
            "charge_amps_max": 32,
        }
    }):
        device_config.reload()
        tesla_config = load_tesla_config()

    assert tesla_config.charge_amps_min == 8
    assert tesla_config.charge_amps_max == 32


# --- Parse load manage enabled tests ---


def test_parse_true_variants():
    """Accepts various true-like strings."""
    assert _parse_load_manage_enabled("True") is True
    assert _parse_load_manage_enabled("true") is True
    assert _parse_load_manage_enabled("TRUE") is True
    assert _parse_load_manage_enabled("1") is True
    assert _parse_load_manage_enabled("yes") is True


def test_parse_false_variants():
    """Accepts various false-like strings."""
    assert _parse_load_manage_enabled("False") is False
    assert _parse_load_manage_enabled("false") is False
    assert _parse_load_manage_enabled("FALSE") is False
    assert _parse_load_manage_enabled("0") is False
    assert _parse_load_manage_enabled("no") is False
    assert _parse_load_manage_enabled("") is False


def test_parse_time_range():
    """Parses HH:MM-HH:MM format correctly."""
    result = _parse_load_manage_enabled("06:45-15:00")
    assert isinstance(result, tuple)
    assert len(result) == 2
    start, end = result
    assert start == time(hour=6, minute=45)
    assert end == time(hour=15, minute=0)


def test_parse_time_range_single_digit_hour():
    """Accepts single-digit hours without leading zero."""
    result = _parse_load_manage_enabled("7:30-23:45")
    start, end = result
    assert start == time(hour=7, minute=30)
    assert end == time(hour=23, minute=45)


def test_parse_time_range_midnight():
    """Handles midnight boundaries."""
    result = _parse_load_manage_enabled("00:00-12:00")
    start, end = result
    assert start == time(hour=0, minute=0)
    assert end == time(hour=12, minute=0)


def test_parse_invalid_value_raises():
    """Raises ValueError for unrecognized format."""
    with pytest.raises(ValueError):
        _parse_load_manage_enabled("maybe")


def test_parse_invalid_time_range_format():
    """Raises ValueError for malformed time range."""
    with pytest.raises(ValueError):
        _parse_load_manage_enabled("6am-3pm")


def test_parse_whitespace_stripped():
    """Leading/trailing whitespace is ignored."""
    result = _parse_load_manage_enabled("  True  ")
    assert result is True
    result = _parse_load_manage_enabled("  06:45-15:00  ")
    start, _end = result
    assert start == time(hour=6, minute=45)


def test_parse_bool_true():
    """Accepts a Python bool True (from Config.load_manage_enabled)."""
    assert _parse_load_manage_enabled(True) is True


def test_parse_bool_false():
    """Accepts a Python bool False (from Config.load_manage_enabled)."""
    assert _parse_load_manage_enabled(False) is False


# --- Load manager time range tests ---


def _make_manager_with_enabled(
    enabled, predicted_wh: float = -2000.0
) -> LoadManager:
    """Create a LoadManager with the given enabled value and predicted Wh."""
    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)
    metrics_data = _make_metrics_with_wh("main_panel", "QH3", predicted_wh)

    energy_cache = EnergyCache()
    return LoadManager(
        metrics_fetch=lambda: metrics_data,
        energy_cache=energy_cache,
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=enabled,
        dry_run=False,
    )


def test_is_enabled_at_bool_true():
    """is_enabled_at returns True when enabled is True."""
    mgr = _make_manager_with_enabled(True)
    now = datetime.now(timezone.utc)
    assert mgr.is_enabled_at(now) is True


def test_is_enabled_at_bool_false():
    """is_enabled_at returns False when enabled is False."""
    mgr = _make_manager_with_enabled(False)
    now = datetime.now(timezone.utc)
    assert mgr.is_enabled_at(now) is False


@patch("config._decouple_config")
def test_is_enabled_at_in_range(mock_config):
    """is_enabled_at returns True when current time is in range."""
    mock_config.return_value = "America/Los_Angeles"
    mgr = _make_manager_with_enabled((time(6, 0), time(18, 0)))
    tz = pytz.timezone("America/Los_Angeles")
    now = tz.localize(datetime(2025, 6, 15, 12, 0, 0))
    assert mgr.is_enabled_at(now) is True


@patch("config._decouple_config")
def test_is_enabled_at_before_range(mock_config):
    """is_enabled_at returns False when current time is before range."""
    mock_config.return_value = "America/Los_Angeles"
    mgr = _make_manager_with_enabled((time(6, 0), time(18, 0)))
    tz = pytz.timezone("America/Los_Angeles")
    now = tz.localize(datetime(2025, 6, 15, 3, 0, 0))
    assert mgr.is_enabled_at(now) is False


@patch("config._decouple_config")
def test_is_enabled_at_after_range(mock_config):
    """is_enabled_at returns False when current time is after range."""
    mock_config.return_value = "America/Los_Angeles"
    mgr = _make_manager_with_enabled((time(6, 0), time(18, 0)))
    tz = pytz.timezone("America/Los_Angeles")
    now = tz.localize(datetime(2025, 6, 15, 21, 0, 0))
    assert mgr.is_enabled_at(now) is False


@patch("config._decouple_config")
def test_is_enabled_at_inclusive_start(mock_config):
    """is_enabled_at returns True exactly at start time."""
    mock_config.return_value = "America/Los_Angeles"
    mgr = _make_manager_with_enabled((time(6, 0), time(18, 0)))
    tz = pytz.timezone("America/Los_Angeles")
    now = tz.localize(datetime(2025, 6, 15, 6, 0, 0))
    assert mgr.is_enabled_at(now) is True


@patch("config._decouple_config")
def test_is_enabled_at_exclusive_end(mock_config):
    """is_enabled_at returns False exactly at end time."""
    mock_config.return_value = "America/Los_Angeles"
    mgr = _make_manager_with_enabled((time(6, 0), time(18, 0)))
    tz = pytz.timezone("America/Los_Angeles")
    now = tz.localize(datetime(2025, 6, 15, 18, 0, 0))
    assert mgr.is_enabled_at(now) is False


@patch("config._decouple_config")
def test_run_cycle_disabled_outside_range(mock_config):
    """run_cycle returns disabled when outside time range."""
    mock_config.return_value = "America/Los_Angeles"
    mgr = _make_manager_with_enabled((time(6, 0), time(18, 0)))

    tz = pytz.timezone("America/Los_Angeles")
    fake_now = tz.localize(datetime(2025, 6, 15, 3, 0, 0)).astimezone(timezone.utc)
    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.timedelta = timedelta
        result = mgr.run_cycle()

    assert result["status"] == "disabled"
    assert "outside_time_range" in result["diagnostics"]["reason"]


@patch("config._decouple_config")
def test_run_cycle_enabled_in_range(mock_config):
    """run_cycle proceeds when inside time range."""
    mock_config.return_value = "America/Los_Angeles"
    mgr = _make_manager_with_enabled(
        (time(6, 0), time(18, 0)), predicted_wh=-2000.0
    )

    tz = pytz.timezone("America/Los_Angeles")
    fake_now = tz.localize(datetime(2025, 6, 15, 12, 0, 0)).astimezone(timezone.utc)
    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.timedelta = timedelta
        result = mgr.run_cycle()

    assert result["status"] != "disabled"


# --- Sync plug states tests ---


def test_sync_reconciles_external_turn_off():
    """When a plug is externally turned off, desired_state is reconciled."""
    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=2000.0,
            role="flexible",
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)
    plug_ctrl._state["heater"] = False

    mgr = LoadManager(
        metrics_fetch=lambda: _make_metrics_with_wh("main_panel", "QH3", -2000.0),
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )
    mgr.state.devices["heater"] = DeviceState(
        name="heater", desired_state=True, actual_state=True
    )

    asyncio.run(mgr._sync_plug_states())

    dev = mgr.state.devices["heater"]
    assert dev.actual_state is False
    assert dev.desired_state is False


def test_sync_populates_new_device():
    """Sync creates DeviceState entry for a plug not yet tracked."""
    plugs = {
        "pump": PlugConfig(
            name="pump",
            accessory_id="p1",
            power_watts=500.0,
            role="flexible",
            priority=5,
        ),
    }
    plug_ctrl = PlugController(plugs)
    plug_ctrl._state["pump"] = True

    mgr = LoadManager(
        metrics_fetch=lambda: _make_metrics_with_wh("main_panel", "QH3", -2000.0),
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )
    assert "pump" not in mgr.state.devices

    asyncio.run(mgr._sync_plug_states())

    dev = mgr.state.devices["pump"]
    assert dev.actual_state is True
    assert dev.desired_state is True


def test_sync_handles_controller_error():
    """Sync continues gracefully when a controller raises an error."""

    class FailingController(AbstractPlugController):
        """Controller that always fails on get_state."""

        def __init__(self, plugs: dict[str, PlugConfig]) -> None:
            self.plugs = plugs

        async def get_state(self, _name: str) -> bool | None:
            """Always raise to simulate network failure."""
            raise RuntimeError("network timeout")

        async def set_state(self, _name: str, _on: bool) -> bool:
            """Always return False since the controller is failing."""
            return False

    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=2000.0,
            role="flexible",
            priority=10,
        ),
    }
    plug_ctrl = FailingController(plugs)

    mgr = LoadManager(
        metrics_fetch=lambda: _make_metrics_with_wh("main_panel", "QH3", -2000.0),
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    asyncio.run(mgr._sync_plug_states())


def test_sync_no_reconciliation_when_states_match():
    """Sync does nothing when desired and actual states already match."""
    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=2000.0,
            role="flexible",
            priority=10,
        ),
    }
    plug_ctrl = PlugController(plugs)
    plug_ctrl._state["heater"] = True

    mgr = LoadManager(
        metrics_fetch=lambda: _make_metrics_with_wh("main_panel", "QH3", -2000.0),
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )
    mgr.state.devices["heater"] = DeviceState(
        name="heater", desired_state=True, actual_state=True
    )

    asyncio.run(mgr._sync_plug_states())

    dev = mgr.state.devices["heater"]
    assert dev.actual_state is True
    assert dev.desired_state is True


# --- Time range midnight wrapping tests ---


@patch("config._decouple_config")
def test_time_range_wraps_midnight(mock_config):
    """A time range like 22:00-06:00 that wraps midnight always returns False
    with the current simple comparison logic (start <= now < end), since no
    time satisfies 22:00 <= t < 06:00."""
    mock_config.return_value = "America/Los_Angeles"
    mgr = _make_manager_with_enabled((time(22, 0), time(6, 0)))

    tz = pytz.timezone("America/Los_Angeles")
    # 23:00 — between start and midnight
    now_23 = tz.localize(datetime(2025, 6, 15, 23, 0, 0))
    assert mgr.is_enabled_at(now_23) is False

    # 01:00 — after midnight, before end
    now_01 = tz.localize(datetime(2025, 6, 16, 1, 0, 0))
    assert mgr.is_enabled_at(now_01) is False


# --- Tesla token persistence tests ---


def test_tesla_tokens_save_and_load(tmp_path):
    """Tokens saved to a file can be loaded back with matching values."""
    from load_manager import save_tesla_tokens, load_tesla_tokens

    tokens_path = tmp_path / "tokens.json"
    save_tesla_tokens(
        refresh_token="refresh-abc",
        access_token="access-def",
        expires=1700000000,
        tokens_path=tokens_path,
    )

    loaded = load_tesla_tokens(tokens_path=tokens_path)

    assert loaded is not None
    assert loaded["refresh_token"] == "refresh-abc"
    assert loaded["access_token"] == "access-def"
    assert loaded["expires"] == 1700000000


def test_remove_tesla_tokens_deletes_file(tmp_path):
    """Removing tokens deletes the file cleanly."""
    from load_manager import save_tesla_tokens, remove_tesla_tokens, load_tesla_tokens

    tokens_path = tmp_path / "tokens.json"
    save_tesla_tokens(
        refresh_token="refresh-abc",
        access_token="access-def",
        expires=1700000000,
        tokens_path=tokens_path,
    )
    assert tokens_path.exists()

    remove_tesla_tokens(tokens_path=tokens_path)

    assert not tokens_path.exists()
    assert load_tesla_tokens(tokens_path=tokens_path) is None


# --- Per-device time range tests ---


def test_load_plug_time_range_parsed():
    """Plug with time_range string parses into (time, time) tuple."""
    with patch("device_config._load", return_value={
        "plugs": {
            "homekit": [
                {"name": "water_heater", "accessory_id": "abc123",
                 "power_watts": 4500, "role": "flexible",
                 "time_range": "10:00-15:00"},
            ]
        }
    }):
        device_config.reload()
        plugs = load_plugs_from_file()

    wh = plugs["water_heater"]
    assert wh.time_range is not None
    assert wh.time_range[0] == time(10, 0)
    assert wh.time_range[1] == time(15, 0)


def test_load_plug_no_time_range():
    """Plug without time_range key has None."""
    with patch("device_config._load", return_value={
        "plugs": {
            "homekit": [
                {"name": "heater", "accessory_id": "abc123",
                 "power_watts": 4500, "role": "flexible"},
            ]
        }
    }):
        device_config.reload()
        plugs = load_plugs_from_file()

    assert plugs["heater"].time_range is None


def test_load_plug_invalid_time_range():
    """Invalid time_range format logs warning and loads as None."""
    with patch("device_config._load", return_value={
        "plugs": {
            "homekit": [
                {"name": "heater", "accessory_id": "abc123",
                 "power_watts": 4500, "role": "flexible",
                 "time_range": "6am-3pm"},
            ]
        }
    }):
        device_config.reload()
        plugs = load_plugs_from_file()

    assert plugs["heater"].time_range is None


def test_load_vocolinc_plug_time_range_parsed():
    """VOCOlinc plug with time_range string parses correctly."""
    from load_manager import load_vocolinc_plugs_from_file

    with patch("device_config._load", return_value={
        "plugs": {
            "vocolinc": [
                {"name": "floor_lamp", "device_name": "LivingRoomLamp",
                 "power_watts": 60, "role": "flexible",
                 "time_range": "18:00-23:00"},
            ]
        }
    }):
        device_config.reload()
        plugs = load_vocolinc_plugs_from_file()

    lamp = plugs["floor_lamp"]
    assert lamp.time_range is not None
    assert lamp.time_range[0] == time(18, 0)
    assert lamp.time_range[1] == time(23, 0)


def test_load_tesla_time_range_parsed():
    """Tesla config with time_range parses correctly."""
    config.set("TESLA_CLIENT_ID", "client-id")
    config.set("TESLA_CLIENT_SECRET", "client-secret")

    with patch("device_config._load", return_value={
        "tesla": {
            "vehicle_id": "vehicle-123",
            "redirect_uri": "http://localhost/callback",
            "home_lat": 37.7749,
            "home_lon": -122.4194,
            "time_range": "08:00-20:00",
        }
    }):
        device_config.reload()
        tesla_config = load_tesla_config()

    assert tesla_config is not None
    assert tesla_config.time_range is not None
    assert tesla_config.time_range[0] == time(8, 0)
    assert tesla_config.time_range[1] == time(20, 0)


def test_is_device_in_time_range_no_restriction():
    """Device with time_range=None always returns True."""
    mgr = _make_manager_with_enabled(True)
    assert mgr._is_device_in_time_range("any_device", None) is True


@patch("config._decouple_config")
def test_is_device_in_time_range_inside(mock_config):
    """Current time within range returns True."""
    mock_config.return_value = "America/Los_Angeles"
    mgr = _make_manager_with_enabled(True)

    # 12:00 PT is inside 06:00-18:00
    tz = pytz.timezone("America/Los_Angeles")
    fake_now = tz.localize(datetime(2025, 6, 15, 12, 0, 0)).astimezone(timezone.utc)

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.timezone = timezone
        mock_dt.timedelta = timedelta
        result = mgr._is_device_in_time_range(
            "heater", (time(6, 0), time(18, 0))
        )

    assert result is True


@patch("config._decouple_config")
def test_is_device_in_time_range_outside(mock_config):
    """Current time outside range returns False."""
    mock_config.return_value = "America/Los_Angeles"
    mgr = _make_manager_with_enabled(True)

    # 03:00 PT is outside 06:00-18:00
    tz = pytz.timezone("America/Los_Angeles")
    fake_now = tz.localize(datetime(2025, 6, 15, 3, 0, 0)).astimezone(timezone.utc)

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.timezone = timezone
        mock_dt.timedelta = timedelta
        result = mgr._is_device_in_time_range(
            "heater", (time(6, 0), time(18, 0))
        )

    assert result is False


@patch("config._decouple_config")
def test_candidate_details_shows_outside_range_reason(mock_config):
    """Diagnostics include reason for outside-range device."""
    mock_config.return_value = "America/Los_Angeles"

    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=2000.0,
            role="flexible",
            priority=10,
            time_range=(time(6, 0), time(18, 0)),
        ),
    }
    plug_ctrl = PlugController(plugs)

    mgr = LoadManager(
        metrics_fetch=lambda: _make_metrics_with_wh("main_panel", "QH3", -2000.0),
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    # 03:00 PT is outside the heater's time range
    tz = pytz.timezone("America/Los_Angeles")
    fake_now = tz.localize(datetime(2025, 6, 15, 3, 0, 0)).astimezone(timezone.utc)

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.timezone = timezone
        mock_dt.timedelta = timedelta
        details = mgr._build_candidate_details(
            seconds_remaining=600,
            tesla_state=None,
            tesla_error=None,
            tesla_configured=False,
        )

    heater_detail = next(d for d in details if d["name"] == "heater")
    assert heater_detail.get("reason") == "outside_time_range"


@patch("config._decouple_config")
def test_candidate_details_no_reason_when_in_range(mock_config):
    """Diagnostics omit reason when device is inside time range."""
    mock_config.return_value = "America/Los_Angeles"

    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=2000.0,
            role="flexible",
            priority=10,
            time_range=(time(6, 0), time(18, 0)),
        ),
    }
    plug_ctrl = PlugController(plugs)

    mgr = LoadManager(
        metrics_fetch=lambda: _make_metrics_with_wh("main_panel", "QH3", -2000.0),
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    # 12:00 PT is inside the heater's time range
    tz = pytz.timezone("America/Los_Angeles")
    fake_now = tz.localize(datetime(2025, 6, 15, 12, 0, 0)).astimezone(timezone.utc)

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.timezone = timezone
        mock_dt.timedelta = timedelta
        details = mgr._build_candidate_details(
            seconds_remaining=600,
            tesla_state=None,
            tesla_error=None,
            tesla_configured=False,
        )

    heater_detail = next(d for d in details if d["name"] == "heater")
    assert "reason" not in heater_detail


@patch("config._decouple_config")
def test_cycle_filters_outside_range_plug(mock_config):
    """Plug outside time range is excluded from engine.decide() call."""
    mock_config.return_value = "America/Los_Angeles"

    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=2000.0,
            role="flexible",
            priority=10,
            time_range=(time(6, 0), time(18, 0)),
        ),
    }
    plug_ctrl = PlugController(plugs)

    mgr = LoadManager(
        metrics_fetch=lambda: _make_metrics_with_wh("main_panel", "QH3", -2000.0),
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=True,
    )

    # 03:00 PT is outside the heater's time range
    tz = pytz.timezone("America/Los_Angeles")
    fake_now = tz.localize(datetime(2025, 6, 15, 3, 0, 0)).astimezone(timezone.utc)

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.timezone = timezone
        mock_dt.timedelta = timedelta
        # Patch engine.decide to capture what plugs it receives
        with patch.object(mgr.engine, "decide", return_value=[]) as mock_decide:
            asyncio.run(
                mgr._cycle_async_phase(
                    gap_wh=1500.0,
                    adjusted_wh=-2000.0,
                    seconds_remaining=600,
                    dry_run=True,
                )
            )

    # engine.decide should receive an empty plugs dict since heater is out of range
    call_kwargs = mock_decide.call_args.kwargs if hasattr(mock_decide.call_args, 'kwargs') else mock_decide.call_args[1]
    assert call_kwargs["plugs"] == {}


@patch("config._decouple_config")
def test_cycle_includes_plug_inside_range(mock_config):
    """Plug inside time range is included in engine.decide() call."""
    mock_config.return_value = "America/Los_Angeles"

    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=2000.0,
            role="flexible",
            priority=10,
            time_range=(time(6, 0), time(18, 0)),
        ),
    }
    plug_ctrl = PlugController(plugs)

    mgr = LoadManager(
        metrics_fetch=lambda: _make_metrics_with_wh("main_panel", "QH3", -2000.0),
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=True,
    )

    # 12:00 PT is inside the heater's time range
    tz = pytz.timezone("America/Los_Angeles")
    fake_now = tz.localize(datetime(2025, 6, 15, 12, 0, 0)).astimezone(timezone.utc)

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.timezone = timezone
        mock_dt.timedelta = timedelta
        with patch.object(mgr.engine, "decide", return_value=[]) as mock_decide:
            asyncio.run(
                mgr._cycle_async_phase(
                    gap_wh=1500.0,
                    adjusted_wh=-2000.0,
                    seconds_remaining=600,
                    dry_run=True,
                )
            )

    call_kwargs = mock_decide.call_args.kwargs if hasattr(mock_decide.call_args, 'kwargs') else mock_decide.call_args[1]
    assert "heater" in call_kwargs["plugs"]


@patch("config._decouple_config")
def test_cycle_filters_outside_range_tesla(mock_config):
    """Tesla outside time range is excluded from engine.decide() call."""
    mock_config.return_value = "America/Los_Angeles"

    plugs: dict[str, PlugConfig] = {}
    plug_ctrl = PlugController(plugs)

    tesla_cfg = TeslaConfig(
        client_id="cid",
        client_secret="csec",
        redirect_uri="http://localhost/callback",
        vehicle_id="v1",
        home_lat=37.0,
        home_lon=-122.0,
        home_radius_m=500,
        time_range=(time(8, 0), time(20, 0)),
    )

    mgr = LoadManager(
        metrics_fetch=lambda: _make_metrics_with_wh("main_panel", "QH3", -2000.0),
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=True,
    )
    mgr.tesla_config = tesla_cfg

    # 03:00 PT is outside Tesla's time range
    tz = pytz.timezone("America/Los_Angeles")
    fake_now = tz.localize(datetime(2025, 6, 15, 3, 0, 0)).astimezone(timezone.utc)

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.timezone = timezone
        mock_dt.timedelta = timedelta
        # Mock _fetch_tesla_state_async to return a valid state for the cycle
        with patch.object(
            mgr, "_fetch_tesla_state_async",
            return_value=(
                TeslaState(
                    is_charging=True, current_amps=32, soc_percent=50.0,
                    plugged_in=True, at_home=True, at_charge_limit=False,
                ),
                None,
                None,
            )
        ):
            with patch.object(mgr.engine, "decide", return_value=[]) as mock_decide:
                asyncio.run(
                    mgr._cycle_async_phase(
                        gap_wh=-1500.0,
                        adjusted_wh=1000.0,
                        seconds_remaining=600,
                        dry_run=True,
                    )
                )

    call_kwargs = mock_decide.call_args.kwargs if hasattr(mock_decide.call_args, 'kwargs') else mock_decide.call_args[1]
    # Tesla should be None since it's outside its time range
    assert call_kwargs["tesla"] is None


@patch("config._decouple_config")
def test_no_action_reason_skips_outside_range_plug(mock_config):
    """_determine_no_action_reason skips outside-range plugs when checking eligibility."""
    mock_config.return_value = "America/Los_Angeles"

    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=2000.0,
            role="flexible",
            priority=10,
            time_range=(time(6, 0), time(18, 0)),
        ),
    }
    plug_ctrl = PlugController(plugs)

    mgr = LoadManager(
        metrics_fetch=lambda: _make_metrics_with_wh("main_panel", "QH3", -2000.0),
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )

    # 03:00 PT is outside the heater's time range
    tz = pytz.timezone("America/Los_Angeles")
    fake_now = tz.localize(datetime(2025, 6, 15, 3, 0, 0)).astimezone(timezone.utc)

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.timezone = timezone
        mock_dt.timedelta = timedelta
        reason = mgr._determine_no_action_reason(
            results=[],
            gap_wh=1000.0,
            seconds_remaining=600,
            tesla_state=None,
            tesla_configured=False,
            tesla_error=None,
        )

    # Heater is the only plug and it's outside range, so no eligible devices
    assert reason == "no_eligible"


# --- Tesla token refresh tests ---


def test_authenticate_calls_access_token_not_check_access_token(tmp_path):
    """authenticate() uses access_token() which makes a real API call,
    not check_access_token() which only checks the local expiry timestamp."""
    from load_controllers import RealTeslaController, TESLA_TOKENS_FILE

    tokens_path = tmp_path / "tesla-tokens.json"
    import load_controllers as lc

    original = lc.TESLA_TOKENS_FILE
    lc.TESLA_TOKENS_FILE = tokens_path

    try:
        from load_manager import save_tesla_tokens

        future_expires = 9999999999
        save_tesla_tokens(
            refresh_token="refresh-abc",
            access_token="access-def",
            expires=future_expires,
            tokens_path=tokens_path,
        )

        tesla_config = TeslaConfig(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="12345",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
        )

        controller = RealTeslaController(tesla_config)

        # _ensure_api() creates self._api lazily. Patch it to return a mock API
        # so we can control what methods get called during authenticate().
        from unittest.mock import AsyncMock, MagicMock

        mock_api_obj = MagicMock()
        mock_api_obj.check_access_token = AsyncMock(return_value=None)
        mock_api_obj.access_token = AsyncMock(return_value="new-token")

        with patch.object(controller, "_ensure_api"):
            controller._api = mock_api_obj  # type: ignore[attr-defined]

            asyncio.run(controller.authenticate())

        # access_token() MUST be called (it does the real API validation)
        mock_api_obj.access_token.assert_called_once()
        # check_access_token() MUST NOT be called (it's a no-op local check)
        mock_api_obj.check_access_token.assert_not_called()

    finally:
        lc.TESLA_TOKENS_FILE = original


def test_get_charging_state_converts_forbidden_to_tesla_auth_error(tmp_path):
    """When the Tesla API returns 403 Forbidden, get_charging_state() raises
    TeslaAuthError instead of letting the raw exception propagate."""
    from load_controllers import RealTeslaController, TESLA_TOKENS_FILE
    from tesla_fleet_api.exceptions import Forbidden

    tokens_path = tmp_path / "tesla-tokens.json"
    import load_controllers as lc

    original = lc.TESLA_TOKENS_FILE
    lc.TESLA_TOKENS_FILE = tokens_path

    try:
        from load_manager import save_tesla_tokens

        future_expires = 9999999999
        save_tesla_tokens(
            refresh_token="refresh-abc",
            access_token="access-def",
            expires=future_expires,
            tokens_path=tokens_path,
        )

        tesla_config = TeslaConfig(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="12345",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
        )

        controller = RealTeslaController(tesla_config)

        # Mock _fetch_vehicle_data to raise Forbidden (403)
        with patch.object(
            controller, "_fetch_vehicle_data", side_effect=Forbidden({"message": "not authorized"})
        ):
            with pytest.raises(TeslaAuthError) as exc_info:
                asyncio.run(controller.get_charging_state())

            assert "not authorized" in str(exc_info.value).lower()

    finally:
        lc.TESLA_TOKENS_FILE = original


def test_authenticate_saves_refreshed_tokens(tmp_path):
    """When access_token() refreshes the token, save_tesla_tokens is called with new values."""
    from load_controllers import RealTeslaController, TESLA_TOKENS_FILE

    tokens_path = tmp_path / "tesla-tokens.json"
    import load_controllers as lc

    original = lc.TESLA_TOKENS_FILE
    lc.TESLA_TOKENS_FILE = tokens_path

    try:
        from load_manager import save_tesla_tokens, load_tesla_tokens

        # Write an old token
        save_tesla_tokens(
            refresh_token="old-refresh",
            access_token="old-access",
            expires=1000000,  # expired
            tokens_path=tokens_path,
        )

        tesla_config = TeslaConfig(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="12345",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
        )

        controller = RealTeslaController(tesla_config)

        # Patch _ensure_api to skip real API init, then set up a mock _api
        from unittest.mock import AsyncMock, MagicMock

        mock_api_obj = MagicMock()
        mock_api_obj.access_token = AsyncMock(return_value="new-access")

        with patch.object(controller, "_ensure_api"):
            controller._api = mock_api_obj  # type: ignore[attr-defined]

            async def mock_access():
                controller._api.refresh_token = "new-refresh"  # type: ignore[attr-defined]
                controller._api._access_token = "new-access"  # type: ignore[attr-defined]
                controller._api.expires = 9999999999  # type: ignore[attr-defined]
                return "new-access"

            mock_api_obj.access_token.side_effect = mock_access

            # Patch save_tesla_tokens to capture what it's called with
            with patch("load_controllers.save_tesla_tokens") as mock_save:
                asyncio.run(controller.authenticate())

            # Verify save_tesla_tokens was called with refreshed values
            mock_save.assert_called_once()
            call_kwargs = (
                mock_save.call_args.kwargs
                if hasattr(mock_save.call_args, "kwargs")
                else mock_save.call_args[1]
            )
            assert call_kwargs["refresh_token"] == "new-refresh"
            assert call_kwargs["access_token"] == "new-access"

    finally:
        lc.TESLA_TOKENS_FILE = original
