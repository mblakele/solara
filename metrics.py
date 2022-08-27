"""
Call Emporia VUE API and marshal predicted usage.
"""
from datetime import datetime, timedelta, timezone

import json
import locale
import logging
import requests

from pyemvue import PyEmVue
from pyemvue.enums import Scale, Unit
from decouple import config

DEBUG = config('DEBUG', default='False', cast=bool)

# avoid need for `self.logger`
logger = logging.getLogger(__name__)

class RetryableMetricsException(Exception):
    """
    Use this exception class to signal that the Emporia VUE API
    responded with a server error and the controller should retry.
    """

class Metrics:
    """
    Metrics for predicting hourly usage,
    based on Emporia VUE Utility Connect data.
    """
    device_info = {}
    instant = None
    metrics = {}
    vue = PyEmVue()
    vue_keys = '.vue-keys.json'

    def __init__(self, logger_next=None):
        self.metrics = {
            'api_response': {},
            'debug': DEBUG,
            'devices': []
        }

        if logger_next is not None:
            global logger
            logger = logger_next

        logger.debug('init')
        self.vue_init()

        # take instant after any auth, to reduce data lag
        self.instant = datetime.now(timezone.utc)
        self.metrics['instant'] = self.instant

        # We only need to fetch this data once.
        if len(self.device_info) < 1:
            self.get_device_info()

        self.populate()
        self.predict()

        self.metrics['api_response']['total'] = sum(
            self.metrics['api_response'].values(), timedelta())

        logger.debug(self.device_info.keys())
        for gid, vdi in self.device_info.items():
            device_metrics = {
                'gid': gid,
                'lag': vdi.lag,
                'name': vdi.device_name,
                'minute_predicted': vdi.minute_predicted,
                'minutes_remaining': vdi.seconds_remaining / 60.0,
                'prediction': vdi.prediction,
                'prediction_min': vdi.prediction_min,
                'prediction_max': vdi.prediction_max,
                'scales': vdi.scales,
                'smoothing': vdi.smoothing,
                'timezone': vdi.time_zone,
            }
            self.metrics['devices'].append(device_metrics)

        logger.debug('reporting metrics for %d devices',
                     len(self.metrics['devices']))

    def get_device_info(self):
        """
        Wrapper for vue.get_devices,
        filtering results for ZIG001 devices.
        """
        rt_start = datetime.now(timezone.utc)

        try:
            devices = self.vue.get_devices()
        except:
            # Might be stale auth tokens, so force login on retry
            logger.logexception('invalidating auth tokens')
            self.vue.auth = None
            raise RetryableMetricsException('get_devices failed')

        self.metrics['api_response']['get_devices'] = (
            datetime.now(timezone.utc) - rt_start)

        for vdi in devices:
            if not vdi.connected:
                continue
            # Only recognize the zigbee utility connect,
            # not the other kind of Vue that lives in the panel.
            if not vdi.model == 'ZIG001':
                continue
            if not len(vdi.channels) > 0:
                continue
            if not vdi.device_gid in self.device_info:
                # not needed? we seem to have timezone anyway
                #vdi = vue.populate_device_properties(vdi)
                self.device_info[vdi.device_gid] = vdi

    def populate(self):
        """
        Fetch recent data. Use seconds to minimize lag.
        Seconds data usually lags by a few seconds, but sometimes longer.
        Fetch all seconds in current hour.
        Performance seems ok, and rounding seems ok.
        """
        chart_start = self.instant - timedelta(
            minutes=self.instant.minute,
            seconds=self.instant.second,
            microseconds=self.instant.microsecond)
        scale = Scale.SECOND.value
        for vdi in self.device_info.values():
            for chan in vdi.channels:
                rt_start = datetime.now(timezone.utc)
                usage_data, usage_data_start = self.vue.get_chart_usage(
                    chan, chart_start, self.instant,
                    scale=scale,
                    unit=Unit.KWH.value)
                if len(usage_data) < 1:
                    raise RetryableMetricsException("No data for hour")
                if usage_data[0] is None:
                    raise RetryableMetricsException("No data for hour")
                self.metrics['api_response'][
                    'get_chart_usage/' + str(chan.channel_num)] = (
                        datetime.now(timezone.utc) - rt_start)
                # hourly sum
                self.populate_scale(
                    chan, usage_data, usage_data_start, Scale.HOUR.value)
                # successively slice off the last x minutes of data
                usage_data_len = len(usage_data)
                usage_data_end = usage_data_start + timedelta(
                    seconds=usage_data_len)
                usage_minutes = min([10, max(1, usage_data_end.minute)])
                logger.debug({
                    'usage_data': usage_data[:3],
                    'usage_data_start': usage_data_start,
                    'usage_data_len': usage_data_len,
                    'usage_minutes': usage_minutes })
                for usm in range(1, 1 + usage_minutes):
                    # most recent minute(s) presented as xMIN scale
                    uss = 60 * usm
                    scale = str(usm) + 'MIN'
                    offset_data = usage_data[-uss:]
                    offset_start = usage_data_end - timedelta(minutes=usm)
                    self.populate_scale(
                        chan, offset_data, offset_start, scale)

    def populate_scale(self, channel, data, data_start, scale):
        """
        Populate N seconds of usage data for a scale
        of 1H or M minutes.
        """
        data_len = len(data)
        gid = channel.device_gid

        dig = self.device_info[gid]
        if not hasattr(dig, 'scales'):
            dig.scales = {}
        if not hasattr(dig.scales, scale):
            dig.scales[scale] = {}
        dsi = dig.scales[scale]

        if DEBUG:
            dsi['data'] = data[:3]
            dsi['data_len'] = data_len
            dsi['data_start'] = data_start
        logger.debug({
            'scale': scale,
            'data': data[:3],
            'data_len': data_len,
            'data_start': data_start })
        # sum all available data to hour so far
        # Convert from kWh to Wh while we are here.
        # There may be any number of seconds in data
        usage = 1000.0 * sum(data)
        if not scale == '1H':
            # seconds: scale to minutes
            usage = usage * 60.0 / data_len

        dsi['instant'] = data_start + timedelta(seconds=data_len)
        dsi['seconds'] = data_len
        dsi['usage'] = usage

    def predict(self):
        """Predict consumption or surplus at end of current hour."""
        for vdi in self.device_info.values():
            hour_next = self.instant + timedelta(hours=1) - timedelta(
                minutes=self.instant.minute,
                seconds=self.instant.second,
                microseconds=self.instant.microsecond)
            scales = vdi.scales
            hour = scales[Scale.HOUR.value]
            # hour instant is based on chart data, accounting for lag
            seconds_remaining = (hour_next - hour['instant']).total_seconds()

            # strategy: predict remaining hour from 1MIN
            minute_predicted = (
                seconds_remaining *
                scales[Scale.MINUTE.value]['usage'] / 60.0)
            prediction = hour['usage'] + minute_predicted

            # smoothing
            vdi.smoothing = {}
            prediction_min = prediction
            prediction_max = prediction
            for scale in scales.keys():
                if not scale.endswith('MIN'):
                    continue
                sval = hour['usage'] + (
                    seconds_remaining *
                    scales[scale]['usage'] / 60.0)
                if sval < prediction_min:
                    prediction_min = sval
                if sval > prediction_max:
                    prediction_max = sval
                vdi.smoothing[scale] = sval

            # enrich device_info with output
            vdi.lag = (
                self.instant - hour['instant']
                if hour['instant'] < self.instant
                else timedelta(0))
            vdi.minute_predicted = minute_predicted
            vdi.prediction = prediction
            vdi.prediction_max = prediction_max
            vdi.prediction_min = prediction_min
            vdi.seconds_remaining = seconds_remaining

    def vue_init(self):
        """
        Initialize access to Emporia VUE API.
        Prefer stored authentication token,
        falling back on username and password.

        TODO improve logging of exception data?
        """
        rt_start = datetime.now(timezone.utc)

        # are we already authenticated?
        if self.vue and hasattr(self.vue, 'auth'):
            return

        logger.debug('trying %s', self.vue_keys)
        try:
            encoding = locale.getpreferredencoding()
            vkf = open(self.vue_keys, encoding=encoding)
            with vkf:
                vkf_data = json.load(vkf)
                self.vue.login(id_token=vkf_data['id_token'],
                               access_token=vkf_data['access_token'],
                               refresh_token=vkf_data['refresh_token'],
                               token_storage_file=self.vue_keys)
        except (requests.exceptions.HTTPError, IOError):
            logger.exception('keys failed: will use password')
            self.vue.login(username=config('VUE_USERNAME'),
                           password=config('VUE_PASSWORD'),
                           token_storage_file=self.vue_keys)

        # TODO This is buggy: response 403 but pyemvue expects 404
        #downtime = self.vue.down_for_maintenance()
        #if downtime:
        #    raise RetryableMetricsException(downtime)

        self.metrics['api_response']['auth'] = (
            datetime.now(timezone.utc) - rt_start)


