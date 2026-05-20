"""
Pytest tests for EnergyDataAggregator consolidation (Subtask D).

Covers all four public aggregate methods and their shared _aggregate
implementation.  Each test uses assert-based assertions (pytest style)
and exercises distinct inputs to validate correctness before and after
the DRY refactoring.
"""

from datetime import datetime, timedelta

import pytz
import pytest

from energy_aggregator import EnergyDataAggregator


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def local_tz() -> pytz.tzinfo:
    """Return the configured local timezone (America/Los_Angeles by default)."""
    from config import get_timezone as _gt  # deferred, after clean_env
    return pytz.timezone(_gt())


# ---------------------------------------------------------------------------
# Tests for classify_hour  (unchanged behaviour)
# ---------------------------------------------------------------------------


class TestClassifyHour:
    def test_off_peak_hours(self):
        for hour in range(0, 15):
            assert EnergyDataAggregator.classify_hour(hour) == "off_peak"

    def test_part_peak_transition(self):
        assert EnergyDataAggregator.classify_hour(15) == "part_peak"

    def test_peak_hours(self):
        for hour in range(16, 21):
            assert EnergyDataAggregator.classify_hour(hour) == "peak"

    def test_part_peak_evening(self):
        for hour in range(21, 24):
            assert EnergyDataAggregator.classify_hour(hour) == "part_peak"


# ---------------------------------------------------------------------------
# Tests for _aggregate  (the new shared internal method)
# ---------------------------------------------------------------------------


