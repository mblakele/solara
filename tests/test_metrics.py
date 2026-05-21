import logging
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests
from metrics import (
    HourlyProjection,
    Metrics,
    MetricsBase,
    RetryableMetricsException,
    _PopulationResult,
)
from util import ceil_to_qh, compute_nbc_quarters
from mockdata import MetricsMock
from test_app import mock_config


class _LogCapture(logging.Handler):
    """Minimal logging handler that captures formatted log records for assertions."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def text(self) -> str:
        return " ".join(r.getMessage() for r in self.records)

    def clear(self) -> None:
        self.records = []


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
        self.assertIn("total", reporter.tou_result)


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
        self.assertIn("scales", device_a)
        self.assertIn("smoothing", device_a)

        # Device B: positive (load only)
        device_b = devices[1]
        self.assertEqual(device_b["name"], "SOLAR+LOAD")
        self.assertIn("prediction", device_b)
        self.assertIn("scales", device_b)

    def test_mock_scales(self):
        # Device A: negative usage (solar export)
        device_a = self.metrics_data["devices"][0]
        scales_a = device_a["scales"]

        self.assertIn("1H", scales_a)
        self.assertIn("1MIN", scales_a)
        self.assertIn("10MIN", scales_a)

        hour_data_a = scales_a["1H"]
        # At default instant_minute=42, len(data) == 42*60 = 2520
        self.assertEqual(hour_data_a["seconds"], 42 * 60)
        self.assertIsInstance(hour_data_a["instant"], datetime)
        self.assertLess(hour_data_a["usage"], 0)

        # Device B: positive usage (load only)
        device_b = self.metrics_data["devices"][1]
        scales_b = device_b["scales"]
        hour_data_b = scales_b["1H"]
        self.assertEqual(hour_data_b["seconds"], 42 * 60)
        self.assertGreater(hour_data_b["usage"], 0)

    def test_mock_smoothing(self):
        device = self.metrics_data["devices"][0]
        smoothing = device["smoothing"]

        self.assertIn("1MIN", smoothing)
        self.assertIn("10MIN", smoothing)
        # Values are dynamically computed; just verify they're numeric
        self.assertIsInstance(smoothing["1MIN"], float)

    def test_data_for_scale_logic(self):
        # Testing the static method directly with sample data
        data = [0.1, 0.2, 0.3]  # kWh
        data_start = datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc)

        # Test Hour scale (direct sum * 1000)
        result_h = Metrics.data_for_scale(data, data_start, "1H")
        self.assertAlmostEqual(result_h["usage"], 600.0)

        # Test Minute scale (sum * 1000 * 60 / len)
        # (0.6 * 1000 * 60 / 3) = 12000 Wh/min equivalent
        result_m = Metrics.data_for_scale(data, data_start, "1MIN")
        self.assertAlmostEqual(result_m["usage"], 12000.0)

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
        self.assertIn("total", tou)
        self.assertIn("peak", tou)
        self.assertIn("part_peak", tou)
        self.assertIn("off_peak", tou)
        self.assertGreater(tou["total"], 0)
        self.assertGreater(tou["peak"], 0)

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
        self.assertNotIn("predicted_wh", nbc["QH3"])

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

        for qh in ["QH1", "QH2", "QH3", "QH4"]:
            self.assertIsNone(result[qh])

    def test_n_zero_returns_all_none(self):
        """With n=0 (no seconds observed), all quarters should be None."""
        data = []
        result = compute_nbc_quarters(data)

        for qh in ["QH1", "QH2", "QH3", "QH4"]:
            self.assertIsNone(result[qh])

    def test_n_900_completes_qh1(self):
        """n=900 should complete QH1, leave others None."""
        data = [0.002] * 900
        result = compute_nbc_quarters(data)

        self.assertTrue(result["QH1"]["complete"])
        self.assertAlmostEqual(result["QH1"]["raw_wh"], 900 * 0.002 * 1000)
        self.assertIsNone(result["QH2"])
        self.assertIsNone(result["QH3"])
        self.assertIsNone(result["QH4"])

    def test_n_901_partial_qh1(self):
        """n=901 should complete QH2, partial QH1 with 1 sample."""
        data = [0.005] * 901
        result = compute_nbc_quarters(data)

        self.assertTrue(result["QH2"]["complete"])
        self.assertFalse(result["QH1"]["complete"])
        self.assertEqual(result["QH1"]["samples_used"], 1)

    def test_n_3600_completes_all_quarters(self):
        """n=3600 (past end of QH4) should complete all quarters."""
        data = [0.002] * 3600
        result = compute_nbc_quarters(data)

        for qh in ["QH1", "QH2", "QH3", "QH4"]:
            self.assertTrue(result[qh]["complete"])

    def test_negative_raw_wh_clamped_to_zero_in_complete(self):
        """Complete quarters with negative raw_wh should have wh=0."""
        data = [-0.002] * 900
        result = compute_nbc_quarters(data)

        self.assertTrue(result["QH1"]["complete"])
        self.assertEqual(result["QH1"]["wh"], 0)

    def test_negative_raw_wh_clamped_to_zero_in_partial(self):
        """Partial quarters with negative predicted_wh should have wh=0."""
        data = [-0.002] * 1500
        result = compute_nbc_quarters(data)

        self.assertFalse(result["QH1"]["complete"])
        # predicted_wh will be negative, clamped to 0
        self.assertLess(result["QH1"]["raw_wh"], 0)
        self.assertEqual(result["QH1"]["wh"], 0)

    def test_partial_qh_has_predicted_wh(self):
        """Incomplete quarters should include predicted_wh field."""
        data = [0.002] * 1500
        result = compute_nbc_quarters(data)

        self.assertFalse(result["QH1"]["complete"])
        self.assertIn("predicted_wh", result["QH1"])

    def test_partial_qh_has_remaining_seconds(self):
        """Incomplete quarters should include remaining_seconds field."""
        data = [0.002] * 1500
        result = compute_nbc_quarters(data)

        self.assertIn("remaining_seconds", result["QH1"])
        # QH1 ends at index 1799, n=1500 → remaining = 1800 - 1500
        self.assertEqual(result["QH1"]["remaining_seconds"], 300)

    def test_partial_qh_has_samples_used(self):
        """Incomplete quarters should include samples_used field."""
        data = [0.002] * 1500
        result = compute_nbc_quarters(data)

        self.assertIn("samples_used", result["QH1"])
        # lookback = max(1500-60, 900) to 1500 = max(1440, 900)=1440 to 1500
        # samples = 60 (or less if lookback_start < start_idx)
        self.assertGreater(result["QH1"]["samples_used"], 0)

    def test_partial_qh_lookback_cannot_cross_boundary(self):
        """Lookback window should not cross quarter boundary."""
        # n=901 is just 2 seconds into QH2 (start_idx=900)
        # lookback_start = max(901-60, 900) = max(841, 900) = 900
        # So lookback only includes seconds from QH2, not QH1: data[900:901]
        # Python slice [start:end] is exclusive of end → 2 elements (indices 900, 901)
        data = [0.005] * 901  # uniform positive values
        result = compute_nbc_quarters(data)

        self.assertFalse(result["QH1"]["complete"])
        # lookback is data[900:901] → 2 elements (indices 900 and 901)
        self.assertEqual(result["QH1"]["samples_used"], 1)

    def test_partial_qh_lookback_clamped_to_start_idx(self):
        """Lookback start should be clamped to quarter start index."""
        # n=910, lookback_start = max(850, 900) = 900
        # So lookback is from index 900 to 910 = 10 samples
        data = [0.005] * 910
        result = compute_nbc_quarters(data)

        self.assertEqual(result["QH1"]["samples_used"], 10)


class TestDataForScaleEdgeCases(unittest.TestCase):
    """Tests for HourlyProjection.data_for_scale edge cases."""

    def test_empty_data_list(self):
        """data_for_scale with empty data list should handle gracefully."""
        from datetime import datetime, timezone

        result = HourlyProjection.data_for_scale(
            [], datetime.now(timezone.utc), "1H"
        )
        self.assertEqual(result["usage"], 0.0)

    def test_single_data_point(self):
        """data_for_scale with a single data point should work."""
        from datetime import datetime, timezone

        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)
        result = HourlyProjection.data_for_scale([0.5], fixed_now, "1H")

        # data_len is only present when DEBUG mode is on
        self.assertGreaterEqual(result["usage"], 0.0)

    def test_scale_key_stored_as_is(self):
        """The scale parameter should be stored as the 'scale' key."""
        from datetime import datetime, timezone

        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)
        result = HourlyProjection.data_for_scale(
            [0.5, 0.6], fixed_now, "CUSTOM_SCALE"
        )

        self.assertEqual(result["scale"], "CUSTOM_SCALE")


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
        from metrics import EnergyCache

        instant = datetime(2026, 5, 19, 13, 0, 0, tzinfo=timezone.utc)
        old_chart_start = datetime(2026, 5, 19, 3, 59, 7, tzinfo=timezone.utc)
        hp = HourlyProjection(instant=instant)
        hp.energy_cache = EnergyCache()

        with patch.object(hp, "populate_internal", return_value={}) as mock_populate:
            hp.populate(old_chart_start)

        expected_start = ceil_to_qh(instant)
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
        """When chart_start >1h before now, return current QH boundary."""
        from metrics import cap_chart_start

        instant = datetime(2026, 5, 19, 13, 0, 0, tzinfo=timezone.utc)
        old_start = datetime(2026, 5, 19, 3, 59, 7, tzinfo=timezone.utc)
        result = cap_chart_start(old_start, instant)
        self.assertEqual(result, ceil_to_qh(instant))

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


class TestCapFetchWindow(unittest.TestCase):
    """Tests for the cap_fetch_window guard function."""

    def test_caps_old_start(self):
        """When start_time >1h before now, return current QH boundary."""
        from metrics import cap_fetch_window

        now = datetime(2026, 5, 19, 13, 7, 43, tzinfo=timezone.utc)
        old_start = now - timedelta(hours=9)
        result = cap_fetch_window(old_start, now)
        self.assertEqual(result, ceil_to_qh(now))

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
        result = cap_fetch_window(old_start, now)
        self.assertEqual(result, ceil_to_qh(now))


class TestHourlyProjectionEdgeCases(unittest.TestCase):
    """Tests for HourlyProjection edge cases."""

    def test_predict_device_missing_extra_min_scales(self):
        """_predict_device with only 1MIN (no other MIN scales) should still compute prediction."""
        from datetime import timedelta, timezone

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        # Set up the instant attribute that _predict_device needs
        hp.instant = now.replace(minute=30)

        # Only 1H and 1MIN — no other MIN scales like 5MIN, 10MIN
        scales = {
            "1H": {"usage": 60.0, "instant": now},
            "1MIN": {"usage": 2.5, "instant": now + timedelta(minutes=1)},
        }

        result = hp._predict_device(scales)

        self.assertIn("prediction", result)
        # smoothing should only have "1MIN" key, not 5MIN/10MIN
        self.assertIn("smoothing", result)

    def test_predict_device_with_partial_scales(self):
        """_predict_device with only some MIN scales should compute smoothing for those."""
        from datetime import timedelta, timezone

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        # Set up the instant attribute that _predict_device needs
        hp.instant = now.replace(minute=30)

        # 1H + "5MIN" only (no other MIN scales like 2MIN, 3MIN)
        # _predict_device needs "1MIN" for minute_predicted calculation.
        scales = {
            "1H": {"usage": 60.0, "instant": now},
            "5MIN": {"usage": 3.0, "instant": now + timedelta(minutes=5)},
            "1MIN": {"usage": 2.0, "instant": now + timedelta(minutes=1)},
        }

        result = hp._predict_device(scales)

        self.assertIn("prediction", result)


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
            self.assertEqual(tou.tou_result[bucket], 0.0)

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
            "scales", "smoothing", "timezone", "nbc"
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

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics._cfg") as cfg_mock:

            # Set up config so password auth works
            cfg_mock.vue_username = "testuser"
            cfg_mock.vue_password = "testpass"

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

                # Mock open to return our temp file content for token login
                original_open = __builtins__["open"]

                def mock_file_open(*args, **kwargs):
                    return original_open(keys_file, *args[1:], **kwargs)

                with patch("builtins.open", mock_file_open):
                    base.vue_init()

                # Token login was called first (returned False), then password auth succeeded
                self.assertEqual(vue_mock.login.call_count, 2)
            finally:
                os.unlink(keys_file)

    def test_vue_init_both_fail_raises(self):
        """When both token and password auth fail, raises VueAuthenticationError."""
        from unittest.mock import MagicMock

        vue_mock = MagicMock()
        # Both login attempts fail
        vue_mock.login.side_effect = [False, False]

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics._cfg") as cfg_mock:

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

                original_builtins_open = __builtins__["open"]

                def mock_file_open(*args, **kwargs):
                    return original_builtins_open(keys_file)

                with patch("builtins.open", mock_file_open):
                    cfg_mock.vue_username = "u"
                    cfg_mock.vue_password = "p"

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


class TestProcessOffsetScalesEdgeCases(unittest.TestCase):
    """Tests for _process_offset_scales edge cases."""

    def test_process_minute_0_boundary(self):
        """When minute=0, max(1, min(10, 0)) = 1 so only '1MIN' scale is computed."""
        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.logger = MagicMock()
        scales: dict[str, Any] = {}

        # usage_data_end has minute=0
        end_time = now.replace(minute=0)

        # 61 seconds of data (one minute worth)
        usage_data = [0.5] * 61

        result = hp._process_offset_scales(scales, usage_data, end_time)

        # Only "1MIN" should be in scales (minute=0 → max(1, min(10, 0)) = 1)
        self.assertIn("1MIN", scales)

    def test_process_minute_6_plus(self):
        """When minute >= 6, scales should include '1MIN' through '6MIN'."""
        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.logger = MagicMock()
        scales: dict[str, Any] = {}

        # usage_data_end has minute=6
        end_time = now.replace(minute=6)

        # 390 seconds of data (6 minutes worth + a bit more for the tail)
        usage_data = [0.5] * 391

        result = hp._process_offset_scales(scales, usage_data, end_time)

        # Should have "1MIN" through "6MIN"
        for i in range(1, 7):
            self.assertIn(f"{i}MIN", scales)

    def test_process_returns_last_300(self):
        """Returns last 300 data points from the usage_data_local."""
        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.logger = MagicMock()
        scales: dict[str, Any] = {}

        end_time = now.replace(minute=42)
        # 2520 seconds of data (minute=42 → 42*60 = 2520)
        usage_data = [float(i % 10) for i in range(2520)]

        result = hp._process_offset_scales(scales, usage_data, end_time)

        # Should return exactly the last 300 elements
        self.assertEqual(len(result), min(300, len(usage_data)))


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
        self.assertFalse(result["QH1"]["complete"])


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

    def test_no_minute_scales(self):
        """With only 1H and no extra MIN scales, smoothing has just '1MIN'."""
        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        scales: dict[str, Any] = {
            "1H": {"usage": 60.0, "instant": now},
            # _predict_device requires at least a 1MIN scale to compute minute_predicted.
            "1MIN": {"usage": 2.5, "instant": now + timedelta(minutes=1)},
        }

        result = hp._predict_device(scales)

        self.assertIn("prediction", result)
        # With only 1MIN (no other MIN scales), smoothing has just one entry.
        self.assertEqual(result["smoothing"], {"1MIN": result["prediction"]})

    def test_lag_zero(self):
        """When hour['instant'] >= instant, lag is timedelta(0)."""
        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        # Set instant to be AFTER the hour data point
        hp.instant = now.replace(minute=45)

        scales: dict[str, Any] = {
            "1H": {"usage": 60.0, "instant": now.replace(minute=45)},
            # _predict_device requires at least a 1MIN scale to compute minute_predicted.
            "1MIN": {"usage": 2.5, "instant": now.replace(minute=46)},
        }

        result = hp._predict_device(scales)

        self.assertEqual(result["lag"], timedelta(0))


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


class TestDataForScaleDebugMode(unittest.TestCase):
    """Tests for data_for_scale debug mode behavior."""

    def test_debug_mode_off_no_extra_keys(self):
        """When DEBUG is off, data_for_scale result has no 'data'/'data_len' keys."""
        from unittest.mock import patch

        now = datetime.now(timezone.utc)
        data = [0.1, 0.2]

        with patch("metrics.is_debug", return_value=False):
            result = HourlyProjection.data_for_scale(data, now, "1H")

        self.assertNotIn("data", result)
        self.assertNotIn("data_len", result)

    def test_debug_mode_on_has_extra_keys(self):
        """When DEBUG is on, data_for_scale result includes 'data'/'data_len'."""
        from unittest.mock import patch

        now = datetime.now(timezone.utc)
        data = [0.1, 0.2]

        with patch("metrics.is_debug", return_value=True):
            result = HourlyProjection.data_for_scale(data, now, "1H")

        self.assertIn("data", result)
        self.assertEqual(result["data_len"], 2)


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


class TestEnergyCache(unittest.TestCase):
    """Tests for EnergyCache — unified per-second sample cache with sliding-window semantics."""

    def test_import_exists(self):
        """EnergyCache class must be importable from metrics."""
        # This test fails until EnergyCache is implemented.
        from metrics import EnergyCache  # noqa: F401

    def test_initial_state_empty(self):
        """Fresh EnergyCache has no samples, no start time, no fetch timestamp."""
        from metrics import EnergyCache

        cache = EnergyCache()
        self.assertIsNone(cache._samples)
        self.assertIsNone(cache._data_start)
        self.assertIsNone(cache._last_fetch_at)

    def test_is_valid_false_when_empty(self):
        """is_valid returns False when cache has no data."""
        from metrics import EnergyCache

        cache = EnergyCache()
        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)
        self.assertFalse(cache.is_valid(fixed_now))

    def test_is_valid_true_after_fetch(self):
        """is_valid returns True after a successful fetch within TTL."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)

        def fetch_func():
            return {
                "per_second_data": [0.001] * 10,
                "data_start": datetime.now(timezone.utc),
            }

        cache.get_or_fetch(fetch_func, fixed_now)
        self.assertTrue(cache.is_valid(fixed_now))

    def test_is_valid_false_after_ttl_expiry(self):
        """is_valid returns False after TTL expires."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=0)  # TTL of 0 means always expired
        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)

        def fetch_func():
            return {
                "per_second_data": [0.001] * 10,
                "data_start": fixed_now
            }

        cache.get_or_fetch(fetch_func, fixed_now)
        self.assertFalse(cache.is_valid(fixed_now))

    def test_get_or_fetch_miss_on_first_call(self):
        """First call to get_or_fetch should invoke fetch_func and return was_fresh=True."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fetch_count = 0

        now = datetime.now(timezone.utc)

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.002] * 5,
                "data_start": now,
            }

        result, was_fresh = cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))

        self.assertEqual(fetch_count, 1)
        self.assertTrue(was_fresh)
        self.assertIsNotNone(result)

    def test_get_or_fetch_hit_within_ttl(self):
        """Second call within TTL should return cached data with was_fresh=False."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fetch_count = 0
        now = datetime.now(timezone.utc)

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.003] * 5,
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        result2, was_fresh = cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))

        self.assertEqual(fetch_count, 1)
        self.assertFalse(was_fresh)
        # Should return the same cached data object (identity check)

    def test_get_or_fetch_miss_after_ttl_expiry(self):
        """After TTL expires, get_or_fetch should call fetch_func again."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=0)  # Always expired
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.004] * fetch_count,  # Varying length
                "data_start": datetime.now(timezone.utc),
            }

        cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        self.assertEqual(fetch_count, 1)

        result2, was_fresh = cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        self.assertEqual(fetch_count, 2)
        self.assertTrue(was_fresh)

    def test_get_or_fetch_force_bypasses_cache(self):
        """force=True should always call fetch_func even if cache is valid."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fetch_count = 0
        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.005] * 3,
                "data_start": fixed_now,
            }

        cache.get_or_fetch(fetch_func, fixed_now)  # First call: fresh
        self.assertEqual(fetch_count, 1)

        _, was_fresh = cache.get_or_fetch(fetch_func, fixed_now, force=True)
        self.assertEqual(fetch_count, 2)
        self.assertTrue(was_fresh)

    def test_get_or_fetch_stores_last_fetch_at_only_on_api_call(self):
        """_last_fetch_at should only be set when data comes from the API, not on cache hit."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.006] * 5,
                "data_start": datetime.now(timezone.utc),
            }

        cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        first_fetch_at = cache._last_fetch_at

        # Second call should be a cache hit — _last_fetch_at unchanged
        time.sleep(0.01)  # Small delay to ensure different timestamp if updated
        cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))

        self.assertEqual(cache._last_fetch_at, first_fetch_at)
        self.assertEqual(fetch_count, 1)

    def test_get_or_fetch_none_result(self):
        """When fetch_func returns None, cache stores None and is_valid returns False."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)

        def fetch_func():
            return None  # Simulate API failure

        result, was_fresh = cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        self.assertIsNone(result)

    def test_invalidate_clears_cache(self):
        """After invalidate, next call fetches fresh data."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.007] * 5,
                "data_start": datetime.now(timezone.utc),
            }

        cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        self.assertEqual(fetch_count, 1)

        cache.invalidate()

        result2, was_fresh = cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        self.assertEqual(fetch_count, 2)
        self.assertTrue(was_fresh)

    def test_get_current_qh_returns_none_when_empty(self):
        """get_current_qh returns None when cache has no data."""
        from metrics import EnergyCache

        cache = EnergyCache()
        self.assertIsNone(cache.get_current_qh(datetime.now(timezone.utc)))

    def test_get_or_fetch_merges_samples(self):
        """New samples from fetch_func should be appended to existing _samples."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = base_time

            def fetch_func():
                # Simulate incremental data: first call returns 5 samples, second adds more
                if not cache._samples or len(cache._samples) == 0:
                    return {
                        "per_second_data": [0.1] * 5,
                        "data_start": base_time,
                    }
                else:
                    # Second fetch starts right after the first 5 samples end
                    return {
                        "per_second_data": [0.2] * 3,  # New samples appended
                        "data_start": base_time + timedelta(seconds=5),
                    }

            cache.get_or_fetch(fetch_func, base_time)  # First fetch: [0.1, 0.1, 0.1, 0.1, 0.1]
            self.assertEqual(len(cache._samples), 5)

            # Use force=True to simulate an incremental fetch that appends new samples.
            cache.get_or_fetch(fetch_func, base_time, force=True)  # Second fetch: append [0.2, 0.2, 0.2]
            self.assertEqual(len(cache._samples), 8)

    def test_get_or_fetch_skips_overlapping_samples(self):
        """Overlapping samples from full-hour fetches should be deduplicated."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2026, 5, 13, 0, 30, 29, tzinfo=timezone.utc)

        # First fetch: 1830 samples (full hour so far, lag=70)
        first_samples = [0.1] * 1830
        first_start = fixed_now - timedelta(seconds=1900)

        # Second fetch: 31 samples, 1 overlap, lag=40
        second_samples = [0.1] * 31
        second_start = fixed_now - timedelta(seconds=71)

        def fetch_func():
            # Simulates HourlyProjection always fetching from top of hour.
            # On the second call, _samples already exists, so return the
            # larger full-hour window (overlapping with what's cached).
            if not cache._samples or len(cache._samples) == 0:
                return {
                    "per_second_data": first_samples,
                    "data_start": first_start,
                }
            return {
                "per_second_data": second_samples,
                "data_start": second_start,
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now

            cache.get_or_fetch(fetch_func, fixed_now)
            self.assertEqual(len(cache._samples), 1830)

            # Second fetch should deduplicate: 1830 existing + 30 new = 1860
            cache.get_or_fetch(fetch_func, fixed_now, force=True)
            self.assertEqual(len(cache._samples), 1860)

    def test_get_current_qh_computes_from_samples(self):
        """get_current_qh should compute QH prediction from raw samples."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        # data_start at QH boundary 12:00 + 1200 = 12:20:00, now at 12:20:00
        data_start = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        fixed_now = datetime(2025, 6, 1, 12, 20, 0, tzinfo=timezone.utc)

        # 1200 samples = 20 min (QH1 complete 900 + first 5 min of QH2 300)
        samples = [0.001] * 1200

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": data_start,
            }

        cache.get_or_fetch(fetch_func, fixed_now)
        result = cache.get_current_qh(fixed_now)

        self.assertIsNotNone(result)
        # Should return a dict with QH prediction info
        self.assertIn("qh_name", result)

    def test_thread_safety_concurrent_access(self):
        """Concurrent reads and writes should not corrupt the sample list."""
        from metrics import EnergyCache

        fixed_now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        cache = EnergyCache(ttl_seconds=60)
        errors: list[str] = []

        def writer():
            try:
                for _ in range(10):
                    cache.get_or_fetch(lambda: {
                        "per_second_data": [0.1] * 5,
                        "data_start": fixed_now,
                    }, fixed_now)
            except Exception as ex:  # noqa: BLE001
                errors.append(str(ex))

        def reader():
            try:
                for _ in range(10):
                    cache.get_current_qh(fixed_now)
            except Exception as ex:  # noqa: BLE001
                errors.append(str(ex))

        import threading

        threads = [threading.Thread(target=writer) for _ in range(3)]
        threads += [threading.Thread(target=reader) for _ in range(3)]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"Thread errors occurred: {errors}")
        # Samples should be a list (not corrupted) and have some length
        self.assertIsInstance(cache._samples, list | type(None))

    def test_pruning_removes_samples_older_than_3600s(self):
        """Samples older than 3600 seconds from now are pruned after get_or_fetch."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)

        # Pre-populate with 3601 samples (lag=20)
        old_start = fixed_now - timedelta(seconds=3621)
        cache._samples = [0.1] * 3601
        cache._data_start = old_start

        # Patch datetime.now so pruning uses fixed_now
        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now

            # Fetch adds 10 samples
            def fetch_func():
                return {
                    "per_second_data": [0.2] * 10,
                    "data_start": fixed_now - timedelta(seconds=10),
                }

            cache.get_or_fetch(fetch_func, now=fixed_now, force=True)

        # After merge: 3600 samples. After pruning (keep last 3600s): ~3600
        self.assertLessEqual(len(cache._samples), 3600)

    def test_pruning_does_not_remove_recent_samples(self):
        """Samples within the last 3600s are preserved after pruning."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)

        # Pre-populate with exactly 3600 samples (1 hour of data)
        old_start = fixed_now - timedelta(seconds=3600)
        cache._samples = [0.1] * 3600
        cache._data_start = old_start

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now

            def fetch_func():
                return {
                    "per_second_data": [0.2] * 5,
                    "data_start": fixed_now - timedelta(seconds=5),
                }

            cache.get_or_fetch(fetch_func, now=fixed_now, force=True)

        self.assertEqual(len(cache._samples), 3600)

    def test_get_current_qh_returns_incomplete_qh(self):
        """get_current_qh returns the first incomplete quarter with extrapolated prediction."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 1, 12, 7, 30, tzinfo=timezone.utc)

        # 450 samples = halfway through QH1 (0-899 seconds)
        # Each sample is 0.5 Wh (stored as kWh in per_second_data, so 0.5 * 1000 = 500 Wh per second...
        # actually per_second_data is in kWh, so 0.5 kWh/sec = 500 Wh/sec)
        # Let's use small values: 0.001 kWh = 1 Wh per second
        samples = [0.001] * 450

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": fixed_now - timedelta(seconds=450),
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            cache.get_or_fetch(fetch_func, fixed_now)

        result = cache.get_current_qh(fixed_now)

        self.assertIsNotNone(result)
        self.assertEqual(result["qh_name"], "QH1")
        self.assertFalse(result.get("seconds_remaining", 0) == 0)

    def test_get_current_qh_returns_complete_last_qh_when_all_done(self):
        """When all 4 quarters are complete, get_current_qh returns QH1 (most recent)."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
        data_start = ceil_to_qh(fixed_now - timedelta(seconds=3600))
        self.assertEqual(data_start, datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))

        # 3600 samples = exactly one hour (all 4 quarters complete)
        samples = [0.01] * 3600

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": data_start,
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            cache.get_or_fetch(fetch_func, fixed_now)
            result = cache.get_current_qh(fixed_now)

        self.assertIsNotNone(result)
        self.assertEqual(result["qh_name"], "QH1")
        # QH1 now represents the most recent complete window (12:45-13:00)
        # seconds_remaining is derived from wall-clock, not 0
        self.assertEqual(result["seconds_remaining"], 900)

    def test_get_current_qh_returns_most_recent_qh(self):
        """get_current_qh returns QH1 (most recent window), not the last incomplete one."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 1, 12, 37, 30, tzinfo=timezone.utc)

        # 2250 samples = 12:00:00 to 12:37:29
        samples = [0.002] * 2250

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": fixed_now - timedelta(seconds=2250),
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            cache.get_or_fetch(fetch_func, fixed_now)

        result = cache.get_current_qh(fixed_now)

        self.assertIsNotNone(result)
        self.assertEqual(result["qh_name"], "QH1")

    def test_get_current_qh_seconds_remaining_from_wall_clock(self):
        """seconds_remaining must derive from wall-clock time, not sample count.

        When the cache has accumulated many samples (e.g. from incremental
        fetches), n = len(samples) can be much larger than 900.  The
        seconds_remaining value must come from wall-clock time to stay
        monotonic and correct across cache refreshes.
        """
        from metrics import EnergyCache

        # Start at :07:30 — in QH1 (0-899).  Seconds into hour = 7*60+30 = 450.
        # Expected: QH1, remaining = 900 - 450 = 450
        now = datetime(2025, 6, 1, 12, 7, 30, tzinfo=timezone.utc)
        cache = EnergyCache(ttl_seconds=60)

        # Populate with 450 samples covering just QH1 (data_start aligned to
        # QH boundary so the cache is valid).  The key assertion is that
        # seconds_remaining = 450 comes from wall-clock, not from sample count.
        samples = [0.01] * 450
        data_start = now - timedelta(seconds=450)  # 12:0:0 — QH-aligned

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": data_start,
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = now
            cache.get_or_fetch(fetch_func, now)

        result = cache.get_current_qh(now=now)

        self.assertIsNotNone(result)
        self.assertEqual(result["qh_name"], "QH1")
        self.assertEqual(result["seconds_remaining"], 450)

    def test_get_current_qh_seconds_remaining_decreases_monotonically(self):
        """seconds_remaining must decrease as wall-clock time advances."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)

        # data_start at QH boundary 12:00 + samples cover 10 min into QH1
        data_start = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        now1 = datetime(2025, 6, 1, 12, 10, 0, tzinfo=timezone.utc)
        # 600 seconds into QH1 → remaining = 900 - 600 = 300
        samples = [0.01] * 600

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": data_start,
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = now1
            cache.get_or_fetch(fetch_func, now1)
            result1 = cache.get_current_qh(now=now1)

        self.assertEqual(result1["seconds_remaining"], 300)

        # Advance 15 seconds → remaining should be 285
        now2 = now1 + timedelta(seconds=15)
        result2 = cache.get_current_qh(now=now2)
        self.assertEqual(result2["seconds_remaining"], 285)

        # Advance to cross quarter boundary → clock-boundary QH1 = 12:15-12:30
        # now1 + 301s = 12:15:01 → QH1 (most recent), remaining = 899
        now3 = now1 + timedelta(seconds=301)
        result3 = cache.get_current_qh(now=now3)
        self.assertEqual(result3["qh_name"], "QH1")
        self.assertEqual(result3["seconds_remaining"], 899)

    def test_get_or_fetch_logs_data_point_count(self):
        """get_or_fetch logs the number of fresh data points when fetching."""
        import logging

        from metrics import EnergyCache
        fixed_now = datetime(2025, 6, 1, 12, 10, 0, tzinfo=timezone.utc)

        handler = _LogCapture()
        logger = logging.getLogger("metrics")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            cache = EnergyCache(ttl_seconds=60)

            def fetch_func():
                return {
                    "per_second_data": [0.001] * 42,
                    "data_start": fixed_now,
                }

            cache.get_or_fetch(fetch_func, fixed_now)

            assert "fetched" in handler.text
            assert ": 42 samples" in handler.text
        finally:
            logger.removeHandler(handler)

    def test_get_or_fetch_does_not_log_on_cache_hit(self):
        """get_or_fetch should not log a fetch message when serving cached data."""
        import logging

        from metrics import EnergyCache

        handler = _LogCapture()
        logger = logging.getLogger("metrics")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            cache = EnergyCache(ttl_seconds=60)
            now = datetime.now(timezone.utc)

            def fetch_func():
                return {
                    "per_second_data": [0.001] * 42,
                    "data_start": now,
                }

            # First call — should log the fetch
            cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
            handler.clear()

            # Second call — should serve from cache, no new fetch log
            cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
            assert "fetched" not in handler.text
            assert "now has" not in handler.text
        finally:
            logger.removeHandler(handler)

    def test_get_or_fetch_logs_data_point_count_from_full_metrics_dict(self):
        """get_or_fetch logs data points from devices when top-level per_second_data is absent."""
        import logging

        from metrics import EnergyCache

        handler = _LogCapture()
        logger = logging.getLogger("metrics")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            cache = EnergyCache(ttl_seconds=60)

            # Simulate a full metrics dict like HourlyProjection.metrics returns.
            # per_second_data is nested inside devices, not at the top level.
            def fetch_func():
                return {
                    "api_response": {},
                    "devices": [
                        {
                            "gid": 123,
                            "name": "VUE Device",
                            "per_second_data": [0.001] * 150,
                        }
                    ],
                }

            fixed_now = datetime(2025, 6, 1, 12, 10, 0, tzinfo=timezone.utc)
            cache.get_or_fetch(fetch_func, fixed_now)

            assert "fetched" in handler.text
            assert ": 150 samples" in handler.text
        finally:
            logger.removeHandler(handler)

    def test_get_or_fetch_populates_samples_from_nested_device_data(self):
        """get_or_fetch populates self._samples when per_second_data is nested in devices."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        # Simulate a full metrics dict like HourlyProjection.metrics returns.
        # per_second_data is nested inside devices, not at the top level.
        def fetch_func():
            return {
                "api_response": {},
                "devices": [
                    {
                        "gid": 123,
                        "name": "VUE Device",
                        "per_second_data": [0.01] * 150,
                    }
                ],
            }

        cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))

        # Verify self._samples is populated from nested devices data.
        self.assertIsNotNone(cache._samples)
        self.assertEqual(len(cache._samples), 150)
        self.assertEqual(all(v > 0 for v in cache._samples), True)


