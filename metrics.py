"""
Call Emporia VUE API and marshal predicted usage.
"""

from datetime import datetime, timedelta, timezone
import json
import locale
import logging
import random
from typing import Any, Dict, List, Optional

import pytz
import requests


from decouple import config
from pyemvue import PyEmVue
from pyemvue.enums import Scale, Unit

from energy_aggregator import EnergyDataAggregator
from util import CustomJSONProvider, TIMEZONE


DEBUG = config("DEBUG", default="False", cast=bool)

logger = logging.getLogger(__name__)


class VueAuthenticationError(Exception):
    """Raised when Vue authentication fails."""


class RetryableMetricsException(Exception):
    """Signal that the Emporia VUE API responded with a server error and the caller should retry."""

    def __init__(self, message, *args):
        self.message = message
        self.instant = datetime.now(timezone.utc)
        super(RetryableMetricsException, self).__init__(message, *args)


class MetricsBase:
    """
    Base class handling PyEmVue connection and authentication only.
    """

    device_info = {}
    json = CustomJSONProvider
    vue = PyEmVue()
    vue_auth = {}
    vue_keys = ".vue-keys.json"

    def __init__(self, logger_next: Optional[logging.Logger] = None) -> None:
        self.logger = logger_next or logger
        self.logger.debug("metrics base init")
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

    metrics = {}

    def __init__(self, logger_next: Optional[logging.Logger] = None) -> None:
        self.metrics: Dict[str, object] = {
            "api_response": {},
            "debug": DEBUG,
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
                    self._compute_nbc(vdi.nbc_seconds, vdi.nbc_data_start)
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
        self, dig: object, scale: str, data_start: datetime, data: List[float]
    ) -> None:
        """
        Populate N seconds of usage data for a scale
        of 1H or M minutes.
        """
        self.logger.debug(
            {"dig": dig, "scale": scale, "data_start": data_start, "data": data[:3]}
        )
        if not hasattr(dig, "scales"):
            dig.scales = {}
        if not hasattr(dig.scales, scale):
            dig.scales[scale] = {}
        dig.scales[scale] = self.data_for_scale(data, data_start, scale)

    @staticmethod
    def data_for_scale(
        data: List[float], data_start: datetime, scale: str
    ) -> Dict[str, object]:
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

        if DEBUG:
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
        self, usage_data_local: List[float], usage_data_start_local: datetime
    ) -> Dict[str, Any]:
        """Compute NBC values for each quarter hour in the current hour.

        PG&E bills Non-Bypassable Charges based on net consumption over each
        15-minute interval. For complete quarters, sums all per-second kWh
        values and converts to Wh (clamped at zero). For incomplete quarters,
        uses a 60-second lookback window to extrapolate the full quarter value.

        Args:
            usage_data_local: Per-second kWh data for the current hour.
            usage_data_start_local: Start time of the first data point (hour boundary).

        Returns:
            Dict with keys QH1-QH4, each containing NBC metrics or None if
            the quarter has not yet started.
        """
        # Determine how many seconds have been observed so far in this hour
        elapsed = self.instant - usage_data_start_local
        n = max(0, int(elapsed.total_seconds()))

        # Convert instant to device's local timezone for quarter boundary checks
        try:
            local_tz = pytz.timezone(self.device_info.get("time_zone", TIMEZONE))
        except (pytz.exceptions.UnknownTimeZoneError, AttributeError):
            local_tz = pytz.timezone(TIMEZONE)

        if self.instant.tzinfo is None:
            instant_local = local_tz.localize(self.instant)
        else:
            instant_local = self.instant.astimezone(local_tz)

        current_minute = instant_local.minute
        current_second = instant_local.second
        seconds_into_hour = current_minute * 60 + current_second

        quarters = [
            ("QH1", 0, 899),       # minutes 0-14 (first 15 min)
            ("QH2", 900, 1799),    # minutes 15-29
            ("QH3", 1800, 2699),   # minutes 30-44
            ("QH4", 2700, 3599),   # minutes 45-59
        ]

        result: Dict[str, Any] = {}
        for qh_name, start_idx, end_idx in quarters:
            # Not started: no seconds observed for this quarter yet
            if seconds_into_hour <= start_idx:
                result[qh_name] = None
                continue

            # Determine which indices have data in this quarter
            obs_start = max(start_idx, 0)
            obs_end = min(n, end_idx + 1)  # exclusive upper bound

            if n > end_idx:
                # Complete: all seconds in this quarter have been observed
                values = usage_data_local[start_idx:end_idx + 1]
                raw_wh = sum(values) * 1000
                result[qh_name] = {
                    "wh": max(0, raw_wh),
                    "complete": True,
                    "raw_wh": raw_wh,
                }
            else:
                # Incomplete: look back up to 60 seconds from current position
                # (absolute window - may include data from previous quarter)
                lookback_start = max(n - 60, start_idx)
                values = usage_data_local[lookback_start:n]
                rate = sum(values) / len(values) if values else 0.0
                # predicted_wh not clamped to zero by design
                predicted_wh = rate * 900 * 1000

                # raw_wh = actual observed data in this quarter only (not lookback)
                raw_values = usage_data_local[obs_start:obs_end]
                raw_wh = sum(raw_values) * 1000

                result[qh_name] = {
                    "wh": predicted_wh,
                    "complete": False,
                    "raw_wh": raw_wh,
                    "predicted_wh": predicted_wh,
                    "samples_used": len(values),
                }

        return result


