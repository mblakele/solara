"""Explicit target Wh bands replace the heuristic hysteresis.

A band is absolute: no action while adjusted/predicted Wh is inside
``[low, high]``. The jump/aim still uses ``target_wh``. Without a band,
``int(abs(target_wh) / 3)`` remains the fallback deadband.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import device_config
from clock import FakeClock
from config import Config
from load_controllers import PlugController
from load_manager import LoadManager, LoadManagerConfig
from load_models import DeviceState, PlugConfig
from load_nbc import DecideContext, GapMinder, StateTracker

UTC = timezone.utc
NOW = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)


def _plug(name: str = "heater", watts: float = 4857.0) -> PlugConfig:
    """Build one always-on plug for a decision context."""
    return PlugConfig(
        name=name, accessory_id="h1", power_watts=watts, priority=60,
    )


def _ctx(plugs: dict[str, PlugConfig], seconds: int = 300) -> DecideContext:
    """Build a DecideContext with every plug already on."""
    state = StateTracker()
    for name in plugs:
        state.devices[name] = DeviceState(
            name=name, desired_state=True, actual_state=True,
        )
    return DecideContext(
        now=NOW, seconds_remaining=seconds, state=state, plugs=plugs, tesla=None,
    )


def _actions(
    engine: GapMinder,
    predicted_wh: float,
    target_wh: float,
    band: tuple[int, int] | None = None,
) -> list[str]:
    """Return the actions the engine decided for one plug."""
    ctx = _ctx({"heater": _plug()})
    return [
        action.action
        for action in engine.decide(
            ctx=ctx,
            predicted_wh=predicted_wh,
            target_wh=target_wh,
            target_wh_band=band,
        )
    ]


# --- GapMinder band tests ---


def test_band_suppresses_turn_off_inside_band() -> None:
    """Inside the band, a gap larger than hysteresis takes no action."""
    engine = GapMinder(hysteresis_wh=3)
    # target -9 / predicted -5: gap 4 > 3, but -5 is the band's high bound.
    assert _actions(engine, -5.0, -9.0, band=(-10, -5)) == []


def test_without_band_heuristic_still_acts() -> None:
    """The old deadband is the fallback when no band is configured."""
    engine = GapMinder(hysteresis_wh=3)
    assert _actions(engine, -5.0, -9.0) == ["turn_off"]


@pytest.mark.parametrize("predicted", [-10.0, -5.0])
def test_band_bounds_are_inclusive(predicted: float) -> None:
    """Both bounds are acceptable values."""
    engine = GapMinder(hysteresis_wh=3)
    assert _actions(engine, predicted, 0.0, band=(-10, -5)) == []


def test_asymmetric_band_suppresses_far_from_aim() -> None:
    """A wide asymmetric band can sit far from the aim and still suppress."""
    engine = GapMinder(hysteresis_wh=3)
    # Aim 0, band [-20, -5], predicted -18: 18 Wh from aim, still acceptable.
    assert _actions(engine, -18.0, 0.0, band=(-20, -5)) == []


def test_band_does_not_block_outside_values() -> None:
    """Outside the band the engine decides normally."""
    engine = GapMinder(hysteresis_wh=3)
    # Above the band: predicted -2 vs aim -9 is a 7 Wh deficit.
    assert _actions(engine, -2.0, -9.0, band=(-10, -5)) == ["turn_off"]


# --- LoadManager wiring ---


def _payload(smartmeter: dict) -> dict:
    """Build a devices.json payload with a UTC timezone."""
    return {"timezone": "UTC", "smartmeter": {"device": "M1", **smartmeter}}


def _manager(
    path: Path,
    clock: FakeClock,
    *,
    config: Config | None = None,
) -> LoadManager:
    """Build a LoadManager against a temporary devices.json."""
    with patch.object(device_config, "_DEVICES_FILE", path):
        device_config.reload()
        return LoadManager(
            LoadManagerConfig(
                config=config,
                plug_ctrl=PlugController({}),
                tesla_ctrl=None,
                nbc_device="M1",
                enabled=True,
                dry_run=True,
                clock=clock,
            )
        )


def test_manager_resolves_band_at_startup(tmp_path: Path) -> None:
    """A top-level band reaches LoadManager."""
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(
        _payload({"target_wh": -9, "target_wh_range": [-10, -5]})
    ))
    manager = _manager(path, FakeClock(NOW))
    assert manager.target_wh == -9
    assert manager.target_wh_band == (-10, -5)


def test_manager_schedule_switches_on_clock_advance(tmp_path: Path) -> None:
    """The resolved target follows the TOU window each cycle."""
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(_payload({
        "target_wh": -9,
        "target_wh_schedule": [
            {"time_range": "00:00-15:00", "target_wh_range": [90, 110]},
            {"time_range": "15:00-00:00", "target_wh_range": [-10, -5]},
        ],
    })))
    clock = FakeClock(datetime(2026, 5, 7, 14, 0, tzinfo=UTC))
    manager = _manager(path, clock)
    assert manager.target_wh == 100
    assert manager.target_wh_band == (90, 110)

    clock.advance(3600)  # 15:00:00, second window starts
    with patch.object(device_config, "_DEVICES_FILE", path):
        manager._refresh_target(clock.now())

    assert manager.target_wh == -8
    assert manager.target_wh_band == (-10, -5)


def test_env_override_disables_schedule_and_band(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """LOAD_TARGET_WH wins over every devices.json target form."""
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(_payload({
        "target_wh": -9,
        "target_wh_range": [-10, -5],
        "target_wh_schedule": [
            {"time_range": "00:00-15:00", "target_wh_range": [90, 110]},
        ],
    })))
    cfg = Config(overrides={"LOAD_TARGET_WH": "-1000"})
    manager = _manager(path, FakeClock(NOW), config=cfg)

    assert manager.target_wh == -1000
    assert manager.target_wh_band is None
    assert "LOAD_TARGET_WH" in caplog.text
    assert "overrides" in caplog.text


def test_inside_target_band_without_band_uses_heuristic(tmp_path: Path) -> None:
    """No band falls back to the existing hysteresis width."""
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(_payload({"target_wh": -9})))
    manager = _manager(path, FakeClock(NOW))

    assert manager._inside_target_band(-7.0) is True    # gap 2 <= 3
    assert manager._inside_target_band(-5.0) is False   # gap 4 > 3
