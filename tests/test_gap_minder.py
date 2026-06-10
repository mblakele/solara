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
from load_nbc import DecideContext

fixed_now = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)


# --- Excess solar (turn on) tests ---


def test_hysteresis_no_action():
    """No action when within hysteresis margin."""
    engine = GapMinder()
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=None,
        ),
        predicted_wh=-500.0,
        target_wh=-500.0,
    )

    assert len(actions) == 0


def test_hysteresis_no_action_at_boundary():
    """No action exactly at +/-999 Wh (within margin)."""
    engine = GapMinder()
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=None,
        ),
        predicted_wh=-1500.0,
        target_wh=-500.0,
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
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=None,
        ),
        predicted_wh=-1000.0,
        target_wh=-500.0,
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
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=None,
        ),
        predicted_wh=-2000.0,
        target_wh=-500.0,
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
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=None,
        ),
        predicted_wh=-2500.0,
        target_wh=-500.0,
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
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=None,
        ),
        predicted_wh=-2500.0,
        target_wh=-500.0,
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
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=None,
        ),
        predicted_wh=-2000.0,
        target_wh=-500.0,
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
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=1000,
            state=state,
            plugs=plugs,
            tesla=None,
        ),
        predicted_wh=-2500.0,
        target_wh=-500.0,
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
            ctx=DecideContext(
                now=fixed_now,
                seconds_remaining=300,
                state=state,
                plugs=plugs,
                tesla=None,
            ),
            predicted_wh=-2000.0,
            target_wh=-500.0,
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
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=None,
        ),
        predicted_wh=2000.0,
        target_wh=-500.0,
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
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=1000,
            state=state,
            plugs=plugs,
            tesla=None,
        ),
        predicted_wh=2000.0,
        target_wh=500.0,
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
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=None,
        ),
        predicted_wh=2000.0,
        target_wh=-500.0,
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
        plugged_in=True,
        at_home=False,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=-2000.0,
        target_wh=-500.0,
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
        plugged_in=False,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=-2000.0,
        target_wh=-500.0,
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
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=711,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=-561.258618125,
        target_wh=-9.0,
    )

    assert len(actions) == 1
    assert actions[0].action == "set_amps"
    assert actions[0].device_name == "tesla"
    assert actions[0].target_amps == 16


def test_decide_tesla_increase_amps_7_9():
    """When predicted_wh < target and Tesla amps can increase,
    the engine should call set_amps with expected value."""
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=7,  # Low amps; reducing would drop below min of 5
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=231,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=-43.87908600000001,
        target_wh=-9.0,
    )

    assert len(actions) == 1
    assert actions[0].action == "set_amps"
    assert actions[0].device_name == "tesla"
    assert actions[0].target_amps == 9


def test_decide_tesla_reduce_below_min_clamps_amps():
    """When predicted_wh > target and Tesla amps would drop below min,
    the engine clamps to min amps instead of stopping."""
    engine = GapMinder()
    state = StateTracker()
    # No plugs to turn off — only Tesla is available for reduction
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=8,  # Low amps; reducing would drop below min of 5
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=2000.0,
        target_wh=-500.0,
    )

    # Engine reduces to min amps (5) instead of stopping
    assert len(actions) == 1
    assert actions[0].device_name == "tesla"
    assert actions[0].action == "set_amps"
    assert actions[0].target_amps == 5


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
        current_amps=5,
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=-3000.0,
        target_wh=-500.0,
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
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=None,
        ),
        predicted_wh=-450.0,
        target_wh=-500.0,
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
        plugged_in=True,
        at_home=True,
    )

    # Small over-target gap: predicted 350 Wh vs target -500 Wh → abs_gap = 150 Wh
    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=350.0,
        target_wh=-500.0,
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
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=None,
        ),
        predicted_wh=-350.0,
        target_wh=-500.0,
    )

    # fan savings ≈ 12.5 Wh (fits), dehumidifier savings ≈ 50 Wh (fits)
    # Both fit in the gap but total savings (62.5) < abs_gap (150).
    # Hysteresis blocks entirely since abs_gap (150) < HYSTERESIS_WH (1000)
    # and no oversized device would benefit from bypass.
    assert len(actions) == 0


# --- Deadband edge-gap tests ---


