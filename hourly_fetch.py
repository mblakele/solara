"""Hourly fetch, populate, prediction, and drift tracking.

Split out of ``metrics.py`` (architecture-v2 Phase E.1). ``metrics.py``
re-exports these names so existing imports keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import threading
from time import monotonic as _monotonic
from typing import Any, Optional

import requests
from pyemvue import PyEmVue
from pyemvue.enums import Scale, Unit

from config import Config
from constants import DRIFT_REJECTION_ALERT_AFTER
from energy_cache import EnergyCache
from prediction import (
    DevicePrediction,
    PredictionMixin,
    _PopulationResult,
)
from util import ceil_to_qh, floor_to_qh, is_debug
from vue_session import MetricsBase, RetryableMetricsException


logger = logging.getLogger(__name__)


MAX_FETCH_WINDOW = timedelta(hours=1)

# Drift-rejection tracker keyed by ``(channel_num, chart_start)``.  A fetch
# whose ``data_start`` drifts off the requested (QH-aligned) ``chart_start``
# is rejected so a misaligned window never reaches the cache; if the API
# permanently drops the head sample of a QH, every fetch for that QH is
# rejected forever and the QH stalls.  This dict counts consecutive
# rejections per QH so the stall becomes observable — an error log plus a
# one-time Telegram alert per QH key (see ``_fetch_channel_data``) — instead
# of an infinite silent retry loop.
_drift_rejections: dict[tuple[int, datetime], int] = {}

# One-time alert events queued when a QH key first crosses
# ``DRIFT_REJECTION_ALERT_AFTER`` consecutive rejections.  Drained by
# ``drain_drift_alerts()`` (the LoadManager cycle) so the caller can log at
# ERROR level and surface the stall as a Telegram alert.
_drift_alerts: list["DriftAlert"] = []

# Serializes mutations of ``_drift_rejections``/``_drift_alerts``: fetches
# run in EnergyCache worker threads while the LoadManager cycle drains from
# its own thread.
_drift_lock = threading.Lock()


@dataclass(frozen=True)
class DriftAlert:
    """A persistent data-start drift detected for one channel/QH key.

    Attributes:
        channel_num: The Emporia channel whose fetches are being rejected.
        chart_start: The requested (QH-aligned) fetch window start.
        data_start: The API-reported ``firstUsageInstant`` (drifted).
        count: Consecutive rejections observed for this key at enqueue time.
    """

    channel_num: int
    chart_start: datetime
    data_start: datetime
    count: int


def drain_drift_alerts() -> list[DriftAlert]:
    """Return and clear all pending drift alert events.

    Safe to call from any thread; the fetch path and this drain serialize
    on an internal lock.

    Returns:
        The list of ``DriftAlert`` events queued since the last drain.
    """
    with _drift_lock:
        pending = list(_drift_alerts)
        _drift_alerts.clear()
        return pending


def cap_chart_start(chart_start: datetime, now: datetime) -> datetime:
    """Cap chart_start to prevent over-fetching after stale cache.

    If chart_start is more than 1 hour before *now*, return the earliest
    appropriate quarter-hour boundary.  Otherwise return chart_start unchanged.

    Also guards against chart_start being in the future (which causes the
    Emporia API to return a 400 when start > end).  A future chart_start
    indicates corrupted cache state; fall back to a full-hour fetch.

    This guard prevents the Emporia API from rejecting large 1-second
    resolution requests when the load manager has been idle for a long
    period (e.g. disabled overnight).

    Args:
        chart_start: The proposed fetch window start time.
        now: The current time.

    Returns:
        A datetime no more than 1 hour before *now*, or *chart_start*
        unchanged if it is already within the 1-hour window.
    """
    if chart_start > now:
        return ceil_to_qh(now - MAX_FETCH_WINDOW)

    if now - chart_start <= MAX_FETCH_WINDOW:
        return chart_start

    return ceil_to_qh(now - MAX_FETCH_WINDOW)


def _chart_start_for(energy_cache: EnergyCache, now: datetime) -> datetime:
    """Pick the fetch window start for the current cycle.

    On the first call (``data_start`` is None) the full hour up to *now* is
    fetched.  In steady state the window starts at the current QH boundary
    so replace semantics need no overlap/merge logic.  When the cached
    window still starts in an already-completed QH (QH-aligned
    ``data_start`` older than the current QH), the window stays anchored to
    that older boundary so the completed QH reaches 900 samples and is
    compacted into a ``CompletedNBCPeriod`` on the first fetch after the
    boundary — otherwise ``EnergyCache`` replace semantics would discard
    the un-compacted window and lose the QH.

    The anchor is intentionally QH-aligned-only: a stale *misaligned*
    ``data_start`` (e.g. ``13:45:31`` when ``now`` is ``14:00:10``) falls
    through to ``current_qh_start``, so its partially-filled QH is
    discarded by the next replace.  That is correct — the head of that QH
    genuinely is missing (the misalignment itself means the API never
    returned those samples).

    Args:
        energy_cache: The EnergyCache instance storing per-second samples.
        now: Current time.

    Returns:
        A QH-aligned fetch window start time.
    """
    data_start = energy_cache.data_start
    if data_start is None:
        return ceil_to_qh(now - MAX_FETCH_WINDOW)
    current_qh_start = floor_to_qh(now)
    aligned = data_start == ceil_to_qh(data_start)
    if data_start < current_qh_start and aligned:
        # Anchor to the stale QH-aligned data_start so samples accumulate to
        # 900 and compact() materialises a CompletedNBCPeriod.  This applies
        # to any number of QHs back — compact() processes all complete chunks
        # in one pass, so a two-QH gap is handled correctly as well.
        return data_start
    return current_qh_start


def create_metrics(energy_cache: EnergyCache, now: datetime, logger: logging.Logger) -> dict[str, Any] | None:
    """Fetch metrics with QH-window chart_start tracking via EnergyCache.

    On the first call, EnergyCache has no samples, so chart_start is set to
    3600 seconds ago (full hour of historical data). After that, chart_start
    is floored to the current QH boundary so the full quarter-hour is
    refetched on every cycle regardless of the API's data_start alignment;
    a stale QH-aligned data_start keeps the window anchored so a completed
    QH is not lost (see ``_chart_start_for``).

    Args:
        energy_cache: instance of EnergyCache.
        now: current datetime in local timezone.
        logger: Logger instance.

    Returns:
        Metrics dict from HourlyProjection, or None on failure.
    """
    # First call: fetch up to four QH periods.
    # Subsequent calls: QH-window fetch anchored at a QH boundary.
    logger.debug(
        "create_metrics: len %d, last_sample_at %s",
        len(energy_cache.samples or []),
        energy_cache.last_sample_at
    )
    try:
        chart_start = cap_chart_start(_chart_start_for(energy_cache, now), now)
        hp = HourlyProjection(now, logger, energy_cache)
        hp.populate(chart_start)
        logger.debug(
            "create_metrics result: devices=%d, data_start=%s, "
            "per_second_data_total=%d, data_lag_secs=%.1f",
            len(hp.metrics.get("devices", [])),
            hp.metrics.get("data_start"),
            sum(
                len(d.get("per_second_data", []))
                for d in hp.metrics.get("devices", [])
            ) if hp.metrics.get("devices") else 0,
            hp.metrics.get("_data_lag_secs", 0.0),
        )
        # Stamp the fetch time so consumers (SSE lag recalculation in
        # app.py, data_point_at in load_nbc.py) compute elapsed time from
        # when this data was actually fetched rather than falling back to
        # "now" at read time.
        hp.metrics["_fetched_at"] = now
        return hp.metrics

    except AssertionError as ae:
        logger.error("AssertionError in create_metrics: %s", ae)
        # force clean cache and full fetch on next cycle
        energy_cache.invalidate()
        raise RetryableMetricsException(ae) from ae


class HourlyProjection(PredictionMixin, MetricsBase):
    """
    Hourly prediction behavior.
    Maintains backward compatibility with original Metrics class.
    """

    def __init__(
        self,
        instant: datetime,
        logger_next: Optional[logging.Logger] = None,
        energy_cache: Optional["EnergyCache"] = None,
        config: Config | None = None,
        vue: Optional[PyEmVue] = None,
        vue_auth: Optional[dict[str, Any]] = None,
        device_info: Optional[dict[int, Any]] = None,
    ) -> None:
        self.metrics: dict[str, Any] = {
            "api_response": {},
            "debug": is_debug(config),
            "devices": [],
        }

        super().__init__(
            logger_next,
            config=config,
            vue=vue,
            vue_auth=vue_auth,
            device_info=device_info,
        )

        self.instant = instant
        self.metrics["instant"] = self.instant
        self.energy_cache = energy_cache  # Merged samples for NBC computation

    def populate(self, chart_start: datetime) -> dict[int, DevicePrediction]:
        """Fetch recent data using second granularity to minimize lag.

        The caller must compute chart_start. On the first call, use
        now - 3600 seconds, aligned to QH boundary, for up to a full hour
        of historical data. On subsequent calls, use the current QH
        boundary (floor_to_qh(now)) so the full quarter-hour is refetched.

        Evict older data so that the cache contains at most 3600 samples.

        Args:
            chart_start: Start of the fetch window (inclusive). Must be a
                timezone-aware datetime — never None.

        Returns:
            Dict of gid -> prediction results for each device.
        """
        # Cap chart_start to prevent over-fetching after stale cache.
        # If the cache has been stale for >1 hour (e.g. load manager was
        # disabled overnight), chart_start may point far in the past.
        # The Emporia API rejects large 1-s resolution requests.
        capped = cap_chart_start(chart_start, self.instant)
        if capped != chart_start:
            self.logger.debug(
                "[HourlyProjection.populate] chart_start %s capped to %s "
                "(was >1h before now)",
                chart_start,
                capped,
            )
            chart_start = capped

        self.logger.debug("populate from %s", chart_start)
        # Fetch usage data without mutating device_info
        population = self.populate_internal(chart_start, self.energy_cache)

        # Per-channel entries are monotonic elapsed seconds (floats) recorded
        # by _fetch_channel_data; sum them numerically and convert so "total"
        # stays a timedelta (JSON serializes it as an ISO 8601 duration).
        total_fetch_secs: float = sum(self.metrics["api_response"].values())
        self.metrics["api_response"]["total"] = timedelta(seconds=total_fetch_secs)

        # Compute predictions from population results
        predictions = self.predict(population)

        # Build metrics from pure computation results
        for gid, vdi in self.device_info.items():
            if gid not in population:
                continue
            pop_result = population[gid]
            pred_result = predictions[gid]
            device_metrics = self._compute_device_metrics(
                vdi, pop_result, pred_result
            )
            self.metrics["devices"].append(device_metrics.to_dict())

        self.logger.debug(
            "reporting metrics for %d devices", len(self.metrics["devices"])
        )

        # Expose the actual API-reported data start so that EnergyCache can
        # update data_start and last_sample_at.  Without this key the
        # get_or_fetch merge block silently skips the last_sample_at update,
        # leaving it permanently None and causing every call to create_metrics
        # to request a full-hour fetch instead of an incremental one.
        if population:
            first_gid = next(iter(population))
            self.metrics["data_start"] = population[first_gid].nbc_data_start

        # Compute overall API lag from the first device's prediction.
        # This represents how far behind the most recent data point is
        # relative to when metrics were computed (self.instant).
        if predictions:
            first_gid = next(iter(predictions))
            pred_result = predictions[first_gid]
            lag_td: timedelta = pred_result.lag
            self.metrics["_data_lag_secs"] = lag_td.total_seconds()

        return predictions

    def _fetch_channel_data(self, chan, chart_start, instant):
        """
        Fetch channel usage data from the VUE API and validate it.

        Returns a tuple of (usage_data_local, usage_data_start_local, channel_num).
        Raises RetryableMetricsException if no valid data is returned.
        """
        scale = Scale.SECOND.value
        fetch_started = _monotonic()
        usage_data_local, usage_data_start_local = self.vue.get_chart_usage(
            chan,
            chart_start,
            instant,
            scale=scale,
            unit=Unit.KWH.value,
        )
        # Monotonic delta: immune to NTP steps that would distort a
        # wall-clock (FakeClock/_CLOCK) measurement.
        fetch_elapsed = _monotonic() - fetch_started
        if (
            usage_data_start_local is None
            or usage_data_local is None
            or len(usage_data_local) < 1
            or usage_data_local[0] is None
        ):
            self.logger.debug({"usage_data": usage_data_local})
            raise RetryableMetricsException("No data for hour")
        if usage_data_start_local != chart_start:
            # pyemvue returns the API's ``firstUsageInstant`` as the start,
            # which drifts off the requested (QH-aligned) chart_start when
            # data is missing at the head of the window.  Storing it would
            # leave a misaligned data_start in the cache — treat as a
            # transient condition and let the caller retry with stale data.
            # If the same QH keeps being rejected, the head sample is
            # likely permanently missing: log at ERROR level (every N
            # rejections) and enqueue a one-time Telegram alert per QH key
            # so the resulting stall is observable instead of silent.
            # Stale entries for long-past windows are pruned first so the
            # tracker stays bounded: chart_start advances every QH, so a key
            # older than the fetch window can never be fetched again.
            cutoff = instant - MAX_FETCH_WINDOW
            with _drift_lock:
                for stale_key in list(_drift_rejections):
                    if stale_key[1] < cutoff:
                        del _drift_rejections[stale_key]
                key = (chan.channel_num, chart_start)
                count = _drift_rejections.get(key, 0) + 1
                _drift_rejections[key] = count
                escalated = (
                    count >= DRIFT_REJECTION_ALERT_AFTER
                    and count % DRIFT_REJECTION_ALERT_AFTER == 0
                )
                if escalated:
                    if count == DRIFT_REJECTION_ALERT_AFTER:
                        # First crossing: queue a one-time alert for this QH
                        # key (a (channel_num, chart_start) key is unique, so
                        # this fires at most once per stalled window).
                        _drift_alerts.append(
                            DriftAlert(
                                channel_num=chan.channel_num,
                                chart_start=chart_start,
                                data_start=usage_data_start_local,
                                count=count,
                            )
                        )
                    self.logger.error(
                        "get_chart_usage drift for QH starting %s: data_start "
                        "%s persistently rejected %d consecutive fetches; the "
                        "head of this window appears permanently missing",
                        chart_start,
                        usage_data_start_local,
                        count,
                    )
            self.logger.warning(
                "get_chart_usage returned data_start %s != requested "
                "chart_start %s; treating as transient",
                usage_data_start_local,
                chart_start,
            )
            raise RetryableMetricsException(
                f"data_start {usage_data_start_local} != chart_start {chart_start}"
            )
        with _drift_lock:
            _drift_rejections.pop((chan.channel_num, chart_start), None)
        self.metrics["api_response"]["get_chart_usage/" + str(chan.channel_num)] = (
            fetch_elapsed
        )
        return usage_data_local, usage_data_start_local, chan.channel_num

    def _populate_device(
        self,
        vdi: Any,
        chart_start: datetime,
        energy_cache: Optional["EnergyCache"] = None,
    ) -> Optional[_PopulationResult]:
        """Fetch and compute usage data for one device without mutating API objects.

        Args:
            vdi: The VDeviceUsageInfo object from pyemvue (read-only).
            chart_start: Start of the chart window.
            energy_cache: Optional merged sample cache for NBC computation.

        Returns:
            PopulationResult with computed per-device data, or None on error.
        """
        # Store cache reference for _compute_device_metrics to use for NBC.
        if energy_cache is not None:
            self.energy_cache = energy_cache
        for chan in vdi.channels:
            usage_data_start_local = chart_start  # safe default if fetch fails
            try:
                usage_data_local, usage_data_start_local, _ = (
                    self._fetch_channel_data(chan, chart_start, self.instant)
                )
            except (requests.exceptions.RequestException, IOError) as exc:
                self.logger.warning(
                    "error fetching device data: skipping %s (%s)",
                    vdi.device_name, exc,
                )
                return None

            chart_data = usage_data_local[-300:]

            return _PopulationResult(
                per_second_data=usage_data_local,
                chart_data=chart_data,
                nbc_seconds=usage_data_local,
                nbc_data_start=usage_data_start_local,
                nbc_sample_count=len(usage_data_local),
            )
        return None

    def populate_internal(
        self, chart_start: datetime, energy_cache: Optional["EnergyCache"] = None
    ) -> dict[int, _PopulationResult]:
        """Fetch recent data using second granularity to minimize lag.

        This is the internal implementation used by populate(). It handles
        the actual API calls and per-device population.

        Args:
            chart_start: Start of the fetch window (inclusive).
            energy_cache: Optional merged sample cache for NBC computation.

        Returns:
            Dict of gid -> _PopulationResult for each successfully populated device.
        """
        results: dict[int, _PopulationResult] = {}
        for vdi in self.device_info.values():
            self.logger.debug("device: %s", vdi)
            result = self._populate_device(vdi, chart_start, energy_cache)
            if result is not None:
                results[vdi.device_gid] = result
        return results



# Maintain backward compatibility by aliasing Metrics to HourlyProjection
Metrics = HourlyProjection
