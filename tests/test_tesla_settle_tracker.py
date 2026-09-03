"""Tests for TeslaSettleTracker (extracted from StateTracker)."""

from datetime import datetime, timedelta, timezone

from load_models import PendingEffect
from load_nbc import EffectStore, StateTracker, TeslaSettleTracker

FIXED_NOW = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)


def _settle_effect(
    age_secs: int = 10,
    direction: str = "increase",
    qh_name: str = "QH1",
) -> PendingEffect:
    """Build a Tesla set_amps settle effect aged by age_secs."""
    ts = FIXED_NOW - timedelta(seconds=age_secs)
    return PendingEffect(
        device_name="tesla",
        action="set_amps",
        timestamp=ts,
        data_point_at=ts,
        power_watts=0,
        target_amps=20,
        direction=direction,  # type: ignore[arg-type]
        suppress_action="turn_off",  # type: ignore[arg-type]
        qh_name=qh_name,
    )


def _tracker(window_secs: int = 30) -> TeslaSettleTracker:
    """Build a tracker with an empty effect store."""
    return TeslaSettleTracker(EffectStore(), window_secs=window_secs)


def test_record_and_clear_command():
    """record_command stores amps; recording None clears."""
    tracker = _tracker()
    assert tracker.last_commanded_amps is None
    tracker.record_command(18)
    assert tracker.last_commanded_amps == 18
    tracker.record_command(None)
    assert tracker.last_commanded_amps is None


def test_inflight_zero_when_no_command():
    """No command or no report yields zero Wh."""
    tracker = _tracker()
    assert tracker.inflight_wh(10, 900, FIXED_NOW) == 0.0
    tracker.record_command(18)
    assert tracker.inflight_wh(None, 900, FIXED_NOW) == 0.0


def test_inflight_confirmed_level_is_zero():
    """Reported amps equal to commanded amps yields zero Wh."""
    tracker = _tracker()
    tracker.record_command(18)
    assert tracker.inflight_wh(18, 900, FIXED_NOW) == 0.0


def test_inflight_single_zero_keeps_command():
    """A single 0 A frame keeps the full commanded delta accounted."""
    tracker = _tracker()
    tracker.record_command(18)
    result = tracker.inflight_wh(0, 900, FIXED_NOW)
    assert result > 0.0
    assert tracker.last_commanded_amps == 18


def test_inflight_two_zeroes_clear_command():
    """Two consecutive 0 A frames clear the command."""
    tracker = _tracker()
    tracker.record_command(18)
    tracker.inflight_wh(0, 900, FIXED_NOW)
    result = tracker.inflight_wh(0, 900, FIXED_NOW)
    assert result == 0.0
    assert tracker.last_commanded_amps is None


def test_is_settling_matches_get_active():
    """is_settling is True exactly when get_active returns an effect."""
    tracker = _tracker()
    assert tracker.is_settling(FIXED_NOW) is False
    assert tracker.get_active(FIXED_NOW) is None
    tracker.effects.add(_settle_effect(age_secs=10))
    assert tracker.is_settling(FIXED_NOW) is True
    assert tracker.get_active(FIXED_NOW) is not None


def test_settle_expires_across_qh():
    """A settle effect from another QH is expired."""
    tracker = _tracker()
    tracker.effects.add(_settle_effect(age_secs=10, qh_name="QH2"))
    assert tracker.get_active(FIXED_NOW, current_qh="QH1") is None
    assert tracker.is_settling(FIXED_NOW, current_qh="QH1") is False


def test_state_tracker_delegates_command_state():
    """StateTracker.last_commanded_amps stays settable and in sync."""
    tracker = StateTracker()
    tracker.last_commanded_amps = 12
    assert tracker.settle.last_commanded_amps == 12
    tracker.record_tesla_amp_command(20)
    assert tracker.last_commanded_amps == 20
    assert tracker.settle.last_commanded_amps == 20


def test_state_tracker_window_sync():
    """Committed prediction-window changes propagate to the settle tracker."""
    tracker = StateTracker(prediction_window_seconds=30)
    assert tracker.settle.window_secs == 30
    tracker.apply_prediction_window(120)
    tracker.apply_prediction_window(120)
    assert tracker.effective_settle_secs == 120
    assert tracker.settle.window_secs == 120