def test_edge_gap_reduces_surplus_turn_on():
    """With edge_gap, only plugs that fit within the deadband edge are turned on.

    gap = target - predicted = -500 - (-2000) = 1500 (surplus).
    h=1000 → edge_gap = 1500 - 1000 = 500.
    big_plug (1000W, 900s → 250 Wh) fits in edge_gap.
    huge_plug (2000W, 900s → 500 Wh) does NOT fit after big occupies 250.
    Old code turned on both; new code only turns on big.
    """
    engine = GapMinder(hysteresis_wh=1000)
    state = StateTracker()
    plugs = {
        "big": PlugConfig(
            name="big",
            accessory_id="b1",
            power_watts=1000.0,
        ),
        "huge": PlugConfig(
            name="huge",
            accessory_id="h1",
            power_watts=2000.0,
            priority=5,
        ),
    }

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=900,
            state=state,
            plugs=plugs,
            tesla=None,
        ),
        predicted_wh=-2000.0,
        target_wh=-500.0,
    )

    # huge (priority 5, 500 Wh) exactly fits edge_gap, big (priority 0) skipped
    assert len(actions) == 1
    assert actions[0].device_name == "huge"


def test_edge_gap_reduces_tesla_amps_reduction():
    """With edge_gap, Tesla amp reduction is computed against deadband edge.

    gap = target - predicted = 500 - 2000 = -1500 (deficit).
    h=1000 → edge_gap = 1500 - 1000 = 500.
    Old code reduces by ceil(1500*3600/(240*900))=25 amps → target=23.
    New code reduces by ceil(500*3600/(240*900))=9 amps  → target=39.
    """
    engine = GapMinder(hysteresis_wh=1000)
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=48,
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=900,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=2000.0,
        target_wh=500.0,
    )

    assert len(actions) == 1
    assert actions[0].action == "set_amps"
    # Old: 23 amps  New: 39 amps (edge_gap-based reduction)
    assert actions[0].target_amps == 39


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
    fixed_now = datetime(2026, 5, 7, 15, 59, 39, tzinfo=timezone.utc)
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
            ctx=DecideContext(
                now=fixed_now,
                seconds_remaining=21,
                state=state,
                plugs=plugs,
                tesla=None,
            ),
            predicted_wh=-2000.0,
            target_wh=-500.0,
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
            power_watts=1000.0,
        ),
    }

    with patch("load_nbc.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        actions = engine.decide(
            ctx=DecideContext(
                now=fixed_now,
                seconds_remaining=600,
                state=state,
                plugs=plugs,
                tesla=None,
            ),
            predicted_wh=-2000.0,
            target_wh=-500.0,
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
            ctx=DecideContext(
                now=fixed_now,
                seconds_remaining=21,
                state=state,
                plugs=plugs,
                tesla=None,
            ),
            predicted_wh=2000.0,
            target_wh=-500.0,
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
            ctx=DecideContext(
                now=fixed_now,
                seconds_remaining=60,
                state=state,
                plugs=plugs,
                tesla=None,
            ),
            predicted_wh=-2000.0,
            target_wh=-500.0,
        )

    assert len(actions) == 1


# --- Tesla amps clamping tests (belt-and-suspenders) ---


def test_decide_tesla_increase_clamped_to_custom_max():
    """When calculated target_amps exceeds charge_amps_max, it must be clamped.

    Regression guard: _decide_tesla_amps must never return a value above
    charge_amps_max, even when the gap would require more amps.
    """
    engine = GapMinder(
        hysteresis_wh=3,
        charge_amps_max=24,  # custom max lower than default 48
    )
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=22,
        plugged_in=True,
        at_home=True,
    )

    # Large gap: 5000 Wh surplus → huge needed_watts → target_amps well above 24
    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=200,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=-5000.0,
        target_wh=-500.0,
    )

    assert len(actions) == 1
    assert actions[0].action == "set_amps"
    assert actions[0].device_name == "tesla"
    # Raw calculation: needed_watts = 5000 * 3600 / 200 = 90000 W
    #amps = 90000 / 120 ≈ 750 → target = 22 + 728 = 750 → clamped to 24
    assert actions[0].target_amps == 24