class TOUReporter(MetricsBase):
    """
    Multi-day Time-of-Use aggregation.

    Fetches historical data at minute granularity and aggregates
    into TOU buckets (total, peak, part_peak, off_peak).
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

        self.fetch_usage_data()
        if DEBUG:
            filename = (
                datetime.now().isoformat()
                + f"_{start_date}_{end_date if end_date else 'None'}_"
            )
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(self.usage_data_list, f, default=CustomJSONProvider().default)
        self.aggregate_tou()

    def fetch_usage_data(self) -> None:
        """
        Fetch historical usage data at minute granularity.

        Due to API limit of 13H of 1MIN data, we fetch data in chunks.
        """
        self.usage_data_list: List[Dict[str, object]] = []
        self._fetch_error: Optional[Exception] = None

        for vdi in self.device_info.values():
            for chan in vdi.channels:
                self.logger.debug("fetching TOU data for channel: %s", chan.name)

                current_time = self.start_date
                while current_time < self.end_date:
                    chunk_end = min(current_time + timedelta(hours=12), self.end_date)

                    try:
                        self.logger.debug(
                            "fetching chunk: %s - %s", current_time, chunk_end
                        )
                        usage_data, usage_data_start = self.vue.get_chart_usage(
                            chan,
                            current_time,
                            chunk_end,
                            scale=Scale.MINUTE.value,
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

                    current_time = chunk_end + timedelta(minutes=1)

    def aggregate_tou(self) -> None:
        """
        Aggregate fetched usage data into TOU buckets.

        Delegates to EnergyDataAggregator for the actual classification
        and bucket aggregation.
        """
        combined_buckets: Dict[str, float] = {
            "total": 0.0,
            "peak": 0.0,
            "part_peak": 0.0,
            "off_peak": 0.0,
        }

        for data_chunk in self.usage_data_list:
            chunk_buckets = EnergyDataAggregator.aggregate_from_minutes(
                data_chunk["start"], data_chunk["data"]
            )

            for bucket in combined_buckets:
                combined_buckets[bucket] += chunk_buckets[bucket]

        self.tou_result = combined_buckets


# Maintain backward compatibility by aliasing Metrics to HourlyProjection
Metrics = HourlyProjection


def _generate_hour_seconds(
    device_seed: int, minute_of_hour: int, sign: float = -1.0
) -> List[float]:
    """Generate deterministic kWh/second values for up to an hour (0-3600 values).

    Returns a variable-length list of exactly `minute_of_hour * 60` floats (capped at 3600).

    Args:
        device_seed: Seed for reproducibility.
        minute_of_hour: Current minute (0-59), determines list length.
        sign: +1.0 for positive consumption, -1.0 for negative (solar export).

    Returns:
        List of `minute_of_hour * 60` floats (kWh/second).
    """
    rng = random.Random(device_seed)
    num_seconds = min(minute_of_hour * 60, 3600)
    if sign < 0:
        return [rng.uniform(-0.001, -0.0004) for _ in range(num_seconds)]
    else:
        return [rng.uniform(0.0002, 0.0008) for _ in range(num_seconds)]


class MetricsMock:
    """
    Mock metrics data for testing. Supports NBC quarter-hour computation,
    TOU bucket reporting, and all existing hourly prediction features.

    The mock is parameterized by instant_minute so it always represents a
    current timestamp. Tests should verify invariant properties rather than
    absolute timestamps.

    NOTE: Negative 'usage' values represent solar generation exceeding consumption
    (power exported to the grid). This is physically normal during sunny hours when
    PV panels produce more than the home uses. The downstream TOU aggregator handles
    these correctly by reducing the bucket total.
    """

    metrics: Dict[str, Any]
    tou_result: Dict[str, float]

    def __init__(self, instant_minute: int = 42) -> None:
        now = datetime.now(timezone.utc)

        is_full_hour = instant_minute >= 60
        minute_of_hour = min(instant_minute, 59)

        self.instant = now.replace(
            minute=minute_of_hour, second=0, microsecond=0
        )

        _per_second_data_a = _generate_hour_seconds(12345, minute_of_hour, sign=-1.0)
        _per_second_data_b = _generate_hour_seconds(67890, minute_of_hour, sign=1.0)

        if is_full_hour:
            _per_second_data_a.extend([_per_second_data_a[-1]] * (3600 - len(_per_second_data_a)))
            _per_second_data_b.extend([_per_second_data_b[-1]] * (3600 - len(_per_second_data_b)))

        self.metrics: Dict[str, object] = {
            "api_response": {
                "get_chart_usage/1,2,3": timedelta(microseconds=750072),
                "total": timedelta(microseconds=750072),
            },
            "debug": True,
            "devices": [
                self._build_device(
                    _per_second_data_a, minute_of_hour, device_name="MOCK", sign=-1.0
                ),
                self._build_device(
                    _per_second_data_b,
                    minute_of_hour,
                    device_name="SOLAR+LOAD",
                    timezone_str=TIMEZONE,
                    sign=1.0,
                ),
            ],
            "instant": datetime(
                2026, 2, 27, 18, 42, 34, 170162, tzinfo=timezone.utc
            ),
        }

        self.tou_result: Dict[str, float] = {
            "total": 12847.3,
            "peak": 3200.5,
            "part_peak": 4100.2,
            "off_peak": 5546.6,
        }

    def _build_device(
        self,
        per_second_data: List[float],
        minute_of_hour: int,
        device_name: str = "MOCK",
        timezone_str: str = TIMEZONE,
        sign: float = -1.0,
    ) -> Dict[str, Any]:
        """Build a single device dict with all fields for backward-compatible testing.

        Produces dynamic scale data derived from the actual per-second values.
        The scales' data arrays remain truncated (3 sample floats each); full
        per-second data is stored separately in the 'per_second_data' field for NBC use.

        Args:
            per_second_data: Full array of kWh/second values observed so far.
            minute_of_hour: Current minute (0-59) within the hour.
            device_name: Display name for this device.
            timezone_str: IANA timezone string for the device.
            sign: +1.0 for positive consumption, -1.0 for negative (solar export).

        Returns:
            Device dictionary with all fields matching the production structure.
        """
        n = len(per_second_data)
        hour_usage = 1000.0 * sum(per_second_data)

        scales: Dict[str, Any] = {
            "1H": self._make_scale_entry(
                per_second_data,
                self.instant.replace(second=0, microsecond=0),
                n,
                hour_usage,
            )
        }

        smoothing: Dict[str, float] = {}
        for usm in range(1, 11):
            uss = 60 * usm
            if uss > n:
                continue
            offset_data = per_second_data[-uss:]
            offset_start = self.instant.replace(second=0, microsecond=0) + timedelta(
                minutes=minute_of_hour - usm
            )
            scale_usage = 1000.0 * sum(offset_data) * 60.0 / len(offset_data)
            scales[str(usm) + "MIN"] = self._make_scale_entry(
                offset_data, offset_start, len(offset_data), scale_usage
            )
            smoothing[str(usm) + "MIN"] = hour_usage + (
                (3600 - minute_of_hour * 60) * scale_usage / 60.0
            )

        one_min_usage = scales.get("1MIN", {}).get("usage", 0) if "1MIN" in scales else 0
        seconds_remaining = 3600 - minute_of_hour * 60
        minute_predicted = seconds_remaining * one_min_usage / 60.0
        prediction = hour_usage + minute_predicted

        return {
            "gid": hash(device_name) % (10**8),
            "lag": timedelta(seconds=2, microseconds=(hash(device_name) % 999999)),
            "name": device_name,
            "minute_predicted": round(minute_predicted, 14),
            "minutes_remaining": round(seconds_remaining / 60.0, 14),
            "per_second_data": per_second_data,
            "prediction": round(prediction, 14),
            "prediction_min": round(prediction, 14),
            "prediction_max": round(prediction * 0.9 if sign < 0 else prediction * 1.1, 14),
            "scales": scales,
            "smoothing": {k: round(v, 14) for k, v in smoothing.items()},
            "timezone": timezone_str,
            "nbc": self._compute_nbc(
                per_second_data,
                start=self.instant - timedelta(seconds=n),
            ),
        }

    @staticmethod
    def _make_scale_entry(
        data: List[float], data_start: datetime, data_len: int, usage: float
    ) -> Dict[str, Any]:
        """Create a scale entry dict from raw per-second slice data.

        Args:
            data: kWh/second values for this scale window.
            data_start: Start time of the first data point.
            data_len: Number of seconds in this scale window.
            usage: Pre-computed Wh usage value for display.

        Returns:
            Scale entry dict with data (truncated to 3 samples), metadata, and usage.
        """
        return {
            "data": data[:3] if len(data) >= 3 else list(data),
            "data_len": data_len,
            "data_start": data_start,
            "instant": data_start + timedelta(seconds=data_len),
            "seconds": data_len,
            "usage": usage,
        }

    def _compute_nbc(
        self, per_second_data: List[float], start: datetime | None = None
    ) -> Dict[str, Any]:
        """Compute NBC values for each quarter hour.

        Args:
            per_second_data: Variable-length list of observed kWh/second values.
            start: When data collection started (defaults to instant - 1 hour).

        Returns:
            Dict with keys QH1-QH4, each containing NBC metrics or None.
        """
        if start is not None:
            elapsed = self.instant - start
            n = max(0, int(elapsed.total_seconds()))
        else:
            n = len(per_second_data)  # fallback for backward compat

        quarters = [
            ("QH1", 0, 899),       # minutes 0-14 (first 15 min)
            ("QH2", 900, 1799),    # minutes 15-29
            ("QH3", 1800, 2699),   # minutes 30-44
            ("QH4", 2700, 3599),   # minutes 45-59
        ]

        result: Dict[str, Any] = {}
        for qh_name, start_idx, end_idx in quarters:
            # Not started: no seconds observed for this quarter yet
            if n <= start_idx:
                result[qh_name] = None
                continue

            # Determine which indices have data in this quarter
            obs_start = max(start_idx, 0)
            obs_end = min(n, end_idx + 1)  # exclusive upper bound

            if n > end_idx:
                # Complete: all seconds in this quarter have been observed
                values = per_second_data[start_idx:end_idx + 1]
                raw_wh = sum(values) * 1000
                result[qh_name] = {
                    "wh": max(0, raw_wh),
                    "complete": True,
                    "raw_wh": raw_wh,
                }
            else:
                # Incomplete: look back up to 60 seconds from current position
                # (absolute window - may include data from previous quarter)
                lookback_start = max(n - 60, start_idx)
                values = per_second_data[lookback_start:n]
                rate = sum(values) / len(values) if values else 0.0
                predicted_wh = max(0, rate * 900 * 1000)

                # raw_wh = actual observed data in this quarter only (not lookback)
                raw_values = per_second_data[obs_start:obs_end]
                raw_wh = sum(raw_values) * 1000

                result[qh_name] = {
                    "wh": predicted_wh,
                    "complete": False,
                    "raw_wh": raw_wh,
                    "predicted_wh": predicted_wh,
                    "samples_used": len(values),
                }

        return result
