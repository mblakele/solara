"""
Call Emporia VUE API and marshal predicted usage.
"""

from datetime import datetime, timedelta, timezone
import json
import locale
import logging
from typing import Dict, List, Optional

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
    """
    Use this exception class to signal that the Emporia VUE API
    responded with a server error and the controller should retry.
    Include utcnow for display, so that page refresh is obvious.
    """

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
        """
        Fetch recent data. Use seconds to minimize lag.
        Seconds data usually lags by a few seconds, but sometimes longer.
        Fetch all seconds in current hour.
        Performance seems ok, and rounding seems ok.
        """
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
        """Calculate usage statistics for a given scale (hour or minutes)."""
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


class MetricsMock:
    """
    Mock metrics data, for testing.
    """

    metrics: Dict[str, object]

    def __init__(self) -> None:
        self.metrics: Dict[str, object] = {
            "api_response": {
                "get_chart_usage/1,2,3": timedelta(microseconds=750072),
                "total": timedelta(microseconds=750072),
            },
            "debug": True,
            "devices": [
                {
                    "gid": 12345,
                    "lag": timedelta(seconds=2, microseconds=170162),
                    "name": "MOCK",
                    "minute_predicted": -468.43419509779943,
                    "minutes_remaining": 17.466666666666665,
                    "prediction": -52.516668090260964,
                    "prediction_min": -52.516668090260964,
                    "prediction_max": -38.242027851465195,
                    "scales": {
                        "1H": {
                            "data": [
                                0.0012375001112620038,
                                0.0012375001112620038,
                                0.0012299999926090241,
                            ],
                            "data_len": 2552,
                            "data_start": datetime(
                                2026, 2, 27, 18, 0, tzinfo=timezone.utc
                            ),
                            "instant": datetime(
                                2026, 2, 27, 18, 42, 32, tzinfo=timezone.utc
                            ),
                            "seconds": 2552,
                            "usage": 415.91752700753847,
                        },
                        "1MIN": {
                            "data": [
                                -0.0004437500238418579,
                                -0.0004437500238418579,
                                -0.0004437500238418579,
                            ],
                            "data_len": 60,
                            "data_start": datetime(
                                2026, 2, 27, 18, 41, 32, tzinfo=timezone.utc
                            ),
                            "instant": datetime(
                                2026, 2, 27, 18, 42, 32, tzinfo=timezone.utc
                            ),
                            "seconds": 60,
                            "usage": -26.818751627736606,
                        },
                        "2MIN": {
                            "data": [
                                -0.00044500003258387253,
                                -0.00044500003258387253,
                                -0.00044500003258387253,
                            ],
                            "data_len": 120,
                            "data_start": datetime(
                                2026, 2, 27, 18, 40, 32, tzinfo=timezone.utc
                            ),
                            "instant": datetime(
                                2026, 2, 27, 18, 42, 32, tzinfo=timezone.utc
                            ),
                            "seconds": 120,
                            "usage": -26.768751608861805,
                        },
                        "3MIN": {
                            "data": [
                                -0.0004450000325838725,
                                -0.0004450000325838725,
                                -0.0004450000325838725,
                            ],
                            "data_len": 180,
                            "data_start": datetime(
                                2026, 2, 27, 18, 39, 32, tzinfo=timezone.utc
                            ),
                            "instant": datetime(
                                2026, 2, 27, 18, 42, 32, tzinfo=timezone.utc
                            ),
                            "seconds": 180,
                            "usage": -26.71666824208363,
                        },
                        "4MIN": {
                            "data": [
                                -0.00043750001319249474,
                                -0.00043750001319249474,
                                -0.00043750001319249474,
                            ],
                            "data_len": 240,
                            "data_start": datetime(
                                2026, 2, 27, 18, 38, 32, tzinfo=timezone.utc
                            ),
                            "instant": datetime(
                                2026, 2, 27, 18, 42, 32, tzinfo=timezone.utc
                            ),
                            "seconds": 240,
                            "usage": -26.600001379847455,
                        },
                        "5MIN": {
                            "data": [
                                -0.0004375000132189857,
                                -0.0004375000132189857,
                                -0.0004375000132189857,
                            ],
                            "data_len": 300,
                            "data_start": datetime(
                                2026, 2, 27, 18, 37, 32, tzinfo=timezone.utc
                            ),
                            "instant": datetime(
                                2026, 2, 27, 18, 42, 32, tzinfo=timezone.utc
                            ),
                            "seconds": 300,
                            "usage": -26.50000128470518,
                        },
                        "6MIN": {
                            "data": [
                                -0.0004300000270207724,
                                -0.0004300000270207724,
                                -0.0004300000270207724,
                            ],
                            "data_len": 360,
                            "data_start": datetime(
                                2026, 2, 27, 18, 36, 32, tzinfo=timezone.utc
                            ),
                            "instant": datetime(
                                2026, 2, 27, 18, 42, 32, tzinfo=timezone.utc
                            ),
                            "seconds": 360,
                            "usage": -26.402084584633567,
                        },
                        "7MIN": {
                            "data": [
                                -0.0004300000270207724,
                                -0.0004300000270207724,
                                -0.0004300000270207724,
                            ],
                            "data_len": 420,
                            "data_start": datetime(
                                2026, 2, 27, 18, 35, 32, tzinfo=timezone.utc
                            ),
                            "instant": datetime(
                                2026, 2, 27, 18, 42, 32, tzinfo=timezone.utc
                            ),
                            "seconds": 420,
                            "usage": -26.298215559494096,
                        },
                        "8MIN": {
                            "data": [
                                -0.00042000002337826625,
                                -0.00042000002337826625,
                                -0.00042500002516640556,
                            ],
                            "data_len": 480,
                            "data_start": datetime(
                                2026, 2, 27, 18, 34, 32, tzinfo=timezone.utc
                            ),
                            "instant": datetime(
                                2026, 2, 27, 18, 42, 32, tzinfo=timezone.utc
                            ),
                            "seconds": 480,
                            "usage": -26.20343880499426,
                        },
                        "9MIN": {
                            "data": [
                                -0.0004175000058809917,
                                -0.0004175000058809917,
                                -0.0004175000058809917,
                            ],
                            "data_len": 540,
                            "data_start": datetime(
                                2026, 2, 27, 18, 33, 32, tzinfo=timezone.utc
                            ),
                            "instant": datetime(
                                2026, 2, 27, 18, 42, 32, tzinfo=timezone.utc
                            ),
                            "seconds": 540,
                            "usage": -26.098890160866922,
                        },
                        "10MIN": {
                            "data": [
                                -0.0004200000233385299,
                                -0.0004200000233385299,
                                -0.0004200000233385299,
                            ],
                            "data_len": 600,
                            "data_start": datetime(
                                2026, 2, 27, 18, 32, 32, tzinfo=timezone.utc
                            ),
                            "instant": datetime(
                                2026, 2, 27, 18, 42, 32, tzinfo=timezone.utc
                            ),
                            "seconds": 600,
                            "usage": -26.001501232385706,
                        },
                    },
                    "smoothing": {
                        "1MIN": -52.516668090260964,
                        "2MIN": -51.64333442724774,
                        "3MIN": -50.733611620855584,
                        "4MIN": -48.69583042713043,
                        "5MIN": -46.94916209864539,
                        "6MIN": -45.23888373739453,
                        "7MIN": -43.42463809829178,
                        "8MIN": -41.769204119694564,
                        "9MIN": -39.94308780227044,
                        "10MIN": -38.242027851465195,
                    },
                    "timezone": TIMEZONE,
                }
            ],
            "instant": datetime(2026, 2, 27, 18, 42, 34, 170162, tzinfo=timezone.utc),
        }