def test_decide_tesla_reduce_clamped_to_custom_max():
    """_decide_tesla_reduce must clamp new_amps to charge_amps_max.

    Belt-and-suspenders: even if Tesla reports an anomalously high current_amps
    (above the configured max), the reduce path must not return a value exceeding
    charge_amps_max.  Directly calls the private method to verify the clamp.
    """
    engine = GapMinder(
        hysteresis_wh=3,
        charge_amps_max=24,
        charge_amps_min=5,
    )
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=48,  # above custom max of 24
        plugged_in=True,
        at_home=True,
    )

    action = engine._decide_tesla_reduce(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        reduce_wh=100.0,
        stop_allowed=False,
    )

    assert action is not None
    assert action.action == "set_amps"
    assert action.device_name == "tesla"
    assert action.target_amps <= 24


def test_decide_tesla_increase_no_clamp_when_within_max():
    """When calculated target_amps is within charge_amps_max, no clamp occurs.

    Ensures the clamp doesn't incorrectly limit valid values.
    """
    engine = GapMinder(
        hysteresis_wh=3,
        charge_amps_max=24,
    )
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=10,
        plugged_in=True,
        at_home=True,
    )

    # Small gap: -600 Wh vs target -500 Wh → gap = 100 Wh
    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=-600.0,
        target_wh=-500.0,
    )

    # With small gap, target_amps should be within range and not clamped
    assert len(actions) == 1
    assert actions[0].target_amps == 12


def test_decide_tesla_increase_hard_max_amps():
    """HARD_MAX_AMPS must cap target_amps even when charge_amps_max is higher.

    Regression guard: if config loading provides an anomalously high or
    absent charge_amps_max, the hard absolute max must still protect the
    vehicle.  This test verifies target_amps never exceeds HARD_MAX_AMPS.
    """
    engine = GapMinder(
        hysteresis_wh=3,
        charge_amps_max=60,  # above the hard max of 48
    )
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=22,
        plugged_in=True,
        at_home=True,
    )

    # Massive gap to push target well above 48
    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=200,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=-10000.0,
        target_wh=-500.0,
    )

    assert len(actions) == 1
    assert actions[0].action == "set_amps"
    assert actions[0].device_name == "tesla"
    # Even though charge_amps_max=60, the hard max (48) must win
    assert actions[0].target_amps <= 48


def test_decide_tesla_reduce_hard_max_amps():
    """_decide_tesla_reduce must respect HARD_MAX_AMPS.

    Regression guard: when current_amps is above the hard max,
    the reduce path must never return a value above HARD_MAX_AMPS.
    """
    engine = GapMinder(
        hysteresis_wh=3,
        charge_amps_max=60,  # above the hard max of 48
        charge_amps_min=5,
    )
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=48,  # at the hard max
        plugged_in=True,
        at_home=True,
    )

    # Reduce by a tiny amount, the resulting amps should be at most 48
    action = engine._decide_tesla_reduce(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        reduce_wh=10.0,
        stop_allowed=False,
    )

    assert action is not None
    assert action.action == "set_amps"
    assert action.device_name == "tesla"
    assert action.target_amps is not None
    assert action.target_amps <= 48


def test_turn_off_skip_plugs_when_tesla_reduce_exactly_fills_deficit():
    """When Tesla amp reduction exactly fills the deficit (remaining=0),
    on-plugs must not be turned off.

    Regression guard: _decide_turn_off checks remaining_reduction <= 0
    only AFTER appending a plug turn-off action.  When remaining is 0
    going into the plug loop, the first eligible plug is still turned
    off.  The fix must check remaining_reduction <= 0 before appending.
    """
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()
    # Ecoflow is on (270 W)
    state.devices["ecoflow"] = DeviceState(
        name="ecoflow",
        desired_state=True,
    )
    plugs = {
        "ecoflow": PlugConfig(
            name="ecoflow",
            accessory_id="eco1",
            power_watts=270.0,
        ),
    }
    tesla = TeslaState(
        is_charging=True,
        current_amps=48,
        plugged_in=True,
        at_home=True,
    )

    # gap = -500 - (-440) = -60 → abs_gap = 60 > 3, _decide_turn_off(ctx, 60)
    # seconds_remaining = 900 (full quarter-hour)
    # wh_to_amps(60, 900) = 60 * 3600 / (240 * 900) = 1.0 → ceil=1
    # new_amps = 48 - 1 = 47
    # delta = 48 - 47 = 1 >= 1 → action created
    # savings = 1 * 240 * 900 / 3600 = 60 Wh
    # remaining = 60 - 60 = 0 → exactly filled
    # Bug: without the fix, ecoflow (270W * 900/3600 = 67.5 Wh) still turned off
    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=900,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=-440.0,
        target_wh=-500.0,
    )

    # Only the Tesla set_amps action — no plug turn_off
    assert len(actions) == 1
    assert actions[0].device_name == "tesla"
    assert actions[0].action == "set_amps"


