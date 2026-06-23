"""
Call Emporia VUE API and marshal predicted usage.
"""

import dataclasses

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import locale
import logging
from typing import Any, Callable, ClassVar, Optional

import requests
from pyemvue import PyEmVue
from pyemvue.enums import Scale, Unit

from clock import Clock, RealClock
from energy_cache import EnergyCache
from energy_aggregator import EnergyDataAggregator, TOUBuckets
from util import (
    CustomJSONProvider,
    NBCQuarterSet,
    ceil_to_qh,
    compute_nbc_quarters,
    custom_json_default,
    is_debug,
)

from config import Config, _config


logger = logging.getLogger(__name__)

_CLOCK: Clock = RealClock()


def set_clock(clock: Clock) -> None:
    """Override the module-level clock for testing.

    Args:
        clock: A ``Clock`` instance (typically ``FakeClock`` in tests).
    """
    global _CLOCK  # noqa: PLW0603
    _CLOCK = clock


MAX_FETCH_WINDOW = timedelta(hours=1)


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


def cap_fetch_window(start_time: datetime, now: datetime) -> datetime:
    """Cap a fetch start_time to prevent over-fetching after stale cache.

    If *start_time* is more than 1 hour before *now*, return the earliest
    appropriate quarter-hour boundary.  Otherwise return start_time unchanged.

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
    if now - start_time <= MAX_FETCH_WINDOW:
        return start_time

    return ceil_to_qh(now - MAX_FETCH_WINDOW)


def create_metrics(energy_cache: EnergyCache, now: datetime, logger: logging.Logger) -> dict[str, Any] | None:
    """Fetch metrics with incremental chart_start tracking via EnergyCache.

    On the first call, EnergyCache has no samples, so chart_start is set to
    3600 seconds ago (full hour of historical data). After that, chart_start
    advances to the most recent sample timestamp from the cache.

    Args:
        energy_cache: instance of EnergyCache.
        now: current datetime in local timezone.
        logger: Logger instance.

    Returns:
        Metrics dict from HourlyProjection, or None on failure.
    """
    # First call: fetch up to four QH periods.
    # Subsequent calls: fetch incremental data from the last sample timestamp.
    logger.debug(
        "create_metrics: len %d, last_sample_at %s",
        len(energy_cache._samples or []),
        energy_cache.last_sample_at
    )
    try:
        chart_start = (
            ceil_to_qh(now - MAX_FETCH_WINDOW)
            if energy_cache.last_sample_at is None
            else cap_chart_start(energy_cache.last_sample_at, now)
        )
        hp = HourlyProjection(now, logger, energy_cache)
        hp.populate(chart_start)
        logger.debug(
            "create_metrics result: devices=%d, data_start=%s, "
            "per_second_data_total=%d",
            len(hp.metrics.get("devices", [])),
            hp.metrics.get("data_start"),
            sum(
                len(d.get("per_second_data", []))
                for d in hp.metrics.get("devices", [])
            ) if hp.metrics.get("devices") else 0,
        )
        return hp.metrics

    except AssertionError as ae:
        logger.error(ae)
        # force clean cache and full fetch on next cycle
        energy_cache.invalidate()
        raise RetryableMetricsException(ae) from ae


@dataclass
class _PopulationResult:
    """Intermediate results from populating one device — no mutation of API objects."""

    per_second_data: list[float]
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
    """Minute-scale prediction data (internal, not serialized directly)."""
    predicted: float
    minutes_remaining: float


@dataclass(frozen=True)
class DevicePrediction:
    """Prediction result for one device.

    Replaces the dict return from _predict_device() with a typed dataclass.
    """

    lag: timedelta
    minute_predicted: float
    prediction: float
    prediction_min: float
    prediction_max: float
    seconds_remaining: float

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for backward compat with intermediate callers."""
        return {
            "lag": self.lag,
            "minute_predicted": self.minute_predicted,
            "prediction": self.prediction,
            "prediction_min": self.prediction_min,
            "prediction_max": self.prediction_max,
            "seconds_remaining": self.seconds_remaining,
        }


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
    nbc: NBCQuarterSet = dataclasses.field(  # type: ignore[assignment]
        default_factory=lambda: NBCQuarterSet(
            qh1=None, qh2=None, qh3=None, qh4=None
        ),
        repr=False,
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
            "timezone": self.timezone,
            "nbc": self.nbc.to_dict(),
            "per_second_data": self.per_second_data,
        }


