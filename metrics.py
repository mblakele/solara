"""
Call Emporia VUE API and marshal predicted usage.
"""

import dataclasses
import threading

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import locale
import logging
from typing import Any, Callable, ClassVar, Optional

import requests
from pyemvue import PyEmVue
from pyemvue.enums import Scale, Unit

from util import (
    CustomJSONProvider,
    ceil_to_qh,
    compute_nbc_quarters,
    custom_json_default,
    is_debug,
    qh_seconds_remaining,
)
from energy_aggregator import EnergyDataAggregator

from config import cfg as _cfg


logger = logging.getLogger(__name__)

MAX_FETCH_WINDOW = timedelta(hours=1)


def cap_chart_start(chart_start: datetime, now: datetime) -> datetime:
    """Cap chart_start to prevent over-fetching after stale cache.

    If chart_start is more than 1 hour before *now*, return the current
    quarter-hour boundary.  Otherwise return chart_start unchanged.

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
    if now - chart_start > MAX_FETCH_WINDOW:
        return ceil_to_qh(now)
    return chart_start


def cap_fetch_window(start_time: datetime, now: datetime) -> datetime:
    """Cap a fetch start_time to prevent over-fetching after stale cache.

    If *start_time* is more than 1 hour before *now*, return the current
    quarter-hour boundary.  Otherwise return start_time unchanged.

    Used by the incremental fetch builder to guard against stale
    EnergyCache state that would otherwise request hours of 1-second
    data.

    Args:
        start_time: The proposed fetch window start time.
        now: The current time.

    Returns:
        A datetime no more than 1 hour before *now*, or *start_time*
        unchanged if it is already within the 1-hour window.
    """
    if now - start_time > MAX_FETCH_WINDOW:
        return ceil_to_qh(now)
    return start_time


def create_metrics(energy_cache: EnergyCache, now: datetime, logger: logging.Logger) -> dict[str, Any] | None:
    """Fetch metrics with incremental chart_start tracking via EnergyCache.

    On the first call, EnergyCache has no samples, so chart_start is set to
    3600 seconds ago (full hour of historical data). After that, chart_start
    advances to the most recent sample timestamp from the cache.

    Args:
        evergy_cache: instance of EnergyCache.
        now: current datetime in local timezone.
        logger: Logger instance.

    Returns:
        Metrics dict from HourlyProjection, or None on failure.
    """
    # First call: fetch up to four QH periods.
    # Subsequent calls: fetch incremental data from the last sample timestamp.
    logger.debug("create_metrics: last_sample_at %s", energy_cache.last_sample_at)
    chart_start = (
        ceil_to_qh(now - timedelta(seconds=3600))
        if energy_cache.last_sample_at is None
        else energy_cache.last_sample_at
    )

    hp = HourlyProjection(now, logger, energy_cache)
    hp.populate(chart_start)
    return hp.metrics




@dataclass
class _PopulationResult:
    """Intermediate results from populating one device — no mutation of API objects."""

    per_second_data: list[float]
    scales: dict[str, Any]
    chart_data: list[float]
    nbc_seconds: list[float]
    nbc_data_start: datetime
    nbc_sample_count: int = 0


@dataclass(frozen=True)
class _PredictionData:
    """Aggregated prediction values for a device."""

    value: float
    min_value: float
    max_value: float


@dataclass(frozen=True)
class _MinuteData:
    """Per-minute prediction and remaining time."""

    predicted: float
    minutes_remaining: float


@dataclass
class DeviceMetrics:
    """Computed metrics for one device, separate from raw pyemvue response."""

    gid: int = 0
    name: str = ""
    lag: timedelta = dataclasses.field(  # type: ignore[assignment]
        default_factory=timedelta, repr=False
    )
    per_second_data: list[float] = dataclasses.field(  # type: ignore[assignment]
        default_factory=list, repr=False
    )
    prediction: _PredictionData = dataclasses.field(  # type: ignore[assignment]
        default_factory=lambda: _PredictionData(value=0.0, min_value=0.0, max_value=0.0),
        repr=False,
    )
    minute_data: _MinuteData = dataclasses.field(  # type: ignore[assignment]
        default_factory=lambda: _MinuteData(predicted=0.0, minutes_remaining=0.0),
        repr=False,
    )
    scales: dict[str, Any] = dataclasses.field(  # type: ignore[assignment]
        default_factory=dict, repr=False
    )
    smoothing: dict[str, float] = dataclasses.field(  # type: ignore[assignment]
        default_factory=dict, repr=False
    )
    nbc: dict[str, Any] = dataclasses.field(  # type: ignore[assignment]
        default_factory=dict, repr=False
    )
    timezone: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON/template consumption.

        per_second_data is truncated to the latest 300 samples for compact output.
        """
        return {
            "gid": self.gid,
            "lag": self.lag,
            "name": self.name,
            "prediction": round(self.prediction.value, 14),
            "prediction_min": round(self.prediction.min_value, 14),
            "prediction_max": round(self.prediction.max_value, 14),
            "minute_predicted": round(self.minute_data.predicted, 14),
            "minutes_remaining": round(self.minute_data.minutes_remaining, 14),
            "scales": self.scales,
            "smoothing": {k: round(v, 14) for k, v in self.smoothing.items()},
            "timezone": self.timezone,
            "nbc": self.nbc,
            "per_second_data": self.per_second_data,
        }