def test_turn_off_skip_plugs_when_tesla_reduce_underfills_deficit():
    """When Tesla amp reduction partially fills the deficit (remaining > 0),
    on-plugs should still be turned off.  Ensures the guard does not
    suppress legitimate turn-off actions when a real deficit remains.

    Uses a scenario where ceil rounds up but the charge_amps_min clamp
    prevents full coverage, leaving a residual deficit.
    """
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()
    state.devices["ecoflow"] = DeviceState(
        name="ecoflow",
        desired_state=True,
    )
    plugs = {
        "ecoflow": PlugConfig(
            name="ecoflow",
            accessory_id="eco1",
            power_watts=270.0,
        ),
    }
    # At 7A with 300s left and a 50 Wh deficit:
    #   wh_to_amps(50, 300) = 50*3600/(240*300) = 2.5 → ceil=3
    #   Without clamp: new_amps = 7 - 3 = 4
    #   With min clamp (stop_allowed=False): new_amps = max(4, 5) = 5
    #   Actual savings = (7-5) * 240 * 300 / 3600 = 40 Wh
    #   Remaining = 50 - 40 = 10 → plug still needed
    tesla = TeslaState(
        is_charging=True,
        current_amps=7,
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=-450.0,
        target_wh=-500.0,
    )

    # Both Tesla set_amps AND ecoflow turn_off should be returned
    assert len(actions) == 2
    assert actions[0].device_name == "tesla"
    assert actions[0].action == "set_amps"
    assert actions[0].target_amps == 5
    assert actions[1].device_name == "ecoflow"
    assert actions[1].action == "turn_off"


# --- Tesla 5A defer policy tests ---


def test_safe_defer_secs_calculates_correctly():
    """Unit test for _safe_defer_secs helper method."""
    engine = GapMinder()

    # gap=40 Wh → 40*3=120 → capped at MAX_DEFER_SECS=120
    assert engine._safe_defer_secs(40) == 120

    # gap=10 Wh → 10*3=30
    assert engine._safe_defer_secs(10) == 30

    # gap=0 Wh → 0
    assert engine._safe_defer_secs(0) == 0

    # gap=1 Wh → 1*3=3
    assert engine._safe_defer_secs(1) == 3


def test_decide_tesla_reduce_at_5a_defers_with_large_gap():
    """Tesla at 5A, seconds_remaining=300, turn_off path, gap=-1000 Wh.

    Safe window = min(120, 997*3) = 120 → defer (120 < 300).
    Step 1 returns None (stop not allowed at min amps). Step 3 delegates
    to _decide_tesla_reduce which defers (secs_remaining > safe_defer).
    """
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=500.0,
        target_wh=-500.0,
    )

    # Step 3 delegates to _decide_tesla_reduce with stop_allowed=True,
    # which defers because secs_remaining=300 > safe_defer=120.
    assert len(actions) == 0


def test_decide_tesla_reduce_at_5a_stops_with_tight_gap():
    """Tesla at 5A, seconds_remaining=50, turn_off path, gap=-1000 Wh.

    Safe window = min(120, 997*3) = 120 → stop (50 < 120).
    Step 1 returns None (stop not allowed at min amps). Step 3 delegates
    to _decide_tesla_reduce with stop_allowed=True, which stops because
    seconds_remaining < safe_defer.
    """
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=50,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=500.0,
        target_wh=-500.0,
    )

    assert len(actions) == 1
    assert actions[0].device_name == "tesla"
    assert actions[0].action == "turn_off"


def test_decide_tesla_reduce_at_5a_stops_with_zero_gap():
    """Tesla at 5A, seconds_remaining=300, gap=0 Wh.

    Safe window = min(120, 0*3) = 0 → stop (0 < 300).
    """
    engine = GapMinder()
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=-500.0,
        target_wh=-500.0,
    )

    # Within hysteresis (gap=0), so no action.
    assert len(actions) == 0


def test_decide_tesla_reduce_at_5a_defers_with_small_edge_gap():
    """Tesla at 5A, seconds_remaining=10, turn_off path, gap=-4 Wh.

    After hysteresis subtraction, edge_gap=1. Safe window = min(120, 1*3) = 3.
    secs_remaining=10 > 3 → defers. The small edge gap means the defer
    window is very short.
    """
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=10,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=-496.0,
        target_wh=-500.0,
    )

    assert len(actions) == 0


