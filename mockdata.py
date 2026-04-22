"""
Mock metrics data for testing.

Provides MetricsMock and _generate_hour_seconds to generate deterministic
test data that mirrors the structure of real Emporia VUE API responses,
including NBC quarter-hour computation and TOU bucket reporting.
"""

from datetime import datetime, timedelta, timezone
import random
from typing import Any, Dict, List

from util import TIMEZONE


def _generate_hour_seconds(
    device_seed: int, minute_of_hour: int, sign: float = -1.0
) -> List[float]:
    """Generate deterministic kWh/second values for up to an hour (0-3600 values).

    Returns a variable-length list of exactly `minute_of_hour * 60` floats (capped at 3600).

    Args:
        device_seed: Seed for reproducibility.
        minute_of_hour: Current minute (0-59), determines list length.
        sign: +1.0 for positive consumption, -1.0 for negative (solar export).

    Returns:
        List of `minute_of_hour * 60` floats (kWh/second).
    """
    rng = random.Random(device_seed)
    num_seconds = min(minute_of_hour * 60, 3600)
    if sign < 0:
        return [rng.uniform(-0.001, -0.0004) for _ in range(num_seconds)]
    else:
        return [rng.uniform(0.0002, 0.0008) for _ in range(num_seconds)]


class MetricsMock:
    """
    Mock metrics data for testing. Supports NBC quarter-hour computation,
    TOU bucket reporting, and all existing hourly prediction features.

    The mock is parameterized by instant_minute so it always represents a
    current timestamp. Tests should verify invariant properties rather than
    absolute timestamps.

    NOTE: Negative 'usage' values represent solar generation exceeding consumption
    (power exported to the grid). This is physically normal during sunny hours when
    PV panels produce more than the home uses. The downstream TOU aggregator handles
    these correctly by reducing the bucket total.
    """

    metrics: Dict[str, Any]
    tou_result: Dict[str, float]

    def __init__(self, instant_minute: int = 42) -> None:
        now = datetime.now(timezone.utc)

        is_full_hour = instant_minute >= 60
        minute_of_hour = min(instant_minute, 59)

        self.instant = now.replace(
            minute=minute_of_hour, second=0, microsecond=0
        )

        _per_second_data_a = _generate_hour_seconds(12345, minute_of_hour, sign=-1.0)
        _per_second_data_b = _generate_hour_seconds(67890, minute_of_hour, sign=1.0)

        if is_full_hour:
            _per_second_data_a.extend([_per_second_data_a[-1]] * (3600 - len(_per_second_data_a)))
            _per_second_data_b.extend([_per_second_data_b[-1]] * (3600 - len(_per_second_data_b)))

        self.metrics: Dict[str, object] = {
            "api_response": {
                "get_chart_usage/1,2,3": timedelta(microseconds=750072),
                "total": timedelta(microseconds=750072),
            },
            "debug": True,
            "devices": [
                self._build_device(
                    _per_second_data_a, minute_of_hour, device_name="MOCK", sign=-1.0
                ),
                self._build_device(
                    _per_second_data_b,
                    minute_of_hour,
                    device_name="SOLAR+LOAD",
                    timezone_str=TIMEZONE,
                    sign=1.0,
                ),
            ],
            "instant": datetime(
                2026, 2, 27, 18, 42, 34, 170162, tzinfo=timezone.utc
            ),
        }

        self.tou_result: Dict[str, float] = {
            "total": 12847.3,
            "peak": 3200.5,
            "part_peak": 4100.2,
            "off_peak": 5546.6,
        }

    def _build_device(
        self,
        per_second_data: List[float],
        minute_of_hour: int,
        device_name: str = "MOCK",
        timezone_str: str = TIMEZONE,
        sign: float = -1.0,
    ) -> Dict[str, Any]:
        """Build a single device dict with all fields for backward-compatible testing.

        Produces dynamic scale data derived from the actual per-second values.
        The scales' data arrays remain truncated (3 sample floats each); full
        per-second data is stored separately in the 'per_second_data' field for NBC use.

        Args:
            per_second_data: Full array of kWh/second values observed so far.
            minute_of_hour: Current minute (0-59) within the hour.
            device_name: Display name for this device.
            timezone_str: IANA timezone string for the device.
            sign: +1.0 for positive consumption, -1.0 for negative (solar export).

        Returns:
            Device dictionary with all fields matching the production structure.
        """
        n = len(per_second_data)
        hour_usage = 1000.0 * sum(per_second_data)

        scales: Dict[str, Any] = {
            "1H": self._make_scale_entry(
                per_second_data,
                self.instant.replace(second=0, microsecond=0),
                n,
                hour_usage,
            )
        }

        smoothing: Dict[str, float] = {}
        for usm in range(1, 11):
            uss = 60 * usm
            if uss > n:
                continue
            offset_data = per_second_data[-uss:]
            offset_start = self.instant.replace(second=0, microsecond=0) + timedelta(
                minutes=minute_of_hour - usm
            )
            scale_usage = 1000.0 * sum(offset_data) * 60.0 / len(offset_data)
            scales[str(usm) + "MIN"] = self._make_scale_entry(
                offset_data, offset_start, len(offset_data), scale_usage
            )
            smoothing[str(usm) + "MIN"] = hour_usage + (
                (3600 - minute_of_hour * 60) * scale_usage / 60.0
            )

        one_min_usage = scales.get("1MIN", {}).get("usage", 0) if "1MIN" in scales else 0
        seconds_remaining = 3600 - minute_of_hour * 60
        minute_predicted = seconds_remaining * one_min_usage / 60.0
        prediction = hour_usage + minute_predicted

        return {
            "gid": hash(device_name) % (10**8),
            "lag": timedelta(seconds=2, microseconds=(hash(device_name) % 999999)),
            "name": device_name,
            "minute_predicted": round(minute_predicted, 14),
            "minutes_remaining": round(seconds_remaining / 60.0, 14),
            "per_second_data": per_second_data,
            "prediction": round(prediction, 14),
            "prediction_min": round(prediction, 14),
            "prediction_max": round(prediction * 0.9 if sign < 0 else prediction * 1.1, 14),
            "scales": scales,
            "smoothing": {k: round(v, 14) for k, v in smoothing.items()},
            "timezone": timezone_str,
            "nbc": self._compute_nbc(
                per_second_data,
                start=self.instant - timedelta(seconds=n),
            ),
        }

    @staticmethod
    def _make_scale_entry(
        data: List[float], data_start: datetime, data_len: int, usage: float
    ) -> Dict[str, Any]:
        """Create a scale entry dict from raw per-second slice data.

        Args:
            data: kWh/second values for this scale window.
            data_start: Start time of the first data point.
            data_len: Number of seconds in this scale window.
            usage: Pre-computed Wh usage value for display.

        Returns:
            Scale entry dict with data (truncated to 3 samples), metadata, and usage.
        """
        return {
            "data": data[:3] if len(data) >= 3 else list(data),
            "data_len": data_len,
            "data_start": data_start,
            "instant": data_start + timedelta(seconds=data_len),
            "seconds": data_len,
            "usage": usage,
        }

    def _compute_nbc(
        self, per_second_data: List[float], start: datetime | None = None
    ) -> Dict[str, Any]:
        """Compute NBC values for each quarter hour.

        Args:
            per_second_data: Variable-length list of observed kWh/second values.
            start: When data collection started (defaults to instant - 1 hour).

        Returns:
            Dict with keys QH1-QH4, each containing NBC metrics or None.
        """
        if start is not None:
            elapsed = self.instant - start
            n = max(0, int(elapsed.total_seconds()))
        else:
            n = len(per_second_data)  # fallback for backward compat

        quarters = [
            ("QH1", 0, 899),       # minutes 0-14 (first 15 min)
            ("QH2", 900, 1799),    # minutes 15-29
            ("QH3", 1800, 2699),   # minutes 30-44
            ("QH4", 2700, 3599),   # minutes 45-59
        ]

        result: Dict[str, Any] = {}
        for qh_name, start_idx, end_idx in quarters:
            # Not started: no seconds observed for this quarter yet
            if n <= start_idx:
                result[qh_name] = None
                continue

            # Determine which indices have data in this quarter
            obs_start = max(start_idx, 0)
            obs_end = min(n, end_idx + 1)  # exclusive upper bound

            if n > end_idx:
                # Complete: all seconds in this quarter have been observed
                values = per_second_data[start_idx:end_idx + 1]
                raw_wh = sum(values) * 1000
                result[qh_name] = {
                    "wh": max(0, raw_wh),
                    "complete": True,
                    "raw_wh": raw_wh,
                }
            else:
                # Incomplete: look back up to 60 seconds from current position
                # (absolute window - may include data from previous quarter)
                lookback_start = max(n - 60, start_idx)
                values = per_second_data[lookback_start:n]
                rate = sum(values) / len(values) if values else 0.0

                # raw_wh = actual observed data in this quarter only (not lookback)
                raw_values = per_second_data[obs_start:obs_end]
                raw_wh = sum(raw_values) * 1000

                # predicted_wh = observed data + extrapolation for remaining seconds
                # not clamped to zero by design
                remaining_seconds = end_idx + 1 - n
                predicted_wh = raw_wh + rate * remaining_seconds * 1000

                result[qh_name] = {
                    "wh": max(0, predicted_wh),
                    "complete": False,
                    "raw_wh": raw_wh,
                    "predicted_wh": predicted_wh,
                    "samples_used": len(values),
                }

        return result
