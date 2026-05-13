"""Tests for GapMinder decision logic."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from load_manager import (
    PlugConfig,
    DeviceState,
    StateTracker,
    TeslaState,
    GapMinder,
)

fixed_now = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)


# --- Excess solar (turn on) tests ---


def test_hysteresis_no_action():
    """No action when within hysteresis margin."""
    engine = GapMinder()
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}

    actions = engine.decide(
        predicted_wh=-500.0,
        target_wh=-500.0,
        seconds_remaining=300,
        state=state,
        plugs=plugs,
        tesla=None,
    )

    assert len(actions) == 0


def test_hysteresis_no_action_at_boundary():
    """No action exactly at +/-999 Wh (within margin)."""
    engine = GapMinder()
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}

    actions = engine.decide(
        predicted_wh=-1500.0,
        target_wh=-500.0,
        seconds_remaining=300,
        state=state,
        plugs=plugs,
        tesla=None,
    )

    assert len(actions) == 0


def test_hysteresis_custom_value():
    """Custom hysteresis allows action within default margin."""
    # With default hysteresis of 1000, gap=500 would be within margin.
    # With hysteresis=100, gap=500 should trigger action.
    engine = GapMinder(hysteresis_wh=100)
    state = StateTracker()
    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="abc123",
            power_watts=1000.0,
        )
    }

    actions = engine.decide(
        predicted_wh=-1000.0,
        target_wh=-500.0,
        seconds_remaining=300,
        state=state,
        plugs=plugs,
        tesla=None,
    )

    assert len(actions) == 1
    assert actions[0].action == "turn_on"


def test_hysteresis_default_value():
    """Default hysteresis is 1000 for backward compatibility."""
    engine = GapMinder()
    assert engine.HYSTERESIS_WH == 1000


def test_turn_on_plug():
    """Turns on plug when gap exists."""
    engine = GapMinder()
    state = StateTracker()
    plugs = {
        "water_heater": PlugConfig(
            name="water_heater",
            accessory_id="abc123",
            power_watts=4500.0,
        )
    }

    actions = engine.decide(
        predicted_wh=-2000.0,
        target_wh=-500.0,
        seconds_remaining=300,
        state=state,
        plugs=plugs,
        tesla=None,
    )

    assert len(actions) == 1
    assert actions[0].action == "turn_on"
    assert actions[0].device_name == "water_heater"


def test_turn_on_plug_2():
    """Turns on plug that's currently off."""
    engine = GapMinder()
    state = StateTracker()
    state.devices["pool_pump"] = DeviceState(
        name="pool_pump",
        desired_state=False,
    )
    plugs = {
        "pool_pump": PlugConfig(
            name="pool_pump",
            accessory_id="xyz789",
            power_watts=1500.0,
        )
    }

    actions = engine.decide(
        predicted_wh=-2500.0,
        target_wh=-500.0,
        seconds_remaining=600,
        state=state,
        plugs=plugs,
        tesla=None,
    )

    assert len(actions) == 1
    assert actions[0].action == "turn_on"


def test_skip_plug_already_on():
    """Skips plug already on."""
    engine = GapMinder()
    state = StateTracker()
    state.devices["pool_pump"] = DeviceState(
        name="pool_pump",
        desired_state=True,
    )
    plugs = {
        "pool_pump": PlugConfig(
            name="pool_pump",
            accessory_id="xyz789",
            power_watts=1500.0,
        )
    }

    actions = engine.decide(
        predicted_wh=-2500.0,
        target_wh=-500.0,
        seconds_remaining=600,
        state=state,
        plugs=plugs,
        tesla=None,
    )

    assert len(actions) == 0


def test_priority_ordering():
    """Higher priority (higher number) turns on first."""
    engine = GapMinder()
    state = StateTracker()
    plugs = {
        "high_pri": PlugConfig(
            name="high_pri",
            accessory_id="abc",
            power_watts=1000.0,
            priority=20,
        ),
        "low_pri": PlugConfig(
            name="low_pri",
            accessory_id="def",
            power_watts=1000.0,
            priority=10,
        ),
    }

    actions = engine.decide(
        predicted_wh=-2000.0,
        target_wh=-500.0,
        seconds_remaining=300,
        state=state,
        plugs=plugs,
        tesla=None,
    )

    assert actions[0].device_name == "high_pri"
    assert actions[1].device_name == "low_pri"