def test_decide_tesla_reduce_at_5a_defers_when_time_longer_than_safe_window():
    """Tesla at 5A, seconds_remaining=20, turn_off path, gap=-4 Wh.

    Safe window = min(120, 1*3) = 3 → defer (20 > 3).
    Step 3 delegates to _decide_tesla_reduce which defers.
    """
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=20,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=-496.0,
        target_wh=-500.0,
    )

    assert len(actions) == 0


def test_decide_turn_off_defers_at_min_amps_with_buffer():
    """Tesla at 5A (minimum), large gap, lots of QH time remaining.

    Step 1 returns None (amps-only, stop not allowed). Step 3 delegates
    to _decide_tesla_reduce with stop_allowed=True. Safe defer window
    = min(120, 342.5*3) = 120. secs_remaining=686 > 120 → defers.

    Matches the scenario from bugs/2026-06-08-tesla-stop.log:
    - predicted_wh=336.5, target_wh=-9.0  → gap=-345.5 → turn_off path
    - seconds_remaining=686
    - All plugs off (no entries in plugs dict)
    - Tesla at 5A, charging, at_home=True, plugged_in=True
    """
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=686,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=336.5,
        target_wh=-9.0,
    )

    # Step 3 delegates to _decide_tesla_reduce(stop_allowed=True).
    # safe_defer=120 < secs_remaining=686 → defers, no action.
    assert len(actions) == 0


def test_decide_tesla_reduce_at_5a_no_defer_without_stop_allowed():
    """Tesla at 5A, seconds_remaining=300, turn_off path, gap=-1000 Wh.

    Step 1 returns None (stop_allowed=False at min amps).
    Step 3 delegates to _decide_tesla_reduce(stop_allowed=True).
    safe_defer=120 < secs_remaining=300 → defers.
    """
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=5,
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=500.0,
        target_wh=-500.0,
    )

    # Step 3 delegates; safe_defer=120 < 300 → defers.
    assert len(actions) == 0


def test_decide_tesla_reduce_below_5a_defers_with_enough_time():
    """Tesla at 4A (edge case), seconds_remaining=200, turn_off path.

    Same defer logic applies (current_amps <= 5).
    Safe window = min(120, 997*3) = 120 → defer (120 < 200).
    Step 3 delegates to _decide_tesla_reduce which defers.
    """
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()
    plugs: dict[str, PlugConfig] = {}
    tesla = TeslaState(
        is_charging=True,
        current_amps=4,
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=200,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        predicted_wh=500.0,
        target_wh=-500.0,
    )

    # Step 3 delegates; safe_defer=120 < 200 → defers.
    assert len(actions) == 0


def test_decide_tesla_reduce_rounds_up_to_min_amps():
    """Tesla at 11A, gap=250.9 Wh, 656s remaining.

    wh_to_amps(250.9, 656) = 250.9*3600/(240*656) ≈ 5.737.
    With ceil: reduce_amps = 6, new_amps = 11 - 6 = 5 (min).
    Savings = 6 * 240 * 656 / 3600 = 262.4 Wh >= 250.9 gap.
    No plug toggle should occur.
    """
    engine = GapMinder(hysteresis_wh=3)
    state = StateTracker()
    state.devices["ecoflow"] = DeviceState(
        name="ecoflow",
        desired_state=True,
    )
    plugs = {
        "ecoflow": PlugConfig(
            name="ecoflow",
            accessory_id="eco1",
            power_watts=270.0,
        ),
    }
    tesla = TeslaState(
        is_charging=True,
        current_amps=11,
        plugged_in=True,
        at_home=True,
    )

    actions = engine.decide(
        ctx=DecideContext(
            now=fixed_now,
            seconds_remaining=656,
            state=state,
            plugs=plugs,
            tesla=tesla,
        ),
        # gap = target - predicted = -500 - (-249.1) = -250.9 -> turn_off 250.9
        predicted_wh=-249.1,
        target_wh=-500.0,
    )

    # Only Tesla set_amps to min — no plug turn_off
    assert len(actions) == 1
    assert actions[0].device_name == "tesla"
    assert actions[0].action == "set_amps"
    assert actions[0].target_amps == 5