def _build_incremental_fetch(
    energy_cache: "EnergyCache",
    vue: PyEmVue,
    device_gid: int,
    now: datetime,
) -> Callable[[], dict[str, Any] | None]:
    """Build a callable for incremental partial-range API fetches.

    Returns a zero-argument function that can be passed to
    ``energy_cache.get_or_fetch()``. The callable checks whether the cache
    already has samples and, if so, computes a partial-range API call that
    fetches only new data since the last sample. Old samples (older than
    3600s) are pruned after merging.

    On the first call (no existing samples), a full-range fetch is performed
    covering the current hour up to ``now``.

    Args:
        energy_cache: The EnergyCache instance storing per-second samples.
        vue: A PyEmVue client instance for API calls.
        device_gid: The group ID (device identifier) to fetch data for.
        now: Current time. Required.

    Returns:
        A callable that returns a dict with ``per_second_data`` and
        ``data_start``, or None on API error.
    """

    def fetcher() -> dict[str, Any] | None:
        if energy_cache.data_start is None or energy_cache.samples is None or len(
            energy_cache.samples
        ) == 0:
            start_time = ceil_to_qh(now)
        else:
            last_sample_idx = len(energy_cache.samples)
            start_time = energy_cache.data_start + timedelta(seconds=last_sample_idx)

        # Guard against stale cache: if the incremental window would be >1h,
        # fall back to a full-hour fetch to avoid API rejection.
        capped = cap_fetch_window(start_time, now)
        if capped != start_time:
            logger.debug(
                "[_build_incremental_fetch] incremental window %s-%s >1h, "
                "falling back to full-hour fetch",
                start_time,
                now,
            )
            start_time = capped

        try:
            usage_data, data_start = vue.get_chart_usage(
                device_gid,
                start_time,
                now,
                scale=Scale.SECOND.value,
                unit=Unit.KWH.value,
            )
            if not usage_data:
                return None
            return {"per_second_data": list(usage_data), "data_start": data_start}
        except (requests.exceptions.RequestException, IOError):
            logger.exception("error fetching incremental data for device %d", device_gid)
            return None

    return fetcher