def test_bin_pack_multiple_plugs():
    """Fits multiple plugs to fill gap."""
    engine = GapMinder()
    state = StateTracker()
    plugs = {
        "small": PlugConfig(
            name="small",
            accessory_id="a",
            power_watts=500.0,
        ),
        "med": PlugConfig(
            name="med",
            accessory_id="b",
            power_watts=1000.0,
        ),
    }

    actions = engine.decide(
        predicted_wh=-2500.0,
        target_wh=-500.0,
        seconds_remaining=1000,
        state=state,
        plugs=plugs,
        tesla=None,
    )

    assert len(actions) == 2


def test_skip_plug_before_debounce():
    """Skips plug that toggled too recently."""
    engine = GapMinder()
    state = StateTracker()

    with patch("load_nbc.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        state.devices["plug"] = DeviceState(
            name="plug",
            last_toggle=fixed_now - timedelta(seconds=30),
        )

    plugs = {
        "plug": PlugConfig(
            name="plug",
            accessory_id="abc",
            power_watts=4500.0,
        )
    }

    with patch("load_nbc.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        actions = engine.decide(
            predicted_wh=-2000.0,
            target_wh=-500.0,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=None,
        )

    assert len(actions) == 0


# --- Over-target (turn off) tests ---


def test_turn_off_all_on_plugs():
    """Turns off all plugs that are currently on."""
    engine = GapMinder()
    state = StateTracker()
    state.devices["pool_pump"] = DeviceState(
        name="pool_pump",
        desired_state=True,
    )
    state.devices["water_heater"] = DeviceState(
        name="water_heater",
        desired_state=True,
    )
    plugs = {
        "pool_pump": PlugConfig(
            name="pool_pump",
            accessory_id="xyz",
            power_watts=1500.0,
        ),
        "water_heater": PlugConfig(
            name="water_heater",
            accessory_id="abc",
            power_watts=4500.0,
        ),
    }

    actions = engine.decide(
        predicted_wh=2000.0,
        target_wh=-500.0,
        seconds_remaining=600,
        state=state,
        plugs=plugs,
        tesla=None,
    )

    assert len(actions) == 2
    assert actions[0].action == "turn_off"
    assert actions[0].device_name == "pool_pump"


def test_remove_lowest_priority_first():
    """Removes lowest priority (lowest number) first."""
    engine = GapMinder()
    state = StateTracker()
    state.devices["high_pri"] = DeviceState(
        name="high_pri",
        desired_state=True,
    )
    state.devices["low_pri"] = DeviceState(
        name="low_pri",
        desired_state=True,
    )
    plugs = {
        "high_pri": PlugConfig(
            name="high_pri",
            accessory_id="a",
            power_watts=1000.0,
            priority=20,
        ),
        "low_pri": PlugConfig(
            name="low_pri",
            accessory_id="b",
            power_watts=1000.0,
            priority=10,
        ),
    }

    actions = engine.decide(
        predicted_wh=2000.0,
        target_wh=500.0,
        seconds_remaining=1000,
        state=state,
        plugs=plugs,
        tesla=None,
    )

    # Each plug saves 278 Wh (1000W * 1000s / 3600), both fit in 1500 Wh gap
    assert len(actions) == 2
    assert actions[0].device_name == "low_pri"


def test_skip_off_plugs():
    """Skips plugs that are already off."""
    engine = GapMinder()
    state = StateTracker()
    plugs = {
        "pool_pump": PlugConfig(
            name="pool_pump",
            accessory_id="xyz",
            power_watts=1500.0,
        )
    }

    actions = engine.decide(
        predicted_wh=2000.0,
        target_wh=-500.0,
        seconds_remaining=600,
        state=state,
        plugs=plugs,
        tesla=None,
    )

    assert len(actions) == 0


# --- Tesla tests ---


