"""Regression: pruning the last Tesla set_amps effect must clear the orphaned command.

Reproduces bugs/2026-10-08-weird-behavior.log: a 12A command issued before
the QH rollover had its effect pruned ("previous QH") while
last_commanded_amps=12 survived. MQTT then froze at 5A, so
delta=7A persisted all quarter as 195-400 Wh phantom deficit.
"""

from datetime import datetime, timedelta, timezone

from load_models import PendingEffect
from load_nbc import StateTracker


def test_prune_last_tesla_effect_clears_orphaned_command():
    """Pruning the final Tesla set_amps effect clears last_commanded_amps."""
    tracker = StateTracker(prediction_window_seconds=30)
    t0 = datetime(2026, 10, 8, 19, 50, 0, tzinfo=timezone.utc)
    tracker.record_tesla_amp_command(12)
    tracker.add_effect(
        PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=t0,
            data_point_at=t0,
            power_watts=1680.0,
            target_amps=12,
            direction="increase",
            suppress_action="turn_off",
            qh_name="QH1",
        )
    )
    # Both ages exceed the 30 s window: effect is eligible for pruning.
    now = t0 + timedelta(seconds=120)
    data_point_at = t0 + timedelta(seconds=120)
    pruned = tracker.prune_old_effects(data_point_at, now)
    assert pruned == 1
    assert tracker.last_commanded_amps is None
    assert tracker.tesla_inflight_wh(5, 800, now, data_point_at=data_point_at) == 0.0


def test_prune_keeps_command_when_newer_tesla_effect_remains():
    """A newer surviving Tesla effect keeps its command alive."""
    tracker = StateTracker(prediction_window_seconds=30)
    t0 = datetime(2026, 10, 8, 19, 50, 0, tzinfo=timezone.utc)
    old = PendingEffect(
        device_name="tesla",
        action="set_amps",
        timestamp=t0,
        data_point_at=t0,
        power_watts=1680.0,
        target_amps=8,
        direction="increase",
        suppress_action="turn_off",
        qh_name="QH1",
    )
    fresh_ts = t0 + timedelta(seconds=110)
    fresh = PendingEffect(
        device_name="tesla",
        action="set_amps",
        timestamp=fresh_ts,
        data_point_at=fresh_ts,
        power_watts=720.0,
        target_amps=12,
        direction="increase",
        suppress_action="turn_off",
        qh_name="QH1",
    )
    tracker.record_tesla_amp_command(12)
    tracker.add_effect(old)
    tracker.add_effect(fresh)
    now = t0 + timedelta(seconds=120)
    pruned = tracker.prune_old_effects(fresh_ts, now)
    assert pruned == 1
    assert tracker.last_commanded_amps == 12