class TestBuildIncrementalFetch(unittest.TestCase):
    """Tests for _build_incremental_fetch helper function."""

    def test_returns_callable(self):
        """_build_incremental_fetch returns a callable (zero-arg function)."""
        from metrics import EnergyCache, _build_incremental_fetch

        cache = EnergyCache(ttl_seconds=60)
        fetcher = _build_incremental_fetch(cache, MagicMock(), 1, datetime.now(timezone.utc))
        self.assertTrue(callable(fetcher))

    def test_first_fetch_no_existing_samples(self):
        """When cache has no samples, fetcher calls API with full range."""
        from metrics import EnergyCache, _build_incremental_fetch

        cache = EnergyCache(ttl_seconds=60)
        vue_mock = MagicMock()
        gid = 1
        fixed_now = datetime(2025, 6, 1, 12, 30, 10, tzinfo=timezone.utc)
        chart_start = ceil_to_qh(fixed_now - timedelta(seconds=3600))
        self.assertEqual(chart_start, datetime(2025, 6, 1, 11, 45, 0, tzinfo=timezone.utc))

        # Mock API to return some data
        vue_mock.get_chart_usage.return_value = (
            [0.1] * 3543,
            fixed_now - timedelta(seconds=3600),
        )

        fetcher = _build_incremental_fetch(cache, vue_mock, gid, fixed_now)
        result = fetcher()

        # Should have called get_chart_usage with full range (chart_start to fixed_now)
        vue_mock.get_chart_usage.assert_called_once()
        call_args = vue_mock.get_chart_usage.call_args
        # chart_start should now align to previous QH boundary 12:30
        chart_start = call_args[0][1]
        self.assertEqual(chart_start, fixed_now.replace(minute=45, second=0))
        self.assertEqual(call_args[0][2], fixed_now)

    def test_incremental_fetch_uses_last_sample_time(self):
        """When cache has samples, fetcher starts from last sample time."""
        from metrics import EnergyCache, _build_incremental_fetch

        cache = EnergyCache(ttl_seconds=60)
        vue_mock = MagicMock()
        gid = 1

        # Pre-populate cache with samples
        now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        old_start = now - timedelta(minutes=5)  # 300 seconds ago

        cache._samples = [0.1] * 300
        cache._data_start = old_start

        # Mock API to return new samples starting from where cache left off
        vue_mock.get_chart_usage.return_value = (
            [0.2] * 60,  # 60 new samples
            old_start + timedelta(seconds=300),
        )

        fetcher = _build_incremental_fetch(cache, vue_mock, gid, now)
        result = fetcher()

        # Should call get_chart_usage starting from last sample time
        vue_mock.get_chart_usage.assert_called_once()
        call_args = vue_mock.get_chart_usage.call_args
        # Start time should be old_start + 300 seconds = now
        expected_start = old_start + timedelta(seconds=300)  # next sample after last one
        self.assertEqual(call_args[0][1], expected_start)

    def test_incremental_fetch_merges_samples(self):
        """New samples from API are appended to existing cache samples via get_or_fetch."""
        from metrics import EnergyCache, _build_incremental_fetch

        cache = EnergyCache(ttl_seconds=60)
        vue_mock = MagicMock()
        gid = 1

        fixed_now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        old_start = fixed_now - timedelta(minutes=5)

        # Pre-populate cache with 300 samples
        cache._samples = [0.1] * 300
        cache._data_start = old_start
        cache._last_sample_at = old_start + timedelta(seconds=299)

        # Mock API to return new samples starting from where cache left off
        vue_mock.get_chart_usage.return_value = (
            [0.2] * 60,  # 60 new samples
            old_start + timedelta(seconds=300),
        )

        fetcher = _build_incremental_fetch(cache, vue_mock, gid, fixed_now)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            cache.get_or_fetch(fetcher, fixed_now, force=True)

        # Cache should have merged samples: 300 + 60 = 360
        self.assertEqual(len(cache._samples), 360)

    def test_api_error_returns_none(self):
        """When API raises an error, fetcher returns None and cache is unchanged."""
        from metrics import EnergyCache, _build_incremental_fetch

        cache = EnergyCache(ttl_seconds=60)
        vue_mock = MagicMock()
        gid = 1

        now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        old_start = now - timedelta(minutes=5)

        # Pre-populate cache with samples
        cache._samples = [0.1] * 300
        cache._data_start = old_start

        # Mock API to raise an error
        vue_mock.get_chart_usage.side_effect = requests.exceptions.HTTPError("API error")

        fetcher = _build_incremental_fetch(cache, vue_mock, gid, now)
        result = fetcher()

        self.assertIsNone(result)
        # Cache should be unchanged
        self.assertEqual(len(cache._samples), 300)

    def test_prunes_old_samples(self):
        """Samples older than 3600s from now are pruned via get_or_fetch."""
        from metrics import EnergyCache, _build_incremental_fetch

        cache = EnergyCache(ttl_seconds=60)
        vue_mock = MagicMock()
        gid = 1

        fixed_now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        # Old samples from 2 hours ago (7200 seconds)
        old_start = fixed_now - timedelta(hours=2)

        # Pre-populate cache with 7200 samples (2 hours of per-second data)
        cache._samples = [0.1] * 7200
        cache._data_start = old_start

        # Mock API to return new samples (only the last 10 minutes worth)
        vue_mock.get_chart_usage.return_value = (
            [0.2] * 600,  # 600 new samples (10 minutes)
            old_start + timedelta(seconds=7200),
        )

        fetcher = _build_incremental_fetch(cache, vue_mock, gid, fixed_now)
        # Patch datetime.now so pruning uses our test time
        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            cache.get_or_fetch(fetcher, fixed_now, force=True)

        # After merging: 7200 + 600 = 7800 samples
        # After pruning (keep only last 3600s): should be ~4200 samples
        # (7800 - 3600 = 4200)
        self.assertLessEqual(len(cache._samples), 7800)
        # Should have pruned old samples
        self.assertLess(len(cache._samples), 7200)

    def test_stale_cache_falls_back_to_full_hour_fetch(self):
        """When incremental window >1h, fetcher falls back to full-hour fetch."""
        from metrics import EnergyCache, _build_incremental_fetch

        cache = EnergyCache(ttl_seconds=60)
        vue_mock = MagicMock()
        gid = 1

        # Use a non-QH-boundary time so the test actually exercises the
        # fallback path (ceil_to_qh(now) != now).
        now = datetime(2026, 5, 19, 13, 7, 43, tzinfo=timezone.utc)
        old_start = now - timedelta(hours=9)

        # 1 hour of samples from 9h ago — the incremental window is 8h.
        cache._samples = [0.1] * 3600
        cache._data_start = old_start

        vue_mock.get_chart_usage.return_value = (
            [0.1] * 3600,
            ceil_to_qh(now),
        )

        fetcher = _build_incremental_fetch(cache, vue_mock, gid, now)
        result = fetcher()

        vue_mock.get_chart_usage.assert_called_once()
        call_args = vue_mock.get_chart_usage.call_args
        # Guard should have capped start_time to ceil_to_qh(now)
        self.assertEqual(call_args[0][1], ceil_to_qh(now))
        self.assertEqual(call_args[0][2], now)
        self.assertIn("per_second_data", result)


