"""Tests for StateTracker."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from load_manager import (
    PlugConfig,
    DeviceState,
    PendingEffect,
    StateTracker,
)

fixed_now = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)

def test_watts_to_wh():
    """Test calculation of wh impact of a load in watts."""
    watts = 300
    wh = StateTracker.watts_to_wh(watts, 600)
    assert wh == 50


def test_can_toggle_true_when_never_toggled():
    """True when device never toggled."""
    tracker = StateTracker()
    now = datetime.now(timezone.utc)
    assert tracker.can_toggle("plug", now) is True


def test_can_toggle_on_true_after_debounce():
    """True after MIN_TOGGLE_ON_SECS elapsed."""
    tracker = StateTracker()
    tracker.devices["plug"] = DeviceState(
        name="plug",
        last_toggle=datetime.now(timezone.utc) - timedelta(seconds=91),
        actual_state=True,
    )
    now = datetime.now(timezone.utc)
    assert tracker.can_toggle("plug", now) is True


def test_can_toggle_on_false_before_debounce():
    """False before MIN_TOGGLE_ON_SECS elapsed."""
    tracker = StateTracker()

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        tracker.devices["plug"] = DeviceState(
            name="plug",
            last_toggle=fixed_now - timedelta(seconds=30),
            actual_state=True,
        )

    assert tracker.can_toggle("plug", fixed_now) is False


def test_can_toggle_off_true_after_debounce():
    """True after MIN_TOGGLE_OFF_SECS elapsed."""
    tracker = StateTracker()

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        tracker.devices["plug"] = DeviceState(
            name="plug",
            last_toggle=fixed_now - timedelta(seconds=91),
            actual_state=False,
        )

    assert tracker.can_toggle("plug", fixed_now) is True


def test_can_toggle_off_false_before_debounce():
    """False before MIN_TOGGLE_OFF_SECS elapsed."""
    tracker = StateTracker()

    with patch("load_manager.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        tracker.devices["plug"] = DeviceState(
            name="plug",
            last_toggle=fixed_now - timedelta(seconds=30),
            actual_state=False,
        )

    assert tracker.can_toggle("plug", fixed_now) is False


def test_has_pending_effect_since():
    """True when effect after NBC fetch."""
    tracker = StateTracker()
    tracker.pending_effects.append(
        PendingEffect(
            device_name="plug",
            action="turn_on",
            timestamp=datetime(2025, 1, 1, 0, 0, 30, tzinfo=timezone.utc),
            data_point_at=datetime(2025, 1, 1, 0, 0, 10, tzinfo=timezone.utc),
            power_delta_wh=100.0,
        )
    )
    assert tracker.has_pending_effect_since(datetime(2025, 1, 1, tzinfo=timezone.utc)) is True


def test_estimated_current_wh_adds_pending():
    """Adds pending effect delta to NBC prediction."""
    tracker = StateTracker()
    now = datetime.now(timezone.utc)
    tracker.pending_effects.append(
        PendingEffect(
            device_name="plug",
            action="turn_on",
            timestamp=now,
            data_point_at=now - timedelta(seconds=20),
            power_delta_wh=200.0,
        )
    )
    estimated = tracker.estimated_current_wh(1000.0)
    assert pytest.approx(estimated) == 1200.0


def test_estimated_current_wh_no_pending():
    """Returns raw prediction when no pending effects."""
    tracker = StateTracker()
    estimated = tracker.estimated_current_wh(1000.0)
    assert pytest.approx(estimated) == 1000.0


def test_estimated_current_wh_multiple_effects():
    """Sums all pending effect deltas."""
    tracker = StateTracker()
    now = datetime.now(timezone.utc)
    tracker.pending_effects.extend([
        PendingEffect(
            device_name="a", action="turn_on",
            timestamp=now,
            data_point_at=now - timedelta(seconds=20),
            power_delta_wh=200.0,
        ),
        PendingEffect(
            device_name="b", action="turn_off",
            timestamp=now,
            data_point_at=now - timedelta(seconds=20),
            power_delta_wh=-100.0,
        ),
    ])
    estimated = tracker.estimated_current_wh(1000.0)
    assert pytest.approx(estimated) == 1100.0


def test_pending_since_count_empty():
    """Returns 0 when no pending effects."""
    tracker = StateTracker()
    now = datetime.now(timezone.utc)
    assert tracker.pending_since_count(now) == 0
