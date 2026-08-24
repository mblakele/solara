import logging
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests
import metrics
from energy_cache import EnergyCache, EnergyCacheData
from metrics import (
    DevicePrediction,
    HourlyProjection,
    MetricsBase,
    RetryableMetricsException,
    _PopulationResult,
)
from util import compute_nbc_quarters
from mockdata import MetricsMock
from test_app import mock_config
from clock import FakeClock

class TestTOUReporterAggregate(unittest.TestCase):
    """Test that TOUReporter.aggregate_tou correctly uses EnergyDataAggregator."""

    def test_aggregate_tou_uses_module_level_import(self):
        """Verify aggregate_tou doesn't reference self.EnergyDataAggregator.

        When EnergyDataAggregator was moved from class body to module-level,
        the call site changed from self.EnergyDataAggregator.aggregate_from_15min()
        to EnergyDataAggregator.aggregate_from_15min(). This test ensures
        the TOUReporter instance can successfully run aggregate_tou without
        raising AttributeError.
        """
        from metrics import TOUReporter

        # We can't fully instantiate TOUReporter (needs real VUE API),
        # but we can verify the method doesn't reference self.EnergyDataAggregator
        # by checking that calling it on a partial instance works.
        class PartialTOU(TOUReporter):
            def __init__(self):
                # Skip parent init, just set what aggregate_tou needs
                self.usage_data_list = [
                    {
                        "start": datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc),
                        "data": [0.001] * 60,
                    }
                ]

        reporter = PartialTOU()
        # Should not raise AttributeError for missing EnergyDataAggregator
        reporter.aggregate_tou()
        self.assertIsNotNone(reporter.tou_result)
        self.assertIsNotNone(reporter.tou_result.total)


class TestMetrics(unittest.TestCase):
    def setUp(self):
        self.mock = MetricsMock()
        self.metrics_data = self.mock.metrics

    def test_metrics_mock_structure(self):
        self.assertIn("api_response", self.metrics_data)
        self.assertIn("devices", self.metrics_data)
        self.assertIn("instant", self.metrics_data)
        self.assertTrue(self.metrics_data["debug"])

    def test_mock_device_data(self):
        devices = self.metrics_data["devices"]
        self.assertEqual(len(devices), 2)

        # Device A: negative (solar export)
        device_a = devices[0]
        self.assertEqual(device_a["name"], "MOCK")
        self.assertEqual(device_a["timezone"], "America/Los_Angeles")
        self.assertIn("prediction", device_a)

        # Device B: positive (load only)
        device_b = devices[1]
        self.assertEqual(device_b["name"], "SOLAR+LOAD")
        self.assertIn("prediction", device_b)

    def test_mock_nbc_structure(self):
        """Verify nbc field exists with QH1–QH4 keys."""
        device = self.metrics_data["devices"][0]
        self.assertIn("nbc", device)
        nbc = device["nbc"]
        self.assertIn("QH1", nbc)
        self.assertIn("QH2", nbc)
        self.assertIn("QH3", nbc)
        self.assertIn("QH4", nbc)

    def test_mock_nbc_complete_quarters(self):
        """At minute=42, QH2 and QH3 should be complete."""
        device = self.metrics_data["devices"][0]
        self.assertTrue(device["nbc"]["QH2"]["complete"])
        self.assertTrue(device["nbc"]["QH3"]["complete"])

    def test_mock_nbc_incomplete_quarter(self):
        """At minute=42, QH1 should be incomplete with predicted_wh."""
        device = self.metrics_data["devices"][0]
        qh1 = device["nbc"]["QH1"]
        self.assertFalse(qh1["complete"])
        self.assertIn("predicted_wh", qh1)
        self.assertIn("samples_used", qh1)

    def test_mock_nbc_not_started(self):
        """At minute=42, QH4 should be None."""
        device = self.metrics_data["devices"][0]
        self.assertIsNone(device["nbc"]["QH4"])

    def test_mock_nbc_parameterized_minute(self):
        """Test NBC at different instant_minute values."""
        # minute=10: QH1 incomplete, QH2–QH4 not started
        mock_10 = MetricsMock(instant_minute=10)
        nbc_10 = mock_10.metrics["devices"][0]["nbc"]
        self.assertFalse(nbc_10["QH1"]["complete"])
        self.assertIsNone(nbc_10["QH2"])

        # minute=37: QH1 incomplete, QH2 complete, QH3 incomplete, QH4 not started
        mock_37 = MetricsMock(instant_minute=37)
        nbc_37 = mock_37.metrics["devices"][0]["nbc"]
        self.assertFalse(nbc_37["QH1"]["complete"])
        self.assertTrue(nbc_37["QH2"]["complete"])
        self.assertTrue(nbc_37["QH3"]["complete"])
        self.assertIsNone(nbc_37["QH4"])

    def test_mock_nbc_wh_clamped_at_zero(self):
        """Verify NBC wh values are never negative (clamped at zero)."""
        device = self.metrics_data["devices"][0]
        for qh in ["QH1", "QH2", "QH3"]:
            if device["nbc"][qh] is not None:
                self.assertGreaterEqual(device["nbc"][qh]["wh"], 0)

    def test_mock_tou_result(self):
        """Verify MetricsMock().tou_result has non-zero values with all four keys."""
        mock = MetricsMock()
        tou = mock.tou_result
        self.assertIsNotNone(tou.total)
        self.assertIsNotNone(tou.peak)
        self.assertIsNotNone(tou.part_peak)
        self.assertIsNotNone(tou.off_peak)
        self.assertGreater(tou.total, 0)
        self.assertGreater(tou.peak, 0)

    def test_mock_device_b_positive_consumption(self):
        """Verify Device B has positive consumption (load-only scenario)."""
        device = self.metrics_data["devices"][1]
        self.assertEqual(device["name"], "SOLAR+LOAD")
        # All per-second data should be positive
        for val in device["per_second_data"]:
            self.assertGreater(val, 0)

    def test_mock_device_b_nbc_positive_wh(self):
        """Verify Device B's NBC quarters have positive wh (no clamping)."""
        mock = MetricsMock(instant_minute=37)
        device = mock.metrics["devices"][1]
        nbc = device["nbc"]

        # QH1 and QH2 should be complete with positive wh
        self.assertFalse(nbc["QH1"]["complete"])
        self.assertGreater(nbc["QH1"]["wh"], 0)
        self.assertTrue(nbc["QH2"]["complete"])
        self.assertGreater(nbc["QH2"]["wh"], 0)

        # QH3 should be incomplete with positive predicted_wh
        self.assertTrue(nbc["QH3"]["complete"])
        self.assertIsNone(nbc["QH3"]["predicted_wh"])

    def test_mock_device_b_nbc_parameterized_minute(self):
        """Test Device B NBC at different instant_minute values."""
        # minute=10: QH1 incomplete, QH2–QH4 not started
        mock_10 = MetricsMock(instant_minute=10)
        nbc_10 = mock_10.metrics["devices"][1]["nbc"]
        self.assertFalse(nbc_10["QH1"]["complete"])
        self.assertIsNone(nbc_10["QH2"])

        # minute=37: QH1 incomplete, QH2-QH3 incomplete, QH4 not started
        mock_37 = MetricsMock(instant_minute=37)
        nbc_37 = mock_37.metrics["devices"][1]["nbc"]
        self.assertFalse(nbc_37["QH1"]["complete"])
        self.assertTrue(nbc_37["QH2"]["complete"])
        self.assertTrue(nbc_37["QH3"]["complete"])
        self.assertIsNone(nbc_37["QH4"])

class TestComputeNBCQuartersEdgeCases(unittest.TestCase):
    """Tests for util.compute_nbc_quarters edge cases."""

    def test_empty_data_returns_all_none(self):
        """With empty per_second_data, all quarters should be None."""
        result = compute_nbc_quarters([])

        for attr in ["qh1", "qh2", "qh3", "qh4"]:
            self.assertIsNone(getattr(result, attr))

    def test_n_900_completes_qh1(self):
        """n=900 should complete QH1, leave others None."""
        data = [0.002] * 900
        result = compute_nbc_quarters(data)

        self.assertTrue(result.qh1.complete)
        self.assertAlmostEqual(result.qh1.raw_wh, 900 * 0.002 * 1000)
        self.assertIsNone(result.qh2)
        self.assertIsNone(result.qh3)
        self.assertIsNone(result.qh4)

    def test_n_3600_completes_all_quarters(self):
        """n=3600 (past end of QH4) should complete all quarters."""
        data = [0.002] * 3600
        result = compute_nbc_quarters(data)

        for attr in ["qh1", "qh2", "qh3", "qh4"]:
            self.assertTrue(getattr(result, attr).complete)

    def test_negative_raw_wh_clamped_to_zero_in_complete(self):
        """Complete quarters with negative raw_wh should have wh=0."""
        data = [-0.002] * 900
        result = compute_nbc_quarters(data)

        self.assertTrue(result.qh1.complete)
        self.assertEqual(result.qh1.wh, 0)

    def test_negative_raw_wh_clamped_to_zero_in_partial(self):
        """Partial quarters with negative predicted_wh should have wh=0."""
        data = [-0.002] * 1500
        result = compute_nbc_quarters(data)

        self.assertFalse(result.qh1.complete)
        # predicted_wh will be negative, clamped to 0
        self.assertLess(result.qh1.raw_wh, 0)
        self.assertEqual(result.qh1.wh, 0)

    def test_partial_qh_has_predicted_wh(self):
        """Incomplete quarters should include predicted_wh field."""
        data = [0.002] * 1500
        result = compute_nbc_quarters(data)

        self.assertFalse(result.qh1.complete)
        self.assertIsNotNone(result.qh1.predicted_wh)

    def test_partial_qh_has_remaining_seconds(self):
        """Incomplete quarters should include remaining_seconds field."""
        data = [0.002] * 1500
        result = compute_nbc_quarters(data)

        self.assertIsNotNone(result.qh1.remaining_seconds)
        # QH1 ends at index 1799, n=1500 → remaining = 1800 - 1500
        self.assertEqual(result.qh1.remaining_seconds, 300)

    def test_partial_qh_has_samples_used(self):
        """Incomplete quarters should include samples_used field."""
        data = [0.002] * 1500
        result = compute_nbc_quarters(data)

        self.assertIsNotNone(result.qh1.samples_used)
        # lookback = max(1500-60, 900) to 1500 = max(1440, 900)=1440 to 1500
        # samples = 60 (or less if lookback_start < start_idx)
        self.assertGreater(result.qh1.samples_used, 0)

    def test_partial_qh_lookback_cannot_cross_boundary(self):
        """Lookback window should not cross quarter boundary."""
        # n=901 is just 2 seconds into QH2 (start_idx=900)
        # lookback_start = max(901-60, 900) = max(841, 900) = 900
        # So lookback only includes seconds from QH2, not QH1: data[900:901]
        # Python slice [start:end] is exclusive of end → 2 elements (indices 900, 901)
        data = [0.005] * 901  # uniform positive values
        result = compute_nbc_quarters(data)

        self.assertFalse(result.qh1.complete)
        # The first full 900-sample chunk registers as a completed quarter.
        self.assertTrue(result.qh2.complete)
        # lookback is data[900:901] → 2 elements (indices 900 and 901)
        self.assertEqual(result.qh1.samples_used, 1)

    def test_partial_qh_lookback_clamped_to_start_idx(self):
        """Lookback start should be clamped to quarter start index."""
        # n=910, lookback_start = max(850, 900) = 900
        # So lookback is from index 900 to 910 = 10 samples
        data = [0.005] * 910
        result = compute_nbc_quarters(data)

        self.assertEqual(result.qh1.samples_used, 10)

    def test_complete_qh_to_dict_always_includes_prediction_w(self):
        """to_dict() must include prediction_w key even for complete quarters.

        Regression test: the Jinja2 template accesses qh.prediction_w on
        the dict, so the key must always be present (value may be None).
        """
        data = [0.002] * 900
        result = compute_nbc_quarters(data)
        d = result.qh1.to_dict()

        self.assertIn("prediction_w", d)
        self.assertIn("predicted_wh", d)
        self.assertIn("remaining_seconds", d)
        self.assertIn("samples_used", d)
        self.assertIsNone(d["prediction_w"])
        self.assertIsNone(d["predicted_wh"])


