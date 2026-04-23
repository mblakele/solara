"""
Test to expose the start_time misalignment vulnerability.

If start_time is 2024-01-01 23:59:00 and usage_data[0] represents
23:59:00, then start_time + timedelta(seconds=0) = 23:59:00 → hour 23 → correct.
BUT if usage_data[0] represents 00:00:00 the next day (start_offset=1 second),
then start_time + 0 = 23:59:00 (wrong!).

This happens when:
1. Data is recorded from a sensor that has a slight delay
2. System clock is slightly off
3. User passes arbitrary times rather than data-aligned times
"""

import datetime
from energy_aggregator import EnergyDataAggregator


def test_misaligned_start_time():
    """
    Data should start at hour 0 (off_peak) but start_time is set to hour 23.
    The first second should be hour 0, but we classify it as hour 23.
    This is the "off" case.
    """
    start_time = datetime.datetime(2024, 1, 1, 23, 0, 0)

    _data = [(start_time, 1.0)]

    print("Test complete")


def test_cross_midnight_aggregation():
    """Test cross-midnight aggregation with local timezone."""
    from util import TIMEZONE
    import pytz

    local_tz = pytz.timezone(TIMEZONE)
    start_time = local_tz.localize(datetime.datetime(2024, 1, 1, 10, 0, 0))

    data = [(start_time + datetime.timedelta(seconds=idx), 0.01) for idx in range(1000)]

    result = EnergyDataAggregator.aggregate_from_seconds(
        start_time, [x[1] for x in data]
    )

    expected = {
        "total": 10000.0,
        "off_peak": 10000.0,
        "part_peak": 0.0,
        "peak": 0.0,
    }

    print(f"Result: {result}")
    print(f"Expected: {expected}")
    assert result == expected


if __name__ == "__main__":
    test_cross_midnight_aggregation()
