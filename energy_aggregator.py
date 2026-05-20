"""
Energy data aggregation for Time-of-Use (TOU) reporting.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import pytz

from config import get_timezone


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

    _BUCKET_DEFAULTS: dict[str, float] = {
        "total": 0.0,
        "peak": 0.0,
        "part_peak": 0.0,
        "off_peak": 0.0,
    }

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
        local_tz = pytz.timezone(get_timezone())
        if timestamp.tzinfo is None:
            timestamp = local_tz.localize(timestamp)
        return timestamp.astimezone(local_tz).hour

    @staticmethod
    def _aggregate(
        start_time: datetime,
        data: list[float],
        step: timedelta,
    ) -> dict[str, float]:
        """Aggregate index-based data into TOU buckets.

        Each element in *data* represents one time step starting from
        *start_time* advancing by *step* (e.g. 1 second, 1 minute,
        15 minutes, or 1 hour).

        Args:
            start_time: Start time of the first data point.
            data: List of kWh values, one per time step.
            step: Time delta between consecutive data points.

        Returns:
            Dictionary with bucket totals in Wh:
            {'total', 'peak', 'part_peak', 'off_peak'}
        """
        buckets: dict[str, float] = dict(EnergyDataAggregator._BUCKET_DEFAULTS)

        local_tz = pytz.timezone(get_timezone())
        if start_time.tzinfo is None:
            start_local = local_tz.localize(start_time)
        else:
            start_local = start_time.astimezone(local_tz)

        for idx, usage_kwh in enumerate(data):
            if usage_kwh is None:
                continue
            timestamp = start_local + step * idx
            usage_wh = usage_kwh * 1000.0
            bucket = EnergyDataAggregator.classify_hour(timestamp.hour)
            buckets[bucket] += usage_wh
            buckets["total"] += usage_wh

        return buckets

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
        buckets: Dict[str, float] = dict(EnergyDataAggregator._BUCKET_DEFAULTS)

        for timestamp, usage_kwh in hourly_data:
            if usage_kwh is None:
                continue
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
        return EnergyDataAggregator._aggregate(
            start_time, usage_data, timedelta(seconds=1)
        )

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
        return EnergyDataAggregator._aggregate(
            start_time, usage_data, timedelta(minutes=1)
        )

    @staticmethod
    def aggregate_from_15min(
        start_time: datetime, usage_data: List[float]
    ) -> Dict[str, float]:
        """Aggregate 15-minute energy data into TOU buckets.

        Args:
            start_time: Start time of the first data point (15-min aligned).
            usage_data: List of kWh values, one per 15-minute period.

        Returns:
            Dictionary with bucket totals in Wh:
            {'total': float, 'peak': float, 'part_peak': float, 'off_peak': float}
        """
        return EnergyDataAggregator._aggregate(
            start_time, usage_data, timedelta(minutes=15)
        )