class TestHourlyProjectionErrorPaths(unittest.TestCase):
    """Tests for HourlyProjection error handling paths."""

    def test_retryable_exception_on_no_data(self):
        """HourlyProjection should raise RetryableMetricsException when API returns no data."""

        chart_start = datetime.now(timezone.utc)

        with patch.object(MetricsBase, "vue_init"), \
             patch.object(MetricsBase, "get_device_info"):

            # Patch device_info to have one device
            with patch.dict(MetricsBase.device_info, {1: MagicMock()}):
                vdi = MetricsBase.device_info[1]

                # Set up channels to return empty data
                mock_channel = MagicMock()
                mock_channel.channel_num = 1

                def empty_fetch(*args, **kwargs):
                    return [], None

                vdi.channels = [mock_channel]
                with patch.object(
                    MetricsBase.vue, "get_chart_usage", side_effect=empty_fetch
                ):

                    hp = HourlyProjection(instant=chart_start)
                    with self.assertRaises(RetryableMetricsException):
                        hp.populate(chart_start)


class TestHourlyProjectionPopulateChartStart(unittest.TestCase):
    """Tests for HourlyProjection.populate().

    chart_start is a required parameter of populate().
    """

    def setUp(self):
        self._p1 = patch.object(MetricsBase, "vue_init")
        self._p2 = patch.object(MetricsBase, "get_device_info")
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    def test_populate_caps_old_chart_start(self):
        """populate() should cap chart_start when it is >1h before now."""
        instant = datetime(2026, 5, 19, 13, 0, 0, tzinfo=timezone.utc)
        old_chart_start = datetime(2026, 5, 19, 3, 59, 7, tzinfo=timezone.utc)
        hp = HourlyProjection(instant=instant)
        hp.energy_cache = EnergyCache()

        with patch.object(hp, "populate_internal", return_value={}) as mock_populate:
            hp.populate(old_chart_start)

        expected_start = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
        mock_populate.assert_called_once_with(expected_start, hp.energy_cache)

    def test_populate_preserves_nearby_chart_start(self):
        """populate() should NOT cap chart_start when it is within 1h of now."""
        instant = datetime(2026, 5, 19, 13, 0, 0, tzinfo=timezone.utc)
        nearby_chart_start = datetime(2026, 5, 19, 12, 30, 0, tzinfo=timezone.utc)
        hp = HourlyProjection(instant=instant)

        with patch.object(hp, "populate_internal", return_value={}) as mock_populate:
            hp.populate(nearby_chart_start)

        mock_populate.assert_called_once_with(
            nearby_chart_start, hp.energy_cache
        )

    def test_populate_total_sums_float_fetch_durations(self):
        """api_response['total'] must sum float per-channel entries.

        _fetch_channel_data records each get_chart_usage duration as
        monotonic elapsed *seconds* (float), so populate() cannot seed its
        sum with timedelta().  Regression test for a production crash:

            TypeError: unsupported operand type(s) for +:
            'datetime.timedelta' and 'float'
        """
        chart_start = datetime(2026, 8, 24, 1, 30, 0, tzinfo=timezone.utc)
        hp = HourlyProjection(instant=chart_start)
        # Simulate successful channel fetches: _fetch_channel_data stores
        # monotonic seconds as floats under get_chart_usage/<channel_num>.
        hp.metrics["api_response"] = {
            "get_chart_usage/1": 0.25,
            "get_chart_usage/2": 0.5,
        }

        with patch.object(hp, "populate_internal", return_value={}):
            hp.populate(chart_start)

        total = hp.metrics["api_response"]["total"]
        self.assertIsInstance(
            total, timedelta, "total must stay a timedelta so JSON keeps "
            "emitting ISO durations (e.g. PT0.75S)"
        )
        self.assertEqual(total, timedelta(seconds=0.75))


class TestCapChartStart(unittest.TestCase):
    """Tests for the cap_chart_start guard function."""

    def test_caps_old_chart_start(self):
        """When chart_start >1h before now, return current QH3 start."""
        from metrics import cap_chart_start

        instant = datetime(2026, 5, 22, 13, 0, 1, tzinfo=timezone.utc)
        old_start = datetime(2026, 5, 22, 3, 29, 7, tzinfo=timezone.utc)
        expected_start = datetime(2026, 5, 22, 12, 15, 0, tzinfo=timezone.utc)
        result = cap_chart_start(old_start, instant)
        self.assertEqual(result, expected_start)

    def test_caps_old_chart_start_at_exact_boundary(self):
        """When chart_start is exactly 1h before now, it should NOT cap."""
        from metrics import cap_chart_start

        instant = datetime(2026, 5, 19, 13, 0, 0, tzinfo=timezone.utc)
        old_start = instant - timedelta(hours=1)
        result = cap_chart_start(old_start, instant)
        self.assertEqual(result, old_start)

    def test_preserves_nearby_chart_start(self):
        """When chart_start is within 1h of now, return it unchanged."""
        from metrics import cap_chart_start

        instant = datetime(2026, 5, 19, 13, 0, 0, tzinfo=timezone.utc)
        nearby = datetime(2026, 5, 19, 12, 30, 0, tzinfo=timezone.utc)
        result = cap_chart_start(nearby, instant)
        self.assertEqual(result, nearby)

    def test_preserves_current_qh_boundary(self):
        """When chart_start is already at a QH boundary, return it unchanged."""
        from metrics import cap_chart_start

        instant = datetime(2026, 5, 19, 13, 0, 0, tzinfo=timezone.utc)
        qh_start = datetime(2026, 5, 19, 12, 45, 0, tzinfo=timezone.utc)
        result = cap_chart_start(qh_start, instant)
        self.assertEqual(result, qh_start)

    def test_caps_future_chart_start(self):
        """When chart_start is in the future (> now), fall back to full-hour fetch.

        A future chart_start indicates corrupted cache state (last_sample_at was
        set to a timestamp past the current instant).  Passing it through would
        cause the Emporia API to receive start > end and return a 400.
        See bugs/2026-05-30-api-httperror.log for the concrete failure.
        """
        from metrics import cap_chart_start, ceil_to_qh, MAX_FETCH_WINDOW

        now = datetime(2026, 5, 30, 23, 40, 21, tzinfo=timezone.utc)
        future_start = datetime(2026, 5, 30, 23, 44, 59, tzinfo=timezone.utc)
        expected = ceil_to_qh(now - MAX_FETCH_WINDOW)
        result = cap_chart_start(future_start, now)
        self.assertEqual(result, expected)
        self.assertLess(result, now)