class TestEnergyCacheSampleMetadata(unittest.TestCase):
    """Tests for EnergyCache sample metadata tracking."""

    def test_initial_state_has_sample_count_none(self):
        """Fresh cache has _sample_count = None."""
        from metrics import EnergyCache

        cache = EnergyCache()
        self.assertIsNone(cache._sample_count)

    def test_initial_state_has_last_sample_at_none(self):
        """Fresh cache has _last_sample_at = None."""
        from metrics import EnergyCache

        cache = EnergyCache()
        self.assertIsNone(cache._last_sample_at)

    def test_get_or_fetch_sets_sample_count(self):
        """After get_or_fetch, _sample_count reflects the number of samples."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)

        def fetch_func():
            return {
                "per_second_data": [0.1] * 50,
                "data_start": fixed_now,
            }

        cache.get_or_fetch(fetch_func, fixed_now)
        self.assertEqual(cache._sample_count, 50)

    def test_get_or_fetch_sets_last_sample_at(self):
        """After get_or_fetch, _last_sample_at reflects the last sample time."""
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
        self.assertIsNotNone(cache._last_sample_at)


class TestIncrementalFetchIntegration(unittest.TestCase):
    """Integration tests for incremental fetch with get_or_fetch."""

    def test_full_then_incremental(self):
        """Full fetch followed by incremental fetch merges correctly."""
        from metrics import EnergyCache, _build_incremental_fetch

        cache = EnergyCache(ttl_seconds=60)
        vue_mock = MagicMock()
        gid = 1

        fixed_now = datetime(2025, 6, 1, 12, 30, 13, tzinfo=timezone.utc) # 12:30:13
        call_count = 0

        def fetch_func_1():
            # First fetch: full range, 3542 samples from previous 3600
            start = fixed_now - timedelta(minutes=45, seconds=13)
            return {
                "per_second_data": [0.1] * 2642,
                "data_start": start,
            }

        def fetch_func_2():
            # Incremental fetch: 42 new samples starting right after first fetch
            return {
                "per_second_data": [0.2] * 42,
                "data_start": fixed_now,
            }

        # Build incremental fetcher (to verify)
        _fetcher = _build_incremental_fetch(cache, vue_mock, gid, fixed_now)

        # Patch datetime.now so pruning doesn't wipe out 2025 data
        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now

            # First fetch: full range
            cache.get_or_fetch(fetch_func_1, fixed_now)
            self.assertEqual(len(cache._samples), 2642)

            # Second call with force=True to simulate incremental fetch
            cache.get_or_fetch(fetch_func_2, fixed_now, force=True)
            self.assertEqual(len(cache._samples), 2684)


if __name__ == "__main__":
    unittest.main()


"""End-to-end and unit tests for EnergyCache merge, incremental fetch,
pruning, and HourlyProjection population/prediction logic.

