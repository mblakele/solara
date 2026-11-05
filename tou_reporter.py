"""Time-of-Use (TOU) reporting for historical 15-minute usage data.

Split out of ``metrics.py`` (architecture-v2 Phase E.1). ``metrics.py``
re-exports these names so existing imports keep working.
"""
# The base-class init call is intentionally identical to HourlyProjection's;
# extracting it would hide the explicit session injection (A.2).
# pylint: disable=duplicate-code

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from time import monotonic as _monotonic
from typing import Any, Optional

import requests
from pyemvue import PyEmVue
from pyemvue.enums import Scale, Unit

from config import Config
from energy_aggregator import EnergyDataAggregator, TOUBuckets
from vue_session import MetricsBase


@dataclass(frozen=True)
class TOUResult:
    """Result of a TOU (Time-of-Use) query.

    Wraps TOUBuckets and NBC total for the requested date range, plus
    optional per-15-minute period details for the report table.
    Replaces the dict return from _get_tou_model().
    """

    buckets: TOUBuckets
    nbc: float
    periods: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for backward compat."""
        return {
            "buckets": self.buckets.to_dict(),
            "nbc": self.nbc,
        }


class TOUBudgetExceeded(TimeoutError):
    """Raised when a TOU report run exceeds its request time budget.

    The reporter checks the deadline between 15-minute chunks so a slow or
    degraded API cannot hold a web worker past the request budget.
    """


class TOUReporter(MetricsBase):
    """
    Multi-day Time-of-Use aggregation.

    Fetches historical data at 15-minute granularity and aggregates
    into TOU buckets (total, peak, part_peak, off_peak). NBC is the
    sum of all 15-minute period values in Wh across the entire period.
    Supports both historical (completed days) and real-time
    (current partial day) reporting.
    """

    def __init__(
        self,
        start_date: datetime,
        end_date: datetime,
        logger_next: Optional[logging.Logger] = None,
        config: Config | None = None,
        vue: Optional[PyEmVue] = None,
        vue_auth: Optional[dict[str, Any]] = None,
        device_info: Optional[dict[int, Any]] = None,
        *,
        budget_secs: float | None = None,
    ) -> None:
        super().__init__(
            logger_next,
            config=config,
            vue=vue,
            vue_auth=vue_auth,
            device_info=device_info,
        )

        self.start_date = start_date
        self.end_date = end_date
        self.tou_result: Optional[TOUBuckets] = None
        self.nbc_result: Optional[float] = None
        self.periods: list[dict[str, Any]] = []

        # Request budget (architecture-v2 F.2): checked between chunks so a
        # degraded API cannot hold a web worker forever.
        self._deadline = (
            _monotonic() + budget_secs if budget_secs is not None else None
        )

        self.fetch_usage_data()
        self.aggregate_tou()

    def _check_budget(self) -> None:
        """Raise :class:`TOUBudgetExceeded` when the request budget is spent.

        Raises:
            TOUBudgetExceeded: The deadline passed before the next chunk.
        """
        deadline = getattr(self, "_deadline", None)
        if deadline is not None and _monotonic() >= deadline:
            raise TOUBudgetExceeded(
                "TOU report exceeded its request time budget"
            )

    def fetch_usage_data(self) -> None:
        """
        Fetch historical usage data at 15-minute granularity.

        The 15MIN scale has a much larger API limit than per-minute data,
        but we still chunk to be safe and handle large date ranges.
        """
        self.usage_data_list: list[dict[str, Any]] = []
        self._fetch_error: Optional[Exception] = None

        for vdi in self.device_info.values():
            for chan in vdi.channels:
                self.logger.debug("fetching TOU data for channel: %s", chan.name)

                current_time = self.start_date
                while current_time < self.end_date:
                    self._check_budget()
                    chunk_end = min(current_time + timedelta(days=7), self.end_date)

                    try:
                        self.logger.debug(
                            "fetching chunk: %s - %s", current_time, chunk_end
                        )
                        usage_data, usage_data_start = self.vue.get_chart_usage(
                            chan,
                            current_time,
                            chunk_end,
                            scale=Scale.MINUTES_15.value,
                            unit=Unit.KWH.value,
                        )

                        if usage_data and len(usage_data) > 0:
                            self.usage_data_list.append(
                                {"start": usage_data_start, "data": usage_data}
                            )
                    except (requests.exceptions.RequestException, IOError) as ex:
                        error_msg = str(ex)
                        if isinstance(ex, requests.exceptions.HTTPError):
                            if ex.response is not None:
                                try:
                                    error_msg = f"{error_msg}: {ex.response.text}"
                                except (
                                    requests.exceptions.RequestException,
                                    AttributeError,
                                ):
                                    pass
                        self.logger.exception("error fetching TOU data: %s", error_msg)
                        self._fetch_error = ex
                        raise

                    current_time = chunk_end + timedelta(minutes=15)

    def aggregate_tou(self) -> None:
        """
        Aggregate fetched usage data into TOU buckets and NBC total.

        Delegates to EnergyDataAggregator for TOU bucket classification.
        NBC is the sum of all 15-minute period values in Wh across the
        entire reporting period. Also builds the per-15-minute period
        details used by the TOU report table.
        """
        total = 0.0
        peak = 0.0
        part_peak = 0.0
        off_peak = 0.0

        nbc_total_wh = 0.0
        periods: list[dict[str, Any]] = []

        for data_chunk in self.usage_data_list:
            chunk_buckets: TOUBuckets = EnergyDataAggregator.aggregate_from_15min(
                data_chunk["start"], data_chunk["data"]
            )

            total += chunk_buckets.total
            peak += chunk_buckets.peak
            part_peak += chunk_buckets.part_peak
            off_peak += chunk_buckets.off_peak

            # Sum positive 15-min periods only (imports); negatives are exports, ignored
            chunk_start = data_chunk["start"]
            for idx, usage_kwh in enumerate(data_chunk["data"]):
                if usage_kwh is None:
                    continue
                timestamp = chunk_start + timedelta(minutes=15 * idx)
                wh = usage_kwh * 1000.0
                bucket = EnergyDataAggregator.classify_timestamp(timestamp)
                periods.append(
                    {"timestamp": timestamp, "wh": wh, "bucket": bucket}
                )
                if usage_kwh > 0:
                    nbc_total_wh += wh

        self.tou_result = TOUBuckets(
            total=total, peak=peak, part_peak=part_peak, off_peak=off_peak,
        )
        self.nbc_result = nbc_total_wh
        self.periods = periods
