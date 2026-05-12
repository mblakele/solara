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

import pytz
import requests
from pyemvue import PyEmVue
from pyemvue.enums import Scale, Unit

from util import CustomJSONProvider, compute_nbc_quarters, custom_json_default, is_debug
from energy_aggregator import EnergyDataAggregator

from config import cfg as _cfg


logger = logging.getLogger(__name__)




@dataclass
class _PopulationResult:
    """Intermediate results from populating one device — no mutation of API objects."""

    per_second_data: list[float]
    scales: dict[str, Any]
    chart_data: list[float]
    nbc_seconds: list[float]
    nbc_data_start: datetime
    prev_hour_data: list[float] = dataclasses.field(default_factory=list)


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
    clock_boundary_nbc: dict[str, Any] = dataclasses.field(  # type: ignore[assignment]
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
            "clock_boundary_nbc": self.clock_boundary_nbc,
            "per_second_data": self.per_second_data[-300:],
        }


class MetricsCache:
    """Cache for HourlyProjection metrics data.

    Caches the full metrics dict for a configurable TTL. Callers get either
    a fresh fetch or a cached copy depending on TTL expiry. The fetch
    timestamp is stored in the returned data under the ``_fetched_at`` key
    so downstream consumers can distinguish real API fetch time from cache
    hit time.
    """

    def __init__(self, ttl_seconds: int = 30) -> None:
        self._data: dict[str, Any] | None = None
        self._fetched_at: datetime | None = None
        self._ttl = timedelta(seconds=ttl_seconds)

    def get_or_fetch(
        self,
        fetch_func: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        """Return (metrics_data, was_fresh).

        Returns cached data if not expired. Otherwise calls fetch_func and
        caches the result.

        Args:
            fetch_func: Callable that returns a fresh metrics dict.

        Returns:
            Tuple of (metrics_data, was_fresh). ``was_fresh`` is True when
            fetch_func was actually called.
        """
        now = datetime.now(timezone.utc)
        if (
            self._data is not None
            and self._fetched_at is not None
            and (now - self._fetched_at) < self._ttl
        ):
            return self._data, False

        fresh = fetch_func()
        fresh["_fetched_at"] = now
        self._data = fresh
        self._fetched_at = now
        return fresh, True

    def invalidate(self) -> None:
        """Clear the cache."""
        self._data = None
        self._fetched_at = None



def _build_incremental_fetch(
    energy_cache: "EnergyCache",
    vue: PyEmVue,
    device_gid: int,
    now: datetime | None = None,
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
        now: Current time. Defaults to ``datetime.now(timezone.utc)``.

    Returns:
        A callable that returns a dict with ``per_second_data`` and
        ``data_start``, or None on API error.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    def fetcher() -> dict[str, Any] | None:
        """Fetch incremental per-second data and merge into cache.

        Returns:
            Dict with merged samples on success, None on error.
        """
        # Determine the start time for this fetch.
        if energy_cache._samples is None or len(energy_cache._samples) == 0:  # pylint: disable=W0212
            # First fetch — get data for the current hour.
            chart_start = now.replace(minute=0, second=0, microsecond=0)
            start_time = chart_start
        else:
            # Incremental fetch — start from the last sample time.
            if energy_cache._data_start is None:  # pylint: disable=W0212
                # Should not happen, but fall back to full fetch.
                chart_start = now.replace(minute=0, second=0, microsecond=0)
                start_time = chart_start
            else:
                last_sample_idx = len(energy_cache._samples) - 1  # pylint: disable=W0212
                start_time = energy_cache._data_start + timedelta(  # pylint: disable=W0212
                    seconds=last_sample_idx
                )

        try:
            usage_data, data_start = vue.get_chart_usage(
                device_gid,
                start_time,
                now,
                scale=Scale.SECOND.value,
                unit=Unit.KWH.value,
            )

            if usage_data is None or len(usage_data) == 0:
                return None

            # Return dict compatible with EnergyCache.get_or_fetch().
            return {
                "per_second_data": list(usage_data),
                "data_start": data_start,
            }

        except (requests.exceptions.RequestException, IOError):
            logger.exception(
                "error fetching incremental data for device %d", device_gid
            )
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

    def is_valid(self, now: datetime | None = None) -> bool:
        """Check if cache has non-expired data.

        Args:
            now: Current time for TTL check. Defaults to datetime.now(timezone.utc).

        Returns:
            True if cache has data and it hasn't expired.
        """
        with self._lock:
            if now is None:
                now = datetime.now(timezone.utc)
            if self._samples is None or len(self._samples) == 0:
                return False
            if self._last_fetch_at is None:
                return False
            elapsed = now - self._last_fetch_at
            return elapsed.total_seconds() < self._ttl_seconds

    def get_or_fetch(
        self, fetch_func: Callable[[], dict[str, Any] | None], force: bool = False
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
            force: When True, bypass cache and always fetch.

        Returns:
            Tuple of (metrics_dict_or_none, was_fresh).
        """
        with self._lock:
            now = datetime.now(timezone.utc)

            # Check if cache is valid (non-expired data exists).
            if not force and self._is_valid_unlocked(now):
                return self._build_result(), False

            # Fetch fresh data.
            result = fetch_func()

            if result is not None:
                # Store the full result dict for non-incremental callers.
                self._data = result

                new_samples = result.get("per_second_data", [])

                # Merge with existing samples (incremental fetch path).
                if self._samples is not None and len(self._samples) > 0:
                    # Append new samples to existing ones.
                    self._samples = list(self._samples) + list(new_samples)

                elif new_samples:
                    self._samples = list(new_samples)

                # Update data_start if we have samples.
                if self._samples and "data_start" in result:
                    # If we already have samples, compute the true start time.
                    if self._data_start is not None:
                        # New samples are appended, so start time doesn't change.
                        pass
                    else:
                        self._data_start = result["data_start"]

                # Prune old samples older than 3600s from now.
                if self._samples:
                    cutoff = now - timedelta(seconds=3600)
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
                        "EnergyCache fetched %d data points (cache now has %d samples)",
                        len(psd),
                        len(self._samples or []),
                    )

                self._last_fetch_at = now
            else:
                result = None  # Ensure we store None on failure.

            return (result, True) if result is not None else (None, True)

    def _is_valid_unlocked(self, now: datetime | None = None) -> bool:
        """Check if cache has non-expired data (caller must hold lock).

        Args:
            now: Current time for TTL check. Defaults to datetime.now(timezone.utc).

        Returns:
            True if cache has data and it hasn't expired.
        """
        if now is None:
            now = datetime.now(timezone.utc)
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

    def get_current_qh(self) -> dict[str, Any] | None:
        """Extract current incomplete QH prediction from cached samples.

        Computes NBC quarters on demand and returns the same structure that
        NBCCache.get_or_fetch() would return: {qh_name, predicted_wh, seconds_remaining}.

        Returns:
            Dict with QH prediction info or None if no cached data.
        """
        with self._lock:
            samples = self._samples

        if samples is None or len(samples) == 0:
            return None

        # Determine how many seconds of data we have.
        n = len(samples)

        # Compute NBC quarters from raw samples.
        data_start = self._data_start if self._data_start else datetime.now(timezone.utc)
        nbc = compute_nbc_quarters(samples, min(n, len(samples)))

        # Find the first incomplete QH.
        qh_order = ["QH1", "QH2", "QH3", "QH4"]
        for qh_name in qh_order:
            qh_data = nbc.get(qh_name)
            if qh_data is None:
                continue
            if not qh_data.get("complete", True):
                return {
                    "qh_name": qh_name,
                    "predicted_wh": qh_data.get("predicted_wh", 0),
                    "seconds_remaining": qh_data.get("remaining_seconds", 900 - n % 900),
                    "data_start": data_start,
                }

        # All quarters complete — return the last one.
        for qh_name in reversed(qh_order):
            qh_data = nbc.get(qh_name)
            if qh_data is not None and qh_data.get("complete", False):
                return {
                    "qh_name": qh_name,
                    "predicted_wh": qh_data.get("wh", 0),
                    "seconds_remaining": 0,
                    "data_start": data_start,
                }

        return None

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
        self.instant = datetime.now(timezone.utc)
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

        self.vue_auth["last"] = datetime.now(timezone.utc)

    def get_device_info(self) -> None:
        """
        Wrapper for vue get_devices,
        filtering results for ZIG001 devices.
        """
        rt_start = datetime.now(timezone.utc)
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

    def __init__(self, logger_next: Optional[logging.Logger] = None) -> None:
        self.metrics: dict[str, Any] = {
            "api_response": {},
            "debug": is_debug(),
            "devices": [],
        }

        super().__init__(logger_next)

        self.instant = datetime.now(pytz.timezone(_cfg.timezone))
        self.metrics["instant"] = self.instant

        # Fetch usage data without mutating device_info
        population = self.populate()

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

        # Compute overall API lag from the first device's prediction.
        # This represents how far behind the most recent data point is
        # relative to when metrics were computed (self.instant).
        if predictions:
            first_gid = next(iter(predictions))
            pred_result = predictions[first_gid]
            lag_td: timedelta = pred_result.get("lag", timedelta(0))
            self.metrics["_data_lag_secs"] = lag_td.total_seconds()

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
            datetime.now(timezone.utc) - chart_start
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

    def populate(self) -> dict[int, _PopulationResult]:
        """Fetch recent data using second granularity to minimize lag.

        Returns a dict mapping device gid to PopulationResult without mutating
        the pyemvue API objects in device_info.

        Returns:
            Dict of gid -> _PopulationResult for each successfully populated device.
        """
        chart_start = self.instant - timedelta(
            minutes=self.instant.minute,
            seconds=self.instant.second,
            microseconds=self.instant.microsecond,
        )
        results: dict[int, _PopulationResult] = {}
        for vdi in self.device_info.values():
            self.logger.debug("device: %s", vdi)
            result = self._populate_device(vdi, chart_start)
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
        usage_data_local: list[float],
        usage_data_start_local: datetime,
        device_time_zone: str | None = None,  # pylint: disable=unused-argument
    ) -> dict[str, Any]:
        """Compute NBC values for each quarter hour in the current hour.

        Delegates to ``compute_nbc_quarters`` in util. Quarter boundaries are
        determined by the number of observed data points (``n``), not by
        wall-clock time, so API lag cannot cause two quarters to appear
        incomplete simultaneously.

        Args:
            usage_data_local: Per-second kWh data for the current hour.
            usage_data_start_local: Start time of the first data point.
            device_time_zone: Unused; kept for call-site compatibility.

        Returns:
            Dict with keys QH1-QH4, each containing NBC metrics or None if
            the quarter has not yet started.
        """
        elapsed = self.instant - usage_data_start_local
        n = max(0, int(elapsed.total_seconds()))
        n = min(n, len(usage_data_local))
        return compute_nbc_quarters(usage_data_local, n)

    def _populate_device(
        self, vdi: Any, chart_start: datetime
    ) -> Optional[_PopulationResult]:
        """Fetch and compute usage data for one device without mutating API objects.

        Args:
            vdi: The VDeviceUsageInfo object from pyemvue (read-only).
            chart_start: Start of the chart window.

        Returns:
            PopulationResult with computed scales, or None on error.
        """
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

            usage_data_end = usage_data_start_local + timedelta(
                seconds=len(usage_data_local)
            )
            chart_data = self._process_offset_scales(scales, usage_data_local, usage_data_end)

            # Fetch previous hour data for clock-boundary NBC quarters
            prev_hour_start = chart_start - timedelta(hours=1)
            prev_hour_data: list[float] = []
            try:
                prev_usage, _ = self.vue.get_chart_usage(
                    chan,
                    prev_hour_start,
                    chart_start,
                    scale=Scale.SECOND.value,
                    unit=Unit.KWH.value,
                )
                if prev_usage and len(prev_usage) > 0 and prev_usage[0] is not None:
                    prev_hour_data = prev_usage
            except (requests.exceptions.RequestException, IOError):
                self.logger.debug(
                    "could not fetch previous hour data for %s", vdi.device_name
                )

            return _PopulationResult(
                per_second_data=usage_data_local,
                scales=scales,
                chart_data=chart_data,
                nbc_seconds=usage_data_local,
                nbc_data_start=usage_data_start_local,
                prev_hour_data=prev_hour_data,
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
        seconds_remaining = (hour_next - hour["instant"]).total_seconds()

        minute_predicted = (
            seconds_remaining * scales[Scale.MINUTE.value]["usage"] / 60.0
        )
        prediction = hour["usage"] + minute_predicted

        smoothing: dict[str, float] = {}
        prediction_min = prediction
        prediction_max = prediction
        for scale in scales.keys():
            if not scale.endswith("MIN"):
                continue
            sval = hour["usage"] + (
                seconds_remaining * scales[scale]["usage"] / 60.0
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
            "seconds_remaining": seconds_remaining,
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
        nbc_result = self._compute_nbc(
            pop_result.nbc_seconds,
            pop_result.nbc_data_start,
            getattr(vdi, "time_zone", None),
        )

        # Compute clock-boundary NBC quarters for the 4 most recent 15-min windows
        from util import compute_clock_boundary_nbc_quarters

        clock_boundary_nbc = {}
        if pop_result.prev_hour_data:
            try:
                clock_boundary_nbc = compute_clock_boundary_nbc_quarters(
                    pop_result.prev_hour_data,
                    pop_result.nbc_seconds,
                    self.instant,
                )
            except Exception:  # pylint: disable=broad-exception-caught
                self.logger.debug(
                    "clock-boundary NBC computation failed for %s", vdi.device_name
                )

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
            clock_boundary_nbc=clock_boundary_nbc,
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
                datetime.now().isoformat()
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
