"""Tests for CycleContext dataclass (Direction A, Phase 1)."""

from __future__ import annotations

from datetime import datetime, timezone

from load_models import CycleContext, PendingEffect, TeslaState


class TestCycleContextConstruction:
    """CycleContext can be constructed with default and explicit values."""

    def test_default_now_raises(self):
        """CycleContext requires 'now' — no default."""
        ctx = CycleContext(now=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
        assert ctx.now == datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert ctx.force is False

    def test_force_true(self):
        """force=True is reflected in the context."""
        ctx = CycleContext(
            now=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc), force=True
        )
        assert ctx.force is True

    def test_default_values(self):
        """All optional fields default to None or sensible defaults."""
        ctx = CycleContext(now=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
        assert ctx.qh_name is None
        assert ctx.predicted_wh is None
        assert ctx.seconds_remaining is None
        assert ctx.data_point_at is None
        assert ctx.adjusted_wh is None
        assert ctx.gap_wh is None
        assert ctx.tesla_state is None
        assert ctx.tesla_error is None
        assert ctx.tesla_login_url is None
        assert ctx.succeeded_effects == []
        assert ctx.actions == []
        assert ctx.sentinel_on is False


class TestCycleContextPopulation:
    """CycleContext fields can be populated after construction (not frozen)."""

    def test_populate_stage2_outputs(self):
        """Fields set by stage 2 (NBC fetch) can be assigned."""
        ctx = CycleContext(now=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
        ctx.qh_name = "QH2"
        ctx.predicted_wh = 500.0
        ctx.seconds_remaining = 450
        ctx.data_point_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert ctx.qh_name == "QH2"
        assert ctx.predicted_wh == 500.0
        assert ctx.seconds_remaining == 450
        assert ctx.data_point_at == datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_populate_stage4_outputs(self):
        """Fields set by stage 4 (compute gap) can be assigned."""
        ctx = CycleContext(now=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
        ctx.adjusted_wh = 450.0
        ctx.gap_wh = -50.0
        assert ctx.adjusted_wh == 450.0
        assert ctx.gap_wh == -50.0

    def test_populate_stage5_outputs(self):
        """Fields set by stage 5 (async phase) can be assigned."""
        ctx = CycleContext(now=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
        ctx.tesla_state = TeslaState(
            is_charging=True,
            current_amps=16,
            plugged_in=True,
            at_home=True,
        )
        ctx.tesla_error = "mock error"
        ctx.tesla_login_url = "https://example.com/login"
        ctx.succeeded_effects = [
            PendingEffect(
                device_name="plug1",
                action="turn_on",
                timestamp=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
                data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
                power_watts=1500.0,
            )
        ]
        ctx.actions = [
            PendingEffect(
                device_name="plug1",
                action="turn_on",
                timestamp=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
                data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
                power_watts=1500.0,
            )
        ]
        ctx.sentinel_on = True
        assert ctx.tesla_state is not None
        assert ctx.tesla_state.is_charging is True
        assert ctx.tesla_error == "mock error"
        assert ctx.tesla_login_url == "https://example.com/login"
        assert len(ctx.succeeded_effects) == 1
        assert len(ctx.actions) == 1
        assert ctx.sentinel_on is True