class TestAggregate:
    """Tests for the shared _aggregate static method."""

    # -- step=1 second (seconds resolution) --

    def test_seconds_empty(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 10, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [], timedelta(seconds=1))
        assert result == {"total": 0.0, "peak": 0.0, "part_peak": 0.0, "off_peak": 0.0}

    def test_seconds_single(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 10, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [0.001], timedelta(seconds=1))
        assert result["off_peak"] == 1.0
        assert result["total"] == 1.0

    def test_seconds_negative(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 10, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [-0.001], timedelta(seconds=1))
        assert result["off_peak"] == -1.0
        assert result["total"] == -1.0

    def test_seconds_none_skipped(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 10, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [0.001, None, 0.001], timedelta(seconds=1))
        assert result["off_peak"] == 2.0
        assert result["total"] == 2.0

    def test_seconds_crosses_hour_boundary(self, local_tz):
        """Data spanning hour 15→16 classifies into part_peak then peak."""
        start = local_tz.localize(datetime(2024, 1, 1, 15, 59, 59))
        # idx 0 → 15:59:59 (part_peak), idx 1 → 16:00:00 (peak)
        result = EnergyDataAggregator._aggregate(start, [0.001, 0.002], timedelta(seconds=1))
        assert result["part_peak"] == 1.0
        assert result["peak"] == 2.0
        assert result["total"] == 3.0

    def test_seconds_full_hour(self, local_tz):
        """3600 one-second samples = 1 hour of 1 Wh each."""
        start = local_tz.localize(datetime(2024, 1, 1, 10, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [0.001] * 3600, timedelta(seconds=1))
        assert result["off_peak"] == 3600.0
        assert result["total"] == 3600.0

    # -- step=1 minute (minutes resolution) --

    def test_minutes_empty(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 10, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [], timedelta(minutes=1))
        assert result == {"total": 0.0, "peak": 0.0, "part_peak": 0.0, "off_peak": 0.0}

    def test_minutes_single(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 10, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [0.001], timedelta(minutes=1))
        assert result["off_peak"] == 1.0
        assert result["total"] == 1.0

    def test_minutes_crosses_boundary(self, local_tz):
        """Data spanning hour 15→16."""
        start = local_tz.localize(datetime(2024, 1, 1, 15, 59, 0))
        result = EnergyDataAggregator._aggregate(start, [0.001, 0.002], timedelta(minutes=1))
        assert result["part_peak"] == 1.0
        assert result["peak"] == 2.0
        assert result["total"] == 3.0

    # -- step=15 minutes (15-minute resolution) --

    def test_15min_empty(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 10, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [], timedelta(minutes=15))
        assert result == {"total": 0.0, "peak": 0.0, "part_peak": 0.0, "off_peak": 0.0}

    def test_15min_single_off_peak(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 8, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [0.001], timedelta(minutes=15))
        assert result["off_peak"] == 1.0
        assert result["total"] == 1.0

    def test_15min_single_peak(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 18, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [0.001], timedelta(minutes=15))
        assert result["peak"] == 1.0
        assert result["total"] == 1.0

    def test_15min_mixed(self, local_tz):
        """Sparse data at specific 15-min slots across off-peak, part-peak, peak."""
        start = local_tz.localize(datetime(2024, 1, 1, 8, 0, 0))
        # idx 0 → 8:00 off, idx 28 → 15:00 part, idx 29 → 16:00 peak, idx 37 → 21:00 part
        data = [0.0] * 40
        data[0] = 0.001
        data[28] = 0.001
        data[29] = 0.001
        data[37] = 0.001
        result = EnergyDataAggregator._aggregate(start, data, timedelta(minutes=15))
        assert result["off_peak"] == 1.0
        assert result["part_peak"] == 2.0
        assert result["peak"] == 1.0
        assert result["total"] == 4.0

    def test_15min_full_day(self, local_tz):
        """96 periods (one per 15 min for 24 h)."""
        start = local_tz.localize(datetime(2024, 1, 1, 0, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [0.001] * 96, timedelta(minutes=15))
        assert result["off_peak"] == 60.0   # hours 0-14: 15*4 = 60
        assert result["peak"] == 20.0        # hours 16-20: 5*4 = 20
        assert result["part_peak"] == 16.0   # hour 15 + 21-23: 4*4 = 16
        assert result["total"] == 96.0

    def test_15min_negative(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 8, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [-0.001], timedelta(minutes=15))
        assert result["off_peak"] == -1.0
        assert result["total"] == -1.0

    def test_15min_none_skipped(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 8, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [0.001, None, 0.001], timedelta(minutes=15))
        assert result["off_peak"] == 2.0
        assert result["total"] == 2.0

    def test_15min_crosses_midnight(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 23, 45, 0))
        result = EnergyDataAggregator._aggregate(start, [0.001, 0.001], timedelta(minutes=15))
        # idx 0 → 23:45 (part_peak), idx 1 → 00:00 (off_peak)
        assert result["part_peak"] == 1.0
        assert result["off_peak"] == 1.0
        assert result["total"] == 2.0

    # -- step=1 hour (hourly resolution) --

    def test_hourly_empty(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 10, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [], timedelta(hours=1))
        assert result == {"total": 0.0, "peak": 0.0, "part_peak": 0.0, "off_peak": 0.0}

    def test_hourly_single(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 10, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [1.0], timedelta(hours=1))
        assert result["off_peak"] == 1000.0
        assert result["total"] == 1000.0

    def test_hourly_negative(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 10, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [-0.5], timedelta(hours=1))
        assert result["off_peak"] == -500.0
        assert result["total"] == -500.0

    def test_hourly_none_skipped(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 10, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [1.0, None, 0.5], timedelta(hours=1))
        assert result["off_peak"] == 1500.0
        assert result["total"] == 1500.0

    def test_hourly_mixed(self, local_tz):
        start = local_tz.localize(datetime(2024, 1, 1, 0, 0, 0))
        result = EnergyDataAggregator._aggregate(start, [1.0] * 24, timedelta(hours=1))
        # Hours 0-14 → off_peak (15h × 1000), 16-20 → peak (5h × 1000)
        # Hour 15 + 21-23 → part_peak (4h × 1000)
        assert result["off_peak"] == 15000.0
        assert result["peak"] == 5000.0
        assert result["part_peak"] == 4000.0
        assert result["total"] == 24000.0


# ---------------------------------------------------------------------------
# Tests for aggregate_from_seconds  (public API)
# ---------------------------------------------------------------------------


class TestAggregateFromSeconds:
    def test_empty_data(self, local_tz):
        result = EnergyDataAggregator.aggregate_from_seconds(
            local_tz.localize(datetime(2024, 1, 1, 0, 0, 0)), []
        )
        assert result == {"total": 0.0, "peak": 0.0, "part_peak": 0.0, "off_peak": 0.0}

    def test_single_second_off_peak(self, local_tz):
        result = EnergyDataAggregator.aggregate_from_seconds(
            local_tz.localize(datetime(2024, 1, 1, 8, 0, 0)), [0.001]
        )
        assert result["off_peak"] == 1.0
        assert result["total"] == 1.0

    def test_single_second_peak(self, local_tz):
        result = EnergyDataAggregator.aggregate_from_seconds(
            local_tz.localize(datetime(2024, 1, 1, 18, 0, 0)), [0.001]
        )
        assert result["peak"] == 1.0
        assert result["total"] == 1.0

    def test_none_values_skipped(self, local_tz):
        result = EnergyDataAggregator.aggregate_from_seconds(
            local_tz.localize(datetime(2024, 1, 1, 8, 0, 0)), [0.001, None, 0.001]
        )
        assert result["off_peak"] == 2.0
        assert result["total"] == 2.0

    def test_negative_values(self, local_tz):
        result = EnergyDataAggregator.aggregate_from_seconds(
            local_tz.localize(datetime(2024, 1, 1, 8, 0, 0)), [-0.001]
        )
        assert result["off_peak"] == -1.0
        assert result["total"] == -1.0

    def test_bucket_boundaries(self, local_tz):
        usage_data = [0.001] * 3600
        for _ in range(1, 24):
            usage_data += [0.001] * 3600
        result = EnergyDataAggregator.aggregate_from_seconds(
            local_tz.localize(datetime(2024, 1, 1, 0, 0, 0)), usage_data
        )
        assert result["off_peak"] == 54000.0
        assert result["peak"] == 18000.0
        assert result["part_peak"] == 14400.0
        assert result["total"] == 86400.0


# ---------------------------------------------------------------------------
# Tests for aggregate_from_hourly  (public API)
# ---------------------------------------------------------------------------


class TestAggregateFromHourly:
    def test_empty_data(self):
        result = EnergyDataAggregator.aggregate_from_hourly([])
        assert result == {"total": 0.0, "peak": 0.0, "part_peak": 0.0, "off_peak": 0.0}

    def test_single_hour_off_peak(self, local_tz):
        hourly_data = [(local_tz.localize(datetime(2024, 1, 1, 8, 0, 0)), 1.0)]
        result = EnergyDataAggregator.aggregate_from_hourly(hourly_data)
        assert result["off_peak"] == 1000.0
        assert result["total"] == 1000.0

    def test_single_hour_peak(self, local_tz):
        hourly_data = [(local_tz.localize(datetime(2024, 1, 1, 18, 0, 0)), 1.0)]
        result = EnergyDataAggregator.aggregate_from_hourly(hourly_data)
        assert result["peak"] == 1000.0
        assert result["total"] == 1000.0

    def test_mixed_hours(self, local_tz):
        hourly_data = [
            (local_tz.localize(datetime(2024, 1, 1, 8, 0, 0)), 1.0),
            (local_tz.localize(datetime(2024, 1, 1, 17, 0, 0)), 2.0),
            (local_tz.localize(datetime(2024, 1, 1, 22, 0, 0)), 0.5),
        ]
        result = EnergyDataAggregator.aggregate_from_hourly(hourly_data)
        assert result["off_peak"] == 1000.0
        assert result["peak"] == 2000.0
        assert result["part_peak"] == 500.0
        assert result["total"] == 3500.0

    def test_none_values_skipped(self, local_tz):
        hourly_data = [
            (local_tz.localize(datetime(2024, 1, 1, 8, 0, 0)), 1.0),
            (local_tz.localize(datetime(2024, 1, 1, 9, 0, 0)), None),
            (local_tz.localize(datetime(2024, 1, 1, 10, 0, 0)), 0.5),
        ]
        result = EnergyDataAggregator.aggregate_from_hourly(hourly_data)
        assert result["off_peak"] == 1500.0
        assert result["total"] == 1500.0

    def test_negative_values(self, local_tz):
        hourly_data = [(local_tz.localize(datetime(2024, 1, 1, 8, 0, 0)), -0.5)]
        result = EnergyDataAggregator.aggregate_from_hourly(hourly_data)
        assert result["off_peak"] == -500.0
        assert result["total"] == -500.0

    def test_full_day(self, local_tz):
        hourly_data = [
            (local_tz.localize(datetime(2024, 1, 1, h, 0, 0)), 1.0) for h in range(24)
        ]
        result = EnergyDataAggregator.aggregate_from_hourly(hourly_data)
        # Hours 0-14 → off_peak (15h × 1000 Wh), 16-20 → peak (5h × 1000 Wh)
        # Hour 15 + 21-23 → part_peak (4h × 1000 Wh)
        assert result["off_peak"] == 15000.0
        assert result["peak"] == 5000.0
        assert result["part_peak"] == 4000.0
        assert result["total"] == 24000.0


# ---------------------------------------------------------------------------
# Tests for aggregate_from_minutes  (public API)
# ---------------------------------------------------------------------------


class TestAggregateFromMinutes:
    def test_empty_data(self, local_tz):
        result = EnergyDataAggregator.aggregate_from_minutes(
            local_tz.localize(datetime(2024, 1, 1, 0, 0, 0)), []
        )
        assert result == {"total": 0.0, "peak": 0.0, "part_peak": 0.0, "off_peak": 0.0}

    def test_none_values_skipped(self, local_tz):
        result = EnergyDataAggregator.aggregate_from_minutes(
            local_tz.localize(datetime(2024, 1, 1, 8, 0, 0)), [0.001, None, 0.001]
        )
        assert result["off_peak"] == 2.0
        assert result["total"] == 2.0

    def test_negative_values(self, local_tz):
        result = EnergyDataAggregator.aggregate_from_minutes(
            local_tz.localize(datetime(2024, 1, 1, 8, 0, 0)), [-0.001]
        )
        assert result["off_peak"] == -1.0
        assert result["total"] == -1.0

    def test_mixed_hours(self, local_tz):
        usage_data = [0.001] * 60
        for _ in range(1, 24):
            usage_data += [0.001] * 60
        result = EnergyDataAggregator.aggregate_from_minutes(
            local_tz.localize(datetime(2024, 1, 1, 0, 0, 0)), usage_data
        )
        assert result["off_peak"] == 900.0
        assert result["peak"] == 300.0
        assert result["part_peak"] == 240.0
        assert result["total"] == 1440.0


# ---------------------------------------------------------------------------
# Tests for aggregate_from_15min  (public API)
# ---------------------------------------------------------------------------


class TestAggregateFrom15min:
    def test_empty_data(self, local_tz):
        result = EnergyDataAggregator.aggregate_from_15min(
            local_tz.localize(datetime(2024, 1, 1, 0, 0, 0)), []
        )
        assert result == {"total": 0.0, "peak": 0.0, "part_peak": 0.0, "off_peak": 0.0}

    def test_single_period_off_peak(self, local_tz):
        result = EnergyDataAggregator.aggregate_from_15min(
            local_tz.localize(datetime(2024, 1, 1, 8, 0, 0)), [0.001]
        )
        assert result["off_peak"] == 1.0
        assert result["total"] == 1.0

    def test_single_period_peak(self, local_tz):
        result = EnergyDataAggregator.aggregate_from_15min(
            local_tz.localize(datetime(2024, 1, 1, 18, 0, 0)), [0.001]
        )
        assert result["peak"] == 1.0
        assert result["total"] == 1.0

    def test_mixed_hours(self, local_tz):
        usage_data = [0.0] * 40
        usage_data[0] = 0.001    # hour 8, off-peak
        usage_data[28] = 0.001   # hour 15, part-peak
        usage_data[29] = 0.001   # hour 16, peak
        usage_data[37] = 0.001   # hour 21, part-peak
        result = EnergyDataAggregator.aggregate_from_15min(
            local_tz.localize(datetime(2024, 1, 1, 8, 0, 0)), usage_data
        )
        assert result["off_peak"] == 1.0
        assert result["part_peak"] == 2.0
        assert result["peak"] == 1.0
        assert result["total"] == 4.0

    def test_negative_values(self, local_tz):
        result = EnergyDataAggregator.aggregate_from_15min(
            local_tz.localize(datetime(2024, 1, 1, 8, 0, 0)), [-0.001]
        )
        assert result["off_peak"] == -1.0
        assert result["total"] == -1.0

    def test_none_values_skipped(self, local_tz):
        result = EnergyDataAggregator.aggregate_from_15min(
            local_tz.localize(datetime(2024, 1, 1, 8, 0, 0)), [0.001, None, 0.001]
        )
        assert result["off_peak"] == 2.0
        assert result["total"] == 2.0

    def test_crosses_midnight(self, local_tz):
        usage_data = [0.001, 0.001]
        result = EnergyDataAggregator.aggregate_from_15min(
            local_tz.localize(datetime(2024, 1, 1, 23, 45, 0)), usage_data
        )
        assert result["part_peak"] == 1.0
        assert result["off_peak"] == 1.0
        assert result["total"] == 2.0

    def test_full_day(self, local_tz):
        usage_data = [0.001] * 96
        result = EnergyDataAggregator.aggregate_from_15min(
            local_tz.localize(datetime(2024, 1, 1, 0, 0, 0)), usage_data
        )
        assert result["off_peak"] == 60.0
        assert result["part_peak"] == 16.0
        assert result["peak"] == 20.0
        assert result["total"] == 96.0