class MetricsMock:
    """
    Mock metrics data, for testing.
    """
    metrics = {}
    def __init__(self):
        self.metrics = {
            'api_response': {
                'get_chart_usage/1,2,3': timedelta(microseconds=750072),
                'total': timedelta(microseconds=750072)
            },
            'debug': True,
            'devices': [
                {
                    'gid': 12345,
                    'lag': timedelta(seconds=2, microseconds=170162),
                    'name': 'MOCK',
                    'minute_predicted': -468.43419509779943,
                    'minutes_remaining': 17.466666666666665,
                    'prediction': -52.516668090260964,
                    'prediction_min': -52.516668090260964,
                    'prediction_max': -38.242027851465195,
                    'scales': {
                        '1H': {
                            'data': [
                                0.0012375001112620038,
                                0.0012375001112620038,
                                0.0012299999926090241
                            ],
                            'data_len': 2552,
                            'data_start': datetime(2022, 8, 27, 18, 0, tzinfo=timezone.utc),
                            'instant': datetime(2022, 8, 27, 18, 42, 32, tzinfo=timezone.utc),
                            'seconds': 2552,
                            'usage': 415.91752700753847
                        },
                        '1MIN': {'data': [-0.0004437500238418579, -0.0004437500238418579, -0.0004437500238418579], 'data_len': 60, 'data_start': datetime(2022, 8, 27, 18, 41, 32, tzinfo=timezone.utc), 'instant': datetime(2022, 8, 27, 18, 42, 32, tzinfo=timezone.utc), 'seconds': 60, 'usage': -26.818751627736606},
                        '2MIN': {'data': [-0.00044500003258387253, -0.00044500003258387253, -0.00044500003258387253], 'data_len': 120, 'data_start': datetime(2022, 8, 27, 18, 40, 32, tzinfo=timezone.utc), 'instant': datetime(2022, 8, 27, 18, 42, 32, tzinfo=timezone.utc), 'seconds': 120, 'usage': -26.768751608861805},
                        '3MIN': {'data': [-0.0004450000325838725, -0.0004450000325838725, -0.0004450000325838725], 'data_len': 180, 'data_start': datetime(2022, 8, 27, 18, 39, 32, tzinfo=timezone.utc), 'instant': datetime(2022, 8, 27, 18, 42, 32, tzinfo=timezone.utc), 'seconds': 180, 'usage': -26.71666824208363},
                        '4MIN': {'data': [-0.00043750001319249474, -0.00043750001319249474, -0.00043750001319249474], 'data_len': 240, 'data_start': datetime(2022, 8, 27, 18, 38, 32, tzinfo=timezone.utc), 'instant': datetime(2022, 8, 27, 18, 42, 32, tzinfo=timezone.utc), 'seconds': 240, 'usage': -26.600001379847455},
                        '5MIN': {'data': [-0.0004375000132189857, -0.0004375000132189857, -0.0004375000132189857], 'data_len': 300, 'data_start': datetime(2022, 8, 27, 18, 37, 32, tzinfo=timezone.utc), 'instant': datetime(2022, 8, 27, 18, 42, 32, tzinfo=timezone.utc), 'seconds': 300, 'usage': -26.50000128470518},
                        '6MIN': {'data': [-0.0004300000270207724, -0.0004300000270207724, -0.0004300000270207724], 'data_len': 360, 'data_start': datetime(2022, 8, 27, 18, 36, 32, tzinfo=timezone.utc), 'instant': datetime(2022, 8, 27, 18, 42, 32, tzinfo=timezone.utc), 'seconds': 360, 'usage': -26.402084584633567},
                        '7MIN': {'data': [-0.0004300000270207724, -0.0004300000270207724, -0.0004300000270207724], 'data_len': 420, 'data_start': datetime(2022, 8, 27, 18, 35, 32, tzinfo=timezone.utc), 'instant': datetime(2022, 8, 27, 18, 42, 32, tzinfo=timezone.utc), 'seconds': 420, 'usage': -26.298215559494096},
                        '8MIN': {'data': [-0.00042000002337826625, -0.00042000002337826625, -0.00042500002516640556], 'data_len': 480, 'data_start': datetime(2022, 8, 27, 18, 34, 32, tzinfo=timezone.utc), 'instant': datetime(2022, 8, 27, 18, 42, 32, tzinfo=timezone.utc), 'seconds': 480, 'usage': -26.20343880499426},
                        '9MIN': {'data': [-0.0004175000058809917, -0.0004175000058809917, -0.0004175000058809917], 'data_len': 540, 'data_start': datetime(2022, 8, 27, 18, 33, 32, tzinfo=timezone.utc), 'instant': datetime(2022, 8, 27, 18, 42, 32, tzinfo=timezone.utc), 'seconds': 540, 'usage': -26.098890160866922},
                        '10MIN': {'data': [-0.0004200000233385299, -0.0004200000233385299, -0.0004200000233385299], 'data_len': 600, 'data_start': datetime(2022, 8, 27, 18, 32, 32, tzinfo=timezone.utc), 'instant': datetime(2022, 8, 27, 18, 42, 32, tzinfo=timezone.utc), 'seconds': 600, 'usage': -26.001501232385706}
                    },
                    'smoothing': {
                        '1MIN': -52.516668090260964,
                     '2MIN': -51.64333442724774,
                     '3MIN': -50.733611620855584,
                     '4MIN': -48.69583042713043,
                     '5MIN': -46.94916209864539,
                     '6MIN': -45.23888373739453,
                     '7MIN': -43.42463809829178,
                     '8MIN': -41.769204119694564,
                     '9MIN': -39.94308780227044,
                     '10MIN': -38.242027851465195},
                 'timezone': 'America/Los_Angeles'}],
            'instant': datetime(2022, 8, 27, 18, 42, 34, 170162, tzinfo=timezone.utc)
        }

# end
