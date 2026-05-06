"""
Unit tests for EnergyDataAggregator.
"""

import unittest
from datetime import datetime
import pytz

from energy_aggregator import EnergyDataAggregator
from util import get_timezone


class TestClassifyHour(unittest.TestCase):
    def test_off_peak_hours(self):
        for hour in range(0, 15):
            self.assertEqual(
                EnergyDataAggregator.classify_hour(hour),
                "off_peak",
                f"Hour {hour} should be off_peak",
            )

    def test_part_peak_transition(self):
        self.assertEqual(
            EnergyDataAggregator.classify_hour(15),
            "part_peak",
            "Hour 15 should be part_peak",
        )

    def test_peak_hours(self):
        for hour in range(16, 21):
            self.assertEqual(
                EnergyDataAggregator.classify_hour(hour),
                "peak",
                f"Hour {hour} should be peak",
            )

    def test_part_peak_evening(self):
        for hour in range(21, 24):
            self.assertEqual(
                EnergyDataAggregator.classify_hour(hour),
                "part_peak",
                f"Hour {hour} should be part_peak",
            )


local_tz = pytz.timezone(get_timezone())


class TestAggregateFromSeconds(unittest.TestCase):
    def test_empty_data(self):
        result = EnergyDataAggregator.aggregate_from_seconds(
            datetime(2024, 1, 1, 0, 0, 0, tzinfo=local_tz), []
        )
        self.assertEqual(
            result, {"total": 0.0, "peak": 0.0, "part_peak": 0.0, "off_peak": 0.0}
        )

    def test_single_second_off_peak(self):
        usage_data = [0.001]
        result = EnergyDataAggregator.aggregate_from_seconds(
            datetime(2024, 1, 1, 8, 0, 0, tzinfo=local_tz), usage_data
        )
        self.assertEqual(result["off_peak"], 1.0)
        self.assertEqual(result["total"], 1.0)

    def test_single_second_peak(self):
        usage_data = [0.001]
        result = EnergyDataAggregator.aggregate_from_seconds(
            datetime(2024, 1, 1, 18, 0, 0, tzinfo=local_tz), usage_data
        )
        self.assertEqual(result["peak"], 1.0)
        self.assertEqual(result["total"], 1.0)

    def test_mixed_hours(self):
        start_time = local_tz.localize(datetime(2024, 1, 1, 8, 0, 0))
        usage = [0.001] * 3600 * 4
        result = EnergyDataAggregator.aggregate_from_seconds(start_time, usage)
        self.assertEqual(result["off_peak"], 14400.0)
        self.assertEqual(result["peak"], 0.0)
        self.assertEqual(result["part_peak"], 0.0)
        self.assertEqual(result["total"], 14400.0)

    def test_bucket_boundaries(self):
        usage_data = [0.001] * 3600
        for _ in range(1, 24):
            usage_data += [0.001] * 3600

        result = EnergyDataAggregator.aggregate_from_seconds(
            datetime(2024, 1, 1, 0, 0, 0, tzinfo=local_tz), usage_data
        )

        self.assertEqual(result["off_peak"], 54000.0)
        self.assertEqual(result["peak"], 18000.0)
        self.assertEqual(result["part_peak"], 14400.0)
        self.assertEqual(result["total"], 86400.0)


class TestAggregateFromHourly(unittest.TestCase):
    def test_empty_data(self):
        result = EnergyDataAggregator.aggregate_from_hourly([])
        self.assertEqual(
            result, {"total": 0.0, "peak": 0.0, "part_peak": 0.0, "off_peak": 0.0}
        )

    def test_single_hour_off_peak(self):
        hourly_data = [(datetime(2024, 1, 1, 8, 0, 0, tzinfo=local_tz), 1.0)]
        result = EnergyDataAggregator.aggregate_from_hourly(hourly_data)
        self.assertEqual(result["off_peak"], 1000.0)
        self.assertEqual(result["total"], 1000.0)

    def test_single_hour_peak(self):
        hourly_data = [(datetime(2024, 1, 1, 18, 0, 0, tzinfo=local_tz), 1.0)]
        result = EnergyDataAggregator.aggregate_from_hourly(hourly_data)
        self.assertEqual(result["peak"], 1000.0)
        self.assertEqual(result["total"], 1000.0)

    def test_mixed_hours(self):
        hourly_data = [
            (datetime(2024, 1, 1, 8, 0, 0, tzinfo=local_tz), 1.0),
            (datetime(2024, 1, 1, 17, 0, 0, tzinfo=local_tz), 2.0),
            (datetime(2024, 1, 1, 22, 0, 0, tzinfo=local_tz), 0.5),
        ]
        result = EnergyDataAggregator.aggregate_from_hourly(hourly_data)
        self.assertEqual(result["off_peak"], 1000.0)
        self.assertEqual(result["peak"], 2000.0)
        self.assertEqual(result["part_peak"], 500.0)
        self.assertEqual(result["total"], 3500.0)


