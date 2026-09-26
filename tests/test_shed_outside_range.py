"""RED tests for shed_outside_range + out-of-range ON alerts.

shed_outside_range: per-plug flag allowing turn_off (shed) outside the
configured time_range while still forbidding turn_on there.

Out-of-range alerts: Telegram notice when a managed plug is ON outside
its time_range, throttled once per QH per device, bypassing the
telegram.devices whitelist and dry-run guard (like data-health alerts).
"""

import asyncio
from datetime import datetime, time, timedelta, timezone
from unittest.mock import patch

import pytz

import device_config
from clock import FakeClock
from config_loader import load_plugs_from_file, load_vocolinc_plugs_from_file
from load_controllers import PlugController
from load_manager import LoadManager, LoadManagerConfig
from load_models import DeviceState, PlugConfig
from tests.helpers import _make_metrics_with_wh


def _pt(hour: int, minute: int = 0) -> datetime:
    tz = pytz.timezone("America/Los_Angeles")
    return tz.localize(datetime(2025, 6, 15, hour, minute, 0)).astimezone(timezone.utc)


def _mgr_with_plug(plug: PlugConfig, **kwargs) -> LoadManager:
    plug_ctrl = PlugController({plug.name: plug})
    defaults = dict(
        metrics_fetch=lambda: _make_metrics_with_wh("main_panel", -2000.0),
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=False,
    )
    defaults.update(kwargs)
    return LoadManager(LoadManagerConfig(**defaults))


@patch("config._lookup", return_value="America/Los_Angeles")
def test_shed_flag_parsed_homekit(_mock_tz):
    """shed_outside_range: true is parsed into PlugConfig."""
    with patch("device_config._load", return_value={
        "plugs": {"homekit": [
            {"name": "water_heater", "accessory_id": "a1",
             "power_watts": 4500, "time_range": "06:00-22:00",
             "shed_outside_range": True},
        ]}
    }):
        device_config.reload()
        plugs = load_plugs_from_file()
    assert plugs["water_heater"].shed_outside_range is True


@patch("config._lookup", return_value="America/Los_Angeles")
def test_shed_flag_defaults_false(_mock_tz):
    """Plugs without the flag default to False (current behavior)."""
    with patch("device_config._load", return_value={
        "plugs": {"homekit": [
            {"name": "heater", "accessory_id": "a1", "power_watts": 4500},
        ]}
    }):
        device_config.reload()
        plugs = load_plugs_from_file()
    assert plugs["heater"].shed_outside_range is False


@patch("config._lookup", return_value="America/Los_Angeles")
def test_shed_flag_parsed_vocolinc(_mock_tz):
    """VOCOlinc plugs also parse shed_outside_range."""
    with patch("device_config._load", return_value={
        "plugs": {"vocolinc": [
            {"name": "lamp", "device_name": "Lamp",
             "power_watts": 60, "time_range": "18:00-23:00",
             "shed_outside_range": True},
        ]}
    }):
        device_config.reload()
        plugs = load_vocolinc_plugs_from_file()
    assert plugs["lamp"].shed_outside_range is True


@patch("config._lookup", return_value="America/Los_Angeles")
def test_shed_plug_included_on_deficit_outside_range(_mock_tz):
    """Outside range + deficit + shed flag -> plug reaches the engine."""
    plug = PlugConfig(
        name="heater", accessory_id="h1", power_watts=2000.0, priority=10,
        time_range=(time(6, 0), time(18, 0)), shed_outside_range=True,
    )
    mgr = _mgr_with_plug(plug, dry_run=True)
    fake_now = _pt(3, 0)  # outside 06:00-18:00
    with patch.object(mgr.engine, "decide", return_value=[]) as mock_decide:
        asyncio.run(mgr._cycle_async_phase(
            gap_wh=-1500.0, adjusted_wh=1000.0, now=fake_now,
            seconds_remaining=600, dry_run=True,
        ))
    ctx = mock_decide.call_args[1]["ctx"]
    assert "heater" in ctx.plugs


@patch("config._lookup", return_value="America/Los_Angeles")
def test_shed_plug_excluded_on_surplus_outside_range(_mock_tz):
    """Outside range + surplus + shed flag -> still no turn_on."""
    plug = PlugConfig(
        name="heater", accessory_id="h1", power_watts=2000.0, priority=10,
        time_range=(time(6, 0), time(18, 0)), shed_outside_range=True,
    )
    mgr = _mgr_with_plug(plug, dry_run=True)
    fake_now = _pt(3, 0)
    with patch.object(mgr.engine, "decide", return_value=[]) as mock_decide:
        asyncio.run(mgr._cycle_async_phase(
            gap_wh=1500.0, adjusted_wh=-2000.0, now=fake_now,
            seconds_remaining=600, dry_run=True,
        ))
    ctx = mock_decide.call_args[1]["ctx"]
    assert "heater" not in ctx.plugs


@patch("config._lookup", return_value="America/Los_Angeles")
def test_no_shed_without_flag_on_deficit(_mock_tz):
    """Without the flag, deficit outside range still excludes the plug."""
    plug = PlugConfig(
        name="heater", accessory_id="h1", power_watts=2000.0, priority=10,
        time_range=(time(6, 0), time(18, 0)),
    )
    mgr = _mgr_with_plug(plug, dry_run=True)
    fake_now = _pt(3, 0)
    with patch.object(mgr.engine, "decide", return_value=[]) as mock_decide:
        asyncio.run(mgr._cycle_async_phase(
            gap_wh=-1500.0, adjusted_wh=1000.0, now=fake_now,
            seconds_remaining=600, dry_run=True,
        ))
    ctx = mock_decide.call_args[1]["ctx"]
    assert ctx.plugs == {}