def test_tesla_skip_when_not_at_home():
    """Skips Tesla when not at home."""
    engine = GapMinder()
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=False,
        current_amps=0,
        soc_percent=50.0,
        plugged_in=True,
        at_home=False,
        at_charge_limit=False,
    )

    actions = engine.decide(
        predicted_wh=-2000.0,
        target_wh=-500.0,
        seconds_remaining=300,
        state=state,
        plugs=plugs,
        tesla=tesla,
    )

    assert len(actions) == 0


def test_tesla_skip_when_not_plugged_in():
    """Skips Tesla when not plugged in."""
    engine = GapMinder()
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=False,
        current_amps=0,
        soc_percent=50.0,
        plugged_in=False,
        at_home=True,
        at_charge_limit=False,
    )

    actions = engine.decide(
        predicted_wh=-2000.0,
        target_wh=-500.0,
        seconds_remaining=300,
        state=state,
        plugs=plugs,
        tesla=tesla,
    )

    assert len(actions) == 0


def test_tesla_skip_at_charge_limit():
    """Skips Tesla when at charge limit (saturated)."""
    engine = GapMinder()
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=48,
        soc_percent=90.0,
        plugged_in=True,
        at_home=True,
        at_charge_limit=True,
    )

    actions = engine.decide(
        predicted_wh=-2000.0,
        target_wh=-500.0,
        seconds_remaining=300,
        state=state,
        plugs=plugs,
        tesla=tesla,
    )

    assert len(actions) == 0


def test_decide_tesla_increase_amps_5_8():
    """When predicted_wh < target and Tesla amps can increase,
    the engine should call set_amps with expected value."""
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,  # Low amps; reducing would drop below min of 5
        soc_percent=60.0,
        plugged_in=True,
        at_home=True,
        at_charge_limit=False,
    )

    actions = engine.decide(
        predicted_wh=-561.258618125,
        target_wh=-9.0,
        seconds_remaining=711,
        state=state,
        plugs=plugs,
        tesla=tesla,
    )

    assert len(actions) == 1
    assert actions[0].action == "set_amps"
    assert actions[0].device_name == "tesla"
    assert actions[0].target_amps == 8


def test_decide_tesla_increase_amps_7_9():
    """When predicted_wh < target and Tesla amps can increase,
    the engine should call set_amps with expected value."""
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=7,  # Low amps; reducing would drop below min of 5
        soc_percent=60.0,
        plugged_in=True,
        at_home=True,
        at_charge_limit=False,
    )

    actions = engine.decide(
        predicted_wh=-43.87908600000001,
        target_wh=-9.0,
        seconds_remaining=231,
        state=state,
        plugs=plugs,
        tesla=tesla,
    )

    assert len(actions) == 1
    assert actions[0].action == "set_amps"
    assert actions[0].device_name == "tesla"
    assert actions[0].target_amps == 9


def test_decide_tesla_reduce_below_min_stops_charging():
    """When predicted_wh > target and Tesla amps would drop below min,
    the engine should call stop_charging instead of set_amps."""
    engine = GapMinder()
    state = StateTracker()
    # No plugs to turn off — only Tesla is available for reduction
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=8,  # Low amps; reducing would drop below min of 5
        soc_percent=60.0,
        plugged_in=True,
        at_home=True,
        at_charge_limit=False,
    )

    actions = engine.decide(
        predicted_wh=2000.0,
        target_wh=-500.0,
        seconds_remaining=600,
        state=state,
        plugs=plugs,
        tesla=tesla,
    )

    assert len(actions) == 1
    assert actions[0].action == "turn_off"
    assert actions[0].device_name == "tesla"


