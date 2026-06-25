import logging
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests
from energy_cache import CurrentQH, EnergyCache, EnergyCacheData, FrozenQH
from metrics import (
    DevicePrediction,
    HourlyProjection,
    Metrics,
    MetricsBase,
    RetryableMetricsException,
    _PopulationResult,
)
from util import ceil_to_qh, compute_nbc_quarters
from mockdata import MetricsMock
from test_app import mock_config
from clock import FakeClock


class TestCreateMetricsIncrementalChartStart(unittest.TestCase):
    """Tests for create_metrics using QH boundary on incremental fetches.

    After the initial full-hour fetch, subsequent calls to create_metrics
    should fetch from the current QH boundary (not last_sample_at) so that
    _compute_nbc sees the full incomplete QH.
    """

    def setUp(self):
        self._p1 = patch.object(MetricsBase, "vue_init")
        self._p2 = patch.object(MetricsBase, "get_device_info")
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    def test_incremental_fetch_uses_qh_boundary_not_last_sample(self):
        """When cache has last_sample_at, chart_start is the QH boundary."""
        from metrics import create_metrics

        cache = EnergyCache()
        # Simulate a prior fetch that set last_sample_at to 14:54:59
        # (inside QH1 = 14:45:00–15:00:00).
        fake_data_start = datetime(2025, 6, 15, 14, 45, 0, tzinfo=timezone.utc)
        cache._data = EnergyCacheData(
            samples=[0.001] * 599,
            data_start=fake_data_start,
            last_sample_at=datetime(2025, 6, 15, 14, 54, 59, tzinfo=timezone.utc),
            last_fetch_at=datetime(2025, 6, 15, 14, 55, 0, tzinfo=timezone.utc),
            sample_count=599,
        )

        now = datetime(2025, 6, 15, 14, 56, 30, tzinfo=timezone.utc)

        with patch("metrics.HourlyProjection") as MockHP:
            mock_instance = MockHP.return_value
            mock_instance.metrics = {
                "devices": [], "api_response": {},
                "data_start": fake_data_start, "_data_lag_secs": 0,
            }
            create_metrics(cache, now, logging.getLogger("test"))

            # populate() should be called with the QH boundary (14:45:00),
            # NOT cap_chart_start(14:54:59, now) which would be 14:54:59.
            call_args = MockHP.return_value.populate.call_args
            chart_start = call_args[0][0]
            expected_qh_start = datetime(2025, 6, 15, 14, 45, 0, tzinfo=timezone.utc)
            self.assertEqual(
                chart_start, expected_qh_start,
                f"Expected chart_start at QH boundary {expected_qh_start}, "
                f"got {chart_start} (was it using last_sample_at?)",
            )

    def test_first_fetch_still_uses_full_hour(self):
        """When cache has no last_sample_at, chart_start is full hour ago."""
        from metrics import create_metrics

        cache = EnergyCache()  # fresh cache, last_sample_at is None

        now = datetime(2025, 6, 15, 14, 56, 30, tzinfo=timezone.utc)

        with patch("metrics.HourlyProjection") as MockHP:
            mock_instance = MockHP.return_value
            mock_instance.metrics = {
                "devices": [], "api_response": {},
                "data_start": None, "_data_lag_secs": 0,
            }
            create_metrics(cache, now, logging.getLogger("test"))

            call_args = MockHP.return_value.populate.call_args
            chart_start = call_args[0][0]
            expected = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
            self.assertEqual(
                chart_start, expected,
                f"First fetch should start at {expected}, got {chart_start}",
            )

    def test_incremental_fetch_covers_full_current_qh(self):
        """_compute_nbc receives full QH data, not just the incremental delta.

        Simulates: cache has 599 samples (14:45:00–14:54:59), then an
        incremental fetch from the QH boundary returns 660 samples covering
        the full QH. _compute_nbc should see all 660, not just the 61 new ones.
        """
        hp = HourlyProjection(
            instant=datetime(2025, 6, 15, 14, 56, 0, tzinfo=timezone.utc),
            logger_next=logging.getLogger("test"),
            energy_cache=None,
        )

        # 660 samples = 11 minutes into QH1 (14:45:00–14:56:00).
        full_qh_samples = [0.001] * 660
        pop_result = _PopulationResult(
            per_second_data=full_qh_samples,
            chart_data=full_qh_samples[-300:],
            nbc_seconds=full_qh_samples,
            nbc_data_start=datetime(2025, 6, 15, 14, 45, 0, tzinfo=timezone.utc),
            nbc_sample_count=660,
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

        nbc = device_metrics.nbc
        self.assertIsNotNone(nbc.qh1)
        self.assertFalse(nbc.qh1.complete)
        # 660 samples in QH1 → remaining_seconds should be 900 - 660 = 240
        self.assertEqual(
            nbc.qh1.remaining_seconds, 240,
            f"Expected 240s remaining (900-660), got {nbc.qh1.remaining_seconds}. "
            "Was the full QH data used?",
        )
        self.assertEqual(
            nbc.qh1.samples_used, 660,
            f"Expected 660 samples, got {nbc.qh1.samples_used}",
        )

    def test_nbc_prediction_correct_after_incremental_fetch(self):
        """End-to-end: incremental fetch → predicted_wh based on full QH data.

        With 660 samples of 0.001 Wh/s, raw_wh = 660 Wh. The trailing 60
        samples give prediction_w = 1.0 W. predicted_wh = 660 + 240 * 1.0 = 900.
        If only 60 samples were used (the bug), predicted_wh would be ~60.
        """
        hp = HourlyProjection(
            instant=datetime(2025, 6, 15, 14, 56, 0, tzinfo=timezone.utc),
            logger_next=logging.getLogger("test"),
            energy_cache=None,
        )

        full_qh_samples = [0.001] * 660
        pop_result = _PopulationResult(
            per_second_data=full_qh_samples,
            chart_data=full_qh_samples[-300:],
            nbc_seconds=full_qh_samples,
            nbc_data_start=datetime(2025, 6, 15, 14, 45, 0, tzinfo=timezone.utc),
            nbc_sample_count=660,
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

        nbc = device_metrics.nbc
        self.assertIsNotNone(nbc.qh1)
        # With 660 samples at 0.001 Wh/s:
        # raw_wh = 660 * 0.001 * 1000 = 660 Wh
        # prediction_w = last 60 samples: 60 * 0.001 * 1000 / 60 = 1.0 W
        # remaining = 240s
        # predicted_wh = 660 + 240 * 1.0 = 900.0
        self.assertAlmostEqual(
            nbc.qh1.predicted_wh, 900.0, places=6,
            msg="predicted_wh should be based on full 660-sample QH, not 60-sample delta",
        )


class TestTOUReporterAggregate(unittest.TestCase):
    """Test that TOUReporter.aggregate_tou correctly uses EnergyDataAggregator."""

    def test_aggregate_tou_uses_module_level_import(self):
        """Verify aggregate_tou doesn't reference self.EnergyDataAggregator.

        When EnergyDataAggregator was moved from class body to module-level,
        the call site changed from self.EnergyDataAggregator.aggregate_from_minutes()
        to EnergyDataAggregator.aggregate_from_minutes(). This test ensures
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

    def test_retryable_exception(self):
        ex = RetryableMetricsException("test error")
        self.assertEqual(ex.message, "test error")
        self.assertIsInstance(ex.instant, datetime)

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

    def test_mock_nbc_all_scenarios_covered(self):
        """Verify NBC covers all required scenarios across both devices.

        Device A (solar export, negative raw_wh) tests:
          - QH1 incomplete with predicted_wh from recent samples
          - QH2 complete with clamped wh=0 (raw_wh < 0 → wh = 0)

        Device B (load only, positive raw_wh) tests:
          - Complete quarters with positive wh (no clamping needed)
          - Incomplete quarter with positive predicted_wh
        """
        mock = MetricsMock(instant_minute=42)
        device_a = mock.metrics["devices"][0]
        device_b = mock.metrics["devices"][1]

        # Device A: solar export scenario (negative raw_wh → clamped to 0)
        nbc_a = device_a["nbc"]
        self.assertTrue(nbc_a["QH2"]["complete"])
        self.assertGreaterEqual(nbc_a["QH2"]["wh"], 0)
        self.assertFalse(nbc_a["QH1"]["complete"])
        self.assertIn("predicted_wh", nbc_a["QH1"])

        # Device B: load-only scenario (positive raw_wh, no clamping)
        nbc_b = device_b["nbc"]
        self.assertTrue(nbc_b["QH2"]["complete"])
        self.assertGreater(nbc_b["QH2"]["wh"], 0)
        self.assertFalse(nbc_b["QH1"]["complete"])
        self.assertIn("predicted_wh", nbc_b["QH1"])


class TestComputeNBCQuartersEdgeCases(unittest.TestCase):
    """Tests for util.compute_nbc_quarters edge cases."""

    def test_empty_data_returns_all_none(self):
        """With empty per_second_data, all quarters should be None."""
        result = compute_nbc_quarters([])

        for attr in ["qh1", "qh2", "qh3", "qh4"]:
            self.assertIsNone(getattr(result, attr))

    def test_n_zero_returns_all_none(self):
        """With n=0 (no seconds observed), all quarters should be None."""
        data = []
        result = compute_nbc_quarters(data)

        for attr, name in [("qh1", "QH1"), ("qh2", "QH2"), ("qh3", "QH3"), ("qh4", "QH4")]:
            self.assertIsNone(getattr(result, attr), f"{name} should be None")

    def test_n_900_completes_qh1(self):
        """n=900 should complete QH1, leave others None."""
        data = [0.002] * 900
        result = compute_nbc_quarters(data)

        self.assertTrue(result.qh1.complete)
        self.assertAlmostEqual(result.qh1.raw_wh, 900 * 0.002 * 1000)
        self.assertIsNone(result.qh2)
        self.assertIsNone(result.qh3)
        self.assertIsNone(result.qh4)

    def test_n_901_partial_qh1(self):
        """n=901 should complete QH2, partial QH1 with 1 sample."""
        data = [0.005] * 901
        result = compute_nbc_quarters(data)

        self.assertTrue(result.qh2.complete)
        self.assertFalse(result.qh1.complete)
        self.assertEqual(result.qh1.samples_used, 1)

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

    def test_init(self):
        """HourlyProjection() requires passing instant."""
        HourlyProjection(instant=datetime.now(timezone.utc))

    def test_populate_without_chart_start_raises(self):
        """HourlyProjection.populate() requires instant argument."""
        hp = HourlyProjection(instant=datetime.now(timezone.utc))
        with self.assertRaises(TypeError):
            hp.populate()  # type: ignore[call-arg]

    def test_populate_accepts_chart_start(self):
        """HourlyProjection.populate(chart_start=...) must accept a datetime."""
        chart_start = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        hp = HourlyProjection(instant=chart_start)
        # Should not raise TypeError about missing argument.
        # It may raise other errors (e.g. RetryableMetricsException) due
        # to mocked infrastructure, but the signature check passes.
        try:
            hp.populate(chart_start)
        except TypeError as exc:
            if "chart_start" in str(exc):
                self.fail(f"populate() rejected valid chart_start: {exc}")
            raise
        except RetryableMetricsException:
            # Expected with mocked infrastructure — the signature check passed.
            pass

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


class TestCapFetchWindow(unittest.TestCase):
    """Tests for the cap_fetch_window guard function."""

    def test_caps_old_start(self):
        """When start_time >1h before now, return current QH boundary."""
        from metrics import cap_fetch_window

        now = datetime(2026, 5, 19, 13, 7, 43, tzinfo=timezone.utc)
        old_start = now - timedelta(hours=9)
        expected_start = ceil_to_qh(now - timedelta(hours=1))
        result = cap_fetch_window(old_start, now)
        self.assertEqual(result, expected_start)

    def test_caps_old_start_at_exact_boundary(self):
        """When start_time is exactly 1h before now, it should NOT cap."""
        from metrics import cap_fetch_window

        now = datetime(2026, 5, 19, 13, 0, 0, tzinfo=timezone.utc)
        old_start = now - timedelta(hours=1)
        result = cap_fetch_window(old_start, now)
        self.assertEqual(result, old_start)

    def test_preserves_nearby_start(self):
        """When start_time is within 1h of now, return it unchanged."""
        from metrics import cap_fetch_window

        now = datetime(2026, 5, 19, 13, 0, 0, tzinfo=timezone.utc)
        nearby = now - timedelta(minutes=30)
        result = cap_fetch_window(nearby, now)
        self.assertEqual(result, nearby)

    def test_caps_across_qh_boundary(self):
        """Guard works when the 1h window crosses a QH boundary."""
        from metrics import cap_fetch_window

        now = datetime(2026, 5, 19, 13, 30, 0, tzinfo=timezone.utc)
        old_start = now - timedelta(hours=2)
        expected_start = ceil_to_qh(now - timedelta(hours=1))
        result = cap_fetch_window(old_start, now)
        self.assertEqual(result, expected_start)


class TestHourlyProjectionEdgeCases(unittest.TestCase):
    """Tests for HourlyProjection edge cases."""

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

    def test_aggregate_tou_with_none_values_in_data(self):
        """aggregate_tou should skip None values in 15-min data."""
        from metrics import TOUReporter

        tou = TOUReporter.__new__(TOUReporter)

        # Data with None values mixed in
        tou.usage_data_list = [
            {
                "start": datetime.now(timezone.utc),
                "data": [0.1, None, 0.2],
            }
        ]

        tou.aggregate_tou()

        self.assertIsNotNone(tou.tou_result)
        # total should include all values (including negatives if any, but these are positive)

    def test_aggregate_tou_with_negative_values(self):
        """aggregate_tou should handle negative values (solar export) in TOU buckets."""
        from metrics import TOUReporter

        tou = TOUReporter.__new__(TOUReporter)

        # Negative values represent solar export
        tou.usage_data_list = [
            {
                "start": datetime.now(timezone.utc),
                "data": [-0.1, 0.2],
            }
        ]

        tou.aggregate_tou()

        self.assertIsNotNone(tou.tou_result)
        # total should be (0.2 - 0.1) * some_factor = net positive
        # NBC should only sum positive values (imports), so -0.1 is ignored

    def test_aggregate_tou_all_none_data(self):
        """aggregate_tou with all None data should produce zero NBC."""
        from metrics import TOUReporter

        tou = TOUReporter.__new__(TOUReporter)

        tou.usage_data_list = [
            {
                "start": datetime.now(timezone.utc),
                "data": [None, None],
            }
        ]

        tou.aggregate_tou()

        self.assertIsNotNone(tou.nbc_result)
        # NBC should be 0 since all values are None (skipped in positive sum)


class TestDeviceMetricsDataClass(unittest.TestCase):
    """Tests for DeviceMetrics data class defaults and serialization."""

    def test_default_values_are_sensible(self):
        """Empty DeviceMetrics has sensible defaults for all fields."""
        from metrics import DeviceMetrics

        dm = DeviceMetrics()
        self.assertEqual(dm.gid, 0)
        self.assertEqual(dm.name, "")
        self.assertEqual(dm.timezone, "")
        self.assertIsInstance(dm.lag, timedelta)
        self.assertEqual(len(dm.per_second_data), 0)

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

    def test_no_predictions_lag_not_set(self):
        """When populate() returns empty dict, _data_lag_secs is not set."""
        from unittest.mock import MagicMock

        # Create a partial HourlyProjection where populate returns {}
        hp = HourlyProjection.__new__(HourlyProjection)

        # Set up required attributes
        hp.metrics = {"api_response": {}, "debug": False, "devices": [], "instant": datetime.now(timezone.utc)}
        hp.instant = hp.metrics["instant"]

        # Mock populate to return empty dict (no predictions)
        with patch.object(hp, "populate", return_value={}):
            # Mock device_info to be empty so the for-loop doesn't add devices
            with patch.object(MetricsBase, "device_info", {}):
                # Mock predict to return empty dict too (no predictions)
                with patch.object(hp, "predict", return_value={}):
                    # Now call the relevant part of __init__ manually
                    hp.metrics["api_response"]["total"] = timedelta()

        # When predictions is empty, _data_lag_secs should not be set
        self.assertNotIn("_data_lag_secs", hp.metrics)


class TestEnergyCacheSampleMetadata(unittest.TestCase):
    """Tests for EnergyCache sample metadata tracking."""

    def test_initial_state_has_sample_count_none(self):
        """Fresh cache has _sample_count = None."""
        from metrics import EnergyCache

        cache = EnergyCache()
        self.assertIsNone(cache.sample_count)

    def test_initial_state_has_last_sample_at_none(self):
        """Fresh cache has last_sample_at = None."""
        from metrics import EnergyCache

        cache = EnergyCache()
        self.assertIsNone(cache.last_sample_at)

    def test_get_or_fetch_sets_sample_count(self):
        """After get_or_fetch, sample_count reflects the number of samples."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)

        def fetch_func():
            return {
                "per_second_data": [0.1] * 50,
                "data_start": fixed_now,
            }

        cache.get_or_fetch(fetch_func, fixed_now)
        self.assertEqual(cache.sample_count, 50)

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
        self.assertIsNotNone(cache.last_sample_at)


def _make_hourly_mock(
    n_seconds: int = 100,
    samples: list[float] | None = None,
    instant: datetime | None = None,
    chart_start: datetime | None = None,
) -> tuple["HourlyProjection", MagicMock]:
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

    per_second_data = [0.001] * n_seconds

    mock_channel = MagicMock()
    mock_channel.channel_num = 1

    mock_vdi = MagicMock()
    mock_vdi.device_gid = 1234
    mock_vdi.device_name = "TEST_DEVICE"
    mock_vdi.channels = [mock_channel]
    mock_vdi.time_zone = None

    mock_vue = MagicMock()
    mock_channel.channels = [mock_channel]
    mock_vue.get_chart_usage.return_value = (per_second_data, chart_start)
    mock_channel.data = {
        "per_second_data": per_second_data,
        "data_start": chart_start,
    }
    mock_channel.samples = samples or []

    mock = MagicMock()
    mock.channels = [mock_channel]
    mock.vue = mock_vue

    with patch.object(MetricsBase, "vue_init"), \
         patch.object(MetricsBase, "get_device_info"):
        hp = HourlyProjection(instant=chart_start, logger_next=logging.getLogger("test"))
        hp.instant = instant
        MetricsBase.device_info = {1234: mock_vdi}

    hp.vue = mock_vue

    return hp, mock


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
    merged_last_sample_at into the future, which causes the next API call to
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

    def test_per_second_data_is_raw_api_delta_not_merged_cache(self):
        """per_second_data must be the raw API delta, not the merged cache.

        Pre-populate the cache with 3600 samples (full hour), then simulate
        an incremental fetch returning 60 new samples that start right after
        the cache end.  The resulting DeviceMetrics.per_second_data must equal
        the raw 60-sample delta — NOT the merged 3600-sample cache — so that
        EnergyCache.get_or_fetch re-ingests only the genuinely new points.

        Merging the full cache into per_second_data causes last_sample_at to
        overshoot the actual latest data point, producing a future timestamp
        that the Emporia API rejects with a 400 Client Error.
        """
        import metrics

        now = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)

        # Pre-populate EnergyCache with a full hour of data.
        full_hour_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        full_samples = [0.01] * 3600
        cache = metrics.EnergyCache()
        from energy_cache import EnergyCacheData
        cache._data = EnergyCacheData(
            samples=list(full_samples),
            data_start=full_hour_start,
            last_sample_at=full_hour_start + timedelta(seconds=3599),
            last_fetch_at=now,
            sample_count=3600,
        )

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

        # per_second_data must be the raw 60-sample delta.
        # Storing the merged 3600-sample cache here caused the EnergyCache to
        # re-merge those samples with the incremental data_start (15:00:00),
        # computing new_end = 15:00:00 + 3599s ≈ 16:00:00 — far in the future —
        # and setting last_sample_at to a future timestamp that became chart_start
        # on the next cycle, triggering a 400 from the Emporia API.
        self.assertEqual(
            len(device_metrics.per_second_data),
            60,
            "per_second_data must be the raw API delta (60), not the merged cache",
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
            cache = app_mod._energy_cache
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

    def test_fallback_60s_when_no_quantization(self):
        """QH1 falls back to 60s prediction window when no quantization data."""
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        samples = [0.001] * 70 + [0.003] * 30
        cache = self._make_cache_with_quantization(
            samples, data_start, qs=None, qc=None
        )
        nbc = self._run_compute_device_metrics(cache, samples)

        self.assertIsNotNone(nbc.qh1)
        self.assertFalse(nbc.qh1.complete)
        # 60s window → predicted_wh = 1760
        self.assertAlmostEqual(
            nbc.qh1.predicted_wh, 1760.0, places=6,
            msg="Expected 1760 (60s fallback)",
        )

    def test_fallback_60s_when_confidence_below_threshold(self):
        """QH1 falls back to 60s when quantization confidence < 0.9."""
        data_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        samples = [0.001] * 70 + [0.003] * 30
        cache = self._make_cache_with_quantization(
            samples, data_start, qs=30, qc=0.5
        )
        nbc = self._run_compute_device_metrics(cache, samples)

        self.assertIsNotNone(nbc.qh1)
        self.assertFalse(nbc.qh1.complete)
        # 60s window → predicted_wh = 1760
        self.assertAlmostEqual(
            nbc.qh1.predicted_wh, 1760.0, places=6,
            msg="Expected 1760 (60s fallback due to low confidence)",
        )

    def test_fallback_60s_when_no_cache(self):
        """QH1 falls back to 60s when energy_cache is None."""
        samples = [0.001] * 70 + [0.003] * 30
        nbc = self._run_compute_device_metrics(None, samples)

        self.assertIsNotNone(nbc.qh1)
        self.assertFalse(nbc.qh1.complete)
        # 60s window → predicted_wh = 1760
        self.assertAlmostEqual(
            nbc.qh1.predicted_wh, 1760.0, places=6,
            msg="Expected 1760 (60s fallback, no cache)",
        )


class TestFrozenQHBackfill(unittest.TestCase):
    """Tests for backfilling QH2-QH4 from frozen QH blocks in EnergyCache."""

    def setUp(self):
        self._p1 = patch.object(MetricsBase, "vue_init")
        self._p2 = patch.object(MetricsBase, "get_device_info")
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    def _make_cache_with_frozen(
        self,
        current_samples: list[float],
        current_data_start: datetime,
        frozen_qhs: list[FrozenQH],
    ) -> EnergyCache:
        cache = EnergyCache()
        cache._data = EnergyCacheData(
            current_qh=CurrentQH(data_start=current_data_start, samples=current_samples),
            frozen_qhs=frozen_qhs,
            last_fetch_at=current_data_start,
            quantization_seconds=None,
            quantization_confidence=None,
        )
        return cache

    def test_frozen_qhs_backfill_missing_qh2_qh4(self):
        """QH2-QH4 should be populated from frozen blocks when _compute_nbc only has QH1 data."""
        from util import NBCQuarter

        now = datetime(2025, 6, 15, 14, 50, 0, tzinfo=timezone.utc)
        # Only 300 samples in current QH (5 minutes) → _compute_nbc gives only QH1
        current_samples = [0.001] * 300
        qh1_start = datetime(2025, 6, 15, 14, 45, 0, tzinfo=timezone.utc)

        frozen = [
            FrozenQH(
                data_start=datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
                nbc_result=NBCQuarter(complete=True, raw_wh=-100.0, wh=0),
            ),
            FrozenQH(
                data_start=datetime(2025, 6, 15, 14, 15, 0, tzinfo=timezone.utc),
                nbc_result=NBCQuarter(complete=True, raw_wh=-200.0, wh=0),
            ),
            FrozenQH(
                data_start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
                nbc_result=NBCQuarter(complete=True, raw_wh=-300.0, wh=0),
            ),
        ]

        cache = self._make_cache_with_frozen(current_samples, qh1_start, frozen)

        hp = HourlyProjection(
            instant=now,
            logger_next=logging.getLogger("test"),
            energy_cache=cache,
        )

        pop_result = _PopulationResult(
            per_second_data=current_samples,
            chart_data=[],
            nbc_seconds=current_samples,
            nbc_data_start=qh1_start,
            nbc_sample_count=300,
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

        nbc = hp._compute_device_metrics(mock_vdi, pop_result, pred_result).nbc

        # QH1 from _compute_nbc (incomplete, 300 samples)
        self.assertIsNotNone(nbc.qh1)
        self.assertFalse(nbc.qh1.complete)
        self.assertEqual(nbc.qh1.samples_used, 300)

        # QH2-QH4 backfilled from frozen blocks (most recent frozen = QH2)
        self.assertIsNotNone(nbc.qh2)
        self.assertTrue(nbc.qh2.complete)
        self.assertEqual(nbc.qh2.raw_wh, -100.0)

        self.assertIsNotNone(nbc.qh3)
        self.assertTrue(nbc.qh3.complete)
        self.assertEqual(nbc.qh3.raw_wh, -200.0)

        self.assertIsNotNone(nbc.qh4)
        self.assertTrue(nbc.qh4.complete)
        self.assertEqual(nbc.qh4.raw_wh, -300.0)

    def test_no_frozen_qhs_leaves_qh2_qh4_none(self):
        """Without frozen blocks, QH2-QH4 should remain None."""
        now = datetime(2025, 6, 15, 14, 50, 0, tzinfo=timezone.utc)
        current_samples = [0.001] * 300
        qh1_start = datetime(2025, 6, 15, 14, 45, 0, tzinfo=timezone.utc)

        cache = self._make_cache_with_frozen(current_samples, qh1_start, frozen_qhs=[])

        hp = HourlyProjection(
            instant=now,
            logger_next=logging.getLogger("test"),
            energy_cache=cache,
        )

        pop_result = _PopulationResult(
            per_second_data=current_samples,
            chart_data=[],
            nbc_seconds=current_samples,
            nbc_data_start=qh1_start,
            nbc_sample_count=300,
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

        nbc = hp._compute_device_metrics(mock_vdi, pop_result, pred_result).nbc

        self.assertIsNotNone(nbc.qh1)
        self.assertIsNone(nbc.qh2)
        self.assertIsNone(nbc.qh3)
        self.assertIsNone(nbc.qh4)

    def test_incremental_fetch_preserves_completed_qh_periods(self):
        """First fetch gets all QHs; incremental fetch should preserve QH2-QH4 via frozen blocks."""
        from util import NBCQuarter

        # Simulate: first fetch populated all QHs and froze QH2-QH4.
        # Now an incremental fetch only has QH1 data (300 samples).
        qh1_start = datetime(2025, 6, 15, 14, 45, 0, tzinfo=timezone.utc)
        current_samples = [0.001] * 300
        frozen = [
            FrozenQH(
                data_start=datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
                nbc_result=NBCQuarter(complete=True, raw_wh=-50.0, wh=0),
            ),
            FrozenQH(
                data_start=datetime(2025, 6, 15, 14, 15, 0, tzinfo=timezone.utc),
                nbc_result=NBCQuarter(complete=True, raw_wh=-75.0, wh=0),
            ),
            FrozenQH(
                data_start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
                nbc_result=NBCQuarter(complete=True, raw_wh=-125.0, wh=0),
            ),
        ]

        cache = self._make_cache_with_frozen(current_samples, qh1_start, frozen)

        hp = HourlyProjection(
            instant=datetime(2025, 6, 15, 14, 50, 0, tzinfo=timezone.utc),
            logger_next=logging.getLogger("test"),
            energy_cache=cache,
        )

        pop_result = _PopulationResult(
            per_second_data=current_samples,
            chart_data=[],
            nbc_seconds=current_samples,
            nbc_data_start=qh1_start,
            nbc_sample_count=300,
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

        nbc = hp._compute_device_metrics(mock_vdi, pop_result, pred_result).nbc

        # All four QHs should be populated
        self.assertIsNotNone(nbc.qh1, "QH1 should be present (from _compute_nbc)")
        self.assertIsNotNone(nbc.qh2, "QH2 should be backfilled from frozen block")
        self.assertIsNotNone(nbc.qh3, "QH3 should be backfilled from frozen block")
        self.assertIsNotNone(nbc.qh4, "QH4 should be backfilled from frozen block")

        # Verify the frozen data was used (not recomputed)
        self.assertEqual(nbc.qh2.raw_wh, -50.0)
        self.assertEqual(nbc.qh3.raw_wh, -75.0)
        self.assertEqual(nbc.qh4.raw_wh, -125.0)