class EnergyCache:
    """Unified cache for per-second energy samples with sliding-window semantics.

    Stores raw kWh/second data points in a time-ordered list keyed by device name
    (currently only one device is used). Supports incremental partial-range fetches:
    when new data arrives, old points older than 3600s are pruned and only the
    delta is fetched from pyemvue.

    Thread-safe via internal lock for concurrent access between Flask and
    LoadManager background threads.

    Attributes:
        _samples: Per-second data points, one float per second (kWh). Ordered by time.
        _data_start: Timestamp of the first sample in _samples.
        _last_fetch_at: When data was last fetched from API (for TTL/stale checks).
        _ttl_seconds: Maximum age of cached data before forcing a refresh.
        _sample_count: Number of samples in _samples (cached for incremental fetch).
        _last_sample_at: Timestamp of the last sample in _samples.
    """

    def __init__(self, ttl_seconds: int = 30) -> None:
        self._samples: list[float] | None = None
        self._data_start: datetime | None = None  # start time of first sample
        self._last_fetch_at: datetime | None = None
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        # Sample metadata for incremental fetch tracking.
        self._sample_count: int | None = None  # number of samples in _samples
        self._last_sample_at: datetime | None = None  # time of last sample in _samples
        # Full result dict from the most recent fetch (for non-incremental callers).
        self._data: dict[str, Any] | None = None

    @property
    def last_sample_at(self) -> datetime | None:
        """Timestamp of the most recent sample in the cache, or None if empty.

        Returns:
            datetime of the last sample (data_start + len(samples) - 1), or None.
        """
        return self._last_sample_at

    @property
    def samples(self) -> list[float] | None:
        """The list of per-second Wh samples, or None if empty.

        Returns:
            List of float Wh values, or None.
        """
        return self._samples

    @property
    def data_start(self) -> datetime | None:
        """Timestamp of the first sample in the cache, or None if empty.

        Returns:
            datetime of the first sample, or None.
        """
        return self._data_start

    def is_valid(self, now: datetime) -> bool:
        """Check if cache has non-expired data.

        Args:
            now: Current time for TTL check. Required.

        Returns:
            True if cache has data and it hasn't expired.
        """
        with self._lock:
            if self._samples is None or len(self._samples) == 0:
                return False
            if self._last_fetch_at is None:
                return False
            elapsed = now - self._last_fetch_at
            return elapsed.total_seconds() < self._ttl_seconds

    @staticmethod
    def merge_incremental(data_start: datetime,
                          result_data_start: datetime,
                          old_samples: [],
                          new_samples: []
                          ) -> [] | None:
        """
        TODO docstring
        """
        # Compute the cache's effective time range from its
        # own _data_start so overlap detection is anchored
        # to real sample positions.
        cache_start_time = data_start
        cache_end_time = data_start + timedelta(seconds=len(old_samples) - 1)

        # Number of new samples strictly before / after the cache.
        # Initialized here so they're always defined for the
        # data_start update below.
        after_count = 0

        # Standard overlap detection:
        # New samples should never arrive before the cache start.
        assert(result_data_start >= cache_start_time)

        # New samples after the cache end.
        # Where the new samples end (from API's data_start).
        new_end_time = result_data_start + timedelta(seconds=len(new_samples) - 1)
        if new_end_time > cache_end_time:
            after_count = int((new_end_time - cache_end_time).total_seconds())
            # Don't double-count: after_count can't exceed remaining samples.
            # TODO assert that these counts match exactly?
            after_count = min(after_count, len(new_samples))
            # TODO assert that any overlap has identical values?

        # Build merged samples in time order:
        #   before_samples + existing + after_samples
        after_samples = list(new_samples[len(new_samples) - after_count:])

        return list(old_samples) + after_samples


    def get_or_fetch(
        self, fetch_func: Callable[[], dict[str, Any] | None], now: datetime, force: bool = False
    ) -> tuple[dict[str, Any] | None, bool]:
        """Return (metrics_dict_or_none, was_fresh).

        If cache is valid and force=False: return cached data with was_fresh=False.
        Otherwise calls fetch_func() (which should do an incremental or full API call),
        stores the result, and returns was_fresh=True.

        The fetch_func may return either:
          - A full metrics dict (e.g. HourlyProjection.metrics) — stored as-is in
            ``_data`` and returned directly to the caller.
          - An incremental dict with "per_second_data" and "data_start" keys — the
            per-second samples are merged into ``_samples`` for use by callers that
            need raw data (e.g. NBCReader).

        Args:
            fetch_func: Callable that returns fresh data dict.
            now: current datetime.
            force: When True, bypass cache and always fetch.

        Returns:
            Tuple of (metrics_dict_or_none, was_fresh).
        """
        with self._lock:
            # Check if cache is valid (non-expired data exists).
            if not force and self._is_valid_unlocked(now):
                return self._build_result(), False

            # Fetch fresh data.
            result = fetch_func()

            if result is not None:
                # Store the full result dict for non-incremental callers.
                self._data = result

                new_samples = result.get("per_second_data", [])

                # When data comes from nested devices (full metrics dict path,
                # e.g. HourlyProjection.metrics), extract per_second_data from
                # devices so the merge logic below populates self._samples.
                if not new_samples and "devices" in result:
                    new_samples = [
                        point
                        for device in result["devices"]
                        for point in device.get("per_second_data", [])
                    ]

                # Merge with existing samples (incremental fetch path).
                if self._samples is not None and len(self._samples) > 0:
                    result_data_start = result.get("data_start")
                    if (self._last_sample_at is not None
                        and self._data_start is not None
                        and result_data_start is not None):
                        merged_samples = self.merge_incremental(
                            self._data_start,
                            result_data_start,
                            self._samples,
                            new_samples)
                        self._samples = merged_samples
                elif new_samples:
                    self._samples = list(new_samples)

                # Update data_start if this looks like an initial fetch.
                if self._samples and "data_start" in result:
                    if self._data_start is None:
                        self._data_start = result["data_start"]

                if self._samples:
                    cutoff = ceil_to_qh(now - timedelta(seconds=3600))
                    # Determine the time of each sample to know which are old.
                    if self._data_start is not None:
                        # Compute how many samples are before the cutoff.
                        old_count = 0
                        for i, _ in enumerate(self._samples):
                            sample_time = self._data_start + timedelta(seconds=i)
                            if sample_time < cutoff:
                                old_count += 1
                            else:
                                break
                        if old_count > 0:
                            self._samples = self._samples[old_count:]
                            if self._data_start is not None:
                                self._data_start = (
                                    self._data_start + timedelta(seconds=old_count)
                                )

                # Update sample metadata.
                if self._samples:
                    self._sample_count = len(self._samples)
                    if self._data_start is not None:
                        self._last_sample_at = (
                            self._data_start + timedelta(seconds=len(self._samples) - 1)
                        )

                psd = result.get("per_second_data")
                if not psd:
                    # Fall back to counting per_second_data from devices
                    # (full metrics dict path, e.g. HourlyProjection.metrics).
                    devices = result.get("devices", [])
                    if devices:
                        psd = [
                            point
                            for device in devices
                            for point in device.get("per_second_data", [])
                        ]

                if psd:
                    logger.debug(
                        "EnergyCache fetched %d data points (%s: %d samples) at %s",
                        len(psd),
                        self._data_start,
                        len(self._samples or []),
                        now,
                    )

                self._last_fetch_at = now
            else:
                result = None  # Ensure we store None on failure.

            return (result, True) if result is not None else (None, True)

    def _is_valid_unlocked(self, now: datetime) -> bool:
        """Check if cache has non-expired data (caller must hold lock).

        Args:
            now: Current time for TTL check. Required.

        Returns:
            True if cache has data and it hasn't expired.
        """
        # Cache is valid if we have either raw samples or a full result dict.
        has_data = (
            self._samples is not None and len(self._samples) > 0
        ) or (self._data is not None)
        if not has_data:
            return False
        if self._last_fetch_at is None:
            return False
        elapsed = now - self._last_fetch_at
        return elapsed.total_seconds() < self._ttl_seconds

    def _build_result(self) -> dict[str, Any] | None:
        """Build the result dict from cached data (caller must hold lock).

        Returns the full result dict stored by the most recent fetch.
        Falls back to building a dict from raw samples when no full result is cached.

        Returns:
            Cached metrics dict, or a dict with per_second_data and data_start
            built from raw samples, or None if no cached data exists.
        """
        # Return the full result dict if available (non-incremental path).
        if self._data is not None:
            return self._data

        # Fall back to building from raw samples (incremental path).
        if self._samples is None:
            return None

        result: dict[str, Any] = {
            "per_second_data": self._samples,
        }

        if self._data_start is not None:
            result["data_start"] = self._data_start

        return result

    def get_current_qh(self, now: datetime) -> dict[str, Any] | None:
        """Extract current incomplete QH prediction from cached samples.

        Computes NBC quarters using clock-boundary alignment (QH1 = most recent
        15-min window) and returns the same structure that
        NBCCache.get_or_fetch() would return: {qh_name, predicted_wh,
        seconds_remaining}.

        ``seconds_remaining`` is derived from wall-clock time so it stays
        monotonic across cache refreshes even when the sample count fluctuates.

        Args:
            now: Current time for QH boundary computation. Required.

        Returns:
            Dict with QH prediction info or None if no cached data.
        """
        with self._lock:
            samples = self._samples

        if samples is None:
            return None

        samples_len = len(samples)
        if samples_len == 0:
            return None

        # Required: data_start present and aligned to a QH boundary
        assert self._data_start is not None
        assert self._data_start == ceil_to_qh(self._data_start)

        #self.logger.debug("get_current_qh len %d", len(samples))
        nbc = compute_nbc_quarters(samples)

        # Find QH1 (most recent window).
        qh1_data = nbc.get("QH1")

        if qh1_data is None:
            # No data at all — return the first non-None QH as fallback.
            for qh in ("QH2", "QH3", "QH4"):
                if nbc.get(qh) is not None:
                    qh_data = nbc[qh]
                    return {
                        "qh_name": qh,
                        "predicted_wh": qh_data.get("predicted_wh", 0),
                        "seconds_remaining": qh_data.get("remaining_seconds", 0),
                        "data_start": self._data_start,
                    }
            return None

        seconds_remaining = qh_seconds_remaining(now)
        predicted_wh = qh1_data.get("predicted_wh", qh1_data.get("wh", 0))
        return {
            "qh_name": "QH1",
            "predicted_wh": predicted_wh,
            "seconds_remaining": seconds_remaining,
            "data_start": self._data_start,
        }


    def invalidate(self) -> None:
        """Clear the cache."""
        with self._lock:
            self._samples = None
            self._data_start = None
            self._last_fetch_at = None
            self._sample_count = None
            self._last_sample_at = None
            self._data = None