def test_decide_with_plugs_and_tesla_priority_ordering():
    """When both plugs and Tesla are available for excess solar, verify
    plugs turn on first (by priority), then Tesla fills remaining gap."""
    engine = GapMinder()
    state = StateTracker()
    plugs = {
        "high_pri": PlugConfig(
            name="high_pri",
            accessory_id="a",
            power_watts=1000.0,
            priority=20,
        ),
        "low_pri": PlugConfig(
            name="low_pri",
            accessory_id="b",
            power_watts=500.0,
            priority=10,
        ),
    }
    tesla = TeslaState(
        is_charging=True,
        current_amps=None,
        soc_percent=40.0,
        plugged_in=True,
        at_home=True,
        at_charge_limit=False,
    )

    actions = engine.decide(
        predicted_wh=-3000.0,
        target_wh=-500.0,
        seconds_remaining=600,
        state=state,
        plugs=plugs,
        tesla=tesla,
    )

    # high_pri plug (1000W * 600/3600 = 166.7 Wh) turns on first by priority
    assert actions[0].device_name == "high_pri"
    assert actions[0].action == "turn_on"
    # low_pri plug (500W * 600/3600 = 83.3 Wh) turns on second
    assert actions[1].device_name == "low_pri"
    assert actions[1].action == "turn_on"
    # Tesla fills the remaining gap after plugs are exhausted
    assert any(a.device_name == "tesla" for a in actions)


# --- Hysteresis anti-chatter tests ---


def test_hysteresis_blocks_small_gap_turn_off():
    """A small over-target gap must not turn off a device when the
    resulting undershoot would exceed the current overshoot.

    Regression guard: if the top-level hysteresis gate is removed for
    over-target cases, this test will fail because the engine turns off
    the pool pump creating a 200 Wh undershoot for a 50 Wh gap.
    """
    engine = GapMinder()
    state = StateTracker()

    # Pool pump on, savings ≈ 1500 * 600 / 3600 = 250 Wh
    state.devices["pool_pump"] = DeviceState(
        name="pool_pump",
        desired_state=True,
    )
    plugs = {
        "pool_pump": PlugConfig(
            name="pool_pump",
            accessory_id="xyz",
            power_watts=1500.0,
        ),
    }

    # Tiny over-target gap: predicted -450 Wh vs target -500 Wh
    # → gap = -500 - (-450) = -50 Wh (abs_gap = 50, within hysteresis)
    actions = engine.decide(
        predicted_wh=-450.0,
        target_wh=-500.0,
        seconds_remaining=600,
        state=state,
        plugs=plugs,
        tesla=None,
    )

    # savings (250) > 2 * abs_gap (100), so undershoot would be worse than
    # overshoot — no action taken.
    assert len(actions) == 0


def test_hysteresis_blocks_small_gap_tesla_reduce():
    """A small over-target gap must not reduce Tesla amps when the gap is
    within hysteresis and no oversized device would benefit.

    Regression guard: if the top-level hysteresis gate is removed for
    over-target cases, this test will fail because the engine calls
    _decide_tesla_reduce() for a 200 Wh gap, potentially thrashing amps.
    """
    engine = GapMinder()
    state = StateTracker()

    # No plugs on — only Tesla is available
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=48,
        soc_percent=60.0,
        plugged_in=True,
        at_home=True,
        at_charge_limit=False,
    )

    # Small over-target gap: predicted 350 Wh vs target -500 Wh → abs_gap = 150 Wh
    actions = engine.decide(
        predicted_wh=350.0,
        target_wh=-500.0,
        seconds_remaining=600,
        state=state,
        plugs=plugs,
        tesla=tesla,
    )

    # Within hysteresis (150 < 1000) and no oversized device to bypass — no action
    assert len(actions) == 0


def test_hysteresis_blocks_small_gap_multiple():
    """A small over-target gap must not turn off any devices when
    all of them fit in the gap but total savings is less than abs_gap.

    Regression guard: if the top-level hysteresis gate is removed for
    over-target cases, this test will fail because the engine turns off
    both plugs (total savings 62.5 Wh) for a 150 Wh gap.
    """
    engine = GapMinder()
    state = StateTracker()

    # Two small devices on
    state.devices["fan"] = DeviceState(
        name="fan",
        desired_state=True,
    )
    state.devices["dehumidifier"] = DeviceState(
        name="dehumidifier",
        desired_state=True,
    )
    plugs = {
        "fan": PlugConfig(
            name="fan",
            accessory_id="a1",
            power_watts=75.0,
            priority=5,
        ),
        "dehumidifier": PlugConfig(
            name="dehumidifier",
            accessory_id="b2",
            power_watts=300.0,
            priority=10,
        ),
    }

    # Small over-target gap: predicted -350 Wh vs target -500 Wh
    # → gap = -500 - (-350) = -150 Wh (abs_gap = 150, within hysteresis)
    actions = engine.decide(
        predicted_wh=-350.0,
        target_wh=-500.0,
        seconds_remaining=600,
        state=state,
        plugs=plugs,
        tesla=None,
    )

    # fan savings ≈ 12.5 Wh (fits), dehumidifier savings ≈ 50 Wh (fits)
    # Both fit in the gap but total savings (62.5) < abs_gap (150).
    # Hysteresis blocks entirely since abs_gap (150) < HYSTERESIS_WH (1000)
    # and no oversized device would benefit from bypass.
    assert len(actions) == 0


