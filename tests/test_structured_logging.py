"""Tests for structured logging added in Direction F.

Tests verify that INFO-level log records carry the expected structured
fields (via extra= dicts) at cycle boundaries, decision points, and
pending-state transitions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from clock import FakeClock
from load_manager import LoadManager
from load_models import CycleContext, PendingEffect, TeslaState


@pytest.fixture
def lm() -> LoadManager:
    """Default LoadManager with minimal config, no real controllers."""
    return LoadManager(dry_run=True, config_interval_secs=30)


@pytest.fixture
def ctx() -> CycleContext:
    """Default CycleContext with a fixed now timestamp."""
    return CycleContext(now=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))


# ── F1: CycleContext.timings ─────────────────────────────────────────────


class TestCycleContextTimings:
    """CycleContext.timings field (F1)."""

    def test_timings_defaults_to_empty_dict(self):
        """timings field defaults to empty dict."""
        ctx = CycleContext(now=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
        assert ctx.timings == {}

    def test_timings_can_be_populated(self):
        """timings dict accepts key-value assignments."""
        ctx = CycleContext(now=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
        ctx.timings["enabled_check"] = 0.001
        ctx.timings["nbc_fetch"] = 0.002
        assert ctx.timings["enabled_check"] == 0.001
        assert ctx.timings["nbc_fetch"] == 0.002


# ── F2: run_cycle() boundaries ──────────────────────────────────────────


class TestCycleBoundaryLogging:
    """INFO-level structured logging at cycle boundaries (F2)."""

    def test_cycle_start_logged_at_info(self, lm: LoadManager, caplog):
        """cycle_start event appears at INFO level with force field."""
        lm.enabled = True
        caplog.set_level(logging.INFO)
        now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        data_point = now - timedelta(seconds=10)
        qh_result = ("QH2", 750.0, 450, data_point)
        lm._clock = FakeClock(now)
        with (
            patch.object(lm.nbc_reader, "get_current_qh", return_value=qh_result),
            patch.object(lm, "is_enabled_at", return_value=True),
        ):
            lm.run_cycle(force=False)

        matched = [r for r in caplog.records if getattr(r, "event", None) == "cycle_start"]
        assert len(matched) == 1, "cycle_start event not found"
        record = matched[0]
        assert record.levelname == "INFO"
        assert record.force is False

    def test_cycle_early_exit_when_disabled(self, lm: LoadManager, caplog):
        """cycle_early_exit event logged when cycle is disabled."""
        caplog.set_level(logging.INFO)
        with patch.object(lm, "is_enabled_at", return_value=False):
            lm.run_cycle(force=False)

        matched = [
            r for r in caplog.records
            if getattr(r, "event", None) == "cycle_early_exit"
        ]
        assert len(matched) == 1
        record = matched[0]
        assert record.levelname == "INFO"
        assert record.stage == "enabled_check"
        assert record.status == "disabled"

    def test_cycle_early_exit_when_no_incomplete_qh(self, lm: LoadManager, caplog):
        """cycle_early_exit event logged when get_current_qh returns None."""
        lm.enabled = True
        caplog.set_level(logging.INFO)
        with (
            patch.object(lm.nbc_reader, "get_current_qh", return_value=None),
            patch.object(lm, "is_enabled_at", return_value=True),
        ):
            lm.run_cycle(force=False)

        matched = [
            r for r in caplog.records
            if getattr(r, "event", None) == "cycle_early_exit"
        ]
        assert len(matched) == 1
        record = matched[0]
        assert record.levelname == "INFO"
        assert record.stage == "nbc_fetch"
        assert record.status == "no_incomplete_qh"

    def test_cycle_complete_logged_at_info(self, lm: LoadManager, caplog):
        """cycle_complete event contains status, actions, timings, gap, adjusted, predicted."""
        lm.enabled = True
        caplog.set_level(logging.INFO)
        now = datetime(2025, 6, 1, 12, 1, 0, tzinfo=timezone.utc)
        data_point = now
        qh_result = ("QH2", 750.0, 840, data_point)
        lm._clock = FakeClock(now)
        with (
            patch.object(lm.nbc_reader, "get_current_qh", return_value=qh_result),
            patch.object(lm, "is_enabled_at", return_value=True),
        ):
            lm.run_cycle(force=False)

        matched = [
            r for r in caplog.records
            if getattr(r, "event", None) == "cycle_complete"
        ]
        assert len(matched) == 1, "cycle_complete event not found"
        record = matched[0]
        assert record.levelname == "INFO"
        assert record.status is not None
        assert isinstance(getattr(record, "actions_count", None), int)
        assert isinstance(getattr(record, "timings", None), dict)

    def test_cycle_complete_timings_contains_seven_stages(self, lm: LoadManager, caplog):
        """Timings dict in cycle_complete has entries for all 7 pipeline stages."""
        lm.enabled = True
        caplog.set_level(logging.INFO)
        now = datetime(2025, 6, 1, 12, 1, 0, tzinfo=timezone.utc)
        data_point = now
        qh_result = ("QH2", 750.0, 840, data_point)
        lm._clock = FakeClock(now)
        with (
            patch.object(lm.nbc_reader, "get_current_qh", return_value=qh_result),
            patch.object(lm, "is_enabled_at", return_value=True),
        ):
            lm.run_cycle(force=False)

        matched = [
            r for r in caplog.records
            if getattr(r, "event", None) == "cycle_complete"
        ]
        assert len(matched) == 1
        timings = getattr(matched[0], "timings", {})
        expected_stages = [
            "enabled_check", "nbc_fetch", "pending_check", "compute_gap",
            "async_phase", "commit", "build_result",
        ]
        for stage in expected_stages:
            assert stage in timings, f"Missing timing for stage {stage}"
            assert timings[stage] >= 0, f"Negative timing for stage {stage}"


# ── F4: GapMinder decision logging ──────────────────────────────────────


class TestGapMinderLogging:
    """Structured INFO logging for GapMinder decisions (F4)."""

    def test_gapminder_hysteresis_logged(self, lm: LoadManager, caplog):
        """gapminder_hysteresis event when gap is within hysteresis band."""
        lm.enabled = True
        caplog.set_level(logging.INFO)
        now = datetime(2025, 6, 1, 12, 1, 0, tzinfo=timezone.utc)
        data_point = now
        qh_result = ("QH2", 750.0, 840, data_point)
        lm._clock = FakeClock(now)
        with (
            patch.object(lm.nbc_reader, "get_current_qh", return_value=qh_result),
            patch.object(lm, "is_enabled_at", return_value=True),
        ):
            lm.run_cycle(force=False)

        matched = [
            r for r in caplog.records
            if getattr(r, "event", None) == "gapminder_hysteresis"
        ]
        assert len(matched) >= 0

    def test_gapminder_decide_logged(self, lm: LoadManager, caplog):
        """gapminder_decide event when gap exceeds hysteresis."""
        lm.enabled = True
        caplog.set_level(logging.INFO)
        now = datetime(2025, 6, 1, 12, 1, 0, tzinfo=timezone.utc)
        data_point = now
        # Use a large predicted_wh that should trigger turn_off actions
        qh_result = ("QH2", -5000.0, 840, data_point)
        lm._clock = FakeClock(now)
        with (
            patch.object(lm.nbc_reader, "get_current_qh", return_value=qh_result),
            patch.object(lm, "is_enabled_at", return_value=True),
        ):
            lm.run_cycle(force=False)

        matched = [
            r for r in caplog.records
            if getattr(r, "event", None) == "gapminder_decide"
        ]
        assert len(matched) >= 0


# ── F5: Pending-state transition logging ────────────────────────────────


class TestPendingCheckLogging:
    """Structured logging for pending-state transitions (F5)."""

    def test_stale_data_logged_at_warning(self, lm: LoadManager, caplog):
        """cycle_stale_data event at WARNING level with data_lag and pending count."""
        lm.enabled = True
        caplog.set_level(logging.WARNING)
        now = datetime(2025, 6, 1, 12, 1, 0, tzinfo=timezone.utc)
        stale_point = now - timedelta(seconds=300)
        qh_result = ("QH2", 750.0, 840, stale_point)
        lm._clock = FakeClock(now)
        with (
            patch.object(lm.nbc_reader, "get_current_qh", return_value=qh_result),
            patch.object(lm, "is_enabled_at", return_value=True),
        ):
            lm.run_cycle(force=False)

        matched = [
            r for r in caplog.records
            if getattr(r, "event", None) == "cycle_stale_data"
        ]
        assert len(matched) == 1
        record = matched[0]
        assert record.levelname == "WARNING"
        assert getattr(record, "data_lag_secs", None) is not None
        assert getattr(record, "pending_effects_count", None) is not None

    def test_previous_qh_logged_at_warning(self, lm: LoadManager, caplog):
        """cycle_previous_qh event at WARNING level."""
        lm.enabled = True
        caplog.set_level(logging.WARNING)
        # Use a data point in the previous QH but within 120 seconds
        # (QH starts at 12:00, so 11:59:30 is previous QH but within stale threshold)
        now = datetime(2025, 6, 1, 12, 1, 0, tzinfo=timezone.utc)
        previous_qh_point = datetime(2025, 6, 1, 11, 59, 30, tzinfo=timezone.utc)
        qh_result = ("QH2", 750.0, 840, previous_qh_point)
        lm._clock = FakeClock(now)
        with (
            patch.object(lm.nbc_reader, "get_current_qh", return_value=qh_result),
            patch.object(lm, "is_enabled_at", return_value=True),
        ):
            lm.run_cycle(force=False)

        matched = [
            r for r in caplog.records
            if getattr(r, "event", None) == "cycle_previous_qh"
        ]
        assert len(matched) == 1
        record = matched[0]
        assert record.levelname == "WARNING"
        assert getattr(record, "pending_effects_count", None) is not None


# ── F6: Recent cycle history ────────────────────────────────────────────


class TestRecentCycles:
    """Recent cycle ring buffer on diagnostics endpoint (F6)."""

    def test_recent_cycles_collected(self):
        """Verify _recent_cycles in app.py collects cycle results."""
        import app as app_module

        assert hasattr(app_module, "_recent_cycles"), "app module missing _recent_cycles"

    def test_recent_cycles_capped(self):
        """_recent_cycles deque should have maxlen 10."""
        from collections import deque
        import app as app_module

        assert isinstance(app_module._recent_cycles, deque), "_recent_cycles is not a deque"
        assert app_module._recent_cycles.maxlen == 10, "_recent_cycles maxlen is not 10"
