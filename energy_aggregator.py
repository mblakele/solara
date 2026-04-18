"""
Energy data aggregation for Time-of-Use (TOU) reporting.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Tuple
import pytz

from util import TIMEZONE


class EnergyDataAggregator:
    """
    Aggregates raw energy consumption data into Time-of-Use buckets.

    IMPORTANT: Negative kWh values represent solar generation (power exported
    to the grid). These are aggregated normally — a negative value in any bucket
    reduces that bucket's total, which is correct for net metering calculations.

    Calculates four buckets based on time-of-day rules:
    - Total: Sum of all power used in the period.
    - Peak: Sum of power used daily from 16:00 to 21:00.
    - Off-Peak: Sum of power used daily from 00:00 to 15:00.
    - Part-Peak: Sum of power used daily from 15:00 to 16:00 and from 21:00 to 00:00.

    Units: All results in Watt-hours (Wh). Negative totals indicate net export.
    """

    @staticmethod
    def classify_hour(hour: int) -> str:
        """
        Classify a given hour (0-23) into a TOU bucket.

        Args:
            hour: Hour of day (0-23).

         Returns:
             Bucket name: 'peak', 'part_peak', or 'off_peak'.
        """
        if 16 <= hour <= 20:
            return "peak"
        if hour == 15 or hour >= 21:
            return "part_peak"
        return "off_peak"

    @staticmethod
    def _get_local_hour(timestamp: datetime) -> int:
        """Convert a timestamp to local hour for TOU bucket classification."""
        local_tz = pytz.timezone(TIMEZONE)
        if timestamp.tzinfo is None:
            timestamp = local_tz.localize(timestamp)
        return timestamp.astimezone(local_tz).hour

    @staticmethod
    def aggregate_from_hourly(
        hourly_data: List[Tuple[datetime, float]],
    ) -> Dict[str, float]:
        """
        Aggregate hourly energy data into TOU buckets.

        Args:
            hourly_data: List of (timestamp, usage_kwh) tuples.
                        Timestamp should be hour-aligned.

        Returns:
            Dictionary with bucket totals in Wh:
            {'total': float, 'peak': float, 'part_peak': float, 'off_peak': float}
        """
        buckets = {"total": 0.0, "peak": 0.0, "part_peak": 0.0, "off_peak": 0.0}

        for timestamp, usage_kwh in hourly_data:
            usage_wh = usage_kwh * 1000.0
            local_hour = EnergyDataAggregator._get_local_hour(timestamp)
            bucket = EnergyDataAggregator.classify_hour(local_hour)
            buckets[bucket] += usage_wh
            buckets["total"] += usage_wh

        return buckets

    @staticmethod
    def aggregate_from_seconds(
        start_time: datetime, usage_data: List[float]
    ) -> Dict[str, float]:
        """
        Aggregate per-second energy data into TOU buckets.

        Args:
            start_time: Start time of the first data point.
            usage_data: List of kWh values, one per second.

        Returns:
            Dictionary with bucket totals in Wh:
            {'total': float, 'peak': float, 'part_peak': float, 'off_peak': float}
        """
        buckets = {"total": 0.0, "peak": 0.0, "part_peak": 0.0, "off_peak": 0.0}

        start_local = (
            start_time.astimezone(pytz.timezone(TIMEZONE))
            if start_time.tzinfo
            else pytz.timezone(TIMEZONE).localize(start_time)
        )
        for idx, usage_kwh in enumerate(usage_data):
            timestamp = start_local + timedelta(seconds=idx)
            usage_wh = usage_kwh * 1000.0
            bucket = EnergyDataAggregator.classify_hour(timestamp.hour)
            buckets[bucket] += usage_wh
            buckets["total"] += usage_wh

        return buckets

    @staticmethod
    def aggregate_from_minutes(
        start_time: datetime, usage_data: List[float]
    ) -> Dict[str, float]:
        """
        Aggregate per-minute energy data into TOU buckets.

        Args:
            start_time: Start time of the first data point.
            usage_data: List of kWh values, one per minute.

        Returns:
            Dictionary with bucket totals in Wh:
            {'total': float, 'peak': float, 'part_peak': float, 'off_peak': float}
        """
        buckets = {"total": 0.0, "peak": 0.0, "part_peak": 0.0, "off_peak": 0.0}

        start_local = (
            start_time.astimezone(pytz.timezone(TIMEZONE))
            if start_time.tzinfo
            else pytz.timezone(TIMEZONE).localize(start_time)
        )
        for idx, usage_kwh in enumerate(usage_data):
            timestamp = start_local + timedelta(minutes=idx)
            usage_wh = usage_kwh * 1000.0
            bucket = EnergyDataAggregator.classify_hour(timestamp.hour)
            buckets[bucket] += usage_wh
            buckets["total"] += usage_wh

        return buckets
