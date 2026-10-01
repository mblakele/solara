"""Unit tests for GapTrendTracker (ramp-aware Tesla stop).

The tracker keys its quarter-hour identity on ``floor_to_qh(data_point_at)``
rather than a caller-supplied label, because ``ParsedMetricsQH.qh_name`` is
the hardcoded literal ``"QH1"`` for every incomplete quarter
(load_nbc.py:411) and so can never signal a rollover.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from constants import GAP_TREND_MAX_SPAN_SECS
from gap_trend import GapTrendTracker


def _ts(base: datetime, secs: int) -> datetime:
    return base + timedelta(seconds=secs)


def test_single_sample_untrusted() -> None:
    """Fewer than 2 samples cannot produce a trend."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    rate, trusted = tracker.update(base, 17.7)
    assert rate == 0.0
    assert trusted is False


def test_two_samples_untrusted_needs_confirmation() -> None:
    """One slope is not enough — require two confirming slopes."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    tracker.update(base, 17.7)
    rate, trusted = tracker.update(_ts(base, 30), 25.2)
    assert trusted is False
    assert rate == 0.0


def test_sustained_ramp_trusted() -> None:
    """Sustained +0.25 Wh/s ramp over 3 samples is trusted."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    tracker.update(base, 17.7)
    tracker.update(_ts(base, 30), 25.2)  # +0.25/s
    rate, trusted = tracker.update(_ts(base, 60), 32.7)
    assert trusted is True
    assert abs(rate - 0.25) < 1e-9


def test_flat_gap_zero_rate_untrusted() -> None:
    """Flat gap has zero slope — below any positive noise floor."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    tracker.update(base, 45.3)
    tracker.update(_ts(base, 30), 45.3)
    rate, trusted = tracker.update(_ts(base, 60), 45.3, noise_floor=0.02)
    assert rate == 0.0
    assert trusted is False


def test_spike_then_revert_untrusted() -> None:
    """A spike that reverts (sign flip) never commits."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    tracker.update(base, 40.0)
    tracker.update(_ts(base, 30), 48.0)  # up
    rate, trusted = tracker.update(_ts(base, 60), 40.5)  # down
    assert trusted is False
    assert rate == 0.0


def test_below_noise_floor_untrusted() -> None:
    """Tiny drift below the noise floor is untrusted even if sustained."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    tracker.update(base, 40.0)
    tracker.update(_ts(base, 30), 40.3)  # +0.01/s
    rate, trusted = tracker.update(_ts(base, 60), 40.6, noise_floor=0.02)
    assert trusted is False
    assert rate == 0.0


def test_stale_identical_timestamp_no_crash() -> None:
    """Identical data_point_at (stale fetch) must not divide by zero."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    tracker.update(base, 40.0)
    rate, trusted = tracker.update(base, 45.0)  # dt=0, ignored
    assert trusted is False
    assert rate == 0.0
    assert tracker.update(_ts(base, 30), 50.0) == (0.0, False)


def test_plateau_inside_ramp_stays_trusted() -> None:
    """A flat repeat inside a ramp (refetched data) keeps confirmation."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 51, 4, tzinfo=timezone.utc)
    tracker.update(base, 38.3)
    tracker.update(_ts(base, 30), 38.3)
    rate, trusted = tracker.update(_ts(base, 60), 45.1)
    assert trusted is True
    assert rate > 0


def test_plateau_then_drop_trusted_negative() -> None:
    """Flat then down mirrors flat-then-up (consumers only act on r > 0)."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 51, 4, tzinfo=timezone.utc)
    tracker.update(base, 40.0)
    tracker.update(_ts(base, 30), 40.0)
    rate, trusted = tracker.update(_ts(base, 60), 38.0)
    assert trusted is True
    assert rate < 0


