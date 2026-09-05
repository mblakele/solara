"""External plug changes synthesize pending effects.

When _sync_plug_states reconciles an external on/off flip, it records a
pending effect as if the load manager had decided it, so NBC math
(estimated_current_wh) and the can_toggle debounce both account for the
change before the next decision.
"""

import asyncio
from datetime import datetime, timezone

from clock import FakeClock
from load_controllers import PlugController
from load_manager import LoadManager, LoadManagerConfig
from load_models import DeviceState, PlugConfig
from tests.helpers import _make_metrics_with_wh


def _make_mgr(
    plugs: dict,
    dry_run: bool = False,
    now: datetime | None = None,
) -> LoadManager:
    """Build a LoadManager with a stub plug controller."""
    plug_ctrl = PlugController(plugs)
    mgr = LoadManager(LoadManagerConfig(
        metrics_fetch=lambda: _make_metrics_with_wh("main_panel", -2000.0),
        plug_ctrl=plug_ctrl,
        tesla_ctrl=None,
        target_wh=-500,
        nbc_device="main_panel",
        enabled=True,
        dry_run=dry_run,
    ))
    if now is not None:
        mgr._clock = FakeClock(now)
    return mgr


def test_sync_external_turn_on_adds_effect() -> None:
    """desired=False/actual=True records a +watts turn_on effect."""
    now = datetime(2026, 9, 5, 15, 16, 3, tzinfo=timezone.utc)
    plugs = {
        "ecoflow": PlugConfig(
            name="ecoflow", accessory_id="e1",
            power_watts=2000.0, priority=10,
        ),
    }
    mgr = _make_mgr(plugs, now=now)
    mgr.plug_ctrl._state["ecoflow"] = True  # type: ignore[attr-defined]
    mgr.state.devices["ecoflow"] = DeviceState(
        name="ecoflow", desired_state=False, actual_state=False,
        last_toggle=None,
    )

    asyncio.run(mgr._sync_plug_states())

    dev = mgr.state.devices["ecoflow"]
    assert dev.actual_state is True
    assert dev.desired_state is True
    assert dev.last_toggle == now
    effects = mgr.state.snapshot_effects()
    assert len(effects) == 1
    eff = effects[0]
    assert eff.device_name == "ecoflow"
    assert eff.action == "turn_on"
    assert eff.power_watts == 2000.0
    assert eff.timestamp == now
    assert eff.data_point_at == now


def test_sync_external_turn_off_adds_effect() -> None:
    """desired=True/actual=False records a -watts turn_off effect."""
    now = datetime(2026, 9, 5, 15, 16, 3, tzinfo=timezone.utc)
    plugs = {
        "heater": PlugConfig(
            name="heater", accessory_id="h1",
            power_watts=2000.0, priority=10,
        ),
    }
    mgr = _make_mgr(plugs, now=now)
    mgr.plug_ctrl._state["heater"] = False  # type: ignore[attr-defined]
    mgr.state.devices["heater"] = DeviceState(
        name="heater", desired_state=True, actual_state=True,
        last_toggle=None,
    )

    asyncio.run(mgr._sync_plug_states())

    dev = mgr.state.devices["heater"]
    assert dev.actual_state is False
    assert dev.desired_state is False
    assert dev.last_toggle == now
    effects = mgr.state.snapshot_effects()
    assert len(effects) == 1
    assert effects[0].action == "turn_off"
    assert effects[0].power_watts == -2000.0


def test_sync_no_effect_when_states_agree() -> None:
    """No divergence means no synthetic effect and no toggle bump."""
    now = datetime(2026, 9, 5, 15, 16, 3, tzinfo=timezone.utc)
    plugs = {
        "heater": PlugConfig(
            name="heater", accessory_id="h1",
            power_watts=2000.0, priority=10,
        ),
    }
    mgr = _make_mgr(plugs, now=now)
    mgr.plug_ctrl._state["heater"] = True  # type: ignore[attr-defined]
    mgr.state.devices["heater"] = DeviceState(
        name="heater", desired_state=True, actual_state=True,
        last_toggle=None,
    )

    asyncio.run(mgr._sync_plug_states())

    assert mgr.state.snapshot_effects() == []
    assert mgr.state.devices["heater"].last_toggle is None


def test_sync_skips_effect_without_power() -> None:
    """Plugs with unknown power reconcile state but record no effect."""
    now = datetime(2026, 9, 5, 15, 16, 3, tzinfo=timezone.utc)
    plugs = {
        "guard": PlugConfig(
            name="guard", accessory_id="g1",
            power_watts=None, priority=0, sentinel=True,
        ),
    }
    mgr = _make_mgr(plugs, now=now)
    mgr.plug_ctrl._state["guard"] = True  # type: ignore[attr-defined]
    mgr.state.devices["guard"] = DeviceState(
        name="guard", desired_state=False, actual_state=False,
        last_toggle=None,
    )

    asyncio.run(mgr._sync_plug_states())

    dev = mgr.state.devices["guard"]
    assert dev.desired_state is True
    assert mgr.state.snapshot_effects() == []
    assert dev.last_toggle is None


def test_sync_skips_effect_in_dry_run() -> None:
    """Dry-run still reconciles tracking but records no effect."""
    now = datetime(2026, 9, 5, 15, 16, 3, tzinfo=timezone.utc)
    plugs = {
        "heater": PlugConfig(
            name="heater", accessory_id="h1",
            power_watts=2000.0, priority=10,
        ),
    }
    mgr = _make_mgr(plugs, dry_run=True, now=now)
    mgr.plug_ctrl._state["heater"] = False  # type: ignore[attr-defined]
    mgr.state.devices["heater"] = DeviceState(
        name="heater", desired_state=True, actual_state=True,
        last_toggle=None,
    )

    asyncio.run(mgr._sync_plug_states())

    dev = mgr.state.devices["heater"]
    assert dev.desired_state is False
    assert mgr.state.snapshot_effects() == []
    assert dev.last_toggle is None


def test_sync_skips_effect_on_first_observation() -> None:
    """Unknown prior actual means first sight, not an external flip."""
    now = datetime(2026, 9, 5, 15, 16, 3, tzinfo=timezone.utc)
    plugs = {
        "heater": PlugConfig(
            name="heater", accessory_id="h1",
            power_watts=2000.0, priority=10,
        ),
    }
    mgr = _make_mgr(plugs, now=now)
    mgr.plug_ctrl._state["heater"] = False  # type: ignore[attr-defined]
    mgr.state.devices["heater"] = DeviceState(
        name="heater", desired_state=True, actual_state=None,
        last_toggle=None,
    )

    asyncio.run(mgr._sync_plug_states())

    dev = mgr.state.devices["heater"]
    assert dev.actual_state is False
    assert dev.desired_state is False
    assert mgr.state.snapshot_effects() == []
    assert dev.last_toggle is None