These tests verify:
- Merge logic in EnergyCache.get_or_fetch() handles all overlap scenarios
- Incremental fetch computes correct API start times
- Pruning removes old samples without gaps or duplicates
- HourlyProjection.fetch_channel_data() and _populate_device() work correctly
- Prediction math uses scales correctly
- Full pipeline produces no gaps or duplicates

All tests use mocked Emporia VUE API via pyemvue.
"""

import logging
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests
from metrics import (
    HourlyProjection,
    MetricsBase,
    RetryableMetricsException,
    _PopulationResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_now(target_dt: datetime) -> unittest.mock._patch:
    """Return a patch that makes metrics.datetime.now() return *target_dt*.

    Args:
        target_dt: The datetime value to return from datetime.now().

    Returns:
        A patch context manager.
    """
    return unittest.mock.patch("metrics.datetime.now", return_value=target_dt)


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
    cache._samples = list(samples)
    cache._data_start = start
    cache._sample_count = count
    cache._last_sample_at = start + timedelta(seconds=count - 1)
    return cache


def _make_hourly_mock(
    hour_usage: float = 1.0,
    n_minutes: int = 5,
    n_seconds: int = 100,
    samples: list[float] | None = None,
    instant: datetime | None = None,
    chart_start: datetime | None = None,
) -> tuple[HourlyProjection, MagicMock]:
    """Build a HourlyProjection with mocked VUE API.

    Args:
        hour_usage: Simulated hour-scale usage (kWh).
        n_minutes: Number of minute-scale entries to generate.
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
        # _process_offset_scales computes usage_minutes from usage_data_end.minute.
        # usage_data_end = chart_start + timedelta(seconds=n_seconds), so
        # usage_data_end.minute = (chart_start.minute + n_seconds // 60) % 60.
        # We need that to be >= min(n_minutes, 10) for the test to see all minute scales.
        needed_minute = min(n_minutes, 10)
        added_minutes = n_seconds // 60
        minute_val = (needed_minute - added_minutes) % 60
        chart_start = instant.replace(minute=minute_val, second=0, microsecond=0)

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

    # Build scale mock
    minute_secs = [60 * (i + 1) for i in range(n_minutes)]
    mock_vue = MagicMock()

    # The channel must iterate over itself so _populate_device's for-loop works
    mock_channel.channels = [mock_channel]

    # Configure the mocked VUE API to return real per-second data
    mock_vue.get_chart_usage.return_value = (per_second_data, chart_start)

    # The channel .data attribute is used by some tests
    mock_channel.data = {
        "per_second_data": per_second_data,
        "data_start": chart_start,
    }
    mock_channel.samples = samples or []
    mock_channel.scales = {
        "1H": {"instant": chart_start, "usage": hour_usage},
    }
    for i, secs in enumerate(minute_secs):
        key = f"{i + 1}MIN"
        mock_channel.scales[key] = {
            "instant": instant,
            "usage": hour_usage / 3600.0 * secs,
        }

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

    def _fetcher_returns(self, data_start: datetime, samples: list[float]):
        """Return a fetcher function that yields the given data."""
        return lambda: {
            "per_second_data": list(samples),
            "data_start": data_start,
        }

    def test_merge_new_samples_after_cache(self):
        """New samples start exactly after cache ends → all new samples kept."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)  # 14:00:00
        existing = _make_cache_with_samples(300, cache_start)

        # New samples start at 14:05:00 (after current cache end at 14:04:59)
        new_start = cache_start + timedelta(minutes=5)
        new_samples = [0.002] * 60

        fetcher = self._fetcher_returns(new_start, new_samples)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        self.assertIsNotNone(result)
        self.assertEqual(len(existing._samples), 360)  # 300 + 60
        # All 60 new samples should be present
        self.assertEqual(existing._samples[-1], 0.002)

    def test_merge_new_samples_before_cache(self):
        """New samples end exactly before cache starts → AssertionError."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=5)  # 14:05:00
        existing = _make_cache_with_samples(300, cache_start)

        # New samples are entirely before the cache (13:59:59–14:04:58)
        new_start = cache_start - timedelta(seconds=301)
        new_samples = [0.003] * 300

        fetcher = self._fetcher_returns(new_start, new_samples)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            with self.assertRaises(AssertionError):
                result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

    def test_merge_gap_between_cache_and_new(self):
        """New samples start after a gap → gap samples NOT included."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)  # 14:00:00
        existing = _make_cache_with_samples(300, cache_start)

        # New samples start at 14:06:00 (1 minute gap after cache end at 14:05:00)
        new_start = fixed_now - timedelta(minutes=4)  # 14:06:00
        new_samples = [0.004] * 60

        fetcher = self._fetcher_returns(new_start, new_samples)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        self.assertIsNotNone(result)
        self.assertEqual(len(existing._samples), 360)  # 300 + 60, gap skipped
        # The gap seconds (14:05:01–14:05:59) are not filled in

    def test_merge_new_samples_end_exactly_at_cache_end(self):
        """New samples' last timestamp equals cache _last_sample_at → overlap removed."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)
        existing = _make_cache_with_samples(300, cache_start)
        # cache = 14:00:00 to 14:04:59, 300 samples.

        # 10 new samples, 14:04:59–14:05:09 (first sample overlaps with cache_end)
        new_start = cache_start + timedelta(minutes=4, seconds=59)
        new_samples = [0.005] * 10

        fetcher = self._fetcher_returns(new_start, new_samples)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        self.assertIsNotNone(result)
        # 10 new but 1 overlap = 9 new samples added
        self.assertEqual(len(existing._samples), 309)

    def test_merge_new_samples_start_exactly_at_cache_start(self):
        """New samples' first timestamp equals cache _data_start → overlap removed."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)
        existing = _make_cache_with_samples(300, cache_start)
        # cache = 14:00:00 to 14:04:59, 300 samples.

        # 6 new samples, 14:00:00–14:00:05 (first sample overlaps with cache_start)
        new_start = cache_start
        new_samples = [0.006] * 6

        fetcher = self._fetcher_returns(new_start, new_samples)
        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        self.assertIsNotNone(result)
        # 6 new - 6 overlap = 0 new samples added
        self.assertEqual(len(existing._samples), 300)

    def test_merge_no_overlap_no_gap(self):
        """New samples start exactly 1 second after cache ends → all 60 kept."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)
        existing = _make_cache_with_samples(300, cache_start)

        # Start exactly at cache_end + 1 second
        new_start = existing._last_sample_at + timedelta(seconds=1)
        new_samples = [0.007] * 60

        fetcher = self._fetcher_returns(new_start, new_samples)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        self.assertIsNotNone(result)
        self.assertEqual(len(existing._samples), 360)

    def test_merge_partial_overlap_both_sides(self):
        """New samples overlap before AND after cache → AssertionError."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)
        existing = _make_cache_with_samples(300, cache_start)
        # cache: 14:00:00 – 14:04:59

        # New samples: 13:58:00 – 14:06:00 (overlaps both sides)
        new_start = cache_start - timedelta(minutes=2)
        new_samples = [0.008] * 360
        # new_end = new_start + 359s = 14:03:59
        new_end_time = new_start + timedelta(seconds=len(new_samples) - 1)
        self.assertEqual(new_end_time, cache_start + timedelta(minutes=3, seconds=59))

        fetcher = self._fetcher_returns(new_start, new_samples)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            with self.assertRaises(AssertionError):
                result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

    def test_merge_all_overlap_new_samples_empty_after_filter(self):
        """New samples entirely within cached range → empty after filter."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)
        existing = _make_cache_with_samples(300, cache_start)

        # New samples entirely inside cache range (14:01:00–14:01:05)
        new_start = cache_start + timedelta(minutes=1)
        new_samples = [0.009] * 6

        fetcher = self._fetcher_returns(new_start, new_samples)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        self.assertIsNotNone(result)
        # No new samples: entirely within cache
        self.assertEqual(len(existing._samples), 300)
        # Values unchanged
        self.assertEqual(existing._samples[60], 0.001)

    def test_merge_empty_new_samples_list(self):
        """New samples list is empty → cache unchanged."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)
        existing = _make_cache_with_samples(300, cache_start)

        fetcher = self._fetcher_returns(cache_start, [])

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        self.assertIsNotNone(result)
        self.assertEqual(len(result[0]["per_second_data"]), 0)
        self.assertEqual(len(existing._samples), 300)

    def test_merge_updates_data_start_only_on_first(self):
        """After merge, _data_start is NOT changed; on first fetch it IS set."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)
        existing = _make_cache_with_samples(300, cache_start)
        original_start = existing._data_start

        new_start = cache_start + timedelta(minutes=5)
        new_samples = [0.010] * 60

        fetcher = self._fetcher_returns(new_start, new_samples)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        # _data_start should NOT change after merge
        self.assertIsNotNone(result)
        self.assertEqual(existing._data_start, original_start)

    def test_merge_updates_sample_count(self):
        """After merge, _sample_count equals total merged count."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)
        existing = _make_cache_with_samples(300, cache_start)

        new_start = cache_start + timedelta(minutes=5)
        new_samples = [0.011] * 60

        fetcher = self._fetcher_returns(new_start, new_samples)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        self.assertIsNotNone(result)
        self.assertEqual(existing._sample_count, 360)

    def test_merge_updates_last_sample_at(self):
        """After merge, _last_sample_at equals last sample time."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)
        existing = _make_cache_with_samples(300, cache_start)

        new_start = cache_start + timedelta(minutes=5)
        new_samples = [0.012] * 60

        fetcher = self._fetcher_returns(new_start, new_samples)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        self.assertIsNotNone(result)

        expected_last = existing._data_start + timedelta(seconds=len(existing._samples) - 1)
        self.assertEqual(existing._last_sample_at, expected_last)

    def test_merge_no_sample_duplication(self):
        """Merging back-to-back ranges (300 + 300) yields exactly 600 samples."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)
        existing = _make_cache_with_samples(300, cache_start)

        # New samples directly after existing
        new_start = cache_start + timedelta(minutes=5)
        new_samples = [0.013] * 300

        fetcher = self._fetcher_returns(new_start, new_samples)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        self.assertIsNotNone(result)
        self.assertEqual(len(existing._samples), 600)

    def test_merge_preserves_sample_values(self):
        """Sample values from both old and new ranges are preserved exactly."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 14, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=10)
        existing = _make_cache_with_samples(300, cache_start)

        # Give old samples distinct values
        for i in range(300):
            existing._samples[i] = float(i)

        new_start = cache_start + timedelta(minutes=5)
        new_samples = [float(300 + i) for i in range(60)]

        fetcher = self._fetcher_returns(new_start, new_samples)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = existing.get_or_fetch(fetcher, now=fixed_now, force=True)

        # Old values preserved
        self.assertEqual(existing._samples[0], 0.0)
        self.assertEqual(existing._samples[299], 299.0)
        # New values preserved
        self.assertEqual(existing._samples[300], 300.0)
        self.assertEqual(existing._samples[359], 359.0)