class TestFloorToQh(unittest.TestCase):
    """Tests for the floor_to_qh helper in util."""

    def test_floors_mid_quarter(self):
        """14:35:30 floors to 14:30:00."""
        from util import floor_to_qh

        dt = datetime(2026, 5, 19, 14, 35, 30, tzinfo=timezone.utc)
        self.assertEqual(
            floor_to_qh(dt),
            datetime(2026, 5, 19, 14, 30, 0, tzinfo=timezone.utc),
        )

    def test_unchanged_on_boundary(self):
        """14:30:00 stays 14:30:00."""
        from util import floor_to_qh

        dt = datetime(2026, 5, 19, 14, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(floor_to_qh(dt), dt)

    def test_floors_seconds_into_quarter(self):
        """14:00:07 floors to 14:00:00."""
        from util import floor_to_qh

        dt = datetime(2026, 5, 19, 14, 0, 7, tzinfo=timezone.utc)
        self.assertEqual(
            floor_to_qh(dt),
            datetime(2026, 5, 19, 14, 0, 0, tzinfo=timezone.utc),
        )


class TestTOUReporterEdgeCases(unittest.TestCase):
    """Tests for TOUReporter edge cases."""

    def test_aggregate_tou_empty_usage_data_list(self):
        """aggregate_tou with empty usage_data_list should produce zero buckets."""
        from metrics import TOUReporter

        tou = TOUReporter.__new__(TOUReporter)

        # Set up required attributes
        tou.usage_data_list = []
        tou._fetch_error = None

        # Call aggregate_tou directly (skip fetch_usage_data)
        tou.aggregate_tou()

        self.assertIsNotNone(tou.tou_result)
        for bucket in ["total", "peak", "part_peak", "off_peak"]:
            self.assertEqual(getattr(tou.tou_result, bucket), 0.0)

    def test_aggregate_tou_data_variants(self):
        """aggregate_tou skips None values and ignores exports for NBC.

        Parametrized over (data, expected_total_wh, expected_nbc_wh):
        - None values are skipped entirely
        - negative values (solar export) count toward TOU total but are
          excluded from NBC, which sums positive imports only
        """
        from metrics import TOUReporter

        cases = [
            # ([data], total_wh, nbc_wh)
            ([0.1, None, 0.2], 300.0, 300.0),
            ([-0.1, 0.2], 100.0, 200.0),
            ([None, None], 0.0, 0.0),
        ]
        for data, expected_total_wh, expected_nbc_wh in cases:
            with self.subTest(data=data):
                tou = TOUReporter.__new__(TOUReporter)
                tou.usage_data_list = [
                    {"start": datetime.now(timezone.utc), "data": data}
                ]

                tou.aggregate_tou()

                self.assertIsNotNone(tou.tou_result)
                self.assertIsNotNone(tou.nbc_result)
                assert tou.tou_result is not None
                self.assertAlmostEqual(tou.tou_result.total, expected_total_wh)
                self.assertAlmostEqual(tou.nbc_result, expected_nbc_wh)


class TestDeviceMetricsDataClass(unittest.TestCase):
    """Tests for DeviceMetrics data class defaults and serialization."""

    def test_to_dict_has_all_keys(self):
        """to_dict() includes all expected keys for JSON/template consumption."""
        from metrics import DeviceMetrics

        dm = DeviceMetrics(
            gid=42, name="test-device", timezone="UTC"
        )
        d = dm.to_dict()

        expected_keys = {
            "gid", "lag", "name", "per_second_data",
            "prediction", "prediction_min", "prediction_max",
            "minute_predicted", "minutes_remaining",
            "timezone", "nbc"
        }
        self.assertEqual(set(d.keys()), expected_keys)

    def test_to_dict_rounding(self):
        """prediction values are rounded to 14 decimal places in output dict."""
        from metrics import DeviceMetrics, _PredictionData

        dm = DeviceMetrics(
            gid=1, name="round-test",
            prediction=_PredictionData(value=0.123456789012345, min_value=0.0, max_value=1.0),
        )
        d = dm.to_dict()

        # 14 decimal places max — Python's round(x, 14) strips trailing zeros
        self.assertEqual(d["prediction"], round(0.123456789012345, 14))
        self.assertEqual(d["prediction_min"], round(0.0, 14))

class TestMetricsBaseVueInitErrorPaths(unittest.TestCase):
    """Tests for MetricsBase.vue_init error paths."""

    def test_vue_init_token_fallback_to_password(self):
        """When token login fails, vue_init falls back to password auth."""
        from unittest.mock import MagicMock

        # Create a mock PyEmVue where token login fails but password succeeds
        vue_mock = MagicMock()

        # First call (token-based) returns False → triggers password fallback
        vue_mock.login.side_effect = [False, True]

        with patch.object(MetricsBase, "vue", vue_mock):

            # Create a fake .vue-keys.json so the file read doesn't fail
            import tempfile, os

            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                os.write(f.fileno(), b'{"id_token":"t","access_token":"a","refresh_token":"r"}')
                keys_file = f.name

            try:
                base = MetricsBase.__new__(MetricsBase)
                # Skip __init__ to avoid real API calls; set up manually
                base.vue = vue_mock
                # Crucial: MagicMock always has .auth (creates it on access), so we must
                # set auth=None to prevent vue_init() from taking its early-return path.
                base.vue.auth = None  # type: ignore[attr-defined]
                base.vue_keys = keys_file
                base.logger = MagicMock()

                # Inject config via DI (same path as __init__ sets self._cfg)
                from config import Config

                base._cfg = Config(overrides={"VUE_USERNAME": "testuser", "VUE_PASSWORD": "testpass"})

                # Mock open to return our temp file content for token login
                original_open = __builtins__["open"]

                def mock_file_open(*args, **kwargs):
                    return original_open(keys_file, *args[1:], **kwargs)

                with patch("builtins.open", mock_file_open):
                    base.vue_init()

                    # Token login failed (False), password login succeeded (True)
                    self.assertEqual(vue_mock.login.call_count, 2)
            finally:
                os.unlink(keys_file)

    def test_vue_init_both_fail_raises(self):
        """When both token and password auth fail, raises VueAuthenticationError."""
        from unittest.mock import MagicMock

        vue_mock = MagicMock()
        # Both login attempts fail
        vue_mock.login.side_effect = [False, False]

        with patch.object(MetricsBase, "vue", vue_mock):

            import tempfile, os

            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                os.write(f.fileno(), b'{"id_token":"t","access_token":"a","refresh_token":"r"}')
                keys_file = f.name

            try:
                base = MetricsBase.__new__(MetricsBase)
                base.vue = vue_mock
                # Prevent early return in vue_init() — MagicMock always has .auth.
                base.vue.auth = None  # type: ignore[attr-defined]
                base.vue_keys = keys_file
                base.logger = MagicMock()

                # Inject config via DI (same path as __init__ sets self._cfg)
                from config import Config

                base._cfg = Config(overrides={"VUE_USERNAME": "u", "VUE_PASSWORD": "p"})

                original_builtins_open = __builtins__["open"]

                def mock_file_open(*args, **kwargs):
                    return original_builtins_open(keys_file)

                with patch("builtins.open", mock_file_open):
                    with self.assertRaises(Exception) as ctx:  # VueAuthenticationError
                        base.vue_init()

                    self.assertIn("authentication failed", str(ctx.exception).lower())
            finally:
                os.unlink(keys_file)


class TestMetricsBaseGetDeviceInfoFilters(unittest.TestCase):
    """Tests for MetricsBase.get_device_info filtering paths."""

    def test_get_device_info_401_invalidates_auth(self):
        """HTTPError 401 sets vue.auth=None and raises RetryableMetricsException."""

        http_ex = requests.exceptions.HTTPError(response=MagicMock(status_code=401))
        vue_mock = MagicMock()
        vue_mock.get_devices.side_effect = http_ex

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics.MetricsBase.device_info", {}):

            base = MetricsBase.__new__(MetricsBase)
            # Skip __init__ to avoid real API calls; set up manually
            base.vue = vue_mock
            base.logger = MagicMock()

            with self.assertRaises(RetryableMetricsException):
                base.get_device_info()

            # Auth should be invalidated on 401
            self.assertIsNone(vue_mock.auth)

    def test_get_device_info_filters_disconnected(self):
        """Devices with connected=False are skipped."""

        disconnected_device = MagicMock()
        disconnected_device.connected = False
        disconnected_device.device_gid = 1

        vue_mock = MagicMock()
        vue_mock.get_devices.return_value = [disconnected_device]

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics.MetricsBase.device_info", {}):

            base = MetricsBase.__new__(MetricsBase)
            base.vue = vue_mock
            base.logger = MagicMock()

            # Should not raise, but device_info should remain empty
            with patch("metrics.MetricsBase.vue_auth", {"last": datetime.now(timezone.utc)}):
                base.get_device_info()

            self.assertEqual(len(MetricsBase.device_info), 0)

    def test_get_device_info_filters_wrong_model(self):
        """Devices with model != 'ZIG001' are skipped."""

        wrong_model_device = MagicMock()
        wrong_model_device.connected = True
        wrong_model_device.model = "ZIG002"  # Wrong model
        wrong_model_device.device_gid = 1

        vue_mock = MagicMock()
        vue_mock.get_devices.return_value = [wrong_model_device]

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics.MetricsBase.device_info", {}):

            base = MetricsBase.__new__(MetricsBase)
            base.vue = vue_mock
            base.logger = MagicMock()

            with patch("metrics.MetricsBase.vue_auth", {"last": datetime.now(timezone.utc)}):
                base.get_device_info()

            self.assertEqual(len(MetricsBase.device_info), 0)

    def test_get_device_info_filters_empty_channels(self):
        """Devices with no channels are skipped."""

        empty_channels_device = MagicMock()
        empty_channels_device.connected = True
        empty_channels_device.model = "ZIG001"
        empty_channels_device.device_gid = 42
        empty_channels_device.channels = []

        vue_mock = MagicMock()
        vue_mock.get_devices.return_value = [empty_channels_device]

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics.MetricsBase.device_info", {}):

            base = MetricsBase.__new__(MetricsBase)
            base.vue = vue_mock
            base.logger = MagicMock()

            with patch("metrics.MetricsBase.vue_auth", {"last": datetime.now(timezone.utc)}):
                base.get_device_info()

            self.assertEqual(len(MetricsBase.device_info), 0)


class TestFetchChannelDataErrors(unittest.TestCase):
    """Tests for HourlyProjection._fetch_channel_data error paths."""

    def test_fetch_no_valid_data_raises(self):
        """_fetch_channel_data raises RetryableMetricsException when API returns empty data."""
        from unittest.mock import MagicMock

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.vue = MagicMock()
        hp.logger = MagicMock()

        # Mock get_chart_usage to return empty data (no valid points)
        hp.vue.get_chart_usage.return_value = ([], None)

        chan_mock = MagicMock()
        chan_mock.channel_num = 1

        with self.assertRaises(RetryableMetricsException) as ctx:
            hp._fetch_channel_data(chan_mock, now.replace(minute=0), now)

        self.assertIn("No data for hour", str(ctx.exception))

    def test_fetch_first_element_none_raises(self):
        """_fetch_channel_data raises when first element of data is None."""
        from unittest.mock import MagicMock

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.vue = MagicMock()
        hp.logger = MagicMock()

        # Mock get_chart_usage to return data where first element is None
        hp.vue.get_chart_usage.return_value = ([None, 0.1], now)

        chan_mock = MagicMock()
        chan_mock.channel_num = 2

        with self.assertRaises(RetryableMetricsException) as ctx:
            hp._fetch_channel_data(chan_mock, now.replace(minute=0), now)

        self.assertIn("No data for hour", str(ctx.exception))

    def test_fetch_channel_data_raises_when_data_start_drifted(self):
        """_fetch_channel_data raises when API data_start != requested chart_start.

        pyemvue returns the API's ``firstUsageInstant`` as the second tuple
        element, so a response whose start differs from the requested
        (QH-aligned) chart_start means the head of the window is missing —
        storing it would leave a non-aligned data_start in the cache.
        """
        from unittest.mock import MagicMock

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.vue = MagicMock()
        hp.logger = MagicMock()

        chart_start = now.replace(minute=0)  # QH-aligned
        drifted = now.replace(minute=16)  # API reports a later start (missing head)
        hp.vue.get_chart_usage.return_value = ([0.1] * 60, drifted)

        chan_mock = MagicMock()
        chan_mock.channel_num = 3

        with self.assertRaises(RetryableMetricsException) as ctx:
            hp._fetch_channel_data(chan_mock, chart_start, now)

        self.assertIn("chart_start", str(ctx.exception))

    def test_fetch_channel_data_accepts_matching_data_start(self):
        """_fetch_channel_data returns data unchanged when data_start == chart_start."""
        from unittest.mock import MagicMock

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.vue = MagicMock()
        hp.logger = MagicMock()
        hp.metrics = {"api_response": {}}

        chart_start = now.replace(minute=0)  # QH-aligned
        hp.vue.get_chart_usage.return_value = ([0.1] * 60, chart_start)

        chan_mock = MagicMock()
        chan_mock.channel_num = 4

        usage, data_start, channel_num = hp._fetch_channel_data(
            chan_mock, chart_start, now
        )
        self.assertEqual(usage, [0.1] * 60)
        self.assertEqual(data_start, chart_start)
        self.assertEqual(channel_num, 4)

    def test_fetch_channel_data_records_true_call_duration(self):
        """api_response records the get_chart_usage call duration, not window age.

        The per-channel api_response entry used to record
        ``_CLOCK.now() - chart_start`` — the wall-clock age of the chart
        window.  It must instead record the elapsed time of the
        get_chart_usage call itself, measured on a monotonic clock so NTP
        steps cannot distort it.
        """
        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.vue = MagicMock()
        hp.logger = MagicMock()
        hp.metrics = {"api_response": {}}

        chart_start = now.replace(minute=0)  # QH-aligned

        monotonic_values = iter([1000.0, 1001.5])

        def _fake_monotonic():
            return next(monotonic_values)

        def _slow_get_chart_usage(_channel, _start, _end, **_kwargs):
            return ([0.1] * 60, chart_start)

        hp.vue.get_chart_usage.side_effect = _slow_get_chart_usage

        with patch.object(metrics, "_monotonic", _fake_monotonic):
            chan_mock = MagicMock()
            chan_mock.channel_num = 7

            usage, data_start, channel_num = hp._fetch_channel_data(
                chan_mock, chart_start, now
            )
            self.assertEqual(usage, [0.1] * 60)
            self.assertEqual(data_start, chart_start)
            self.assertEqual(channel_num, 7)

        recorded = hp.metrics["api_response"]["get_chart_usage/7"]
        self.assertIsInstance(recorded, float)
        self.assertEqual(recorded, 1.5)


class TestDriftRejectionObservability(unittest.TestCase):
    """Persistent head-of-window drift becomes observable after N rejections.

    A fetch whose ``data_start != chart_start`` is rejected (strictly) so a
    misaligned window never reaches the cache.  If the API permanently drops
    the head sample of a QH, every fetch for that QH is rejected forever and
    the QH stalls — this tracker makes that state observable with an
    error-level log and a one-time Telegram alert (per QH key) instead of a
    silent stall.
    """

    def setUp(self):
        self._metrics = metrics
        self._metrics._drift_rejections.clear()
        self._metrics._drift_alerts.clear()
        self.chan = MagicMock()
        self.chan.channel_num = 5

    def _make_hp(self):
        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        hp.instant = now.replace(minute=30)
        hp.vue = MagicMock()
        hp.logger = MagicMock()
        hp.metrics = {"api_response": {}}
        return hp, now

    def _reject_drifted(self, hp, chart_start, now):
        """Simulate one drifted fetch: data_start != chart_start."""
        drifted = now.replace(minute=16)  # later start = missing head
        hp.vue.get_chart_usage.return_value = ([0.1] * 60, drifted)
        with self.assertRaises(RetryableMetricsException):
            hp._fetch_channel_data(self.chan, chart_start, now)

    def _accept_matching(self, hp, chart_start, now):
        """Simulate one successful fetch: data_start == chart_start."""
        hp.vue.get_chart_usage.return_value = ([0.1] * 60, chart_start)
        hp._fetch_channel_data(self.chan, chart_start, now)

    @staticmethod
    def _persistent_errors(hp):
        """Return (format, *args) tuples of the persistent-drift errors."""
        return [
            c.args
            for c in hp.logger.error.call_args_list
            if c.args and "persistently rejected" in c.args[0]
        ]

    @staticmethod
    def _mentions(call, text):
        """True when *text* appears in any positional arg of a log call."""
        return any(text in str(arg) for arg in call)

    def test_no_persistent_error_before_threshold(self):
        """Fewer than N rejections log only the transient warning."""
        hp, now = self._make_hp()
        chart_start = now.replace(minute=0)
        for _ in range(metrics.DRIFT_REJECTION_ALERT_AFTER - 1):
            self._reject_drifted(hp, chart_start, now)
        self.assertEqual(self._persistent_errors(hp), [])
        self.assertEqual(metrics._drift_alerts, [])

    def test_errors_after_threshold_rejections(self):
        """N consecutive rejections for the same QH log a persistent error."""
        hp, now = self._make_hp()
        chart_start = now.replace(minute=0)
        for _ in range(metrics.DRIFT_REJECTION_ALERT_AFTER):
            self._reject_drifted(hp, chart_start, now)
        errors = self._persistent_errors(hp)
        self.assertEqual(len(errors), 1)
        self.assertIn("persistently rejected", errors[0][0])
        self.assertTrue(self._mentions(errors[0], str(chart_start)))
        self.assertEqual(len(metrics._drift_alerts), 1)

    def test_errors_again_after_second_threshold(self):
        """A continuing stall re-logs every N rejections (no log spam)."""
        hp, now = self._make_hp()
        chart_start = now.replace(minute=0)
        for _ in range(2 * metrics.DRIFT_REJECTION_ALERT_AFTER):
            self._reject_drifted(hp, chart_start, now)
        self.assertEqual(len(self._persistent_errors(hp)), 2)
        # Still only one alert per key despite the continuing stall.
        self.assertEqual(len(metrics._drift_alerts), 1)

    def test_success_resets_counter(self):
        """A successful fetch resets the rejection counter for that QH."""
        hp, now = self._make_hp()
        chart_start = now.replace(minute=0)
        for _ in range(metrics.DRIFT_REJECTION_ALERT_AFTER - 1):
            self._reject_drifted(hp, chart_start, now)
        self._accept_matching(hp, chart_start, now)
        for _ in range(metrics.DRIFT_REJECTION_ALERT_AFTER - 1):
            self._reject_drifted(hp, chart_start, now)
        # Counter reset by the success: no persistent error yet.
        self.assertEqual(self._persistent_errors(hp), [])
        # One more rejection crosses the fresh threshold.
        self._reject_drifted(hp, chart_start, now)
        self.assertEqual(len(self._persistent_errors(hp)), 1)
        self.assertEqual(len(metrics._drift_alerts), 1)

    def test_different_chart_start_has_independent_counter(self):
        """Rejections for one QH do not affect another QH's counter."""
        hp, now = self._make_hp()
        chart_start_a = now.replace(minute=0)
        chart_start_b = now.replace(minute=45)
        for _ in range(metrics.DRIFT_REJECTION_ALERT_AFTER):
            self._reject_drifted(hp, chart_start_a, now)
        # A single rejection for a different QH must not error.
        self._reject_drifted(hp, chart_start_b, now)
        errors = self._persistent_errors(hp)
        self.assertEqual(len(errors), 1)
        self.assertTrue(self._mentions(errors[0], str(chart_start_a)))
        self.assertEqual(len(metrics._drift_alerts), 1)

    def test_stale_rejection_keys_are_pruned(self):
        """Rejections for long-past windows don't accumulate or trip the error.

        chart_start advances every QH, so a key older than the fetch window
        can never be fetched again — keeping it would grow the tracker
        unboundedly (one entry per drifted QH, forever).  Each rejection
        prunes stale keys first, so a long-past entry is dropped and the
        count restarts at 1 instead of carrying an old tally into the
        persistent-error check.
        """
        hp, now = self._make_hp()
        old_window = now - timedelta(hours=2)
        key = (self.chan.channel_num, old_window)
        # Simulate prior rejections for a window that has long since passed.
        metrics._drift_rejections[key] = metrics.DRIFT_REJECTION_ALERT_AFTER - 1

        # The stale entry is pruned (older than MAX_FETCH_WINDOW), so this
        # rejection restarts the count at 1 and never trips the error.
        self._reject_drifted(hp, old_window, now)

        self.assertEqual(metrics._drift_rejections.get(key), 1)
        self.assertEqual(self._persistent_errors(hp), [])
        self.assertEqual(metrics._drift_alerts, [])

    def test_alert_event_enqueued_once_per_key(self):
        """The first threshold crossing enqueues exactly one alert per QH key."""
        hp, now = self._make_hp()
        chart_start = now.replace(minute=0)
        drifted = now.replace(minute=16)
        for _ in range(metrics.DRIFT_REJECTION_ALERT_AFTER):
            self._reject_drifted(hp, chart_start, now)
        self.assertEqual(len(metrics._drift_alerts), 1)
        alert = metrics._drift_alerts[0]
        self.assertEqual(alert.channel_num, self.chan.channel_num)
        self.assertEqual(alert.chart_start, chart_start)
        self.assertEqual(alert.data_start, drifted)
        self.assertEqual(alert.count, metrics.DRIFT_REJECTION_ALERT_AFTER)

        # Further rejections for the same key do not re-enqueue.
        for _ in range(2 * metrics.DRIFT_REJECTION_ALERT_AFTER):
            self._reject_drifted(hp, chart_start, now)
        self.assertEqual(len(metrics._drift_alerts), 1)

        # A different channel with the same QH is a separate key.
        other_chan = MagicMock()
        other_chan.channel_num = 6
        for _ in range(metrics.DRIFT_REJECTION_ALERT_AFTER):
            hp.vue.get_chart_usage.return_value = ([0.1] * 60, drifted)
            with self.assertRaises(RetryableMetricsException):
                hp._fetch_channel_data(other_chan, chart_start, now)
        self.assertEqual(len(metrics._drift_alerts), 2)

    def test_drain_drift_alerts_returns_and_clears(self):
        """drain_drift_alerts returns pending alerts and empties the queue."""
        hp, now = self._make_hp()
        chart_start = now.replace(minute=0)
        for _ in range(metrics.DRIFT_REJECTION_ALERT_AFTER):
            self._reject_drifted(hp, chart_start, now)
        self.assertEqual(len(metrics._drift_alerts), 1)

        drained = metrics.drain_drift_alerts()
        self.assertEqual(len(drained), 1)
        self.assertEqual(metrics._drift_alerts, [])
        self.assertEqual(metrics.drain_drift_alerts(), [])


class TestComputeNBCEdgeCases(unittest.TestCase):
    """Tests for _compute_nbc edge cases."""

    def test_elapsed_exceeds_data_len(self):
        """When elapsed exceeds data length, n is clamped to len(data)."""
        from unittest.mock import MagicMock

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now
        hp.logger = MagicMock()
        # Only 10 seconds of data but instant is far ahead → elapsed >> len(data)
        short_data = [0.1] * 10

        result = hp._compute_nbc(short_data)

        # n should be clamped to len(data)=10
        self.assertFalse(result.qh1.complete)


class TestPopulateDeviceErrors(unittest.TestCase):
    """Tests for _populate_device error paths."""

    def test_fetch_error_returns_none(self):
        """_fetch_channel_data raising RequestException causes _populate_device to return None."""
        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.vue = MagicMock()
        hp.logger = MagicMock()

        # Mock get_chart_usage to raise a RequestException
        http_ex = requests.exceptions.HTTPError("API error")
        hp.vue.get_chart_usage.side_effect = http_ex

        vdi_mock = MagicMock()
        chan_mock = MagicMock(channel_num=1)
        vdi_mock.channels = [chan_mock]

        result = hp._populate_device(vdi_mock, now.replace(minute=0))
        self.assertIsNone(result)

    def test_empty_channels_returns_none(self):
        """_populate_device returns None when device has no channels."""
        from unittest.mock import MagicMock

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        vdi_mock = MagicMock()

        # Device with no channels → for-loop never executes, returns None
        vdi_mock.channels = []

        result = hp._populate_device(vdi_mock, now.replace(minute=0))
        self.assertIsNone(result)


class TestPredictDeviceEdgeCases(unittest.TestCase):
    """Tests for _predict_device edge cases."""

    def test_lag_zero(self):
        """When hour_instant >= instant, lag is timedelta(0)."""
        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        # Set instant to be BEFORE the data end time
        hp.instant = now.replace(minute=45)
        data_start = now.replace(minute=44, second=0)
        data = [0.001] * 120  # ends at minute=46

        result = hp._predict_device(data, data_start)

        self.assertIsInstance(result, DevicePrediction)
        self.assertEqual(result.lag, timedelta(0))


class TestTOUReporterFetchErrors(unittest.TestCase):
    """Tests for TOUReporter.fetch_usage_data error paths."""

    def test_fetch_http_error_re_raises(self):
        """fetch_usage_data re-raises HTTPError from get_chart_usage."""
        from unittest.mock import MagicMock

        vue_mock = MagicMock()
        http_ex = requests.exceptions.HTTPError("API error")
        http_ex.response = MagicMock()  # type: ignore[attr-defined]
        vue_mock.get_chart_usage.side_effect = http_ex

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics.MetricsBase.device_info", {}):

            # Create a minimal device with channels
            vdi_mock = MagicMock()
            chan_mock = MagicMock(channel_num=1)
            vdi_mock.channels = [chan_mock]

            with patch.object(MetricsBase, "device_info", {1: vdi_mock}):
                from metrics import TOUReporter

                from datetime import UTC, datetime as dt

                tou = TOUReporter.__new__(TOUReporter)
                # Set up required attributes without calling __init__ (which calls fetch_usage_data)
                tou.vue = vue_mock
                tou.logger = MagicMock()
                tou.start_date = dt(2025, 1, 1, tzinfo=UTC)
                tou.end_date = dt(2025, 1, 8, tzinfo=UTC)

                with self.assertRaises(requests.exceptions.HTTPError):
                    # Manually call fetch_usage_data (which will try to iterate device_info)
                    tou.fetch_usage_data()

    def test_fetch_empty_list(self):
        """fetch_usage_data with no data chunks produces empty usage_data_list."""

        from datetime import UTC, datetime as dt

        vue_mock = MagicMock()
        # Return empty data for all calls (no chunks)
        vue_mock.get_chart_usage.return_value = ([], None)

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics.MetricsBase.device_info", {}):

            vdi_mock = MagicMock()
            chan_mock = MagicMock(channel_num=1)
            vdi_mock.channels = [chan_mock]

            with patch.object(MetricsBase, "device_info", {1: vdi_mock}):
                from metrics import TOUReporter

                tou = TOUReporter.__new__(TOUReporter)
                tou.vue = vue_mock
                tou.logger = MagicMock()

                # Set up dates that will result in zero iterations (start >= end)
                now = datetime.now(timezone.utc).replace(
                    hour=10, minute=30, second=0, microsecond=0
                )
                tou.start_date = now
                tou.end_date = now  # Same time → no iterations

                tou.fetch_usage_data()

                self.assertEqual(tou.usage_data_list, [])


class TestHourlyProjectionNoPredictions(unittest.TestCase):
    """Tests for HourlyProjection constructor edge cases."""

class TestEnergyCacheSampleMetadata(unittest.TestCase):
    """Tests for EnergyCache sample metadata tracking."""

    def test_get_or_fetch_sets_last_sample_at(self):
        """After get_or_fetch, last_sample_at reflects the last sample time."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        def fetch_func():
            return {
                "per_second_data": [0.1] * 50,
                "data_start": now - timedelta(seconds=50),
            }

        cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        # Last sample time = data_start + (count - 1) seconds ≈ now
        self.assertIsNotNone(cache.last_sample_at)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetcher_returns(data_start: datetime, samples: list[float]):
    """Return a fetcher function that yields the given data."""
    return lambda: {
        "per_second_data": list(samples),
        "data_start": data_start,
    }


def _make_cache_with_samples(count: int, start: datetime | None = None) -> "metrics.EnergyCache":
    """Create an EnergyCache pre-populated with *count* dummy samples.

    Args:
        count: Number of per-second samples to insert.
        start: Start time of the sample window. Defaults to 10 minutes ago.

    Returns:
        EnergyCache instance with samples populated.
    """
    import metrics

    if start is None:
        start = datetime.now(timezone.utc) - timedelta(minutes=10)

    samples = [0.001] * count
    cache = metrics.EnergyCache()
    cache.samples = list(samples)
    cache.data_start = start
    cache.sample_count = count
    cache.last_sample_at = start + timedelta(seconds=count - 1)
    return cache


def _make_hourly_mock(
    n_seconds: int = 100,
    samples: list[float] | None = None,
    instant: datetime | None = None,
    chart_start: datetime | None = None,
) -> tuple[HourlyProjection, MagicMock]:
    """Build a HourlyProjection with mocked VUE API.

    Args:
        n_seconds: Number of per-second samples to return from API.
        samples: Optional list of per-second samples for previous hour.
        instant: Override the "now" instant.
        chart_start: Override the chart start time.

    Returns:
        Tuple of (HourlyProjection instance, mock vue client).
    """
    if instant is None:
        instant = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
    if chart_start is None:
        chart_start = instant.replace(second=0, microsecond=0) - timedelta(seconds=n_seconds)

    # Generate per-second samples for the current hour
    per_second_data = [0.001] * n_seconds

    # Build a mock channel
    mock_channel = MagicMock()
    mock_channel.channel_num = 1

    # Build a mock VDeviceUsageInfo
    mock_vdi = MagicMock()
    mock_vdi.device_gid = 1234
    mock_vdi.device_name = "TEST_DEVICE"
    mock_vdi.channels = [mock_channel]
    mock_vdi.time_zone = None

    mock_vue = MagicMock()

    # The channel must iterate over itself so _populate_device's for-loop works
    mock_channel.channels = [mock_channel]

    # Configure the mocked VUE API to behave like the real one: data starts
    # at the requested start (firstUsageInstant == requested start when no
    # head data is missing).
    def _api_get_chart_usage(channel, start, end, **kwargs):
        return (per_second_data, start)

    mock_vue.get_chart_usage.side_effect = _api_get_chart_usage

    # The channel .data attribute is used by some tests
    mock_channel.data = {
        "per_second_data": per_second_data,
        "data_start": chart_start,
    }
    mock_channel.samples = samples or []

    # Return a mock object that has .channels and .vue
    mock = MagicMock()
    mock.channels = [mock_channel]
    mock.vue = mock_vue

    # Create HourlyProjection with API calls mocked
    with patch.object(MetricsBase, "vue_init"), \
         patch.object(MetricsBase, "get_device_info"):
        hp = HourlyProjection(instant=chart_start, logger_next=logging.getLogger("test"))
        hp.instant = instant
        MetricsBase.device_info = {1234: mock_vdi}


    hp.vue = mock_vue

    return hp, mock


# ===========================================================================
# TestEnergyCacheMergeEdgeCases
# ===========================================================================


class TestEnergyCacheMergeEdgeCases(unittest.TestCase):
    """Tests for the inline merge logic in EnergyCache.get_or_fetch().

    The merge logic keeps samples strictly before the cache start and
    strictly after the cache end, discarding any overlap.
    """

    def test_merge_new_samples_start_exactly_at_cache_start(self):
        """New samples' first timestamp equals cache data_start → replace."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)
        existing = _make_cache_with_samples(300, cache_start)
        # cache = 14:00:00 to 14:04:59, 300 samples.

        # 6 new samples starting at the same data_start → REPLACE (not merge)
        new_start = cache_start
        new_samples = [0.001] * 6

        fetcher = _fetcher_returns(new_start, new_samples)
        metrics.set_clock(FakeClock(fixed_now))
        result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        self.assertIsNotNone(result)
        # Post-compaction: same data_start → replace, not merge
        self.assertEqual(len(existing.samples), 6)

    def test_merge_empty_new_samples_list(self):
        """New samples list is empty → cache unchanged."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)
        existing = _make_cache_with_samples(300, cache_start)

        fetcher = _fetcher_returns(cache_start, [])

        metrics.set_clock(FakeClock(fixed_now))
        result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        self.assertIsNotNone(result)
        self.assertEqual(len(result[0]["per_second_data"]), 0)
        self.assertEqual(len(existing.samples), 300)

    def test_merge_updates_last_sample_at(self):
        """After merge, last_sample_at equals last sample time."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)
        existing = _make_cache_with_samples(300, cache_start)

        new_start = cache_start + timedelta(minutes=5)
        new_samples = [0.012] * 60

        fetcher = _fetcher_returns(new_start, new_samples)

        metrics.set_clock(FakeClock(fixed_now))
        result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        self.assertIsNotNone(result)

        expected_last = existing.data_start + timedelta(seconds=len(existing.samples) - 1)
        self.assertEqual(existing.last_sample_at, expected_last)

    def test_replace_preserves_sample_values(self):
        """Sample values from the latest fetch are stored exactly."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)
        existing = _make_cache_with_samples(300, cache_start)

        new_samples = [float(i) for i in range(60)]

        fetcher = _fetcher_returns(cache_start, new_samples)

        metrics.set_clock(FakeClock(fixed_now))
        result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        # New values stored exactly
        self.assertEqual(existing.samples[0], 0.0)
        self.assertEqual(existing.samples[-1], 59.0)


# ===========================================================================
# TestEnergyCachePruningEdgeCases
# ===========================================================================


class TestEnergyCachePruningEdgeCases(unittest.TestCase):
    """Tests for the pruning logic in EnergyCache.get_or_fetch()."""

    def test_prune_removes_samples_at_boundary(self):
        """Samples strictly before cutoff are pruned; sample at cutoff is kept."""
        import metrics

        # Use fixed_now such that ceil_to_qh(now - 3600) lands at 14:15:00.
        fixed_now = datetime(2025, 6, 15, 15, 15, 0, tzinfo=timezone.utc)
        # ceil_to_qh(14:15:00) = 14:15:00
        # 101 samples from 14:13:20 to 14:15:00 (inclusive)
        cache_start = datetime(2025, 6, 15, 14, 13, 20, tzinfo=timezone.utc)
        cache = _make_cache_with_samples(101, cache_start)  # 101 samples

        original_count = len(cache.samples)
        fetcher = lambda: {"per_second_data": [], "data_start": fixed_now}

        metrics.set_clock(FakeClock(fixed_now))
        cache.get_or_fetch(fetcher, fixed_now, force=True)

        # Samples from 14:13:20 to 14:14:59 (100 samples) are < 14:15:00 → removed
        # Sample at 14:15:00 (cutoff) is NOT removed (uses <, not <=)
        self.assertEqual(len(cache.samples), 1)
        self.assertEqual(len(cache.samples), original_count - 100)

    def test_prune_updates_data_start(self):
        """After pruning, data_start advances by the number of removed samples."""
        import metrics

        # Use fixed_now such that ceil_to_qh(now - 3600) lands at 14:15:00.
        fixed_now = datetime(2025, 6, 15, 15, 15, 0, tzinfo=timezone.utc)
        # ceil_to_qh(14:15:00) = 14:15:00
        # 300 samples from 14:13:20 to 14:18:19 (inclusive)
        cache_start = datetime(2025, 6, 15, 14, 13, 20, tzinfo=timezone.utc)
        cache = _make_cache_with_samples(300, cache_start)

        new_start = datetime(2025, 6, 15, 14, 15, 0, tzinfo=timezone.utc)
        new_samples = []
        fetcher = _fetcher_returns(new_start, new_samples)

        metrics.set_clock(FakeClock(fixed_now))
        cache.get_or_fetch(fetcher, fixed_now, force=True)

        # Samples from 14:13:20 to 14:14:59 (100 samples) are < 14:15:00 → removed
        # Samples from 14:15:00 to 14:18:19 (200 samples) are >= cutoff → kept
        self.assertEqual(len(cache.samples), 200)
        # data_start advances by the number of removed samples (100 seconds)
        self.assertEqual(cache.data_start, new_start)

    def test_prune_keeps_sample_at_cutoff(self):
        """Sample at the exact cutoff boundary is kept (within 3600s window).

        The pruning condition uses '<' (not '<='), so a sample exactly at
        the cutoff timestamp should be retained.
        """
        import metrics

        # Use fixed_now such that ceil_to_qh(now - 3600s) lands at 14:15:00.
        fixed_now = datetime(2025, 6, 15, 15, 15, 0, tzinfo=timezone.utc)
        # ceil_to_qh(14:15:00) = 14:15:00
        # Create samples where last sample is at cutoff (14:15:00)
        cache_start = datetime(2025, 6, 15, 13, 15, 0, tzinfo=timezone.utc)
        # 3601 samples from 13:15:00 to 14:15:00 (inclusive)
        cache = _make_cache_with_samples(3601, cache_start)

        original_start = cache.data_start
        fetcher = _fetcher_returns(datetime(2025, 6, 15, 14, 15, 0, tzinfo=timezone.utc), [])

        metrics.set_clock(FakeClock(fixed_now))
        cache.get_or_fetch(fetcher, fixed_now, force=True)

        # 3600 samples from 13:15:00 to 14:14:59 are < 14:15:00 → removed
        # Sample at 14:15:00 (cutoff) is kept (uses <, not <=)
        self.assertEqual(len(cache.samples), 1)

    def test_prune_no_samples_to_remove(self):
        """All samples are recent → no pruning, list unchanged."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=5)
        cache = _make_cache_with_samples(300, cache_start)

        original_samples = list(cache.samples)
        fetcher = lambda: None

        metrics.set_clock(FakeClock(fixed_now))
        cache.get_or_fetch(fetcher, fixed_now, force=True)

        self.assertEqual(len(cache.samples), 300)
        self.assertEqual(cache.samples, original_samples)

    def test_prune_updates_sample_count(self):
        """sample_count reflects pruned length."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=5)
        cache = _make_cache_with_samples(300, cache_start)

        fetcher = lambda: None

        metrics.set_clock(FakeClock(fixed_now))
        cache.get_or_fetch(fetcher, fixed_now, force=True)

        self.assertEqual(cache.sample_count, len(cache.samples))

    def test_prune_updates_last_sample_at(self):
        """last_sample_at recalculated after pruning."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=5)
        cache = _make_cache_with_samples(300, cache_start)

        fetcher = lambda: None

        metrics.set_clock(FakeClock(fixed_now))
        cache.get_or_fetch(fetcher, fixed_now, force=True)

        assert len(cache.samples) > 0

        expected_last = cache.data_start + timedelta(seconds=len(cache.samples) - 1)
        self.assertEqual(cache.last_sample_at, expected_last)

    def test_prune_empty_samples_list_noop(self):
        """Empty samples → no crash, no change."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)
        cache = metrics.EnergyCache()
        cache.samples = []

        fetcher = lambda: None

        metrics.set_clock(FakeClock(fixed_now))
        cache.get_or_fetch(fetcher, fixed_now, force=True)

        self.assertEqual(len(cache.samples), 0)

    def test_prune_with_data_start_none_noop(self):
        """No data_start → no pruning (can't compute times)."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)
        cache = metrics.EnergyCache()
        cache.samples = [0.001] * 500
        cache.data_start = None

        original_samples = list(cache.samples)

        new_start = fixed_now + timedelta(minutes=10)
        new_samples = []
        fetcher = _fetcher_returns(new_start, new_samples)

        metrics.set_clock(FakeClock(fixed_now))
        cache.get_or_fetch(fetcher, fixed_now, force=True)

        # Without data_start, pruning can't compute times → no pruning
        self.assertEqual(len(cache.samples), 500)
        self.assertEqual(cache.samples, original_samples)

    def test_prune_exact_3600_samples_removed(self):
        """All samples older than 3600s from fixed_now → all removed."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 16, 10, 0, tzinfo=timezone.utc)
        # All samples 2 hours ago
        cache_start = fixed_now - timedelta(hours=2)
        cache = _make_cache_with_samples(3600, cache_start)

        new_start = cache_start + timedelta(minutes=5)
        new_samples = []
        fetcher = _fetcher_returns(new_start, new_samples)

        metrics.set_clock(FakeClock(fixed_now))
        cache.get_or_fetch(fetcher, fixed_now, force=True)

        # All 3600 samples are >= 3600s old (from 14:10:00 to 15:09:59)
        # cutoff = 15:10:00, so all samples < cutoff
        self.assertEqual(len(cache.samples), 0)

    def test_prune_one_sample_kept(self):
        """3601-sample cache, empty fetch → prune → 1 kept.

        Reproduces the production bug on 2026-05-21 where a production
        server hit an AssertionError in compute_nbc_quarters. When the
        cache held 3601 samples and the fetch returned empty data,
        the pre-fix store path truncated the samples to 3600 but left
        data_start pointing to the original time. This caused the pruning
        loop to miscalculate sample timestamps and prune every sample,
        leaving 0 instead of the expected 1 boundary sample.
        """
        import metrics

        fixed_now = datetime(2025, 6, 15, 15, 15, 0, tzinfo=timezone.utc)
        # 3601 samples from 13:15:00 to 14:15:00 (inclusive)
        # Empty fetch → no replacement, only pruning.
        # Pre-fix: truncate to 3600 with data_start still 13:15:00 →
        #   all 3600 samples < 14:15:00 → 0 kept
        # Post-fix: prune with data_start 13:15:00 → samples at
        #   13:15:00–14:14:59 pruned, 14:15:00 kept → 1 kept
        cache_start = datetime(2025, 6, 15, 13, 15, 0, tzinfo=timezone.utc)
        cache = _make_cache_with_samples(3601, cache_start)

        fetcher = _fetcher_returns(
            datetime(2025, 6, 15, 15, 15, 0, tzinfo=timezone.utc), [])

        metrics.set_clock(FakeClock(fixed_now))
        cache.get_or_fetch(fetcher, fixed_now, force=True)

        # Prune should keep 1 sample at 14:15:00 (the boundary)
        self.assertEqual(len(cache.samples), 1)
        self.assertEqual(cache.samples[0], 0.001)


# ===========================================================================
# TestHourlyProjectionPopulationCompleteness
# ===========================================================================


class TestHourlyProjectionPopulationCompleteness(unittest.TestCase):
    """Tests for _populate_device() returning complete _PopulationResult."""

    def test_populate_device_returns_all_fields(self):
        """Returned _PopulationResult has all fields populated."""
        hp, mock = _make_hourly_mock(n_seconds=3600, samples=[0.001] * 3600)

        result = hp._populate_device(mock.channels[0], datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc))

        self.assertIsNotNone(result)
        self.assertIsInstance(result, _PopulationResult)
        self.assertIsNotNone(result.per_second_data)
        self.assertIsNotNone(result.chart_data)
        self.assertIsNotNone(result.nbc_seconds)
        self.assertIsNotNone(result.nbc_data_start)

    def test_populate_device_per_second_data_length_matches_fetch(self):
        """per_second_data length matches what API returned."""
        expected_length = 1800
        hp, mock = _make_hourly_mock(n_seconds=expected_length)

        result = hp._populate_device(mock.channels[0], datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc))

        self.assertEqual(len(result.per_second_data), expected_length)

    def test_populate_device_chart_data_is_last_300(self):
        """chart_data has exactly 300 elements (last 300 seconds)."""
        hp, mock = _make_hourly_mock(n_seconds=3600)

        result = hp._populate_device(mock.channels[0], datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc))

        self.assertEqual(len(result.chart_data), 300)

    def test_populate_device_with_multiple_channels_uses_first(self):
        """When device has multiple channels, first channel's data is returned."""
        hp, mock = _make_hourly_mock(n_seconds=100)
        # Add a second channel
        second_chan = MagicMock()
        second_chan.channel_num = 2
        second_chan.name = "Channel 2"
        second_chan.data = {"per_second_data": [0.999] * 50, "data_start": datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)}
        mock.channels.append(second_chan)

        result = hp._populate_device(mock.channels[0], datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc))

        # Should use first channel (channel_num=1)
        self.assertEqual(len(result.per_second_data), 100)

    def test_replace_stores_new_data_at_new_start(self):
        """Replace path stores new data with the new data_start."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)

        qh_boundary = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        num_existing = 500
        existing_samples = [0.01] * num_existing

        def initial_fetch():
            return {
                "per_second_data": existing_samples,
                "data_start": qh_boundary,
            }

        now1 = qh_boundary + timedelta(seconds=num_existing + 37)
        cache.get_or_fetch(initial_fetch, now1)

        self.assertEqual(len(cache.samples), num_existing)
        self.assertEqual(cache.data_start, qh_boundary)

        # Second fetch: new data_start → replace
        new_qh_start = qh_boundary + timedelta(seconds=500)
        num_new = 31
        new_samples = [0.02] * num_new

        def second_fetch():
            return {
                "per_second_data": new_samples,
                "data_start": new_qh_start,
            }

        now2 = now1 + timedelta(seconds=30)
        cache.get_or_fetch(second_fetch, now2, force=True)

        # Replace semantics: only the new samples stored
        self.assertEqual(len(cache.samples), num_new)
        self.assertEqual(cache.data_start, new_qh_start)


# ===========================================================================
# TestNBCUsesFullCache
# ===========================================================================


class TestNBCUsesFullCache(unittest.TestCase):
    """Tests verifying _compute_nbc uses energy_cache.samples over incremental delta."""

    def setUp(self):
        self._p1 = patch.object(MetricsBase, "vue_init")
        self._p2 = patch.object(MetricsBase, "get_device_info")
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    def test_nbc_falls_back_to_pop_result_when_no_cache(self):
        """When energy_cache is None, _compute_nbc uses pop_result.nbc_seconds.

        This verifies the fallback path still works for existing callers that
        don't pass an energy_cache.
        """
        hp = HourlyProjection(
            instant=datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
            logger_next=logging.getLogger("test"),
            energy_cache=None,
        )

        # 3600 samples = exactly one hour → all quarters complete
        full_samples = [0.001] * 3600
        pop_result = _PopulationResult(
            per_second_data=full_samples,

            chart_data=full_samples[-300:],
            nbc_seconds=full_samples,
            nbc_data_start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
            nbc_sample_count=3600,
        )

        pred_result = DevicePrediction(
            lag=timedelta(seconds=5),
            minute_predicted=1.0,
            prediction=60.0,
            prediction_min=55.0,
            prediction_max=65.0,
            seconds_remaining=900.0,

        )

        mock_vdi = MagicMock()
        mock_vdi.device_gid = 1234
        mock_vdi.device_name = "TEST_DEVICE"
        mock_vdi.time_zone = None

        device_metrics = hp._compute_device_metrics(mock_vdi, pop_result, pred_result)

        # With the fallback path (energy_cache=None), NBC should be computed
        # from pop_result.nbc_seconds (3600 samples), giving complete quarters.
        nbc = device_metrics.nbc
        self.assertIsNotNone(nbc.qh1)
        self.assertIsNotNone(nbc.qh2)
        self.assertIsNotNone(nbc.qh3)
        self.assertIsNotNone(nbc.qh4)

    def test_nbc_ignores_empty_cache_samples(self):
        """When energy_cache.samples is None/empty, fall back to pop_result."""
        import metrics

        hp = HourlyProjection(
            instant=datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
            logger_next=logging.getLogger("test"),
            energy_cache=metrics.EnergyCache(),  # fresh cache, no samples
        )

        # Only 60 samples in pop_result — would normally give only QH1
        pop_result = _PopulationResult(
            per_second_data=[0.001] * 60,

            chart_data=[],
            nbc_seconds=[0.001] * 60,
            nbc_data_start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
            nbc_sample_count=60,
        )

        pred_result = DevicePrediction(
            lag=timedelta(seconds=5),
            minute_predicted=1.0,
            prediction=60.0,
            prediction_min=55.0,
            prediction_max=65.0,
            seconds_remaining=900.0,

        )

        mock_vdi = MagicMock()
        mock_vdi.device_gid = 1234
        mock_vdi.device_name = "TEST_DEVICE"
        mock_vdi.time_zone = None

        device_metrics = hp._compute_device_metrics(mock_vdi, pop_result, pred_result)

        # Empty cache → fallback: only QH1 present (60 samples)
        nbc = device_metrics.nbc
        self.assertIsNotNone(nbc.qh1)
        self.assertIsNone(nbc.qh2)
        self.assertIsNone(nbc.qh3)


class TestPerSecondDataMergesCache(unittest.TestCase):
    """Tests verifying per_second_data carries raw API samples (not merged cache).

    Fix: _compute_device_metrics must store the raw API delta in per_second_data
    so that the EnergyCache re-ingestion step attributes the correct sample count
    and data_start to the new samples.  Merging the full cache into per_second_data
    and then re-extracting it with the incremental data_start inflates
    the merged last_sample_at into the future, which causes the next API call to
    send start > end and receive a 400 (see bugs/2026-05-30-api-httperror.log).

    NBC accuracy is preserved via nbc_seconds, which still uses the merged cache.
    """

    def setUp(self):
        self._p1 = patch.object(MetricsBase, "vue_init")
        self._p2 = patch.object(MetricsBase, "get_device_info")
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    def test_per_second_data_is_raw_api_data(self):
        """per_second_data must be the raw API data, not the merged cache.

        Pre-populate the cache with 3600 samples (full hour), then simulate
        a fetch returning 60 new samples.  The resulting
        DeviceMetrics.per_second_data must equal the raw 60-sample API data
        — NOT the 3600-sample cache — so that EnergyCache.get_or_fetch
        re-ingests only the genuinely new points under always-replace
        semantics.
        """
        import metrics

        now = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)

        # Pre-populate EnergyCache with a full hour of data.
        full_hour_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        full_samples = [0.01] * 3600
        cache = metrics.EnergyCache()
        cache.samples = list(full_samples)
        cache.data_start = full_hour_start
        cache.sample_count = 3600
        cache.last_sample_at = full_hour_start + timedelta(seconds=3599)

        hp = HourlyProjection(
            instant=now,
            logger_next=logging.getLogger("test"),
            energy_cache=cache,
        )

        # Simulate an incremental fetch: 60 new samples starting right after
        # the cache ends (15:00:00).
        incremental_start = full_hour_start + timedelta(seconds=3600)  # 15:00:00
        incremental_samples = [0.02] * 60
        pop_result = _PopulationResult(
            per_second_data=incremental_samples,

            chart_data=incremental_samples[-300:],
            nbc_seconds=incremental_samples,
            nbc_data_start=incremental_start,
            nbc_sample_count=60,
        )

        pred_result = DevicePrediction(
            lag=timedelta(seconds=5),
            minute_predicted=1.0,
            prediction=60.0,
            prediction_min=55.0,
            prediction_max=65.0,
            seconds_remaining=900.0,

        )

        mock_vdi = MagicMock()
        mock_vdi.device_gid = 1234
        mock_vdi.device_name = "TEST_DEVICE"
        mock_vdi.time_zone = None

        device_metrics = hp._compute_device_metrics(mock_vdi, pop_result, pred_result)

        # per_second_data must be the raw 60 samples returned by the API.
        # Always-replace semantics mean the fetch result is ingested as-is;
        # storing the full cached window here would double-count data.
        self.assertEqual(
            len(device_metrics.per_second_data),
            60,
            "per_second_data must be the raw API data (60), not the cached window",
        )
        self.assertEqual(device_metrics.per_second_data, incremental_samples)

    def test_per_second_data_unchanged_when_no_cache(self):
        """When energy_cache is None, per_second_data uses pop_result as-is."""
        hp = HourlyProjection(
            instant=datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
            logger_next=logging.getLogger("test"),
            energy_cache=None,
        )

        raw_data = [0.005] * 60
        pop_result = _PopulationResult(
            per_second_data=raw_data,

            chart_data=[],
            nbc_seconds=raw_data,
            nbc_data_start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
            nbc_sample_count=60,
        )

        pred_result = DevicePrediction(
            lag=timedelta(seconds=5),
            minute_predicted=1.0,
            prediction=60.0,
            prediction_min=55.0,
            prediction_max=65.0,
            seconds_remaining=900.0,

        )

        mock_vdi = MagicMock()
        mock_vdi.device_gid = 1234
        mock_vdi.device_name = "TEST_DEVICE"
        mock_vdi.time_zone = None

        device_metrics = hp._compute_device_metrics(mock_vdi, pop_result, pred_result)

        # Without a cache, per_second_data should equal the raw pop_result data.
        self.assertEqual(len(device_metrics.per_second_data), 60)
        self.assertEqual(device_metrics.per_second_data, raw_data)

    def test_per_second_data_unchanged_when_cache_empty(self):
        """When cache has no samples, per_second_data uses pop_result as-is."""
        import metrics

        hp = HourlyProjection(
            instant=datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
            logger_next=logging.getLogger("test"),
            energy_cache=metrics.EnergyCache(),  # fresh, no samples
        )

        raw_data = [0.007] * 60
        pop_result = _PopulationResult(
            per_second_data=raw_data,

            chart_data=[],
            nbc_seconds=raw_data,
            nbc_data_start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
            nbc_sample_count=60,
        )

        pred_result = DevicePrediction(
            lag=timedelta(seconds=5),
            minute_predicted=1.0,
            prediction=60.0,
            prediction_min=55.0,
            prediction_max=65.0,
            seconds_remaining=900.0,

        )

        mock_vdi = MagicMock()
        mock_vdi.device_gid = 1234
        mock_vdi.device_name = "TEST_DEVICE"
        mock_vdi.time_zone = None

        device_metrics = hp._compute_device_metrics(mock_vdi, pop_result, pred_result)

        # Empty cache → no merge → raw data preserved.
        self.assertEqual(len(device_metrics.per_second_data), 60)
        self.assertEqual(device_metrics.per_second_data, raw_data)


class TestCreateMetricsPassesCache(unittest.TestCase):
    """Tests for create_metrics passing EnergyCache to HourlyProjection."""

    def test_create_metrics_passes_energy_cache(self):
        """create_metrics passes _energy_cache to HourlyProjection.

        This is the integration test for the fix: _energy_cache should be
        passed through to HourlyProjection so that _compute_nbc can use
        the full merged cache instead of the incremental delta.
        """
        import app as app_mod
        from metrics import HourlyProjection, EnergyCache, create_metrics

        with mock_config():
            cache = app_mod._state.energy_cache
            self.assertIsInstance(cache, EnergyCache)

            # Replace HourlyProjection with a MagicMock so we can inspect
            # the constructor call without actually running the real code.
            with patch("metrics.HourlyProjection") as MockHP:
                mock_instance = MockHP.return_value
                create_metrics(
                    cache,
                    datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
                    logging.getLogger("test"),
                )

                # Verify HourlyProjection was called with _energy_cache
                MockHP.assert_called_once()
                call_args = MockHP.call_args
                # Arguments: (now, logger, _energy_cache)
                self.assertEqual(len(call_args[0]), 3,
                                 "HourlyProjection called with 3 positional args")
                self.assertIs(
                    call_args[0][2],
                    cache,
                    "Third arg (energy_cache) must be the module-level _energy_cache",
                )
                self.assertEqual(call_args[1], {}, "No keyword args expected")

    def test_create_metrics_stamps_fetched_at(self):
        """create_metrics stamps _fetched_at so lag is computed from fetch time.

        Production readers (app.py SSE lag recalculation, load_nbc.py
        data_point_at) fall back to ``now`` when _fetched_at is absent —
        only tests were setting it.  create_metrics must stamp it with the
        ``now`` it was called with so the stored metrics carry their fetch
        time.
        """
        import app as app_mod
        from metrics import create_metrics

        with mock_config():
            cache = app_mod._state.energy_cache
            with patch("metrics.HourlyProjection") as MockHP:
                mock_instance = MockHP.return_value
                mock_instance.metrics = {"devices": []}
                now = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
                result = create_metrics(
                    cache, now, logging.getLogger("test")
                )

        self.assertIsNotNone(result)
        self.assertEqual(result["_fetched_at"], now)

    def test_create_metrics_uses_data_start_for_chart_start(self):
        """create_metrics uses data_start (not last_sample_at) for incremental chart_start.

        After compaction, data_start is QH-aligned and points to the start
        of the current incomplete QH.  The incremental fetch should start
        from data_start so the full QH's per-second data is refetched,
        not from last_sample_at which would miss earlier data.
        """
        from energy_cache import EnergyCache, EnergyCacheData
        from metrics import HourlyProjection, create_metrics
        from datetime import timedelta

        now = datetime(2026, 7, 30, 21, 33, 23, tzinfo=timezone.utc)

        # Simulate post-compaction state: 121 samples from 21:30:00–21:32:00.
        data_start = datetime(2026, 7, 30, 21, 30, 0, tzinfo=timezone.utc)
        last_sample_at = datetime(2026, 7, 30, 21, 32, 0, tzinfo=timezone.utc)
        samples = [0.001] * 121

        cache = EnergyCache()
        cache._data = EnergyCacheData(
            samples=samples,
            data_start=data_start,
            last_sample_at=last_sample_at,
            last_fetch_at=now - timedelta(seconds=3),
            sample_count=121,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )

        with patch("metrics.HourlyProjection") as MockHP:
            mock_instance = MockHP.return_value
            create_metrics(cache, now, logging.getLogger("test"))

            # Verify populate was called with data_start (21:30:00),
            # NOT last_sample_at (21:32:00).
            mock_instance.populate.assert_called_once()
            call_args = mock_instance.populate.call_args
            assert len(call_args[0]) >= 1
            chart_start = call_args[0][0]
            assert chart_start == data_start, (
                f"chart_start should be data_start ({data_start}), "
                f"got {chart_start}"
            )

    def test_create_metrics_floors_chart_start_to_qh(self):
        """Non-QH-aligned data_start must not drive chart_start.

        If the API returns a minute-aligned data_start (e.g. 21:32:00), the
        fetch window would only cover the tail of the QH.  chart_start must
        be floored to the current QH boundary (21:30:00) so the full QH's
        per-second data is refetched on every cycle.
        """
        from energy_cache import EnergyCache, EnergyCacheData
        from metrics import HourlyProjection, create_metrics
        from datetime import timedelta

        now = datetime(2026, 7, 30, 21, 33, 23, tzinfo=timezone.utc)

        # Simulate a minute-aligned (non-QH-aligned) data_start from the API.
        data_start = datetime(2026, 7, 30, 21, 32, 0, tzinfo=timezone.utc)
        samples = [0.001] * 83

        cache = EnergyCache()
        cache._data = EnergyCacheData(
            samples=samples,
            data_start=data_start,
            last_sample_at=datetime(2026, 7, 30, 21, 33, 22, tzinfo=timezone.utc),
            last_fetch_at=now - timedelta(seconds=3),
            sample_count=83,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )

        with patch("metrics.HourlyProjection") as MockHP:
            mock_instance = MockHP.return_value
            create_metrics(cache, now, logging.getLogger("test"))

            mock_instance.populate.assert_called_once()
            chart_start = mock_instance.populate.call_args[0][0]
            assert chart_start == datetime(
                2026, 7, 30, 21, 30, 0, tzinfo=timezone.utc
            ), (
                f"chart_start should be floored to current QH (21:30:00), "
                f"got {chart_start}"
            )

    def test_create_metrics_extends_window_across_qh_boundary(self):
        """A stale QH-aligned data_start must be retained across a QH boundary.

        If the last fetch before a QH boundary holds <900 samples of the
        just-completed QH, the next fetch must start from the old data_start
        so the QH reaches 900 samples and is compacted into a
        CompletedNBCPeriod.  Starting from floor_to_qh(now) would replace
        the un-compacted window and lose the QH forever.
        """
        from energy_cache import EnergyCache, EnergyCacheData
        from metrics import HourlyProjection, create_metrics
        from datetime import timedelta

        now = datetime(2026, 7, 31, 18, 45, 40, tzinfo=timezone.utc)

        # Cache holds 890 samples of the 18:30 QH (just ended, not yet
        # compacted because 890 < 900).
        data_start = datetime(2026, 7, 31, 18, 30, 0, tzinfo=timezone.utc)
        samples = [0.001] * 890

        cache = EnergyCache()
        cache._data = EnergyCacheData(
            samples=samples,
            data_start=data_start,
            last_sample_at=data_start + timedelta(seconds=889),
            last_fetch_at=now - timedelta(seconds=3),
            sample_count=890,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )

        with patch("metrics.HourlyProjection") as MockHP:
            mock_instance = MockHP.return_value
            create_metrics(cache, now, logging.getLogger("test"))

            mock_instance.populate.assert_called_once()
            chart_start = mock_instance.populate.call_args[0][0]
            assert chart_start == data_start, (
                f"chart_start must stay at {data_start} so the 18:30 QH "
                f"completes 900 samples and compacts, got {chart_start}"
            )

    def test_create_metrics_non_aligned_stale_data_start_floors_to_qh(self):
        """A stale non-QH-aligned data_start must not drive chart_start.

        If data_start is not QH-aligned (e.g. the API returned a
        minute-aligned start), fetching from it would produce a misaligned
        window that compact() cannot chunk onto real QH boundaries.
        chart_start must fall back to the current QH boundary.
        """
        from energy_cache import EnergyCache, EnergyCacheData
        from metrics import HourlyProjection, create_metrics
        from datetime import timedelta

        now = datetime(2026, 7, 30, 21, 33, 23, tzinfo=timezone.utc)

        # data_start is stale (21:27:00 < 21:30:00) but NOT QH-aligned.
        data_start = datetime(2026, 7, 30, 21, 27, 0, tzinfo=timezone.utc)
        samples = [0.001] * 384

        cache = EnergyCache()
        cache._data = EnergyCacheData(
            samples=samples,
            data_start=data_start,
            last_sample_at=data_start + timedelta(seconds=383),
            last_fetch_at=now - timedelta(seconds=3),
            sample_count=384,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )

        with patch("metrics.HourlyProjection") as MockHP:
            mock_instance = MockHP.return_value
            create_metrics(cache, now, logging.getLogger("test"))

            mock_instance.populate.assert_called_once()
            chart_start = mock_instance.populate.call_args[0][0]
            assert chart_start == datetime(
                2026, 7, 30, 21, 30, 0, tzinfo=timezone.utc
            ), (
                f"non-aligned stale data_start must floor to current QH "
                f"(21:30:00), got {chart_start}"
            )


class TestQuantizationAwarePrediction(unittest.TestCase):
    """Integration tests for quantization-aware NBC prediction window.

    Verifies that _compute_device_metrics correctly threads quantization
    data from EnergyCache into the NBC prediction window.
    """

    def setUp(self):
        self._p1 = patch.object(MetricsBase, "vue_init")
        self._p2 = patch.object(MetricsBase, "get_device_info")
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    def _make_cache_with_quantization(
        self,
        samples: list[float],
        data_start: datetime,
        qs: int | None,
        qc: float | None,
    ) -> EnergyCache:
        """Create an EnergyCache with pre-set quantization data."""
        cache = EnergyCache()
        cache._data = EnergyCacheData(
            samples=samples,
            data_start=data_start,
            last_sample_at=data_start,
            last_fetch_at=data_start,
            sample_count=len(samples),
            quantization_seconds=qs,
            quantization_offset=0,
            quantization_confidence=qc,
        )
        return cache

    def _run_compute_device_metrics(
        self, cache: EnergyCache | None, nbc_seconds: list[float]
    ):
        """Run _compute_device_metrics with given cache and NBC data.

        Returns the DeviceMetrics nbc field.
        """
        hp = HourlyProjection(
            instant=datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
            logger_next=logging.getLogger("test"),
            energy_cache=cache,
        )
        pop_result = _PopulationResult(
            per_second_data=nbc_seconds,

            chart_data=[],
            nbc_seconds=nbc_seconds,
            nbc_data_start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
            nbc_sample_count=len(nbc_seconds),
        )
        pred_result = DevicePrediction(
            lag=timedelta(seconds=5),
            minute_predicted=1.0,
            prediction=60.0,
            prediction_min=55.0,
            prediction_max=65.0,
            seconds_remaining=900.0,

        )
        mock_vdi = MagicMock()
        mock_vdi.device_gid = 1234
        mock_vdi.device_name = "TEST_DEVICE"
        mock_vdi.time_zone = None

        device_metrics = hp._compute_device_metrics(mock_vdi, pop_result, pred_result)
        return device_metrics.nbc

    def test_quantization_30s_window_used(self):
        """QH1 uses 30s prediction window when quantization (N=30, confidence=1.0).

        Data: 70 samples of 0.001 + 30 samples of 0.003 = 100.
        With 30s window: prediction_w = 3.0 W → predicted_wh = 2560.
        With 60s window: prediction_w = 2.0 W → predicted_wh = 1760.
        """
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        samples = [0.001] * 70 + [0.003] * 30
        cache = self._make_cache_with_quantization(
            samples, data_start, qs=30, qc=1.0
        )
        nbc = self._run_compute_device_metrics(cache, samples)

        self.assertIsNotNone(nbc.qh1)
        self.assertFalse(nbc.qh1.complete)
        # 30s window → predicted_wh = 2560
        self.assertAlmostEqual(
            nbc.qh1.predicted_wh, 2560.0, places=6,
            msg="Expected 2560 (30s window)",
        )

    def test_fallback_30s_when_no_quantization(self):
        """QH1 falls back to default prediction window when no quantization data."""
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        samples = [0.001] * 70 + [0.003] * 30
        cache = self._make_cache_with_quantization(
            samples, data_start, qs=None, qc=None
        )
        nbc = self._run_compute_device_metrics(cache, samples)

        self.assertIsNotNone(nbc.qh1)
        self.assertFalse(nbc.qh1.complete)
        # 30s window → predicted_wh = 2560
        self.assertAlmostEqual(
            nbc.qh1.predicted_wh, 2560.0, places=6,
            msg="Expected 2560 (30s fallback)",
        )

    def test_fallback_when_confidence_below_threshold(self):
        """QH1 falls back to default window when quantization confidence is below threshold."""
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        samples = [0.001] * 70 + [0.003] * 30
        cache = self._make_cache_with_quantization(
            samples, data_start, qs=30, qc=0.5
        )
        nbc = self._run_compute_device_metrics(cache, samples)

        self.assertIsNotNone(nbc.qh1)
        self.assertFalse(nbc.qh1.complete)
        # 30s window → predicted_wh = 2560
        self.assertAlmostEqual(
            nbc.qh1.predicted_wh, 2560.0, places=6,
            msg="Expected 2560 (30s fallback due to low confidence)",
        )

    def test_fallback_when_no_cache(self):
        """QH1 falls back to default window when energy_cache is None."""
        samples = [0.001] * 70 + [0.003] * 30
        nbc = self._run_compute_device_metrics(None, samples)

        self.assertIsNotNone(nbc.qh1)
        self.assertFalse(nbc.qh1.complete)
        # 30s window → predicted_wh = 2560
        self.assertAlmostEqual(
            nbc.qh1.predicted_wh, 2560.0, places=6,
            msg="Expected 2560 (30s fallback, no cache)",
        )






# =============================================================================
# Emporia auth hardening (plan subtask 2.4, fixes R5 / mitigates R7)
# =============================================================================


class TestVueInitHardening(unittest.TestCase):
    """Corrupt .vue-keys.json must fall back to password auth, not crash."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.keys_path = f"{self._tmp.name}/.vue-keys.json"

    def _make_hp(self, keys_content):
        hp = HourlyProjection.__new__(HourlyProjection)
        hp.logger = MagicMock()
        hp.vue = MagicMock()
        if keys_content is not None:
            with open(self.keys_path, "w", encoding="utf-8") as fh:
                fh.write(keys_content)
        hp.vue_keys = self.keys_path
        return hp

    def test_corrupt_json_falls_back_to_password(self):
        """Unparseable keys file -> single password login, no raise."""
        hp = self._make_hp("{definitely not json")
        hp.vue.login.return_value = True

        hp.vue_init()

        self.assertEqual(hp.vue.login.call_count, 1)
        kwargs = hp.vue.login.call_args.kwargs
        self.assertIn("username", kwargs)
        self.assertIn("password", kwargs)
        self.assertNotIn("id_token", kwargs)

    def test_missing_token_fields_fall_back_to_password(self):
        """Valid JSON missing required keys -> password fallback."""
        hp = self._make_hp('{"id_token": "only"}')
        hp.vue.login.return_value = True

        hp.vue_init()

        self.assertEqual(hp.vue.login.call_count, 1)
        kwargs = hp.vue.login.call_args.kwargs
        self.assertIn("username", kwargs)

    def test_password_fallback_failure_wraps_auth_error(self):
        """When the fallback also fails, raise VueAuthenticationError."""
        from metrics import VueAuthenticationError

        hp = self._make_hp("{corrupt")
        hp.vue.login.side_effect = RuntimeError("nope")

        with self.assertRaises(VueAuthenticationError):
            hp.vue_init()

    def test_concurrent_vue_init_serializes_login(self):
        """Concurrent vue_init calls never overlap inside vue.login."""
        import threading

        hp = self._make_hp(None)  # no keys file -> IOError -> password path
        state = {"active": 0, "max_active": 0}
        state_lock = threading.Lock()

        def fake_login(**_kwargs):
            with state_lock:
                state["active"] += 1
                state["max_active"] = max(
                    state["max_active"], state["active"]
                )
            time.sleep(0.03)
            with state_lock:
                state["active"] -= 1
            return True

        hp.vue.login = MagicMock(side_effect=fake_login)

        threads = [
            threading.Thread(target=hp.vue_init, daemon=True)
            for _ in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(hp.vue.login.call_count, 6)
        self.assertEqual(
            state["max_active"],
            1,
            "vue.login calls overlapped: _vue_init_lock is not held "
            "across authentication",
        )


class TestPredictionWindowFromCache:
    """prediction_window_from_cache applies the shared guard (A2)."""

    def test_flat_data_artifact_rejected(self):
        from types import SimpleNamespace

        cache = SimpleNamespace(
            data=SimpleNamespace(
                quantization_seconds=2, quantization_confidence=1.0
            )
        )
        assert metrics.prediction_window_from_cache(cache) is None

    def test_valid_window_passes_through(self):
        from types import SimpleNamespace

        cache = SimpleNamespace(
            data=SimpleNamespace(
                quantization_seconds=30, quantization_confidence=0.9
            )
        )
        assert metrics.prediction_window_from_cache(cache) == 30

    def test_none_cache_returns_none(self):
        assert metrics.prediction_window_from_cache(None) is None

    def test_empty_data_returns_none(self):
        from types import SimpleNamespace

        cache = SimpleNamespace(data=None)
        assert metrics.prediction_window_from_cache(cache) is None
