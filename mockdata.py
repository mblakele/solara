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

from energy_aggregator import TOUBuckets
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
    tou_result: TOUBuckets
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

        self.tou_result: TOUBuckets = TOUBuckets(
            total=12847.3,
            peak=3200.5,
            part_peak=4100.2,
            off_peak=5546.6,
        )

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

        seconds_remaining_hour = 3600 - minute_of_hour * 60

        one_min_scale_usage = 0.0
        if n >= 60:
            tail = per_second_data[-60:]
            one_min_scale_usage = 1000.0 * sum(tail) * 60.0 / len(tail)
        minute_predicted = seconds_remaining_hour * one_min_scale_usage / 60.0
        prediction = hour_usage + minute_predicted

        return {
            "gid": hash(device_name) % (10**8),
            "lag": timedelta(seconds=2, microseconds=hash(device_name) % 999999),
            "name": device_name,
            "minute_predicted": round(minute_predicted, 14),
            "minutes_remaining": round(seconds_remaining_hour / 60.0, 14),
            "per_second_data": per_second_data,
            "prediction": round(prediction, 14),
            "prediction_min": round(prediction, 14),
            "prediction_max": round(prediction * 0.9 if sign < 0 else prediction * 1.1, 14),
            "timezone": timezone_str,
            "nbc": compute_nbc_quarters(per_second_data).to_dict(),
        }
