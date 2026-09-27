"""Unit tests for GapTrendTracker (ramp-aware Tesla stop, subtask 1).

Red phase: this module does not exist yet — these tests must fail
with ImportError until gap_trend.py is implemented.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gap_trend import GapTrendTracker


def _ts(base: datetime, secs: int) -> datetime:
    return base + timedelta(seconds=secs)


def test_single_sample_untrusted() -> None:
    """Fewer than 2 samples cannot produce a trend."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    rate, trusted = tracker.update(base, 17.7, qh_name="QH1")
    assert rate == 0.0
    assert trusted is False


def test_two_samples_untrusted_needs_confirmation() -> None:
    """One slope is not enough — require two consecutive same-sign slopes."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    tracker.update(base, 17.7, qh_name="QH1")
    rate, trusted = tracker.update(_ts(base, 30), 25.2, qh_name="QH1")
    assert trusted is False
    assert rate == 0.0


def test_sustained_ramp_trusted() -> None:
    """Sustained +0.25 Wh/s ramp over 3 samples is trusted."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    tracker.update(base, 17.7, qh_name="QH1")
    tracker.update(_ts(base, 30), 25.2, qh_name="QH1")  # +0.25/s
    rate, trusted = tracker.update(_ts(base, 60), 32.7, qh_name="QH1")
    assert trusted is True
    assert abs(rate - 0.25) < 1e-9


def test_flat_gap_zero_rate_untrusted() -> None:
    """Flat gap has zero slope — below any positive noise floor."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    tracker.update(base, 45.3, qh_name="QH1")
    tracker.update(_ts(base, 30), 45.3, qh_name="QH1")
    rate, trusted = tracker.update(
        _ts(base, 60), 45.3, qh_name="QH1", noise_floor=0.02
    )
    assert rate == 0.0
    assert trusted is False


def test_spike_then_revert_untrusted() -> None:
    """A spike that reverts (sign flip) never commits."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    tracker.update(base, 40.0, qh_name="QH1")
    tracker.update(_ts(base, 30), 48.0, qh_name="QH1")  # up
    rate, trusted = tracker.update(_ts(base, 60), 40.5, qh_name="QH1")  # down
    assert trusted is False
    assert rate == 0.0


def test_below_noise_floor_untrusted() -> None:
    """Tiny drift below the noise floor is untrusted even if sustained."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    tracker.update(base, 40.0, qh_name="QH1")
    tracker.update(_ts(base, 30), 40.3, qh_name="QH1")  # +0.01/s
    rate, trusted = tracker.update(
        _ts(base, 60), 40.6, qh_name="QH1", noise_floor=0.02
    )
    assert trusted is False
    assert rate == 0.0


def test_stale_identical_timestamp_no_crash() -> None:
    """Identical data_point_at (stale fetch) must not divide by zero."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    tracker.update(base, 40.0, qh_name="QH1")
    tracker.update(base, 45.0, qh_name="QH1")  # dt=0, ignored
    rate, trusted = tracker.update(_ts(base, 30), 50.0, qh_name="QH1")
    assert trusted is False
    assert rate == 0.0


def test_qh_change_resets() -> None:
    """QH rollover discards history — old-QH slope must not leak."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    tracker.update(base, 17.7, qh_name="QH1")
    tracker.update(_ts(base, 30), 25.2, qh_name="QH1")
    rate, trusted = tracker.update(_ts(base, 60), 32.7, qh_name="QH2")
    assert trusted is False
    assert rate == 0.0


def test_plateau_inside_ramp_stays_trusted() -> None:
    """A flat repeat inside a ramp (refetched data) keeps confirmation."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 56, 59, tzinfo=timezone.utc)
    tracker.update(base, 38.3, qh_name="QH1")
    tracker.update(_ts(base, 30), 38.3, qh_name="QH1")
    rate, trusted = tracker.update(_ts(base, 60), 45.1, qh_name="QH1")
    assert trusted is True
    assert rate > 0


def test_plateau_then_drop_trusted_negative() -> None:
    """Flat then down mirrors flat-then-up (consumers only act on r > 0)."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 56, 59, tzinfo=timezone.utc)
    tracker.update(base, 40.0, qh_name="QH1")
    tracker.update(_ts(base, 30), 40.0, qh_name="QH1")
    rate, trusted = tracker.update(_ts(base, 60), 38.0, qh_name="QH1")
    assert trusted is True
    assert rate < 0


def test_reset_discards_history() -> None:
    """Explicit reset clears samples and QH identity."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    tracker.update(base, 17.7, qh_name="QH1")
    tracker.update(_ts(base, 30), 25.2, qh_name="QH1")
    tracker.reset()
    rate, trusted = tracker.update(_ts(base, 60), 32.7, qh_name="QH1")
    assert trusted is False
    assert rate == 0.0