class VueAuthenticationError(Exception):
    """Raised when Vue authentication fails."""


class RetryableMetricsException(Exception):
    """Signal that the Emporia VUE API responded with a server error and the caller should retry."""

    def __init__(self, message, *args):
        self.message = message
        self.instant = datetime.now(timezone.utc) # ok
        super().__init__(message, *args)


class MetricsBase:
    """
    Base class handling PyEmVue connection and authentication only.

    ``device_info``, ``vue``, and ``vue_auth`` are intentional class-level
    caches shared across instances so that repeated short-lived instantiations
    (e.g., one per request) reuse the same authenticated PyEmVue session
    without re-logging in on every call.
    """

    device_info: ClassVar[dict[int, Any]] = {}
    json: ClassVar[type] = CustomJSONProvider
    vue: ClassVar[PyEmVue] = PyEmVue()
    vue_auth: ClassVar[dict[str, Any]] = {}
    vue_keys: ClassVar[str] = ".vue-keys.json"

    def __init__(self, logger_next: Optional[logging.Logger] = None) -> None:
        self.logger = logger_next or logger
        self.vue_init()
        self.get_device_info()

    def vue_init(self) -> None:
        """
        Initialize access to Emporia VUE API.
        Prefer stored authentication token,
        falling back on username and password.
        """

        if not self.vue:
            return

        self.logger.debug({"keys": self.vue_keys})
        try:
            encoding = locale.getpreferredencoding()
            vkf = open(self.vue_keys, encoding=encoding)
            with vkf:
                vkf_data = json.load(vkf)
                login_ok = self.vue.login(
                    id_token=vkf_data["id_token"],
                    access_token=vkf_data["access_token"],
                    refresh_token=vkf_data["refresh_token"],
                    token_storage_file=self.vue_keys,
                )
        except (requests.exceptions.RequestException, IOError):
            self.logger.exception("keys failed: will use password")
            try:
                login_ok = self.vue.login(
                    username=_cfg.vue_username,
                    password=_cfg.vue_password,
                    token_storage_file=self.vue_keys,
                )
            except Exception as inner_ex:
                raise VueAuthenticationError(
                    "Vue authentication failed: check credentials"
                ) from inner_ex

        if login_ok:
            self.logger.debug("login ok")
        else:
            # Token login returned False — fall back to password auth.
            self.logger.debug("token login failed, trying password")
            try:
                login_ok = self.vue.login(
                    username=_cfg.vue_username,
                    password=_cfg.vue_password,
                    token_storage_file=self.vue_keys,
                )
            except Exception as inner_ex:
                raise VueAuthenticationError(
                    "Vue authentication failed: check credentials"
                ) from inner_ex

        if not login_ok:
            self.logger.error("login failed")
            raise VueAuthenticationError(
                "Vue authentication failed: check credentials"
            )

        self.vue_auth["last"] = datetime.now(timezone.utc) # ok

    def get_device_info(self) -> None:
        """
        Wrapper for vue get_devices,
        filtering results for ZIG001 devices.
        """
        rt_start = datetime.now(timezone.utc) # ok
        age_limit = timedelta(hours=24)
        self.logger.debug(
            {"device_info_len": len(self.device_info), "vue_auth": self.vue_auth}
        )
        if len(self.device_info) > 0 and "last" in self.vue_auth:
            age = rt_start - self.vue_auth["last"]
            self.logger.debug({"age": age})
            if age < age_limit:
                self.logger.debug({"device_info": self.device_info})
                return

        try:
            devices = [self.vue.get_devices()[-1]]
        except requests.exceptions.HTTPError as ex:
            if ex.response is not None and ex.response.status_code == 401:
                self.logger.exception("invalidating auth tokens")
                self.vue.auth = None
            else:
                self.logger.exception(ex)
            raise RetryableMetricsException("get_devices failed") from ex

        for vdi in devices:
            self.logger.debug(
                "device %s, connected %s, model %s, channels %d",
                vdi.device_gid,
                vdi.connected,
                vdi.model,
                len(vdi.channels),
            )
            if not vdi.connected:
                continue
            if not vdi.model == "ZIG001":
                continue
            if not len(vdi.channels) > 0:
                continue
            if not vdi.device_gid in self.device_info:
                self.device_info[vdi.device_gid] = vdi
                break


