"""
Energy data aggregation for Time-of-Use (TOU) reporting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, List, Tuple

import pytz

from config import get_timezone


@dataclass(frozen=True)
class TOUBuckets:
    """Time-of-Use bucket totals in Wh.

    Replaces the dict return from _aggregate() with a typed dataclass.
    """

    total: float = 0.0
    peak: float = 0.0
    part_peak: float = 0.0
    off_peak: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for backward compat."""
        return {
            "total": self.total,
            "peak": self.peak,
            "part_peak": self.part_peak,
            "off_peak": self.off_peak,
        }


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
        local_tz = pytz.timezone(get_timezone())
        if timestamp.tzinfo is None:
            timestamp = local_tz.localize(timestamp)
        return timestamp.astimezone(local_tz).hour

    @staticmethod
    def _aggregate(
        start_time: datetime,
        data: list[float],
        step: timedelta,
    ) -> TOUBuckets:
        """Aggregate index-based data into TOU buckets.

        Each element in *data* represents one time step starting from
        *start_time* advancing by *step* (e.g. 1 second, 1 minute,
        15 minutes, or 1 hour).

        Args:
            start_time: Start time of the first data point.
            data: List of kWh values, one per time step.
            step: Time delta between consecutive data points.

        Returns:
            TOUBuckets with bucket totals in Wh.
        """
        total = 0.0
        peak = 0.0
        part_peak = 0.0
        off_peak = 0.0

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
            if bucket == "peak":
                peak += usage_wh
            elif bucket == "part_peak":
                part_peak += usage_wh
            else:
                off_peak += usage_wh
            total += usage_wh

        return TOUBuckets(
            total=total, peak=peak, part_peak=part_peak, off_peak=off_peak,
        )

    @staticmethod
    def aggregate_from_hourly(
        hourly_data: List[Tuple[datetime, float]],
    ) -> TOUBuckets:
        """
        Aggregate hourly energy data into TOU buckets.

        Args:
            hourly_data: List of (timestamp, usage_kwh) tuples.
                        Timestamp should be hour-aligned.

        Returns:
            TOUBuckets with bucket totals in Wh.
        """
        total = 0.0
        peak = 0.0
        part_peak = 0.0
        off_peak = 0.0

        for timestamp, usage_kwh in hourly_data:
            if usage_kwh is None:
                continue
            usage_wh = usage_kwh * 1000.0
            local_hour = EnergyDataAggregator._get_local_hour(timestamp)
            bucket = EnergyDataAggregator.classify_hour(local_hour)
            if bucket == "peak":
                peak += usage_wh
            elif bucket == "part_peak":
                part_peak += usage_wh
            else:
                off_peak += usage_wh
            total += usage_wh

        return TOUBuckets(
            total=total, peak=peak, part_peak=part_peak, off_peak=off_peak,
        )

    @staticmethod
    def aggregate_from_seconds(
        start_time: datetime, usage_data: List[float]
    ) -> TOUBuckets:
        """
        Aggregate per-second energy data into TOU buckets.

        Args:
            start_time: Start time of the first data point.
            usage_data: List of kWh values, one per second.

        Returns:
            TOUBuckets with bucket totals in Wh.
        """
        return EnergyDataAggregator._aggregate(
            start_time, usage_data, timedelta(seconds=1)
        )

    @staticmethod
    def aggregate_from_minutes(
        start_time: datetime, usage_data: List[float]
    ) -> TOUBuckets:
        """
        Aggregate per-minute energy data into TOU buckets.

        Args:
            start_time: Start time of the first data point.
            usage_data: List of kWh values, one per minute.

        Returns:
            TOUBuckets with bucket totals in Wh.
        """
        return EnergyDataAggregator._aggregate(
            start_time, usage_data, timedelta(minutes=1)
        )

    @staticmethod
    def aggregate_from_15min(
        start_time: datetime, usage_data: List[float]
    ) -> TOUBuckets:
        """Aggregate 15-minute energy data into TOU buckets.

        Args:
            start_time: Start time of the first data point (15-min aligned).
            usage_data: List of kWh values, one per 15-minute period.

        Returns:
            TOUBuckets with bucket totals in Wh.
        """
        return EnergyDataAggregator._aggregate(
            start_time, usage_data, timedelta(minutes=15)
        )
