"""Prediction models and the quantization prediction-window helper.

Split out of ``metrics.py`` (architecture-v2 Phase E.1). ``metrics.py``
re-exports these names so existing imports keep working.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from energy_cache import EnergyCache
from quantization import usable_window
from util import (
    NBCQuarterSet,
    compute_nbc_quarters,
    inject_completed_qh,
    qh_seconds_remaining,
)


def prediction_window_from_cache(energy_cache: Optional["EnergyCache"]) -> int | None:
    """Return the usable quantization window from *energy_cache*, or None.

    Applies the shared ``quantization.usable_window`` guard so the
    flat-data N=2 artifact is rejected here too (plan 3.2).

    Args:
        energy_cache: The shared EnergyCache (or None).

    Returns:
        Accepted period in seconds, or None when unavailable/rejected.
    """
    if energy_cache is None:
        return None
    data = energy_cache.data
    if data is None:
        return None
    return usable_window(
        getattr(data, "quantization_seconds", None),
        getattr(data, "quantization_confidence", None),
    )


@dataclass
class _PopulationResult:
    """Intermediate results from populating one device — no mutation of API objects."""

    per_second_data: list[float]
    chart_data: list[float]
    nbc_seconds: list[float]
    nbc_data_start: datetime
    nbc_sample_count: int = 0


@dataclass(frozen=True)
class _PredictionData:
    """Aggregated prediction values for a device."""

    value: float
    min_value: float
    max_value: float


@dataclass(frozen=True)
class _MinuteData:
    """Minute-scale prediction data (internal, not serialized directly)."""
    predicted: float
    minutes_remaining: float


@dataclass(frozen=True)
class DevicePrediction:
    """Prediction result for one device.

    Replaces the dict return from _predict_device() with a typed dataclass.
    """

    lag: timedelta
    minute_predicted: float
    prediction: float
    prediction_min: float
    prediction_max: float
    seconds_remaining: float

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for backward compat with intermediate callers."""
        return {
            "lag": self.lag,
            "minute_predicted": self.minute_predicted,
            "prediction": self.prediction,
            "prediction_min": self.prediction_min,
            "prediction_max": self.prediction_max,
            "seconds_remaining": self.seconds_remaining,
        }