# --- QH boundary guards (near-end-of-quarter-hour skip) ---


def test_turn_on_skipped_when_seconds_remaining_below_min():
    """Skips plug turn-on when fewer than MIN_SECONDS_TO_ACT remain in QH.

    Near the end of a quarter-hour, turning on a plug wastes energy because
    it will stay on through the QH boundary and draw power not counted toward
    any surplus.  The same MIN_SECONDS_TO_ACT guard that protects Tesla actions
    should also protect plug turn-on decisions.

    Regression test for: jackery turned on with 21 s remaining in QH3,
    then left running through the entire next quarter-hour.
    """
    engine = GapMinder()
    state = StateTracker()

    plugs = {
        "jackery": PlugConfig(
            name="jackery",
            accessory_id="j1",
            power_watts=270.0,
        ),
    }

    with patch("load_nbc.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        actions = engine.decide(
            predicted_wh=-2000.0,  # big surplus: gap = -1500 Wh
            target_wh=-500.0,
            seconds_remaining=21,  # only 21 s left in QH3
            state=state,
            plugs=plugs,
            tesla=None,
        )

    assert len(actions) == 0


def test_turn_on_allowed_when_seconds_remaining_above_min():
    """Allows plug turn-on when enough time remains in the QH."""
    engine = GapMinder()
    state = StateTracker()

    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=4500.0,
        ),
    }

    with patch("load_nbc.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        actions = engine.decide(
            predicted_wh=-2000.0,  # big surplus: gap = -1500 Wh
            target_wh=-500.0,
            seconds_remaining=600,  # plenty of time left (10 min)
            state=state,
            plugs=plugs,
            tesla=None,
        )

    assert len(actions) == 1
    assert actions[0].action == "turn_on"


def test_turn_off_not_affected_by_min_seconds_guard():
    """Turn-off decisions are NOT blocked by MIN_SECONDS_TO_ACT.

    Turning off a device near the QH boundary is always safe — it saves
    energy regardless of how much time remains.  Only turn-on needs the guard.
    """
    engine = GapMinder()
    state = StateTracker()
    state.devices["pool_pump"] = DeviceState(
        name="pool_pump",
        desired_state=True,
    )

    plugs = {
        "pool_pump": PlugConfig(
            name="pool_pump",
            accessory_id="p1",
            power_watts=1500.0,
        ),
    }

    with patch("load_nbc.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        actions = engine.decide(
            predicted_wh=2000.0,  # deficit: gap = -2500 Wh (exceeds hysteresis)
            target_wh=-500.0,
            seconds_remaining=21,  # only 21 s left — turn-off should still fire
            state=state,
            plugs=plugs,
            tesla=None,
        )

    assert len(actions) == 1
    assert actions[0].action == "turn_off"


def test_turn_on_at_exact_min_seconds_boundary():
    """Turn-on is allowed when seconds_remaining equals MIN_SECONDS_TO_ACT exactly."""
    engine = GapMinder()
    state = StateTracker()

    plugs = {
        "heater": PlugConfig(
            name="heater",
            accessory_id="h1",
            power_watts=4500.0,
        ),
    }

    with patch("load_nbc.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        actions = engine.decide(
            predicted_wh=-2000.0,  # big surplus: gap = -1500 Wh
            target_wh=-500.0,
            seconds_remaining=60,  # exactly MIN_SECONDS_TO_ACT (1 min)
            state=state,
            plugs=plugs,
            tesla=None,
        )

    assert len(actions) == 1
