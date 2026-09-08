"""Construction tests for the DecideContext dataclass.

Behavioral decide() coverage lives in test_gap_minder.py (including the
relocated dry-run and data_point_at propagation tests); this module pins
only the dataclass shape and defaults.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from load_models import PlugConfig, TeslaState
from load_nbc import DecideContext, StateTracker

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

