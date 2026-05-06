"""
Call Emporia VUE API and marshal predicted usage.
"""

from datetime import datetime, timedelta, timezone
import json
import locale
import logging
from typing import Any, Callable, ClassVar, Dict, List, Optional

import requests


from pyemvue import PyEmVue
from pyemvue.enums import Scale, Unit

from decouple import config
from energy_aggregator import EnergyDataAggregator
from util import CustomJSONProvider, compute_nbc_quarters, custom_json_default, is_debug


logger = logging.getLogger(__name__)


class MetricsCache:
    """Cache for HourlyProjection metrics data.

    Caches the full metrics dict for a configurable TTL. Callers get either
    a fresh fetch or a cached copy depending on TTL expiry. The fetch
    timestamp is stored in the returned data under the ``_fetched_at`` key
    so downstream consumers can distinguish real API fetch time from cache
    hit time.
    """

    def __init__(self, ttl_seconds: int = 30) -> None:
        self._data: Dict[str, Any] | None = None
        self._fetched_at: datetime | None = None
        self._ttl = timedelta(seconds=ttl_seconds)

    def get_or_fetch(
        self,
        fetch_func: Callable[[], Dict[str, Any]],
    ) -> tuple[Dict[str, Any], bool]:
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

    device_info: ClassVar[Dict[str, Any]] = {}
    json: ClassVar[type] = CustomJSONProvider
    vue: ClassVar[PyEmVue] = PyEmVue()
    vue_auth: ClassVar[Dict[str, Any]] = {}
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

        if self.vue and hasattr(self.vue, "auth"):
            self.logger.debug(
                {"auth": getattr(self.vue, "auth"), "vue_auth": self.vue_auth}
            )
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
                    username=config("VUE_USERNAME"),
                    password=config("VUE_PASSWORD"),
                    token_storage_file=self.vue_keys,
                )
            except Exception as inner_ex:
                raise VueAuthenticationError(
                    "Vue authentication failed: check credentials"
                ) from inner_ex

        if login_ok:
            self.logger.debug("login ok")
        else:
            self.logger.error("login failed")
            raise VueAuthenticationError("Vue authentication failed: check credentials")

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
        self.metrics: Dict[str, Any] = {
            "api_response": {},
            "debug": is_debug(),
            "devices": [],
        }

        super().__init__(logger_next)

        self.instant = datetime.now(timezone.utc)
        self.metrics["instant"] = self.instant

        self.populate()

        self.metrics["api_response"]["total"] = sum(
            self.metrics["api_response"].values(), timedelta()
        )

        self.predict()

        self.logger.debug("device %s", self.device_info)
        for gid, vdi in self.device_info.items():
            device_metrics = {
                "gid": gid,
                "lag": vdi.lag,
                "name": vdi.device_name,
                "minute_predicted": vdi.minute_predicted,
                "minutes_remaining": vdi.seconds_remaining / 60.0,
                "prediction": vdi.prediction,
                "prediction_min": vdi.prediction_min,
                "prediction_max": vdi.prediction_max,
                "chart_data": vdi.chart_data,
                "scales": vdi.scales,
                "smoothing": vdi.smoothing,
                "timezone": vdi.time_zone,
                "nbc": (
                    self._compute_nbc(
                        vdi.nbc_seconds, vdi.nbc_data_start, getattr(vdi, "time_zone", None)
                    )
                    if hasattr(vdi, "nbc_seconds")
                    else None
                ),
            }
            self.metrics["devices"].append(device_metrics)

        self.logger.debug(
            "reporting metrics for %d devices", len(self.metrics["devices"])
        )

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

    def _process_offset_scales(self, dig, usage_data_local, usage_data_end):
        """
        Process minute-scale offset data (1MIN–10MIN) and set chart_data.

        Computes usage for each minute scale from the tail of the dataset
        and stores results via populate_scale(). Also sets the last 300
        data points as chart_data on the device info object.
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
            self.populate_scale(dig, scale, offset_start, offset_data)
        dig.chart_data = usage_data_local[-300:]

    def populate(self) -> None:
        """Fetch recent data using second granularity to minimize lag."""
        chart_start = self.instant - timedelta(
            minutes=self.instant.minute,
            seconds=self.instant.second,
            microseconds=self.instant.microsecond,
        )
        for vdi in self.device_info.values():
            self.logger.debug("device: %s", vdi)
            for chan in vdi.channels:
                self.logger.debug("channel: %s", chan.name)
                gid = chan.device_gid
                dig = self.device_info[gid]
                usage_data_start_local = chart_start  # safe default if fetch fails
                try:
                    usage_data_local, usage_data_start_local, _ = (
                        self._fetch_channel_data(chan, chart_start, self.instant)
                    )
                    self.populate_scale(
                        dig, Scale.HOUR.value, usage_data_start_local, usage_data_local
                    )
                    usage_data_end = usage_data_start_local + timedelta(
                        seconds=len(usage_data_local)
                    )
                    self._process_offset_scales(dig, usage_data_local, usage_data_end)
                    dig.nbc_seconds = usage_data_local
                    dig.nbc_data_start = usage_data_start_local
                except (requests.exceptions.RequestException, IOError):
                    self.logger.exception(
                        "error fetching device data: skipping %s", vdi.device_name
                    )
                    vdi.scales = {}
                    self.populate_scale(
                        dig, Scale.HOUR.value, usage_data_start_local, []
                    )

    def populate_scale(
        self, dig: Any, scale: str, data_start: datetime, data: List[float]
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
        data: List[float], data_start: datetime, scale: str
    ) -> Dict[str, Any]:
        """Calculate usage statistics for a given scale (hour or minutes).

        Args:
            data: kWh values per second/minute. Negative values indicate solar export.
            data_start: Start time of the first data point.
            scale: Scale identifier ('1H', '1MIN'-'10MIN').

        Returns:
            Dict with keys: usage (Wh), seconds, instant, and optionally
                data/data_len/data_start if DEBUG is enabled.
        """
        dsi: Dict[str, object] = {}
        data_len = len(data)

        if is_debug():
            dsi["data"] = data[:3]
            dsi["data_len"] = data_len
            dsi["data_start"] = data_start

        usage = 1000.0 * sum(data)
        if not scale == "1H" and data_len != 0:
            usage = usage * 60.0 / data_len

        dsi["instant"] = data_start + timedelta(seconds=data_len)
        dsi["seconds"] = data_len
        dsi["usage"] = usage
        return dsi

    def predict(self) -> None:
        """
        Predict consumption or surplus at end of current hour.

        Uses the minute-scale usage rate to extrapolate remaining
        consumption for the current hour, then computes min/max bounds
        across all available minute scales (1MIN–10MIN).
        """
        for vdi in self.device_info.values():
            hour_next = (
                self.instant
                + timedelta(hours=1)
                - timedelta(
                    minutes=self.instant.minute,
                    seconds=self.instant.second,
                    microseconds=self.instant.microsecond,
                )
            )
            scales = vdi.scales
            hour = scales[Scale.HOUR.value]
            seconds_remaining = (hour_next - hour["instant"]).total_seconds()

            minute_predicted = (
                seconds_remaining * scales[Scale.MINUTE.value]["usage"] / 60.0
            )
            prediction = hour["usage"] + minute_predicted

            vdi.smoothing = {}
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
                vdi.smoothing[scale] = sval

            vdi.lag = (
                self.instant - hour["instant"]
                if hour["instant"] < self.instant
                else timedelta(0)
            )
            vdi.minute_predicted = minute_predicted
            vdi.prediction = prediction
            vdi.prediction_max = prediction_max
            vdi.prediction_min = prediction_min
            vdi.seconds_remaining = seconds_remaining

    def _compute_nbc(
        self,
        usage_data_local: List[float],
        usage_data_start_local: datetime,
        device_time_zone: str | None = None,  # pylint: disable=unused-argument
    ) -> Dict[str, Any]:
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
        self.tou_result: Optional[Dict[str, float]] = None
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
        self.usage_data_list: List[Dict[str, Any]] = []
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
        combined_buckets: Dict[str, float] = {
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
