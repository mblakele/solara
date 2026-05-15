"""
Mock metrics data for testing.

Provides MetricsMock and _generate_hour_seconds to generate deterministic
test data that mirrors the structure of real Emporia VUE API responses,
including NBC quarter-hour computation and TOU bucket reporting.
"""

from datetime import datetime, timedelta, timezone
import random
from typing import Any, Dict, List

from config import get_timezone

from util import compute_nbc_quarters


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
    nbc_result: float

    def __init__(self, instant_minute: int = 42) -> None:
        # Watch out for datetime-related problems from this!        
        now = datetime.now(timezone.utc) # TODO consider fixed_now pattern in all tests.

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
                    timezone_str=get_timezone(),
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

        self.nbc_result: float = -2756.5

    def _build_device(
        self,
        per_second_data: List[float],
        minute_of_hour: int,
        device_name: str = "MOCK",
        timezone_str: str | None = None,
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
        if timezone_str is None:
            timezone_str = get_timezone()
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
            "lag": timedelta(seconds=2, microseconds=hash(device_name) % 999999),
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

        Delegates to ``compute_nbc_quarters`` in util.

        Args:
            per_second_data: Variable-length list of observed kWh/second values.
            start: When data collection started. When None, all data is assumed
                observed (n = len(per_second_data)).

        Returns:
            Dict with keys QH1-QH4, each containing NBC metrics or None.
        """
        if start is not None:
            elapsed = self.instant - start
            n = max(0, int(elapsed.total_seconds()))
        else:
            n = len(per_second_data)
        n = min(n, len(per_second_data))
        return compute_nbc_quarters(per_second_data, n)