class HourlyProjection(MetricsBase):
    """
    Hourly prediction behavior.
    Maintains backward compatibility with original Metrics class.
    """

    def __init__(
        self,
        instant: datetime,
        logger_next: Optional[logging.Logger] = None,
        energy_cache: Optional["EnergyCache"] = None,
    ) -> None:
        self.metrics: dict[str, Any] = {
            "api_response": {},
            "debug": is_debug(),
            "devices": [],
        }

        super().__init__(logger_next)

        self.instant = instant
        self.metrics["instant"] = self.instant
        self.energy_cache = energy_cache  # Merged samples for NBC computation

    def populate(self, chart_start: datetime) -> dict[int, dict[str, Any]]:
        """Fetch recent data using second granularity to minimize lag.

        The caller must compute chart_start. On the first call, use
        now - 3600 seconds, aligned to QH boundary, for up to a full hour
        of historical data. On subsequent calls, use the most recent sample
        timestamp from EnergyCache to fetch only incremental new data.

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

        self.metrics["api_response"]["total"] = sum(
            self.metrics["api_response"].values(), timedelta()
        )

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
        # update _data_start and _last_sample_at.  Without this key the
        # get_or_fetch merge block silently skips the _last_sample_at update,
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
            lag_td: timedelta = pred_result.get("lag", timedelta(0))
            self.metrics["_data_lag_secs"] = lag_td.total_seconds()

        return predictions

    def _fetch_channel_data(self, chan, chart_start, instant):
        """
        Fetch channel usage data from the VUE API and validate it.

        Returns a tuple of (usage_data_local, usage_data_start_local, channel_num).
        Raises RetryableMetricsException if no valid data is returned.
        """
        scale = Scale.SECOND.value
        usage_data_local, usage_data_start_local = self.vue.get_chart_usage(
            chan,
            chart_start,
            instant,
            scale=scale,
            unit=Unit.KWH.value,
        )
        if (
            usage_data_start_local is None
            or usage_data_local is None
            or len(usage_data_local) < 1
            or usage_data_local[0] is None
        ):
            self.logger.debug({"usage_data": usage_data_local})
            raise RetryableMetricsException("No data for hour")
        self.metrics["api_response"]["get_chart_usage/" + str(chan.channel_num)] = (
            datetime.now(timezone.utc) - chart_start # ok
        )
        return usage_data_local, usage_data_start_local, chan.channel_num

    def _process_offset_scales(
        self, scales: dict[str, Any], usage_data_local: list[float], usage_data_end: datetime
    ) -> list[float]:
        """Process minute-scale offset data (1MIN–10MIN) and return chart_data.

        Computes usage for each minute scale from the tail of the dataset
        and stores results in the provided scales dict. Returns the last 300
        data points as chart_data without mutating any API objects.

        Args:
            scales: Dict to populate with minute-scale entries (mutated in-place).
            usage_data_local: Per-second usage data for the current hour.
            usage_data_end: End time of the usage data window.

        Returns:
            Last 300 data points as chart_data.
        """
        usage_data_len = len(usage_data_local)
        usage_minutes = max(1, min(10, usage_data_end.minute))
        self.logger.debug(
            {
                "usage_data": usage_data_local[:3],
                "usage_data_start": usage_data_end - timedelta(seconds=usage_data_len),
                "usage_data_len": usage_data_len,
                "usage_minutes": usage_minutes,
            }
        )
        for usm in range(1, 1 + usage_minutes):
            uss = 60 * usm
            scale = str(usm) + "MIN"
            offset_data = usage_data_local[-uss:]
            offset_start = usage_data_end - timedelta(minutes=usm)
            scales[scale] = self.data_for_scale(offset_data, offset_start, scale)
        return usage_data_local[-300:]

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

    def populate_scale(
        self, dig: Any, scale: str, data_start: datetime, data: list[float]
    ) -> None:
        """
        Populate N seconds of usage data for a scale
        of 1H or M minutes.
        """
        #self.logger.debug(
        #    {"dig": dig, "scale": scale, "data_start": data_start, "data": data[:3]}
        #)
        if not hasattr(dig, "scales"):
            dig.scales = {}
        if scale not in dig.scales:
            dig.scales[scale] = {}
        dig.scales[scale] = self.data_for_scale(data, data_start, scale)

    @staticmethod
    def data_for_scale(
        data: list[float], data_start: datetime, scale: str
    ) -> dict[str, Any]:
        """Calculate usage statistics for a given scale (hour or minutes).

        Args:
            data: kWh values per second/minute. Negative values indicate solar export.
            data_start: Start time of the first data point.
            scale: Scale identifier ('1H', '1MIN'-'10MIN').

        Returns:
            Dict with keys: usage (Wh), seconds, instant, and optionally
                data/data_len/data_start if DEBUG is enabled.
        """
        dsi: dict[str, object] = {}
        data_len = len(data)

        if is_debug():
            dsi["data"] = data[:3]
            dsi["data_len"] = data_len
            dsi["data_start"] = data_start

        usage = 1000.0 * sum(data)
        if not scale == "1H" and data_len != 0:
            usage = usage * 60.0 / data_len

        dsi["scale"] = scale
        dsi["instant"] = data_start + timedelta(seconds=data_len)
        dsi["seconds"] = data_len
        dsi["usage"] = usage
        return dsi

    def predict(
        self, population: dict[int, _PopulationResult]
    ) -> dict[int, dict[str, Any]]:
        """Predict consumption or surplus at end of current hour.

        Uses the minute-scale usage rate to extrapolate remaining
        consumption for the current hour, then computes min/max bounds
        across all available minute scales (1MIN–10MIN).

        Args:
            population: Results from populate(), mapping gid -> PopulationResult.

        Returns:
            Dict of gid -> prediction results for each device.
        """
        predictions: dict[int, dict[str, Any]] = {}
        for gid, pop_result in population.items():
            pred_result = self._predict_device(pop_result.scales)
            predictions[gid] = pred_result
        return predictions

    def _compute_nbc(
        self,
        usage_data_local: list[float]
    ) -> dict[str, Any]:
        """Compute NBC values for each quarter hour in the current hour.

        Delegates to ``compute_nbc_quarters`` in util. Quarter boundaries are
        determined by the number of observed data points (``n``), not by
        wall-clock time, so API lag cannot cause two quarters to appear
        incomplete simultaneously.

        Args:
            usage_data_local: Per-second kWh data for the current hour.

        Returns:
            Dict with keys QH1-QH4, each containing NBC metrics or None if
            the quarter has not yet started.
        """
        result = compute_nbc_quarters(usage_data_local)
        self.logger.debug("_compute_nbc len %d (%s)", len(usage_data_local), result)
        return result

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
            PopulationResult with computed scales, or None on error.
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
            except (requests.exceptions.RequestException, IOError):
                self.logger.exception(
                    "error fetching device data: skipping %s", vdi.device_name
                )
                return None

            scales: dict[str, Any] = {}
            scales[Scale.HOUR.value] = self.data_for_scale(
                usage_data_local, usage_data_start_local, Scale.HOUR.value
            )

            usage_data_end = usage_data_start_local + timedelta(seconds=len(usage_data_local))
            chart_data = self._process_offset_scales(scales, usage_data_local, usage_data_end)

            return _PopulationResult(
                per_second_data=usage_data_local,
                scales=scales,
                chart_data=chart_data,
                nbc_seconds=usage_data_local,
                nbc_data_start=usage_data_start_local,
                nbc_sample_count=len(usage_data_local),
            )
        return None

    def _predict_device(self, scales: dict[str, Any]) -> dict[str, Any]:
        """Compute prediction and smoothing for one device from its scales.

        Args:
            scales: Computed scale entries (1H, 1MIN-10MIN) from population.

        Returns:
            Dict with prediction, min/max bounds, smoothing, lag, etc.
        """
        hour_next = (
            self.instant
            + timedelta(hours=1)
            - timedelta(
                minutes=self.instant.minute,
                seconds=self.instant.second,
                microseconds=self.instant.microsecond,
            )
        )
        hour = scales[Scale.HOUR.value]
        seconds_remaining_hour = (hour_next - hour["instant"]).total_seconds() # hour, not NBC QH period

        minute_predicted = (
            seconds_remaining_hour * scales[Scale.MINUTE.value]["usage"] / 60.0
        )
        prediction = hour["usage"] + minute_predicted

        smoothing: dict[str, float] = {}
        prediction_min = prediction
        prediction_max = prediction
        for scale in scales.keys():
            if not scale.endswith("MIN"):
                continue
            sval = hour["usage"] + (
                seconds_remaining_hour * scales[scale]["usage"] / 60.0
            )
            prediction_min = min(sval, prediction_min)
            prediction_max = max(sval, prediction_max)
            smoothing[scale] = sval

        lag = (
            self.instant - hour["instant"]
            if hour["instant"] < self.instant
            else timedelta(0)
        )

        return {
            "lag": lag,
            "minute_predicted": minute_predicted,
            "prediction": prediction,
            "prediction_min": prediction_min,
            "prediction_max": prediction_max,
            "seconds_remaining": seconds_remaining_hour,
            "smoothing": smoothing,
        }

    def _compute_device_metrics(
        self,
        vdi: Any,
        pop_result: _PopulationResult,
        pred_result: dict[str, Any],
    ) -> DeviceMetrics:
        """Build a DeviceMetrics from population and prediction results.

        This is a pure constructor that takes raw inputs and returns computed
        output without mutating any input data.

        Args:
            vdi: The VDeviceUsageInfo object from pyemvue (read-only metadata).
            pop_result: Intermediate data from _populate_device.
            pred_result: Computed predictions from _predict_device.

        Returns:
            DeviceMetrics instance with all derived fields.
        """
        nbc_seconds = pop_result.nbc_seconds
        pop_data_start = pop_result.nbc_data_start
        # Use merged cache samples for NBC computation when available.
        # This ensures _compute_nbc sees the full hour of data rather than
        # only the incremental delta from the API call.
        if nbc_seconds is not None and pop_data_start is not None and self.energy_cache is not None:
            cache_samples = self.energy_cache.samples
            if cache_samples:
                nbc_seconds = self.energy_cache.merge_incremental(
                    self.energy_cache._data_start,
                    pop_data_start,
                    cache_samples,
                    nbc_seconds)

        nbc_result = self._compute_nbc(nbc_seconds)

        return DeviceMetrics(
            gid=vdi.device_gid,
            name=vdi.device_name,
            lag=pred_result["lag"],
            per_second_data=pop_result.per_second_data,
            prediction=_PredictionData(
                value=pred_result["prediction"],
                min_value=pred_result["prediction_min"],
                max_value=pred_result["prediction_max"],
            ),
            minute_data=_MinuteData(
                predicted=pred_result["minute_predicted"],
                minutes_remaining=pred_result["seconds_remaining"] / 60.0,
            ),
            scales=pop_result.scales,
            smoothing=pred_result["smoothing"],
            nbc=nbc_result,
            timezone=getattr(vdi, "time_zone", None) or "",
        )


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
    ) -> None:
        super().__init__(logger_next)

        self.start_date = start_date
        self.end_date = end_date
        self.tou_result: Optional[dict[str, float]] = None
        self.nbc_result: Optional[float] = None

        self.fetch_usage_data()
        if is_debug():
            filename = (
                datetime.now().isoformat() # ok
                + f"_{start_date}_{end_date if end_date else 'None'}_"
            )
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(self.usage_data_list, f, default=custom_json_default)
        self.aggregate_tou()

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
        entire reporting period.
        """
        combined_buckets: dict[str, float] = {
            "total": 0.0,
            "peak": 0.0,
            "part_peak": 0.0,
            "off_peak": 0.0,
        }

        nbc_total_wh = 0.0

        for data_chunk in self.usage_data_list:
            chunk_buckets = EnergyDataAggregator.aggregate_from_15min(
                data_chunk["start"], data_chunk["data"]
            )

            for bucket in combined_buckets:
                combined_buckets[bucket] += chunk_buckets[bucket]

            # Sum positive 15-min periods only (imports); negatives are exports, ignored
            for usage_kwh in data_chunk["data"]:
                if usage_kwh is not None and usage_kwh > 0:
                    nbc_total_wh += usage_kwh * 1000.0

        self.tou_result = combined_buckets
        self.nbc_result = nbc_total_wh


# Maintain backward compatibility by aliasing Metrics to HourlyProjection
Metrics = HourlyProjection