@dataclass(frozen=True)
class TOUResult:
    """Result of a TOU (Time-of-Use) query.

    Wraps TOUBuckets and NBC total for the requested date range.
    Replaces the dict return from _get_tou_model().
    """

    buckets: TOUBuckets
    nbc: float

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for backward compat."""
        return {
            "buckets": self.buckets.to_dict(),
            "nbc": self.nbc,
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
            start_time = ceil_to_qh(now - MAX_FETCH_WINDOW)
        else:
            last_sample_idx = len(energy_cache.samples)
            quant = energy_cache.quantization_seconds or 0
            start_time = energy_cache.data_start + timedelta(
                seconds=max(0, last_sample_idx - quant)
            )

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

        # pyemvue throws error if start_time is earlier than end_time (now)
        assert start_time <= now

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


class VueAuthenticationError(Exception):
    """Raised when Vue authentication fails."""


class RetryableMetricsException(Exception):
    """Signal that the Emporia VUE API responded with a server error and the caller should retry."""

    def __init__(self, message, *args):
        self.message = message
        self.instant = _CLOCK.now()
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

    def __init__(
        self,
        logger_next: Optional[logging.Logger] = None,
        config: Config | None = None,
    ) -> None:
        self._cfg = config if config is not None else _config
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

        cfg = getattr(self, '_cfg', _config)

        #self.logger.debug({"keys": self.vue_keys})
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
                    username=cfg.vue_username,
                    password=cfg.vue_password,
                    token_storage_file=self.vue_keys,
                )
            except Exception as inner_ex:
                raise VueAuthenticationError(
                    "Vue authentication failed: check credentials"
                ) from inner_ex

        if not login_ok:
            # Token login returned False — fall back to password auth.
            self.logger.debug("token login failed, trying password")
            try:
                login_ok = self.vue.login(
                    username=cfg.vue_username,
                    password=cfg.vue_password,
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

        self.vue_auth["last"] = _CLOCK.now()

    def get_device_info(self) -> None:
        """
        Wrapper for vue get_devices,
        filtering results for ZIG001 devices.
        """
        rt_start = _CLOCK.now()
        age_limit = timedelta(hours=24)
        self.logger.debug(
            {"device_info_len": len(self.device_info), "vue_auth": self.vue_auth}
        )
        if len(self.device_info) > 0 and "last" in self.vue_auth:
            age = rt_start - self.vue_auth["last"]
            #self.logger.debug({"age": age})
            if age < age_limit:
                #self.logger.debug({"device_info": self.device_info})
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
        config: Config | None = None,
    ) -> None:
        self.metrics: dict[str, Any] = {
            "api_response": {},
            "debug": is_debug(config),
            "devices": [],
        }

        super().__init__(logger_next, config=config)

        self.instant = instant
        self.metrics["instant"] = self.instant
        self.energy_cache = energy_cache  # Merged samples for NBC computation

    def populate(self, chart_start: datetime) -> dict[int, DevicePrediction]:
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
            _CLOCK.now() - chart_start
        )
        return usage_data_local, usage_data_start_local, chan.channel_num

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

    def predict(
        self, population: dict[int, _PopulationResult]
    ) -> dict[int, DevicePrediction]:
        """Predict consumption or surplus at end of current hour.

        Uses the minute-scale usage rate to extrapolate remaining
        consumption for the current hour, then computes min/max bounds
        across all available minute scales (1MIN–10MIN).

        Args:
            population: Results from populate(), mapping gid -> PopulationResult.

        Returns:
            Dict of gid -> DevicePrediction for each device.
        """
        predictions: dict[int, DevicePrediction] = {}
        for gid, pop_result in population.items():
            pred_result = self._predict_device(
                pop_result.per_second_data, pop_result.nbc_data_start
            )
            predictions[gid] = pred_result
        return predictions

    def _compute_nbc(
        self,
        usage_data_local: list[float],
        prediction_window_seconds: int | None = None,
    ) -> NBCQuarterSet:
        """Compute NBC values for each quarter hour in the current hour.

        Delegates to ``compute_nbc_quarters`` in util. Quarter boundaries are
        determined by the number of observed data points (``n``), not by
        wall-clock time, so API lag cannot cause two quarters to appear
        incomplete simultaneously.

        Args:
            usage_data_local: Per-second kWh data for the current hour.
            prediction_window_seconds: Number of trailing seconds to use for
                rate extrapolation of the incomplete quarter. Passed through
                to ``compute_nbc_quarters``. Defaults to 60 when ``None``.

        Returns:
            An ``NBCQuarterSet`` with QH1-QH4 containing NBC metrics or
            ``None`` if the quarter has not yet started.
        """
        result = compute_nbc_quarters(
            usage_data_local,
            prediction_window_seconds=prediction_window_seconds,
        )
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
            except (requests.exceptions.RequestException, IOError):
                self.logger.exception(
                    "error fetching device data: skipping %s", vdi.device_name
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

    def _predict_device(
        self, per_second_data: list[float], data_start: datetime
    ) -> DevicePrediction:
        """Compute prediction for one device from raw per-second data.

        Args:
            per_second_data: Per-second kWh values for the current hour.
            data_start: Start time of the per-second data.

        Returns:
            DevicePrediction with prediction, min/max bounds, lag, etc.
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
        hour_instant = data_start + timedelta(seconds=len(per_second_data))
        hour_usage = 1000.0 * sum(per_second_data)
        seconds_remaining_hour = (hour_next - hour_instant).total_seconds()

        def _minute_usage(data: list[float]) -> float:
            """Compute Wh/minute usage rate from last 60 seconds of data."""
            tail = data[-60:]
            total_wh = 1000.0 * sum(tail)
            return total_wh * 60.0 / len(tail) if len(tail) != 0 else 0.0

        minute_predicted = seconds_remaining_hour * _minute_usage(per_second_data) / 60.0
        prediction = hour_usage + minute_predicted

        lag = (
            self.instant - hour_instant
            if hour_instant < self.instant
            else timedelta(0)
        )

        return DevicePrediction(
            lag=lag,
            minute_predicted=minute_predicted,
            prediction=prediction,
            prediction_min=prediction,
            prediction_max=prediction,
            seconds_remaining=seconds_remaining_hour,
        )

    def _compute_device_metrics(
        self,
        vdi: Any,
        pop_result: _PopulationResult,
        pred_result: DevicePrediction,
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
        pop_data_start = pop_result.nbc_data_start
        energy_cache = self.energy_cache

        def _maybe_merge_with_cache(raw_data: list[float] | None) -> list[float]:
            """Merge raw incremental data with cached samples when available.

            When the cache holds a previous fetch (e.g. a full hour) and the
            API returns only an incremental delta (~60 samples), this produces
            a complete, gapless time series by appending the delta to the
            cached data.

            Args:
                raw_data: Per-second data from the API (incremental delta).

            Returns:
                Merged data list, or the original input when no cache is
                available or merge yields nothing.
            """
            if raw_data is None:
                return raw_data or []
            if energy_cache is None or energy_cache._data is None:
                return raw_data
            merged = energy_cache.merge_incremental(
                energy_cache._data,
                raw_data,
                pop_data_start,
            )
            if merged is not None and merged.samples is not None:
                return merged.samples
            return raw_data

        nbc_seconds = _maybe_merge_with_cache(pop_result.nbc_seconds)
        # Use raw API samples for per_second_data so that the EnergyCache
        # re-ingests only genuinely new points.  _maybe_merge_with_cache
        # returns the full merged cache (old + new), which EnergyCache then
        # re-extracts and mis-labels with the incremental data_start.  That
        # mismatch inflates merged_last_sample_at into the future, which
        # causes the next call to send start > end to the Emporia API (400).
        # NBC still uses nbc_seconds (merged) for prediction accuracy.
        per_second_data = list(pop_result.per_second_data) if pop_result.per_second_data is not None else []

        # Determine the prediction window from quantization data, if available.
        prediction_window_seconds: int | None = None
        if energy_cache is not None and energy_cache.data is not None:
            qs = energy_cache.quantization_seconds
            qc = energy_cache.quantization_confidence
            if qs is not None and qc is not None and qc >= 0.9:
                prediction_window_seconds = qs

        nbc_result = self._compute_nbc(nbc_seconds, prediction_window_seconds)

        return DeviceMetrics(
            gid=vdi.device_gid,
            name=vdi.device_name,
            lag=pred_result.lag,
            per_second_data=per_second_data,
            prediction=_PredictionData(
                value=pred_result.prediction,
                min_value=pred_result.prediction_min,
                max_value=pred_result.prediction_max,
            ),
            minute_data=_MinuteData(
                predicted=pred_result.minute_predicted,
                minutes_remaining=pred_result.seconds_remaining / 60.0,
            ),
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
        config: Config | None = None,
    ) -> None:
        super().__init__(logger_next, config=config)

        self.start_date = start_date
        self.end_date = end_date
        self.tou_result: Optional[TOUBuckets] = None
        self.nbc_result: Optional[float] = None

        self.fetch_usage_data()
        if is_debug(config):
            filename = (
                _CLOCK.now().isoformat()
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
        total = 0.0
        peak = 0.0
        part_peak = 0.0
        off_peak = 0.0

        nbc_total_wh = 0.0

        for data_chunk in self.usage_data_list:
            chunk_buckets: TOUBuckets = EnergyDataAggregator.aggregate_from_15min(
                data_chunk["start"], data_chunk["data"]
            )

            total += chunk_buckets.total
            peak += chunk_buckets.peak
            part_peak += chunk_buckets.part_peak
            off_peak += chunk_buckets.off_peak

            # Sum positive 15-min periods only (imports); negatives are exports, ignored
            for usage_kwh in data_chunk["data"]:
                if usage_kwh is not None and usage_kwh > 0:
                    nbc_total_wh += usage_kwh * 1000.0

        self.tou_result = TOUBuckets(
            total=total, peak=peak, part_peak=part_peak, off_peak=off_peak,
        )
        self.nbc_result = nbc_total_wh


# Maintain backward compatibility by aliasing Metrics to HourlyProjection
Metrics = HourlyProjection