@dataclass
class DeviceMetrics:
    """Computed metrics for one device, separate from raw pyemvue response."""

    gid: int = 0
    name: str = ""
    lag: timedelta = dataclasses.field(  # type: ignore[assignment]
        default_factory=timedelta, repr=False
    )
    per_second_data: list[float] = dataclasses.field(  # type: ignore[assignment]
        default_factory=list, repr=False
    )
    prediction: _PredictionData = dataclasses.field(  # type: ignore[assignment]
        default_factory=lambda: _PredictionData(value=0.0, min_value=0.0, max_value=0.0),
        repr=False,
    )
    minute_data: _MinuteData = dataclasses.field(  # type: ignore[assignment]
        default_factory=lambda: _MinuteData(predicted=0.0, minutes_remaining=0.0),
        repr=False,
    )
    nbc: NBCQuarterSet = dataclasses.field(  # type: ignore[assignment]
        default_factory=lambda: NBCQuarterSet(
            qh1=None, qh2=None, qh3=None, qh4=None
        ),
        repr=False,
    )
    timezone: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON/template consumption.

        per_second_data is truncated to the latest 300 samples for compact output.
        """
        return {
            "gid": self.gid,
            "lag": self.lag,
            "name": self.name,
            "prediction": round(self.prediction.value, 14),
            "prediction_min": round(self.prediction.min_value, 14),
            "prediction_max": round(self.prediction.max_value, 14),
            "minute_predicted": round(self.minute_data.predicted, 14),
            "minutes_remaining": round(self.minute_data.minutes_remaining, 14),
            "timezone": self.timezone,
            "nbc": self.nbc.to_dict(),
            "per_second_data": self.per_second_data,
        }


class PredictionMixin:
    """Prediction and NBC computation mixed into ``HourlyProjection``.

    Split out of ``hourly_fetch.py`` (architecture-v2 E.1) to keep that
    module under 500 lines; the methods rely on ``self.instant``,
    ``self.energy_cache`` and ``self.logger`` from the concrete class.
    """

    instant: datetime
    energy_cache: Optional[EnergyCache]
    logger: logging.Logger

    def predict(
        self, population: dict[int, _PopulationResult]
    ) -> dict[int, DevicePrediction]:
        """Predict consumption or surplus at end of current hour.

        Uses the minute-scale usage rate to extrapolate remaining
        consumption for the current hour, then computes min/max bounds
        across all available minute scales (1MIN–10MIN).

        Args:
            population: Results from populate(), mapping gid -> PopulationResult.

        Returns:
            Dict of gid -> DevicePrediction for each device.
        """
        predictions: dict[int, DevicePrediction] = {}
        for gid, pop_result in population.items():
            pred_result = self._predict_device(
                pop_result.per_second_data, pop_result.nbc_data_start
            )
            predictions[gid] = pred_result
        return predictions

    def _compute_nbc(
        self,
        usage_data_local: list[float],
        prediction_window_seconds: int | None = None,
        now: datetime | None = None,
    ) -> NBCQuarterSet:
        """Compute NBC values for each quarter hour in the current hour.

        Delegates to ``compute_nbc_quarters`` in util. Quarter boundaries are
        determined by the number of observed data points (``n``), not by
        wall-clock time, so API lag cannot cause two quarters to appear
        incomplete simultaneously.

        One time base (architecture-v2 C.2): the incomplete quarter is
        extrapolated with the wall-clock remainder from
        ``util.qh_seconds_remaining``, exactly as
        ``EnergyCache.get_current_qh`` does. The display path, the cache path,
        and ``NBCReader._parse_metrics`` therefore report the same
        ``predicted_wh`` for the same samples and clock.

        Args:
            usage_data_local: Per-second kWh data for the current hour.
            prediction_window_seconds: Number of trailing seconds to use for
                rate extrapolation of the incomplete quarter. Passed through
                to ``compute_nbc_quarters``. Defaults to 60 when ``None``.
            now: Wall-clock time to extrapolate from. Defaults to
                ``self.instant``.

        Returns:
            An ``NBCQuarterSet`` with QH1-QH4 containing NBC metrics or
            ``None`` if the quarter has not yet started.
        """
        return compute_nbc_quarters(
            usage_data_local,
            prediction_window_seconds=prediction_window_seconds,
            seconds_remaining_override=qh_seconds_remaining(now or self.instant),
        )

    def _predict_device(
        self, per_second_data: list[float], data_start: datetime
    ) -> DevicePrediction:
        """Compute prediction for one device from raw per-second data.

        Args:
            per_second_data: Per-second kWh values for the current hour.
            data_start: Start time of the per-second data.

        Returns:
            DevicePrediction with prediction, min/max bounds, lag, etc.
        """
        hour_next = (
            self.instant
            + timedelta(hours=1)
            - timedelta(
                minutes=self.instant.minute,
                seconds=self.instant.second,
                microseconds=self.instant.microsecond,
            )
        )
        hour_instant = data_start + timedelta(seconds=len(per_second_data))
        hour_usage = 1000.0 * sum(per_second_data)
        seconds_remaining_hour = (hour_next - hour_instant).total_seconds()

        def _minute_usage(data: list[float]) -> float:
            """Compute Wh/minute usage rate from last 60 seconds of data."""
            tail = data[-60:]
            total_wh = 1000.0 * sum(tail)
            return total_wh * 60.0 / len(tail) if len(tail) != 0 else 0.0

        minute_predicted = seconds_remaining_hour * _minute_usage(per_second_data) / 60.0
        prediction = hour_usage + minute_predicted

        lag = (
            self.instant - hour_instant
            if hour_instant < self.instant
            else timedelta(0)
        )

        return DevicePrediction(
            lag=lag,
            minute_predicted=minute_predicted,
            prediction=prediction,
            prediction_min=prediction,
            prediction_max=prediction,
            seconds_remaining=seconds_remaining_hour,
        )

    def _compute_device_metrics(
        self,
        vdi: Any,
        pop_result: _PopulationResult,
        pred_result: DevicePrediction,
    ) -> DeviceMetrics:
        """Build a DeviceMetrics from population and prediction results.

        This is a pure constructor that takes raw inputs and returns computed
        output without mutating any input data.

        Args:
            vdi: The VDeviceUsageInfo object from pyemvue (read-only metadata).
            pop_result: Intermediate data from _populate_device.
            pred_result: Computed predictions from _predict_device.

        Returns:
            DeviceMetrics instance with all derived fields.
        """
        energy_cache = self.energy_cache

        nbc_seconds = list(pop_result.nbc_seconds) if pop_result.nbc_seconds is not None else []
        per_second_data = list(pop_result.per_second_data) if pop_result.per_second_data is not None else []

        # Determine the prediction window from quantization data via the
        # shared guard (plan 3.2) — rejects the flat-data N=2 artifact.
        prediction_window_seconds = prediction_window_from_cache(energy_cache)

        nbc_result = self._compute_nbc(
            nbc_seconds, prediction_window_seconds, now=self.instant
        )

        # If cache has completed periods, inject QH2-QH4 from them.
        completed_periods = (
            energy_cache.completed_periods if energy_cache is not None else None
        )
        if completed_periods:
            nbc_result = inject_completed_qh(nbc_result, completed_periods)

        # Log the final set *after* injection: QH2-QH4 are reconstructed from
        # CompletedNBCPeriod records at this point, so this is the truth that
        # reaches DeviceMetrics/HTTP. Logging inside _compute_nbc (the old
        # behavior) dumped the pre-injection set and made healthy responses
        # look like missing data (qh2=qh3=qh4=None on every cycle).
        self.logger.debug("nbc_set len %d (%s)", len(nbc_seconds), nbc_result)

        return DeviceMetrics(
            gid=vdi.device_gid,
            name=vdi.device_name,
            lag=pred_result.lag,
            per_second_data=per_second_data,
            prediction=_PredictionData(
                value=pred_result.prediction,
                min_value=pred_result.prediction_min,
                max_value=pred_result.prediction_max,
            ),
            minute_data=_MinuteData(
                predicted=pred_result.minute_predicted,
                minutes_remaining=pred_result.seconds_remaining / 60.0,
            ),
            nbc=nbc_result,
            timezone=getattr(vdi, "time_zone", None) or "",
        )
