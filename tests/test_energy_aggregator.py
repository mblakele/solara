"""
Unit tests for EnergyDataAggregator.
"""

import unittest
from datetime import datetime
import pytz

from energy_aggregator import EnergyDataAggregator
from util import TIMEZONE


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


local_tz = pytz.timezone(TIMEZONE)


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


if __name__ == "__main__":
    unittest.main()
