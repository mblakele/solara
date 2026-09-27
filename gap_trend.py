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
    GAP_TREND_MIN_SLOPES,
    GAP_TREND_WINDOW,
)


class GapTrendTracker:
    """EWMA slope of the adjusted gap over recent cycles.

    Samples are keyed on ``data_point_at`` (not wall clock) so stale or
    repeated fetches never fabricate a slope.  History resets on
    quarter-hour rollover so one QH's ramp cannot leak into the next.
    """

    def __init__(
        self,
        window: int = GAP_TREND_WINDOW,
        alpha: float = GAP_TREND_EWMA_ALPHA,
    ) -> None:
        """Initialize an empty tracker.

        Args:
            window: Recent samples retained (need window-1 slopes).
            alpha: Weight of the newest slope in the EWMA.
        """
        self._window = window
        self._alpha = alpha
        self._samples: deque[tuple[datetime, float]] = deque(maxlen=window)
        self._qh_name: str | None = None

    def reset(self) -> None:
        """Discard all history (e.g. on QH rollover detected by caller)."""
        self._samples.clear()
        self._qh_name = None

    def update(
        self,
        data_point_at: datetime,
        gap_wh: float,
        qh_name: str | None = None,
        noise_floor: float = 0.0,
    ) -> tuple[float, bool]:
        """Record one cycle's adjusted gap and return the trend.

        Args:
            data_point_at: Timestamp of the NBC data point this cycle.
            gap_wh: Adjusted gap (target − adjusted prediction), always
                computed from pending-effect-corrected predictions so the
                trend never chases our own actions.
            qh_name: Current quarter-hour id.  A change discards history.
            noise_floor: Minimum |slope| (Wh/s) to trust.  Callers pass
                ``hysteresis / seconds_remaining``.

        Returns:
            Tuple of (ewma_slope_wh_per_s, trusted).  Rate is 0.0 whenever
            untrusted.  Trusted requires GAP_TREND_MIN_SLOPES slopes with no
            strictly opposing signs and |ewma| above the noise floor; flat
            (zero) slopes are neutral and neither confirm nor break a ramp.
        """
        if (
            qh_name is not None
            and self._qh_name is not None
            and qh_name != self._qh_name
        ):
            self._samples.clear()
        if qh_name is not None:
            self._qh_name = qh_name

        if self._samples and data_point_at <= self._samples[-1][0]:
            return 0.0, False
        self._samples.append((data_point_at, gap_wh))

        if len(self._samples) < GAP_TREND_MIN_SLOPES + 1:
            return 0.0, False

        # Slopes over the last GAP_TREND_MIN_SLOPES intervals.
        slopes: list[float] = []
        recent = list(self._samples)[-(GAP_TREND_MIN_SLOPES + 1):]
        for (prev_ts, prev_gap), (cur_ts, cur_gap) in zip(recent, recent[1:]):
            dt = (cur_ts - prev_ts).total_seconds()
            if dt <= 0:
                return 0.0, False
            slopes.append((cur_gap - prev_gap) / dt)

        if len(slopes) < GAP_TREND_MIN_SLOPES:
            return 0.0, False
        # Zero slopes are neutral (repeated fetch of the same data carries
        # no new information); only strictly opposing nonzero slopes break
        # confirmation. This tolerates meter/NBC plateaus inside a ramp
        # (bugs/2026-09-26-tesla-stop-charging.log: 38.3 → 38.3 → 45.1).
        if any(s > 0 for s in slopes) and any(s < 0 for s in slopes):
            return 0.0, False

        ewma = slopes[0]
        for slope in slopes[1:]:
            ewma = self._alpha * slope + (1.0 - self._alpha) * ewma
        if abs(ewma) <= noise_floor:
            return 0.0, False
        return ewma, True