def test_reset_discards_history() -> None:
    """Explicit reset clears samples and quarter-hour identity."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc)
    tracker.update(base, 17.7)
    tracker.update(_ts(base, 30), 25.2)
    tracker.reset()
    rate, trusted = tracker.update(_ts(base, 60), 32.7)
    assert trusted is False
    assert rate == 0.0


# ── Quarter-hour identity ────────────────────────────────────────────


def test_qh_boundary_resets_history() -> None:
    """Crossing a real quarter-hour boundary discards prior history."""
    tracker = GapTrendTracker()
    tracker.update(datetime(2026, 9, 26, 23, 53, 4, tzinfo=timezone.utc), 17.7)
    tracker.update(datetime(2026, 9, 26, 23, 54, 4, tzinfo=timezone.utc), 25.2)
    rate, trusted = tracker.update(
        datetime(2026, 9, 27, 0, 0, 31, tzinfo=timezone.utc), 32.7
    )
    assert trusted is False
    assert rate == 0.0


def test_hour_boundary_resets_history() -> None:
    """The hour rollover (a new QH1) also resets — the production bug.

    bugs/2026-10-10-tesla-overshoot.log c587 slope-fitted a 20:45-21:00
    sample against 20:43/20:44 samples from the previous hour's QH because
    the reset keyed on ``qh_name``, which load_nbc.py hardcodes to "QH1".
    """
    tracker = GapTrendTracker()
    tracker.update(datetime(2026, 10, 1, 20, 43, 1, tzinfo=timezone.utc), 7.688)
    tracker.update(datetime(2026, 10, 1, 20, 44, 1, tzinfo=timezone.utc), -1.223)
    rate, trusted = tracker.update(
        datetime(2026, 10, 1, 20, 45, 31, tzinfo=timezone.utc), -553.538
    )
    assert trusted is False
    assert rate == 0.0


def test_production_overshoot_sequence_yields_no_trend() -> None:
    """The real c574→c582 gap sequence never commits a trend (it oscillates).

    Guards against a future filter change accidentally "confirming" the
    surplus/deficit boundary oscillation that caused the +2 Wh overshoot.
    """
    tracker = GapTrendTracker()
    seq = [
        ((20, 40, 31), 12.731666250000025),
        ((20, 41, 1), 28.281547499999995),
        ((20, 42, 1), 3.4183974999999975),
        ((20, 43, 1), 7.68822333333334),
        ((20, 44, 1), -1.223295833333328),
    ]
    trusted_any = False
    for (h, m, s), gap in seq:
        _, trusted = tracker.update(
            datetime(2026, 10, 1, h, m, s, tzinfo=timezone.utc), gap,
            noise_floor=3.0 / 60,
        )
        trusted_any = trusted_any or trusted
    assert trusted_any is False


# ── Inter-sample span guard ──────────────────────────────────────────


def test_production_cadence_gaps_still_accumulate() -> None:
    """30 s and 60 s data-point deltas keep building a trend.

    Half of the production cycles exit at pending_check with
    waiting_for_fresh_data, so real deltas are 30 s or 60 s — both must
    stay on-scale.
    """
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 51, 4, tzinfo=timezone.utc)
    tracker.update(base, 10.0)
    tracker.update(_ts(base, 30), 17.5)   # +0.25/s
    rate, trusted = tracker.update(_ts(base, 90), 32.5)  # +0.25/s
    assert trusted is True
    assert abs(rate - 0.25) < 1e-9


def test_oversized_span_resets_history() -> None:
    """A data-point gap beyond the decision horizon clears history.

    A slope averaged over more seconds than the stop decision ever looks
    ahead (``MAX_DEFER_SECS``) describes a regime the decision cannot act
    on, so it must not be trusted.
    """
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 51, 4, tzinfo=timezone.utc)
    tracker.update(base, 10.0)
    tracker.update(_ts(base, 30), 17.5)
    rate, trusted = tracker.update(
        _ts(base, 30 + GAP_TREND_MAX_SPAN_SECS + 1), 25.0
    )
    assert trusted is False
    assert rate == 0.0


def test_span_just_under_limit_accumulates() -> None:
    """The span guard is inclusive of GAP_TREND_MAX_SPAN_SECS itself."""
    tracker = GapTrendTracker()
    base = datetime(2026, 9, 26, 23, 51, 4, tzinfo=timezone.utc)
    tracker.update(base, 10.0)
    tracker.update(_ts(base, 30), 17.5)
    rate, trusted = tracker.update(_ts(base, 30 + GAP_TREND_MAX_SPAN_SECS), 25.0)
    assert trusted is True
    assert rate > 0