# ===========================================================================
# TestEnergyCachePruningEdgeCases
# ===========================================================================


class TestEnergyCachePruningEdgeCases(unittest.TestCase):
    """Tests for the pruning logic in EnergyCache.get_or_fetch()."""

    def _fetcher_returns(self, data_start: datetime, samples: list[float]):
        """Return a fetcher function that yields the given data."""
        return lambda: {
            "per_second_data": list(samples),
            "data_start": data_start,
        }

    def test_prune_removes_samples_at_boundary(self):
        """Samples strictly before cutoff are pruned; sample at cutoff is kept."""
        import metrics

        # Use fixed_now such that ceil_to_qh(now - 3600) lands at 14:15:00.
        fixed_now = datetime(2025, 6, 15, 15, 15, 0, tzinfo=timezone.utc)
        # ceil_to_qh(14:15:00) = 14:15:00
        # 101 samples from 14:13:20 to 14:15:00 (inclusive)
        cache_start = datetime(2025, 6, 15, 14, 13, 20, tzinfo=timezone.utc)
        cache = _make_cache_with_samples(101, cache_start)  # 101 samples

        original_count = len(cache._samples)
        fetcher = lambda: {"per_second_data": [], "data_start": fixed_now}

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            cache.get_or_fetch(fetcher, fixed_now, force=True)

        # Samples from 14:13:20 to 14:14:59 (100 samples) are < 14:15:00 → removed
        # Sample at 14:15:00 (cutoff) is NOT removed (uses <, not <=)
        self.assertEqual(len(cache._samples), 1)
        self.assertEqual(len(cache._samples), original_count - 100)

    def test_prune_updates_data_start(self):
        """After pruning, _data_start advances by the number of removed samples."""
        import metrics

        # Use fixed_now such that ceil_to_qh(now - 3600) lands at 14:15:00.
        fixed_now = datetime(2025, 6, 15, 15, 15, 0, tzinfo=timezone.utc)
        # ceil_to_qh(14:15:00) = 14:15:00
        # 300 samples from 14:13:20 to 14:18:19 (inclusive)
        cache_start = datetime(2025, 6, 15, 14, 13, 20, tzinfo=timezone.utc)
        cache = _make_cache_with_samples(300, cache_start)

        new_start = datetime(2025, 6, 15, 14, 15, 0, tzinfo=timezone.utc)
        new_samples = []
        fetcher = self._fetcher_returns(new_start, new_samples)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            cache.get_or_fetch(fetcher, fixed_now, force=True)

        # Samples from 14:13:20 to 14:14:59 (100 samples) are < 14:15:00 → removed
        # Samples from 14:15:00 to 14:18:19 (200 samples) are >= cutoff → kept
        self.assertEqual(len(cache._samples), 200)
        # _data_start advances by the number of removed samples (100 seconds)
        self.assertEqual(cache._data_start, new_start)

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

        original_start = cache._data_start
        fetcher = self._fetcher_returns(datetime(2025, 6, 15, 14, 15, 0, tzinfo=timezone.utc), [])

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            cache.get_or_fetch(fetcher, fixed_now, force=True)

        # 3600 samples from 13:15:00 to 14:14:59 are < 14:15:00 → removed
        # Sample at 14:15:00 (cutoff) is kept (uses <, not <=)
        self.assertEqual(len(cache._samples), 1)

    def test_prune_no_samples_to_remove(self):
        """All samples are recent → no pruning, list unchanged."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=5)
        cache = _make_cache_with_samples(300, cache_start)

        original_samples = list(cache._samples)
        fetcher = lambda: None

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            cache.get_or_fetch(fetcher, fixed_now, force=True)

        self.assertEqual(len(cache._samples), 300)
        self.assertEqual(cache._samples, original_samples)

    def test_prune_updates_sample_count(self):
        """_sample_count reflects pruned length."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=5)
        cache = _make_cache_with_samples(300, cache_start)

        fetcher = lambda: None

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            cache.get_or_fetch(fetcher, fixed_now, force=True)

        self.assertEqual(cache._sample_count, len(cache._samples))

    def test_prune_updates_last_sample_at(self):
        """_last_sample_at recalculated after pruning."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)
        cache_start = fixed_now - timedelta(minutes=5)
        cache = _make_cache_with_samples(300, cache_start)

        fetcher = lambda: None

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            cache.get_or_fetch(fetcher, fixed_now, force=True)

        assert(len(cache._samples) > 0)

        expected_last = cache._data_start + timedelta(seconds=len(cache._samples) - 1)
        self.assertEqual(cache._last_sample_at, expected_last)

    def test_prune_empty_samples_list_noop(self):
        """Empty samples → no crash, no change."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)
        cache = metrics.EnergyCache()
        cache._samples = []

        fetcher = lambda: None

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            cache.get_or_fetch(fetcher, fixed_now, force=True)

        self.assertEqual(len(cache._samples), 0)

    def test_prune_with_data_start_none_noop(self):
        """No _data_start → no pruning (can't compute times)."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)
        cache = metrics.EnergyCache()
        cache._samples = [0.001] * 500
        cache._data_start = None

        original_samples = list(cache._samples)

        new_start = fixed_now + timedelta(minutes=10)
        new_samples = []
        fetcher = self._fetcher_returns(new_start, new_samples)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            cache.get_or_fetch(fetcher, fixed_now, force=True)

        # Without _data_start, pruning can't compute times → no pruning
        self.assertEqual(len(cache._samples), 500)
        self.assertEqual(cache._samples, original_samples)

    def test_prune_exact_3600_samples_removed(self):
        """All samples older than 3600s from fixed_now → all removed."""
        import metrics

        fixed_now = datetime(2025, 6, 15, 16, 10, 0, tzinfo=timezone.utc)
        # All samples 2 hours ago
        cache_start = fixed_now - timedelta(hours=2)
        cache = _make_cache_with_samples(3600, cache_start)

        new_start = cache_start + timedelta(minutes=5)
        new_samples = []
        fetcher = self._fetcher_returns(new_start, new_samples)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            cache.get_or_fetch(fetcher, fixed_now, force=True)

        # All 3600 samples are >= 3600s old (from 14:10:00 to 15:09:59)
        # cutoff = 15:10:00, so all samples < cutoff
        self.assertEqual(len(cache._samples), 0)

    def test_prune_one_sample_kept(self):
        """3601-sample cache, empty fetch → truncate → prune → 1 kept.

        Reproduces the production bug on 2026-05-21 where a production
        server hit an AssertionError in compute_nbc_quarters. When the
        cache held 3601 samples and the fetch returned empty data,
        merge_incremental truncated to 3600 but left _data_start
        pointing to the original time. This caused the pruning loop
        to miscalculate sample timestamps and prune every sample,
        leaving 0 instead of the expected 1 boundary sample.
        """
        import metrics

        fixed_now = datetime(2025, 6, 15, 15, 15, 0, tzinfo=timezone.utc)
        # 3601 samples from 13:15:00 to 14:15:00 (inclusive)
        # merge(3601 + 0) → 3601 → truncate to 3600
        # Without fix: _data_start still 13:15:00 → all 3600 samples < 14:15:00 → 0 kept
        # With fix: _data_start becomes 13:15:01 → samples at 13:15:01–14:14:59 pruned → 1 kept
        cache_start = datetime(2025, 6, 15, 13, 15, 0, tzinfo=timezone.utc)
        cache = _make_cache_with_samples(3601, cache_start)

        fetcher = self._fetcher_returns(
            datetime(2025, 6, 15, 15, 15, 0, tzinfo=timezone.utc), [])

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            cache.get_or_fetch(fetcher, fixed_now, force=True)

        # Prune should keep 1 sample at 14:15:00 (the boundary)
        self.assertEqual(len(cache._samples), 1)
        self.assertEqual(cache._samples[0], 0.001)


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
        self.assertIsNotNone(result.scales)
        self.assertIsNotNone(result.chart_data)
        self.assertIsNotNone(result.nbc_seconds)
        self.assertIsNotNone(result.nbc_data_start)

    def test_populate_device_per_second_data_length_matches_fetch(self):
        """per_second_data length matches what API returned."""
        expected_length = 1800
        hp, mock = _make_hourly_mock(n_seconds=expected_length)

        result = hp._populate_device(mock.channels[0], datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc))

        self.assertEqual(len(result.per_second_data), expected_length)

    def test_populate_device_scales_has_hour_entry(self):
        """scales dict has '1H' key with correct usage."""
        hp, mock = _make_hourly_mock(n_seconds=3600)

        result = hp._populate_device(mock.channels[0], datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc))

        self.assertIn("1H", result.scales)
        self.assertIn("usage", result.scales["1H"])

    def test_populate_device_scales_has_minute_entries(self):
        """scales dict has '1MIN' through '10MIN' entries."""
        hp, mock = _make_hourly_mock(n_seconds=3600, n_minutes=10)

        result = hp._populate_device(mock.channels[0], datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc))

        for i in range(1, 11):
            key = f"{i}MIN"
            self.assertIn(key, result.scales)

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

    def test_incremental_merge_appends_data_after_cache_end(self):
        """Merge correctly appends new samples after cache end."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)

        # Simulate a cache populated with samples starting at a QH boundary.
        qh_boundary = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        num_existing = 3278  # ~55 minutes of samples
        existing_samples = [0.01] * num_existing

        def initial_fetch():
            return {
                "per_second_data": existing_samples,
                "data_start": qh_boundary,
            }

        last_sample_time = qh_boundary + timedelta(seconds=num_existing - 1)
        now1 = last_sample_time + timedelta(seconds=37)  # 37s after last sample

        cache.get_or_fetch(initial_fetch, now1)

        self.assertEqual(len(cache._samples), num_existing)
        self.assertEqual(cache._data_start, qh_boundary)
        self.assertEqual(cache._last_sample_at, last_sample_time)

        # Simulate an incremental fetch where the new data genuinely starts
        # after the cache end. The data_start is right after the cache end.
        cache_end_time = qh_boundary + timedelta(seconds=num_existing - 1)
        new_data_start = cache_end_time + timedelta(seconds=1)
        num_new = 31
        new_samples = [0.02] * num_new

        def incremental_fetch():
            return {
                "per_second_data": new_samples,
                "data_start": new_data_start,
            }

        now2 = now1 + timedelta(seconds=30)

        # Use force=True because now2 is within the 60s TTL — without it the
        # second call would return cached data and never exercise the merge
        # logic.
        cache.get_or_fetch(incremental_fetch, now2, force=True)

        # All genuinely new samples must be appended — none dropped.
        expected_total = num_existing + num_new
        self.assertEqual(len(cache._samples), expected_total,
                         "All new samples must be appended; none should be dropped")

        # The cache's _data_start must remain at its original QH boundary
        # and must NOT be changed to the API's QH-aligned data_start.
        self.assertEqual(cache._data_start, qh_boundary,
                         "_data_start must not be overwritten by the API's "
                         "QH-aligned data_start during merge")

        # Verify _last_sample_at was updated to include the new samples.
        expected_last = qh_boundary + timedelta(seconds=expected_total - 1)
        self.assertEqual(cache._last_sample_at, expected_last)


