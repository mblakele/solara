"""Gap-trend tracker for ramp-aware load decisions.

Estimates the slope of the *adjusted* gap (target − prediction after
pending-effect correction) across load-management cycles.  The slope is
cause-agnostic: sunset, clouds, shade, and unknown manual loads all move
the gap identically — only persistence matters, so a trend commits only
after consecutive same-sign slopes.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

from constants import (
    GAP_TREND_EWMA_ALPHA,
    GAP_TREND_MAX_SPAN_SECS,
    GAP_TREND_MIN_SLOPES,
    GAP_TREND_WINDOW,
)
from util import floor_to_qh


class GapTrendTracker:
    """EWMA slope of the adjusted gap over recent cycles.

    Samples are keyed on ``data_point_at`` (not wall clock) so stale or
    repeated fetches never fabricate a slope.  History resets on
    quarter-hour rollover so one QH's ramp cannot leak into the next.

    The quarter-hour identity is derived from ``data_point_at`` itself
    rather than taken from the caller.  ``ParsedMetricsQH.qh_name`` is the
    hardcoded literal ``"QH1"`` for every incomplete quarter, so keying on
    it can never detect a rollover — which let a 20:45 sample be
    slope-fitted against 20:43/20:44 samples from the previous hour
    (bugs/2026-10-01-tesla-overshoot.log, c587).
    """

    def __init__(
        self,
        window: int = GAP_TREND_WINDOW,
        alpha: float = GAP_TREND_EWMA_ALPHA,
        max_span_secs: int = GAP_TREND_MAX_SPAN_SECS,
    ) -> None:
        """Initialize an empty tracker.

        Args:
            window: Recent samples retained (need window-1 slopes).
            alpha: Weight of the newest slope (and the newest churn sample)
                in their EWMA.
            max_span_secs: Largest ``data_point_at`` delta to slope across;
                a larger gap clears history.
        """
        self._window = window
        self._alpha = alpha
        self._max_span_secs = max_span_secs
        self._samples: deque[tuple[datetime, float]] = deque(maxlen=window)
        self._churn_samples: deque[tuple[datetime, float]] = deque(maxlen=window)
        self._qh_start: datetime | None = None
        self._churn: float | None = None

    @property
    def churn_wh_per_s(self) -> float:
        """Cycle-to-cycle swing of the slope estimate (Wh/s).

        EWMA of ``|slope_n - slope_(n-1)|`` over successive updates: how
        far the end-of-quarter gap projection moves between cycles. This
        is the jitter/uncertainty signal for turn-on decisions — reported
        even while the trend itself is untrusted, because the oscillating
        incident series (bugs/2026-10-01-tesla-overshoot.log) is exactly
        the case where ``update()`` confirms no trend but the swing is
        large.

        Accumulated over the churn-ready window only (see
        ``update(churn_ready=...)``), so a slope pair straddling a
        quarter's extrapolation-dominated opening cannot seed it.

        Returns:
            0.0 until two slopes exist among churn-ready samples (third
            ready sample onward). Resets with the sample history
            (explicit reset, QH rollover, span guard).
        """
        return 0.0 if self._churn is None else self._churn

    def reset(self) -> None:
        """Discard all history (samples, churn, and quarter-hour identity)."""
        self._clear_history()
        self._qh_start = None

    def _clear_history(self) -> None:
        """Drop both sample windows and the churn accumulator."""
        self._samples.clear()
        self._churn_samples.clear()
        self._churn = None

    @staticmethod
    def _slopes(samples: list[tuple[datetime, float]]) -> list[float]:
        """Slope (Wh/s) of each adjacent pair of a strictly rising series.

        Args:
            Samples ordered by ``data_point_at``. ``update()`` rejects any
            timestamp at or before the newest sample, so every adjacent
            pair here has ``dt > 0``.

        Returns:
            One slope per adjacent pair.
        """
        slopes: list[float] = []
        for (prev_ts, prev_gap), (cur_ts, cur_gap) in zip(samples, samples[1:]):
            dt = (cur_ts - prev_ts).total_seconds()
            slopes.append((cur_gap - prev_gap) / dt)
        return slopes

    def _fold_churn(self, samples: deque[tuple[datetime, float]]) -> None:
        """Fold the newest ``|Δslope|`` into the churn EWMA, if measurable.

        Jitter: the ``|delta|`` between the two newest slopes of
        ``samples``, EWMA-smoothed so one calm fetch cannot mask an
        oscillating quarter. Folded before the trust checks in
        ``update()``: churn must be reported while the trend stays
        untrusted — that dark zone is where consumers need it.

        Args:
            samples: The churn-eligible sample window. Needs
                ``GAP_TREND_MIN_SLOPES + 1`` entries (two slopes);
                otherwise the accumulator is left untouched (it stays
                unmeasurable rather than dropping to zero).
        """
        if len(samples) < GAP_TREND_MIN_SLOPES + 1:
            return
        slopes = self._slopes(list(samples)[-(GAP_TREND_MIN_SLOPES + 1):])
        pair_churn = abs(slopes[-1] - slopes[-2])
        if self._churn is None:
            self._churn = pair_churn
        else:
            self._churn = self._alpha * pair_churn + (1.0 - self._alpha) * self._churn

    def update(
        self,
        data_point_at: datetime,
        gap_wh: float,
        noise_floor: float = 0.0,
        churn_ready: bool = True,
    ) -> tuple[float, bool]:
        """Record one cycle's adjusted gap and return the trend.

        History is cleared when the new sample falls in a different
        quarter-hour, or when it is more than ``max_span_secs`` past the
        previous sample.

        Args:
            data_point_at: Timestamp of the NBC data point this cycle.
            gap_wh: Adjusted gap (target − adjusted prediction), always
                computed from pending-effect-corrected predictions so the
                trend never chases our own actions.
            noise_floor: Minimum |slope| (Wh/s) to trust.  Callers pass
                ``hysteresis / seconds_remaining``.
            churn_ready: Whether this sample may take part in churn
                accumulation. Callers pass ``False`` while more than
                ``JITTER_MAX_REMAINING_SECS`` of the quarter remain, where
                the projection is dominated by ``prediction_w *
                seconds_remaining`` extrapolation: seeding churn there
                measured one quarter-opening sign flip as 1.10 Wh/s of
                oscillation (bugs/2026-10-02-sunrise-marine-layer-jitter.log,
                cluster B). The sample is still recorded for the trend —
                slope, trust rule and ``churn_wh_per_s``'s resets are
                unaffected; only the churn window skips it. When every
                sample is ready the two windows are identical.

        Returns:
            Tuple of (ewma_slope_wh_per_s, trusted).  Rate is 0.0 whenever
            untrusted.  Trusted requires GAP_TREND_MIN_SLOPES slopes with no
            opposing signs above the noise floor and |ewma| above the noise
            floor; flat (zero) slopes — and slopes at or below
            ``noise_floor``, which are indistinguishable from meter noise —
            are neutral and neither confirm nor break a ramp.
        """
        qh_start = floor_to_qh(data_point_at)
        if self._qh_start is not None and qh_start != self._qh_start:
            self._clear_history()
        self._qh_start = qh_start

        if self._samples:
            prev_ts = self._samples[-1][0]
            if data_point_at <= prev_ts:
                return 0.0, False
            span = (data_point_at - prev_ts).total_seconds()
            if span > self._max_span_secs:
                # Too far from the last sample to describe the current
                # regime; keep the new sample as a fresh seed only.
                self._clear_history()
        self._samples.append((data_point_at, gap_wh))
        if churn_ready:
            self._churn_samples.append((data_point_at, gap_wh))

        if len(self._samples) < GAP_TREND_MIN_SLOPES + 1:
            return 0.0, False

        # Slopes over the last GAP_TREND_MIN_SLOPES intervals.
        slopes = self._slopes(list(self._samples)[-(GAP_TREND_MIN_SLOPES + 1):])

        # Jitter (folded before the trust checks below).
        self._fold_churn(self._churn_samples)

        # Zero slopes are neutral (repeated fetch of the same data carries
        # no new information), and so are slopes at or below the noise
        # floor (indistinguishable from meter noise): only opposing
        # slopes *above* the floor break confirmation. This tolerates
        # meter/NBC plateaus inside a ramp
        # (bugs/2026-09-26-tesla-stop-charging.log: 38.3 → 38.3 → 45.1).
        meaningful = [s for s in slopes if abs(s) > noise_floor]
        if any(s > 0 for s in meaningful) and any(s < 0 for s in meaningful):
            return 0.0, False

        ewma = slopes[0]
        for slope in slopes[1:]:
            ewma = self._alpha * slope + (1.0 - self._alpha) * ewma
        if abs(ewma) <= noise_floor:
            return 0.0, False
        return ewma, True