class TestAggregateFromMinutes(unittest.TestCase):
    def test_empty_data(self):
        result = EnergyDataAggregator.aggregate_from_minutes(
            datetime(2024, 1, 1, 0, 0, 0, tzinfo=local_tz), []
        )
        self.assertEqual(
            result, {"total": 0.0, "peak": 0.0, "part_peak": 0.0, "off_peak": 0.0}
        )

    def test_mixed_hours(self):
        usage_data = [0.001] * 60
        for _ in range(1, 24):
            usage_data += [0.001] * 60

        result = EnergyDataAggregator.aggregate_from_minutes(
            datetime(2024, 1, 1, 0, 0, 0, tzinfo=local_tz), usage_data
        )

        self.assertEqual(result["off_peak"], 900.0)
        self.assertEqual(result["peak"], 300.0)
        self.assertEqual(result["part_peak"], 240.0)
        self.assertEqual(result["total"], 1440.0)


class TestAggregateFrom15min(unittest.TestCase):
    def test_empty_data(self):
        result = EnergyDataAggregator.aggregate_from_15min(
            datetime(2024, 1, 1, 0, 0, 0, tzinfo=local_tz), []
        )
        self.assertEqual(
            result, {"total": 0.0, "peak": 0.0, "part_peak": 0.0, "off_peak": 0.0}
        )

    def test_single_period_off_peak(self):
        """One 15-min period at hour 8 (off-peak)."""
        usage_data = [0.001]  # 0.001 kWh per 15 min
        result = EnergyDataAggregator.aggregate_from_15min(
            datetime(2024, 1, 1, 8, 0, 0, tzinfo=local_tz), usage_data
        )
        self.assertEqual(result["off_peak"], 1.0)
        self.assertEqual(result["total"], 1.0)

    def test_single_period_peak(self):
        """One 15-min period at hour 18 (peak)."""
        usage_data = [0.001]
        result = EnergyDataAggregator.aggregate_from_15min(
            datetime(2024, 1, 1, 18, 0, 0, tzinfo=local_tz), usage_data
        )
        self.assertEqual(result["peak"], 1.0)
        self.assertEqual(result["total"], 1.0)

    def test_mixed_hours(self):
        """Periods spanning off-peak, part-peak, and peak hours."""
        # Start at hour 8; each index advances 15 minutes:
        # idx 0 → 8:00 (off), idx 4 → 9:00 (off), ...
        # idx 28 → 15:00 (part), idx 29 → 16:00 (peak)
        # idx 37 → 21:00 (part)
        usage_data = [0.0] * 40
        usage_data[0] = 0.001    # hour 8, off-peak
        usage_data[28] = 0.001   # hour 15, part-peak
        usage_data[29] = 0.001   # hour 16, peak
        usage_data[37] = 0.001   # hour 21, part-peak
        result = EnergyDataAggregator.aggregate_from_15min(
            datetime(2024, 1, 1, 8, 0, 0, tzinfo=local_tz), usage_data
        )
        self.assertEqual(result["off_peak"], 1.0)
        self.assertEqual(result["part_peak"], 2.0)
        self.assertEqual(result["peak"], 1.0)
        self.assertEqual(result["total"], 4.0)

    def test_negative_values(self):
        """Negative kWh values (solar export) reduce bucket totals."""
        usage_data = [-0.001]
        result = EnergyDataAggregator.aggregate_from_15min(
            datetime(2024, 1, 1, 8, 0, 0, tzinfo=local_tz), usage_data
        )
        self.assertEqual(result["off_peak"], -1.0)
        self.assertEqual(result["total"], -1.0)

    def test_none_values_skipped(self):
        """None values in data are skipped without error."""
        usage_data = [0.001, None, 0.001]
        result = EnergyDataAggregator.aggregate_from_15min(
            datetime(2024, 1, 1, 8, 0, 0, tzinfo=local_tz), usage_data
        )
        self.assertEqual(result["off_peak"], 2.0)
        self.assertEqual(result["total"], 2.0)

    def test_crosses_midnight(self):
        """Data spanning midnight classifies hours correctly."""
        # Period at hour 23 (part-peak), next period wraps to hour 0 (off-peak)
        usage_data = [0.001, 0.001]
        result = EnergyDataAggregator.aggregate_from_15min(
            datetime(2024, 1, 1, 23, 45, 0, tzinfo=local_tz), usage_data
        )
        self.assertEqual(result["part_peak"], 1.0)
        self.assertEqual(result["off_peak"], 1.0)
        self.assertEqual(result["total"], 2.0)

    def test_full_day(self):
        """96 periods (one per 15 min for 24 hours) classify correctly."""
        usage_data = [0.001] * 96
        result = EnergyDataAggregator.aggregate_from_15min(
            datetime(2024, 1, 1, 0, 0, 0, tzinfo=local_tz), usage_data
        )
        # Off-peak: hours 0-14 (15 hours * 4 periods = 60)
        self.assertEqual(result["off_peak"], 60.0)
        # Part-peak: hour 15 + hours 21-23 (4 hours * 4 periods = 16)
        self.assertEqual(result["part_peak"], 16.0)
        # Peak: hours 16-20 (5 hours * 4 periods = 20)
        self.assertEqual(result["peak"], 20.0)
        self.assertEqual(result["total"], 96.0)


if __name__ == "__main__":
    unittest.main()