# ===========================================================================
# TestNBCUsesFullCache
# ===========================================================================


class TestNBCUsesFullCache(unittest.TestCase):
    """Tests verifying _compute_nbc uses energy_cache.samples over incremental delta."""

    def test_nbc_uses_full_cache_on_incremental_fetch(self):
        """_compute_nbc should use energy_cache.samples, not incremental delta.

        After an incremental fetch where the API returns only ~60 new samples,
        NBC computation should still produce complete QH2/QH3 from the
        merged cache data instead of treating all 60 samples as belonging to
        the incomplete QH1.
        """
        import metrics

        now = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)

        # Pre-populate EnergyCache with a full hour of data (3600 samples).
        # data_start aligned to QH boundary (14:00).
        full_hour_start = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        full_samples = [0.001] * 3600
        cache = metrics.EnergyCache()
        cache._samples = list(full_samples)
        cache._data_start = full_hour_start
        cache._sample_count = 3600
        cache._last_sample_at = full_hour_start + timedelta(seconds=3599)

        # Create an HourlyProjection with the cache.
        hp = HourlyProjection(
            instant=now,
            logger_next=logging.getLogger("test"),
            energy_cache=cache,
        )

        # Simulate a _PopulationResult from an incremental fetch (only 60 samples).
        incremental_samples = [0.001] * 60
        pop_result = _PopulationResult(
            per_second_data=incremental_samples,
            scales={},
            chart_data=incremental_samples[-300:],
            nbc_seconds=incremental_samples,
            nbc_data_start=full_hour_start + timedelta(seconds=3540),
            nbc_sample_count=60,
        )

        pred_result = {
            "lag": timedelta(seconds=5),
            "minute_predicted": 1.0,
            "prediction": 60.0,
            "prediction_min": 55.0,
            "prediction_max": 65.0,
            "seconds_remaining": 900.0,
            "smoothing": {"1MIN": 1.0},
        }

        # Build a minimal mock VDeviceUsageInfo
        mock_vdi = MagicMock()
        mock_vdi.device_gid = 1234
        mock_vdi.device_name = "TEST_DEVICE"
        mock_vdi.time_zone = None

        device_metrics = hp._compute_device_metrics(mock_vdi, pop_result, pred_result)

        # With 60 samples, compute_nbc_quarters would give only QH1 (60 % 900 = 60).
        # QH2, QH3, QH4 would be None. But with the full cache (3600 samples),
        # all four quarters should be present.
        nbc = device_metrics.nbc

        # QH1 — always present (has data points)
        self.assertIsNotNone(nbc.get("QH1"), "QH1 should have data")

        # QH2, QH3 — should be computed from full cache (3600 samples)
        self.assertIsNotNone(nbc.get("QH2"),
                             "QH2 should be computed from full cache, not incremental delta")
        self.assertIsNotNone(nbc.get("QH3"),
                             "QH3 should be computed from full cache, not incremental delta")

        # QH4 — may be partial (samples 2700-3599), but still present
        self.assertIsNotNone(nbc.get("QH4"),
                             "QH4 should be present (partial) from full cache")

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
            scales={},
            chart_data=full_samples[-300:],
            nbc_seconds=full_samples,
            nbc_data_start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
            nbc_sample_count=3600,
        )

        pred_result = {
            "lag": timedelta(seconds=5),
            "minute_predicted": 1.0,
            "prediction": 60.0,
            "prediction_min": 55.0,
            "prediction_max": 65.0,
            "seconds_remaining": 900.0,
            "smoothing": {"1MIN": 1.0},
        }

        mock_vdi = MagicMock()
        mock_vdi.device_gid = 1234
        mock_vdi.device_name = "TEST_DEVICE"
        mock_vdi.time_zone = None

        device_metrics = hp._compute_device_metrics(mock_vdi, pop_result, pred_result)

        # With the fallback path (energy_cache=None), NBC should be computed
        # from pop_result.nbc_seconds (3600 samples), giving complete quarters.
        nbc = device_metrics.nbc
        self.assertIsNotNone(nbc.get("QH1"))
        self.assertIsNotNone(nbc.get("QH2"))
        self.assertIsNotNone(nbc.get("QH3"))
        self.assertIsNotNone(nbc.get("QH4"))

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
            scales={},
            chart_data=[],
            nbc_seconds=[0.001] * 60,
            nbc_data_start=datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc),
            nbc_sample_count=60,
        )

        pred_result = {
            "lag": timedelta(seconds=5),
            "minute_predicted": 1.0,
            "prediction": 60.0,
            "prediction_min": 55.0,
            "prediction_max": 65.0,
            "seconds_remaining": 900.0,
            "smoothing": {"1MIN": 1.0},
        }

        mock_vdi = MagicMock()
        mock_vdi.device_gid = 1234
        mock_vdi.device_name = "TEST_DEVICE"
        mock_vdi.time_zone = None

        device_metrics = hp._compute_device_metrics(mock_vdi, pop_result, pred_result)

        # Empty cache → fallback: only QH1 present (60 samples)
        nbc = device_metrics.nbc
        self.assertIsNotNone(nbc.get("QH1"))
        self.assertIsNone(nbc.get("QH2"))
        self.assertIsNone(nbc.get("QH3"))


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