@patch("config._lookup", return_value="America/Los_Angeles")
def test_shed_plug_turns_off_on_deficit_end_to_end(_mock_tz):
    """Real GapMinder sheds a shed-enabled ON plug on deficit outside range."""
    plug = PlugConfig(
        name="heater", accessory_id="h1", power_watts=2000.0, priority=10,
        time_range=(time(6, 0), time(18, 0)), shed_outside_range=True,
    )
    mgr = _mgr_with_plug(plug, dry_run=True)
    fake_now = _pt(3, 0)
    mgr.plug_ctrl._state["heater"] = True  # type: ignore[attr-defined]
    mgr.state.set_device_state(
        "heater", DeviceState(name="heater", desired_state=True, actual_state=True),
    )
    result = asyncio.run(mgr._cycle_async_phase(
        gap_wh=-1500.0, adjusted_wh=1000.0, now=fake_now,
        seconds_remaining=600, dry_run=True,
    ))
    assert [a.device_name for a in result.actions] == ["heater"]
    assert result.actions[0].action == "turn_off"


@patch("config._lookup", return_value="America/Los_Angeles")
def test_candidate_details_shed_only_reason(_mock_tz):
    """Diagnostics distinguish shed-only from fully excluded."""
    plug = PlugConfig(
        name="heater", accessory_id="h1", power_watts=2000.0, priority=10,
        time_range=(time(6, 0), time(18, 0)), shed_outside_range=True,
    )
    mgr = _mgr_with_plug(plug)
    details = mgr._build_candidate_details(
        _pt(3, 0), seconds_remaining=600, tesla_state=None,
        _tesla_error=None, tesla_configured=False,
    )
    heater = next(d for d in details if d.name == "heater")
    assert heater.reason == "outside_time_range_shed_only"


@patch("config._lookup", return_value="America/Los_Angeles")
def test_out_of_range_alert_queued_when_on(_mock_tz):
    """Plug ON outside its range queues a Telegram alert bypassing whitelist."""
    from telegram_client import TelegramConfig
    from telegram import TelegramSender

    plug = PlugConfig(
        name="heater", accessory_id="h1", power_watts=2000.0, priority=10,
        time_range=(time(6, 0), time(18, 0)),
    )
    sender = TelegramSender(TelegramConfig(bot_token="t", chat_id="c"))
    mgr = _mgr_with_plug(plug, telegram_sender=sender, dry_run=True)
    # No whitelist configured at all (None) — alert must still fire.
    mgr._telegram_devices = None
    mgr.state.set_device_state(
        "heater", DeviceState(name="heater", desired_state=True, actual_state=True),
    )
    mgr._check_out_of_range_alert(_pt(3, 0))
    assert len(mgr._pending_notifications) == 1
    msg = mgr._pending_notifications[0].format_message()
    assert "heater" in msg


@patch("config._lookup", return_value="America/Los_Angeles")
def test_out_of_range_alert_throttled_per_qh(_mock_tz):
    """Second check in the same QH does not re-alert."""
    from telegram_client import TelegramConfig
    from telegram import TelegramSender

    plug = PlugConfig(
        name="heater", accessory_id="h1", power_watts=2000.0, priority=10,
        time_range=(time(6, 0), time(18, 0)),
    )
    sender = TelegramSender(TelegramConfig(bot_token="t", chat_id="c"))
    mgr = _mgr_with_plug(plug, telegram_sender=sender, dry_run=True)
    mgr._telegram_devices = None
    mgr.state.set_device_state(
        "heater", DeviceState(name="heater", desired_state=True, actual_state=True),
    )
    base = _pt(3, 5)
    mgr._check_out_of_range_alert(base)
    mgr._check_out_of_range_alert(base + timedelta(seconds=30))
    assert len(mgr._pending_notifications) == 1


@patch("config._lookup", return_value="America/Los_Angeles")
def test_no_out_of_range_alert_when_off_or_in_range(_mock_tz):
    """No alert when the plug is off, or when ON inside its range."""
    from telegram_client import TelegramConfig
    from telegram import TelegramSender

    plug = PlugConfig(
        name="heater", accessory_id="h1", power_watts=2000.0, priority=10,
        time_range=(time(6, 0), time(18, 0)),
    )
    sender = TelegramSender(TelegramConfig(bot_token="t", chat_id="c"))
    mgr = _mgr_with_plug(plug, telegram_sender=sender, dry_run=True)
    mgr._telegram_devices = None
    # Off outside range -> silent.
    mgr.state.set_device_state(
        "heater", DeviceState(name="heater", desired_state=False, actual_state=False),
    )
    mgr._check_out_of_range_alert(_pt(3, 0))
    # On inside range -> silent.
    mgr.state.set_device_state(
        "heater", DeviceState(name="heater", desired_state=True, actual_state=True),
    )
    mgr._check_out_of_range_alert(_pt(12, 0))
    assert mgr._pending_notifications == []
