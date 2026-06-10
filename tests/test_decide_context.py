"""Behavior-capture tests for GapMinder using DecideContext.

These tests verify that the DecideContext dataclass correctly replaces
the previous 8+ individual arguments on GapMinder.decide() and its
private helper methods.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from load_manager import (
    DeviceState,
    GapMinder,
    PlugConfig,
    StateTracker,
    TeslaState,
)
from load_nbc import DecideContext

fixed_now = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)


# --- DecideContext construction tests ---


class TestDecideContextConstruction:
    """Tests for DecideContext dataclass creation."""

    def test_minimal_context(self):
        """Minimal context with only required fields creates correctly."""
        state = StateTracker()
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs={},
            tesla=None,
        )
        assert ctx.now == fixed_now
        assert ctx.seconds_remaining == 600
        assert ctx.state is state
        assert ctx.plugs == {}
        assert ctx.tesla is None
        assert ctx.dry_run is False
        assert ctx.data_point_at is None

    def test_full_context(self):
        """Full context with all fields including optional ones."""
        state = StateTracker()
        tesla = TeslaState(
            is_charging=True,
            current_amps=10,
            plugged_in=True,
            at_home=True,
        )
        dp_at = fixed_now - timedelta(seconds=5)
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs={"heater": PlugConfig(
                name="heater",
                accessory_id="abc",
                power_watts=4500.0,
            )},
            tesla=tesla,
            dry_run=True,
            data_point_at=dp_at,
        )
        assert ctx.dry_run is True
        assert ctx.data_point_at == dp_at

    def test_context_is_frozen(self):
        """DecideContext should be immutable (frozen=True)."""
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=StateTracker(),
            plugs={},
            tesla=None,
        )
        with pytest.raises(Exception):
            ctx.now = fixed_now.replace(year=2027)

    def test_context_defaults(self):
        """Default values for optional fields are correct."""
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=StateTracker(),
            plugs={},
            tesla=None,
        )
        assert ctx.dry_run is False
        assert ctx.data_point_at is None


# --- decide() with DecideContext tests ---


class TestDecideWithContext:
    """Tests that GapMinder.decide() works with DecideContext."""

    def test_hysteresis_no_action_with_context(self):
        """No action when within hysteresis margin — using DecideContext."""
        engine = GapMinder()
        state = StateTracker()
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs={},
            tesla=None,
        )

        actions = engine.decide(
            ctx=ctx,
            predicted_wh=-500.0,
            target_wh=-500.0,
        )

        assert len(actions) == 0

    def test_turn_on_plug_with_context(self):
        """Turns on plug when gap exists — using DecideContext."""
        engine = GapMinder()
        state = StateTracker()
        plugs = {
            "water_heater": PlugConfig(
                name="water_heater",
                accessory_id="abc123",
                power_watts=4500.0,
            )
        }
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=None,
        )

        actions = engine.decide(
            ctx=ctx,
            predicted_wh=-2000.0,
            target_wh=-500.0,
        )

        assert len(actions) == 1
        assert actions[0].action == "turn_on"
        assert actions[0].device_name == "water_heater"

    def test_turn_off_plug_with_context(self):
        """Turns off plug when over target — using DecideContext."""
        engine = GapMinder()
        state = StateTracker()
        state.devices["pool_pump"] = DeviceState(
            name="pool_pump",
            desired_state=True,
        )
        plugs = {
            "pool_pump": PlugConfig(
                name="pool_pump",
                accessory_id="xyz",
                power_watts=1500.0,
            )
        }
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=None,
        )

        actions = engine.decide(
            ctx=ctx,
            predicted_wh=2000.0,
            target_wh=-500.0,
        )

        assert len(actions) == 1
        assert actions[0].action == "turn_off"
        assert actions[0].device_name == "pool_pump"

    def test_dry_run_with_context(self):
        """Dry-run mode does not mutate state.devices."""
        engine = GapMinder()
        state = StateTracker()
        plugs = {
            "heater": PlugConfig(
                name="heater",
                accessory_id="abc",
                power_watts=4500.0,
            )
        }
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=None,
            dry_run=True,
        )

        actions = engine.decide(
            ctx=ctx,
            predicted_wh=-2000.0,
            target_wh=-500.0,
        )

        assert len(actions) == 1
        # State should not be mutated in dry-run
        assert "heater" not in state.devices

    def test_data_point_at_propagated_to_effects(self):
        """data_point_at from context is used for PendingEffect timestamps."""
        engine = GapMinder()
        state = StateTracker()
        dp_at = fixed_now - timedelta(seconds=10)
        plugs = {
            "heater": PlugConfig(
                name="heater",
                accessory_id="abc",
                power_watts=4500.0,
            )
        }
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=None,
            data_point_at=dp_at,
        )

        actions = engine.decide(
            ctx=ctx,
            predicted_wh=-2000.0,
            target_wh=-500.0,
        )

        assert len(actions) == 1
        assert actions[0].data_point_at == dp_at


# --- _decide_tesla_amps with context tests ---


class TestDecideTeslaAmpsWithContext:
    """Tests for _decide_tesla_amps using DecideContext."""

    def test_tesla_amps_increase_with_context(self):
        """Tesla amps increase decision works with DecideContext."""
        engine = GapMinder(hysteresis_wh=3)
        state = StateTracker()
        tesla = TeslaState(
            is_charging=True,
            current_amps=5,
            plugged_in=True,
            at_home=True,
        )
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=711,
            state=state,
            plugs={},
            tesla=tesla,
        )

        actions = engine.decide(
            ctx=ctx,
            predicted_wh=-561.258618125,
            target_wh=-9.0,
        )

        assert len(actions) == 1
        assert actions[0].action == "set_amps"
        assert actions[0].device_name == "tesla"
        assert actions[0].target_amps == 16

    def test_tesla_amps_skipped_when_just_started_charging(self):
        """Tesla amps increase skipped when current_amps < charge_amps_min."""
        engine = GapMinder(hysteresis_wh=3)
        state = StateTracker()
        tesla = TeslaState(
            is_charging=True,
            current_amps=1,
            plugged_in=True,
            at_home=True,
        )
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=711,
            state=state,
            plugs={},
            tesla=tesla,
        )

        actions = engine.decide(
            ctx=ctx,
            predicted_wh=-561.258618125,
            target_wh=-9.0,
        )

        assert len(actions) == 0

    def test_tesla_amps_reduce_with_context(self):
        """Tesla amps reduce decision works with DecideContext."""
        engine = GapMinder()
        state = StateTracker()
        tesla = TeslaState(
            is_charging=True,
            current_amps=48,
            plugged_in=True,
            at_home=True,
        )
        plugs: dict[str, PlugConfig] = {}
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=tesla,
        )

        actions = engine.decide(
            ctx=ctx,
            predicted_wh=2000.0,
            target_wh=-500.0,
        )

        # Tesla should be reduced (or stopped) to cover deficit
        assert len(actions) >= 1
        assert any(a.device_name == "tesla" for a in actions)


# --- _decide_turn_on/_decide_turn_off with context tests ---


class TestDecideTurnOnOffWithContext:
    """Tests for _decide_turn_on/_decide_turn_off using DecideContext."""

    def test_priority_ordering_with_context(self):
        """Higher priority plug turns on first — using DecideContext."""
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
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=None,
        )

        actions = engine.decide(
            ctx=ctx,
            predicted_wh=-2000.0,
            target_wh=-500.0,
        )

        assert actions[0].device_name == "high_pri"
        assert actions[1].device_name == "low_pri"

    def test_bin_pack_multiple_with_context(self):
        """Fits multiple plugs to fill gap — using DecideContext."""
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
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=1000,
            state=state,
            plugs=plugs,
            tesla=None,
        )

        actions = engine.decide(
            ctx=ctx,
            predicted_wh=-2500.0,
            target_wh=-500.0,
        )

        assert len(actions) == 2

    def test_turn_off_multiple_with_context(self):
        """Turns off all on plugs in priority order — using DecideContext."""
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
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=600,
            state=state,
            plugs=plugs,
            tesla=None,
        )

        actions = engine.decide(
            ctx=ctx,
            predicted_wh=2000.0,
            target_wh=-500.0,
        )

        assert len(actions) == 2
        assert actions[0].action == "turn_off"
        assert actions[0].device_name == "pool_pump"


# --- Negative/edge cases with context ---


class TestEdgeCasesWithContext:
    """Edge case tests using DecideContext."""

    def test_skip_debounce_with_context(self):
        """Skips plug that toggled too recently — using DecideContext."""
        from unittest.mock import patch

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
        ctx = DecideContext(
            now=fixed_now,
            seconds_remaining=300,
            state=state,
            plugs=plugs,
            tesla=None,
        )

        with patch("load_nbc.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            actions = engine.decide(
                ctx=ctx,
                predicted_wh=-2000.0,
                target_wh=-500.0,
            )

        assert len(actions) == 0

    def test_skip_seconds_remaining_too_low_with_context(self):
        """Skips turn-on when too little time remains — using DecideContext."""
        from unittest.mock import patch

        engine = GapMinder()
        state = StateTracker()
        near_end = datetime(2026, 5, 7, 15, 59, 39, tzinfo=timezone.utc)
        plugs = {
            "jackery": PlugConfig(
                name="jackery",
                accessory_id="j1",
                power_watts=270.0,
            ),
        }
        ctx = DecideContext(
            now=near_end,
            seconds_remaining=21,
            state=state,
            plugs=plugs,
            tesla=None,
        )

        with patch("load_nbc.datetime") as mock_dt:
            mock_dt.now.return_value = near_end
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            actions = engine.decide(
                ctx=ctx,
                predicted_wh=-2000.0,
                target_wh=-500.0,
            )

        assert len(actions) == 0
