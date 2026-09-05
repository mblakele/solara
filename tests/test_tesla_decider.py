"""Tests for PendingEffect factories and TeslaDecider (from GapMinder)."""

from datetime import datetime, timezone

import pytest

from load_models import PlugConfig, TeslaState
from load_nbc import (
    DecideContext,
    GapMinder,
    StateTracker,
    TeslaDecider,
    make_plug_effect,
    make_tesla_set_amps,
    make_tesla_stop,
)

FIXED_NOW = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)


def _ctx(**overrides) -> DecideContext:
    """Build a DecideContext with a charging Tesla and no plugs by default."""
    base = {
        "now": FIXED_NOW,
        "seconds_remaining": 900,
        "state": StateTracker(),
        "plugs": {},
        "tesla": TeslaState(
            is_charging=True, current_amps=10, plugged_in=True, at_home=True
        ),
    }
    base.update(overrides)
    return DecideContext(**base)  # type: ignore[arg-type]


def test_make_plug_effect_signs():
    """turn_on is positive load, turn_off is negative (signed invariant)."""
    on = make_plug_effect("heater", "turn_on", 1000.0, FIXED_NOW)
    assert on.power_watts == 1000.0
    assert on.timestamp == FIXED_NOW
    assert on.data_point_at == FIXED_NOW
    off = make_plug_effect(
        "heater", "turn_off", 1000.0, FIXED_NOW, data_point_at=FIXED_NOW
    )
    assert off.power_watts == -1000.0


def test_make_plug_effect_rejects_bad_action():
    """Unknown actions raise instead of silently building an effect."""
    with pytest.raises(ValueError):
        make_plug_effect("heater", "blink", 1000.0, FIXED_NOW)  # type: ignore[arg-type]


def test_make_tesla_set_amps_and_stop():
    """Tesla factories set device/action/amps with volt-derived watts."""
    up = make_tesla_set_amps(16, 6, FIXED_NOW)
    assert up.device_name == "tesla"
    assert up.action == "set_amps"
    assert up.target_amps == 16
    assert up.power_watts > 0
    stop = make_tesla_stop(10, FIXED_NOW)
    assert stop.action == "turn_off"
    assert stop.power_watts < 0


def test_decider_increase_matches_gapminder():
    """TeslaDecider.decide_increase agrees with GapMinder._decide_tesla_amps."""
    engine = GapMinder(hysteresis_wh=3)
    ctx = _ctx()
    assert (
        engine.tesla_decider.decide_increase(ctx, 200.0)
        == engine._decide_tesla_amps(ctx, 200.0)
    )


def test_decider_reduce_matches_gapminder():
    """TeslaDecider.decide_reduce agrees with GapMinder._decide_tesla_reduce."""
    engine = GapMinder(hysteresis_wh=3)
    ctx = _ctx()
    assert (
        engine.tesla_decider.decide_reduce(ctx, 200.0, stop_allowed=False)
        == engine._decide_tesla_reduce(ctx, 200.0, stop_allowed=False)
    )


def test_decider_clamps_to_config_max():
    """Decider clamps target amps to its configured max."""
    decider = TeslaDecider(charge_amps_min=5, charge_amps_max=24)
    assert decider.charge_amps_max == 24
    action = decider.decide_increase(_ctx(), 10000.0)
    assert action is not None
    assert action.target_amps is not None
    assert action.target_amps <= 24


def test_gapminder_owns_configured_decider():
    """GapMinder wires its amp limits into TeslaDecider."""
    engine = GapMinder(charge_amps_min=6, charge_amps_max=32)
    assert engine.charge_amps_min == 6
    assert engine.tesla_decider.charge_amps_min == 6
    assert engine.tesla_decider.charge_amps_max == 32


def test_eligible_plugs_turn_on_order():
    """_eligible_plugs(want_on=True) returns off-plugs, highest priority first."""
    state = StateTracker()
    plugs = {
        "low": PlugConfig(name="low", accessory_id="a", power_watts=500.0, priority=1),
        "high": PlugConfig(name="high", accessory_id="b", power_watts=500.0, priority=9),
    }
    engine = GapMinder()
    ctx = _ctx(state=state, plugs=plugs, tesla=None)
    names = [name for _, name, _ in engine._eligible_plugs(ctx, want_on=True)]
    assert names == ["high", "low"]


def test_eligible_plugs_turn_off_order():
    """_eligible_plugs(want_on=False) returns on-plugs, lowest priority first."""
    from load_models import DeviceState

    state = StateTracker()
    plugs = {
        "low": PlugConfig(name="low", accessory_id="a", power_watts=500.0, priority=1),
        "high": PlugConfig(name="high", accessory_id="b", power_watts=500.0, priority=9),
    }
    state.set_device_state("low", DeviceState(name="low", desired_state=True))
    state.set_device_state("high", DeviceState(name="high", desired_state=True))
    engine = GapMinder()
    ctx = _ctx(state=state, plugs=plugs, tesla=None)
    names = [name for _, name, _ in engine._eligible_plugs(ctx, want_on=False)]
    assert names == ["low", "high"]
