"""
Unit tests to expose the day boundary handling bug in energy_aggregator.py.

The aggregator assumes all data falls on a single day. If data crosses midnight,
the hours after midnight (00:xx) get classified incorrectly because the code
doesn't reset the day boundary when calculating timestamps.
"""

import datetime
from energy_aggregator import EnergyDataAggregator


def test_48_hour_aggregation():
    """Test aggregation of exactly 48 non-overlapping hours."""
    from util import get_timezone
    import pytz

    local_tz = pytz.timezone(get_timezone())
    start_time = local_tz.localize(datetime.datetime(2024, 1, 1, 0, 0, 0))

    # One kWh per hour for 48 hours (values are kWh; _aggregate converts to Wh)
    data = [1.0] * 48

    result = EnergyDataAggregator._aggregate(
        start_time, data, datetime.timedelta(hours=1)
    )

    # Expected values (1 kWh = 1000 Wh per hour)
    # 48 hours = 2 days worth of data
    # off_peak: hours 0-14 = 15 hours per day -> 30 total
    # part_peak: hour 15 + hours 21-23 = 4 hours per day -> 8 total
    # peak: hours 16-20 = 5 hours per day -> 10 total
    expected = {
        "total": 48000.0,
        "peak": 10000.0,  # 5 hours * 1000 * 2 days
        "part_peak": 8000.0,  # 4 hours * 1000 * 2 days
        "off_peak": 30000.0,  # 15 hours * 1000 * 2 days
    }

    print(f"Result: {result}")
    print(f"Expected: {expected}")
    print(f"Match: {result.to_dict() == expected}")

    assert result.to_dict() == expected, f"Got {result}, expected {expected}"


if __name__ == "__main__":
    test_48_hour_aggregation()
